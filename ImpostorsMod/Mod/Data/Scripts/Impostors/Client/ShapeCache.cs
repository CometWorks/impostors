using System;
using System.Collections.Generic;
using System.IO;
using Sandbox.ModAPI;
using VRage.Utils;

namespace Impostors
{
    // The shapes the client got from servers, kept on disk so rejoining a server or meeting the
    // same far grids again costs no shapes for unchanged impostors (see Protocol.Inventory).
    // One file per world (by its name), encoded shapes keyed by impostor id, version and level.
    //
    // The server counts on the client having every shape it announced or got since, so nothing
    // is dropped during a session. Retention runs when a world's file is loaded and saved: an
    // id keeps only its last used version, entries unused for MaxDays go, and the files keep
    // under MaxBytes together, the least recently used world first, then the least recently
    // used entries.
    public class ShapeCache
    {
        public const long MaxBytes = 64L * 1024 * 1024;
        public const int MaxDays = 30;

        // A session that got this much starts over with a pruned cache, see TooLarge
        public const long SessionMaxBytes = 2 * MaxBytes;

        private const int Magic = 0x43504d49; // "IMPC"
        private const byte Format = 1;
        private const string IndexFile = "cache-index.txt";

        // Saved this long after the last new shape, and at least this often while they come
        private static readonly TimeSpan SaveQuiet = TimeSpan.FromSeconds(30);
        private static readonly TimeSpan SaveEvery = TimeSpan.FromMinutes(10);

        private class Entry
        {
            public long Id;
            public int Version;
            public byte Lod;
            public int Day;
            public long Order;
            public byte[] Data;
        }

        private readonly bool localStorage;
        private readonly Dictionary<long, List<Entry>> entries =
            new Dictionary<long, List<Entry>>();
        private string file;
        private long order;
        private bool dirty;
        private bool saving;

        // The background save and the one at the end of the session write the same file
        private readonly object writing = new object();
        private DateTime changed;
        private DateTime saved = DateTime.UtcNow;

        public long Bytes { get; private set; }

        public bool Loaded
        {
            get { return file != null; }
        }

        public bool TooLarge
        {
            get { return Bytes > SessionMaxBytes; }
        }

        // The mod writes into its local storage, the client plugin's copy into the global one
        public ShapeCache(bool localStorage)
        {
            this.localStorage = localStorage;
        }

        private static int Today
        {
            get { return (int)(DateTime.UtcNow.Ticks / TimeSpan.TicksPerDay); }
        }

        public void Load(string world)
        {
            file = "cache-" + Fnv(world).ToString("x16") + ".bin";
            entries.Clear();
            Bytes = 0;
            try
            {
                if (Exists(file))
                    Read();
            }
            catch (Exception e)
            {
                MyLog.Default.WriteLineAndConsole(
                    "Impostors: dropped the shape cache " + file + ": " + e.Message
                );
                entries.Clear();
                Bytes = 0;
            }
            Prune();
            try
            {
                UpdateIndex();
            }
            catch (Exception e)
            {
                MyLog.Default.WriteLineAndConsole(
                    "Impostors: failed to update the shape cache index: " + e.Message
                );
            }
        }

        // Ids, versions and level masks for the Inventory message, with those of the asteroid
        // pack if there is one; how many it had
        public int Inventory(ByteWriter writer, Action send, AsteroidPack pack)
        {
            var masks = new Dictionary<long, Dictionary<int, byte>>();
            if (pack != null)
            {
                pack.ForEach(
                    (id, version, mask) =>
                    {
                        var versions = new Dictionary<int, byte>();
                        versions[version] = mask;
                        masks[id] = versions;
                    }
                );
            }
            foreach (var list in entries.Values)
            {
                foreach (var entry in list)
                {
                    Dictionary<int, byte> versions;
                    if (!masks.TryGetValue(entry.Id, out versions))
                        masks[entry.Id] = versions = new Dictionary<int, byte>();
                    byte mask;
                    versions.TryGetValue(entry.Version, out mask);
                    versions[entry.Version] = (byte)(mask | Protocol.LodBit(entry.Lod));
                }
            }

            var count = 0;
            var total = 0;
            var countPosition = 0;
            Action start = () =>
            {
                writer.Clear();
                writer.Byte(Protocol.Inventory);
                writer.Byte(1);
                countPosition = writer.Length;
                writer.UShort(0);
                count = 0;
            };
            start();
            foreach (var id in masks)
            {
                foreach (var version in id.Value)
                {
                    if (writer.Length + 13 > Protocol.MaxMessageSize || count == ushort.MaxValue)
                    {
                        writer.UShortAt(countPosition, (ushort)count);
                        send();
                        start();
                    }
                    writer.Long(id.Key);
                    writer.Int(version.Key);
                    writer.Byte(version.Value);
                    count++;
                    total++;
                }
            }
            writer.UShortAt(countPosition, (ushort)count);
            writer.ByteAt(1, 0);
            send();
            return total;
        }

        public byte[] Get(long id, int version, byte lod)
        {
            var entry = Find(id, version, lod);
            if (entry == null)
                return null;
            entry.Day = Today;
            entry.Order = order++;
            return entry.Data;
        }

        public void Add(long id, int version, byte lod, byte[] data)
        {
            var entry = Find(id, version, lod);
            if (entry == null)
            {
                List<Entry> list;
                if (!entries.TryGetValue(id, out list))
                    entries[id] = list = new List<Entry>();
                entry = new Entry
                {
                    Id = id,
                    Version = version,
                    Lod = lod,
                };
                list.Add(entry);
            }
            else
            {
                Bytes -= entry.Data.Length;
            }
            entry.Data = data;
            entry.Day = Today;
            entry.Order = order++;
            Bytes += data.Length;
            Changed();
        }

        // Retention, only where the server doesn't count on the entries: on load, on save at
        // the end of the session, and for a session that grew over SessionMaxBytes
        public void Prune()
        {
            var oldest = Today - MaxDays;
            var before = Count;
            var kept = new List<Entry>();
            foreach (var list in entries.Values)
            {
                // The version used last, its levels stay
                Entry last = null;
                foreach (var entry in list)
                {
                    if (last == null || entry.Order > last.Order)
                        last = entry;
                }
                foreach (var entry in list)
                {
                    if (entry.Version == last.Version && entry.Day >= oldest)
                        kept.Add(entry);
                }
            }

            kept.Sort((a, b) => b.Order.CompareTo(a.Order));
            entries.Clear();
            Bytes = 0;
            foreach (var entry in kept)
            {
                if (Bytes + entry.Data.Length > MaxBytes)
                    continue;
                List<Entry> list;
                if (!entries.TryGetValue(entry.Id, out list))
                    entries[entry.Id] = list = new List<Entry>();
                list.Add(entry);
                Bytes += entry.Data.Length;
            }
            if (Count != before)
                Changed();
        }

        private int Count
        {
            get
            {
                var count = 0;
                foreach (var list in entries.Values)
                    count += list.Count;
                return count;
            }
        }

        private void Changed()
        {
            dirty = true;
            changed = DateTime.UtcNow;
        }

        // On a background thread, without pruning: once no new shape came for SaveQuiet, or
        // SaveEvery while they keep coming. A session that ends without unloading, like a
        // crash, loses only the last few seconds.
        public void Update()
        {
            if (!Loaded || !dirty || saving)
                return;
            var now = DateTime.UtcNow;
            if (now - changed < SaveQuiet && now - saved < SaveEvery)
                return;
            saved = now;
            var data = Serialize();
            var name = file;
            saving = true;
            dirty = false;
            MyAPIGateway.Parallel.StartBackground(() =>
            {
                try
                {
                    Write(name, data);
                }
                catch (Exception e)
                {
                    MyLog.Default.WriteLineAndConsole(
                        "Impostors: failed to save the shape cache: " + e.Message
                    );
                }
                saving = false;
            });
        }

        // At the end of the session: prune, save, and drop the files of the worlds used least
        // recently while all of them are over MaxBytes together
        public void Save()
        {
            if (!Loaded)
                return;
            try
            {
                Prune();
                if (dirty || !Exists(file))
                    Write(file, Serialize());
                dirty = false;
                UpdateIndex();
            }
            catch (Exception e)
            {
                MyLog.Default.WriteLineAndConsole(
                    "Impostors: failed to save the shape cache: " + e.Message
                );
            }
        }

        public void Clear()
        {
            entries.Clear();
            Bytes = 0;
            file = null;
        }

        private Entry Find(long id, int version, byte lod)
        {
            List<Entry> list;
            if (!entries.TryGetValue(id, out list))
                return null;
            foreach (var entry in list)
            {
                if (entry.Version == version && entry.Lod == lod)
                    return entry;
            }
            return null;
        }

        // Magic, format, count, entries (long id, int version, byte level, int day, int length,
        // shape), all inside a length prefix and followed by a checksum of it
        private byte[] Serialize()
        {
            var w = new ByteWriter((int)Math.Min(Bytes + 1024, int.MaxValue / 2));
            w.Int(Magic);
            w.Byte(Format);
            w.Int(Count);
            var list = new List<Entry>();
            foreach (var l in entries.Values)
                list.AddRange(l);

            // Oldest use first, so the order survives the round trip
            list.Sort((a, b) => a.Order.CompareTo(b.Order));
            foreach (var entry in list)
            {
                w.Long(entry.Id);
                w.Int(entry.Version);
                w.Byte(entry.Lod);
                w.Int(entry.Day);
                w.Int(entry.Data.Length);
                w.Bytes(entry.Data);
            }
            return w.ToArray();
        }

        private void Read()
        {
            byte[] data;
            int checksum;
            using (var reader = OpenRead(file))
            {
                var length = reader.ReadInt32();
                data = reader.ReadBytes(length);
                checksum = reader.ReadInt32();
            }
            if (Checksum(data) != checksum)
                throw new FormatException("checksum mismatch");

            var r = new ByteReader(data);
            if (r.Int() != Magic || r.Byte() != Format)
                throw new FormatException("unknown format");
            var count = r.Int();
            for (var i = 0; i < count; i++)
            {
                var entry = new Entry
                {
                    Id = r.Long(),
                    Version = r.Int(),
                    Lod = r.Byte(),
                    Day = r.Int(),
                    Order = order++,
                };
                entry.Data = r.Bytes(r.Int());
                List<Entry> list;
                if (!entries.TryGetValue(entry.Id, out list))
                    entries[entry.Id] = list = new List<Entry>();
                list.Add(entry);
                Bytes += entry.Data.Length;
            }
        }

        private void Write(string name, byte[] data)
        {
            lock (writing)
                WriteFile(name, data);
        }

        private void WriteFile(string name, byte[] data)
        {
            using (var writer = OpenWrite(name))
            {
                writer.Write(data.Length);
                writer.Write(data);
                writer.Write(Checksum(data));
            }
        }

        // cache-index.txt: a line per world's file with its bytes and the day it was used last
        private void UpdateIndex()
        {
            var worlds = new List<string[]>();
            if (Exists(IndexFile))
            {
                using (var reader = OpenReadText(IndexFile))
                {
                    string line;
                    while ((line = reader.ReadLine()) != null)
                    {
                        var parts = line.Split(' ');
                        if (parts.Length == 3 && parts[0] != file)
                            worlds.Add(parts);
                    }
                }
            }
            worlds.Add(new[] { file, Bytes.ToString(), Today.ToString() });

            // The current world last, it was just used
            worlds.Sort(
                (a, b) =>
                    a[0] == file ? 1
                    : b[0] == file ? -1
                    : int.Parse(a[2]).CompareTo(int.Parse(b[2]))
            );
            long total = 0;
            foreach (var world in worlds)
                total += long.Parse(world[1]);
            while (total > MaxBytes && worlds.Count > 1)
            {
                total -= long.Parse(worlds[0][1]);
                Delete(worlds[0][0]);
                worlds.RemoveAt(0);
            }

            using (var writer = OpenWriteText(IndexFile))
            {
                foreach (var world in worlds)
                    writer.WriteLine(world[0] + " " + world[1] + " " + world[2]);
            }
        }

        private static int Checksum(byte[] data)
        {
            var hash = 2166136261;
            foreach (var b in data)
                hash = (hash ^ b) * 16777619;
            return (int)hash;
        }

        private static long Fnv(string text)
        {
            var hash = 14695981039346656037;
            foreach (var c in text ?? "")
                hash = (hash ^ c) * 1099511628211;
            return (long)hash;
        }

        // The mod's local storage, or the global one with the client plugin's prefix
        private string Global(string name)
        {
            return "Impostors." + name;
        }

        private bool Exists(string name)
        {
            return localStorage
                ? MyAPIGateway.Utilities.FileExistsInLocalStorage(name, typeof(ShapeCache))
                : MyAPIGateway.Utilities.FileExistsInGlobalStorage(Global(name));
        }

        private BinaryReader OpenRead(string name)
        {
            return localStorage
                ? MyAPIGateway.Utilities.ReadBinaryFileInLocalStorage(name, typeof(ShapeCache))
                : MyAPIGateway.Utilities.ReadBinaryFileInGlobalStorage(Global(name));
        }

        private BinaryWriter OpenWrite(string name)
        {
            return localStorage
                ? MyAPIGateway.Utilities.WriteBinaryFileInLocalStorage(name, typeof(ShapeCache))
                : MyAPIGateway.Utilities.WriteBinaryFileInGlobalStorage(Global(name));
        }

        private TextReader OpenReadText(string name)
        {
            return localStorage
                ? MyAPIGateway.Utilities.ReadFileInLocalStorage(name, typeof(ShapeCache))
                : MyAPIGateway.Utilities.ReadFileInGlobalStorage(Global(name));
        }

        private TextWriter OpenWriteText(string name)
        {
            return localStorage
                ? MyAPIGateway.Utilities.WriteFileInLocalStorage(name, typeof(ShapeCache))
                : MyAPIGateway.Utilities.WriteFileInGlobalStorage(Global(name));
        }

        private void Delete(string name)
        {
            if (!Exists(name))
                return;
            if (localStorage)
                MyAPIGateway.Utilities.DeleteFileInLocalStorage(name, typeof(ShapeCache));
            else
                MyAPIGateway.Utilities.DeleteFileInGlobalStorage(Global(name));
        }
    }
}
