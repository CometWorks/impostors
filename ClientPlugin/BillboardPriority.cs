using System;
using System.Diagnostics;
using System.Threading;
using HarmonyLib;
using Impostors;
using Sandbox.ModAPI;
using Shared.Logging;
using VRage.Utils;
using VRageRender;

namespace ClientPlugin;

// The game draws at most 32768 billboards per frame. MyBillboardRenderer.PrepareList groups
// them by blend type (Standard, AdditiveBottom, AdditiveTop, LDR, PostPP) and cuts off the
// rest, so the PostPP ones and the LDR ones added after the impostors are the first to go.
// When a frame is over the limit, this takes the impostor quads out of the LDR bucket instead,
// as many as the frame is over, farthest first, which moves every other billboard into the
// drawn range.
//
// It also tells the renderer how many quads the next frame has room for after every other
// billboard of the last one, so the impostors usually fit and only a sudden burst of effects
// needs the cut.
public static class BillboardPriority
{
    private const int Limit = Protocol.BillboardLimit;
    private const int LdrBucket = (int)MyBillboard.BlendTypeEnum.LDR;

    // Room left for effects that come and go between two frames
    public const int Margin = 2048;

    private static readonly MyStringId Material = MyStringId.GetOrCompute(
        ImpostorRenderer.MaterialName
    );
    private static readonly MyStringId Square = MyStringId.GetOrCompute("Square");

    private static IPluginLogger log;
    private static bool patched;
    private static bool materialAdded;
    private static int tick;

    // The last frame, written on the render thread
    private static int lastTotal;
    private static int lastImpostors;

    // Since the last summary, written on the render thread
    private static int frames;
    private static int overFrames;
    private static int peakTotal;
    private static int peakOthers;
    private static int peakImpostors;
    private static long droppedQuads;
    private static int peakDropped;
    private static long ticks;
    private static long peakTicks;

    public static void Patch(Harmony harmony, IPluginLogger logger)
    {
        log = logger;
        try
        {
            var renderer = AccessTools.TypeByName("VRageRender.MyBillboardRenderer");
            var prepareList = AccessTools.DeclaredMethod(renderer, "PrepareList");
            harmony.Patch(
                prepareList,
                postfix: new HarmonyMethod(typeof(BillboardPriority), nameof(Postfix))
            );
            patched = true;
        }
        catch (Exception e)
        {
            log.Error(e, "Impostor billboards won't go first when a frame is over the limit");
        }
    }

    public static void Update()
    {
        if (!patched || MyAPIGateway.Session == null)
            return;

        AddMaterial();

        // The renderer runs on the update thread, so it gets the budget as a mod message
        var others = Volatile.Read(ref lastTotal) - Volatile.Read(ref lastImpostors);
        MyAPIGateway.Utilities.SendModMessage(
            ImpostorRenderer.BudgetMessage,
            Math.Max(0, Limit - others - Margin)
        );

        if (++tick % 3600 == 0)
            LogSummary();
    }

    // The mod defines the material, a world without it doesn't
    private static void AddMaterial()
    {
        if (materialAdded || !MyTransparentMaterials.TryGetMaterial(Square, out var m))
            return;
        if (!MyTransparentMaterials.ContainsMaterial(Material))
        {
            MyTransparentMaterials.AddMaterial(
                new MyTransparentMaterial(
                    Material,
                    m.TextureType,
                    m.Texture,
                    m.GlossTexture,
                    m.SoftParticleDistanceScale,
                    m.CanBeAffectedByOtherLights,
                    m.AlphaMistingEnable,
                    m.Color,
                    m.ColorAdd,
                    m.ShadowMultiplier,
                    m.LightMultiplier,
                    m.IsFlareOccluder,
                    m.TriangleFaceCulling,
                    m.UseAtlas,
                    m.AlphaMistingStart,
                    m.AlphaMistingEnd,
                    m.AlphaSaturation,
                    m.Reflectivity,
                    m.AlphaCutout,
                    m.TargetSize,
                    m.Fresnel,
                    m.ReflectionShadow,
                    m.Gloss,
                    m.GlossTextureAdd,
                    m.SpecularColorFactor
                )
                {
                    UVOffset = m.UVOffset,
                    UVSize = m.UVSize,
                }
            );
        }
        materialAdded = true;
    }

    // Billboard totals of the last minute, to see how much room the effects of a fight leave
    private static void LogSummary()
    {
        var count = Interlocked.Exchange(ref frames, 0);
        if (count > 0 && peakImpostors > 0)
        {
            log.Info(
                $"Billboards in the last minute: peak {peakTotal}, others {peakOthers}, "
                    + $"impostors {peakImpostors}; over {Limit} in {overFrames} of {count} "
                    + $"frames, {droppedQuads} impostor quads dropped, {peakDropped} at most "
                    + $"in one; patch {TicksToMs(ticks) * 1000 / count:0.0} us per frame, "
                    + $"{TicksToMs(peakTicks):0.000} ms at most"
            );
        }
        overFrames = peakTotal = peakOthers = peakImpostors = peakDropped = 0;
        droppedQuads = ticks = peakTicks = 0;
    }

    private static double TicksToMs(long t) => t * 1000.0 / Stopwatch.Frequency;

    // Runs on the render thread. The buffer holds the frame's billboards bucket by bucket,
    // the indices say where each bucket starts, the result is how many of them are drawn.
    private static void Postfix(
        ref int __result,
        MyBillboard[] ___m_tempBuffer,
        int[] ___m_bucketCounts,
        int[] ___m_bucketIndices
    )
    {
        // An empty frame leaves the whole budget
        if (__result == 0)
        {
            Volatile.Write(ref lastTotal, 0);
            Volatile.Write(ref lastImpostors, 0);
            return;
        }

        var start = Stopwatch.GetTimestamp();
        var buffer = ___m_tempBuffer;
        var counts = ___m_bucketCounts;
        var indices = ___m_bucketIndices;

        var total = 0;
        foreach (var count in counts)
            total += count;
        var ldrStart = indices[LdrBucket];
        var ldrEnd = ldrStart + counts[LdrBucket];

        var impostors = 0;
        for (var i = ldrStart; i < ldrEnd; i++)
        {
            if (buffer[i].Material == Material)
                impostors++;
        }

        // The renderer submits them far to near and the game keeps LDR billboards in
        // submission order, so the first ones are the farthest
        var drop = Math.Min(total - Limit, impostors);
        if (drop > 0)
        {
            var write = ldrStart;
            var skipped = 0;
            for (var read = ldrStart; read < total; read++)
            {
                var billboard = buffer[read];
                if (read < ldrEnd && skipped < drop && billboard.Material == Material)
                {
                    skipped++;
                    continue;
                }
                buffer[write++] = billboard;
            }
            Array.Clear(buffer, write, total - write);

            counts[LdrBucket] -= drop;
            for (var b = LdrBucket + 1; b < indices.Length; b++)
                indices[b] -= drop;
            __result = Math.Min(Limit, total - drop);
        }

        Volatile.Write(ref lastTotal, total);
        Volatile.Write(ref lastImpostors, impostors);

        var elapsed = Stopwatch.GetTimestamp() - start;
        frames++;
        ticks += elapsed;
        peakTicks = Math.Max(peakTicks, elapsed);
        peakTotal = Math.Max(peakTotal, total);
        peakOthers = Math.Max(peakOthers, total - impostors);
        peakImpostors = Math.Max(peakImpostors, impostors);
        if (total > Limit)
            overFrames++;
        if (drop > 0)
        {
            droppedQuads += drop;
            peakDropped = Math.Max(peakDropped, drop);
        }
    }
}
