using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Sandbox.Game.Entities;
using Shared.Logging;

namespace Shared.Impostors;

// Every entry of every source, and the background builds of their shapes
public sealed class ImpostorRegistry
{
    public const int DefaultMaxBuilds = 2;

    // Builds running at once, more while the asteroid pack is written
    public int MaxBuilds = DefaultMaxBuilds;

    // A build that produced nothing is retried after this long, seconds
    private const double RetrySeconds = 5;

    // An entry gone for this many scans whose entity no longer exists leaves the shape cache.
    // Not at once: while a world unloads its grids leave the scan before they are closed.
    private const int ForgetScans = 60;

    private readonly IPluginLogger log;
    private readonly IShapeCache cache;
    private readonly Dictionary<long, ImpostorEntry> entries = new();
    private readonly ConcurrentQueue<(
        ImpostorEntry entry,
        byte[][] shapes,
        ShapeBuild build
    )> finished = new();
    private readonly Dictionary<long, int> gone = new();
    private int running;
    private int scan;

    // Builds since the last TakeCounts: shapes built, taken from the cache or the asteroid
    // pack, and found unchanged by a refresh
    private int built;
    private int cached;
    private int packed;
    private int unchanged;

    // CPU time of the builds that reused shapes (the content hash), microseconds
    private long reusedCpu;

    public ImpostorRegistry(IPluginLogger log, IShapeCache cache)
    {
        this.log = log;
        this.cache = cache;
    }

    public IEnumerable<ImpostorEntry> Entries => entries.Values;

    public int Count => entries.Count;

    public void BeginScan() => scan++;

    // The entry of this id, created if new, marked as seen in the current scan
    public ImpostorEntry Touch(long id, ImpostorSource source)
    {
        if (!entries.TryGetValue(id, out var entry) || entry.Source != source)
        {
            entry = new ImpostorEntry(id, source);
            entries[id] = entry;
        }
        entry.LastSeen = scan;
        return entry;
    }

    // Drops the entries of this source the current scan has not seen
    public void EndScan(ImpostorSource source)
    {
        foreach (
            var id in entries
                .Values.Where(e => e.Source == source && e.LastSeen != scan)
                .Select(e => e.Id)
                .ToList()
        )
            Remove(id);
    }

    public void Remove(long id)
    {
        if (entries.TryGetValue(id, out var entry) && !entry.Source.Remote)
            gone[id] = scan;
        entries.Remove(id);
    }

    public (int built, int cached, int packed, int unchanged, double reusedCpuMs) TakeCounts()
    {
        var counts = (
            built,
            cached,
            packed,
            unchanged,
            Interlocked.Exchange(ref reusedCpu, 0) / 1000.0
        );
        built = cached = packed = unchanged = 0;
        return counts;
    }

    // Drops the cached shapes of deleted entities
    private void ForgetDeleted()
    {
        foreach (var (id, _) in gone.Where(g => scan - g.Value >= ForgetScans).ToList())
        {
            gone.Remove(id);
            if (!entries.ContainsKey(id) && !MyEntities.EntityExists(id))
                cache?.Forget(id);
        }
    }

    // Game thread: applies finished builds and starts new ones for the wanted entries
    public void Update(double now, double rebuildSeconds, double refreshSeconds)
    {
        ForgetDeleted();

        while (finished.TryDequeue(out var result))
        {
            var entry = result.entry;
            entry.Building = false;
            running--;
            if (result.shapes == null || !entries.ContainsKey(entry.Id))
                continue;

            // Remote shapes are fetched from another server, not built here
            if (!entry.Source.Remote)
            {
                if (result.build.Unchanged)
                    unchanged++;
                else if (result.build.Cached)
                    if (result.build.Packed)
                        packed++;
                    else
                        cached++;
                else
                    built++;
            }
            entry.Input = result.build.Input;
            var version = ImpostorEntry.Hash(result.shapes);
            if (version != entry.Version)
            {
                entry.Shapes = result.shapes;
                entry.Version = version;
            }
        }

        if (running >= MaxBuilds)
            return;

        // Missing shapes first, then the stale ones that waited longest
        var queue = entries
            .Values.Where(e =>
                e.Wanted
                && !e.Building
                && (
                    e.Shapes == null && now - e.LastBuild >= RetrySeconds
                    || (e.Stale || e.Viewed && now - e.LastBuild >= refreshSeconds)
                        && now - e.LastBuild >= rebuildSeconds
                )
            )
            .OrderBy(e => e.Shapes != null)
            .ThenBy(e => e.LastBuild)
            .Take(MaxBuilds - running)
            .ToList();

        foreach (var entry in queue)
        {
            entry.Stale = false;
            entry.LastBuild = now;

            Func<ShapeBuild, byte[][]> work;
            var snapshot = Stopwatch.StartNew();
            try
            {
                work = entry.Source.PrepareBuild(entry);
            }
            catch (Exception e)
            {
                log.Error(e, $"Failed to snapshot impostor {entry.Id}");
                continue;
            }

            if (work == null)
                continue;

            var snapshotMs = snapshot.Elapsed.TotalMilliseconds;
            var size = entry.Source.Describe(entry);
            var build = new ShapeBuild(entry, entry.Source.Remote ? null : cache);
            entry.Building = true;
            running++;
            Task.Run(() =>
            {
                byte[][] shapes = null;
                var timing = BuildTiming.Start();
                try
                {
                    shapes = work(build);
                }
                catch (Exception e)
                {
                    log.Error(e, $"Failed to build impostor {entry.Id}");
                }
                var cpu = timing.CpuMs;
                BuildTiming.Stop();

                var reused = build.Unchanged || build.Cached;
                if (reused && !double.IsNaN(cpu))
                    Interlocked.Add(ref reusedCpu, (long)(cpu * 1000));
                if (shapes != null && !entry.Source.Remote && !reused)
                {
                    // One line per build for tests/build_times.py
                    log.Info(
                        $"Impostor build: id={entry.Id} kind={entry.Kind} {size} snapshot={snapshotMs:0.00}{timing}"
                    );
                    if (build.Input != 0)
                        cache?.Put(entry.Id, entry.Kind, build.Input, shapes);
                }
                else if (shapes != null && build.Cached)
                {
                    // What a cache hit costs: the snapshot, the input hash and the read
                    var from = build.Packed ? "the asteroid pack" : "the cache";
                    log.Info(
                        $"Impostor from {from}: id={entry.Id} kind={entry.Kind} {size} snapshot={snapshotMs:0.00}{timing}"
                    );
                }
                finished.Enqueue((entry, shapes, build));
            });
        }
    }
}
