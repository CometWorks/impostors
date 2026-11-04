using System;
using System.Globalization;
using System.IO;
using System.Linq;
using HarmonyLib;
using Sandbox.Engine.Utils;
using Sandbox.Game.Entities;
using Sandbox.Game.Gui;
using Sandbox.Game.World;
using Sandbox.ModAPI;
using VRage;
using VRage.FileSystem;
using VRage.Game;
using VRage.Game.Components;
using VRage.Plugins;
using VRage.Utils;
using VRageMath;
using VRageRender;

namespace ImpostorsClientProbe;

// Test hooks of the Impostors suite on the client, through files in the user data folder:
// - fov.txt sets the camera's vertical field of view in degrees, like a zoom; without the
//   file it goes back.
// - billboards.txt ("postpp N ldr M pulse T") draws N PostPP and M LDR billboards per frame,
//   on for T ticks and off for T, always with T = 0. billboard-stats.txt then says every
//   second what the game drew of the frames' billboards, see BillboardCheck.
// - camera.txt ("x y z yaw pitch yawrate vx vy vz") flies the spectator camera, see CameraPath.
// - look.txt ("x y z yaw pitch") holds the character there, looking that way in first person
//   with the HUD hidden, see CharacterView.
// - vcut.txt ("id offset radius") cuts a sphere out of a voxel map offline, like the server
//   probe's vcut, then the file goes.
// - voxelray.txt ("x1 y1 z1 x2 y2 z2") looks for voxel maps on that line the way the Radio
//   Spectrometry mod does, and on its first 2 km, into voxelray-result.txt, then the file goes.
// - voxelcenter.txt ("storage name") writes the world position of that loaded voxel map's rock,
//   the middle of its voxels, into voxelcenter-result.txt, then the file goes.
public class Probe : IPlugin
{
    private int tick;
    private float? original;

    public void Init(object gameInstance)
    {
        new Harmony("ImpostorsClientProbe").Patch(
            AccessTools.DeclaredMethod(
                AccessTools.TypeByName("VRageRender.MyBillboardRenderer"),
                "PrepareList"
            ),
            postfix: new HarmonyMethod(typeof(BillboardCheck), nameof(BillboardCheck.Postfix))
            {
                priority = Priority.Last,
            }
        );
    }

    public void Update()
    {
        CameraPath.Update(++tick);
        CharacterView.Update(tick);
        ShadowObservation.Update(tick);
        if (tick % 30 != 0)
            return;

        BillboardStress.Read();
        BillboardCheck.Write();
        VoxelCut();
        VoxelRay();
        VoxelCenter();

        var camera = MySector.MainCamera;
        if (camera == null)
            return;

        var file = Path.Combine(MyFileSystem.UserDataPath, "fov.txt");
        if (
            File.Exists(file)
            && float.TryParse(
                File.ReadAllText(file).Trim(),
                NumberStyles.Float,
                CultureInfo.InvariantCulture,
                out var degrees
            )
        )
        {
            original ??= camera.FieldOfView;
            camera.FieldOfView = MathHelper.ToRadians(degrees);
        }
        else if (original != null)
        {
            camera.FieldOfView = original.Value;
            original = null;
        }
    }

    private static void VoxelCenter()
    {
        var file = Path.Combine(MyFileSystem.UserDataPath, "voxelcenter.txt");
        if (MySession.Static?.Ready != true || !File.Exists(file))
            return;
        var name = File.ReadAllText(file).Trim();
        File.Delete(file);
        var map = MyEntities
            .GetEntities()
            .OfType<MyVoxelBase>()
            .FirstOrDefault(m => m.StorageName == name);
        var text = "none";
        if (map?.Storage != null)
        {
            const int lod = 3;
            var size = map.Storage.Size >> lod;
            var read = new VRage.Voxels.MyStorageData();
            read.Resize(size);
            map.Storage.ReadRange(
                read,
                VRage.Voxels.MyStorageDataTypeFlags.Content,
                lod,
                Vector3I.Zero,
                size - 1
            );
            var sum = Vector3D.Zero;
            var count = 0;
            for (var z = 0; z < size.Z; z++)
            for (var y = 0; y < size.Y; y++)
            for (var x = 0; x < size.X; x++)
            {
                var p = new Vector3I(x, y, z);
                if (read.Content(ref p) < 128)
                    continue;
                sum += new Vector3D(x + 0.5, y + 0.5, z + 0.5) * (1 << lod);
                count++;
            }
            if (count > 0)
            {
                var local = sum / count - map.SizeInMetresHalf;
                var world = Vector3D.Transform(local, map.WorldMatrix);
                text = string.Format(
                    CultureInfo.InvariantCulture,
                    "{0} {1} {2} {3}",
                    world.X,
                    world.Y,
                    world.Z,
                    count
                );
            }
        }
        File.WriteAllText(Path.Combine(MyFileSystem.UserDataPath, "voxelcenter-result.txt"), text);
    }

    private static void VoxelRay()
    {
        var file = Path.Combine(MyFileSystem.UserDataPath, "voxelray.txt");
        if (MySession.Static?.Ready != true || !File.Exists(file))
            return;
        var v = File.ReadAllText(file)
            .Split((char[])null, StringSplitOptions.RemoveEmptyEntries)
            .Select(w => double.Parse(w, CultureInfo.InvariantCulture))
            .ToArray();
        File.Delete(file);
        var from = new Vector3D(v[0], v[1], v[2]);
        var to = new Vector3D(v[3], v[4], v[5]);
        var text = "";
        foreach (var end in new[] { to, from + Vector3D.Normalize(to - from) * 2000 })
        {
            var ray = new LineD(from, end);
            var maps = new System.Collections.Generic.List<
                MyLineSegmentOverlapResult<MyVoxelBase>
            >();
            MyGamePruningStructure.GetVoxelMapsOverlappingRay(ref ray, maps);
            text += $"length={ray.Length:0} maps={maps.Count}";
            foreach (var map in maps)
            {
                map.Element.GetIntersectionWithLine(ref ray, out Vector3D? hit, false);
                text +=
                    $" {map.Element.StorageName}:{(hit.HasValue ? (hit.Value - from).Length().ToString("0") : "miss")}";
            }
            text += "\n";
        }
        File.WriteAllText(Path.Combine(MyFileSystem.UserDataPath, "voxelray-result.txt"), text);
    }

    private static void VoxelCut()
    {
        var file = Path.Combine(MyFileSystem.UserDataPath, "vcut.txt");
        if (!File.Exists(file))
            return;
        var parts = File.ReadAllText(file).Split(' ', StringSplitOptions.RemoveEmptyEntries);
        File.Delete(file);
        if (!(MyEntities.GetEntityById(long.Parse(parts[0])) is MyVoxelBase voxel))
            return;
        var shape = MyAPIGateway.Session.VoxelMaps.GetSphereVoxelHand();
        shape.Center =
            voxel.PositionComp.WorldAABB.Center
            + Vector3D.Right * float.Parse(parts[1], CultureInfo.InvariantCulture);
        shape.Radius = float.Parse(parts[2], CultureInfo.InvariantCulture);
        MyAPIGateway.Session.VoxelMaps.CutOutShape(voxel, shape);
        MyLog.Default.WriteLine($"Impostors client probe: cut {voxel.EntityId}");
    }

    public void Dispose() { }
}

// Draws the billboards of billboards.txt, small squares in front of the camera
[MySessionComponentDescriptor(MyUpdateOrder.NoUpdate)]
public class BillboardStress : MySessionComponentBase
{
    private static int postPP,
        ldr,
        pulse;
    private int frame;
    private static readonly MyStringId Square = MyStringId.GetOrCompute("Square");

    public static void Read()
    {
        var file = Path.Combine(MyFileSystem.UserDataPath, "billboards.txt");
        var words = File.Exists(file) ? File.ReadAllText(file).Split() : new string[0];
        postPP = ldr = pulse = 0;
        for (var i = 0; i + 1 < words.Length; i += 2)
        {
            var n = int.Parse(words[i + 1]);
            if (words[i] == "postpp")
                postPP = n;
            else if (words[i] == "ldr")
                ldr = n;
            else if (words[i] == "pulse")
                pulse = n;
        }
    }

    public override void Draw()
    {
        var camera = MySector.MainCamera;
        frame++;
        if (camera == null || (pulse > 0 && frame / pulse % 2 == 1))
            return;

        var m = camera.WorldMatrix;
        var center = m.Translation + m.Forward * 50;
        Add(center + m.Left * 2, postPP, MyBillboard.BlendTypeEnum.PostPP, m);
        Add(center + m.Right * 2, ldr, MyBillboard.BlendTypeEnum.LDR, m);
    }

    private static void Add(Vector3D at, int count, MyBillboard.BlendTypeEnum blend, MatrixD m)
    {
        var color = new Vector4(0.05f, 0.05f, 0.05f, 0.05f);
        for (var i = 0; i < count; i++)
        {
            MyTransparentGeometry.AddBillboardOriented(
                Square,
                color,
                at + m.Up * (i % 100 * 0.01),
                m.Left,
                m.Up,
                0.01f,
                0.01f,
                Vector2.Zero,
                blend
            );
        }
    }
}

// After the game's PrepareList and the Impostors plugin's patch of it (when loaded): of the
// billboards left in the frame's list, which ones fall beyond the drawn count
public static class BillboardCheck
{
    private static readonly MyStringId Impostor = MyStringId.GetOrCompute("ImpostorSquare");

    // Since the last write, on the render thread
    private static int frames,
        over,
        full,
        maxTotal,
        maxDrawn,
        cutOthers,
        cutPostPP,
        cutImpostors,
        maxImpostorsDrawn;
    private static int minImpostorsDrawn = int.MaxValue;

    public static void Postfix(
        int __result,
        MyBillboard[] ___m_tempBuffer,
        int[] ___m_bucketCounts,
        int[] ___m_bucketIndices
    )
    {
        var total = 0;
        foreach (var count in ___m_bucketCounts)
            total += count;
        if (total == 0)
            return;

        var drawnImpostors = 0;
        for (var i = 0; i < total; i++)
        {
            var b = ___m_tempBuffer[i];
            var impostor = b.Material == Impostor;
            if (i < __result)
            {
                if (impostor)
                    drawnImpostors++;
            }
            else if (impostor)
                cutImpostors++;
            else
            {
                cutOthers++;
                if (b.BlendType == MyBillboard.BlendTypeEnum.PostPP)
                    cutPostPP++;
            }
        }

        frames++;
        if (total == 32768)
            full++;
        if (total > __result)
            over++;
        maxTotal = Math.Max(maxTotal, total);
        maxDrawn = Math.Max(maxDrawn, __result);
        maxImpostorsDrawn = Math.Max(maxImpostorsDrawn, drawnImpostors);
        minImpostorsDrawn = Math.Min(minImpostorsDrawn, drawnImpostors);
    }

    public static void Write()
    {
        if (frames == 0)
            return;
        var text =
            $"frames={frames} over={over} full={full} maxtotal={maxTotal} maxdrawn={maxDrawn} "
            + $"cutothers={cutOthers} cutpostpp={cutPostPP} cutimpostors={cutImpostors} "
            + $"impostorsdrawn={minImpostorsDrawn}-{maxImpostorsDrawn}\n";
        File.WriteAllText(Path.Combine(MyFileSystem.UserDataPath, "billboard-stats.txt"), text);
        frames = over = full = maxTotal = maxDrawn = cutOthers = cutPostPP = cutImpostors = 0;
        maxImpostorsDrawn = 0;
        minImpostorsDrawn = int.MaxValue;
    }
}

// Flies the spectator camera on a path given by camera.txt, set again every tick: from x y z
// (meters) at velocity vx vy vz (m/s), looking at yaw and pitch (degrees, yaw 0 along -Z,
// 90 along +X, pitch up) and turning at yawrate (degrees per second). A changed file starts
// the path again; without the file the camera goes back to the controlled entity.
public static class CameraPath
{
    private static string current;
    private static int start;
    private static double[] values;

    public static void Update(int tick)
    {
        if (tick % 10 == 0)
            Read(tick);
        if (values == null || MySession.Static == null)
            return;

        var t = (tick - start) / 60.0;
        var position =
            new Vector3D(values[0], values[1], values[2])
            + new Vector3D(values[6], values[7], values[8]) * t;
        var yaw = MathHelper.ToRadians(values[3] + values[5] * t);
        var pitch = MathHelper.ToRadians(values[4]);
        var forward = new Vector3D(
            Math.Cos(pitch) * Math.Sin(yaw),
            Math.Sin(pitch),
            -Math.Cos(pitch) * Math.Cos(yaw)
        );

        if (MySession.Static.CameraController != MySpectatorCameraController.Static)
            MySession.Static.SetCameraController(MyCameraControllerEnum.Spectator, null, position);
        MySpectator.Static.SpectatorCameraMovement = MySpectatorCameraMovementEnum.UserControlled;
        MySpectator.Static.Position = position;
        MySpectator.Static.SetTarget(position + forward, Vector3D.Up);
    }

    private static void Read(int tick)
    {
        var file = Path.Combine(MyFileSystem.UserDataPath, "camera.txt");
        var text = File.Exists(file) ? File.ReadAllText(file).Trim() : null;
        if (text == current)
            return;
        current = text;
        start = tick;
        if (text == null)
        {
            if (values != null && MySession.Static?.ControlledEntity != null)
                MySession.Static.SetCameraController(
                    MyCameraControllerEnum.Entity,
                    MySession.Static.ControlledEntity.Entity
                );
            values = null;
            return;
        }
        var words = text.Split((char[])null, StringSplitOptions.RemoveEmptyEntries);
        values = new double[9];
        for (var i = 0; i < 9 && i < words.Length; i++)
            values[i] = double.Parse(words[i], CultureInfo.InvariantCulture);
    }
}

// Holds the local character at x y z (meters), looking at yaw and pitch (degrees, as in
// camera.txt), in first person and with the HUD hidden, so a screenshot shows the view only:
// no body, no spectator text. Set again every tick from look.txt; without the file the HUD
// comes back.
public static class CharacterView
{
    private static string current;
    private static MatrixD? matrix;

    public static void Update(int tick)
    {
        if (tick % 10 == 0)
            Read();
        var character = MySession.Static?.LocalCharacter;
        if (matrix == null || character == null)
            return;

        var target = matrix.Value;
        var world = character.WorldMatrix;
        if (
            Vector3D.DistanceSquared(world.Translation, target.Translation) > 0.01
            || Vector3D.Dot(world.Forward, target.Forward) < 0.99999
        )
        {
            character.Teleport(target);
            character.Physics?.ClearSpeed();
        }
        character.SetHeadLocalXAngle(0);
        if (MySession.Static.CameraController != character)
            MySession.Static.SetCameraController(MyCameraControllerEnum.Entity, character);
        if (!character.IsInFirstPersonView)
            character.IsInFirstPersonView = true;
        MyHud.MinimalHud = true;
    }

    private static void Read()
    {
        var file = Path.Combine(MyFileSystem.UserDataPath, "look.txt");
        var text = File.Exists(file) ? File.ReadAllText(file).Trim() : null;
        if (text == current)
            return;
        current = text;
        if (text == null)
        {
            if (matrix != null)
                MyHud.MinimalHud = false;
            matrix = null;
            return;
        }
        var v = text.Split((char[])null, StringSplitOptions.RemoveEmptyEntries)
            .Select(w => double.Parse(w, CultureInfo.InvariantCulture))
            .ToArray();
        var yaw = MathHelper.ToRadians(v[3]);
        var pitch = MathHelper.ToRadians(v[4]);
        var forward = new Vector3D(
            Math.Cos(pitch) * Math.Sin(yaw),
            Math.Sin(pitch),
            -Math.Cos(pitch) * Math.Cos(yaw)
        );
        var right = Vector3D.Cross(forward, Vector3D.Up);
        if (right.LengthSquared() < 1e-6)
            right = Vector3D.Cross(forward, Vector3D.Forward);
        right.Normalize();
        matrix = MatrixD.CreateWorld(
            new Vector3D(v[0], v[1], v[2]),
            forward,
            Vector3D.Cross(right, forward)
        );
    }
}
