using System;
using System.Collections.Generic;
using System.Linq;
using Impostors;
using Sandbox.Definitions;
using Sandbox.Game.Entities;
using Sandbox.Game.World;
using Shared.Logging;
using VRage.Game;
using VRageMath;

namespace Shared.Impostors;

// Writes the world's grid pack: builds the world's own grids, then spawns the prefabs of the
// world's mods a batch at a time in empty space, through the game's prefab manager like an
// encounter mod does, so they hash and build as they would in play. The grid groups that come
// out are recorded and closed again. Run it on a copy of the world.
public sealed class GridPackWriter
{
    private const int BatchSize = 32;

    // Cubes of this side around a spawn point are free of anything else, meters
    private const double SpawnSpacing = 10000;

    // Ticks after the spawns before building: rotors and connectors attach in these
    private const int SettleTicks = 180;

    // Ticks to wait for the spawns, and for the builds, before going on without them
    private const int TimeoutTicks = 7200;

    private enum Step
    {
        Building,
        Spawning,
        Settling,
    }

    private readonly IPluginLogger log;
    private Queue<string> prefabs;
    private readonly HashSet<long> worldGrids = new();
    private readonly Dictionary<long, byte[][]> shapes = new();
    private readonly List<Vector3D> spots = new();
    private Step step = Step.Building;
    private int since;
    private int total;
    private int worldShapes;
    private bool worldDone;
    private int timeouts;

    public GridPackWriter(IPluginLogger log)
    {
        this.log = log;
    }

    // On the first update: while the service starts the world isn't all there yet
    private void Start()
    {
        var names = MyDefinitionManager
            .Static.GetPrefabDefinitions()
            .Where(p => p.Value.Context != null && !p.Value.Context.IsBaseGame)
            .Select(p => p.Key)
            .OrderBy(n => n, StringComparer.Ordinal)
            .ToList();
        prefabs = new Queue<string>(names);
        total = names.Count;
        foreach (var grid in MyEntities.GetEntities().OfType<MyCubeGrid>())
            worldGrids.Add(grid.EntityId);
        log.Info($"Grid pack: {worldGrids.Count} grids in the world, {total} prefabs in its mods");
    }

    // The grid entries should be built now
    public bool BuildGrids => step == Step.Building;

    // Once a second, after the registry's plan. True when the pack has been written.
    public bool Update(ImpostorRegistry registry, Func<long> write)
    {
        if (prefabs == null)
            Start();
        since += 60;
        switch (step)
        {
            case Step.Building:
                var grids = registry
                    .Entries.Where(e => e.Kind == ImpostorKind.Grid && !e.Source.Remote)
                    .ToList();
                if (
                    grids.Any(e => e.Shapes == null || e.Input == 0 || e.Building)
                    && since < TimeoutTicks
                )
                    return false;
                if (grids.Any(e => e.Shapes == null || e.Input == 0 || e.Building))
                    timeouts++;
                foreach (var e in grids.Where(e => e.Shapes != null && e.Input != 0))
                    shapes[e.Input] = e.Shapes;
                if (!worldDone)
                    worldShapes = shapes.Count;
                worldDone = true;
                CloseSpawned();
                if (prefabs.Count == 0)
                {
                    var bytes = write();
                    log.Info(
                        $"Grid pack written: {shapes.Count} shapes ({worldShapes} of the world's grids) from {total} prefabs, {bytes / 1024} KB, {timeouts} timeouts"
                    );
                    return true;
                }
                Spawn();
                return false;

            case Step.Spawning:
                if (MyPrefabManager.PendingGrids > 0 && since < TimeoutTicks)
                    return false;
                if (MyPrefabManager.PendingGrids > 0)
                    timeouts++;
                step = Step.Settling;
                since = 0;
                return false;

            case Step.Settling:
                if (since >= SettleTicks)
                {
                    step = Step.Building;
                    since = 0;
                }
                return false;
        }
        return false;
    }

    public IEnumerable<(long input, byte[][] shapes)> Shapes =>
        shapes.Select(s => (s.Key, s.Value));

    private void Spawn()
    {
        if (spots.Count == 0)
            FindSpots();
        for (var i = 0; i < BatchSize && prefabs.Count > 0; i++)
        {
            var name = prefabs.Dequeue();
            try
            {
                SwitchOff(name);
                MyPrefabManager.Static.SpawnPrefab(
                    name,
                    spots[i],
                    Vector3.Forward,
                    Vector3.Up,
                    callbacks: new Stack<Action>()
                );
            }
            catch (Exception e)
            {
                log.Error(e, $"Grid pack: failed to spawn prefab {name}");
            }
        }
        log.Info(
            $"Grid pack: {total - prefabs.Count} of {total} prefabs spawned, {shapes.Count} shapes"
        );
        step = Step.Spawning;
        since = 0;
    }

    // Nothing of a prefab needs to run for its shape, and some blocks don't survive it on a
    // server: a looping sound block that was saved playing crashes it (MySoundBlock has no
    // emitter there). The definition isn't used again, the world isn't played on after this.
    private static void SwitchOff(string prefab)
    {
        foreach (var grid in MyDefinitionManager.Static.GetPrefabDefinition(prefab).CubeGrids)
        foreach (var block in grid.CubeBlocks.OfType<MyObjectBuilder_FunctionalBlock>())
            block.Enabled = false;
    }

    // Points inside the world with nothing near them
    private void FindSpots()
    {
        var boxes = MyEntities
            .GetEntities()
            .Where(e => e.Parent == null && e.PositionComp != null)
            .Select(e => e.PositionComp.WorldAABB)
            .ToList();
        var km = MySession.Static.Settings.WorldSizeKm;
        var limit = km * 500.0 - SpawnSpacing;
        for (var r = 1; r < 100 && spots.Count < BatchSize; r++)
        for (var x = -r; x <= r && spots.Count < BatchSize; x++)
        for (var y = -r; y <= r && spots.Count < BatchSize; y++)
        for (var z = -r; z <= r && spots.Count < BatchSize; z++)
        {
            if (Math.Max(Math.Abs(x), Math.Max(Math.Abs(y), Math.Abs(z))) != r)
                continue;
            var spot = new Vector3D(x, y, z) * SpawnSpacing;
            if (
                (km == 0 || spot.AbsMax() < limit)
                && boxes.All(b => b.Distance(spot) > SpawnSpacing / 2)
            )
                spots.Add(spot);
        }
        if (spots.Count < BatchSize)
            throw new InvalidOperationException("Not enough empty space in the world");
    }

    private void CloseSpawned()
    {
        foreach (var grid in MyEntities.GetEntities().OfType<MyCubeGrid>().ToList())
        {
            if (!worldGrids.Contains(grid.EntityId))
                grid.Close();
        }
    }
}
