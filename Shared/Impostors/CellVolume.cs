using System;
using Impostors;
using VRageMath;

namespace Shared.Impostors;

// A coarse cell grid with an average color per occupied cell, turned into an impostor shape
// by greedy meshing its surface faces. Grids and voxel maps both build their shapes this way.
public sealed class CellVolume
{
    public readonly Vector3I Size;
    public readonly float CellSize;
    public readonly Vector3 Origin;
    public readonly byte Lod;

    private readonly int[] red;
    private readonly int[] green;
    private readonly int[] blue;
    private readonly int[] weight;

    public CellVolume(Vector3I size, float cellSize, Vector3 origin, byte lod)
    {
        Size = Vector3I.Clamp(size, Vector3I.One, new Vector3I(255));
        CellSize = cellSize;
        Origin = origin;
        Lod = lod;

        var count = Size.X * Size.Y * Size.Z;
        red = new int[count];
        green = new int[count];
        blue = new int[count];
        weight = new int[count];
    }

    // Cells along the longest axis of the box and the cell size for that, at least minCellSize
    public static float CellSizeFor(Vector3 boxSize, int lod, float minCellSize)
    {
        return Math.Max(boxSize.AbsMax() / lod, minCellSize);
    }

    public bool TryGetCell(Vector3 local, out Vector3I cell)
    {
        var p = (local - Origin) / CellSize;
        cell = new Vector3I((int)Math.Floor(p.X), (int)Math.Floor(p.Y), (int)Math.Floor(p.Z));
        return cell.X >= 0
            && cell.Y >= 0
            && cell.Z >= 0
            && cell.X < Size.X
            && cell.Y < Size.Y
            && cell.Z < Size.Z;
    }

    // Adds color to the cell holding this local position, color components 0..255
    public void Add(Vector3 local, Vector3I color, int w = 1)
    {
        if (!TryGetCell(local, out var cell))
            return;

        var i = Index(cell.X, cell.Y, cell.Z);
        red[i] += color.X * w;
        green[i] += color.Y * w;
        blue[i] += color.Z * w;
        weight[i] += w;
    }

    private int Index(int x, int y, int z) => (z * Size.Y + y) * Size.X + x;

    private bool Filled(int x, int y, int z)
    {
        if (x < 0 || y < 0 || z < 0 || x >= Size.X || y >= Size.Y || z >= Size.Z)
            return false;
        return weight[Index(x, y, z)] > 0;
    }

    // RGB565 of the cell's average color, quantized to 4 bits per channel first, so
    // neighboring cells of nearly the same color merge into one quad
    private ushort Color(int i)
    {
        var w = weight[i];
        var r = (red[i] / w + 8) / 17 * 17;
        var g = (green[i] / w + 8) / 17 * 17;
        var b = (blue[i] / w + 8) / 17 * 17;
        return Shape.ToRgb565(Math.Min(r, 255), Math.Min(g, 255), Math.Min(b, 255));
    }

    public int FilledCount
    {
        get
        {
            var count = 0;
            foreach (var w in weight)
                if (w > 0)
                    count++;
            return count;
        }
    }

    // The six face directions and their normal codes, for flat shading
    private static readonly Vector3[] FaceVectors =
    [
        Vector3.Right,
        Vector3.Left,
        Vector3.Up,
        Vector3.Down,
        Vector3.Backward,
        Vector3.Forward,
    ];

    private static readonly ushort[] FaceNormals = Array.ConvertAll(
        FaceVectors,
        Shape.EncodeNormal
    );

    // The occupancy blurred by a Gaussian of this width, in cells; smoothed normals are its
    // gradient, so neighboring cells get nearly the same normal and shade without speckle
    private const float SmoothSigma = 1.5f;
    private const int SmoothRadius = 3;

    private float[] density;

    private void BlurOccupancy()
    {
        var kernel = new float[2 * SmoothRadius + 1];
        for (var k = -SmoothRadius; k <= SmoothRadius; k++)
            kernel[k + SmoothRadius] = (float)Math.Exp(-k * k / (2 * SmoothSigma * SmoothSigma));

        var source = new float[weight.Length];
        for (var i = 0; i < source.Length; i++)
            source[i] = weight[i] > 0 ? 1 : 0;

        // Separable: along X, then Y, then Z; outside the volume is empty
        var target = new float[source.Length];
        var size = new[] { Size.X, Size.Y, Size.Z };
        var p = new int[3];
        for (var axis = 0; axis < 3; axis++)
        {
            for (p[2] = 0; p[2] < Size.Z; p[2]++)
            for (p[1] = 0; p[1] < Size.Y; p[1]++)
            for (p[0] = 0; p[0] < Size.X; p[0]++)
            {
                var sum = 0f;
                var center = p[axis];
                for (var k = -SmoothRadius; k <= SmoothRadius; k++)
                {
                    var c = center + k;
                    if (c < 0 || c >= size[axis])
                        continue;
                    p[axis] = c;
                    sum += kernel[k + SmoothRadius] * source[Index(p[0], p[1], p[2])];
                }
                p[axis] = center;
                target[Index(p[0], p[1], p[2])] = sum;
            }
            (source, target) = (target, source);
        }
        density = source;
    }

    private float Density(int x, int y, int z)
    {
        if (x < 0 || y < 0 || z < 0 || x >= Size.X || y >= Size.Y || z >= Size.Z)
            return 0;
        return density[Index(x, y, z)];
    }

    // The surface normal at a filled cell: down the blurred occupancy. Where it points nearly
    // along the face or away from it, it is tilted towards the face's own normal, gradually,
    // so the shading has no steps there.
    private ushort SmoothNormal(int x, int y, int z, int face)
    {
        var gradient = new Vector3(
            Density(x - 1, y, z) - Density(x + 1, y, z),
            Density(x, y - 1, z) - Density(x, y + 1, z),
            Density(x, y, z - 1) - Density(x, y, z + 1)
        );
        if (gradient.LengthSquared() < 1e-8f)
            return FaceNormals[face];

        var normal = Vector3.Normalize(gradient);
        var facing = Vector3.Dot(normal, FaceVectors[face]);
        if (facing < 0.3f)
            normal = Vector3.Normalize(normal + FaceVectors[face] * (0.3f - facing) * 2);
        return Shape.EncodeNormal(normal);
    }

    // Encodes the surface as a shape (see Shape in the protocol). Smooth normals shade curved
    // surfaces like asteroids, flat ones suit grids.
    public byte[] ToShape(bool smooth = false)
    {
        if (smooth)
            BlurOccupancy();

        var writer = new ByteWriter(4096);
        writer.Byte(Lod);
        writer.Float(CellSize);
        writer.Float(Origin.X);
        writer.Float(Origin.Y);
        writer.Float(Origin.Z);
        var countPosition = writer.Length;
        writer.UShort(0);

        var quads = 0;
        var cell = new int[3];
        var size = new[] { Size.X, Size.Y, Size.Z };
        for (var face = 0; face < 6; face++)
        {
            Shape.Axes(face, out var n, out var u, out var v);
            var step = (face & 1) == 0 ? 1 : -1;
            var sizeU = size[u];
            var sizeV = size[v];

            // 1 + color + normal << 16 of the visible faces in one slice, 0 where there is none
            var mask = new long[sizeU * sizeV];
            for (var s = 0; s < size[n]; s++)
            {
                for (var j = 0; j < sizeV; j++)
                {
                    for (var i = 0; i < sizeU; i++)
                    {
                        cell[n] = s;
                        cell[u] = i;
                        cell[v] = j;
                        mask[j * sizeU + i] = 0;
                        if (!Filled(cell[0], cell[1], cell[2]))
                            continue;

                        var index = Index(cell[0], cell[1], cell[2]);
                        var normal = smooth
                            ? SmoothNormal(cell[0], cell[1], cell[2], face)
                            : FaceNormals[face];
                        cell[n] = s + step;
                        if (!Filled(cell[0], cell[1], cell[2]))
                            mask[j * sizeU + i] = 1 + Color(index) + ((long)normal << 16);
                    }
                }

                // Greedy merge: grow each quad along U, then along V while the whole row matches
                for (var j = 0; j < sizeV; j++)
                {
                    for (var i = 0; i < sizeU; )
                    {
                        var key = mask[j * sizeU + i];
                        if (key == 0)
                        {
                            i++;
                            continue;
                        }

                        var width = 1;
                        while (
                            i + width < sizeU && width < 255 && mask[j * sizeU + i + width] == key
                        )
                            width++;

                        var height = 1;
                        while (
                            j + height < sizeV
                            && height < 255
                            && RowMatches(mask, sizeU, i, j + height, width, key)
                        )
                            height++;

                        for (var y = 0; y < height; y++)
                        for (var x = 0; x < width; x++)
                            mask[(j + y) * sizeU + i + x] = 0;

                        i += width;
                        if (quads == ushort.MaxValue)
                            continue;

                        cell[n] = s;
                        cell[u] = i - width;
                        cell[v] = j;
                        writer.Byte((byte)face);
                        writer.Byte((byte)cell[0]);
                        writer.Byte((byte)cell[1]);
                        writer.Byte((byte)cell[2]);
                        writer.Byte((byte)width);
                        writer.Byte((byte)height);
                        writer.UShort((ushort)(key - 1));
                        writer.UShort((ushort)((key - 1) >> 16));
                        quads++;
                    }
                }
            }
        }

        writer.UShortAt(countPosition, (ushort)quads);
        return writer.ToArray();
    }

    private static bool RowMatches(long[] mask, int sizeU, int i, int j, int width, long key)
    {
        for (var x = 0; x < width; x++)
            if (mask[j * sizeU + i + x] != key)
                return false;
        return true;
    }
}
