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
    // Radius in pixels of one radian on a 1080p screen with a 70 degree vertical field of view
    private const double PixelsPerRadian = 540 / 0.7002075382097097;

    // Cells per on-screen pixel of an impostor's diameter, about one cell per 2.5 pixels
    private const double CellsPerPixel = 1 / 2.5;

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

        // Shape bytes this player may still get, negative after a shape larger than what was
        // left: the next seconds pay that off
        public int ShapeCredit;

        public Viewer(ulong steamId)
        {
            SteamId = steamId;
        }
    }

    private sealed class Sent
    {
        public int Version;
        public byte Lod;
        public Vector3D Position;
        public Quaternion Orientation;
        public Vector3 Velocity;
    }

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
            sources.Add(new VoxelSource());

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
            return;

        var now = MySession.Static.ElapsedPlayTime.TotalSeconds;

        registry.BeginScan();
        foreach (var source in sources)
        {
            source.Scan(registry);
            registry.EndScan(source);
        }

        foreach (var entry in registry.Entries)
            entry.Wanted = false;

        foreach (var steamId in viewers.Keys.ToList())
        {
            if (!MySession.Static.Players.TryGetPlayerBySteamId(steamId, out _))
            {
                viewers.Remove(steamId);
                continue;
            }

            try
            {
                UpdateViewer(viewers[steamId]);
            }
            catch (Exception e)
            {
                log.Error(e, $"Failed to update the impostors of player {steamId}");
            }
        }

        registry.Update(now, config.RebuildSeconds, config.RefreshSeconds);
    }

    private void OnMessage(ushort channel, byte[] message, ulong sender, bool fromServer)
    {
        if (message.Length < 2 || message[0] != Protocol.Hello || sender == Sync.MyId)
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

    private void UpdateViewer(Viewer viewer)
    {
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

        var candidates = new List<(ImpostorEntry entry, double pixels, double distance)>();
        foreach (var entry in registry.Entries)
        {
            // The client has the real entity, or will have it soon
            if (
                entry.WorldAabb.Intersects(
                    entry.Kind == ImpostorKind.Voxel ? voxelReplicated : replicated
                )
            )
                continue;

            var radius = entry.WorldAabb.HalfExtents.Length();
            var distance = Vector3D.Distance(eye, entry.WorldAabb.Center);
            if (distance - radius > range)
                continue;

            var pixels = radius / Math.Max(distance, 1) * PixelsPerRadian;
            if (2 * pixels < config.MinPixels)
                continue;

            candidates.Add((entry, pixels, distance));
        }

        var shown = candidates
            .OrderByDescending(c => c.pixels)
            .Take(config.MaxImpostorsPerPlayer)
            .ToList();
        var shownIds = new HashSet<long>(shown.Select(c => c.entry.Id));

        var removed = viewer.Sent.Keys.Where(id => !shownIds.Contains(id)).ToList();
        foreach (var id in removed)
            viewer.Sent.Remove(id);
        SendRemove(viewer.SteamId, removed);

        var states = new List<ImpostorState>();
        var shapes = new List<(ImpostorEntry entry, byte[] shape)>();
        viewer.ShapeCredit = Math.Min(
            viewer.ShapeCredit + config.ShapeBytesPerSecond,
            config.ShapeBytesPerSecond
        );
        foreach (var (entry, pixels, distance) in shown)
        {
            entry.Wanted = true;
            if (entry.Shapes == null)
                continue;

            viewer.Sent.TryGetValue(entry.Id, out var sent);
            // The level of the shape at hand, which may be coarser than the wanted one
            var shape = entry.Shapes[entry.ShapeIndex(PickLod(pixels, sent?.Lod ?? 0, entry.Kind))];
            var lod = shape[0];
            var state = entry.State(lod);

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
            else if (!Moved(sent, state, distance))
            {
                continue;
            }
            else
            {
                states.Add(state);
            }

            sent.Version = entry.Version;
            sent.Lod = lod;
            sent.Position = state.Position;
            sent.Orientation = state.Orientation;
            sent.Velocity = state.Velocity;
        }

        SendStates(viewer.SteamId, states);
        SendShapes(viewer.SteamId, shapes);
    }

    // Level of detail for this on-screen radius, with some hysteresis around the current one
    private static byte PickLod(double pixels, byte current, ImpostorKind kind)
    {
        var wanted = 2 * pixels * CellsPerPixel;

        // Grids keep level 16 down to half the size: their coarser levels lose the shape
        // fast, and a zoomed camera shows them larger than this estimate
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
