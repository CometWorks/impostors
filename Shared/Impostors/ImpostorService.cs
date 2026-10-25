using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using Impostors;
using Sandbox.Engine.Multiplayer;
using Sandbox.Game.Multiplayer;
using Sandbox.Game.World;
using Sandbox.ModAPI;
using Shared.Config;
using Shared.Logging;
using VRage.Network;
using VRageMath;

namespace Shared.Impostors;

// Runs where the world is simulated (dedicated server or lobby host): picks the impostors
// each player with the Impostors mod should see and sends them over mod networking.
public sealed class ImpostorService : IDisposable
{
    private const double PixelsPerRadian = Protocol.PixelsPerRadian;

    // Cells per on-screen pixel of an impostor's diameter, about one cell per 2.5 pixels
    private const double CellsPerPixel = 1 / 2.5;

    // Zoomed views beyond this are treated as this much, finer levels give nothing more
    private const double MaxZoom = 32;

    // A finer level sent for a zoomed view stays this long after the zoom ended, seconds
    private const double ZoomKeepSeconds = 30;

    // A new impostor that comes while the player is out of shape credit gets a level of at most
    // this many bytes first, the finer one follows once the credit is paid off. Turning a zoomed
    // view over an asteroid ring brings dozens of new ones at once, at their finest levels
    // (about 150 KB each) that was minutes of debt, and no other impostor got a finer level
    // in the meantime.
    private const int DebtShapeBytes = 2048;

    private static ImpostorService instance;
    private static MySession session;

    private readonly IPluginConfig config;
    private readonly IPluginLogger log;
    private readonly ImpostorRegistry registry;
    private readonly List<ImpostorSource> sources = new();
    private readonly Dictionary<ulong, Viewer> viewers = new();
    private readonly long instanceId = DateTime.UtcNow.Ticks ^ Environment.TickCount;
    private readonly ByteWriter writer = new(Protocol.MaxMessageSize + 1024);
    private int tick;

    private sealed class Viewer
    {
        public readonly ulong SteamId;
        public readonly Dictionary<long, Sent> Sent = new();

        // The shapes of the impostors removed from this player, which its client keeps
        public readonly ShapeCache<Sent> Cache = new();

        // Where the server picked for this player last
        public Vector3D Eye;

        // Counted for the log line once a minute
        public int Picks;
        public long PickTicks;
        public long MaxPickTicks;
        public int Shapes;
        public long ShapeBytes;
        public int States;
        public int Removed;
        public int Restored;
        public int MaxWaiting;

        // Shape bytes this player may still get, negative after a shape larger than what was
        // left: the next seconds pay that off
        public int ShapeCredit;
        public int LastTick;

        // The zoomed view of the client's last View message: how many times larger it shows
        // things than the default estimate (1 when not zoomed), its direction and the half
        // angle of its diagonal
        public double Zoom = 1;
        public Vector3D Forward;
        public double HalfAngle;

        // Impostors whose new level waits for shape credit at the last pick
        public int Waiting;

        // A View message arrived, the impostors are picked again on the next tick
        public bool Dirty;

        public Viewer(ulong steamId)
        {
            SteamId = steamId;
        }
    }

    private sealed class Sent
    {
        public int Version;

        // The level of the shape the client has, its size and the level of the last state, which
        // is a finer one while its shape waits for shape credit
        public byte Lod;
        public int Bytes;
        public byte StateLod;

        // The level picked before a safe zone lowered it, for the hysteresis
        public byte Picked;
        public Color Tint;
        public Vector3D Position;
        public Quaternion Orientation;
        public Vector3 Velocity;

        // A finer level sent for a zoomed view is not replaced by a coarser one before this
        public double KeepUntil;
    }

    // Extra sources registered by the plugin, for example the cluster bridge
    public static readonly List<Func<ImpostorSource>> ExtraSources = new();

    // Builds every entry, not only the ones this server's players see
    public static bool BuildAll;

    public static ImpostorService Instance => instance;

    public IEnumerable<ImpostorEntry> Entries => registry.Entries;

    private ImpostorService(IPluginConfig config, IPluginLogger log)
    {
        this.config = config;
        this.log = log;
        registry = new ImpostorRegistry(log);
        if (config.Grids)
            sources.Add(new GridSource(config));
        if (config.Asteroids)
            sources.Add(new VoxelSource(config));
        if (config.SafeZones)
            sources.Add(new SafeZoneSource());
        foreach (var factory in ExtraSources)
            sources.Add(factory());

        MyAPIGateway.Multiplayer.RegisterSecureMessageHandler(Protocol.Channel, OnMessage);
        log.Info($"Impostors service started, instance {instanceId:x16}");
    }

    public void Dispose()
    {
        MyAPIGateway.Multiplayer?.UnregisterSecureMessageHandler(Protocol.Channel, OnMessage);
        log.Info("Impostors service stopped");
    }

    // Called on every game update by the plugin, starts and stops the service with the session
    public static void UpdateStatic(IPluginConfig config, IPluginLogger log)
    {
        var current = MySession.Static;
        if (session != current || current == null || !current.Ready)
        {
            instance?.Dispose();
            instance = null;
            session = current;
            if (current == null || !current.Ready)
            {
                session = null;
                return;
            }
        }

        // Only a multiplayer server has someone to send impostors to
        if (
            instance == null
            && config.Enabled
            && Sync.IsServer
            && Sandbox.Engine.Multiplayer.MyMultiplayer.Static != null
        )
            instance = new ImpostorService(config, log);

        instance?.Update();
    }

    public static void Stop()
    {
        instance?.Dispose();
        instance = null;
        session = null;
    }

    private void Update()
    {
        if (++tick % 60 != 0)
        {
            // A zoom gets its finer levels within a few ticks, not at the next full second.
            // Six picks per second at most, however many View messages a client sends.
            foreach (var viewer in viewers.Values)
            {
                if (viewer.Dirty && tick - viewer.LastTick >= 10)
                    TryUpdateViewer(viewer);
            }
            return;
        }

        var now = MySession.Static.ElapsedPlayTime.TotalSeconds;

        registry.BeginScan();
        foreach (var source in sources)
        {
            source.Scan(registry);
            registry.EndScan(source);
        }

        foreach (var entry in registry.Entries)
        {
            // Cluster nodes build every local entry, players on other nodes may need it. Safe
            // zones are always built, the stricter cut-off near them needs their shapes.
            entry.Wanted = BuildAll && !entry.Source.Remote || entry.Kind == ImpostorKind.SafeZone;
            entry.Viewed = false;
        }

        foreach (var steamId in viewers.Keys.ToList())
        {
            if (!MySession.Static.Players.TryGetPlayerBySteamId(steamId, out _))
            {
                viewers.Remove(steamId);
                continue;
            }

            TryUpdateViewer(viewers[steamId]);
        }

        registry.Update(now, config.RebuildSeconds, config.RefreshSeconds);

        if (tick % 3600 == 0)
        {
            foreach (var viewer in viewers.Values)
                LogViewer(viewer);
        }
    }

    // What a player got in the last minute: picks and their time on the game thread, shapes,
    // states, removals and those shown again from the client's cache, and the most impostors
    // that waited for shape credit at once
    private void LogViewer(Viewer viewer)
    {
        if (viewer.Picks == 0)
            return;
        var ms = 1000.0 / Stopwatch.Frequency;
        log.Info(
            $"Impostors of player {viewer.SteamId} in the last minute: {viewer.Picks} picks, "
                + $"{viewer.PickTicks * ms / viewer.Picks:0.00} ms average, {viewer.MaxPickTicks * ms:0.00} ms most, "
                + $"{viewer.Shapes} shapes ({viewer.ShapeBytes / 1024} KB), {viewer.States} states, "
                + $"{viewer.Removed} removed, {viewer.Restored} restored from the cache "
                + $"({viewer.Cache.Count} cached, {viewer.Cache.Bytes / 1024} KB), {viewer.MaxWaiting} waiting at most, "
                + $"{viewer.Sent.Count} shown"
        );
        viewer.Picks = 0;
        viewer.PickTicks = 0;
        viewer.MaxPickTicks = 0;
        viewer.Shapes = 0;
        viewer.ShapeBytes = 0;
        viewer.States = 0;
        viewer.Removed = 0;
        viewer.Restored = 0;
        viewer.MaxWaiting = 0;
    }

    private void TryUpdateViewer(Viewer viewer)
    {
        var start = Stopwatch.GetTimestamp();
        try
        {
            UpdateViewer(viewer);
        }
        catch (Exception e)
        {
            log.Error(e, $"Failed to update the impostors of player {viewer.SteamId}");
        }
        var elapsed = Stopwatch.GetTimestamp() - start;
        viewer.Picks++;
        viewer.PickTicks += elapsed;
        viewer.MaxPickTicks = Math.Max(viewer.MaxPickTicks, elapsed);
    }

    private void OnMessage(ushort channel, byte[] message, ulong sender, bool fromServer)
    {
        if (message.Length < 2 || sender == Sync.MyId)
            return;

        if (message[0] == Protocol.View)
        {
            OnView(new ByteReader(message, 1), message.Length, sender);
            return;
        }

        if (message[0] != Protocol.Hello)
            return;

        if (message[1] != Protocol.Version)
        {
            log.Warning(
                $"Player {sender} has Impostors protocol version {message[1]}, the server has {Protocol.Version}"
            );
            return;
        }

        if (!viewers.ContainsKey(sender))
        {
            viewers[sender] = new Viewer(sender);
            log.Info($"Player {sender} draws impostors");
        }

        writer.Clear();
        writer.Byte(Protocol.Welcome);
        writer.Byte(Protocol.Version);
        writer.Long(instanceId);
        writer.Int(config.MaxQuadsPerFrame);
        Send(sender);
    }

    private void OnView(ByteReader r, int length, ulong sender)
    {
        if (length < 21 || !viewers.TryGetValue(sender, out var viewer))
            return;

        var fov = r.Float();
        var width = r.UShort();
        var height = r.UShort();
        var forward = new Vector3D(r.Float(), r.Float(), r.Float());
        if (!(fov > 0.001f && fov < 3f) || width == 0 || height == 0 || !forward.IsValid())
            return;

        var zoom = Protocol.Zoom(fov, height);
        viewer.Dirty = true;
        if (zoom < Protocol.MinZoom || forward.LengthSquared() < 0.5)
        {
            viewer.Zoom = 1;
            return;
        }

        viewer.Zoom = Math.Min(zoom, MaxZoom);
        viewer.Forward = Vector3D.Normalize(forward);
        // The client reports a turn of an eighth of its field of view, the margin covers that
        // and what a turning view brings in before the next pick
        viewer.HalfAngle =
            Math.Atan(Math.Tan(fov / 2) * Math.Sqrt(1 + (double)width * width / height / height))
            + fov / 4;
    }

    private void UpdateViewer(Viewer viewer)
    {
        viewer.Dirty = false;
        if (!MySession.Static.Players.TryGetPlayerBySteamId(viewer.SteamId, out var player))
            return;

        var eye = Eye(player);
        if (eye == null)
            return;
        viewer.Eye = eye.Value;
        var settings = MySession.Static.Settings;
        var range = config.MaxDistance > 0 ? config.MaxDistance : settings.ViewDistance;
        var half = Math.Max(0, settings.SyncDistance - config.ReplicationMargin);
        var replicated = new BoundingBoxD(viewer.Eye - half, viewer.Eye + half);

        // Saved and edited asteroids replicate farther: MyVoxelReplicable inflates their AABB
        var voxelHalf = Math.Max(
            half,
            settings.ViewDistance / Math.Sqrt(3) - config.ReplicationMargin
        );
        var voxelReplicated = new BoundingBoxD(viewer.Eye - voxelHalf, viewer.Eye + voxelHalf);

        var zones = registry.Entries.Select(ZoneVolume.Of).Where(z => z.HasValue).ToList();

        // Pixels on a default screen, and how many times larger the player's view shows them
        var candidates =
            new List<(ImpostorEntry entry, double pixels, double zoom, double distance)>();
        foreach (var entry in registry.Entries)
        {
            // The client has the real entity, or will have it soon. Another node's entities are
            // never replicated to this node's players.
            if (
                !entry.Source.Remote
                && entry.WorldAabb.Intersects(
                    entry.Kind == ImpostorKind.Voxel ? voxelReplicated : replicated
                )
            )
                continue;

            var radius = entry.WorldAabb.HalfExtents.Length();
            var distance = Vector3D.Distance(viewer.Eye, entry.WorldAabb.Center);
            if (distance - radius > range)
                continue;

            var pixels = radius / Math.Max(distance, 1) * PixelsPerRadian;
            var zoom =
                viewer.Zoom > 1
                && InView(viewer, entry.WorldAabb.Center - viewer.Eye, distance, radius)
                    ? viewer.Zoom
                    : 1;
            if (2 * pixels * zoom < config.MinPixels)
                continue;

            candidates.Add((entry, pixels, zoom, distance));
        }

        // Safe zones are few and show where players are protected, they go first. Zoomed ones
        // are larger, so they come next and get the shape credit first.
        var shown = candidates
            .OrderByDescending(c => c.entry.Kind == ImpostorKind.SafeZone)
            .ThenByDescending(c => c.pixels * c.zoom)
            .Take(config.MaxImpostorsPerPlayer)
            .ToList();
        var shownIds = new HashSet<long>(shown.Select(c => c.entry.Id));

        // The client keeps the shapes of removed impostors in the same cache, in the same order
        var removed = viewer.Sent.Keys.Where(id => !shownIds.Contains(id)).ToList();
        foreach (var id in removed)
        {
            var gone = viewer.Sent[id];
            viewer.Sent.Remove(id);
            viewer.Cache.Add(id, gone.Version, gone.Bytes, gone);
        }
        SendRemove(viewer.SteamId, removed);
        viewer.Removed += removed.Count;
        viewer.Waiting = 0;

        var states = new List<ImpostorState>();
        var shapes = new List<(ImpostorEntry entry, byte[] shape)>();
        // Paid by the time since the last update, which is shorter after a View message
        var elapsed = viewer.LastTick == 0 ? 60 : Math.Min(tick - viewer.LastTick, 60);
        viewer.LastTick = tick;
        viewer.ShapeCredit = Math.Min(
            viewer.ShapeCredit + config.ShapeBytesPerSecond * elapsed / 60,
            config.ShapeBytesPerSecond
        );
        var now = MySession.Static.ElapsedPlayTime.TotalSeconds;
        foreach (var (entry, pixels, zoom, distance) in shown)
        {
            entry.Wanted = true;
            entry.Viewed = true;
            if (entry.Shapes == null)
                continue;

            // Shown again with the shape the client kept, a state brings it back
            if (
                !viewer.Sent.TryGetValue(entry.Id, out var sent)
                && viewer.Cache.Take(entry.Id, entry.Version, out sent)
            )
            {
                viewer.Sent[entry.Id] = sent;
                viewer.Restored++;
                var restored = entry.State(sent.Lod);
                restored.Tint = sent.Tint;
                sent.StateLod = sent.Lod;
                states.Add(restored);
            }

            var picked = PickLod(pixels * zoom, sent?.Picked ?? 0, entry.Kind);
            var index = entry.ShapeIndex(picked);

            // A zoomed level only up to half the client's quad budget, about half of a
            // shape's quads face the camera. The level kept from a zoom stays a while.
            var normal = zoom > 1 ? entry.ShapeIndex(PickLod(pixels, 0, entry.Kind)) : index;
            while (index > normal && QuadCount(entry.Shapes[index]) > config.MaxQuadsPerFrame)
                index--;
            var keep = index > normal;

            // Inside a safe zone: one level lower and tinted with the zone's color, the zone's
            // distorting surface shows less of it than of one outside
            var tint = entry.Kind == ImpostorKind.SafeZone ? default : ZoneTint(zones, entry);
            if (tint.A != 0 && index > 0)
                index--;
            if (sent != null && now < sent.KeepUntil && sent.Version == entry.Version)
                index = Math.Max(index, entry.ShapeIndex(sent.Lod));

            // A new impostor gets a small level first while the player is in debt
            if (sent == null && viewer.ShapeCredit <= 0)
            {
                while (index > 0 && entry.Shapes[index].Length > DebtShapeBytes)
                    index--;
            }

            // The level of the shape at hand, which may be coarser than the wanted one
            var shape = entry.Shapes[index];
            var lod = shape[0];
            var state = entry.State(lod);
            state.Tint = tint;

            if (sent == null || sent.Version != entry.Version || sent.Lod != lod)
            {
                // New impostors don't wait, the rest wait while the player is in debt. A shape
                // larger than a second's worth still goes out, or it would never be sent.
                // The state names the wanted level once, so the client can tell it waits.
                if (viewer.ShapeCredit <= 0 && sent != null)
                {
                    viewer.Waiting++;
                    if (sent.StateLod != lod && sent.Version == entry.Version)
                    {
                        var waiting = entry.State(lod);
                        waiting.Tint = sent.Tint;
                        waiting.Position = sent.Position;
                        waiting.Orientation = sent.Orientation;
                        waiting.Velocity = sent.Velocity;
                        sent.StateLod = lod;
                        states.Add(waiting);
                    }
                    continue;
                }
                viewer.ShapeCredit -= shape.Length;
                viewer.Shapes++;
                viewer.ShapeBytes += shape.Length;

                // The states go first, so the client knows the version a shape belongs to
                states.Add(state);
                shapes.Add((entry, shape));
                viewer.Sent[entry.Id] = sent = new Sent { Bytes = shape.Length };
            }
            else if (!Moved(sent, state, distance) && sent.Tint == tint)
            {
                if (keep)
                    sent.KeepUntil = now + ZoomKeepSeconds;
                continue;
            }
            else
            {
                states.Add(state);
            }

            sent.Version = entry.Version;
            sent.Lod = lod;
            sent.StateLod = lod;
            sent.Picked = picked;
            sent.Tint = tint;
            sent.Position = state.Position;
            sent.Orientation = state.Orientation;
            sent.Velocity = state.Velocity;
            if (keep)
                sent.KeepUntil = now + ZoomKeepSeconds;
        }

        viewer.States += states.Count;
        viewer.MaxWaiting = Math.Max(viewer.MaxWaiting, viewer.Waiting);
        SendStates(viewer.SteamId, states);
        SendShapes(viewer.SteamId, shapes);
    }

    // Where the game replicates entities to the player: its controlled entity, or the spectator
    // camera of a player with creative rights (MyClientState), which can be far from the
    // character. Without a client state yet, the controlled entity.
    private static Vector3D? Eye(MyPlayer player)
    {
        var server = MyMultiplayer.ReplicationLayer as MyReplicationServer;
        var position = server?.GetClientData(new Endpoint(player.Id.SteamId, 0))?.Position;
        if (position.HasValue && position.Value != Vector3D.Zero)
            return position.Value;

        var controlled = player.Controller?.ControlledEntity?.Entity ?? player.Character;
        return controlled?.GetTopMostParent().PositionComp.WorldAABB.Center;
    }

    // The color of the first enabled safe zone holding the entry's center, none if outside
    private static Color ZoneTint(List<ZoneVolume?> zones, ImpostorEntry entry)
    {
        foreach (var zone in zones)
        {
            if (zone.Value.Contains(entry.WorldAabb.Center))
            {
                var color = zone.Value.Color;
                color.A = 255;
                return color;
            }
        }
        return default;
    }

    // Inside the zoomed view: the angle off its direction, less the impostor's own angular
    // radius, is within the view's half diagonal
    private static bool InView(Viewer viewer, Vector3D toCenter, double distance, double radius)
    {
        if (radius >= distance)
            return true;
        var cos = Vector3D.Dot(toCenter, viewer.Forward) / distance;
        var angle = Math.Acos(Math.Max(-1, Math.Min(1, cos))) - Math.Asin(radius / distance);
        return angle <= viewer.HalfAngle;
    }

    private static int QuadCount(byte[] shape)
    {
        return (shape.Length - Shape.HeaderSize) / Shape.QuadSize;
    }

    // Level of detail for this on-screen radius, with some hysteresis around the current one
    private static byte PickLod(double pixels, byte current, ImpostorKind kind)
    {
        var wanted = 2 * pixels * CellsPerPixel;

        // Grids keep level 16 down to half the size: their coarser levels lose the shape
        // fast, and a zoom the client doesn't report shows them larger than this estimate
        if (kind == ImpostorKind.Grid && wanted < 16)
            wanted = Math.Min(16, wanted * 2);
        var lod = ImpostorEntry.Lods.Last();
        foreach (var l in ImpostorEntry.Lods)
        {
            if (l >= wanted)
            {
                lod = l;
                break;
            }
        }

        if (current != 0 && lod < current && wanted > current * 0.4)
            return current;
        return lod;
    }

    // Worth a state update: moved by more than about a pixel, turned or changed its velocity
    private static bool Moved(Sent sent, ImpostorState state, double distance)
    {
        var tolerance = Math.Max(0.5, distance / PixelsPerRadian);
        var predicted = sent.Position + sent.Velocity;
        return Vector3D.DistanceSquared(sent.Position, state.Position) > tolerance * tolerance
                && Vector3D.DistanceSquared(predicted, state.Position) > tolerance * tolerance
            || Math.Abs(Quaternion.Dot(sent.Orientation, state.Orientation)) < 0.99995f
            || Vector3.DistanceSquared(sent.Velocity, state.Velocity) > 1f;
    }

    private void SendRemove(ulong steamId, List<long> ids)
    {
        const int perMessage = (Protocol.MaxMessageSize - 3) / 8;
        for (var start = 0; start < ids.Count; start += perMessage)
        {
            var count = Math.Min(perMessage, ids.Count - start);
            writer.Clear();
            writer.Byte(Protocol.Remove);
            writer.UShort((ushort)count);
            for (var i = start; i < start + count; i++)
                writer.Long(ids[i]);
            Send(steamId);
        }
    }

    private void SendStates(ulong steamId, List<ImpostorState> states)
    {
        const int perMessage = (Protocol.MaxMessageSize - 3) / ImpostorState.Size;
        for (var start = 0; start < states.Count; start += perMessage)
        {
            var count = Math.Min(perMessage, states.Count - start);
            writer.Clear();
            writer.Byte(Protocol.States);
            writer.UShort((ushort)count);
            for (var i = start; i < start + count; i++)
                states[i].Write(writer);
            Send(steamId);
        }
    }

    // Small shapes share a message up to MaxMessageSize: hundreds of far asteroids at levels 2
    // and 4 take a few dozen bytes each. A larger shape goes alone.
    private void SendShapes(ulong steamId, List<(ImpostorEntry entry, byte[] shape)> shapes)
    {
        var start = 0;
        while (start < shapes.Count)
        {
            writer.Clear();
            writer.Byte(Protocol.ShapeData);
            var countPosition = writer.Length;
            writer.UShort(0);
            var count = 0;
            while (
                start + count < shapes.Count
                && (
                    count == 0
                    || writer.Length + 12 + shapes[start + count].shape.Length
                        <= Protocol.MaxMessageSize
                )
            )
            {
                var (entry, shape) = shapes[start + count];
                writer.Long(entry.Id);
                writer.Int(entry.Version);
                writer.Bytes(shape);
                count++;
            }
            writer.UShortAt(countPosition, (ushort)count);
            Send(steamId);
            start += count;
        }
    }

    private void Send(ulong steamId)
    {
        MyAPIGateway.Multiplayer.SendMessageTo(Protocol.Channel, writer.ToArray(), steamId);
    }
}
