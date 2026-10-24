using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Impostors;
using PluginSdk.Clustering;
using Sandbox.Game.Entities;
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
public sealed class ClusterBridge
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

    private static readonly PluginTarget WorldAuthority = new()
    {
        Kind = PluginTargetKind.WorldAuthority,
    };

    public static ClusterBridge Instance { get; private set; }

    private readonly IPluginLogger log;
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

    // Node: the index as last published by the WA, id to (record, owner node; null when offline)
    public readonly Dictionary<long, (ClusterRecord record, string node)> Index = new();
    private long indexInstance;
    private long indexRevision;

    // WA: the merged index, the changes since the last publication and the shape cache
    private readonly long instance = DateTime.UtcNow.Ticks ^ Environment.TickCount;
    private long revision;
    private readonly Dictionary<long, (ClusterRecord record, string node)> merged = new();
    private readonly HashSet<long> changed = new();
    private readonly HashSet<long> removed = new();
    private readonly Dictionary<long, (int version, byte[][] shapes)> shapes = new();

    private ClusterBridge(IPluginLogger log)
    {
        this.log = log;
        cluster = PluginCluster.ForPlugin(PluginId);
    }

    public string Node => cluster.Context?.Node;

    private bool IsWorldAuthority => cluster.Context?.Role == "WorldAuthority";

    // Called from Init: does nothing outside a cluster
    public static void Start(IPluginLogger log)
    {
        if (!PluginCluster.IsClusterProcess)
            return;

        try
        {
            Instance = new ClusterBridge(log);
        }
        catch (Exception e)
        {
            // Far view still works within each node
            log.Error(e, "no cluster bridge, impostors stay within each node");
            return;
        }

        ImpostorService.BuildAll = true;
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
                Publish();
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
        foreach (var entry in service.Entries)
        {
            if (entry.Source.Remote || entry.Version == 0)
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

            current[entry.Id] = entry;
            var partition = cluster.LocateEntity(located)?.Partition ?? 0;
            if (partition != 0)
                partitions[entry.Id] = partition;
        }

        var upserts = current
            .Values.Select(ClusterRecord.From)
            .Where(r => !acked.TryGetValue(r.Id, out var a) || a.Differs(r))
            .ToList();

        // Gone from this node: released when its partition left the node (handover or offline),
        // so the WA keeps the record, deleted otherwise
        var owned = cluster.Context?.OwnedPartitions ?? new Dictionary<ulong, long>();
        var gone = acked
            .Keys.Where(id => !current.ContainsKey(id))
            .Select(id =>
                (id, released: !partitions.TryGetValue(id, out var p) || !owned.ContainsKey(p))
            )
            .ToList();

        var newShapes = current
            .Values.Where(e => !pushed.Contains((e.Id, e.Version)))
            .Select(e => (e.Id, e.Version, e.Shapes))
            .ToList();

        if (upserts.Count == 0 && gone.Count == 0 && newShapes.Count == 0)
            return;

        busy = SendReport(upserts, gone, newShapes);
    }

    private async Task SendReport(
        List<ClusterRecord> upserts,
        List<(long id, bool released)> gone,
        List<(long id, int version, byte[][] shapes)> newShapes
    )
    {
        // Shapes first, so the WA has them by the time other nodes learn about the versions
        foreach (var (id, version, entryShapes) in newShapes)
        {
            var ok = true;
            for (var i = 0; i < entryShapes.Length && ok; i++)
            {
                if (entryShapes[i] == null)
                    break;
                var w = new ByteWriter(entryShapes[i].Length + 32);
                w.Long(id);
                w.Int(version);
                w.Byte((byte)i);
                w.Byte((byte)entryShapes.Count(s => s != null));
                w.Bytes(entryShapes[i]);
                var result = await cluster
                    .RequestAsync(WorldAuthority, TopicShape, w.ToArray(), Guid.NewGuid(), Timeout)
                    .ConfigureAwait(false);
                ok = result.Code == PluginResultCode.Success;
            }
            if (ok)
                gameThread.Enqueue(() => pushed.Add((id, version)));
        }

        for (var start = 0; start == 0 || start < upserts.Count; start += RecordsPerMessage)
        {
            var batch = upserts.Skip(start).Take(RecordsPerMessage).ToList();
            var last = start + RecordsPerMessage >= upserts.Count;
            var batchGone = last ? gone : new List<(long, bool)>();

            var w = new ByteWriter(batch.Count * ClusterRecord.Size + batchGone.Count * 9 + 32);
            w.Long(knownWaInstance);
            w.UShort((ushort)batch.Count);
            foreach (var r in batch)
                r.Write(w);
            w.UShort((ushort)batchGone.Count);
            foreach (var (id, released) in batchGone)
            {
                w.Long(id);
                w.Byte(released ? (byte)1 : (byte)0);
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

    // ---------------------------------------------------------------------------------------
    // World Authority side
    // ---------------------------------------------------------------------------------------

    private Task<byte[]> OnReport(PluginMessage message)
    {
        var node = message.Source;
        var r = new ByteReader(message.Payload);
        r.Long(); // The WA instance the node knows, a mismatch is answered by our instance below

        var count = r.UShort();
        for (var i = 0; i < count; i++)
        {
            var record = ClusterRecord.Read(r);
            merged[record.Id] = (record, node);
            changed.Add(record.Id);
            removed.Remove(record.Id);
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
                merged[id] = (current.record, null);
                changed.Add(id);
            }
            else
            {
                merged.Remove(id);
                shapes.Remove(id);
                changed.Remove(id);
                removed.Add(id);
            }
        }

        var w = new ByteWriter(8);
        w.Long(instance);
        return Task.FromResult(w.ToArray());
    }

    private Task<byte[]> OnShape(PluginMessage message)
    {
        var r = new ByteReader(message.Payload);
        var id = r.Long();
        var version = r.Int();
        var level = r.Byte();
        var levels = r.Byte();
        if (!shapes.TryGetValue(id, out var cached) || cached.version != version)
            cached = (version, new byte[levels][]);
        if (level < cached.shapes.Length)
            cached.shapes[level] = r.Bytes(r.Remaining);
        shapes[id] = cached;
        return Task.FromResult(new byte[] { 1 });
    }

    // Reply: byte level count, then the shape; empty when the WA doesn't have it
    private Task<byte[]> OnShapeGet(PluginMessage message)
    {
        var r = new ByteReader(message.Payload);
        var id = r.Long();
        var version = r.Int();
        var level = r.Byte();
        if (
            !shapes.TryGetValue(id, out var cached)
            || cached.version != version
            || level >= cached.shapes.Length
            || cached.shapes[level] == null
        )
            return Task.FromResult(Array.Empty<byte>());

        var w = new ByteWriter(cached.shapes[level].Length + 1);
        w.Byte((byte)cached.shapes.Length);
        w.Bytes(cached.shapes[level]);
        return Task.FromResult(w.ToArray());
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
        foreach (var (record, node) in page)
        {
            record.Write(w);
            w.String(node);
        }
        return Task.FromResult(w.ToArray());
    }

    // Sends the changes since the last publication to every node, as consecutive revisions
    private void Publish()
    {
        if (changed.Count == 0 && removed.Count == 0)
            return;

        var upserts = changed.Where(merged.ContainsKey).Select(id => merged[id]).ToList();
        var gone = removed.ToList();
        changed.Clear();
        removed.Clear();

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
            foreach (var (record, node) in batch)
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
