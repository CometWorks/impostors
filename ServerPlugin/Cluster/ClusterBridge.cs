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

    private const string TopicReport = "report";
    private const string TopicShape = "shape";
    private const string TopicShapeGet = "shape-get";
    private const string TopicIndex = "index";
    private const string TopicSnapshot = "snapshot";
    private const string TopicCacheGet = "cache-get";

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
    private DiskShapeCache store;

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

    // Sends the shapes of an entry to the WA, one message per level
    private async Task<bool> PushShapes(long id, int version, long input, byte[][] entryShapes)
    {
        for (var i = 0; i < entryShapes.Length; i++)
        {
            if (entryShapes[i] == null)
                break;
            var w = new ByteWriter(entryShapes[i].Length + 32);
            w.Long(id);
            w.Int(version);
            w.Long(input);
            w.Byte((byte)i);
            w.Byte((byte)entryShapes.Count(s => s != null));
            w.Bytes(entryShapes[i]);
            var result = await cluster
                .RequestAsync(WorldAuthority, TopicShape, w.ToArray(), Guid.NewGuid(), Timeout)
                .ConfigureAwait(false);
            if (result.Code != PluginResultCode.Success)
                return false;
        }
        return true;
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

    // Background thread: the shapes of an entry of another node, all levels or none
    public byte[][] FetchShapes(long id, int version)
    {
        var result = new List<byte[]>();
        for (var i = 0; i < ImpostorEntry.Lods.Length; i++)
        {
            var w = new ByteWriter(16);
            w.Long(id);
            w.Int(version);
            w.Byte((byte)i);
            var reply = cluster
                .RequestAsync(WorldAuthority, TopicShapeGet, w.ToArray(), Guid.NewGuid(), Timeout)
                .GetAwaiter()
                .GetResult();
            if (
                reply.Code != PluginResultCode.Success
                || reply.Payload == null
                || reply.Payload.Length < 2
            )
                return null;

            var r = new ByteReader(reply.Payload);
            var levels = r.Byte();
            result.Add(r.Bytes(r.Remaining));
            if (result.Count >= levels)
                break;
        }

        var shapes = new byte[ImpostorEntry.Lods.Length][];
        for (var i = 0; i < result.Count; i++)
            shapes[i] = result[i];
        return shapes;
    }

    // Background thread: the shapes the WA has of this id built from this input, all levels or
    // none. The WA answers its own builds from its store directly.
    public byte[][] Get(long id, long input)
    {
        if (IsWorldAuthority)
            return store?.Get(id, input);

        var result = new List<byte[]>();
        for (var i = 0; i < ImpostorEntry.Lods.Length; i++)
        {
            var w = new ByteWriter(24);
            w.Long(id);
            w.Long(input);
            w.Byte((byte)i);
            var reply = cluster
                .RequestAsync(WorldAuthority, TopicCacheGet, w.ToArray(), Guid.NewGuid(), Timeout)
                .GetAwaiter()
                .GetResult();
            if (
                reply.Code != PluginResultCode.Success
                || reply.Payload == null
                || reply.Payload.Length < 2
            )
                return null;

            var r = new ByteReader(reply.Payload);
            var levels = r.Byte();
            result.Add(r.Bytes(r.Remaining));
            if (result.Count >= levels)
                break;
        }

        var cached = new byte[ImpostorEntry.Lods.Length][];
        for (var i = 0; i < result.Count; i++)
            cached[i] = result[i];
        return cached;
    }

    // A node's grids reach the WA with its report. Asteroids and admin safe zones are on every
    // node and never reported, their shapes go to the WA here, so the next node finds them.
    public void Put(long id, ImpostorKind kind, long input, byte[][] built)
    {
        if (IsWorldAuthority)
            store?.Put(id, kind, input, built);
        else if (kind != ImpostorKind.Grid)
            PushShapes(id, ImpostorEntry.Hash(built), input, built).GetAwaiter().GetResult();
    }

    public void Forget(long id)
    {
        if (IsWorldAuthority)
            store?.Forget(id);
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
        if (forgetShapes)
            store?.Forget(id);
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

    private Task<byte[]> OnShape(PluginMessage message)
    {
        var r = new ByteReader(message.Payload);
        var id = r.Long();
        var version = r.Int();
        var input = r.Long();
        var level = r.Byte();
        var levels = r.Byte();
        if (!shapes.TryGetValue(id, out var cached) || cached.version != version)
            cached = (version, input, new byte[levels][]);
        if (level < cached.shapes.Length && cached.shapes[level] == null)
        {
            cached.shapes[level] = r.Bytes(r.Remaining);
            if (store != null && cached.shapes.All(s => s != null))
            {
                var complete = cached;
                Task.Run(() => store.Put(id, complete.input, complete.version, complete.shapes));
            }
        }
        shapes[id] = cached;
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
        if (store?.Read(id) is not { } stored || !match((stored.version, stored.input)))
            return null;
        cached = (stored.version, stored.input, stored.shapes);
        shapes[id] = cached;
        return cached;
    }

    // Reply: byte level count, then the shape; empty when the WA doesn't have it
    private Task<byte[]> OnShapeGet(PluginMessage message)
    {
        var r = new ByteReader(message.Payload);
        var id = r.Long();
        var version = r.Int();
        var level = r.Byte();
        return Task.FromResult(ShapeReply(Cached(id, c => c.version == version), level));
    }

    // The same by input, for a node about to build the shapes
    private Task<byte[]> OnCacheGet(PluginMessage message)
    {
        var r = new ByteReader(message.Payload);
        var id = r.Long();
        var input = r.Long();
        var level = r.Byte();
        return Task.FromResult(ShapeReply(Cached(id, c => c.input == input), level));
    }

    private static byte[] ShapeReply((int version, long input, byte[][] shapes)? cached, int level)
    {
        if (cached is not { } c || level >= c.shapes.Length || c.shapes[level] == null)
            return Array.Empty<byte>();

        var w = new ByteWriter(c.shapes[level].Length + 1);
        w.Byte((byte)c.shapes.Length);
        w.Bytes(c.shapes[level]);
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

        store = ImpostorService.OpenDiskCache(config, log);
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
