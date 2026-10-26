using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using Impostors;
using Sandbox;
using Sandbox.Engine.Multiplayer;
using Sandbox.Game.Multiplayer;
using Sandbox.Game.World;
using Sandbox.ModAPI;
using Shared.Config;
using Shared.Logging;
using VRage.FileSystem;
using VRage.Network;
using VRageMath;

namespace Shared.Impostors;

// Runs where the world is simulated (dedicated server or lobby host): picks the impostors
// each player with the Impostors mod should see and sends them over mod networking. With the
// client plugin it also runs in offline games and serves the local player too, straight to
// the plugin's renderer (LocalClient).
public sealed class ImpostorService : IDisposable
{
    private const double PixelsPerRadian = Protocol.PixelsPerRadian;

    // Cells per on-screen pixel of an impostor's diameter, about one cell per 2.5 pixels
    private const double CellsPerPixel = 1 / 2.5;

    // Zoomed views beyond this are treated as this much, finer levels give nothing more
    private const double MaxZoom = 32;

    // A turning zoomed view gets a margin of what it turns in this many seconds
    private const double TurnLeadSeconds = 0.5;

    // A new impostor that comes while the player is out of shape credit gets a level of at most
    // this many bytes first, the finer one follows once the credit is paid off. Turning a zoomed
    // view over an asteroid ring brings dozens of new ones at once, at their finest levels
    // (about 150 KB each) that was minutes of debt, and no other impostor got a finer level
    // in the meantime.
    private const int DebtShapeBytes = 2048;

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
    private bool packWritten;
    private int tick;

    // Game thread time of the registry's updates since the last minute's log line, all of it
    // and the most in one tick, Stopwatch ticks
    private long buildTicks;
    private long maxBuildTicks;

    private sealed class Viewer
    {
        public readonly ulong SteamId;

        // The player of this game, served through LocalClient
        public readonly bool Local;
        public readonly Dictionary<long, Sent> Sent = new();

        // Impostors whose real entity draws on the client, from its Ready messages
        public readonly HashSet<long> Ready = new();

        // The shapes this player's client has, by id, level and version
        public readonly ShapeStore<bool> Store = new();

        // The level masks of the shapes in the client's disk cache by id and version, from its
        // Inventory and every shape sent since; null for a client without the cache
        public Dictionary<(long id, int version), byte> OnDisk;

        // The tick until which the player's impostors wait for an Inventory the Hello announced
        public int InventoryDue;

        // Where the server picked for this player last
        public Vector3D Eye;

        // The player's spectator camera, from its View messages while it may spectate
        public Vector3D? Camera;

        // Counted for the log line once a minute
        public int Picks;
        public long PickTicks;
        public long MaxPickTicks;
        public int Shapes;
        public long ShapeBytes;
        public int States;
        public int Removed;
        public int Restored;
        public int FromDisk;
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
        public int ViewTick;

        // Added to HalfAngle while the view keeps turning: the client sends a View at least
        // every 10 ticks then
        public double TurnMargin;

        // Impostors whose new level waits for shape credit at the last pick
        public int Waiting;

        // A View message arrived, the impostors are picked again on the next tick
        public bool Dirty;

        public Viewer(ulong steamId, bool local)
        {
            SteamId = steamId;
            Local = local;
        }
    }

    private sealed class Sent
    {
        public int Version;

        // The level of the shape the client draws, and the level of the last state, which is a
        // finer one while its shape waits for shape credit
        public byte Lod;
        public byte StateLod;

        // The level picked before a safe zone lowered it, for the hysteresis
        public byte Picked;
        public Color Tint;
        public Vector3D Position;
        public Quaternion Orientation;
        public Vector3 Velocity;

        // Inside the replication cube since then, waiting for the client's real entity
        public bool Held;
        public double HeldSince;
    }

    // Extra sources registered by the plugin, for example the cluster bridge
    public static readonly List<Func<ImpostorSource>> ExtraSources = new();

    // Builds every entry, not only the ones this server's players see
    public static bool BuildAll;

    // Where built shapes are kept for later; null for the disk cache in the user data folder.
    // Cluster nodes set it to the World Authority's cache.
    public static IShapeCache Cache;

    // The client plugin's renderer in this process, which gets the local player's messages
    // without mod networking. Null on a dedicated server.
    public static Action<byte[]> LocalClient;

    public static ImpostorService Instance => instance;

    public IEnumerable<ImpostorEntry> Entries => registry.Entries;

    private ImpostorService(IPluginConfig config, IPluginLogger log)
    {
        this.config = config;
        this.log = log;
        var cache = Cache ?? OpenDiskCache(config, log);
        var asteroids =
            config.Asteroids && config.AsteroidPack
                ? AsteroidPackCache.Open(config.AsteroidPackMod, log)
                : null;
        if (asteroids != null)
            cache = new AsteroidPackCache(asteroids, cache);
        registry = new ImpostorRegistry(log, cache);
        if (config.WriteAsteroidPack)
        {
            registry.MaxBuilds = Math.Max(2, Environment.ProcessorCount / 2);
            log.Info("Building every asteroid for the asteroid pack");
        }
        if (config.Grids)
            sources.Add(new GridSource(config));
        if (config.Asteroids)
            sources.Add(new VoxelSource(config, log));
        if (config.SafeZones)
            sources.Add(new SafeZoneSource());
        foreach (var factory in ExtraSources)
            sources.Add(factory());

        MyAPIGateway.Multiplayer.RegisterSecureMessageHandler(Protocol.Channel, OnMessage);
        log.Info($"Impostors service started, instance {instanceId:x16}");
    }

    // The cache folder in this server's or game's user data folder, also holds the WA's index
    public static string CacheFolder =>
        Path.Combine(MyFileSystem.UserDataPath, "Storage", "Impostors", "Cache");

    // The disk cache of this server's or game's user data folder, null when disabled
    public static DiskShapeCache OpenDiskCache(IPluginConfig config, IPluginLogger log)
    {
        MoveOldCacheFolder(log);
        if (config.CacheMegabytes <= 0)
            return null;
        try
        {
            var cache = DiskShapeCache.Get(
                CacheFolder,
                config.CacheMegabytes * 1024L * 1024,
                config.CacheDays,
                log
            );
            log.Info(
                $"Impostor cache: {cache.Count} entries, {cache.Bytes / 1024} KB in {cache.Folder}"
            );
            return cache;
        }
        catch (Exception e)
        {
            log.Error(e, "Impostor cache not available");
            return null;
        }
    }

    // Versions before SE-700 kept the cache in <user data>/ImpostorsCache: moved once, or
    // left with a hint when the new folder already exists or the move fails
    private static void MoveOldCacheFolder(IPluginLogger log)
    {
        var oldFolder = Path.Combine(MyFileSystem.UserDataPath, "ImpostorsCache");
        if (!Directory.Exists(oldFolder))
            return;
        var folder = CacheFolder;
        if (Directory.Exists(folder))
        {
            log.Warning(
                $"Impostor cache: {oldFolder} is no longer used, delete it to free the space"
            );
            return;
        }
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(folder));
            Directory.Move(oldFolder, folder);
            log.Info($"Impostor cache: moved {oldFolder} to {folder}");
        }
        catch (Exception e)
        {
            log.Warning(
                e,
                $"Impostor cache: could not move {oldFolder} to {folder}, delete it to free the space"
            );
        }
    }

    public void Dispose()
    {
        // The game allows changing message handlers on its update thread only. Plugin.Dispose
        // runs on the unload thread at shutdown, when the game loop is gone: OnMessage ignores
        // a stopped service, so the handler left registered there does nothing.
        if (Thread.CurrentThread == MySandboxGame.Static?.UpdateThread)
            MyAPIGateway.Multiplayer?.UnregisterSecureMessageHandler(Protocol.Channel, OnMessage);
        foreach (var source in sources)
            (source as IDisposable)?.Dispose();
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

        // A multiplayer server has players to send impostors to, the client plugin has the
        // local player in an offline game too
        if (
            instance == null
            && config.Enabled
            && Sync.IsServer
            && (Sandbox.Engine.Multiplayer.MyMultiplayer.Static != null || LocalClient != null)
        )
            instance = new ImpostorService(config, log);

        instance?.Update();
    }

    // A message of the client plugin's renderer for the service in this process
    public static void FromLocalClient(byte[] message)
    {
        instance?.Receive(message, Sync.MyId, true);
    }

    public static void Stop()
    {
        var stopped = instance;
        instance = null;
        session = null;
        stopped?.Dispose();
    }

    private void Update()
    {
        var now = MySession.Static.ElapsedPlayTime.TotalSeconds;
        if (++tick % 60 != 0)
        {
            // A zoom gets its finer levels within a few ticks, not at the next full second.
            // Six picks per second at most, however many View messages a client sends.
            var picked = false;
            foreach (var viewer in viewers.Values)
            {
                if (viewer.Dirty && tick - viewer.LastTick >= 10)
                {
                    TryUpdateViewer(viewer);
                    picked = true;
                }
            }

            // What a zoomed view wants now is built now, not after the next full second
            UpdateRegistry(now, picked);
            return;
        }

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
            entry.Wanted =
                BuildAll && !entry.Source.Remote
                || entry.Kind == ImpostorKind.SafeZone
                || WritingPack && entry.Kind == ImpostorKind.Voxel;
            entry.Viewed = false;
        }

        foreach (var steamId in viewers.Keys.ToList())
        {
            if (
                !viewers[steamId].Local
                && !MySession.Static.Players.TryGetPlayerBySteamId(steamId, out _)
            )
            {
                viewers.Remove(steamId);
                continue;
            }

            TryUpdateViewer(viewers[steamId]);
        }

        UpdateRegistry(now, true);

        if (WritingPack)
            TryWritePack();

        if (tick % 3600 == 0)
        {
            foreach (var viewer in viewers.Values)
                LogViewer(viewer);

            var (built, cached, packed, unchanged, reusedCpuMs) = registry.TakeCounts();
            var ms = 1000.0 / Stopwatch.Frequency;
            if (built + cached + packed + unchanged > 0)
                log.Info(
                    $"Impostor shapes in the last minute: {built} built, {cached} from the cache, {packed} from the asteroid pack, {unchanged} unchanged; "
                        + $"the reused ones took {reusedCpuMs:0} ms CPU; "
                        + $"starting the builds took {buildTicks * ms:0.0} ms on the game thread, {maxBuildTicks * ms:0.00} ms the most in a tick"
                );
            buildTicks = 0;
            maxBuildTicks = 0;
        }
    }

    // Every tick: applies the finished builds and starts new ones as slots free, after the
    // picks plans which ones; timed for the minute's log line
    private void UpdateRegistry(double now, bool plan)
    {
        var start = Stopwatch.GetTimestamp();
        if (plan)
            registry.Plan(now, config.RebuildSeconds, config.RefreshSeconds);
        else
            registry.Pump(now);
        var elapsed = Stopwatch.GetTimestamp() - start;
        buildTicks += elapsed;
        maxBuildTicks = Math.Max(maxBuildTicks, elapsed);
    }

    private bool WritingPack => config.WriteAsteroidPack && !packWritten;

    // Once every asteroid has its shapes: writes them into the asteroid pack mod folder,
    // Storage/Impostors/AsteroidPack/<world> in the user data folder, next to the cache
    private void TryWritePack()
    {
        var asteroids = registry
            .Entries.Where(e => e.Kind == ImpostorKind.Voxel && !e.Source.Remote)
            .ToList();
        var done = asteroids.Count(e => e.Shapes != null && e.Input != 0 && !e.Building);
        if (tick % 600 == 0)
            log.Info($"Asteroid pack: {done} of {asteroids.Count} asteroids built");
        if (done < asteroids.Count)
            return;

        packWritten = true;
        var world = MySession.Static.Name;
        var name = string.Concat(
            world.Select(c => Path.GetInvalidFileNameChars().Contains(c) ? '_' : c)
        );
        try
        {
            var folder = Path.Combine(
                MyFileSystem.UserDataPath,
                "Storage",
                "Impostors",
                "AsteroidPack",
                name
            );
            Directory.CreateDirectory(folder);
            var path = Path.Combine(folder, AsteroidPack.FileName);
            var bytes = AsteroidPackCache.Write(
                path,
                world,
                asteroids.Select(e => (e.Id, e.Input, e.Shapes)),
                (byte)config.AsteroidPackMaxLevel
            );
            log.Info(
                $"Asteroid pack written: {asteroids.Count} asteroids, {bytes / 1024} KB, levels up to {config.AsteroidPackMaxLevel}, in {folder}"
            );
        }
        catch (Exception e)
        {
            log.Error(e, "Failed to write the asteroid pack");
        }
        registry.MaxBuilds = ImpostorRegistry.DefaultMaxBuilds;
    }

    // What a player got in the last minute: picks and their time on the game thread, shapes,
    // states, removals, the levels switched to with a state only as the client had their
    // shapes, and the most impostors that waited for shape credit at once
    private void LogViewer(Viewer viewer)
    {
        if (viewer.Picks == 0)
            return;
        var ms = 1000.0 / Stopwatch.Frequency;
        log.Info(
            $"Impostors of player {viewer.SteamId} in the last minute: {viewer.Picks} picks, "
                + $"{viewer.PickTicks * ms / viewer.Picks:0.00} ms average, {viewer.MaxPickTicks * ms:0.00} ms most, "
                + $"{viewer.Shapes} shapes ({viewer.ShapeBytes / 1024} KB), {viewer.States} states, "
                + $"{viewer.Removed} removed, {viewer.Restored} from the client's store, {viewer.FromDisk} from its disk cache "
                + $"({viewer.Store.Count} stored, {viewer.Store.Bytes / 1024} KB), {viewer.MaxWaiting} waiting at most, "
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
        viewer.FromDisk = 0;
        viewer.MaxWaiting = 0;
    }

    private void TryUpdateViewer(Viewer viewer)
    {
        // Shapes sent before the Inventory would be sent for nothing
        if (viewer.InventoryDue > tick)
            return;

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
        if (sender != Sync.MyId && this == instance)
            Receive(message, sender, false);
    }

    private void Receive(byte[] message, ulong sender, bool local)
    {
        if (message.Length < 2)
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

        if (message[0] == Protocol.Inventory)
        {
            OnInventory(new ByteReader(message, 1), message.Length, sender);
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

        // A client without a Welcome yet has nothing of what we may have sent before, like after
        // rejoining before the player was gone here
        var fresh = message.Length > 2 && message[2] == 1;
        if (!viewers.ContainsKey(sender) || fresh)
        {
            if (!viewers.ContainsKey(sender))
                log.Info(
                    local ? "The local player draws impostors" : $"Player {sender} draws impostors"
                );
            viewers[sender] = new Viewer(sender, local);
            if (message.Length > 3 && message[3] == 1)
                viewers[sender].InventoryDue = tick + Protocol.InventoryWait * 60;
        }

        writer.Clear();
        writer.Byte(Protocol.Welcome);
        writer.Byte(Protocol.Version);
        writer.Long(instanceId);
        writer.Int(config.MaxQuadsPerFrame);
        Send(sender);
    }

    // Bounds what a client can make us keep, far more than a 64 MB cache holds
    private const int MaxOnDisk = 1 << 18;

    // The client's disk cache, in one or more messages after each Welcome of a new instance
    private void OnInventory(ByteReader r, int length, ulong sender)
    {
        if (length < 4 || !viewers.TryGetValue(sender, out var viewer) || viewer.Local)
            return;
        var more = r.Byte() == 1;
        var count = r.UShort();
        if (length < 4 + count * 13)
            return;
        if (!more)
            viewer.InventoryDue = 0;
        viewer.OnDisk ??= new Dictionary<(long, int), byte>();
        for (var i = 0; i < count && viewer.OnDisk.Count < MaxOnDisk; i++)
        {
            var key = (r.Long(), r.Int());
            var mask = r.Byte();
            viewer.OnDisk.TryGetValue(key, out var known);
            viewer.OnDisk[key] = (byte)(known | mask);
        }
        if (!more)
            log.Info(
                $"Player {sender} has {viewer.OnDisk.Count} impostor versions in its disk cache"
            );
    }

    private void OnView(ByteReader r, int length, ulong sender)
    {
        if (length < 46 || !viewers.TryGetValue(sender, out var viewer))
            return;

        var fov = r.Float();
        var width = r.UShort();
        var height = r.UShort();
        var forward = new Vector3D(r.Float(), r.Float(), r.Float());
        var spectator = r.Byte() == 1;
        var camera = new Vector3D(r.Double(), r.Double(), r.Double());
        if (!(fov > 0.001f && fov < 3f) || width == 0 || height == 0 || !forward.IsValid())
            return;

        // Only from players the game lets spectate, any other could look around the world
        viewer.Camera =
            spectator
            && camera.IsValid()
            && (
                MySession.Static.Settings.EnableSpectator
                || MySession.Static.HasPlayerSpectatorRights(sender)
            )
                ? camera
                : null;

        var zoom = Protocol.Zoom(fov, height);
        viewer.Dirty = true;
        if (zoom < Protocol.MinZoom || forward.LengthSquared() < 0.5)
        {
            viewer.Zoom = 1;
            return;
        }

        // How fast the view turns, from the last View message: a fast pan brings things in
        // before the next View and pick could
        forward = Vector3D.Normalize(forward);
        var turn = 0.0;
        if (viewer.Zoom > 1 && tick > viewer.ViewTick)
        {
            var angle = Math.Acos(Math.Max(-1, Math.Min(1, Vector3D.Dot(forward, viewer.Forward))));
            turn = angle * 60 / (tick - viewer.ViewTick);
        }
        viewer.ViewTick = tick;
        viewer.Zoom = Math.Min(zoom, MaxZoom);
        viewer.Forward = forward;

        // The client reports a turn of an eighth of its field of view, the margin covers that
        // and what a turning view brings in within TurnLeadSeconds, a field of view at most
        viewer.HalfAngle =
            Math.Atan(Math.Tan(fov / 2) * Math.Sqrt(1 + (double)width * width / height / height))
            + fov / 4;
        viewer.TurnMargin = Math.Min(turn * TurnLeadSeconds, fov);
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
        Vector3D? center;
        double drawn = 0;
        if (viewer.Local)
        {
            // This game draws every entity within its camera's far plane: the view distance
            // offline, the sync distance on a lobby host (MySector.LoadData)
            var camera = MySector.MainCamera;
            if (camera == null)
                return;
            center = viewer.Eye = camera.Position;
            drawn = camera.FarPlaneDistance;
        }
        else
        {
            if (!MySession.Static.Players.TryGetPlayerBySteamId(viewer.SteamId, out var player))
                return;

            // The game replicates around this point, the impostors are sized for the camera
            center = ReplicationCenter(player);
            if (center == null)
                return;
            viewer.Eye = viewer.Camera ?? center.Value;
        }

        var settings = MySession.Static.Settings;
        var range = config.MaxDistance > 0 ? config.MaxDistance : settings.ViewDistance;
        var half = Math.Max(0, settings.SyncDistance - config.ReplicationMargin);
        var replicated = new BoundingBoxD(center.Value - half, center.Value + half);

        // Saved and edited asteroids replicate farther: MyVoxelReplicable inflates their AABB
        var voxelHalf = Math.Max(
            half,
            settings.ViewDistance / Math.Sqrt(3) - config.ReplicationMargin
        );
        var voxelReplicated = new BoundingBoxD(center.Value - voxelHalf, center.Value + voxelHalf);

        var zones = registry.Entries.Select(ZoneVolume.Of).Where(z => z.HasValue).ToList();

        // Pixels on a default screen, and how many times larger the player's view shows them
        var now = MySession.Static.ElapsedPlayTime.TotalSeconds;
        var candidates =
            new List<(ImpostorEntry entry, double pixels, double zoom, double distance)>();
        var held = new HashSet<long>();
        foreach (var entry in registry.Entries)
        {
            var radius = entry.WorldAabb.HalfExtents.Length();
            var distance = Vector3D.Distance(viewer.Eye, entry.WorldAabb.Center);

            // The client has the real entity, or will have it soon. Another node's entities are
            // never replicated to this node's players. One the client already has stays until
            // the client says its real entity draws: a grid arriving fast is still streaming.
            // The local player has every entity, the game draws the ones within the far plane.
            if (viewer.Local)
            {
                var aabb = entry.WorldAabb;
                if (
                    Vector3D.DistanceSquared(
                        Vector3D.Clamp(viewer.Eye, aabb.Min, aabb.Max),
                        viewer.Eye
                    )
                    <= drawn * drawn
                )
                    continue;
            }
            else if (
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
                viewer.Zoom > 1
                && InView(viewer, entry.WorldAabb.Center - viewer.Eye, distance, radius)
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
        viewer.Removed += removed.Count;
        viewer.Waiting = 0;

        var states = new List<ImpostorState>();
        var shapes = new List<(ImpostorEntry entry, byte[] shape, bool cached)>();
        // Paid by the time since the last update, which is shorter after a View message
        var elapsed = viewer.LastTick == 0 ? 60 : Math.Min(tick - viewer.LastTick, 60);
        viewer.LastTick = tick;
        viewer.ShapeCredit = Math.Min(
            viewer.ShapeCredit + config.ShapeBytesPerSecond * elapsed / 60,
            config.ShapeBytesPerSecond
        );

        // The local player's shapes don't go over the network, they never wait
        if (viewer.Local)
            viewer.ShapeCredit = int.MaxValue;
        var fit = FitLevels(viewer, shown, zones);
        for (var i = 0; i < shown.Count; i++)
        {
            var (entry, _, _, distance) = shown[i];
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

            var (index, picked, tint) = fit[i];
            // A new impostor gets a level the client has, or a small one while the player is
            // in debt
            if (sent == null && viewer.ShapeCredit <= 0 && !Has(viewer, entry, index))
            {
                var small = index;
                while (small > 0 && entry.Shapes[small].Length > DebtShapeBytes)
                    small--;
                var stored = index;
                while (stored > small && !Has(viewer, entry, stored))
                    stored--;
                index = stored;
            }

            // The level of the shape at hand, which may be coarser than the wanted one
            var shape = entry.Shapes[index];
            var lod = shape[0];
            var state = entry.State(lod);
            state.Tint = tint;

            if (sent == null || sent.Version != entry.Version || sent.Lod != lod)
            {
                if (Stored(viewer, entry, index))
                {
                    // The client has this shape already, the state switches to it
                    viewer.Restored++;
                }
                else if (OnDisk(viewer, entry, index))
                {
                    // The client loads it from its disk cache, a few bytes and no credit
                    viewer.FromDisk++;
                    shapes.Add((entry, shape, true));
                }
                else if (viewer.ShapeCredit <= 0 && sent != null)
                {
                    // New impostors don't wait, the rest wait while the player is in debt. A
                    // shape larger than a second's worth still goes out, or it would never be
                    // sent. The state names the wanted level once, so the client can tell it
                    // waits; it keeps drawing the shape it has.
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
                else
                {
                    viewer.ShapeCredit -= shape.Length;
                    viewer.Shapes++;
                    viewer.ShapeBytes += shape.Length;
                    shapes.Add((entry, shape, false));
                }

                // The states go first, so the client knows the version a shape belongs to
                states.Add(state);
                viewer.Sent[entry.Id] = sent = new Sent();
            }
            else if (!Moved(sent, state, distance) && sent.Tint == tint)
            {
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
        }

        // After the lookups above and in the order of the ShapeData messages, as the client
        // adds them
        foreach (var (entry, shape, cached) in shapes)
        {
            viewer.Store.Add(entry.Id, shape[0], entry.Version, shape.Length, true);

            // The client caches every shape it gets once it sent its inventory
            if (!cached && viewer.OnDisk != null && viewer.OnDisk.Count < MaxOnDisk)
            {
                var key = (entry.Id, entry.Version);
                viewer.OnDisk.TryGetValue(key, out var mask);
                viewer.OnDisk[key] = (byte)(mask | Protocol.LodBit(shape[0]));
            }
        }

        viewer.States += states.Count;
        viewer.MaxWaiting = Math.Max(viewer.MaxWaiting, viewer.Waiting);
        SendStates(viewer.SteamId, states);
        SendShapes(viewer.SteamId, shapes);
    }

    // The client has this level of the entry's current version
    private static bool Stored(Viewer viewer, ImpostorEntry entry, int index)
    {
        return viewer.Store.TryGet(entry.Id, entry.Shapes[index][0], entry.Version, out _);
    }

    // The client's disk cache has this level of the entry's current version
    private static bool OnDisk(Viewer viewer, ImpostorEntry entry, int index)
    {
        return viewer.OnDisk != null
            && viewer.OnDisk.TryGetValue((entry.Id, entry.Version), out var mask)
            && (mask & Protocol.LodBit(entry.Shapes[index][0])) != 0;
    }

    // In the client's store or in its disk cache
    private static bool Has(Viewer viewer, ImpostorEntry entry, int index)
    {
        return Stored(viewer, entry, index) || OnDisk(viewer, entry, index);
    }

    // Where the game replicates entities to the player: its controlled entity, or the spectator
    // camera of a player with creative rights (MyClientState), which can be far from the
    // character. Without a client state yet, the controlled entity.
    private static Vector3D? ReplicationCenter(MyPlayer player)
    {
        var server = MyMultiplayer.ReplicationLayer as MyReplicationServer;
        var position = server?.GetClientData(new Endpoint(player.Id.SteamId, 0))?.Position;
        if (position.HasValue && position.Value != Vector3D.Zero)
            return position.Value;

        var controlled = player.Controller?.ControlledEntity?.Entity ?? player.Character;
        return controlled?.GetTopMostParent().PositionComp.WorldAABB.Center;
    }

    // The level of each shown impostor. Their quads together stay within the client's
    // MaxQuadsPerFrame, about half of a shape's quads face the camera: when the wanted levels
    // are over it, all of them are picked for a smaller screen, so the near ones lose detail
    // instead of the client dropping the far ones. Over an asteroid ring a few near asteroids
    // at level 32 would take the whole budget.
    private List<(int index, byte picked, Color tint)> FitLevels(
        Viewer viewer,
        List<(ImpostorEntry entry, double pixels, double zoom, double distance)> shown,
        List<ZoneVolume?> zones
    )
    {
        var tints = shown
            .Select(c => c.entry.Kind == ImpostorKind.SafeZone ? default : ZoneTint(zones, c.entry))
            .ToList();
        var fit = Levels(viewer, shown, tints, 1, out var quads);
        if (quads <= config.MaxQuadsPerFrame)
            return fit;

        double low = 0,
            high = 1;
        var best = Levels(viewer, shown, tints, 0, out _);
        for (var step = 0; step < 12; step++)
        {
            var scale = (low + high) / 2;
            var levels = Levels(viewer, shown, tints, scale, out quads);
            if (quads <= config.MaxQuadsPerFrame)
            {
                low = scale;
                best = levels;
            }
            else
            {
                high = scale;
            }
        }
        return best;
    }

    // The levels for a screen this many times the size, without the hysteresis when it is
    // under 1. Quads: the sum of half of each shape's quads, of the
    // ones in the zoomed view while the player zooms.
    private List<(int index, byte picked, Color tint)> Levels(
        Viewer viewer,
        List<(ImpostorEntry entry, double pixels, double zoom, double distance)> shown,
        List<Color> tints,
        double scale,
        out int quads
    )
    {
        var result = new List<(int, byte, Color)>(shown.Count);
        quads = 0;
        for (var i = 0; i < shown.Count; i++)
        {
            var (entry, pixels, zoom, _) = shown[i];
            if (entry.Shapes == null)
            {
                result.Add((0, 0, default));
                continue;
            }

            viewer.Sent.TryGetValue(entry.Id, out var sent);
            var full = scale >= 1;
            var picked = PickLod(
                pixels * zoom * scale,
                full ? sent?.Picked ?? 0 : (byte)0,
                entry.Kind
            );
            var index = entry.ShapeIndex(picked);

            // A zoomed level only up to half the client's quad budget, about half of a
            // shape's quads face the camera. After the zoom the client still has it in its
            // shape store, so zooming in again costs a state only.
            var normal =
                zoom > 1 ? entry.ShapeIndex(PickLod(pixels * scale, 0, entry.Kind)) : index;
            while (index > normal && QuadCount(entry.Shapes[index]) > config.MaxQuadsPerFrame)
                index--;

            // Inside a safe zone: one level lower and tinted with the zone's color, the zone's
            // distorting surface shows less of it than of one outside
            var tint = tints[i];
            if (tint.A != 0 && index > 0)
                index--;

            // A zoomed client draws only what is in its view. Unzoomed the server doesn't know
            // the direction, so all of them count.
            if (viewer.Zoom <= 1 || zoom > 1)
                quads += QuadCount(entry.Shapes[index]) / 2;
            result.Add((index, picked, tint));
        }
        return result;
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
    private bool InView(Viewer viewer, Vector3D toCenter, double distance, double radius)
    {
        if (radius >= distance)
            return true;
        var cos = Vector3D.Dot(toCenter, viewer.Forward) / distance;
        var angle = Math.Acos(Math.Max(-1, Math.Min(1, cos))) - Math.Asin(radius / distance);
        return angle <= viewer.HalfAngle + (tick - viewer.ViewTick <= 30 ? viewer.TurnMargin : 0);
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
    private void SendShapes(
        ulong steamId,
        List<(ImpostorEntry entry, byte[] shape, bool cached)> shapes
    )
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
                    || writer.Length + 12 + Encoded(shapes[start + count])
                        <= Protocol.MaxMessageSize
                )
            )
            {
                var (entry, shape, cached) = shapes[start + count];
                writer.Long(entry.Id);
                writer.Int(entry.Version);
                if (cached)
                {
                    writer.Byte(Protocol.FromCache);
                    writer.Byte(shape[0]);
                }
                else
                {
                    writer.Bytes(shape);
                }
                count++;
            }
            writer.UShortAt(countPosition, (ushort)count);
            Send(steamId);
            start += count;
        }
    }

    private static int Encoded((ImpostorEntry entry, byte[] shape, bool cached) item) =>
        item.cached ? 2 : item.shape.Length;

    private void Send(ulong steamId)
    {
        if (steamId == Sync.MyId && LocalClient != null)
            LocalClient(writer.ToArray());
        else
            MyAPIGateway.Multiplayer.SendMessageTo(Protocol.Channel, writer.ToArray(), steamId);
    }
}
