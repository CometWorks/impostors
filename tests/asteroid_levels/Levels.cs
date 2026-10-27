using System;
using System.IO;
using Shared.Impostors;
using VRageMath;

// Builds the shapes of real asteroids through VoxelShapes, the code the server runs, from the
// content the test probe dumped (IMP_VOXEL_DUMP, see VoxelDump in tests/probe): every level of
// every <id>.vxd in the first argument's folder, as <id>-<level>.bin in the second's, for
// tests/asteroid_levels.py. All voxels are stone, smooth normals, no occlusion.
static class Levels
{
    static void Main(string[] args)
    {
        Directory.CreateDirectory(args[1]);
        var colors = new Vector3I[256];
        for (var i = 0; i < colors.Length; i++)
            colors[i] = new Vector3I(170, 170, 170);
        foreach (var path in Directory.GetFiles(args[0], "*.vxd"))
        {
            var id = Path.GetFileNameWithoutExtension(path);
            using var file = new BinaryReader(File.OpenRead(path));
            var size = new Vector3I(file.ReadInt32(), file.ReadInt32(), file.ReadInt32());
            var pyramid = new byte[16][];
            var count = file.ReadInt32();
            for (var k = 0; k < count; k++)
            {
                var lod = file.ReadInt32();
                var voxels = Vector3I.Max(Vector3I.One, size >> lod);
                pyramid[lod] = file.ReadBytes(voxels.X * voxels.Y * voxels.Z);
            }

            var levels = VoxelShapes.Levels(size);
            var contents = new byte[levels.Count][];
            var materials = new byte[levels.Count][];
            var missing = false;
            for (var k = 0; k < levels.Count; k++)
            {
                contents[k] = pyramid[VoxelShapes.ReadLod(levels, k)];
                missing |= contents[k] == null;
                materials[k] = contents[k] == null ? null : new byte[contents[k].Length];
            }
            if (missing)
            {
                Console.WriteLine($"skipped {id}: the dump lacks a level");
                continue;
            }
            var shapes = VoxelShapes.Build(
                size,
                Vector3.Zero,
                colors,
                false,
                true,
                levels,
                contents,
                materials
            );
            foreach (var shape in shapes)
                if (shape != null)
                    File.WriteAllBytes(Path.Combine(args[1], $"{id}-{shape[0]}.bin"), shape);
        }
    }
}
