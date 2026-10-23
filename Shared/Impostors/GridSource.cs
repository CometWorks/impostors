using System;
using System.Collections.Generic;
using System.Linq;
using Impostors;
using Sandbox.Game.Entities;
using Sandbox.Game.Entities.Cube;
using Shared.Config;
using VRage.Game;
using VRageMath;

namespace Shared.Impostors;

// Physical grid groups (subgrids and docked ships move together), each shown as one impostor
// in the frame of its largest grid. Moving parts are frozen at the pose of the last build.
public sealed class GridSource : ImpostorSource
{
    private readonly IPluginConfig config;
    private readonly HashSet<MyCubeGrid> visited = new();
    private readonly List<MyCubeGrid> members = new();

    public GridSource(IPluginConfig config)
    {
        this.config = config;
    }

    public override ImpostorKind Kind => ImpostorKind.Grid;

    // One cube of a block in the frame of the group's main grid
    private struct Sample
    {
        public Vector3 Center;
        public float Size;
        public Vector3I Color;
    }

    public override void Scan(ImpostorRegistry registry)
    {
        visited.Clear();
        foreach (var entity in MyEntities.GetEntities())
        {
            if (entity is not MyCubeGrid grid || visited.Contains(grid) || !Eligible(grid))
                continue;

            members.Clear();
            MyCubeGridGroups.Static.Physical.GetGroupNodes(grid, members);
            if (members.Count == 0)
                members.Add(grid);

            var grids = members.Where(Eligible).ToArray();
            foreach (var member in members)
                visited.Add(member);

            if (grids.Length == 0 || grids.Sum(g => g.BlocksCount) < config.MinBlocks)
                continue;

            // The main grid gives the frame; ties go to the lowest id, so the choice is stable
            var main = grids.OrderByDescending(g => g.BlocksCount).ThenBy(g => g.EntityId).First();
            var entry = registry.Touch(main.EntityId, this);
            entry.Tag = grids;
            entry.WorldMatrix = main.WorldMatrix;
            entry.Velocity = main.Physics?.LinearVelocity ?? Vector3.Zero;

            var aabb = BoundingBoxD.CreateInvalid();
            var signature = 17L;
            foreach (var g in grids)
            {
                aabb.Include(g.PositionComp.WorldAABB);
                unchecked
                {
                    signature = signature * 31 + g.EntityId;
                    signature = signature * 31 + g.BlocksCount;
                    signature = signature * 31 + g.Min.GetHashCode();
                    signature = signature * 31 + g.Max.GetHashCode();
                }
            }
            entry.WorldAabb = aabb;

            if (entry.Signature != signature)
            {
                entry.Signature = signature;
                entry.Stale = true;
            }
        }
    }

    private static bool Eligible(MyCubeGrid grid) => !grid.Closed && !grid.MarkedForClose && !grid.IsPreview && grid.Projector == null && grid.InScene;

    public override Func<byte[][]> PrepareBuild(ImpostorEntry entry)
    {
        if (entry.Tag is not MyCubeGrid[] grids || grids.Length == 0 || grids.Any(g => g.Closed))
            return null;

        var main = grids.FirstOrDefault(g => g.EntityId == entry.Id) ?? grids[0];
        var toMain = MatrixD.Invert(main.WorldMatrix);
        var samples = new List<Sample>(grids.Sum(g => g.BlocksCount));
        var minCellSize = float.MaxValue;
        foreach (var grid in grids)
        {
            var toFrame = grid.WorldMatrix * toMain;
            var size = grid.GridSize;
            minCellSize = Math.Min(minCellSize, size);
            foreach (var block in grid.GetBlocks())
            {
                var color = BlockColor(block);
                for (var x = block.Min.X; x <= block.Max.X; x++)
                for (var y = block.Min.Y; y <= block.Max.Y; y++)
                for (var z = block.Min.Z; z <= block.Max.Z; z++)
                {
                    samples.Add(
                        new Sample
                        {
                            Center = (Vector3)Vector3D.Transform(new Vector3D(x, y, z) * size, toFrame),
                            Size = size,
                            Color = color,
                        }
                    );
                }
            }
        }

        return () => Build(samples, minCellSize);
    }

    private static byte[][] Build(List<Sample> samples, float minCellSize)
    {
        var min = new Vector3(float.MaxValue);
        var max = new Vector3(float.MinValue);
        foreach (var s in samples)
        {
            min = Vector3.Min(min, s.Center - s.Size / 2);
            max = Vector3.Max(max, s.Center + s.Size / 2);
        }
        var box = max - min;

        var shapes = new byte[ImpostorEntry.Lods.Length][];
        for (var i = 0; i < shapes.Length; i++)
        {
            var lod = ImpostorEntry.Lods[i];
            var cellSize = CellVolume.CellSizeFor(box, lod, minCellSize);
            var cells = Vector3I.Ceiling(box / cellSize);
            var volume = new CellVolume(cells, cellSize, min, lod);
            foreach (var s in samples)
                AddSample(volume, s);
            shapes[i] = volume.ToShape();

            // Finer levels would show no more detail than this one
            if (cellSize <= minCellSize)
                break;
        }
        return shapes;
    }

    private static void AddSample(CellVolume volume, Sample s)
    {
        if (s.Size <= volume.CellSize)
        {
            volume.Add(s.Center, s.Color);
            return;
        }

        // A cube larger than a cell covers several of them
        var steps = (int)Math.Ceiling(s.Size / volume.CellSize);
        var step = s.Size / steps;
        var start = s.Center - (s.Size - step) / 2;
        for (var x = 0; x < steps; x++)
        for (var y = 0; y < steps; y++)
        for (var z = 0; z < steps; z++)
            volume.Add(start + new Vector3(x, y, z) * step, s.Color);
    }

    // The painted color for armor, and a duller version of it for functional blocks, whose
    // models are mostly unpainted metal
    private static Vector3I BlockColor(MySlimBlock block)
    {
        var rgb = MyColorPickerConstants.HSVOffsetToHSV(block.ColorMaskHSV).HSVtoColor();
        var color = new Vector3I(rgb.R, rgb.G, rgb.B);
        if (block.FatBlock == null)
            return color;

        var subtype = block.BlockDefinition.Id.SubtypeName;
        if (subtype.Contains("Window") || subtype.Contains("Glass"))
            return new Vector3I(40, 55, 70);

        return (color + new Vector3I(110, 110, 110)) / 2;
    }
}
