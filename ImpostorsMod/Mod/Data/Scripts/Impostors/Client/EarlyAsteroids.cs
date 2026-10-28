using System;
using System.Collections.Generic;
using VRage.Library.Utils;
using VRage.ModAPI;
using VRage.Utils;
using VRageMath;

namespace Impostors
{
    // Asteroids the client shows before the server sends them (SE-712). The world's asteroid
    // pack and the disk cache know where the asteroids are and have their shapes, so the
    // client draws them as soon as the world is loaded, or the player arrives somewhere, at the
    // levels the server would pick, instead of waiting for the server to pick, announce and
    // send each one. Asteroids rarely change. When the server's state for one comes, the
    // impostor is the server's from then on: its shape stays while the version matches, a
    // changed asteroid keeps the old shape until the server's arrives.
    //
    // The impostors are the renderer's, one per id, marked Early until the server sends a
    // state. Ids the server sent a state for or removed are left to it until the next server
    // instance. An early impostor stands in like one of the server's: it hides once the real
    // voxel map draws (see ImpostorRenderer.SendReady), but the server isn't told about it.
    public class EarlyAsteroids
    {
        // As the server picks: about one cell per 2.5 pixels on a 1080p screen at 70 degrees
        private const double CellsPerPixel = 1 / 2.5;

        // Looked up again when the view moved this far or turned by an eighth of its field of
        // view, at most every 10 ticks, and every second anyway
        private const double MoveMeters = 1000;
        private const int PickTicks = 60;

        // Shapes read per tick, milliseconds of the game thread
        private const double LoadMilliseconds = 2;

        private class Known
        {
            public ImpostorState State;
            public byte[] Lods;
            public int[] Lengths;

            // A changed asteroid the server announced, its levels still arriving
            public bool Noticed;
        }

        private struct Candidate
        {
            public Known Known;
            public double Pixels;
            public bool InView;
            public byte Lod;
        }

        private readonly Dictionary<long, ClientImpostor> impostors;
        private readonly ShapeCache cache;
        private readonly AsteroidPack pack;
        private readonly Dictionary<long, Known> known = new Dictionary<long, Known>();
        private readonly HashSet<long> served = new HashSet<long>();
        private readonly List<Candidate> candidates = new List<Candidate>();
        private readonly HashSet<long> wanted = new HashSet<long>();
        private readonly List<long> dropped = new List<long>();
        private readonly MyGameTimer timer = new MyGameTimer();
        private int next;
        private bool due = true;
        private int pickTick;
        private Vector3D pickEye;
        private Vector3 pickForward;
        private bool failed;

        // Stats: impostors shown early, shapes read and their time, the server's states that
        // kept the early shape and those that changed it, the last of those as id:early
        // version:server version:tick
        public int Count { get; private set; }
        public int Loaded { get; private set; }
        public long LoadTicks { get; private set; }
        public int Kept { get; private set; }
        public int Swapped { get; private set; }

        // Changed asteroids the server announced, and the levels of them it sent
        public int Notices { get; private set; }
        public int NoticeLevels { get; private set; }
        public readonly List<string> SwapLog = new List<string>();

        public int KnownCount
        {
            get { return known.Count; }
        }

        public EarlyAsteroids(
            Dictionary<long, ClientImpostor> impostors,
            ShapeCache cache,
            AsteroidPack pack
        )
        {
            this.impostors = impostors;
            this.cache = cache;
            this.pack = pack;
            Reset();
        }

        // A new server instance, or the session started over: everything is early again, with
        // the asteroids the cache has learned since. The renderer dropped its impostors.
        public void Reset()
        {
            served.Clear();
            candidates.Clear();
            Count = 0;
            next = 0;
            due = true;
            known.Clear();
            if (pack != null)
                pack.ForEachFrame(Add);

            // The cache's state is the one the server sent last, it wins over the pack's. Of the
            // pack's version the cache usually has a few levels only, those received before
            // the client picked the levels itself (SE-744): they add to the pack's.
            if (cache != null)
                cache.ForEachAsteroid(Add);
        }

        private void Add(ImpostorState state, byte[] lods, int[] lengths)
        {
            Known k;
            if (known.TryGetValue(state.Id, out k) && k.State.Version == state.Version)
            {
                foreach (var lod in lods)
                {
                    if (Array.IndexOf(k.Lods, lod) < 0)
                        AddLevel(k, lod, lengths[Array.IndexOf(lods, lod)]);
                }
                return;
            }
            known[state.Id] = new Known
            {
                State = state,
                Lods = lods,
                Lengths = lengths,
            };
        }

        // The server announced a changed asteroid when the client picks the asteroids itself
        // (SE-739): all of its levels follow, the cache keeps them, and the client shows that
        // version from then on, the old one until the levels it picks arrive
        public void Notice(ImpostorState state)
        {
            known[state.Id] = new Known
            {
                State = state,
                Lods = new byte[0],
                Lengths = new int[0],
                Noticed = true,
            };
            Notices++;
            due = true;
        }

        public bool Noticed(long id, int version)
        {
            Known k;
            return known.TryGetValue(id, out k) && k.Noticed && k.State.Version == version;
        }

        public void AddLevel(long id, byte lod, int length)
        {
            var k = known[id];
            if (Array.IndexOf(k.Lods, lod) >= 0)
                return;
            AddLevel(k, lod, length);
            NoticeLevels++;
            due = true;
        }

        private static void AddLevel(Known k, byte lod, int length)
        {
            var lods = new byte[k.Lods.Length + 1];
            var lengths = new int[lods.Length];
            Array.Copy(k.Lods, lods, k.Lods.Length);
            Array.Copy(k.Lengths, lengths, k.Lengths.Length);
            lods[k.Lods.Length] = lod;
            lengths[k.Lods.Length] = length;
            k.Lods = lods;
            k.Lengths = lengths;
        }

        // The server sent a state for this id, or removed it: the impostor is the server's
        public void Served(long id, ClientImpostor impostor, int version, int tick)
        {
            served.Add(id);
            if (impostor == null || !impostor.Early)
                return;
            impostor.Early = false;
            Count--;
            if (impostor.Shape != null && impostor.Shape.Version == version)
            {
                Kept++;
                return;
            }
            Swapped++;
            if (SwapLog.Count == 8)
                SwapLog.RemoveAt(0);
            SwapLog.Add(
                id
                    + ":"
                    + (impostor.Shape != null ? impostor.Shape.Version : 0)
                    + ":"
                    + version
                    + ":"
                    + tick
            );
        }

        // Every 10 ticks: looks the asteroids up again when the view moved, turned or a second
        // went by, then reads the shapes that are due within the time per tick
        public void Update(
            IMyCamera camera,
            int tick,
            double range,
            double minPixels,
            double drawnRange,
            int budget
        )
        {
            if (failed || camera == null)
                return;
            try
            {
                if (tick % 10 == 0 && Due(camera, tick))
                    Pick(camera, tick, range, minPixels, drawnRange, budget);
                Load(tick);
            }
            catch (Exception e)
            {
                // A mod's exception would end the game; without early asteroids the server's
                // still come
                failed = true;
                MyLog.Default.WriteLineAndConsole("Impostors: early asteroids failed: " + e);
            }
        }

        private bool Due(IMyCamera camera, int tick)
        {
            var forward = (Vector3)camera.WorldMatrix.Forward;
            return due
                || tick - pickTick >= PickTicks
                || Vector3D.DistanceSquared(camera.Position, pickEye) > MoveMeters * MoveMeters
                || Vector3.Dot(forward, pickForward) < Math.Cos(camera.FovWithZoom / 8);
        }

        // The asteroids that would be shown from here and their levels, in view and large ones
        // first. Not those within drawnRange, where the game draws the real ones, nor those
        // whose real voxel map draws already.
        private void Pick(
            IMyCamera camera,
            int tick,
            double range,
            double minPixels,
            double drawnRange,
            int budget
        )
        {
            due = false;
            pickTick = tick;
            pickEye = camera.Position;
            pickForward = (Vector3)camera.WorldMatrix.Forward;
            var view = MatrixD.Transpose(camera.WorldMatrix);

            candidates.Clear();
            wanted.Clear();
            foreach (var k in known.Values)
            {
                var id = k.State.Id;
                if (served.Contains(id) || k.Lods.Length == 0)
                    continue;
                var radius = k.State.Radius;
                var distance = Vector3D.Distance(pickEye, k.State.Position);
                if (distance - radius > range || distance - radius <= drawnRange)
                    continue;
                var pixels = radius / Math.Max(distance, 1) * Protocol.PixelsPerRadian;
                if (2 * pixels < minPixels)
                    continue;
                wanted.Add(id);

                // The game draws the real one, the impostor hides (SE-744): its shape stays for
                // when it unloads, but it takes no quads from the others. Near ones would want
                // the finest levels and leave coarse ones to the rest.
                ClientImpostor shown;
                if (impostors.TryGetValue(id, out shown) && shown.Early && shown.Ready)
                    continue;
                candidates.Add(
                    new Candidate
                    {
                        Known = k,
                        Pixels = pixels,
                        InView = ImpostorRenderer.InView(camera, view, k.State.Position, radius),
                    }
                );
            }

            dropped.Clear();
            foreach (var impostor in impostors.Values)
            {
                if (impostor.Early && !wanted.Contains(impostor.State.Id))
                    dropped.Add(impostor.State.Id);
            }
            foreach (var id in dropped)
                impostors.Remove(id);
            Count -= dropped.Count;

            FitLevels(budget);
            candidates.Sort(
                (a, b) => a.InView != b.InView ? (a.InView ? -1 : 1) : b.Pixels.CompareTo(a.Pixels)
            );
            next = 0;
        }

        // The levels the server would pick: their quads together, half of each, within the
        // budget, or all of them picked for a smaller screen
        private void FitLevels(int budget)
        {
            if (Levels(1, budget) <= budget)
                return;
            double low = 0,
                high = 1;
            for (var step = 0; step < 12; step++)
            {
                var scale = (low + high) / 2;
                if (Levels(scale, budget) <= budget)
                    low = scale;
                else
                    high = scale;
            }
            Levels(low, budget);
        }

        private int Levels(double scale, int budget)
        {
            var quads = 0;
            for (var i = 0; i < candidates.Count; i++)
            {
                var c = candidates[i];
                ClientImpostor impostor;
                var current =
                    scale >= 1
                    && impostors.TryGetValue(c.Known.State.Id, out impostor)
                    && impostor.Shape != null
                        ? impostor.Shape.Lod
                        : (byte)0;
                var k = Level(c.Known, c.Pixels * scale, current);
                c.Lod = c.Known.Lods[k];
                quads += (c.Known.Lengths[k] - Shape.HeaderSize) / Shape.QuadSize / 2;
                candidates[i] = c;
            }
            return quads;
        }

        // Index of the coarsest level at or above the wanted one, or of the finest, with some
        // hysteresis around the current one
        private static int Level(Known k, double pixels, byte current)
        {
            var wanted = 2 * pixels * CellsPerPixel;
            var best = -1;
            var finest = 0;
            for (var i = 0; i < k.Lods.Length; i++)
            {
                if (k.Lods[i] > k.Lods[finest])
                    finest = i;
                if (k.Lods[i] >= wanted && (best < 0 || k.Lods[i] < k.Lods[best]))
                    best = i;
            }
            if (best < 0)
                best = finest;
            var kept = Array.IndexOf(k.Lods, current);
            if (kept >= 0 && k.Lods[best] < current && wanted > current * 0.4)
                return kept;
            return best;
        }

        // Reads the picked shapes the impostors don't have yet
        private void Load(int tick)
        {
            if (next >= candidates.Count)
                return;
            var start = timer.ElapsedTicks;
            var limit = (long)(LoadMilliseconds * MyGameTimer.Frequency / 1000);
            while (next < candidates.Count && timer.ElapsedTicks - start < limit)
            {
                var c = candidates[next++];
                var state = c.Known.State;
                if (served.Contains(state.Id))
                    continue;
                ClientImpostor impostor;
                if (impostors.TryGetValue(state.Id, out impostor) && !impostor.Early)
                    continue;
                if (
                    impostor != null
                    && impostor.Shape != null
                    && impostor.Shape.Lod == c.Lod
                    && impostor.Shape.Version == state.Version
                )
                    continue;

                // The pack has the levels of its version, the cache the others
                var data = pack != null ? pack.Get(state.Id, state.Version, c.Lod) : null;
                if (data == null && cache != null)
                    data = cache.Get(state.Id, state.Version, c.Lod);
                if (data == null)
                    continue;
                var shape = ClientShape.Read(new ByteReader(data), state.Version);
                if (impostor == null)
                {
                    impostor = new ClientImpostor { Early = true, ReceivedTick = tick };
                    impostors[state.Id] = impostor;
                    Count++;
                }
                impostor.State = state;
                impostor.State.Lod = c.Lod;
                impostor.Shape = shape;
                impostor.ShapeTick = tick;
                if (impostor.FirstShapeTick < 0)
                    impostor.FirstShapeTick = tick;
                Loaded++;
            }
            LoadTicks += timer.ElapsedTicks - start;
        }
    }
}
