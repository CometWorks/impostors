using System;
using System.Collections.Generic;
using System.IO;
using Sandbox.ModAPI;
using VRage.Game;
using VRageMath;

namespace Impostors
{
    // Asteroid impostors built in advance for one world, shipped in a mod of their own that the
    // world lists, so Steam downloads it with the world's other mods. The server takes an
    // asteroid's shapes from it when the voxels are what the pack was built from (same content
    // hash), the client tells the server it has them in its Inventory and gets them from here
    // instead of over the network. A mined asteroid hashes differently and is built as before;
    // reset to its original voxels it hits the pack again.
    //
    // The client also shows the pack's asteroids before the server sends them (see
    // EarlyAsteroids), from the frame each entry has since format 2.
    //
    // File: magic, format, world name, count, then per asteroid long id, long content hash,
    // int version, byte complete, the frame (format 2: float radius, double position x, y, z,
    // float orientation x, y, z, w), byte level count, byte level and int length per level,
    // and after the index the shapes in the same order. "Complete" is 0 when levels were left
    // out of the pack: only clients can use such an entry, the server builds those asteroids.
    public class AsteroidPack
    {
        public const string FileName = "ImpostorsAsteroidPack.bin";
        public const int Magic = 0x50414d49; // "IMAP"
        public const byte Format = 2;

        private class Entry
        {
            public long Input;
            public int Version;
            public bool Complete;
            public byte[] Lods;
            public long[] Offsets;
            public int[] Lengths;
            public byte Mask;

            // Where the asteroid is, Id and Version filled in; Kind 0 in a format 1 pack
            public ImpostorState Frame;
        }

        private readonly Dictionary<long, Entry> entries = new Dictionary<long, Entry>();
        private readonly MyObjectBuilder_Checkpoint.ModItem mod;
        private readonly object sync = new object();
        private long dataStart;

        // The whole file when the mod's file can't seek (a zipped mod), otherwise null
        private byte[] data;

        public string World { get; private set; }
        public ulong ModId
        {
            get { return mod.PublishedFileId; }
        }
        public string ModName
        {
            get { return mod.Name; }
        }
        public int Count
        {
            get { return entries.Count; }
        }
        public long Bytes { get; private set; }

        private AsteroidPack(MyObjectBuilder_Checkpoint.ModItem mod)
        {
            this.mod = mod;
        }

        // The pack of the session's mods: of the mod with this Workshop id, or of the first one
        // that has a pack when 0. Null when there is none.
        public static AsteroidPack Open(ulong modId)
        {
            foreach (var mod in MyAPIGateway.Session.Mods)
            {
                if (modId != 0 && mod.PublishedFileId != modId)
                    continue;
                if (!MyAPIGateway.Utilities.FileExistsInModLocation(FileName, mod))
                    continue;
                var pack = new AsteroidPack(mod);
                pack.ReadIndex();
                return pack;
            }
            return null;
        }

        private void ReadIndex()
        {
            using (var reader = MyAPIGateway.Utilities.ReadBinaryFileInModLocation(FileName, mod))
            {
                if (reader.ReadInt32() != Magic)
                    throw new FormatException("not an asteroid pack");
                var format = reader.ReadByte();
                if (format != 1 && format != Format)
                    throw new FormatException("an asteroid pack of an unknown version");
                World = reader.ReadString();
                var count = reader.ReadInt32();
                long offset = 0;
                for (var i = 0; i < count; i++)
                {
                    var id = reader.ReadInt64();
                    var entry = new Entry
                    {
                        Input = reader.ReadInt64(),
                        Version = reader.ReadInt32(),
                        Complete = reader.ReadByte() == 1,
                    };
                    if (format >= 2)
                    {
                        entry.Frame = new ImpostorState
                        {
                            Id = id,
                            Kind = ImpostorKind.Voxel,
                            Version = entry.Version,
                            Radius = reader.ReadSingle(),
                            Position = new Vector3D(
                                reader.ReadDouble(),
                                reader.ReadDouble(),
                                reader.ReadDouble()
                            ),
                            Orientation = new Quaternion(
                                reader.ReadSingle(),
                                reader.ReadSingle(),
                                reader.ReadSingle(),
                                reader.ReadSingle()
                            ),
                        };
                    }
                    var levels = reader.ReadByte();
                    entry.Lods = new byte[levels];
                    entry.Offsets = new long[levels];
                    entry.Lengths = new int[levels];
                    for (var k = 0; k < levels; k++)
                    {
                        entry.Lods[k] = reader.ReadByte();
                        entry.Offsets[k] = offset;
                        entry.Lengths[k] = reader.ReadInt32();
                        offset += entry.Lengths[k];
                        entry.Mask |= Protocol.LodBit(entry.Lods[k]);
                    }
                    entries[id] = entry;
                }
                Bytes = offset;

                var stream = reader.BaseStream;
                if (stream.CanSeek)
                {
                    dataStart = stream.Position;
                }
                else
                {
                    data = reader.ReadBytes((int)offset);
                    dataStart = 0;
                }
            }
        }

        // Every asteroid's id, version and level mask, for the Inventory
        public void ForEach(Action<long, int, byte> visit)
        {
            foreach (var pair in entries)
                visit(pair.Key, pair.Value.Version, pair.Value.Mask);
        }

        // Client: every asteroid with a frame, its levels and their encoded sizes
        public void ForEachFrame(Action<ImpostorState, byte[], int[]> visit)
        {
            foreach (var entry in entries.Values)
            {
                if (entry.Frame.Kind == ImpostorKind.Voxel)
                    visit(entry.Frame, entry.Lods, entry.Lengths);
            }
        }

        // Client: one level of the asteroid's shapes of this version, or null
        public byte[] Get(long id, int version, byte lod)
        {
            Entry entry;
            if (!entries.TryGetValue(id, out entry) || entry.Version != version)
                return null;
            var k = Array.IndexOf(entry.Lods, lod);
            if (k < 0)
                return null;
            return Read(entry, k, k)[0];
        }

        // Server: every level of the asteroid built from this content, coarsest first, or null
        public byte[][] Get(long id, long input)
        {
            Entry entry;
            if (!entries.TryGetValue(id, out entry) || entry.Input != input || !entry.Complete)
                return null;
            return Read(entry, 0, entry.Lengths.Length - 1);
        }

        // Levels first to last, consecutive in the file
        private byte[][] Read(Entry entry, int first, int last)
        {
            var shapes = new byte[last - first + 1][];
            lock (sync)
            {
                if (data != null)
                {
                    for (var k = first; k <= last; k++)
                    {
                        shapes[k - first] = new byte[entry.Lengths[k]];
                        Array.Copy(data, entry.Offsets[k], shapes[k - first], 0, entry.Lengths[k]);
                    }
                    return shapes;
                }

                using (
                    var reader = MyAPIGateway.Utilities.ReadBinaryFileInModLocation(FileName, mod)
                )
                {
                    reader.BaseStream.Position = dataStart + entry.Offsets[first];
                    for (var k = first; k <= last; k++)
                        shapes[k - first] = reader.ReadBytes(entry.Lengths[k]);
                }
            }
            return shapes;
        }
    }
}
