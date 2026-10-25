using System;
using System.Collections.Generic;
using System.Linq;
using Impostors;
using Sandbox.Game.Multiplayer;
using Sandbox.Game.World;
using Sandbox.ModAPI;
using Shared.Config;
using Shared.Logging;
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

    // An impostor inside the replication cube is kept this long at most while the client
    // hasn't reported its real entity ready, seconds
    private const double HoldSeconds = 60;

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

        // Impostors whose real entity draws on the client, from its Ready messages
        public readonly HashSet<long> Ready = new();

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
        public byte Lod;

        // The level picked before a safe zone lowered it, for the hysteresis
        public byte Picked;
        public Color Tint;
        public Vector3D Position;
        public Quaternion Orientation;
        public Vector3 Velocity;

        // A finer level sent for a zoomed view is not replaced by a coarser one before this
        public double KeepUntil;

        // Inside the replication cube since then, waiting for the client's real entity
        public bool Held;
        public double HeldSince;
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
    }

    private void TryUpdateViewer(Viewer viewer)
    {
        try
        {
            UpdateViewer(viewer);
        }
        catch (Exception e)
        {
            log.Error(e, $"Failed to update the impostors of player {viewer.SteamId}");
        }
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

        if (message[0] == Protocol.Ready)
        {
            OnReady(new ByteReader(message, 1), sender);
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
        viewer.HalfAngle = Math.Atan(
            Math.Tan(fov / 2) * Math.Sqrt(1 + (double)width * width / height / height)
        );
    }

    private void OnReady(ByteReader r, ulong sender)
    {
        if (!viewers.TryGetValue(sender, out var viewer))
            return;

        // Only impostors the client still has, a stale id would drop one sent again later
        for (var i = r.UShort(); i > 0; i--)
        {
            var id = r.Long();
            if (viewer.Sent.ContainsKey(id))
                viewer.Ready.Add(id);
        }
        for (var i = r.UShort(); i > 0; i--)
            viewer.Ready.Remove(r.Long());
    }

    private void UpdateViewer(Viewer viewer)
    {
        viewer.Dirty = false;
        if (!MySession.Static.Players.TryGetPlayerBySteamId(viewer.SteamId, out var player))
            return;

        var controlled = player.Controller?.ControlledEntity?.Entity ?? player.Character;
        if (controlled == null)
            return;

        var eye = controlled.GetTopMostParent().PositionComp.WorldAABB.Center;
        var settings = MySession.Static.Settings;
        var range = config.MaxDistance > 0 ? config.MaxDistance : settings.ViewDistance;
        var half = Math.Max(0, settings.SyncDistance - config.ReplicationMargin);
        var replicated = new BoundingBoxD(eye - half, eye + half);

        // Saved and edited asteroids replicate farther: MyVoxelReplicable inflates their AABB
        var voxelHalf = Math.Max(
            half,
            settings.ViewDistance / Math.Sqrt(3) - config.ReplicationMargin
        );
        var voxelReplicated = new BoundingBoxD(eye - voxelHalf, eye + voxelHalf);

        var zones = registry.Entries.Select(ZoneVolume.Of).Where(z => z.HasValue).ToList();

        // Pixels on a default screen, and how many times larger the player's view shows them
        var now = MySession.Static.ElapsedPlayTime.TotalSeconds;
        var candidates =
            new List<(ImpostorEntry entry, double pixels, double zoom, double distance)>();
        var held = new HashSet<long>();
        foreach (var entry in registry.Entries)
        {
            var radius = entry.WorldAabb.HalfExtents.Length();
            var distance = Vector3D.Distance(eye, entry.WorldAabb.Center);

            // The client has the real entity, or will have it soon. Another node's entities are
            // never replicated to this node's players. One the client already has stays until
            // the client says its real entity draws: a grid arriving fast is still streaming.
            if (
                !entry.Source.Remote
                && entry.WorldAabb.Intersects(
                    entry.Kind == ImpostorKind.Voxel ? voxelReplicated : replicated
                )
            )
            {
                if (
                    !viewer.Sent.TryGetValue(entry.Id, out var kept)
                    || viewer.Ready.Contains(entry.Id)
                    || kept.Held && now - kept.HeldSince > HoldSeconds
                )
                    continue;
                if (!kept.Held)
                {
                    kept.Held = true;
                    kept.HeldSince = now;
                }
                held.Add(entry.Id);
                candidates.Add(
                    (entry, radius / Math.Max(distance, 1) * PixelsPerRadian, 1, distance)
                );
                continue;
            }
            if (distance - radius > range)
                continue;

            var pixels = radius / Math.Max(distance, 1) * PixelsPerRadian;
            var zoom =
                viewer.Zoom > 1 && InView(viewer, entry.WorldAabb.Center - eye, distance, radius)
                    ? viewer.Zoom
                    : 1;
            if (2 * pixels * zoom < config.MinPixels)
                continue;

            candidates.Add((entry, pixels, zoom, distance));
        }

        // Safe zones are few and show where players are protected, they go first, then the ones
        // held for an entity streaming in. Zoomed ones are larger, so they come next and get
        // the shape credit first.
        var shown = candidates
            .OrderByDescending(c => c.entry.Kind == ImpostorKind.SafeZone)
            .ThenByDescending(c => held.Contains(c.entry.Id))
            .ThenByDescending(c => c.pixels * c.zoom)
            .Take(config.MaxImpostorsPerPlayer)
            .ToList();
        var shownIds = new HashSet<long>(shown.Select(c => c.entry.Id));

        var removed = viewer.Sent.Keys.Where(id => !shownIds.Contains(id)).ToList();
        foreach (var id in removed)
        {
            viewer.Sent.Remove(id);
            viewer.Ready.Remove(id);
        }
        SendRemove(viewer.SteamId, removed);

        var states = new List<ImpostorState>();
        var shapes = new List<(ImpostorEntry entry, byte[] shape)>();
        // Paid by the time since the last update, which is shorter after a View message
        var elapsed = viewer.LastTick == 0 ? 60 : Math.Min(tick - viewer.LastTick, 60);
        viewer.LastTick = tick;
        viewer.ShapeCredit = Math.Min(
            viewer.ShapeCredit + config.ShapeBytesPerSecond * elapsed / 60,
            config.ShapeBytesPerSecond
        );
        foreach (var (entry, pixels, zoom, distance) in shown)
        {
            entry.Wanted = true;
            entry.Viewed = true;
            viewer.Sent.TryGetValue(entry.Id, out var sent);

            // Held in the replication cube: the shape the client has, its finest from the
            // approach, only moved
            if (held.Contains(entry.Id))
            {
                var heldState = entry.State(sent.Lod);
                heldState.Version = sent.Version;
                heldState.Tint = sent.Tint;
                if (Moved(sent, heldState, distance))
                {
                    states.Add(heldState);
                    sent.Position = heldState.Position;
                    sent.Orientation = heldState.Orientation;
                    sent.Velocity = heldState.Velocity;
                }
                continue;
            }
            if (sent != null)
                sent.Held = false;
            if (entry.Shapes == null)
                continue;
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

            // The level of the shape at hand, which may be coarser than the wanted one
            var shape = entry.Shapes[index];
            var lod = shape[0];
            var state = entry.State(lod);
            state.Tint = tint;

            if (sent == null || sent.Version != entry.Version || sent.Lod != lod)
            {
                // New impostors don't wait, the rest wait while the player is in debt. A shape
                // larger than a second's worth still goes out, or it would never be sent.
                if (viewer.ShapeCredit <= 0 && sent != null)
                    continue;
                viewer.ShapeCredit -= shape.Length;

                // The states go first, so the client knows the version a shape belongs to
                states.Add(state);
                shapes.Add((entry, shape));
                viewer.Sent[entry.Id] = sent = new Sent();
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
            sent.Picked = picked;
            sent.Tint = tint;
            sent.Position = state.Position;
            sent.Orientation = state.Orientation;
            sent.Velocity = state.Velocity;
            if (keep)
                sent.KeepUntil = now + ZoomKeepSeconds;
        }

        SendStates(viewer.SteamId, states);
        SendShapes(viewer.SteamId, shapes);
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
