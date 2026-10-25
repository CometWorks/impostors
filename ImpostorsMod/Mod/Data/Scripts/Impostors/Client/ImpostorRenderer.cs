using System;
using System.Collections.Generic;
using Sandbox.Game;
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
    // beyond the sync distance, as flat shaded quads. Runs on multiplayer clients only, the
    // server and single player have the real entities anyway.
    //
    // The client plugin compiles this file too, so Pulsar players get impostors on servers
    // whose world doesn't list the mod. That copy stands down when the mod is loaded.
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
        private readonly List<DrawPart> parts = new List<DrawPart>();
        private readonly ByteWriter writer = new ByteWriter(16);
        private readonly List<long> readyIds = new List<long>();
        private readonly List<long> unreadyIds = new List<long>();
        private readonly Vector3[] lefts = new Vector3[6];
        private readonly Vector3[] ups = new Vector3[6];
        private readonly bool[] facing = new bool[6];
        private long serverInstance;

        // The view last reported to the server, while it is zoomed
        private bool viewZoomed;
        private double sentZoom;
        private Vector3 sentForward;
        private int sentViewTick;
        private int zoomTick;
        private bool active;
        private bool debug;

        // Debug: draws the impostors in place of the real entities, which it hides
        private bool compare;

        // Debug: draws no impostors, only what the game itself has
        private bool realOnly;

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

        public override void LoadData()
        {
            if (MyAPIGateway.Utilities.IsDedicated || MyAPIGateway.Multiplayer.IsServer)
                return;

            // The client plugin's copy has no mod context
            if (ModContext == null && ModLoaded())
                return;

            MyAPIGateway.Multiplayer.RegisterSecureMessageHandler(Protocol.Channel, OnMessage);
            MyAPIGateway.Utilities.RegisterMessageHandler(BudgetMessage, OnBudget);
            MyAPIGateway.Utilities.RegisterMessageHandler(VoxelLoadedMessage, OnVoxelLoaded);
            active = true;

            // Tests put this file into the mod's local storage to get stats.txt every second
            // and to switch between the real entities and their impostors (see ReadMode)
            if (ModContext != null)
                debug = MyAPIGateway.Utilities.FileExistsInLocalStorage(
                    "debug",
                    typeof(ImpostorRenderer)
                );
        }

        protected override void UnloadData()
        {
            if (!active)
                return;

            MyAPIGateway.Multiplayer.UnregisterSecureMessageHandler(Protocol.Channel, OnMessage);
            MyAPIGateway.Utilities.UnregisterMessageHandler(BudgetMessage, OnBudget);
            MyAPIGateway.Utilities.UnregisterMessageHandler(VoxelLoadedMessage, OnVoxelLoaded);
            active = false;
            impostors.Clear();
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
            if (!active)
                return;

            if (tick++ % (Protocol.HelloInterval * 60) == 60)
            {
                writer.Clear();
                writer.Byte(Protocol.Hello);
                writer.Byte(Protocol.Version);
                MyAPIGateway.Multiplayer.SendMessageToServer(Protocol.Channel, writer.ToArray());
            }

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

            if (debug && tick % 60 == 0)
            {
                ReadMode();
                WriteStats();
            }
        }

        // Tells the server about a zoomed view (see Protocol.View): when it starts, changes
        // its zoom by 5% or turns by an eighth of its field of view, and when it ends
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
            if (!zoomed && !viewZoomed)
                return;
            if (
                zoomed
                && viewZoomed
                && Math.Abs(zoom / sentZoom - 1) < 0.05
                && Vector3.Dot(forward, sentForward) > Math.Cos(fov / 8)
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
            MyAPIGateway.Multiplayer.SendMessageToServer(Protocol.Channel, writer.ToArray());
            if (zoomed && !viewZoomed)
                zoomTick = tick;
            viewZoomed = zoomed;
            sentZoom = zoom;
            sentForward = forward;
            sentViewTick = tick;
        }

        // Tells the server which impostors' real entities started or stopped drawing. Inside
        // the replication cube it keeps an impostor until then (see Protocol.Ready).
        private void SendReady()
        {
            if (serverInstance == 0)
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
                if (ready == impostor.Ready)
                    continue;
                impostor.Ready = ready;
                if (debug && ready)
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
                MyAPIGateway.Multiplayer.SendMessageToServer(Protocol.Channel, writer.ToArray());
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
        // none of them
        private void ReadMode()
        {
            var mode = "";
            if (MyAPIGateway.Utilities.FileExistsInLocalStorage("mode", typeof(ImpostorRenderer)))
            {
                using (
                    var file = MyAPIGateway.Utilities.ReadFileInLocalStorage(
                        "mode",
                        typeof(ImpostorRenderer)
                    )
                )
                    mode = file.ReadToEnd().Trim();
            }
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
                    + " sun="
                    + Vec(MyVisualScriptLogicProvider.GetSunDirection())
                    + " tick="
                    + tick
                    + " fov="
                    + (camera != null ? MathHelper.ToDegrees(camera.FovWithZoom) : 0).ToString(
                        "0.0"
                    )
                    + " zoomed="
                    + (viewZoomed ? 1 : 0)
                    + " viewtick="
                    + sentViewTick
                    + " zoomtick="
                    + zoomTick
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
                        + ZoneStats(impostor)
                        + ScreenPosition(state)
                );
            }
            readyTicks = 0;
            using (
                var file = MyAPIGateway.Utilities.WriteFileInLocalStorage(
                    "stats.txt",
                    typeof(ImpostorRenderer)
                )
            )
                file.Write(text.ToString());
        }

        private void OnVoxelLoaded(object message)
        {
            if (message is long)
                loadedVoxels.Add((long)message);
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
            switch (type)
            {
                case Protocol.Welcome:
                {
                    if (r.Byte() != Protocol.Version)
                        return;
                    var instance = r.Long();
                    maxQuads = MathHelper.Clamp(r.Int(), 0, Protocol.BillboardLimit);
                    if (instance != serverInstance)
                    {
                        impostors.Clear();
                        serverInstance = instance;

                        // A new server knows nothing of the zoom yet
                        viewZoomed = false;
                    }
                    break;
                }

                case Protocol.States:
                {
                    var count = r.UShort();
                    for (var i = 0; i < count; i++)
                    {
                        var state = ImpostorState.Read(r);
                        Get(state.Id).State = state;
                        Get(state.Id).ReceivedTick = tick;
                    }
                    break;
                }

                case Protocol.Remove:
                {
                    var count = r.UShort();
                    for (var i = 0; i < count; i++)
                        impostors.Remove(r.Long());
                    break;
                }

                case Protocol.ShapeData:
                {
                    var count = r.UShort();
                    for (var i = 0; i < count; i++)
                    {
                        var id = r.Long();
                        var version = r.Int();
                        Get(id).Shape = ClientShape.Read(r, version);
                        Get(id).ShapeTick = tick;
                    }
                    break;
                }
            }
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
            if (!active || impostors.Count == 0 || realOnly)
                return;

            var camera = MyAPIGateway.Session.Camera;
            if (camera == null)
                return;
            var eye = camera.Position;

            // Nearest first while counting the budget, so the far ones are dropped
            foreach (var impostor in impostors.Values)
            {
                if (impostor.Shape == null || impostor.State.Id == 0)
                    continue;

                // The real entity draws itself. Until then, while a grid or safe zone is still
                // streaming in, the impostor stands in for it.
                if (!compare && shift == Vector3D.Zero)
                {
                    if (impostor.Ready)
                        continue;
                    if (MyAPIGateway.Entities.EntityExists(impostor.State.Id))
                        standIns++;
                }

                visible.Add(impostor);
            }

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

            var quads = 0;
            var count = 0;
            while (count < visible.Count)
            {
                var cost = Cost(visible[count], eye);
                if (quads + cost > budget)
                    break;
                quads += cost;
                count++;
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
        private int Cost(ClientImpostor impostor, Vector3D eye)
        {
            var shape = impostor.Shape;
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
            var shape = impostor.Shape;

            Vector3D position;
            Matrix rotation;
            var view = LocalView(impostor, eye, out position, out rotation);
            var sunLocal = Vector3.TransformNormal(sun, Matrix.Transpose(rotation));
            var brightness = Brightness(state, Vector3D.Distance(eye, position));
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
                    Ambient + Diffuse * Math.Max(0, Vector3.Dot(shape.Shading[i], sunLocal));
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
