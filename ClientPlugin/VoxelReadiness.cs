using System.Collections.Generic;
using Impostors;
using Sandbox.Game.Entities;
using Sandbox.Game.EntityComponents.Renders;
using Sandbox.Game.World;
using Sandbox.ModAPI;
using VRageRender.Voxels;

namespace ClientPlugin;

// Tells the renderer when an asteroid's first voxel meshes show (SE-672). An asteroid draws
// nothing until its clipmap's first meshing pass is done, which can take seconds; the mod
// alone can't see that and keeps the impostor for a fixed time instead. The clipmap's Loaded
// event fires once that pass is done (or 5 updates after subscribing to a loaded one), on the
// update thread. With a subscriber the game shows the pass's cells at once instead of fading
// them in, the way it does on its load screen.
public static class VoxelReadiness
{
    // Asteroids waiting for their first meshes, and the ones that have them
    private static readonly Dictionary<long, IMyLodController> watched = new();
    private static readonly HashSet<long> loaded = new();
    private static readonly List<long> gone = new();
    private static MySession session;
    private static int tick;

    public static void Update()
    {
        if (MySession.Static != session)
        {
            foreach (var clipmap in watched.Values)
                clipmap.Loaded -= OnLoaded;
            watched.Clear();
            loaded.Clear();
            session = MySession.Static;
        }
        if (session == null || MyAPIGateway.Session == null || ++tick % 10 != 0)
            return;

        foreach (var voxel in session.VoxelMaps.Instances)
        {
            if (
                voxel is MyVoxelMap
                && !voxel.Closed
                && voxel.Render is MyRenderComponentVoxelMap { Clipmap: { } clipmap }
                && !watched.ContainsKey(voxel.EntityId)
                && !loaded.Contains(voxel.EntityId)
            )
            {
                watched[voxel.EntityId] = clipmap;
                clipmap.Loaded += OnLoaded;
            }
        }

        // Asteroids that went away, a new one of the same id gets watched again
        foreach (var id in watched.Keys)
        {
            if (!(MyEntities.GetEntityById(id) is MyVoxelMap { Closed: false }))
                gone.Add(id);
        }
        foreach (var id in loaded)
        {
            if (!(MyEntities.GetEntityById(id) is MyVoxelMap { Closed: false }))
                gone.Add(id);
        }
        foreach (var id in gone)
        {
            if (watched.Remove(id, out var clipmap))
                clipmap.Loaded -= OnLoaded;
            loaded.Remove(id);
        }
        gone.Clear();
    }

    private static void OnLoaded(IMyLodController clipmap)
    {
        clipmap.Loaded -= OnLoaded;
        foreach (var (id, c) in watched)
        {
            if (c != clipmap)
                continue;
            watched.Remove(id);
            loaded.Add(id);
            MyAPIGateway.Utilities.SendModMessage(ImpostorRenderer.VoxelLoadedMessage, id);
            return;
        }
    }
}
