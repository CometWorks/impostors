using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using HarmonyLib;
using PluginSdk.Commands;
using Sandbox.Engine.Multiplayer;
using Sandbox.Engine.Networking;
using Sandbox.Game.Entities;
using Sandbox.ModAPI;
using VRage.FileSystem;
using VRage.Game;
using VRage.Game.ModAPI;
using VRage.GameServices;
using VRage.ModAPI;
using VRage.Plugins;
using VRage.Utils;
using VRage.Voxels;
using VRageMath;

namespace ImpostorsProbe;

// Test hooks of the Impostors suite: the service's state on the server, moving grids and
// players, and loading the world's mods from the server's Mods folder instead of the Workshop.
// Reaches Impostors by reflection, so it needs no build dependency on it.
public class Probe : IPlugin
{
    public void Init(object gameInstance)
    {
        new Harmony("ImpostorsProbe").PatchAll(Assembly.GetExecutingAssembly());
        ServerCommands.Register(Assembly.GetExecutingAssembly(), typeof(ProbeCommands));
    }

    private bool dumped;

    // IMP_VOXEL_DUMP=<folder> writes the content of every IMP_VOXEL_DUMP_EVERY-th asteroid
    // (by entity id, default 20) once the session is ready, for tests/asteroid_levels.py
    public void Update()
    {
        if (dumped || Sandbox.Game.World.MySession.Static?.Ready != true)
            return;
        dumped = true;
        var folder = Environment.GetEnvironmentVariable("IMP_VOXEL_DUMP");
        if (!string.IsNullOrEmpty(folder))
            VoxelDump.Write(folder, Environment.GetEnvironmentVariable("IMP_VOXEL_DUMP_EVERY"));
    }

    // What the Impostors service left behind once the plugins are disposed (tests/shutdown.py)
    public void Dispose()
    {
        var type = AppDomain
            .CurrentDomain.GetAssemblies()
            .Select(a => a.GetType("Shared.Impostors.ImpostorService"))
            .FirstOrDefault(t => t != null);
        var instance = type?.GetProperty("Instance")?.GetValue(null);
        var session = type
            ?.GetField("session", BindingFlags.NonPublic | BindingFlags.Static)
            ?.GetValue(null);
        MyLog.Default.WriteLineAndConsole(
            $"ImpostorsProbe: disposed, instance={(instance == null ? "null" : "set")} session={(session == null ? "null" : "set")}"
        );
    }
}

// An asteroid's content at every voxel level of detail from the one 128 voxels across (or
// level 0) up to the one of 2 voxels: per asteroid <entity id>.vxd, int size X, Y, Z, int
// level count, then per level int lod and the content bytes, X fastest, then Y, then Z
public static class VoxelDump
{
    public static void Write(string folder, string every)
    {
        var step = int.TryParse(every, out var n) && n > 0 ? n : 20;
        Directory.CreateDirectory(folder);
        var maps = MyEntities
            .GetEntities()
            .OfType<MyVoxelMap>()
            .Where(m => m.Storage != null)
            .OrderBy(m => m.EntityId)
            .ToList();
        var written = 0;
        for (var i = 0; i < maps.Count; i += step)
        {
            var storage = maps[i].Storage;
            var size = storage.Size;
            var first = Math.Max(0, (int)Math.Ceiling(Math.Log(size.AbsMax() / 128.0, 2)));
            var last = Math.Max(first, (int)Math.Floor(Math.Log(size.AbsMax() / 2.0, 2)));
            using var file = new BinaryWriter(
                File.Create(Path.Combine(folder, $"{maps[i].EntityId}.vxd"))
            );
            file.Write(size.X);
            file.Write(size.Y);
            file.Write(size.Z);
            file.Write(last - first + 1);
            for (var lod = first; lod <= last; lod++)
            {
                var voxels = Vector3I.Max(Vector3I.One, size >> lod);
                var data = new MyStorageData();
                data.Resize(voxels);
                storage.ReadRange(
                    data,
                    MyStorageDataTypeFlags.Content,
                    lod,
                    Vector3I.Zero,
                    voxels - 1
                );
                file.Write(lod);
                file.Write(data[MyStorageDataTypeEnum.Content], 0, data.SizeLinear);
            }
            written++;
        }
        MyLog.Default.WriteLineAndConsole(
            $"ImpostorsProbe: voxel dump of {written} of {maps.Count} voxel maps in {folder}"
        );
    }
}

// The world lists the Impostors mod under a stand-in Workshop id the Workshop doesn't
// have. Takes every mod of the world from <data>/Mods/<id>.sbm when all of them are
// staged there, the way Remote's LocalWorldModsPatch does it on the clients.
[HarmonyPatch(typeof(MyWorkshop), nameof(MyWorkshop.DownloadWorldModsBlockingInternal))]
public static class LocalWorldModsPatch
{
    public static bool Prefix(
        List<MyObjectBuilder_Checkpoint.ModItem> mods,
        ref MyWorkshop.ResultData __result
    )
    {
        if (mods == null || mods.Count == 0)
            return true;

        var paths = mods.Select(mod => (mod, path: mod.GetPath())).ToList();
        if (paths.Any(p => p.mod.PublishedFileId == 0 || !Directory.Exists(p.path)))
            return true;

        for (var i = 0; i < mods.Count; i++)
        {
            var (mod, path) = paths[i];
            var item = new MyWorkshopItem
            {
                Id = mod.PublishedFileId,
                Title = mod.FriendlyName ?? mod.Name,
            };
            AccessTools
                .PropertySetter(typeof(MyWorkshopItem), nameof(MyWorkshopItem.Folder))
                .Invoke(item, [path]);
            mod.SetModData(item);
            mods[i] = mod;
            MyLog.Default.WriteLineAndConsole(
                $"ImpostorsProbe: local mod {mod.PublishedFileId} from {path}"
            );
        }

        __result = new MyWorkshop.ResultData(MyGameServiceCallResult.OK, false);
        return false;
    }
}

[CommandRoot("iprobe", "ImpostorsProbe", "Impostors test hooks")]
public sealed class ProbeCommands : CommandModule
{
    private const BindingFlags Any =
        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;

    private static object Service =>
        AppDomain
            .CurrentDomain.GetAssemblies()
            .Select(a => a.GetType("Shared.Impostors.ImpostorService"))
            .First(t => t != null)
            .GetProperty("Instance")
            .GetValue(null);

    private static object Get(object target, string name) =>
        target.GetType().GetField(name, Any)?.GetValue(target)
        ?? target.GetType().GetProperty(name, Any)?.GetValue(target);

    private static string F(double value) => value.ToString("0.#", CultureInfo.InvariantCulture);

    [Command(
        "stats",
        "The impostor service: entries with their versions, and what each player was sent."
    )]
    [Permission(MyPromoteLevel.Admin)]
    public string Stats()
    {
        var service = Service;
        if (service == null)
            return "Probe stats: off";

        var entries = ((IEnumerable)Get(service, "Entries"))
            .Cast<object>()
            .Select(e =>
                $"{Get(e, "Id")}:{Get(e, "Version")}:{(Get(e, "Shapes") is byte[][] s ? string.Join("/", s.Select(x => x?.Length ?? 0)) : "-")}"
            );
        var viewers = ((IDictionary)Get(service, "viewers"))
            .Values.Cast<object>()
            .Select(v =>
                $"{Get(v, "SteamId")}:{string.Join(",", ((IDictionary)Get(v, "Sent")).Keys.Cast<long>().OrderBy(i => i))}"
                + $" ready={string.Join(",", ((IEnumerable)Get(v, "Ready")).Cast<long>().OrderBy(i => i))}"
                + $" held={string.Join(",", Held((IDictionary)Get(v, "Sent")))}"
            );
        return $"Probe stats: entries={string.Join(" ", entries)} viewers={string.Join(" ", viewers)}";
    }

    [Command("inputs", "The impostor entries: id, version and the hash of their content.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Inputs()
    {
        var service = Service;
        if (service == null)
            return "Probe inputs: off";

        var entries = ((IEnumerable)Get(service, "Entries"))
            .Cast<object>()
            .Where(e => Get(e, "Shapes") != null)
            .Select(e => $"{Get(e, "Id")}:{Get(e, "Version")}:{Get(e, "Input")}");
        return $"Probe inputs: {string.Join(" ", entries)}";
    }

    private static IEnumerable<long> Held(IDictionary sent) =>
        sent.Keys.Cast<long>().Where(id => (bool)Get(sent[id], "Held")).OrderBy(id => id);

    [Command(
        "near",
        "The impostor entries within a range of a point: id, kind, the center and radius of the AABB the service sizes them by, and the shapes built."
    )]
    [Permission(MyPromoteLevel.Admin)]
    public string Near(double x, double y, double z, double range)
    {
        var service = Service;
        if (service == null)
            return "Probe near: off";

        var point = new Vector3D(x, y, z);
        var entries = ((IEnumerable)Get(service, "Entries"))
            .Cast<object>()
            .Select(e => (e, aabb: (BoundingBoxD)Get(e, "WorldAabb")))
            .Where(t => Vector3D.Distance(t.aabb.Center, point) <= range)
            .Select(t =>
            {
                var c = t.aabb.Center;
                var shapes = Get(t.e, "Shapes") as byte[][];
                return $"{Get(t.e, "Id")}:{Get(t.e, "Kind")}:{F(c.X)},{F(c.Y)},{F(c.Z)}"
                    + $":{F(t.aabb.HalfExtents.Length())}:{shapes?.Count(s => s != null) ?? 0}";
            });
        return $"Probe near: {string.Join(" ", entries)}";
    }

    [Command("place", "Puts a grid at a position, at rest.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Place(long gridId, double x, double y, double z)
    {
        if (!(MyEntities.GetEntityById(gridId) is MyCubeGrid grid))
            return "Probe place: none";

        grid.Teleport(MatrixD.CreateWorld(new Vector3D(x, y, z), Vector3D.Forward, Vector3D.Up));
        if (grid.Physics != null)
        {
            grid.Physics.LinearVelocity = Vector3.Zero;
            grid.Physics.AngularVelocity = Vector3.Zero;
        }
        return "Probe place: done";
    }

    [Command("push", "Sets a grid's linear velocity.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Push(long gridId, float x, float y, float z)
    {
        if (!(MyEntities.GetEntityById(gridId) is MyCubeGrid grid) || grid.Physics == null)
            return "Probe push: none";

        grid.Physics.LinearVelocity = new Vector3(x, y, z);
        return "Probe push: done";
    }

    [Command("paint", "Paints every block of a grid with a hue.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Paint(long gridId, float hue)
    {
        if (!(MyEntities.GetEntityById(gridId) is MyCubeGrid grid))
            return "Probe paint: none";

        grid.ColorBlocks(grid.Min, grid.Max, new Vector3(hue, -0.3f, 0.45f), false);
        return "Probe paint: done";
    }

    [Command("remove", "Removes a grid's blocks in one layer, x, y or z = index.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Remove(long gridId, string axis, int index)
    {
        if (!(MyEntities.GetEntityById(gridId) is MyCubeGrid grid))
            return "Probe remove: none";

        var layer = grid.GetBlocks()
            .Select(b => b.Position)
            .Where(p =>
                (
                    axis == "x" ? p.X
                    : axis == "y" ? p.Y
                    : p.Z
                ) == index
            )
            .ToList();
        grid.RazeBlocks(layer);
        return $"Probe remove: {layer.Count}";
    }

    [Command("grow", "Adds a layer of armor blocks past the grid's largest x.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Grow(long gridId, float hue)
    {
        if (!(MyEntities.GetEntityById(gridId) is MyCubeGrid grid))
            return "Probe grow: none";

        var x = grid.Max.X + 1;
        var subtype =
            grid.GridSizeEnum == MyCubeSize.Large ? "LargeBlockArmorBlock" : "SmallBlockArmorBlock";
        var added = 0;
        for (var y = grid.Min.Y; y <= grid.Max.Y; y++)
        for (var z = grid.Min.Z; z <= grid.Max.Z; z++)
        {
            var block = new MyObjectBuilder_CubeBlock
            {
                SubtypeName = subtype,
                Min = new Vector3I(x, y, z),
                ColorMaskHSV = new Vector3(hue, -0.3f, 0.45f),
                IntegrityPercent = 1f,
                BuildPercent = 1f,
            };
            if (((IMyCubeGrid)grid).AddBlock(block, false) != null)
                added++;
        }
        return $"Probe grow: {added}";
    }

    [Command("merge", "Merges grid b into grid a, next to a's largest x.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Merge(long a, long b)
    {
        if (
            !(MyEntities.GetEntityById(a) is MyCubeGrid into)
            || !(MyEntities.GetEntityById(b) is MyCubeGrid other)
        )
            return "Probe merge: none";

        var offset = new Vector3I(
            into.Max.X + 1 - other.Min.X,
            into.Min.Y - other.Min.Y,
            into.Min.Z - other.Min.Z
        );
        // Without the merge order check: of two equal grids the game merges the other way round
        var merged = into.MergeGrid_MergeBlock(other, offset, false);
        return merged != null && other.MarkedForClose
            ? $"Probe merge: {merged.EntityId}"
            : $"Probe merge: failed, render {other.HasCubeGridRender}";
    }

    [Command("cut", "Cuts a sphere out of a voxel map, center in world coordinates.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Cut(long voxelId, double x, double y, double z, float radius)
    {
        if (!(MyEntities.GetEntityById(voxelId) is IMyVoxelBase voxel))
            return "Probe cut: none";

        var sphere = MyAPIGateway.Session.VoxelMaps.GetSphereVoxelHand();
        sphere.Center = new Vector3D(x, y, z);
        sphere.Radius = radius;
        MyAPIGateway.Session.VoxelMaps.CutOutShape(voxel, sphere);
        return "Probe cut: done";
    }

    [Command("maxx", "A grid's largest block x.")]
    [Permission(MyPromoteLevel.Admin)]
    public string MaxX(long gridId) =>
        MyEntities.GetEntityById(gridId) is MyCubeGrid grid
            ? $"Probe maxx: {grid.Max.X}"
            : "Probe maxx: none";

    [Command("grids", "The grids on the server: id and block count.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Grids() =>
        "Probe grids: "
        + string.Join(
            " ",
            MyEntities
                .GetEntities()
                .OfType<MyCubeGrid>()
                .OrderBy(g => g.EntityId)
                .Select(g => $"{g.EntityId}:{g.BlocksCount}")
        );

    [Command("pos", "A grid's position on the server.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Position(long gridId)
    {
        if (!(MyEntities.GetEntityById(gridId) is MyCubeGrid grid))
            return "Probe pos: none";

        var p = grid.PositionComp.GetPosition();
        return $"Probe pos: {F(p.X)},{F(p.Y)},{F(p.Z)}";
    }

    [Command("zones", "The safe zones on the server and their impostor entries.")]
    [Permission(MyPromoteLevel.Admin)]
    public string Zones()
    {
        var entries =
            Service == null
                ? new Dictionary<long, object>()
                : ((IEnumerable)Get(Service, "Entries"))
                    .Cast<object>()
                    .ToDictionary(e => (long)Get(e, "Id"));
        var zones = MySessionComponentSafeZones.SafeZones.Select(z =>
        {
            var size = z.PositionComp.WorldAABB.Size;
            var version = entries.TryGetValue(z.EntityId, out var e) ? Get(e, "Version") : "-";
            return $"{z.EntityId}:{z.Shape}:{(z.Enabled ? "on" : "off")}:{F(size.X)}x{F(size.Y)}x{F(size.Z)}:{version}";
        });
        return $"Probe zones: {string.Join(" ", zones)}";
    }

    [Command("vcut", "Cuts a sphere out of a voxel map, this far from its center along X.")]
    [Permission(MyPromoteLevel.Admin)]
    public string VoxelCut(long voxelId, float offset, float radius)
    {
        if (!(MyEntities.GetEntityById(voxelId) is MyVoxelBase voxel))
            return "Probe vcut: none";

        var shape = MyAPIGateway.Session.VoxelMaps.GetSphereVoxelHand();
        shape.Center = voxel.PositionComp.WorldAABB.Center + Vector3D.Right * offset;
        shape.Radius = radius;
        MyAPIGateway.Session.VoxelMaps.CutOutShape(voxel, shape);
        return "Probe vcut: done";
    }

    [Command("vreset", "Resets a voxel map's content and materials to its generator's.")]
    [Permission(MyPromoteLevel.Admin)]
    public string VoxelReset(long voxelId)
    {
        if (!(MyEntities.GetEntityById(voxelId) is MyVoxelBase voxel))
            return "Probe vreset: none";

        voxel.Storage.Reset(VRage.Voxels.MyStorageDataTypeFlags.ContentAndMaterial);
        return "Probe vreset: done";
    }

    [Command("ventry", "A voxel map's impostor version and whether the game counts it changed.")]
    [Permission(MyPromoteLevel.Admin)]
    public string VoxelEntry(long voxelId)
    {
        if (!(MyEntities.GetEntityById(voxelId) is MyVoxelBase voxel))
            return "Probe ventry: none";

        var entry =
            Service == null
                ? null
                : ((IEnumerable)Get(Service, "Entries"))
                    .Cast<object>()
                    .FirstOrDefault(e => (long)Get(e, "Id") == voxelId);
        var version = entry == null ? "-" : Get(entry, "Version");
        return $"Probe ventry: version={version} changed={voxel.ContentChanged} size={F(voxel.PositionComp.WorldAABB.Size.X)}";
    }
}
