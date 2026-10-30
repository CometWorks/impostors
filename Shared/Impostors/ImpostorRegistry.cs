using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Impostors;
using Sandbox.Game.Entities;
using Shared.Logging;

namespace Shared.Impostors;

// Every entry of every source, and the background builds of their shapes
public sealed class ImpostorRegistry
{
    // A quarter of the cores, 2 to 4. An asteroid build takes 25 to 60 ms, so a ring seen for
    // the first time takes 2000 of them.
    public static readonly int DefaultMaxBuilds = Math.Max(
        2,
        Math.Min(4, Environment.ProcessorCount / 4)
    );

    // Builds running at once, more while the asteroid pack is written
    public int MaxBuilds = DefaultMaxBuilds;

    // A build that produced nothing is retried after this long, seconds
    private const double RetrySeconds = 5;

    // Snapshots taken on the game thread in one tick stop after this long, the next tick
    // starts the rest. A grid's can take 5 ms, an asteroid's 0.01 ms.
    private static readonly long SnapshotBudget = Stopwatch.Frequency * 2 / 1000;

    // An entry gone for this many scans whose entity no longer exists leaves the shape cache.
    // Not at once: while a world unloads its grids leave the scan before they are closed.
    private const int ForgetScans = 60;

    private readonly IPluginLogger log;
    private readonly IShapeCache cache;
    private readonly Dictionary<long, ImpostorEntry> entries = new();
    private readonly ConcurrentQueue<(
        ImpostorEntry entry,
        byte[][] shapes,
        ShapeBuild build,
        int version
    )> finished = new();
    private readonly Dictionary<long, int> gone = new();
    private int running;
    private int scan;

    // The entries due for a build at the last Plan, in the order they start in
    private List<ImpostorEntry> due = new();
    private int next;
    private double rebuildSeconds;
    private double refreshSeconds;

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

    // Game thread, after the picks: applies the finished builds, lists the entries due for a
    // build and starts the first ones
    public void Plan(double now, double rebuildSeconds, double refreshSeconds)
    {
        this.rebuildSeconds = rebuildSeconds;
        this.refreshSeconds = refreshSeconds;
        ForgetDeleted();
        Apply();

        // Missing shapes first, then the stale ones that waited longest
        due = entries
            .Values.Where(e => Due(e, now))
            .OrderBy(e => e.Shapes != null)
            .ThenBy(e => e.LastBuild)
            .ToList();
        next = 0;
        Start(now);
    }

    // Game thread, every tick: applies the finished builds and starts the next due ones in the
    // slots they freed, so a build that takes a few ms doesn't hold its slot until the next Plan
    public void Pump(double now)
    {
        if (finished.IsEmpty && (running >= MaxBuilds || next >= due.Count))
            return;
        Apply();
        Start(now);
    }

    private bool Due(ImpostorEntry e, double now) =>
        e.Wanted
        && !e.Building
        && (
            e.Shapes == null && now - e.LastBuild >= RetrySeconds
            || (e.Stale || e.Viewed && now - e.LastBuild >= refreshSeconds)
                && now - e.LastBuild >= rebuildSeconds
        );

    private void Apply()
    {
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
            if (result.version != entry.Version)
            {
                entry.Shapes = result.shapes;
                entry.Version = result.version;
            }
        }
    }

    private void Start(double now)
    {
        var start = Stopwatch.GetTimestamp();
        while (
            running < MaxBuilds
            && next < due.Count
            && Stopwatch.GetTimestamp() - start < SnapshotBudget
        )
        {
            // Gone, replaced, or no longer wanted since the Plan
            var entry = due[next++];
            if (
                entries.TryGetValue(entry.Id, out var current)
                && current == entry
                && Due(entry, now)
            )
                StartBuild(entry, now);
        }
    }

    private void StartBuild(ImpostorEntry entry, double now)
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
            return;
        }

        if (work == null)
            return;

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
                var from =
                    !build.Packed ? "the cache"
                    : entry.Kind == ImpostorKind.Voxel ? "the asteroid pack"
                    : "the grid pack";
                log.Info(
                    $"Impostor from {from}: id={entry.Id} kind={entry.Kind} {size} snapshot={snapshotMs:0.00}{timing}"
                );
            }
            // Hashed here, not on the game thread: an asteroid's shapes take about 0.2 ms
            var version = shapes != null ? ImpostorEntry.Hash(shapes) : 0;
            finished.Enqueue((entry, shapes, build, version));
        });
    }
}
