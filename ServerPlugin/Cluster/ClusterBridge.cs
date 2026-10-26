using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Impostors;
using PluginSdk.Clustering;
using Sandbox.Game.Entities;
using Sandbox.Game.World;
using Shared.Config;
using Shared.Impostors;
using Shared.Logging;

namespace ServerPlugin.Cluster;

// Far view across a cluster. A player sees only the grids of the node it is on, so each node
// reports its grid impostors to the World Authority, which merges them into an index and
// publishes it to every node. Nodes show the other nodes' entries through RemoteSource and
// fetch their shapes from the WA, which caches them. The WA keeps the records of grids whose
// partition went offline, those exist on no node at all.
//
// Reports and publications are deltas, the shape the cluster's AntennaGraphService settled
// on: full resends of a large index don't scale. A WA restart (new instance id) makes the
// nodes report everything again and reload the index.
//
// Asteroids are not reported: saved voxel maps are replicas on every node already.
//
// The WA's shape cache is the cluster's only one: it keeps the shapes on disk, and nodes ask it
// for shapes of unchanged content before building them (the bridge is the service's
// IShapeCache). The WA also saves its index, so the records of offline partitions' grids
// survive a WA restart.
public sealed class ClusterBridge : IShapeCache
{
    // The plugin's loader id, the manifest Id
    private const string PluginId = "66AA3D09-1732-445D-A2B6-A377B31AE76C";

    private const int IntervalTicks = 300;
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(10);

    // Records per message, under the SDK's 64 KiB payload limit with room for node names
    private const int RecordsPerMessage = 500;

    // An entry's shape levels cross the cluster packed into one blob (Pack) and split into
    // chunks under the 64 KiB payload limit: the finest level of a large asteroid or grid is
    // bigger than one message. The topics of these chunks are new names, so a node and a WA of
    // the older one-level-per-message plugin don't misread each other.
    private const int ChunkSize = 60 * 1024;
    private const int MaxPacked = 16 * 1024 * 1024;

    private const string TopicReport = "report";
    private const string TopicShape = "shape-chunk";
    private const string TopicShapeGet = "shape-get-chunk";
    private const string TopicIndex = "index";
    private const string TopicSnapshot = "snapshot";
    private const string TopicCacheGet = "cache-get-chunk";

    // The WA saves its index at most this often, seconds
    private const int IndexSaveSeconds = 60;
    private const int IndexMagic = 0x31584449; // "IDX1"

    private static readonly PluginTarget WorldAuthority = new()
    {
        Kind = PluginTargetKind.WorldAuthority,
    };

    public static ClusterBridge Instance { get; private set; }

    private readonly IPluginLogger log;
    private readonly IPluginConfig config;
    private readonly PluginClusterClient cluster;
    private readonly ConcurrentQueue<Action> gameThread = new();
    private readonly List<IDisposable> handlers = new();
    private int tick;
    private Task busy;

    // Node: what the WA has of this node, the partition each entry was in, shapes pushed
    private readonly Dictionary<long, ClusterRecord> acked = new();
    private readonly Dictionary<long, ulong> partitions = new();
    private readonly HashSet<(long, int)> pushed = new();
    private long knownWaInstance;

    // Node: since when (tick) it owns each partition. A partition owned for less than
    // SettledTicks may still be loading its grids, its report isn't complete.
    private readonly Dictionary<ulong, int> ownedSince = new();
    private const int SettledTicks = 30 * 60;

    // Node: the index as last published by the WA, id to (record, owner node; null when offline)
    public readonly Dictionary<long, (ClusterRecord record, string node)> Index = new();
    private long indexInstance;
    private long indexRevision;

    // WA: the merged index with each record's partition, the changes since the last
    // publication and the shape cache, in memory and on disk (store, null when disabled)
    private readonly long instance = DateTime.UtcNow.Ticks ^ Environment.TickCount;
    private long revision;
    private readonly Dictionary<long, (ClusterRecord record, string node, ulong partition)> merged =
        new();
    private readonly HashSet<long> changed = new();
    private readonly HashSet<long> removed = new();
    private readonly Dictionary<long, (int version, long input, byte[][] shapes)> shapes = new();
    private readonly Dictionary<
        long,
        (int version, long input, byte[] packed, bool[] got, int missing)
    > incoming = new();
    private DiskShapeCache store;
    private volatile bool storeOpened;
    private readonly object storeOpening = new();

    // The WA's store, opened on first use: the WA builds asteroids before its world is
    // ready, and a store opened only then missed those builds and their lookups
    private DiskShapeCache Store
    {
        get
        {
            if (!storeOpened)
            {
                lock (storeOpening)
                {
                    if (!storeOpened)
                    {
                        store = ImpostorService.OpenDiskCache(config, log);
                        storeOpened = true;
                    }
                }
            }
            return store;
        }
    }

    // WA: records loaded from the saved index that no node has reported since, and the index
    // saving state
    private readonly HashSet<long> unconfirmed = new();
    private bool indexLoaded;
    private bool indexDirty;
    private int lastIndexSave;
    private Task indexSave;

    private ClusterBridge(IPluginConfig config, IPluginLogger log)
    {
        this.config = config;
        this.log = log;
        cluster = PluginCluster.ForPlugin(PluginId);
    }

    public string Node => cluster.Context?.Node;

    private bool IsWorldAuthority => cluster.Context?.Role == "WorldAuthority";

    // Called from Init: does nothing outside a cluster
    public static void Start(IPluginConfig config, IPluginLogger log)
    {
        if (!PluginCluster.IsClusterProcess)
            return;

        try
        {
            Instance = new ClusterBridge(config, log);
        }
        catch (Exception e)
        {
            // Far view still works within each node
            log.Error(e, "no cluster bridge, impostors stay within each node");
            return;
        }

        ImpostorService.BuildAll = true;
        ImpostorService.Cache = Instance;
        ImpostorService.ExtraSources.Add(() => new RemoteSource(Instance));
        log.Info("cluster bridge started");
    }

    // Called on every game update
    public void Update()
    {
        while (gameThread.TryDequeue(out var action))
            action();

        if (++tick % IntervalTicks != 0 || PluginCluster.Current == null)
            return;

        if (handlers.Count == 0)
            RegisterHandlers();

        if (busy != null && !busy.IsCompleted)
            return;

        try
        {
            if (IsWorldAuthority)
            {
                LoadIndex();
                Publish();
                SaveIndex();
            }
            else if (indexInstance == 0)
                busy = LoadSnapshot(); // A node that started after the last publication
            else
                Report();
        }
        catch (Exception e)
        {
            log.Error(e, "cluster update failed");
        }
    }

    private void RegisterHandlers()
    {
        try
        {
            handlers.Add(cluster.RegisterHandler(TopicReport, OnReport));
            handlers.Add(cluster.RegisterHandler(TopicShape, OnShape));
            handlers.Add(cluster.RegisterHandler(TopicShapeGet, OnShapeGet));
            handlers.Add(cluster.RegisterHandler(TopicSnapshot, OnSnapshot));
            handlers.Add(cluster.RegisterHandler(TopicIndex, OnIndex));
            handlers.Add(cluster.RegisterHandler(TopicCacheGet, OnCacheGet));
        }
        catch (Exception e)
        {
            log.Warning(e, "cluster handlers not registered yet");
            foreach (var handler in handlers)
                handler.Dispose();
            handlers.Clear();
        }
    }

    // ---------------------------------------------------------------------------------------
    // Node side
    // ---------------------------------------------------------------------------------------

    private void Report()
    {
        var service = ImpostorService.Instance;
        if (service == null)
            return;

        var current = new Dictionary<long, ImpostorEntry>();
        var unbuilt = 0;
        foreach (var entry in service.Entries)
        {
            if (entry.Source.Remote)
                continue;

            // Grids, and the safe zones of blocks, which move between nodes with the block's
            // grid. Asteroids and admin safe zones are on every node already.
            var located = entry.Id;
            if (entry.Kind == ImpostorKind.SafeZone)
                located = SafeZoneSource.BlockGridId(entry);
            else if (entry.Kind != ImpostorKind.Grid)
                continue;
            if (located == 0)
                continue;
            if (entry.Version == 0)
            {
                unbuilt++;
                continue;
            }

            current[entry.Id] = entry;
            var partition = cluster.LocateEntity(located)?.Partition ?? 0;
            if (partition != 0)
                partitions[entry.Id] = partition;
        }

        var upserts = current
            .Values.Select(ClusterRecord.From)
            .Where(r => !acked.TryGetValue(r.Id, out var a) || a.Differs(r))
            .ToList();

        var owned = cluster.Context?.OwnedPartitions ?? new Dictionary<ulong, long>();
        foreach (var partition in ownedSince.Keys.Where(p => !owned.ContainsKey(p)).ToList())
            ownedSince.Remove(partition);
        foreach (var partition in owned.Keys.Where(p => !ownedSince.ContainsKey(p)).ToList())
            ownedSince[partition] = tick;
        // Gone from this node: released when its partition left the node (handover or offline),
        // so the WA keeps the record, deleted otherwise
        var gone = acked
            .Keys.Where(id => !current.ContainsKey(id))
            .Select(id =>
                (id, released: !partitions.TryGetValue(id, out var p) || !owned.ContainsKey(p))
            )
            .ToList();

        var newShapes = current
            .Values.Where(e => !pushed.Contains((e.Id, e.Version)))
            .Select(e => (e.Id, e.Version, e.Input, e.Shapes))
            .ToList();

        if (upserts.Count == 0 && gone.Count == 0 && newShapes.Count == 0)
            return;

        // With everything built and a WA that has all the rest, this report leaves the WA
        // with every record of the partitions this node owns: it drops the ones of its saved
        // index that are not among them, grids deleted while it was down
        var complete =
            knownWaInstance != 0 && unbuilt == 0
                ? ownedSince.Where(o => tick - o.Value >= SettledTicks).Select(o => o.Key).ToList()
                : null;
        var upsertPartitions = upserts
            .Select(r => partitions.TryGetValue(r.Id, out var p) ? p : 0)
            .ToList();
        busy = SendReport(upserts, upsertPartitions, gone, complete, newShapes);
    }

    private async Task SendReport(
        List<ClusterRecord> upserts,
        List<ulong> located,
        List<(long id, bool released)> gone,
        List<ulong> complete,
        List<(long id, int version, long input, byte[][] shapes)> newShapes
    )
    {
        // Shapes first, so the WA has them by the time other nodes learn about the versions
        foreach (var (id, version, input, entryShapes) in newShapes)
        {
            if (await PushShapes(id, version, input, entryShapes).ConfigureAwait(false))
                gameThread.Enqueue(() => pushed.Add((id, version)));
        }

        for (var start = 0; start == 0 || start < upserts.Count; start += RecordsPerMessage)
        {
            var batch = upserts.Skip(start).Take(RecordsPerMessage).ToList();
            var last = start + RecordsPerMessage >= upserts.Count;
            var batchGone = last ? gone : new List<(long, bool)>();

            var w = new ByteWriter(
                batch.Count * (ClusterRecord.Size + 8) + batchGone.Count * 9 + 64
            );
            w.Long(knownWaInstance);
            w.UShort((ushort)batch.Count);
            for (var i = 0; i < batch.Count; i++)
            {
                batch[i].Write(w);
                w.Long((long)located[start + i]);
            }
            w.UShort((ushort)batchGone.Count);
            foreach (var (id, released) in batchGone)
            {
                w.Long(id);
                w.Byte(released ? (byte)1 : (byte)0);
            }
            if (last && complete != null)
            {
                w.Byte(1);
                w.UShort((ushort)complete.Count);
                foreach (var partition in complete)
                    w.Long((long)partition);
            }
            else
            {
                w.Byte(0);
            }

            var result = await cluster
                .RequestAsync(WorldAuthority, TopicReport, w.ToArray(), Guid.NewGuid(), Timeout)
                .ConfigureAwait(false);
            if (result.Code != PluginResultCode.Success || result.Payload == null)
            {
                gameThread.Enqueue(() => log.Warning($"report to the WA failed: {result.Code}"));
                return;
            }

            var waInstance = new ByteReader(result.Payload).Long();
            gameThread.Enqueue(() =>
            {
                if (waInstance != knownWaInstance)
                {
                    // A new WA: it has nothing of ours, the next report sends everything
                    knownWaInstance = waInstance;
                    acked.Clear();
                    pushed.Clear();
                    return;
                }
                foreach (var r in batch)
                    acked[r.Id] = r;
                foreach (var (id, _) in batchGone)
                {
                    acked.Remove(id);
                    partitions.Remove(id);
                }
            });
        }
    }

    // Sends the shapes of an entry to the WA, all chunks at once
    private async Task<bool> PushShapes(long id, int version, long input, byte[][] entryShapes)
    {
        var packed = Pack(entryShapes);
        var sends = new List<Task<PluginResult>>();
        for (var offset = 0; offset < packed.Length; offset += ChunkSize)
        {
            var chunk = Slice(packed, offset);
            var w = new ByteWriter(chunk.Length + 32);
            w.Long(id);
            w.Int(version);
            w.Long(input);
            w.Int(packed.Length);
            w.Int(offset);
            w.Bytes(chunk);
            sends.Add(
                cluster.RequestAsync(
                    WorldAuthority,
                    TopicShape,
                    w.ToArray(),
                    Guid.NewGuid(),
                    Timeout
                )
            );
        }

        var results = await Task.WhenAll(sends).ConfigureAwait(false);
        var failed = results.FirstOrDefault(r => r.Code != PluginResultCode.Success);
        if (failed == null)
            return true;
        log.Warning(
            $"impostor {id}: pushing its shapes ({packed.Length} bytes) to the WA failed: {failed.Code}"
        );
        return false;
    }

    // An entry's levels as one blob: the level count, then each level's length (-1 for none)
    // and bytes, as in the disk cache's files
    private static byte[] Pack(byte[][] levels)
    {
        var w = new ByteWriter(levels.Sum(l => (l?.Length ?? 0) + 4) + 1);
        w.Byte((byte)levels.Length);
        foreach (var level in levels)
        {
            w.Int(level?.Length ?? -1);
            if (level != null)
                w.Bytes(level);
        }
        return w.ToArray();
    }

    private static byte[][] Unpack(byte[] packed)
    {
        var r = new ByteReader(packed);
        var levels = new byte[r.Byte()][];
        for (var i = 0; i < levels.Length; i++)
        {
            var length = r.Int();
            if (length > r.Remaining)
                throw new InvalidDataException($"level {i} of {length} bytes is cut off");
            levels[i] = length < 0 ? null : r.Bytes(length);
        }
        return levels;
    }

    // The chunk of a packed blob at this offset
    private static byte[] Slice(byte[] packed, int offset)
    {
        var chunk = new byte[Math.Min(ChunkSize, packed.Length - offset)];
        Buffer.BlockCopy(packed, offset, chunk, 0, chunk.Length);
        return chunk;
    }

    private Task<byte[]> OnIndex(PluginMessage message)
    {
        var r = new ByteReader(message.Payload);
        var waInstance = r.Long();
        var baseRevision = r.Long();
        var newRevision = r.Long();
        if (waInstance != indexInstance || baseRevision != indexRevision)
        {
            if (busy == null || busy.IsCompleted)
                busy = LoadSnapshot();
            return Task.FromResult<byte[]>(null);
        }

        ApplyIndex(r);
        indexRevision = newRevision;
        return Task.FromResult<byte[]>(null);
    }

    // Upserts with their node, then removed ids
    private void ApplyIndex(ByteReader r)
    {
        var count = r.UShort();
        for (var i = 0; i < count; i++)
        {
            var record = ClusterRecord.Read(r);
            var node = r.String();
            Index[record.Id] = (record, node.Length == 0 ? null : node);
        }
        count = r.UShort();
        for (var i = 0; i < count; i++)
            Index.Remove(r.Long());
    }

    private async Task LoadSnapshot()
    {
        var records = new List<(ClusterRecord, string)>();
        long waInstance = 0,
            snapshotRevision = 0;
        for (var offset = 0; ; offset += RecordsPerMessage)
        {
            var w = new ByteWriter(8);
            w.Int(offset);
            var result = await cluster
                .RequestAsync(WorldAuthority, TopicSnapshot, w.ToArray(), Guid.NewGuid(), Timeout)
                .ConfigureAwait(false);
            if (result.Code != PluginResultCode.Success || result.Payload == null)
                return;

            var r = new ByteReader(result.Payload);
            var pageInstance = r.Long();
            var pageRevision = r.Long();
            if (offset == 0)
            {
                waInstance = pageInstance;
                snapshotRevision = pageRevision;
            }
            else if (pageInstance != waInstance || pageRevision != snapshotRevision)
            {
                return; // Changed while paging, the next publication triggers another load
            }

            var total = r.Int();
            var count = r.UShort();
            for (var i = 0; i < count; i++)
            {
                var record = ClusterRecord.Read(r);
                var node = r.String();
                records.Add((record, node.Length == 0 ? null : node));
            }
            if (offset + count >= total)
                break;
        }

        gameThread.Enqueue(() =>
        {
            Index.Clear();
            foreach (var (record, node) in records)
                Index[record.Id] = (record, node);
            indexInstance = waInstance;
            indexRevision = snapshotRevision;
            log.Info($"cluster index loaded, {Index.Count} records, revision {indexRevision}");
        });
    }

    // Background thread: the shapes of an entry of another node
    public byte[][] FetchShapes(long id, int version) => Fetch(TopicShapeGet, id, version);

    // Background thread: the shapes the WA has of this id built from this input. The WA answers
    // its own builds from its store directly.
    public byte[][] Get(long id, long input) =>
        IsWorldAuthority ? Store?.Get(id, input) : Fetch(TopicCacheGet, id, input);

    // All levels of an entry from the WA, by version (shape-get) or input (cache-get), or null
    // when it doesn't have them. The first reply carries the packed length, the chunks after
    // it are asked for all at once, so an entry costs one round trip, two when it is larger
    // than a chunk.
    private byte[][] Fetch(string topic, long id, long key)
    {
        try
        {
            return FetchAsync(topic, id, key).GetAwaiter().GetResult();
        }
        catch (Exception e)
        {
            log.Warning($"impostor {id}: {topic} from the WA failed: {e.Message}");
            return null;
        }
    }

    private async Task<byte[][]> FetchAsync(string topic, long id, long key)
    {
        if (await RequestChunk(topic, id, key, 0).ConfigureAwait(false) is not { } first)
            return null;

        var packed = new byte[first.total];
        Buffer.BlockCopy(first.chunk, 0, packed, 0, first.chunk.Length);
        var rest = new List<Task<(int version, int total, byte[] chunk)?>>();
        for (var offset = ChunkSize; offset < first.total; offset += ChunkSize)
            rest.Add(RequestChunk(topic, id, key, offset));
        var chunks = await Task.WhenAll(rest).ConfigureAwait(false);
        for (var i = 0; i < chunks.Length; i++)
        {
            // The WA got another version in between
            if (chunks[i] is not { } c || c.version != first.version || c.total != first.total)
                return null;
            Buffer.BlockCopy(c.chunk, 0, packed, (i + 1) * ChunkSize, c.chunk.Length);
        }
        return Unpack(packed);
    }

    // Reply: int version, int packed length, the chunk at the offset; empty when the WA
    // doesn't have it
    private async Task<(int version, int total, byte[] chunk)?> RequestChunk(
        string topic,
        long id,
        long key,
        int offset
    )
    {
        var w = new ByteWriter(24);
        w.Long(id);
        w.Long(key);
        w.Int(offset);
        var reply = await cluster
            .RequestAsync(WorldAuthority, topic, w.ToArray(), Guid.NewGuid(), Timeout)
            .ConfigureAwait(false);
        if (reply.Code != PluginResultCode.Success)
            throw new IOException(reply.Code.ToString());
        if (reply.Payload is not { Length: > 8 } payload)
            return null;

        var r = new ByteReader(payload);
        var version = r.Int();
        var total = r.Int();
        var chunk = r.Bytes(r.Remaining);
        if (
            total <= 0
            || total > MaxPacked
            || offset >= total
            || chunk.Length != Math.Min(ChunkSize, total - offset)
        )
            throw new InvalidDataException($"chunk of {chunk.Length} bytes at {offset} of {total}");
        return (version, total, chunk);
    }

    // A node's grids reach the WA with its report. Asteroids and admin safe zones are on every
    // node and never reported, their shapes go to the WA here, so the next node finds them.
    public void Put(long id, ImpostorKind kind, long input, byte[][] built)
    {
        if (IsWorldAuthority)
            Store?.Put(id, kind, input, built);
        else if (kind != ImpostorKind.Grid)
            PushShapes(id, ImpostorEntry.Hash(built), input, built).GetAwaiter().GetResult();
    }

    public void Forget(long id)
    {
        if (IsWorldAuthority)
            Store?.Forget(id);
    }

    // ---------------------------------------------------------------------------------------
    // World Authority side
    // ---------------------------------------------------------------------------------------

    private Task<byte[]> OnReport(PluginMessage message)
    {
        var node = message.Source;
        var r = new ByteReader(message.Payload);
        var known = r.Long(); // The WA instance the node knows, a mismatch is answered by ours below

        var count = r.UShort();
        for (var i = 0; i < count; i++)
        {
            var record = ClusterRecord.Read(r);
            var partition = (ulong)r.Long();
            merged[record.Id] = (record, node, partition);
            changed.Add(record.Id);
            removed.Remove(record.Id);
            unconfirmed.Remove(record.Id);
        }

        count = r.UShort();
        for (var i = 0; i < count; i++)
        {
            var id = r.Long();
            var released = r.Byte() != 0;
            if (!merged.TryGetValue(id, out var current) || current.node != node)
                continue; // Another node reported it since

            if (released)
            {
                merged[id] = (current.record, null, current.partition);
                changed.Add(id);
            }
            else
            {
                Delete(id, true);
            }
        }

        // The node's partitions, when it reported every record of them
        if (r.Remaining > 0 && r.Byte() == 1)
        {
            var owned = new HashSet<ulong>();
            count = r.UShort();
            for (var i = 0; i < count; i++)
                owned.Add((ulong)r.Long());
            if (known == instance)
                DropUnconfirmed(owned);
        }

        var w = new ByteWriter(8);
        w.Long(instance);
        return Task.FromResult(w.ToArray());
    }

    // A deleted grid: gone from the index, and from the shape cache when a node said so
    private void Delete(long id, bool forgetShapes)
    {
        merged.Remove(id);
        shapes.Remove(id);
        incoming.Remove(id);
        if (forgetShapes)
            Store?.Forget(id);
        changed.Remove(id);
        unconfirmed.Remove(id);
        removed.Add(id);
    }

    // Records of the saved index in these partitions that their node didn't report
    private void DropUnconfirmed(HashSet<ulong> owned)
    {
        var drop = unconfirmed
            .Where(id => merged.TryGetValue(id, out var e) && owned.Contains(e.partition))
            .ToList();
        foreach (var id in drop)
            Delete(id, false);
        if (drop.Count > 0)
            log.Info($"cluster index: dropped {drop.Count} saved records no node has any more");
    }

    // A chunk of an entry's packed shapes. They count once all chunks are in; until then the
    // WA keeps answering with the version it had.
    private Task<byte[]> OnShape(PluginMessage message)
    {
        var r = new ByteReader(message.Payload);
        var id = r.Long();
        var version = r.Int();
        var input = r.Long();
        var total = r.Int();
        var offset = r.Int();
        var chunk = r.Bytes(r.Remaining);
        if (
            total <= 0
            || total > MaxPacked
            || offset < 0
            || offset % ChunkSize != 0
            || offset >= total
            || chunk.Length != Math.Min(ChunkSize, total - offset)
        )
            throw new InvalidDataException($"chunk of {chunk.Length} bytes at {offset} of {total}");

        // Both nodes push the asteroids they built
        if (shapes.TryGetValue(id, out var have) && have.version == version)
            return Task.FromResult(new byte[] { 1 });

        if (
            !incoming.TryGetValue(id, out var part)
            || part.version != version
            || part.packed.Length != total
        )
        {
            var count = (total + ChunkSize - 1) / ChunkSize;
            part = (version, input, new byte[total], new bool[count], count);
        }
        var index = offset / ChunkSize;
        if (!part.got[index])
        {
            part.got[index] = true;
            part.missing--;
            Buffer.BlockCopy(chunk, 0, part.packed, offset, chunk.Length);
        }
        if (part.missing > 0)
        {
            incoming[id] = part;
            return Task.FromResult(new byte[] { 1 });
        }

        incoming.Remove(id);
        var levels = Unpack(part.packed);
        shapes[id] = (version, part.input, levels);
        if (Store is { } disk)
            Task.Run(() => disk.Put(id, part.input, version, levels));
        return Task.FromResult(new byte[] { 1 });
    }

    // The shapes of this id in memory, or loaded from the store when they match
    private (int version, long input, byte[][] shapes)? Cached(
        long id,
        Func<(int version, long input), bool> match
    )
    {
        if (shapes.TryGetValue(id, out var cached) && match((cached.version, cached.input)))
            return cached;
        if (Store?.Read(id) is not { } stored || !match((stored.version, stored.input)))
            return null;
        cached = (stored.version, stored.input, stored.shapes);
        shapes[id] = cached;
        return cached;
    }

    // Request: long id, long version, int offset; reply as in RequestChunk
    private Task<byte[]> OnShapeGet(PluginMessage message)
    {
        var r = new ByteReader(message.Payload);
        var id = r.Long();
        var version = (int)r.Long();
        var offset = r.Int();
        return Task.FromResult(ChunkReply(Cached(id, c => c.version == version), offset));
    }

    // The same by input, for a node about to build the shapes
    private Task<byte[]> OnCacheGet(PluginMessage message)
    {
        var r = new ByteReader(message.Payload);
        var id = r.Long();
        var input = r.Long();
        var offset = r.Int();
        return Task.FromResult(ChunkReply(Cached(id, c => c.input == input), offset));
    }

    private static byte[] ChunkReply((int version, long input, byte[][] shapes)? cached, int offset)
    {
        if (cached is not { } c)
            return Array.Empty<byte>();
        var packed = Pack(c.shapes);
        if (offset < 0 || offset >= packed.Length)
            return Array.Empty<byte>();

        var chunk = Slice(packed, offset);
        var w = new ByteWriter(chunk.Length + 8);
        w.Int(c.version);
        w.Int(packed.Length);
        w.Bytes(chunk);
        return w.ToArray();
    }

    private Task<byte[]> OnSnapshot(PluginMessage message)
    {
        var offset = new ByteReader(message.Payload).Int();
        var page = merged
            .Values.OrderBy(e => e.record.Id)
            .Skip(offset)
            .Take(RecordsPerMessage)
            .ToList();
        var w = new ByteWriter(page.Count * (ClusterRecord.Size + 32) + 32);
        w.Long(instance);
        w.Long(revision);
        w.Int(merged.Count);
        w.UShort((ushort)page.Count);
        foreach (var (record, node, _) in page)
        {
            record.Write(w);
            w.String(node);
        }
        return Task.FromResult(w.ToArray());
    }

    // The saved index, once the world is loaded: its records stay offline until a node
    // reports them. Records nodes reported in the meantime are newer.
    private void LoadIndex()
    {
        if (indexLoaded || MySession.Static is not { Ready: true } session)
            return;
        indexLoaded = true;

        _ = Store;
        var path = IndexPath;
        if (!File.Exists(path))
            return;

        try
        {
            var r = new ByteReader(File.ReadAllBytes(path));
            if (r.Int() != IndexMagic || r.String() != session.CurrentPath)
            {
                log.Info("cluster index: the saved one is of another world, not loaded");
                return;
            }

            var count = r.Int();
            var loaded = 0;
            for (var i = 0; i < count; i++)
            {
                var record = ClusterRecord.Read(r);
                var partition = (ulong)r.Long();
                if (merged.ContainsKey(record.Id))
                    continue;
                merged[record.Id] = (record, null, partition);
                changed.Add(record.Id);
                unconfirmed.Add(record.Id);
                loaded++;
            }
            log.Info($"cluster index: loaded {loaded} saved records");
        }
        catch (Exception e)
        {
            log.Error(e, $"cluster index: failed to load {path}");
        }
    }

    // Writes the index on a background thread after it changed, at most every IndexSaveSeconds
    private void SaveIndex()
    {
        if (
            !indexDirty
            || tick - lastIndexSave < IndexSaveSeconds * 60
            || indexSave is { IsCompleted: false }
            || MySession.Static is not { } session
        )
            return;
        indexDirty = false;
        lastIndexSave = tick;

        var w = new ByteWriter(merged.Count * (ClusterRecord.Size + 8) + 1024);
        w.Int(IndexMagic);
        w.String(session.CurrentPath);
        w.Int(merged.Count);
        foreach (var (record, _, partition) in merged.Values)
        {
            record.Write(w);
            w.Long((long)partition);
        }

        var path = IndexPath;
        indexSave = Task.Run(() =>
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                File.WriteAllBytes(path + ".tmp", w.ToArray());
                if (File.Exists(path))
                    File.Delete(path);
                File.Move(path + ".tmp", path);
            }
            catch (Exception e)
            {
                gameThread.Enqueue(() => log.Warning(e, $"cluster index: failed to save {path}"));
            }
        });
    }

    private static string IndexPath => Path.Combine(ImpostorService.CacheFolder, "index.bin");

    // Sends the changes since the last publication to every node, as consecutive revisions
    private void Publish()
    {
        if (changed.Count == 0 && removed.Count == 0)
            return;

        var upserts = changed.Where(merged.ContainsKey).Select(id => merged[id]).ToList();
        var gone = removed.ToList();
        changed.Clear();
        removed.Clear();
        indexDirty = true;

        var messages = new List<byte[]>();
        for (var start = 0; start == 0 || start < upserts.Count; start += RecordsPerMessage)
        {
            var batch = upserts.Skip(start).Take(RecordsPerMessage).ToList();
            var batchGone = start + RecordsPerMessage >= upserts.Count ? gone : new List<long>();
            var w = new ByteWriter(
                batch.Count * (ClusterRecord.Size + 32) + batchGone.Count * 8 + 32
            );
            w.Long(instance);
            w.Long(revision);
            w.Long(++revision);
            w.UShort((ushort)batch.Count);
            foreach (var (record, node, _) in batch)
            {
                record.Write(w);
                w.String(node);
            }
            w.UShort((ushort)batchGone.Count);
            foreach (var id in batchGone)
                w.Long(id);
            messages.Add(w.ToArray());
        }

        busy = PublishMessages(messages);
    }

    private async Task PublishMessages(List<byte[]> messages)
    {
        foreach (var payload in messages)
        {
            var result = await cluster
                .BroadcastAsync(
                    TopicIndex,
                    payload,
                    Guid.NewGuid(),
                    Timeout,
                    includeWorldAuthority: false
                )
                .ConfigureAwait(false);
            if (result.Code != PluginResultCode.Success)
                gameThread.Enqueue(() => log.Debug($"index publication incomplete: {result.Code}"));
        }
    }
}
