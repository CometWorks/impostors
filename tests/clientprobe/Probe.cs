using System;
using System.Globalization;
using System.IO;
using HarmonyLib;
using Sandbox.Game.World;
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
        if (++tick % 30 != 0)
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
