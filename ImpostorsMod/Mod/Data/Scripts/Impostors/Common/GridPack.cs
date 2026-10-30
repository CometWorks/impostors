using System;
using System.Collections.Generic;
using Sandbox.ModAPI;
using VRage.Game;

namespace Impostors
{
    // Grid impostors built in advance for one world, shipped in a mod of their own: the world's
    // own grids and every prefab of its mods. Grids spawned from a prefab get new entity ids, so
    // unlike the asteroid pack this one is keyed by content. The server takes a group's shapes
    // from it when the group hashes to an entry's input (see GridSource.Hash), so a grid somebody
    // built on or damaged misses and is built as usual. The client tells the server the pack's
    // Id, and when the server has the same pack it sends references to the shapes of the versions
    // in it instead of the shapes.
    //
    // File: magic, format, world name, long id, count, then per entry long input, int version,
    // byte level count, byte level and int length per level, and after the index the shapes in
    // the same order. The id is a hash of the index.
    public class GridPack
    {
        // Not .bin: the game leaves .bin files out when it publishes a mod
        public const string FileName = "ImpostorsGridPack.dat";
        public const int Magic = 0x50474d49; // "IMGP"
        public const byte Format = 1;

        private class Entry
        {
            public int Version;
            public byte[] Lods;
            public long[] Offsets;
            public int[] Lengths;
        }

        private readonly Dictionary<long, Entry> byInput = new Dictionary<long, Entry>();
        private readonly Dictionary<int, Entry> byVersion = new Dictionary<int, Entry>();
        private readonly MyObjectBuilder_Checkpoint.ModItem mod;
        private readonly object sync = new object();
        private long dataStart;

        // The whole file when the mod's file can't seek (a zipped mod), otherwise null
        private byte[] data;

        public string World { get; private set; }
        public long Id { get; private set; }
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
            get { return byInput.Count; }
        }
        public long Bytes { get; private set; }

        private GridPack(MyObjectBuilder_Checkpoint.ModItem mod)
        {
            this.mod = mod;
        }

        // The pack of the first of the session's mods that has one, or null
        public static GridPack Open()
        {
            foreach (var mod in MyAPIGateway.Session.Mods)
            {
                if (!MyAPIGateway.Utilities.FileExistsInModLocation(FileName, mod))
                    continue;
                var pack = new GridPack(mod);
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
                    throw new FormatException("not a grid pack");
                if (reader.ReadByte() != Format)
                    throw new FormatException("a grid pack of another version");
                World = reader.ReadString();
                Id = reader.ReadInt64();
                var count = reader.ReadInt32();
                long offset = 0;
                for (var i = 0; i < count; i++)
                {
                    var input = reader.ReadInt64();
                    var entry = new Entry { Version = reader.ReadInt32() };
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
                    }
                    byInput[input] = entry;
                    byVersion[entry.Version] = entry;
                }
                Bytes = offset;

                var stream = reader.BaseStream;
                if (stream.CanSeek)
                    dataStart = stream.Position;
                else
                    data = reader.ReadBytes((int)offset);
            }
        }

        // Server: this level of this version is in the pack
        public bool Has(int version, byte lod)
        {
            Entry entry;
            return byVersion.TryGetValue(version, out entry) && Array.IndexOf(entry.Lods, lod) >= 0;
        }

        // Client: one level of this version, or null
        public byte[] Get(int version, byte lod)
        {
            Entry entry;
            if (!byVersion.TryGetValue(version, out entry))
                return null;
            var k = Array.IndexOf(entry.Lods, lod);
            return k < 0 ? null : Read(entry, k, k)[0];
        }

        // Server: every level built from this content, coarsest first, or null
        public byte[][] Get(long input)
        {
            Entry entry;
            if (!byInput.TryGetValue(input, out entry))
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
