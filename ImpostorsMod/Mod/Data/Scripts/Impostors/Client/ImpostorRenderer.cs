using System;
using System.Collections.Generic;
using Sandbox.Game;
using Sandbox.Game.Entities;
using Sandbox.ModAPI;
using VRage.Game;
using VRage.Game.Components;
using VRage.Library.Utils;
using VRage.Utils;
using VRageMath;
using VRageRender;

namespace Impostors
{
    // Draws the impostors a server running the Impostors plugin sends: grids and asteroids
    // beyond the sync distance, as flat shaded quads. The mod runs on multiplayer clients only.
    //
    // The client plugin compiles this file too, so Pulsar players get impostors on servers
    // whose world doesn't list the mod. That copy stands down when the mod is loaded. In an
    // offline game or on a lobby host the plugin loads the mod even when the world doesn't list
    // it, and the mod's copy draws for the local player, from the plugin's service in the same
    // process through mod messages (ToLocalServerMessage, FromLocalServerMessage). Without the
    // mod there the plugin's copy does, with the service's delegates (ToLocalServer,
    // FromLocalServer).
    [MySessionComponentDescriptor(MyUpdateOrder.BeforeSimulation)]
    public class ImpostorRenderer : MySessionComponentBase
    {
        private const string MarkerFile = "Data/Scripts/Impostors/Client/ImpostorRenderer.cs";

        // Most quads drawn per frame, the server's MaxQuadsPerFrame once it welcomed us
        private int maxQuads = 16000;

        // Fitted to screenshots of real grids and asteroids from 8 directions:
        // sides away from the sun are nearly black in space, and the game's tone mapping makes
        // sunlit sides brighter than plain Lambert shading would. Faces facing the sun go above
        // 1 and saturate.
        private const float Ambient = 0.08f;
        private const float Diffuse = 1.6f;

        // Planets shade the impostors behind them, faked from a sphere per planet: the
        // sunlight fades out while the ray from the impostor to the sun passes from Top down
        // to sea level (AverageRadius), and is gone below. Top is the highest cloud layer of
        // the game's planets, about as far above the hills as they rise above sea level
        // (MyRenderComponentPlanet, the cloud layers aren't whitelisted for mods), plus a
        // quarter more. Without an atmosphere it's the hilltops. Ambient light stays.
        private const float CloudTop = 2f;
        private const float AboveClouds = 1.25f;
        private const int PlanetTicks = 600;
        private readonly List<PlanetShade> planets = new List<PlanetShade>();
        private readonly HashSet<VRage.ModAPI.IMyEntity> planetEntities =
            new HashSet<VRage.ModAPI.IMyEntity>();

        private struct PlanetShade
        {
            public Vector3D Center;
            public double Ground;
            public double Top;
        }

        // Small asteroids: under about 27 pixels in radius on a 1080p screen the real ones look
        // darker than lit impostors, down to 0.82 times as bright under 13 pixels. Fitted to
        // screenshots of real asteroids from 8 directions; near ones already match.
        private const float SmallBrightness = 0.82f;
        private const float SmallFromPixels = 13f;
        private const float SmallToPixels = 27f;

        // Small grids go the other way: under the same sizes the real ones carry more light
        // than their impostors (0.68 to 0.82 of it at level 16, measured against dark
        // backgrounds), the coarser levels under 16 even less
        private const float SmallGridBrightness = 1.35f;

        // Safe zones, fitted to comparison sheets of real ones: nearly clear seen face on, a
        // thin pale rim where the line of sight grazes the surface. The share of light behind
        // the surface it covers face on, the share added at the rim and how fast it falls off,
        // and how much the rim pales towards white.
        private const float ZoneAlpha = 0.02f;
        private const float ZoneRimAlpha = 0.8f;
        private const int ZoneRimPower = 6;
        private const float ZoneRimWhite = 0.4f;

        // An impostor inside a safe zone is seen through the zone's surface: its colors are
        // blended ZoneTint towards the surface's color, the zone's color ZoneTintPale of the
        // way to white. Fitted to the colors of a ship in a zone, real against impostor, in
        // comparison sheets (least squares): the real ship is lighter and keeps its hue,
        // blended towards the zone's own color it came out blue.
        private const float ZoneTint = 0.35f;
        private const float ZoneTintPale = 0.6f;

        // A real entity draws itself this long after it got into the scene with its render
        // objects, ticks: the game fades new grids and safe zones in over up to a second
        // (MaxTransitionInSeconds). Checked every ReadyCheckTicks.
        private const int ReadyTicks = 60;
        private const int ReadyCheckTicks = 10;

        // An asteroid draws nothing until its clipmap's first meshing pass is done, then fades
        // in. The client plugin sends the asteroid's id here when that pass is done; without
        // it the impostor stays this long, ticks.
        public const long VoxelLoadedMessage = 4721300002;
        private const int VoxelReadyTicks = 300;
        private readonly HashSet<long> loadedVoxels = new HashSet<long>();

        // Re-sort a shape's quads when the view direction turned by about 3 degrees
        private const float ResortCosine = 0.9986f;

        // A copy of the game's Square material under a name of our own (Data/
        // TransparentMaterials.sbc), so the client plugin can tell impostor quads from other
        // billboards when a frame has more of them than the game draws
        public const string MaterialName = "ImpostorSquare";
        private static readonly MyStringId Material = MyStringId.GetOrCompute(MaterialName);

        // The client plugin sends an int here every tick through the mod messages: how many
        // quads the next frame has room for after every other billboard of the last one
        public const long BudgetMessage = 4721300001;
        private const int BudgetTicks = 10;
        private int frameBudget;
        private int frameBudgetTick = -BudgetTicks - 1;

        private readonly Dictionary<long, ClientImpostor> impostors =
            new Dictionary<long, ClientImpostor>();
        private readonly List<ClientImpostor> visible = new List<ClientImpostor>();

        // Every shape the server sent, it switches to one of them or shows a removed impostor
        // again with a state only
        private readonly ShapeStore<ClientShape> store = new ShapeStore<ClientShape>();

        // The shapes of earlier sessions on disk, null for the local player; caching what it
        // gets once the Inventory went out
        private ShapeCache cache;
        private bool caching;

        // The world's asteroid pack, announced with the disk cache in the Inventory (see
        // AsteroidPack); null without one. The client plugin turns it off with UsePack, and
        // names the mod with PackMod (0 for any of the world's mods).
        private AsteroidPack pack;
        public static bool UsePack = true;
        public static ulong PackMod;

        // Asteroids of the pack and the disk cache shown before the server sends them, out to
        // the server's asteroid range (the view distance until its Welcome says) and down to
        // its MinPixels; null without a pack or a cache, or with "noearly" in the storage
        private EarlyAsteroids early;
        private double asteroidRange = -1;
        private double minPixels = 3;

        // In the camera's view in the last frame, before the budget (stats)
        private int inView;
        private readonly List<DrawPart> parts = new List<DrawPart>();
        private readonly ByteWriter writer = new ByteWriter(16);
        private readonly List<long> readyIds = new List<long>();
        private readonly List<long> unreadyIds = new List<long>();
        private readonly Vector3[] lefts = new Vector3[6];
        private readonly Vector3[] ups = new Vector3[6];
        private readonly bool[] facing = new bool[6];
        private long serverInstance;

        // The view last reported to the server, while it is zoomed or a spectator camera
        private bool viewZoomed;
        private bool viewReported;
        private bool sentSpectator;
        private Vector3D sentPosition;
        private double sentZoom;
        private Vector3 sentForward;
        private int sentViewTick;
        private int zoomTick;
        private bool active;
        private bool debug;

        // The local player's copy in an offline game or on a lobby host: messages go to and
        // come from the client plugin's service in this process, and every entity is real here
        private bool local;

        // The mod's copy talks to the service through mod messages, as it can't see the plugin.
        // Without the plugin nobody answers its Hello and it draws nothing.
        public const long ToLocalServerMessage = 4721300003;
        public const long FromLocalServerMessage = 4721300004;

        // The plugin's copy calls the service, and draws only when the mod's copy doesn't: the
        // plugin sets ModDrawsLocally while it loads a session with the mod
        public static Action<byte[]> ToLocalServer;
        public static bool ModDrawsLocally;
        private static ImpostorRenderer localInstance;

        // Debug: draws the impostors in place of the real entities, which it hides
        private bool compare;

        // Debug: draws no impostors, only what the game itself has
        private bool realOnly;

        // Debug: draws the impostors in sunlight, as if no planet shaded them; the time
        // spent in Sunlight and its calls since the last stats
        private bool unshaded;
        private long shadeTicks;
        private int shadeCount;

        // Debug: draws the impostors moved by this much, next to the real entities (voxel
        // maps can't be hidden on the client)
        private Vector3D shift;
        private readonly HashSet<long> hidden = new HashSet<long>();
        private int tick;
        private int drawnQuads;
        private int budget;

        // Impostors drawn while their real entity exists but doesn't draw yet, last frame
        private int standIns;

        // Debug: time spent in SendReady since the last stats, and Ready ids sent so far
        private readonly MyGameTimer timer = new MyGameTimer();
        private long readyTicks;
        private int readySent;

        // Debug: the last impostors that turned ready, as id:drawable tick:ready tick:whether
        // the client plugin reported the asteroid's meshes
        private readonly List<string> readyLog = new List<string>();

        // For stats.txt: removals and shapes taken from the store since the start, impostors
        // outside the view and those over the budget in the last frame
        private int removedCount;
        private int restoredCount;

        // Shapes received and their bytes, and the shapes taken from the disk cache
        private int shapeCount;
        private long shapeBytes;
        private int cachedCount;
        private int packedCount;
        private int culled;
        private int overBudget;
        private int coarser;

        // Bytes of the server's messages, and the last tick the view turned or jumped (see
        // NoteMoves)
        private long receivedBytes;
        private int turnTick = -1;
        private int jumpTick = -1;
        private Vector3 lastForward;
        private Vector3 restForward;
        private int restTick;
        private Vector3D lastEye;

        public override void LoadData()
        {
            if (MyAPIGateway.Utilities.IsDedicated)
                return;

            // On a server only the client plugin's service sends impostors, to the local player
            if (MyAPIGateway.Multiplayer.IsServer)
            {
                if (ModContext == null && (ToLocalServer == null || ModDrawsLocally))
                    return;
                local = true;
                if (ModContext != null)
                    MyAPIGateway.Utilities.RegisterMessageHandler(
                        FromLocalServerMessage,
                        OnLocalMessage
                    );
                else
                    localInstance = this;
                MyLog.Default.WriteLineAndConsole(
                    ModContext != null
                        ? "Impostors: the mod draws for the local player"
                        : "Impostors: the client plugin draws for the local player"
                );
            }
            else if (ModContext == null && ModLoaded())
            {
                return;
            }

            if (!local)
            {
                MyAPIGateway.Multiplayer.RegisterSecureMessageHandler(Protocol.Channel, OnMessage);

                // While the game loads, so the Inventory can go out with the first Welcome
                if (ShapeCache.MaxBytes > 0)
                {
                    cache = new ShapeCache(ModContext != null);
                    cache.Load(MyAPIGateway.Session.Name);
                }
            }
            if (UsePack)
                OpenPack();
            MyAPIGateway.Utilities.RegisterMessageHandler(BudgetMessage, OnBudget);
            MyAPIGateway.Utilities.RegisterMessageHandler(VoxelLoadedMessage, OnVoxelLoaded);
            active = true;

            // Tests put this file into the storage (see StorageExists) to get stats.txt every
            // second and to switch between the real entities and their impostors (see ReadMode)
            debug = StorageExists("debug");

            // Tests turn the early asteroids off with "noearly", for a baseline
            if ((cache != null || pack != null) && !(debug && StorageExists("noearly")))
            {
                early = new EarlyAsteroids(impostors, cache, pack);
                ReadRange();
            }
        }

        private void OpenPack()
        {
            var start = timer.ElapsedTicks;
            try
            {
                pack = AsteroidPack.Open(PackMod);
            }
            catch (Exception e)
            {
                MyLog.Default.WriteLineAndConsole(
                    "Impostors: failed to read the asteroid pack: " + e.Message
                );
                return;
            }
            if (pack == null)
                return;
            MyLog.Default.WriteLineAndConsole(
                "Impostors: asteroid pack of mod "
                    + pack.ModName
                    + " for \""
                    + pack.World
                    + "\": "
                    + pack.Count
                    + " asteroids, "
                    + pack.Bytes / 1024
                    + " KB, index read in "
                    + ((timer.ElapsedTicks - start) * 1000 / MyGameTimer.Frequency)
                    + " ms"
            );
        }

        // The asteroid range and MinPixels of the last Welcome in this world, so the next
        // session shows the early asteroids before the Welcome comes: a line per world, by a
        // hash of its name, in RangeFile, in meters and thousandths of a pixel (no decimal
        // separator, whatever the locale)
        private const string RangeFile = "asteroid-range.txt";
        private const int RangeWorlds = 64;

        private string WorldKey
        {
            get { return ShapeCache.Fnv(MyAPIGateway.Session.Name).ToString("x16"); }
        }

        private List<string> RangeLines()
        {
            var lines = new List<string>();
            if (!StorageExists(RangeFile))
                return lines;
            using (var file = ReadStorage(RangeFile))
            {
                string line;
                while ((line = file.ReadLine()) != null)
                    lines.Add(line);
            }
            return lines;
        }

        private void ReadRange()
        {
            try
            {
                foreach (var line in RangeLines())
                {
                    var words = line.Split(' ');
                    if (words.Length == 3 && words[0] == WorldKey)
                    {
                        asteroidRange = long.Parse(words[1]);
                        minPixels = long.Parse(words[2]) / 1000.0;
                    }
                }
            }
            catch (Exception e)
            {
                MyLog.Default.WriteLineAndConsole(
                    "Impostors: failed to read " + RangeFile + ": " + e.Message
                );
            }
        }

        private void WriteRange()
        {
            if (early == null)
                return;
            try
            {
                var key = WorldKey;
                var lines = RangeLines().FindAll(l => !l.StartsWith(key + " "));
                if (lines.Count >= RangeWorlds)
                    lines.RemoveRange(0, lines.Count - RangeWorlds + 1);
                lines.Add(key + " " + (long)asteroidRange + " " + (long)(minPixels * 1000));
                using (var file = WriteStorage(RangeFile))
                {
                    foreach (var line in lines)
                        file.WriteLine(line);
                }
            }
            catch (Exception e)
            {
                MyLog.Default.WriteLineAndConsole(
                    "Impostors: failed to write " + RangeFile + ": " + e.Message
                );
            }
        }

        // The mod's files are in its local storage. Pulsar compiles the client plugin under a
        // random assembly name, so its copy uses the global storage with an "Impostors." prefix.
        private bool StorageExists(string file)
        {
            if (ModContext != null)
                return MyAPIGateway.Utilities.FileExistsInLocalStorage(
                    file,
                    typeof(ImpostorRenderer)
                );
            return MyAPIGateway.Utilities.FileExistsInGlobalStorage("Impostors." + file);
        }

        private System.IO.TextReader ReadStorage(string file)
        {
            if (ModContext != null)
                return MyAPIGateway.Utilities.ReadFileInLocalStorage(
                    file,
                    typeof(ImpostorRenderer)
                );
            return MyAPIGateway.Utilities.ReadFileInGlobalStorage("Impostors." + file);
        }

        private System.IO.TextWriter WriteStorage(string file)
        {
            if (ModContext != null)
                return MyAPIGateway.Utilities.WriteFileInLocalStorage(
                    file,
                    typeof(ImpostorRenderer)
                );
            return MyAPIGateway.Utilities.WriteFileInGlobalStorage("Impostors." + file);
        }

        protected override void UnloadData()
        {
            if (!active)
                return;

            if (local && ModContext != null)
                MyAPIGateway.Utilities.UnregisterMessageHandler(
                    FromLocalServerMessage,
                    OnLocalMessage
                );
            else if (local)
                localInstance = null;
            else
                MyAPIGateway.Multiplayer.UnregisterSecureMessageHandler(
                    Protocol.Channel,
                    OnMessage
                );
            MyAPIGateway.Utilities.UnregisterMessageHandler(BudgetMessage, OnBudget);
            MyAPIGateway.Utilities.UnregisterMessageHandler(VoxelLoadedMessage, OnVoxelLoaded);
            active = false;
            impostors.Clear();
            store.Clear();
            pack = null;
            early = null;
            if (cache != null)
            {
                cache.Save();
                cache.Clear();
                cache = null;
                caching = false;
            }
        }

        private static bool ModLoaded()
        {
            foreach (var mod in MyAPIGateway.Session.Mods)
            {
                if (MyAPIGateway.Utilities.FileExistsInModLocation(MarkerFile, mod))
                    return true;
            }
            return false;
        }

        public override void UpdateBeforeSimulation()
        {
            if (active && (!local || ModContext != null))
                Update();
        }

        // The client plugin updates its local copy itself: the game keeps the session components
        // to update in a set ordered by priority and type name, where the plugin's copy and the
        // mod's are the same, so with the mod loaded the game updates only the mod's copy
        public static void UpdateLocal()
        {
            if (localInstance != null)
                localInstance.Update();
        }

        private void Update()
        {
            if (tick++ % (Protocol.HelloInterval * 60) == 60)
            {
                writer.Clear();
                writer.Byte(Protocol.Hello);
                writer.Byte(Protocol.Version);
                writer.Byte((byte)(serverInstance == 0 ? 1 : 0));
                writer.Byte((byte)(cache != null ? 1 : 0));
                SendToServer();
            }

            if (cache != null)
                cache.Update();

            if (early != null)
                UpdateEarly();

            // A few View messages per second at most
            if (tick % 10 == 0)
                SendView();

            if (tick % ReadyCheckTicks == 5)
            {
                var start = debug ? timer.ElapsedTicks : 0;
                SendReady();
                if (debug)
                    readyTicks += timer.ElapsedTicks - start;
            }

            if (tick % PlanetTicks == 1)
                FindPlanets();

            if (debug && tick % 60 == 0)
            {
                ReadMode();
                WriteStats();
            }
        }

        // The early asteroids out to the server's range, the view distance until it says, and
        // with the room the server's impostors leave in the quad budget, half of their quads
        // facing the camera. Locally not those the game draws itself, within the far plane.
        private void UpdateEarly()
        {
            var camera = MyAPIGateway.Session.Camera;
            if (camera == null)
                return;
            var used = 0;
            if (tick % 10 == 0)
            {
                foreach (var impostor in impostors.Values)
                {
                    if (!impostor.Early && impostor.Shape != null)
                        used += impostor.Shape.Count / 2;
                }
            }
            early.Update(
                camera,
                tick,
                asteroidRange >= 0
                    ? asteroidRange
                    : MyAPIGateway.Session.SessionSettings.ViewDistance,
                minPixels,
                local ? camera.FarPlaneDistance : 0,
                Math.Max(0, maxQuads - used)
            );
        }

        // Tells the server about a zoomed view or a spectator camera (see Protocol.View): when
        // it starts, changes its zoom by 5%, turns by an eighth of its field of view or moves
        // by ViewMoveMeters, and when it ends
        private void SendView()
        {
            var camera = MyAPIGateway.Session.Camera;
            if (camera == null || serverInstance == 0)
                return;

            var fov = camera.FovWithZoom;
            var size = camera.ViewportSize;
            var zoom = Protocol.Zoom(fov, size.Y);
            var forward = (Vector3)camera.WorldMatrix.Forward;
            var zoomed = zoom >= Protocol.MinZoom;
            var spectator = MyAPIGateway.Session.IsCameraUserControlledSpectator;
            var position = camera.Position;
            var reported = zoomed || spectator;
            if (!reported && !viewReported)
                return;
            if (
                reported
                && viewReported
                && zoomed == viewZoomed
                && spectator == sentSpectator
                && (!zoomed || Math.Abs(zoom / sentZoom - 1) < 0.05)
                && Vector3.Dot(forward, sentForward) > Math.Cos(fov / 8)
                && (
                    !spectator
                    || Vector3D.DistanceSquared(position, sentPosition)
                        < Protocol.ViewMoveMeters * Protocol.ViewMoveMeters
                )
            )
                return;

            writer.Clear();
            writer.Byte(Protocol.View);
            writer.Float(fov);
            writer.UShort((ushort)MathHelper.Clamp(size.X, 1, ushort.MaxValue));
            writer.UShort((ushort)MathHelper.Clamp(size.Y, 1, ushort.MaxValue));
            writer.Float(forward.X);
            writer.Float(forward.Y);
            writer.Float(forward.Z);
            writer.Byte((byte)(spectator ? 1 : 0));
            writer.Double(position.X);
            writer.Double(position.Y);
            writer.Double(position.Z);
            SendToServer();
            if (zoomed && !viewZoomed)
                zoomTick = tick;
            viewZoomed = zoomed;
            viewReported = reported;
            sentSpectator = spectator;
            sentPosition = position;
            sentZoom = zoom;
            sentForward = forward;
            sentViewTick = tick;
        }

        private void SendToServer()
        {
            if (local && ModContext != null)
                MyAPIGateway.Utilities.SendModMessage(ToLocalServerMessage, writer.ToArray());
            else if (local)
                ToLocalServer(writer.ToArray());
            else
                MyAPIGateway.Multiplayer.SendMessageToServer(Protocol.Channel, writer.ToArray());
        }

        // A message of the client plugin's service for the local player
        public static void FromLocalServer(byte[] message)
        {
            if (localInstance != null)
                localInstance.OnMessage(Protocol.Channel, message, 0, true);
        }

        // The same for the mod's copy, as a mod message
        private void OnLocalMessage(object message)
        {
            var bytes = message as byte[];
            if (bytes != null)
                OnMessage(Protocol.Channel, bytes, 0, true);
        }

        // Tells the server which impostors' real entities started or stopped drawing. Inside
        // the replication cube it keeps an impostor until then (see Protocol.Ready).
        private void SendReady()
        {
            // The local player's game has every entity, the service never holds one for it
            if (local)
                return;
            readyIds.Clear();
            unreadyIds.Clear();
            foreach (var impostor in impostors.Values)
            {
                var id = impostor.State.Id;
                if (id == 0)
                    continue;
                if (!RealDrawable(id))
                {
                    impostor.DrawableTick = -1;
                    loadedVoxels.Remove(id);
                }
                else if (impostor.DrawableTick < 0)
                    impostor.DrawableTick = tick;
                var wait =
                    impostor.State.Kind != ImpostorKind.Voxel ? ReadyTicks
                    : loadedVoxels.Contains(id) ? ReadyCheckTicks
                    : VoxelReadyTicks;
                var ready = impostor.DrawableTick >= 0 && tick - impostor.DrawableTick >= wait;

                // Early asteroids hide too, but the server doesn't know them
                if (ready != impostor.Ready && debug && ready)
                {
                    if (readyLog.Count == 8)
                        readyLog.RemoveAt(0);
                    readyLog.Add(
                        id
                            + ":"
                            + impostor.DrawableTick
                            + ":"
                            + tick
                            + ":"
                            + (loadedVoxels.Contains(id) ? 1 : 0)
                    );
                }
                impostor.Ready = ready;
                if (impostor.Early || serverInstance == 0 || ready == impostor.Reported)
                    continue;
                impostor.Reported = ready;
                (ready ? readyIds : unreadyIds).Add(id);
            }
            readySent += readyIds.Count;

            // 1000 ids of each kind per message stay well under MaxMessageSize
            const int perMessage = 1000;
            for (
                var start = 0;
                start < Math.Max(readyIds.Count, unreadyIds.Count);
                start += perMessage
            )
            {
                writer.Clear();
                writer.Byte(Protocol.Ready);
                WriteIds(readyIds, start, perMessage);
                WriteIds(unreadyIds, start, perMessage);
                SendToServer();
            }
        }

        private void WriteIds(List<long> ids, int start, int count)
        {
            count = Math.Max(0, Math.Min(count, ids.Count - start));
            writer.UShort((ushort)count);
            for (var i = start; i < start + count; i++)
                writer.Long(ids[i]);
        }

        // The real entity is in the scene and has render objects: a streamed grid once its
        // render cells are built, a safe zone once its model is, an asteroid its clipmap. An
        // entity that only exists may still be loading on the creation thread.
        private static bool RealDrawable(long id)
        {
            var entity = MyAPIGateway.Entities.GetEntityById(id);
            if (entity == null || entity.Closed || !entity.InScene || entity.Render == null)
                return false;
            var ids = entity.Render.RenderObjectIDs;
            return ids != null && ids.Length > 0 && ids[0] != uint.MaxValue;
        }

        // "impostor" in the mode file draws the impostors instead of the real entities,
        // "shift x y z" draws them moved by that vector next to the real ones, "real" draws
        // none of them. " unshaded" after any of them lights the impostors as if no planet
        // shaded them.
        private void ReadMode()
        {
            var mode = "";
            if (StorageExists("mode"))
            {
                using (var file = ReadStorage("mode"))
                    mode = file.ReadToEnd().Trim();
            }
            unshaded = mode.EndsWith(" unshaded");
            if (unshaded)
                mode = mode.Substring(0, mode.Length - " unshaded".Length);
            var words = mode.Split(' ');
            shift = Vector3D.Zero;
            if (words.Length == 4 && words[0] == "shift")
            {
                shift = new Vector3D(
                    double.Parse(words[1]),
                    double.Parse(words[2]),
                    double.Parse(words[3])
                );
                mode = "";
            }
            compare = mode == "impostor";
            realOnly = mode == "real";

            foreach (var id in impostors.Keys)
            {
                var entity = MyAPIGateway.Entities.GetEntityById(id);
                if (entity == null)
                    continue;
                if (compare && entity.Visible)
                {
                    entity.Visible = false;
                    hidden.Add(id);
                }
                else if (!compare && hidden.Remove(id))
                {
                    entity.Visible = true;
                }
            }
        }

        private static string Vec(Vector3 v)
        {
            return v.X.ToString("0.000")
                + ","
                + v.Y.ToString("0.000")
                + ","
                + v.Z.ToString("0.000");
        }

        // The quads a safe zone draws at the current camera
        private string ZoneStats(ClientImpostor impostor)
        {
            var camera = MyAPIGateway.Session.Camera;
            if (impostor.Shape == null || !impostor.Shape.IsZone || camera == null)
                return "";
            return " zonequads=" + ZoneQuads(impostor, camera.Position).Length;
        }

        // Where an impostor is on screen, in pixels, and its radius there
        private string ScreenPosition(ImpostorState state)
        {
            var camera = MyAPIGateway.Session.Camera;
            if (camera == null)
                return "";
            var entity = MyAPIGateway.Entities.GetEntityById(state.Id);
            var center = (entity != null ? entity.WorldAABB.Center : state.Position) + shift;
            var radius = entity != null ? entity.WorldAABB.HalfExtents.Length() : state.Radius;
            var local = Vector3D.TransformNormal(
                center - camera.Position,
                MatrixD.Transpose(camera.WorldMatrix)
            );
            if (local.Z >= 0)
                return " screen=behind";
            var t = Math.Tan(camera.FovWithZoom / 2);
            var size = camera.ViewportSize;
            var x = (local.X / (-local.Z * t * size.X / size.Y) + 1) / 2 * size.X;
            var y = (1 - local.Y / (-local.Z * t)) / 2 * size.Y;
            var pixels = radius / -local.Z / t * size.Y / 2;
            return " screen="
                + x.ToString("0")
                + ","
                + y.ToString("0")
                + " radius="
                + pixels.ToString("0.0")
                + " viewport="
                + size.X.ToString("0")
                + "x"
                + size.Y.ToString("0")
                + " distance="
                + (-local.Z).ToString("0");
        }

        private void WriteStats()
        {
            var camera = MyAPIGateway.Session.Camera;
            var text = new System.Text.StringBuilder();
            text.AppendLine(
                "impostors="
                    + impostors.Count
                    + " inview="
                    + inView
                    + " drawn="
                    + visible.Count
                    + " quads="
                    + drawnQuads
                    + " maxquads="
                    + maxQuads
                    + " budget="
                    + budget
                    + " standins="
                    + standIns
                    + " readysent="
                    + readySent
                    + " readylog="
                    + string.Join(",", readyLog)
                    + " readyus="
                    + (readyTicks * 1000000 / MyGameTimer.Frequency)
                    + " culled="
                    + culled
                    + " overbudget="
                    + overBudget
                    + " coarser="
                    + coarser
                    + " noshape="
                    + NoShape()
                    + " waiting="
                    + Waiting()
                    + " removed="
                    + removedCount
                    + " restored="
                    + restoredCount
                    + " shapes="
                    + shapeCount
                    + " shapekb="
                    + shapeBytes / 1024
                    + " cached="
                    + cachedCount
                    + " packed="
                    + packedCount
                    + " cachekb="
                    + (cache != null ? cache.Bytes / 1024 : 0)
                    + " stored="
                    + store.Count
                    + " storedkb="
                    + store.Bytes / 1024
                    + " sun="
                    + Vec(MyVisualScriptLogicProvider.GetSunDirection())
                    + " planets="
                    + Planets()
                    + " shadeus="
                    + (shadeTicks * 1000000 / MyGameTimer.Frequency)
                    + " shades="
                    + shadeCount
                    + " tick="
                    + tick
                    + " fov="
                    + (camera != null ? MathHelper.ToDegrees(camera.FovWithZoom) : 0).ToString(
                        "0.0"
                    )
                    + " zoomed="
                    + (viewZoomed ? 1 : 0)
                    + " spectator="
                    + (sentSpectator && viewReported ? 1 : 0)
                    + " camera="
                    + (
                        camera != null
                            ? camera.Position.X.ToString("0.0")
                                + ","
                                + camera.Position.Y.ToString("0.0")
                                + ","
                                + camera.Position.Z.ToString("0.0")
                            : ""
                    )
                    + " forward="
                    + (camera != null ? Vec((Vector3)camera.WorldMatrix.Forward) : "")
                    + " viewtick="
                    + sentViewTick
                    + " zoomtick="
                    + zoomTick
                    + " recvkb="
                    + receivedBytes / 1024
                    + " turntick="
                    + turnTick
                    + " jumptick="
                    + jumpTick
                    + " range="
                    + asteroidRange.ToString("0")
                    + (
                        early != null
                            ? " early="
                                + early.Count
                                + " earlyknown="
                                + early.KnownCount
                                + " earlyloads="
                                + early.Loaded
                                + " earlyms="
                                + (early.LoadTicks * 1000 / MyGameTimer.Frequency)
                                + " kept="
                                + early.Kept
                                + " swapped="
                                + early.Swapped
                                + " swaplog="
                                + string.Join(",", early.SwapLog)
                            : " early=off"
                    )
            );
            foreach (var impostor in impostors.Values)
            {
                var state = impostor.State;
                var shape = impostor.Shape;
                text.AppendLine(
                    "id="
                        + state.Id
                        + " kind="
                        + state.Kind
                        + " version="
                        + state.Version
                        + " lod="
                        + state.Lod
                        + " shape="
                        + (
                            shape == null
                                ? "none"
                                : shape.Version + "/" + shape.Lod + "/" + shape.Count
                        )
                        + " shapetick="
                        + impostor.ShapeTick
                        + " real="
                        + (MyAPIGateway.Entities.EntityExists(state.Id) ? 1 : 0)
                        + " ready="
                        + (impostor.Ready ? 1 : 0)
                        + " drawn="
                        + (visible.Contains(impostor) ? 1 : 0)
                        + " early="
                        + (impostor.Early ? 1 : 0)
                        + " firstshape="
                        + impostor.FirstShapeTick
                        + " firstdrawn="
                        + impostor.FirstDrawnTick
                        + " r="
                        + state.Radius.ToString("0.0")
                        + " pos="
                        + state.Position.X.ToString("0.0")
                        + ","
                        + state.Position.Y.ToString("0.0")
                        + ","
                        + state.Position.Z.ToString("0.0")
                        + (
                            state.Tint.A != 0
                                ? " tint=" + state.Tint.PackedValue.ToString("x8")
                                : ""
                        )
                        + " sunlight="
                        + impostor.Sunlight.ToString("0.000")
                        + ZoneStats(impostor)
                        + ScreenPosition(state)
                );
            }
            readyTicks = 0;
            shadeTicks = 0;
            shadeCount = 0;
            using (var file = WriteStorage("stats.txt"))
                file.Write(text.ToString());
        }

        private void OnVoxelLoaded(object message)
        {
            if (message is long)
                loadedVoxels.Add((long)message);
        }

        // Impostors the server sent no shape for yet
        private int NoShape()
        {
            var count = 0;
            foreach (var impostor in impostors.Values)
            {
                if (impostor.Shape == null)
                    count++;
            }
            return count;
        }

        // Impostors whose state names a level the server hasn't sent yet, for want of shape
        // credit
        private int Waiting()
        {
            var count = 0;
            foreach (var impostor in impostors.Values)
            {
                var shape = impostor.Shape;
                if (shape != null && !shape.IsZone && shape.Lod != impostor.State.Lod)
                    count++;
            }
            return count;
        }

        private void OnBudget(object message)
        {
            if (!(message is int))
                return;
            frameBudget = (int)message;
            frameBudgetTick = tick;
        }

        private void OnMessage(ushort channel, byte[] message, ulong sender, bool fromServer)
        {
            if (!fromServer || message.Length == 0)
                return;
            receivedBytes += message.Length;

            try
            {
                Receive(new ByteReader(message, 1), message[0]);
            }
            catch (Exception e)
            {
                MyLog.Default.WriteLineAndConsole(
                    "Impostors: bad message " + message[0] + ": " + e
                );
            }
        }

        private void Receive(ByteReader r, byte type)
        {
            // After a Restart, what the server sent before it got the Hello
            if (serverInstance == 0 && type != Protocol.Welcome)
                return;

            switch (type)
            {
                case Protocol.Welcome:
                {
                    if (r.Byte() != Protocol.Version)
                        return;
                    var instance = r.Long();
                    maxQuads = MathHelper.Clamp(r.Int(), 0, Protocol.BillboardLimit);
                    if (r.Remaining >= 8)
                    {
                        var range = Math.Max(0, r.Float());
                        var pixels = r.Float();
                        if (range != asteroidRange || pixels != minPixels)
                        {
                            asteroidRange = range;
                            minPixels = pixels;
                            WriteRange();
                        }
                    }
                    if (instance != serverInstance)
                    {
                        impostors.Clear();
                        store.Clear();
                        if (early != null)
                            early.Reset();
                        serverInstance = instance;

                        // A new server knows nothing of the view yet
                        viewZoomed = false;
                        viewReported = false;
                        SendInventory();
                    }
                    break;
                }

                case Protocol.States:
                {
                    var count = r.UShort();
                    for (var i = 0; i < count; i++)
                    {
                        var state = ImpostorState.Read(r);
                        var impostor = Get(state.Id);
                        if (early != null)
                            early.Served(state.Id, impostor, state.Version, tick);
                        if (caching && state.Kind == ImpostorKind.Voxel)
                            cache.AddState(state);
                        impostor.State = state;
                        impostor.ReceivedTick = tick;

                        // A level we have: the server sends no shape for it. One we don't
                        // have is on its way or waits for shape credit.
                        var shape = impostor.Shape;
                        ClientShape stored;
                        if (
                            (
                                shape == null
                                || shape.Lod != state.Lod
                                || shape.Version != state.Version
                            ) && store.TryGet(state.Id, state.Lod, state.Version, out stored)
                        )
                        {
                            impostor.Shape = stored;
                            if (impostor.FirstShapeTick < 0)
                                impostor.FirstShapeTick = tick;
                            restoredCount++;
                        }
                    }
                    break;
                }

                case Protocol.Remove:
                {
                    var count = r.UShort();
                    for (var i = 0; i < count; i++)
                    {
                        var id = r.Long();
                        if (early != null)
                            early.Served(id, null, 0, tick);
                        if (impostors.Remove(id))
                            removedCount++;
                    }
                    break;
                }

                case Protocol.ShapeData:
                {
                    var count = r.UShort();
                    for (var i = 0; i < count; i++)
                    {
                        var id = r.Long();
                        var version = r.Int();
                        byte[] data;
                        ClientShape shape;
                        if (r.Peek() == Protocol.FromCache)
                        {
                            r.Byte();
                            var lod = r.Byte();
                            data = cache != null && caching ? cache.Get(id, version, lod) : null;
                            if (data == null && pack != null && caching)
                            {
                                data = pack.Get(id, version, lod);
                                if (data != null)
                                    packedCount++;
                            }
                            if (data == null)
                            {
                                // Not where the server counted on it: start over with it
                                MyLog.Default.WriteLineAndConsole(
                                    "Impostors: shape "
                                        + id
                                        + " level "
                                        + lod
                                        + " is not in the cache"
                                );
                                Restart();
                                return;
                            }
                            shape = ClientShape.Read(new ByteReader(data), version);
                            cachedCount++;
                        }
                        else
                        {
                            var start = r.Position;
                            shape = ClientShape.Read(r, version);
                            data = r.Since(start);
                            shapeCount++;
                            shapeBytes += data.Length;
                            if (caching)
                                cache.Add(id, version, shape.Lod, data);
                        }

                        var impostor = Get(id);
                        impostor.Shape = shape;
                        impostor.ShapeTick = tick;
                        if (impostor.FirstShapeTick < 0)
                            impostor.FirstShapeTick = tick;
                        store.Add(id, shape.Lod, version, data.Length, shape);
                    }

                    if (caching && cache.TooLarge)
                    {
                        MyLog.Default.WriteLineAndConsole(
                            "Impostors: the shape cache grew over "
                                + ShapeCache.SessionMaxBytes / 1048576
                                + " MB this session, pruning it"
                        );
                        Restart();
                    }
                    break;
                }
            }
        }

        // Tells the server what the disk cache and the asteroid pack have, from then on the
        // cache keeps everything the server sends
        private void SendInventory()
        {
            if (cache == null)
                return;
            var versions = cache.Inventory(writer, SendToServer, pack);
            caching = true;
            MyLog.Default.WriteLineAndConsole(
                "Impostors: "
                    + versions
                    + " impostor versions, "
                    + cache.Bytes / 1024
                    + " KB in the shape cache"
                    + (pack != null ? ", " + pack.Count + " asteroids in the pack" : "")
            );
        }

        // The server counts on shapes we don't have: forget its impostors and ask it to start
        // over, the next Welcome sends the inventory of the pruned cache
        private void Restart()
        {
            impostors.Clear();
            store.Clear();
            if (early != null)
                early.Reset();
            serverInstance = 0;
            caching = false;
            if (cache != null)
                cache.Prune();
            writer.Clear();
            writer.Byte(Protocol.Hello);
            writer.Byte(Protocol.Version);
            writer.Byte(1);
            writer.Byte(1);
            SendToServer();
        }

        private ClientImpostor Get(long id)
        {
            ClientImpostor impostor;
            if (!impostors.TryGetValue(id, out impostor))
            {
                impostor = new ClientImpostor();
                impostors[id] = impostor;
            }
            return impostor;
        }

        public override void Draw()
        {
            visible.Clear();
            standIns = 0;
            inView = 0;
            var camera = MyAPIGateway.Session.Camera;
            if (debug && camera != null)
                NoteMoves(camera);
            if (!active || impostors.Count == 0 || realOnly || camera == null)
                return;
            var eye = camera.Position;
            var view = MatrixD.Transpose(camera.WorldMatrix);
            culled = 0;
            overBudget = 0;

            // Nearest first while counting the budget, so the far ones are dropped. Frustum
            // culled: the ones outside the camera's view would take the budget for nothing.
            foreach (var impostor in impostors.Values)
            {
                if (impostor.Shape == null || impostor.State.Id == 0)
                    continue;

                var center = impostor.State.Position + shift;
                var radius = impostor.State.Radius;
                if (!InView(camera, view, center, radius))
                {
                    culled++;
                    continue;
                }

                // Never with the camera inside its bounding sphere
                if (Vector3D.DistanceSquared(eye, center) <= (double)radius * radius)
                    continue;

                // The real entity draws itself. Until then, while a grid or safe zone is still
                // streaming in, the impostor stands in for it. Locally every entity is real, the
                // service sends only those the game doesn't draw.
                if (!local && !compare && shift == Vector3D.Zero)
                {
                    if (impostor.Ready)
                        continue;
                    if (MyAPIGateway.Entities.EntityExists(impostor.State.Id))
                        standIns++;
                }

                visible.Add(impostor);
            }

            inView = visible.Count;
            visible.Sort(
                (a, b) =>
                    Vector3D
                        .DistanceSquared(eye, a.State.Position)
                        .CompareTo(Vector3D.DistanceSquared(eye, b.State.Position))
            );

            // The client plugin's budget while it keeps sending it, the server's otherwise
            budget = maxQuads;
            if (tick - frameBudgetTick <= BudgetTicks)
                budget = MathHelper.Clamp(frameBudget, 0, maxQuads);

            // One over the budget is drawn at a coarser level we have, or left out, which leaves
            // room for smaller ones behind it
            var quads = 0;
            var count = 0;
            coarser = 0;
            for (var i = 0; i < visible.Count; i++)
            {
                var impostor = visible[i];
                impostor.Drawn = impostor.Shape;
                var cost = Cost(impostor, impostor.Shape, eye);
                if (quads + cost > budget)
                {
                    cost = Coarser(impostor, budget - quads, eye);
                    if (cost < 0)
                    {
                        overBudget++;
                        continue;
                    }
                    coarser++;
                }
                quads += cost;
                visible[count++] = impostor;
            }

            var sun = MyVisualScriptLogicProvider.GetSunDirection();

            // Billboards of this blend mode are drawn in submission order: far to near. A safe
            // zone's far side goes before what is inside it, its near side after.
            parts.Clear();
            for (var i = 0; i < count; i++)
            {
                var impostor = visible[i];
                var distance = Vector3D.Distance(eye, impostor.State.Position + shift);
                if (impostor.Shape.IsZone)
                {
                    var radius = impostor.Shape.Zone.HalfExtents.Length();
                    parts.Add(new DrawPart(impostor, distance + radius, false));
                    parts.Add(new DrawPart(impostor, distance - radius, true));
                }
                else
                {
                    parts.Add(new DrawPart(impostor, distance, false));
                }
            }
            parts.Sort((a, b) => b.Distance.CompareTo(a.Distance));

            drawnQuads = 0;
            foreach (var part in parts)
            {
                if (part.Impostor.Shape.IsZone)
                    DrawZone(part.Impostor, eye, part.Near);
                else
                    DrawImpostor(part.Impostor, eye, sun);
            }
            visible.RemoveRange(count, visible.Count - count);
            foreach (var impostor in visible)
            {
                if (impostor.FirstDrawnTick < 0)
                    impostor.FirstDrawnTick = tick;
            }
        }

        // Stats: the tick the last turn of the view by over 20 degrees started, and the last
        // tick it jumped by over a kilometer from one frame to the next, the rigs time what
        // comes after. At rest the view moves less than half a degree per frame.
        private void NoteMoves(VRage.ModAPI.IMyCamera camera)
        {
            var forward = (Vector3)camera.WorldMatrix.Forward;
            if (lastForward == Vector3.Zero || Vector3.Dot(forward, lastForward) > 0.99996f)
            {
                restForward = forward;
                restTick = tick;
            }
            else if (Vector3.Dot(forward, restForward) < 0.94f)
            {
                turnTick = restTick;
            }
            if (
                lastEye != Vector3D.Zero
                && Vector3D.DistanceSquared(camera.Position, lastEye) > 1e6
            )
                jumpTick = tick;
            lastForward = forward;
            lastEye = camera.Position;
        }

        // A sphere inside the four side planes of the camera's view. Not IMyCamera.IsInFrustum:
        // that frustum ends at the far plane, 20 km away, and impostors go much farther.
        public static bool InView(
            VRage.ModAPI.IMyCamera camera,
            MatrixD view,
            Vector3D center,
            double radius
        )
        {
            var local = Vector3D.TransformNormal(center - camera.Position, view);
            var depth = -local.Z;
            var tanY = Math.Tan(camera.FovWithZoom / 2);
            var tanX = tanY * camera.ViewportSize.X / camera.ViewportSize.Y;
            return Math.Abs(local.X) - depth * tanX <= radius * Math.Sqrt(1 + tanX * tanX)
                && Math.Abs(local.Y) - depth * tanY <= radius * Math.Sqrt(1 + tanY * tanY);
        }

        private struct DrawPart
        {
            public readonly ClientImpostor Impostor;
            public readonly double Distance;

            // The side of a safe zone facing the camera, the far side otherwise
            public readonly bool Near;

            public DrawPart(ClientImpostor impostor, double distance, bool near)
            {
                Impostor = impostor;
                Distance = distance;
                Near = near;
            }
        }

        // A safe zone's bubble, translucent in its color and denser towards its outline, where
        // the line of sight runs along the surface. One side of it: the quads facing the camera
        // or the ones facing away.
        private void DrawZone(ClientImpostor impostor, Vector3D eye, bool near)
        {
            var zone = impostor.Shape.Zone;
            var position = impostor.State.Position + shift;
            var rotation = MatrixD.CreateFromQuaternion(impostor.State.Orientation);
            var color = zone.Color.ToVector3();
            var quad = new MyQuadD();

            foreach (var q in ZoneQuads(impostor, eye))
            {
                if (drawnQuads >= budget)
                    return;
                var center =
                    position + Vector3D.TransformNormal(q.Center * zone.HalfExtents, rotation);
                var normal = Vector3D.TransformNormal(q.Normal, rotation);
                var toEye = Vector3D.Normalize(eye - center);
                var cosine = Vector3D.Dot(normal, toEye);
                if ((cosine > 0) != near)
                    continue;

                var rim = 1 - Math.Abs(cosine);
                var edge = (float)Math.Pow(rim, ZoneRimPower);
                var alpha = Math.Min(1, ZoneAlpha + ZoneRimAlpha * edge);
                var tint = Vector3.Lerp(color, Vector3.One, ZoneRimWhite * edge);
                quad.Point0 =
                    position + Vector3D.TransformNormal(q.P0 * zone.HalfExtents, rotation);
                quad.Point1 =
                    position + Vector3D.TransformNormal(q.P1 * zone.HalfExtents, rotation);
                quad.Point2 =
                    position + Vector3D.TransformNormal(q.P2 * zone.HalfExtents, rotation);
                quad.Point3 =
                    position + Vector3D.TransformNormal(q.P3 * zone.HalfExtents, rotation);
                MyTransparentGeometry.AddQuad(
                    Material,
                    ref quad,
                    new Vector4(tint * alpha, alpha),
                    ref center,
                    -1,
                    MyBillboard.BlendTypeEnum.LDR
                );
                drawnQuads++;
            }
        }

        // A safe zone's mesh for its size on screen, the finest one when the camera is inside
        private ZoneQuad[] ZoneQuads(ClientImpostor impostor, Vector3D eye)
        {
            var zone = impostor.Shape.Zone;
            var distance = Vector3D.Distance(eye, impostor.State.Position + shift);
            var radius = zone.Box ? zone.HalfExtents.Length() : zone.HalfExtents.X;
            var camera = MyAPIGateway.Session.Camera;
            var pixels =
                distance <= radius
                    ? double.MaxValue
                    : radius
                        / distance
                        / Math.Tan(camera.FovWithZoom / 2)
                        * camera.ViewportSize.Y
                        / 2;
            return ZoneMesh.Quads(zone.Box, pixels);
        }

        // Quads an impostor draws: a shape the ones on the faces towards the camera, a safe
        // zone both of its halves
        // The finest coarser level of the impostor in the store that fits into room: it becomes
        // the one drawn this frame, and its cost is returned. -1 if none fits.
        private int Coarser(ClientImpostor impostor, int room, Vector3D eye)
        {
            var shape = impostor.Shape;
            if (shape.IsZone)
                return -1;
            for (var lod = shape.Lod / 2; lod >= 2; lod /= 2)
            {
                ClientShape stored;
                if (!store.TryGet(impostor.State.Id, (byte)lod, shape.Version, out stored))
                    continue;
                var cost = Cost(impostor, stored, eye);
                if (cost <= room)
                {
                    impostor.Drawn = stored;
                    return cost;
                }
            }
            return -1;
        }

        private int Cost(ClientImpostor impostor, ClientShape shape, Vector3D eye)
        {
            if (shape.IsZone)
                return ZoneQuads(impostor, eye).Length;

            Vector3D position;
            Matrix rotation;
            var view = LocalView(impostor, eye, out position, out rotation);
            var cost = 0;
            for (var f = 0; f < 6; f++)
            {
                if (Vector3.Dot(ClientShape.Normals[f], view) < 0)
                    cost += shape.FaceCounts[f];
            }
            return cost;
        }

        // The direction from the camera to the impostor in its local frame, where it is and
        // how it is turned
        private Vector3 LocalView(
            ClientImpostor impostor,
            Vector3D eye,
            out Vector3D position,
            out Matrix rotation
        )
        {
            var state = impostor.State;

            // Extrapolate motion between state updates, a few seconds at most
            var age = Math.Min(tick - impostor.ReceivedTick, 600) / 60f;
            position = state.Position + state.Velocity * age + shift;

            rotation = Matrix.CreateFromQuaternion(state.Orientation);
            return Vector3.Normalize(
                Vector3.TransformNormal((Vector3)(position - eye), Matrix.Transpose(rotation))
            );
        }

        // The planets, every PlanetTicks: few, and they don't move
        private void FindPlanets()
        {
            planets.Clear();
            planetEntities.Clear();
            MyAPIGateway.Entities.GetEntities(planetEntities, e => e is MyPlanet);
            foreach (var entity in planetEntities)
            {
                var planet = (MyPlanet)entity;
                var ground = (double)planet.AverageRadius;
                var top = (double)planet.MaximumRadius;
                if (planet.HasAtmosphere)
                    top = ground + (top - ground) * CloudTop * AboveClouds;
                planets.Add(
                    new PlanetShade
                    {
                        Center = planet.PositionComp.WorldAABB.Center,
                        Ground = ground,
                        Top = Math.Max(top, ground + 1),
                    }
                );
            }
            planetEntities.Clear();
        }

        // The share of the sunlight that reaches a point past the planets. Against each planet
        // one ray towards the sun: how close it passes to the center. On the planet's day side
        // the ray heads away from it, and that distance grows by how far it is the other way,
        // so the light fades in near the ground as the sun rises instead of all at once.
        private float Sunlight(Vector3D point, Vector3 sun)
        {
            var light = 1.0;
            foreach (var planet in planets)
            {
                var toCenter = planet.Center - point;
                var along = Vector3D.Dot(toCenter, sun);
                var squared = toCenter.LengthSquared();
                var pass =
                    along > 0
                        ? Math.Sqrt(Math.Max(0, squared - along * along))
                        : Math.Sqrt(squared) - along;
                if (pass >= planet.Top)
                    continue;
                var t = (pass - planet.Ground) / (planet.Top - planet.Ground);
                if (t <= 0)
                    return 0;
                light *= t * t * (3 - 2 * t);
            }
            return (float)light;
        }

        private string Planets()
        {
            var text = new System.Text.StringBuilder();
            foreach (var planet in planets)
            {
                if (text.Length > 0)
                    text.Append(';');
                text.Append(planet.Ground.ToString("0"))
                    .Append(':')
                    .Append(planet.Top.ToString("0"));
            }
            return text.ToString();
        }

        private static float Brightness(ImpostorState state, double distance)
        {
            var small = state.Kind == ImpostorKind.Voxel ? SmallBrightness : SmallGridBrightness;

            var camera = MyAPIGateway.Session.Camera;
            var pixels =
                state.Radius / Math.Max(distance, 1) / Math.Tan(camera.FovWithZoom / 2) * 540;
            var t = (float)((pixels - SmallFromPixels) / (SmallToPixels - SmallFromPixels));
            return MathHelper.Lerp(small, 1f, MathHelper.Clamp(t, 0f, 1f));
        }

        private void DrawImpostor(ClientImpostor impostor, Vector3D eye, Vector3 sun)
        {
            var state = impostor.State;
            var shape = impostor.Drawn;

            Vector3D position;
            Matrix rotation;
            var view = LocalView(impostor, eye, out position, out rotation);
            var sunLocal = Vector3.TransformNormal(sun, Matrix.Transpose(rotation));
            var brightness = Brightness(state, Vector3D.Distance(eye, position));
            var start = debug ? timer.ElapsedTicks : 0;
            var sunlight = unshaded ? 1f : Sunlight(position, sun);
            if (debug)
            {
                shadeTicks += timer.ElapsedTicks - start;
                shadeCount++;
            }
            impostor.Sunlight = sunlight;
            var diffuse = Diffuse * sunlight;
            var tinted = state.Tint.A != 0;
            var tint = Vector3.Lerp(state.Tint.ToVector3(), Vector3.One, ZoneTintPale);

            for (var f = 0; f < 6; f++)
            {
                facing[f] = Vector3.Dot(ClientShape.Normals[f], view) < 0;

                int n,
                    u,
                    v;
                Shape.Axes(f, out n, out u, out v);
                lefts[f] = Vector3.TransformNormal(ClientShape.Axis[u], rotation);
                ups[f] = Vector3.TransformNormal(ClientShape.Axis[v], rotation);
            }

            if (Vector3.Dot(shape.SortedFor, view) < ResortCosine)
                shape.Sort(view);

            foreach (var i in shape.Order)
            {
                var face = shape.Faces[i];
                if (!facing[face])
                    continue;
                if (drawnQuads >= budget)
                    return;

                var center = position + Vector3.TransformNormal(shape.Centers[i], rotation);
                var light =
                    Ambient + diffuse * Math.Max(0, Vector3.Dot(shape.Shading[i], sunLocal));
                var lit = shape.Colors[i] * light * brightness;
                if (tinted)
                    lit = Vector3.Lerp(lit, tint, ZoneTint);
                var color = new Vector4(lit, 1f);
                var half = shape.HalfSizes[i];
                MyTransparentGeometry.AddBillboardOriented(
                    Material,
                    color,
                    center,
                    lefts[face],
                    ups[face],
                    half.X,
                    half.Y,
                    Vector2.Zero,
                    MyBillboard.BlendTypeEnum.LDR
                );
                drawnQuads++;
            }
        }
    }
}
