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
        public bool Angled;
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

    private static bool Eligible(MyCubeGrid grid) =>
        !grid.Closed
        && !grid.MarkedForClose
        && !grid.IsPreview
        && grid.Projector == null
        && grid.InScene;

    public override Func<byte[][]> PrepareBuild(ImpostorEntry entry)
    {
        if (entry.Tag is not MyCubeGrid[] grids || grids.Length == 0 || grids.Any(g => g.Closed))
            return null;

        var main = grids.FirstOrDefault(g => g.EntityId == entry.Id) ?? grids[0];
        var toMain = MatrixD.Invert(main.WorldMatrix);
        var samples = new List<Sample>(grids.Sum(g => g.BlocksCount));
        foreach (var grid in grids)
        {
            var toFrame = grid.WorldMatrix * toMain;
            var size = grid.GridSize;
            foreach (var block in grid.GetBlocks())
            {
                var color = BlockColor(block);
                var angled = IsAngled(block);
                for (var x = block.Min.X; x <= block.Max.X; x++)
                for (var y = block.Min.Y; y <= block.Max.Y; y++)
                for (var z = block.Min.Z; z <= block.Max.Z; z++)
                {
                    samples.Add(
                        new Sample
                        {
                            Center = (Vector3)
                                Vector3D.Transform(new Vector3D(x, y, z) * size, toFrame),
                            Size = size,
                            Color = color,
                            Angled = angled,
                        }
                    );
                }
            }
        }

        return () => Build(samples, main.GridSize);
    }

    // Cells are a whole number of the main grid's blocks and sit on its block lattice, so at the
    // finest level the main grid comes out block for block: cells of another size cut blocks
    // in uneven parts, which shows as stripes and a shape shifted by up to a cell
    private static byte[][] Build(List<Sample> samples, float lattice)
    {
        var min = new Vector3(float.MaxValue);
        var max = new Vector3(float.MinValue);
        foreach (var s in samples)
        {
            min = Vector3.Min(min, s.Center - s.Size / 2);
            max = Vector3.Max(max, s.Center + s.Size / 2);
        }
        var box = max - min;

        // The main grid's block faces are at (n + 0.5) * lattice in its frame
        var start = (new Vector3(Vector3I.Floor(min / lattice - 0.5f)) + 0.5f) * lattice;

        // Level 8 of a grid saves little against 16 (Rhea, 18k blocks: 104 instead of 246
        // quads, 1.4 KB less) and loses most of the detail, so grids start at 16
        var shapes = new byte[ImpostorEntry.Lods.Length][];
        for (var i = 1; i < shapes.Length; i++)
        {
            var lod = ImpostorEntry.Lods[i];
            var blocks = Math.Max(1, (int)Math.Ceiling(box.AbsMax() / lod / lattice - 0.01f));
            var cellSize = blocks * lattice;
            var cells = Vector3I.Ceiling((max - start) / cellSize - 0.001f);

            // Moved back by whole blocks, so the cells sticking out are shared by both sides
            var spare = new Vector3(cells) * cellSize - box;
            var shift = Vector3.Max(spare / 2 - (min - start), Vector3.Zero) / lattice;
            var origin = start - new Vector3(Vector3I.Floor(shift)) * lattice;
            var volume = new CellVolume(cells, cellSize, origin, lod);
            foreach (var s in samples)
                AddSample(volume, s);
            shapes[i] = volume.ToShape();

            // Finer levels would show no more detail than this one
            if (blocks == 1)
                break;
        }
        shapes[0] = shapes[1];
        return shapes;
    }

    // Fills the cells whose centers are inside the cube, or the one holding its center: a
    // cube marking every cell it touches would make the shape grow by up to a cell per side
    private static void AddSample(CellVolume volume, Sample s)
    {
        var lo = (s.Center - s.Size / 2 - volume.Origin) / volume.CellSize - 0.5f;
        var hi = (s.Center + s.Size / 2 - volume.Origin) / volume.CellSize - 0.5f;
        var first = Vector3I.Ceiling(lo);
        var last = Vector3I.Floor(hi);
        if (first.X > last.X || first.Y > last.Y || first.Z > last.Z)
        {
            volume.Add(s.Center, s.Color, 1, s.Angled);
            return;
        }

        for (var x = first.X; x <= last.X; x++)
        for (var y = first.Y; y <= last.Y; y++)
        for (var z = first.Z; z <= last.Z; z++)
            volume.Add(
                volume.Origin + (new Vector3(x, y, z) + 0.5f) * volume.CellSize,
                s.Color,
                1,
                s.Angled
            );
    }

    // Sloped armor: shaded as a cube, its stair steps alternate between lit and dark faces,
    // which makes the whole grid brighter or darker than the real one depending on the angle
    private static bool IsAngled(MySlimBlock block)
    {
        var subtype = block.BlockDefinition.Id.SubtypeName;
        return subtype.Contains("Slope") || subtype.Contains("Corner") || subtype.Contains("Round");
    }

    // The painted color: block models are painted over most of their surface, functional
    // blocks too. Windows show the dark space behind them.
    private static Vector3I BlockColor(MySlimBlock block)
    {
        var subtype = block.BlockDefinition.Id.SubtypeName;
        if (block.FatBlock != null && (subtype.Contains("Window") || subtype.Contains("Glass")))
            return new Vector3I(40, 55, 70);

        var rgb = MyColorPickerConstants.HSVOffsetToHSV(block.ColorMaskHSV).HSVtoColor();
        return new Vector3I(rgb.R, rgb.G, rgb.B);
    }
}
