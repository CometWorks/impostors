using System;
using System.Collections;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;
using HarmonyLib;
using Impostors;
using Sandbox.ModAPI;
using Shared.Logging;
using VRage.FileSystem;
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
//
// And it draws the impostor quads earlier. LDR billboards are drawn after tone mapping, on
// top of everything the transparent pass drew before: thruster flames, glass, the screens
// of transparent LCDs, none of which write depth. So the impostor quads go to the front of
// the LDR bucket and are drawn from there with the LDR pass's own code right before the
// transparent pass, onto the lit scene. The LDR pass then draws only the rest of its bucket.
// The quads still count as LDR billboards for the limit above.
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

    // Drawing the impostor quads before the transparent pass: the renderer's privates
    private static bool early;
    private static MethodInfo renderLdr;
    private static Array bucketBatches;
    private static IList batches;
    private static FieldInfo bucketStart;
    private static FieldInfo bucketCount;
    private static FieldInfo batchOffset;
    private static FieldInfo batchNum;
    private static FieldInfo gbufferMain;
    private static PropertyInfo lbuffer;
    private static volatile bool earlyFailed;

    // For comparison shots: with this file in the plugin's storage the impostors stay in the
    // LDR pass
    private static readonly string OnTopFile = Path.Combine(
        MyFileSystem.UserDataPath,
        "Storage",
        "Impostors.ontop"
    );
    private static volatile bool onTop;

    // The impostor quads of the frame in the buffer, written and read on the render thread
    private static int earlyStart;
    private static int earlyCount;
    private static MyBillboard[] scratch = new MyBillboard[1024];

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
            return;
        }

        try
        {
            var renderer = AccessTools.TypeByName("VRageRender.MyBillboardRenderer");
            var batch = AccessTools.TypeByName("VRageRender.MyBillboardRendererBatch");
            var bucket = AccessTools.Inner(renderer, "MyBucketBatches");
            var gbuffer = AccessTools.TypeByName("VRage.Render11.Resources.MyGBuffer");
            renderLdr = Required(AccessTools.DeclaredMethod(renderer, "RenderLDR"));
            bucketBatches = (Array)
                Required(AccessTools.Field(renderer, "m_bucketBatches")).GetValue(null);
            batches = (IList)Required(AccessTools.Field(renderer, "m_batches")).GetValue(null);
            bucketStart = Required(AccessTools.Field(bucket, "StartIndex"));
            bucketCount = Required(AccessTools.Field(bucket, "Count"));
            batchOffset = Required(AccessTools.Field(batch, "Offset"));
            batchNum = Required(AccessTools.Field(batch, "Num"));
            gbufferMain = Required(AccessTools.Field(gbuffer, "Main"));
            lbuffer = Required(AccessTools.Property(gbuffer, "LBuffer"));
            harmony.Patch(
                Required(AccessTools.DeclaredMethod(renderer, "RenderAdditiveBottom")),
                postfix: new HarmonyMethod(typeof(BillboardPriority), nameof(DrawEarly))
            );
            early = true;
        }
        catch (Exception e)
        {
            log.Error(e, "Impostors will draw over thruster flames and transparent LCDs");
        }
    }

    private static T Required<T>(T member)
        where T : class
    {
        return member ?? throw new MissingMemberException("Not in this game version");
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

        if (tick % 60 == 0)
            onTop = File.Exists(OnTopFile);

        // The mod lights the quads for where they are drawn
        if (early && !onTop && !earlyFailed)
            MyAPIGateway.Utilities.SendModMessage(ImpostorRenderer.HdrMessage, true);
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
        earlyCount = 0;

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
            ldrEnd -= drop;
        }
        var kept = impostors - Math.Max(drop, 0);

        if (early && !onTop && !earlyFailed && kept > 0 && ldrStart + kept <= __result)
        {
            ToFront(buffer, ldrStart, ldrEnd, kept);
            earlyStart = ldrStart;
            earlyCount = kept;
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

    // Moves the impostor quads of the LDR bucket to its front, both they and the rest keep
    // their order. Mods draw in the order they were registered, so they usually are there.
    private static void ToFront(MyBillboard[] buffer, int start, int end, int impostors)
    {
        var front = start;
        while (front < end && buffer[front].Material == Material)
            front++;
        if (front - start == impostors)
            return;

        if (scratch.Length < end - front)
            scratch = new MyBillboard[Math.Max(end - front, scratch.Length * 2)];
        var write = front;
        var others = 0;
        for (var read = front; read < end; read++)
        {
            var billboard = buffer[read];
            if (billboard.Material == Material)
                buffer[write++] = billboard;
            else
                scratch[others++] = billboard;
        }
        Array.Copy(scratch, 0, buffer, write, others);
        Array.Clear(scratch, 0, others);
    }

    // Runs on the render thread right before the transparent pass, after the billboards
    // were put into batches: runs of one texture within a bucket. Draws the batches of the
    // impostor quads with the LDR pass's code onto the lit scene and leaves the LDR pass the
    // batches after them. A batch that goes on past the impostors is cut in two.
    private static void DrawEarly(object rc, object depthRead)
    {
        if (earlyCount == 0)
            return;
        var end = earlyStart + earlyCount;
        earlyCount = 0;

        try
        {
            var bucket = bucketBatches.GetValue(LdrBucket);
            var first = (int)bucketStart.GetValue(bucket);
            var count = (int)bucketCount.GetValue(bucket);

            var mine = 0;
            object cut = null;
            var rest = 0;
            for (var i = first; i < first + count; i++)
            {
                var batch = batches[i];
                var offset = (int)batchOffset.GetValue(batch);
                if (offset >= end)
                    break;
                mine++;
                var num = (int)batchNum.GetValue(batch);
                if (offset + num > end)
                {
                    cut = batch;
                    rest = offset + num - end;
                    batchNum.SetValue(batch, end - offset);
                    batches[i] = batch;
                }
            }
            if (mine == 0)
                return;

            SetBucket(bucket, first, mine);
            try
            {
                var target = lbuffer.GetValue(gbufferMain.GetValue(null));
                renderLdr.Invoke(null, new[] { rc, depthRead, target });
            }
            finally
            {
                if (cut == null)
                {
                    SetBucket(bucket, first + mine, count - mine);
                }
                else
                {
                    batchOffset.SetValue(cut, end);
                    batchNum.SetValue(cut, rest);
                    batches[first + mine - 1] = cut;
                    SetBucket(bucket, first + mine - 1, count - mine + 1);
                }
            }
        }
        catch (Exception e)
        {
            earlyFailed = true;
            log.Error(e, "Impostors will draw over thruster flames and transparent LCDs");
        }
    }

    private static void SetBucket(object bucket, int start, int count)
    {
        bucketStart.SetValue(bucket, start);
        bucketCount.SetValue(bucket, count);
        bucketBatches.SetValue(bucket, LdrBucket);
    }
}
