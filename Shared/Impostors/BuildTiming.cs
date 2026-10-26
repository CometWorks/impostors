using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using Impostors;

namespace Shared.Impostors;

// Wall clock and CPU time of one background build, level by level. The registry sets it for
// the build's thread; the sources mark each level as it is done. tests/build_times.py
// summarizes the log lines.
public sealed class BuildTiming
{
    [ThreadStatic]
    private static BuildTiming current;

    private readonly Stopwatch wall = Stopwatch.StartNew();
    private readonly double cpuStart = ThreadCpuMs();
    private readonly StringBuilder levels = new();
    private double lastWall;
    private double lastCpu;

    public static BuildTiming Start()
    {
        current = new BuildTiming();
        current.lastCpu = current.cpuStart;
        return current;
    }

    public static void Stop() => current = null;

    // The time since the previous mark went into this level's shape
    public static void Mark(byte lod, byte[] shape)
    {
        var t = current;
        if (t == null)
            return;
        var wallMs = t.wall.Elapsed.TotalMilliseconds;
        var cpuMs = ThreadCpuMs();
        var quads = (shape.Length - Shape.HeaderSize) / Shape.QuadSize;
        t.levels.Append($" L{lod}={wallMs - t.lastWall:0.00}/{cpuMs - t.lastCpu:0.00}/{quads}");
        t.lastWall = wallMs;
        t.lastCpu = cpuMs;
    }

    // CPU time of the build so far, milliseconds
    public double CpuMs => ThreadCpuMs() - cpuStart;

    // " wall=ms cpu=ms L<lod>=wall/cpu/quads ...", levels in the order they were built
    public override string ToString() =>
        $" wall={wall.Elapsed.TotalMilliseconds:0.00} cpu={ThreadCpuMs() - cpuStart:0.00}{levels}";

    // CPU time of the calling thread, milliseconds; NaN where it can't be read
    private static double ThreadCpuMs()
    {
        try
        {
            if (Environment.OSVersion.Platform == PlatformID.Unix)
            {
                return clock_gettime(ClockThreadCpuTime, out var ts) == 0
                    ? ts.Seconds * 1e3 + ts.Nanoseconds / 1e6
                    : double.NaN;
            }
            return GetThreadTimes(GetCurrentThread(), out _, out _, out var kernel, out var user)
                ? (kernel + user) / 1e4
                : double.NaN;
        }
        catch (Exception)
        {
            return double.NaN;
        }
    }

    private const int ClockThreadCpuTime = 3;

    [StructLayout(LayoutKind.Sequential)]
    private struct Timespec
    {
        public long Seconds;
        public long Nanoseconds;
    }

    [DllImport("libc")]
    private static extern int clock_gettime(int clock, out Timespec time);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentThread();

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetThreadTimes(
        IntPtr thread,
        out long creation,
        out long exit,
        out long kernel,
        out long user
    );
}
