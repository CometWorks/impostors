using System;
using System.Diagnostics;
using System.IO;
using Impostors;
using Shared.Impostors;
using VRageMath;

// Builds the shapes of a made-up rock with flat and with smooth normals through VoxelShapes,
// the code the server runs on an asteroid's voxels, and prints per level and mode: the quads,
// the bytes, the build time and how far the normals are off the rock's own surface normal.
// Writes the shapes into the folder of the first argument for tests/asteroid_normals.py.
static class Rock
{
    // Storage size in voxels, the rock fills about three quarters of it
    const int Size = 256;
    static readonly Vector3 Center = new Vector3(Size / 2f);
    static readonly Vector3 Axes = new Vector3(0.36f, 0.3f, 0.25f) * Size;

    // Distance from the surface in voxels, negative inside: an ellipsoid with large bumps and
    // some small ones, which the coarse levels can't show
    static float Distance(Vector3 p)
    {
        var q = (p - Center) / Axes;
        var length = q.Length();
        if (length < 1e-6f)
            return -Axes.Min();
        var d = q / length;
        var bumps =
            1
            + 0.14f * (float)Math.Sin(3 * d.X + 1) * (float)Math.Cos(2 * d.Y)
            + 0.10f * (float)Math.Sin(4 * d.Z + 2 * d.X)
            + 0.06f * (float)Math.Cos(5 * d.Y - 3 * d.Z)
            + 0.025f * (float)Math.Sin(11 * d.X + 7 * d.Z) * (float)Math.Sin(9 * d.Y + 1);
        return (length - bumps) * Axes.Min();
    }

    static Vector3 SurfaceNormal(Vector3 p)
    {
        const float h = 0.5f;
        return Vector3.Normalize(
            new Vector3(
                Distance(p + Vector3.UnitX * h) - Distance(p - Vector3.UnitX * h),
                Distance(p + Vector3.UnitY * h) - Distance(p - Vector3.UnitY * h),
                Distance(p + Vector3.UnitZ * h) - Distance(p - Vector3.UnitZ * h)
            )
        );
    }

    // Content per level of detail, each level the average of 8 voxels of the finer one, as
    // the game's storage keeps them
    static byte[][] Pyramid()
    {
        var levels = new byte[9][];
        var full = new byte[Size * Size * Size];
        for (var z = 0; z < Size; z++)
        for (var y = 0; y < Size; y++)
        for (var x = 0; x < Size; x++)
        {
            var d = Distance(new Vector3(x + 0.5f, y + 0.5f, z + 0.5f));
            full[(z * Size + y) * Size + x] = (byte)MathHelper.Clamp(127.5f - d * 127.5f, 0, 255);
        }
        levels[0] = full;
        for (var lod = 1; lod < levels.Length; lod++)
        {
            var fine = Size >> (lod - 1);
            var n = fine / 2;
            var coarse = new byte[n * n * n];
            for (var z = 0; z < n; z++)
            for (var y = 0; y < n; y++)
            for (var x = 0; x < n; x++)
            {
                var sum = 0;
                for (var k = 0; k < 8; k++)
                    sum += levels[lod - 1][
                        ((2 * z + (k >> 2)) * fine + 2 * y + ((k >> 1) & 1)) * fine
                            + 2 * x
                            + (k & 1)
                    ];
                coarse[(z * n + y) * n + x] = (byte)((sum + 4) / 8);
            }
            levels[lod] = coarse;
        }
        return levels;
    }

    static byte[][] Build(byte[][] pyramid, bool smooth)
    {
        var size = new Vector3I(Size);
        var levels = VoxelShapes.Levels(size);
        var contents = new byte[levels.Count][];
        var materials = new byte[levels.Count][];
        for (var k = 0; k < levels.Count; k++)
        {
            contents[k] = pyramid[VoxelShapes.ReadLod(levels, k)];
            materials[k] = new byte[contents[k].Length];
        }
        var colors = new Vector3I[256];
        for (var i = 0; i < colors.Length; i++)
            colors[i] = new Vector3I(170, 170, 170);
        return VoxelShapes.Build(
            size,
            Vector3.Zero,
            colors,
            false,
            smooth,
            levels,
            contents,
            materials
        );
    }

    // Mean angle between the quads' normals and the rock's at their centers, degrees,
    // weighted by the quads' areas
    static double NormalError(byte[] shape)
    {
        var cellSize = BitConverter.ToSingle(shape, 1);
        var origin = new Vector3(
            BitConverter.ToSingle(shape, 5),
            BitConverter.ToSingle(shape, 9),
            BitConverter.ToSingle(shape, 13)
        );
        double sum = 0,
            area = 0;
        for (var o = Shape.HeaderSize; o + Shape.QuadSize <= shape.Length; o += Shape.QuadSize)
        {
            int face = shape[o];
            Shape.Axes(face, out var n, out var u, out var v);
            var c = new float[] { shape[o + 1], shape[o + 2], shape[o + 3] };
            c[n] += (face & 1) == 0 ? 1 : 0;
            c[u] += shape[o + 4] / 2f;
            c[v] += shape[o + 5] / 2f;
            var center = origin + new Vector3(c[0], c[1], c[2]) * cellSize;
            var normal = Shape.DecodeNormal(BitConverter.ToUInt16(shape, o + 8));
            var dot = MathHelper.Clamp(Vector3.Dot(normal, SurfaceNormal(center)), -1, 1);
            var a = shape[o + 4] * shape[o + 5];
            sum += Math.Acos(dot) * 180 / Math.PI * a;
            area += a;
        }
        return area > 0 ? sum / area : 0;
    }

    static void Main(string[] args)
    {
        var folder = args[0];
        Directory.CreateDirectory(folder);
        var pyramid = Pyramid();
        foreach (var smooth in new[] { false, true })
        {
            var mode = smooth ? "smooth" : "flat";
            byte[][] shapes = null;
            var best = double.MaxValue;
            for (var run = 0; run < 5; run++)
            {
                var watch = Stopwatch.StartNew();
                shapes = Build(pyramid, smooth);
                best = Math.Min(best, watch.Elapsed.TotalMilliseconds);
            }
            Console.WriteLine($"build {mode} ms={best:0.00}");
            foreach (var shape in shapes)
            {
                if (shape == null)
                    continue;
                File.WriteAllBytes(Path.Combine(folder, $"{mode}-{shape[0]}.bin"), shape);
                var quads = (shape.Length - Shape.HeaderSize) / Shape.QuadSize;
                Console.WriteLine(
                    $"level {mode} lod={shape[0]} quads={quads} bytes={shape.Length} error={NormalError(shape):0.00}"
                );
            }
        }
    }
}
