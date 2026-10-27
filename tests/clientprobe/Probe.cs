using System;
using System.Globalization;
using System.IO;
using HarmonyLib;
using Sandbox.Engine.Utils;
using Sandbox.Game.World;
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
        ShadowObservation.Update(tick);
        if (tick % 30 != 0)
            return;

        BillboardStress.Read();
        BillboardCheck.Write();

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
