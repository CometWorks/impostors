using System;
using System.Collections.Generic;
using Impostors;
using VRageMath;

namespace Shared.Impostors;

// The shapes of an asteroid from voxels read from its storage, apart from the game, so
// tests/asteroid_normals.py builds them offline. See VoxelSource for the reading.
public static class VoxelShapes
{
    // Cells across the content at the coarsest level, at least
    private const int CoarsestCells = 6;

    // The game's iso level: voxels with more content are inside
    private const byte IsoLevel = 127;

    // Share of its voxels inside the rock from which a pooled cell is filled. Half of them
    // leaves the silhouette smaller: the cells along its edge hold the thin rim of the rock,
    // and at half levels 8 and 16 covered 0.8 and 0.93 of the real asteroid's area. 0.3
    // covers 1.00 and 1.01 of it, median over every 20th asteroid of the Seed 2 ring
    // (tests/asteroid_levels.py).
    private const float PooledFill = 0.3f;

    // Version of the smooth normals, in the content hash of smooth asteroids only: bump it when
    // they change, so caches and asteroid packs don't serve shapes of the old ones
    public const byte SmoothVersion = 1;

    // Version of how the levels are read and pooled, in the content hash of every asteroid:
    // bump it when that changes, for the same reason. 2: levels 8 and 16 pooled at PooledFill
    // (SE-722); 1 before, when the hash had none.
    public const byte LevelsVersion = 2;

    // The levels read from the storage, as (index in ImpostorEntry.Lods, voxel level of
    // detail), from 8 up to the first that reads single voxels; levels 4 and 2 are downsampled
    // from 8
    public static List<(int index, int voxelLod)> Levels(Vector3I size)
    {
        var levels = new List<(int index, int voxelLod)>();
        for (var i = Array.IndexOf(ImpostorEntry.Lods, (byte)8); i < ImpostorEntry.Lods.Length; i++)
        {
            // The coarsest voxel level of detail with at least this many voxels along the longest axis
            var voxelLod = Math.Max(
                0,
                (int)Math.Floor(Math.Log(size.AbsMax() / (double)ImpostorEntry.Lods[i], 2))
            );
            levels.Add((i, voxelLod));
            if (voxelLod == 0)
                break;
        }
        return levels;
    }

    // The voxel level of detail to read for levels[k]: the two coarsest levels are pooled
    // from finer voxels, see Build
    public static int ReadLod(List<(int index, int voxelLod)> levels, int k) =>
        Math.Max(0, levels[k].voxelLod - PoolSteps(levels, k));

    // How many voxel levels of detail finer than its own a level is read at: two for the two
    // coarsest levels. The game's coarse voxels are point samples of the rock
    // (MyCompositeShapeProvider samples its shape at the voxel's corner, an edited storage
    // keeps one of the 8 finer voxels), which miss the thin parts: read at their own level,
    // level 8 looked half as large as level 16, and level 16 covered 0.7 of the real
    // asteroid's area (tests/asteroid_levels.py).
    private static int PoolSteps(List<(int index, int voxelLod)> levels, int k) =>
        k <= 1 && k < levels.Count - 1 && levels[k].voxelLod > 0 ? 2 : 0;

    private static bool Pooled(List<(int index, int voxelLod)> levels, int k) =>
        PoolSteps(levels, k) > 0;

    // The voxels read at ReadLod of each level: content and material, X fastest, then Y, then Z
    public static Vector3I ReadSize(Vector3I size, int readLod) =>
        Vector3I.Max(Vector3I.One, size >> readLod);

    // Shapes in the places of ImpostorEntry.Lods. Smooth normals follow the voxel content,
    // flat ones the cells, which shows their blocks on the coarse levels.
    public static byte[][] Build(
        Vector3I size,
        Vector3 corner,
        Vector3I[] colors,
        bool occluded,
        bool smooth,
        List<(int index, int voxelLod)> levels,
        byte[][] contents,
        byte[][] materials
    )
    {
        var shapes = new byte[ImpostorEntry.Lods.Length][];
        var first = Array.IndexOf(ImpostorEntry.Lods, (byte)8);

        // Finest first, for the extent of the content. The two coarsest levels are pooled from
        // finer voxels (PoolSteps), a cell filled when PooledFill of them are. The coarsest
        // also gets at least CoarsestCells cells across a rock that is small for its storage.
        var extent = 0f;
        CellVolume level8 = null;
        for (var k = levels.Count - 1; k >= 0; k--)
        {
            var (i, voxelLod) = levels[k];
            var cellSize = (float)(1 << voxelLod);
            var pooled = Pooled(levels, k);
            if (k == 0 && pooled && extent > 0)
                cellSize = Math.Min(cellSize, extent / CoarsestCells);
            voxelLod = ReadLod(levels, k);

            var voxels = ReadSize(size, voxelLod);
            var voxelSize = (float)(1 << voxelLod);
            var content = contents[k];
            var material = materials[k];
            contents[k] = materials[k] = null;

            var cells = Vector3I.Ceiling(new Vector3(size) / cellSize);
            var volume = new CellVolume(cells, cellSize, corner, ImpostorEntry.Lods[i]);

            // The share of a cell one voxel fills at full density
            var share = (float)Math.Pow(Math.Min(voxelSize / cellSize, 1), 3) / 255;
            var min = new Vector3I(int.MaxValue);
            var max = new Vector3I(int.MinValue);
            var p = Vector3I.Zero;
            var n = 0;
            for (p.Z = 0; p.Z < voxels.Z; p.Z++)
            for (p.Y = 0; p.Y < voxels.Y; p.Y++)
            for (p.X = 0; p.X < voxels.X; p.X++, n++)
            {
                var density = content[n];
                if (density == 0)
                    continue;
                var local = corner + (new Vector3(p) + 0.5f) * voxelSize;
                if (smooth)
                    volume.AddFill(local, density * share);
                if (density <= IsoLevel)
                    continue;
                volume.Add(local, colors[material[n]]);
                min = Vector3I.Min(min, p);
                max = Vector3I.Max(max, p);
            }

            if (pooled)
                volume.DropBelow(
                    Math.Max(1, (int)Math.Round(Math.Pow(cellSize / voxelSize, 3) * PooledFill))
                );
            if (extent == 0 && max.X >= min.X)
                extent = (max - min + 1).AbsMax() * voxelSize;
            shapes[i] = volume.ToShape(smooth: true, occluded);
            BuildTiming.Mark(ImpostorEntry.Lods[i], shapes[i]);
            if (i == first)
                level8 = volume;
        }

        // Far asteroids cover a few pixels, where only their color is left to see. A coarse
        // cell is filled when half of its 8 children are, which keeps the volume; a thin rock
        // that would vanish that way keeps every cell with a filled child.
        var coarse = level8;
        for (var i = first - 1; i >= 0 && coarse != null; i--)
        {
            var finer = coarse;
            coarse = finer.Downsample(ImpostorEntry.Lods[i], 4);
            shapes[i] = coarse.ToShape(smooth: true, occluded);
            if (shapes[i].Length <= Shape.HeaderSize)
            {
                coarse = finer.Downsample(ImpostorEntry.Lods[i], 1);
                shapes[i] = coarse.ToShape(smooth: true, occluded);
            }
            BuildTiming.Mark(ImpostorEntry.Lods[i], shapes[i]);
        }
        return shapes;
    }
}
