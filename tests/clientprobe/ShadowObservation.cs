using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using HarmonyLib;
using VRage.FileSystem;

namespace ImpostorsClientProbe;

// Test-only per-draw observations; the ordinary renderer stats sample just once a second.
public static class ShadowObservation
{
    private static readonly HashSet<Type> patched = new();
    private static readonly Dictionary<string, Sample> samples = new();
    private static readonly object gate = new();
    private static bool enabled;
    private static string phase;

    private sealed class Sample
    {
        public int Frames;
        public float Min = float.MaxValue;
        public float Max = float.MinValue;
        public readonly HashSet<int> Levels = new();
    }

    public static void Update(int tick)
    {
        if (tick % 60 != 0)
            return;
        enabled = File.Exists(Path.Combine(MyFileSystem.UserDataPath, "observe-shadow"));
        if (!enabled)
            return;
        var next = File.ReadAllText(Path.Combine(MyFileSystem.UserDataPath, "observe-shadow"));
        lock (gate)
        {
            if (next != phase)
            {
                samples.Clear();
                phase = next;
            }
        }
        foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
        {
            var type = assembly.GetType("Impostors.ImpostorRenderer", false);
            if (type == null || !patched.Add(type))
                continue;
            new Harmony("ImpostorsShadowObservation").Patch(
                AccessTools.DeclaredMethod(type, "Draw"),
                postfix: new HarmonyMethod(typeof(ShadowObservation), nameof(AfterDraw))
            );
        }
        lock (gate)
        {
            var text = new StringBuilder();
            foreach (var pair in samples)
                text.AppendLine(
                    $"source={pair.Key} frames={pair.Value.Frames} "
                        + $"min={pair.Value.Min.ToString("R", CultureInfo.InvariantCulture)} "
                        + $"max={pair.Value.Max.ToString("R", CultureInfo.InvariantCulture)} "
                        + $"levels={string.Join(",", pair.Value.Levels)}"
                );
            File.WriteAllText(
                Path.Combine(MyFileSystem.UserDataPath, "shadow-observation.txt"),
                text.ToString()
            );
        }
    }

    public static void AfterDraw(object __instance)
    {
        if (!enabled)
            return;
        var type = __instance.GetType();
        var visible = (IEnumerable)AccessTools.Field(type, "visible").GetValue(__instance);
        lock (gate)
        {
            foreach (var impostor in visible)
            {
                var itemType = impostor.GetType();
                var state = itemType.GetField("State").GetValue(impostor);
                var id = state.GetType().GetField("Id").GetValue(state);
                var key = type.Assembly.GetName().Name + ":" + __instance.GetHashCode() + ":" + id;
                if (!samples.TryGetValue(key, out var sample))
                    samples.Add(key, sample = new Sample());
                var light = (float)itemType.GetField("Sunlight").GetValue(impostor);
                var shape = itemType.GetField("Drawn").GetValue(impostor);
                sample.Frames++;
                sample.Min = Math.Min(sample.Min, light);
                sample.Max = Math.Max(sample.Max, light);
                if (shape != null)
                    sample.Levels.Add((byte)shape.GetType().GetField("Lod").GetValue(shape));
            }
        }
    }
}
