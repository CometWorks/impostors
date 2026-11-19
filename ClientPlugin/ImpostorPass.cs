using System;
using System.Collections;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using HarmonyLib;
using Impostors;
using Shared.Config;
using Shared.Logging;
using VRage.FileSystem;
using VRage.Utils;
using VRageRender;

namespace ClientPlugin;

// Draws the impostor quads outside the game's 32768 billboards per frame (SE-1541).
//
// The renderer still adds them as billboards. Before the game's billboard renderer counts the
// frame's billboards (MyBillboardRenderer.Gather), this takes the impostor quads out of the
// list, so they use none of the 32768, and draws them itself right there, before the
// transparent pass, onto the lit scene: behind thruster flames and transparent LCDs.
//
// It draws with the game's own code, 32768 quads at a time, which is what the game's index
// buffer and its shader's quad numbering cover. For each such chunk it points the billboard
// renderer at vertex and quad data buffers of its own, lets GatherInternal fill them from the
// chunk's billboards and RenderLDR draw them, then puts the game's buffers back. The game
// uploads billboard data either at once or later on the main render context, depending on the
// graphics adapter, and the chunks follow it.
public static class ImpostorPass
{
    // Quads per draw
    private const int Chunk = Protocol.BillboardLimit;
    private const int LdrBucket = (int)MyBillboard.BlendTypeEnum.LDR;

    public const int MinQuads = 16000;
    public const int MaxQuads = 256000;

    private static readonly MyStringId Material = MyStringId.GetOrCompute(
        ImpostorRenderer.MaterialName
    );

    private static IPluginLogger log;
    private static bool patched;
    private static bool failed;

    // The billboard renderer's privates
    private static FieldInfo vertexBuffer;
    private static FieldInfo quadBuffer;
    private static FieldInfo tempBuffer;
    private static FieldInfo countSafe;
    private static FieldInfo lastBatchOffset;
    private static FieldInfo arrays;
    private static FieldInfo arraysData;
    private static FieldInfo arraysVertex;
    private static int[] bucketIndices;
    private static IList batches;
    private static MethodInfo gatherInternal;
    private static MethodInfo transferData;
    private static MethodInfo transferVertex;
    private static MethodInfo renderLdr;
    private static MethodInfo createVertexBuffer;
    private static MethodInfo createQuadBuffer;
    private static FieldInfo bufferManager;
    private static FieldInfo gbufferMain;
    private static PropertyInfo lbuffer;
    private static PropertyInfo resolvedDepth;
    private static PropertyInfo depthSrv;
    private static Func<bool> transfersAtOnce;

    private sealed class Buffers
    {
        public object Vertex;
        public object Quad;
        public object Arrays;
        public int Count;
    }

    // One per chunk, made when first needed
    private static readonly Buffers[] chunks = new Buffers[(MaxQuads + Chunk - 1) / Chunk];

    // The frame's impostor quads, and the chunks still to upload: render threads only, one
    // frame at a time
    private static MyBillboard[] quads = new MyBillboard[Chunk];
    private static int quadCount;
    private static int pending;

    // Since the last summary, which comes once a minute, every 10 seconds with the plugin's
    // debug file
    private static readonly string DebugFile = Path.Combine(
        MyFileSystem.UserDataPath,
        "Storage",
        "Impostors.debug"
    );
    private static int summarySeconds = 60;
    private static long summaryStart = Stopwatch.GetTimestamp();
    private static int frames;
    private static long quadSum;
    private static int quadPeak;
    private static long ticks;
    private static long tickPeak;

    // Whether the last frames went through here, for the renderer's cap
    private static int lastDrawTick = -1000;
    private static int tick;

    public static bool Active =>
        patched && !failed && !BillboardPriority.OnTop && tick - lastDrawTick < 600;

    // The option in its range, 16000 to 256000 in steps of 16000
    public static int Quads(IPluginConfig config)
    {
        var steps = config.PluginQuadsPerFrame / MinQuads;
        return Math.Max(1, Math.Min(MaxQuads / MinQuads, steps)) * MinQuads;
    }

    public static void Patch(Harmony harmony, IPluginLogger logger)
    {
        log = logger;
        try
        {
            var renderer = AccessTools.TypeByName("VRageRender.MyBillboardRenderer");
            var context = AccessTools.TypeByName("VRage.Render11.RenderContext.MyRenderContext");
            var arrayType = AccessTools.TypeByName("VRageRender.MyBillboardDataArray");
            var gbuffer = AccessTools.TypeByName("VRage.Render11.Resources.MyGBuffer");
            var managers = AccessTools.TypeByName("VRage.Render11.Common.MyManagers");
            var render = AccessTools.TypeByName("VRageRender.MyRender11");

            vertexBuffer = Required(AccessTools.Field(renderer, "m_VB"));
            quadBuffer = Required(AccessTools.Field(renderer, "m_SB"));
            tempBuffer = Required(AccessTools.Field(renderer, "m_tempBuffer"));
            countSafe = Required(AccessTools.Field(renderer, "m_billboardCountSafe"));
            lastBatchOffset = Required(AccessTools.Field(renderer, "m_lastBatchOffset"));
            arrays = Required(AccessTools.Field(renderer, "m_arrayDataBillboards"));
            arraysData = Required(AccessTools.Field(arrayType, "Data"));
            arraysVertex = Required(AccessTools.Field(arrayType, "Vertex"));
            bucketIndices = (int[])
                Required(AccessTools.Field(renderer, "m_bucketIndices")).GetValue(null);
            batches = (IList)Required(AccessTools.Field(renderer, "m_batches")).GetValue(null);
            gatherInternal = Required(AccessTools.DeclaredMethod(renderer, "GatherInternal"));
            transferData = Required(
                AccessTools.DeclaredMethod(
                    renderer,
                    "TransferDataBillboards",
                    new[] { context, typeof(int), typeof(int), arraysData.FieldType }
                )
            );
            transferVertex = Required(
                AccessTools.DeclaredMethod(
                    renderer,
                    "TransferDataBillboards",
                    new[] { context, typeof(int), typeof(int), arraysVertex.FieldType }
                )
            );
            renderLdr = Required(AccessTools.DeclaredMethod(renderer, "RenderLDR"));
            bufferManager = Required(AccessTools.Field(managers, "Buffers"));
            createVertexBuffer = Required(
                AccessTools.Method(bufferManager.FieldType, "CreateVertexBuffer")
            );
            createQuadBuffer = Required(AccessTools.Method(bufferManager.FieldType, "CreateSrv"));
            gbufferMain = Required(AccessTools.Field(gbuffer, "Main"));
            lbuffer = Required(AccessTools.Property(gbuffer, "LBuffer"));
            resolvedDepth = Required(AccessTools.Property(gbuffer, "ResolvedDepthStencil"));
            depthSrv = Required(AccessTools.Property(resolvedDepth.PropertyType, "SrvDepth"));
            transfersAtOnce =
                (Func<bool>)
                    Delegate.CreateDelegate(
                        typeof(Func<bool>),
                        Required(AccessTools.PropertyGetter(render, "DeferredTransferData"))
                    );

            harmony.Patch(
                Required(AccessTools.DeclaredMethod(renderer, "Gather")),
                prefix: new HarmonyMethod(typeof(ImpostorPass), nameof(BeforeGather)),
                postfix: new HarmonyMethod(typeof(ImpostorPass), nameof(AfterGather))
            );
            harmony.Patch(
                Required(AccessTools.DeclaredMethod(renderer, "TransferData")),
                postfix: new HarmonyMethod(typeof(ImpostorPass), nameof(AfterTransfer))
            );
            patched = true;
        }
        catch (Exception e)
        {
            log.Error(e, "Impostor quads will count as billboards of the game");
        }
    }

    private static T Required<T>(T member)
        where T : class
    {
        return member ?? throw new MissingMemberException("Not in this game version");
    }

    // On the update thread: tells the renderer its cap while the quads are drawn here
    public static void Update(IPluginConfig config)
    {
        if (tick++ % 60 == 0)
            summarySeconds = File.Exists(DebugFile) ? 10 : 60;
        if (!Active || Sandbox.ModAPI.MyAPIGateway.Session == null)
            return;
        Sandbox.ModAPI.MyAPIGateway.Utilities.SendModMessage(
            ImpostorRenderer.PluginQuadsMessage,
            Quads(config)
        );
        var seconds = (Stopwatch.GetTimestamp() - summaryStart) / (double)Stopwatch.Frequency;
        if (seconds >= summarySeconds)
            LogSummary(seconds);
    }

    private static void LogSummary(double seconds)
    {
        summaryStart = Stopwatch.GetTimestamp();
        var count = Interlocked.Exchange(ref frames, 0);
        if (count > 0 && quadPeak > 0)
        {
            log.Info(
                $"Impostor quads drawn by the plugin in the last {seconds:0} s: "
                    + $"{quadSum / count} per frame, {quadPeak} at most; "
                    + $"{ticks * 1000.0 / Stopwatch.Frequency / count:0.000} ms per frame, "
                    + $"{tickPeak * 1000.0 / Stopwatch.Frequency:0.000} ms at most; "
                    + $"{count / seconds:0.0} frames per second"
            );
        }
        quadSum = ticks = tickPeak = 0;
        quadPeak = 0;
    }

    // Before the game puts the frame's billboards into its buffers
    private static void BeforeGather(object rc, bool immediateContext)
    {
        quadCount = 0;
        if (!patched || failed || BillboardPriority.OnTop)
            return;

        var start = Stopwatch.GetTimestamp();
        var list = MyRenderProxy.BillboardsRead;
        var kept = 0;
        for (var i = 0; i < list.Count; i++)
        {
            var billboard = list[i];
            if (billboard.Material == Material && (int)billboard.BlendType == LdrBucket)
            {
                if (quadCount == quads.Length)
                    Array.Resize(ref quads, quadCount * 2);
                quads[quadCount++] = billboard;
            }
            else
            {
                list[kept++] = billboard;
            }
        }
        // The renderer stays at its cap as long as frames with quads come through here
        lastDrawTick = tick;
        if (quadCount == 0)
            return;
        list.RemoveRange(kept, list.Count - kept);

        try
        {
            Draw(rc, immediateContext || transfersAtOnce());
        }
        catch (Exception e)
        {
            failed = true;
            pending = 0;
            log.Error(e, "Impostor quads will count as billboards of the game");
        }

        var elapsed = Stopwatch.GetTimestamp() - start;
        frames++;
        quadSum += quadCount;
        quadPeak = Math.Max(quadPeak, quadCount);
        ticks += elapsed;
        tickPeak = Math.Max(tickPeak, elapsed);
    }

    // The same list is drawn again when the next frame comes before the next update
    private static void AfterGather()
    {
        if (quadCount > 0)
            MyRenderProxy.BillboardsRead.AddRange(
                new ArraySegment<MyBillboard>(quads, 0, quadCount)
            );
    }

    private static void Draw(object rc, bool uploadNow)
    {
        var gameVertex = vertexBuffer.GetValue(null);
        var gameQuad = quadBuffer.GetValue(null);
        var gameArrays = arrays.GetValue(null);

        var temp = (MyBillboard[])tempBuffer.GetValue(null);
        if (temp.Length < Chunk)
        {
            temp = new MyBillboard[Chunk];
            tempBuffer.SetValue(null, temp);
        }

        var gbuffer = gbufferMain.GetValue(null);
        var target = lbuffer.GetValue(gbuffer);
        var depth = depthSrv.GetValue(resolvedDepth.GetValue(gbuffer));
        var context = new[] { rc };
        var draw = new[] { rc, depth, target };

        try
        {
            var index = 0;
            for (var start = 0; start < quadCount; start += Chunk, index++)
            {
                var count = Math.Min(Chunk, quadCount - start);
                var chunk = chunks[index] ?? (chunks[index] = Create(index));

                // All of them in the LDR bucket, as the only batches
                Array.Copy(quads, start, temp, 0, count);
                for (var b = 0; b < bucketIndices.Length; b++)
                    bucketIndices[b] = b > LdrBucket ? count : 0;
                countSafe.SetValue(null, count);
                lastBatchOffset.SetValue(null, 0);
                batches.Clear();

                arrays.SetValue(null, chunk.Arrays);
                gatherInternal.Invoke(null, context);
                chunk.Count = (int)countSafe.GetValue(null);

                vertexBuffer.SetValue(null, chunk.Vertex);
                quadBuffer.SetValue(null, chunk.Quad);
                if (uploadNow)
                    Upload(rc, chunk);
                else
                    pending = index + 1;
                renderLdr.Invoke(null, draw);
            }
        }
        finally
        {
            vertexBuffer.SetValue(null, gameVertex);
            quadBuffer.SetValue(null, gameQuad);
            arrays.SetValue(null, gameArrays);
            countSafe.SetValue(null, 0);
            batches.Clear();
        }
    }

    // The renderer's vertex and quad data buffers must be the chunk's
    private static void Upload(object rc, Buffers chunk)
    {
        transferData.Invoke(null, new[] { rc, 0, chunk.Count, arraysData.GetValue(chunk.Arrays) });
        transferVertex.Invoke(
            null,
            new[] { rc, 0, chunk.Count, arraysVertex.GetValue(chunk.Arrays) }
        );
    }

    // After the game uploaded its billboard data on the main render context, when it does
    // that later instead of at once
    private static void AfterTransfer(object rc)
    {
        if (pending == 0)
            return;
        var count = pending;
        pending = 0;

        var start = Stopwatch.GetTimestamp();
        var gameVertex = vertexBuffer.GetValue(null);
        var gameQuad = quadBuffer.GetValue(null);
        try
        {
            for (var index = 0; index < count; index++)
            {
                var chunk = chunks[index];
                vertexBuffer.SetValue(null, chunk.Vertex);
                quadBuffer.SetValue(null, chunk.Quad);
                Upload(rc, chunk);
            }
        }
        catch (Exception e)
        {
            failed = true;
            log.Error(e, "Impostor quads will count as billboards of the game");
        }
        finally
        {
            vertexBuffer.SetValue(null, gameVertex);
            quadBuffer.SetValue(null, gameQuad);
        }
        ticks += Stopwatch.GetTimestamp() - start;
    }

    // Buffers like the game's own (MyBillboardRenderer.Init)
    private static Buffers Create(int index)
    {
        var manager = bufferManager.GetValue(null);
        var name = "ImpostorPass" + index;
        var vertexStride = Marshal.SizeOf(arraysVertex.FieldType.GetElementType()) / 4;
        var quadStride = Marshal.SizeOf(arraysData.FieldType.GetElementType());
        return new Buffers
        {
            Vertex = createVertexBuffer.Invoke(
                manager,
                new[]
                {
                    name,
                    Chunk * 4,
                    vertexStride,
                    null,
                    Dynamic(createVertexBuffer),
                    false,
                    true,
                }
            ),
            Quad = createQuadBuffer.Invoke(
                manager,
                new[] { name, Chunk, quadStride, null, Dynamic(createQuadBuffer), true }
            ),
            Arrays = Activator.CreateInstance(arrays.FieldType, Chunk),
        };
    }

    // SharpDX's ResourceUsage.Dynamic
    private static object Dynamic(MethodInfo create)
    {
        return Enum.ToObject(create.GetParameters()[4].ParameterType, 2);
    }
}
