using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Shared.Logging;

namespace Shared.Impostors;

// Every entry of every source, and the background builds of their shapes
public sealed class ImpostorRegistry
{
    private const int MaxConcurrentBuilds = 2;

    // A build that produced nothing is retried after this long, seconds
    private const double RetrySeconds = 5;

    private readonly IPluginLogger log;
    private readonly Dictionary<long, ImpostorEntry> entries = new();
    private readonly ConcurrentQueue<(ImpostorEntry entry, byte[][] shapes)> finished = new();
    private int running;
    private int scan;

    public ImpostorRegistry(IPluginLogger log)
    {
        this.log = log;
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
            entries.Remove(id);
    }

    public void Remove(long id) => entries.Remove(id);

    // Game thread: applies finished builds and starts new ones for the wanted entries
    public void Update(double now, double rebuildSeconds, double refreshSeconds)
    {
        while (finished.TryDequeue(out var result))
        {
            var entry = result.entry;
            entry.Building = false;
            running--;
            if (result.shapes == null || !entries.ContainsKey(entry.Id))
                continue;

            var version = ImpostorEntry.Hash(result.shapes);
            if (version != entry.Version)
            {
                entry.Shapes = result.shapes;
                entry.Version = version;
            }
        }

        if (running >= MaxConcurrentBuilds)
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
            .Take(MaxConcurrentBuilds - running)
            .ToList();

        foreach (var entry in queue)
        {
            entry.Stale = false;
            entry.LastBuild = now;

            Func<byte[][]> build;
            try
            {
                build = entry.Source.PrepareBuild(entry);
            }
            catch (Exception e)
            {
                log.Error(e, $"Failed to snapshot impostor {entry.Id}");
                continue;
            }

            if (build == null)
                continue;

            entry.Building = true;
            running++;
            Task.Run(() =>
            {
                byte[][] shapes = null;
                try
                {
                    shapes = build();
                }
                catch (Exception e)
                {
                    log.Error(e, $"Failed to build impostor {entry.Id}");
                }
                finished.Enqueue((entry, shapes));
            });
        }
    }
}
