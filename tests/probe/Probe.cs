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
using VRage.Plugins;
using VRage.Utils;
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

    public void Update() { }

    public void Dispose() { }
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

    private static IEnumerable<long> Held(IDictionary sent) =>
        sent.Keys.Cast<long>().Where(id => (bool)Get(sent[id], "Held")).OrderBy(id => id);

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
