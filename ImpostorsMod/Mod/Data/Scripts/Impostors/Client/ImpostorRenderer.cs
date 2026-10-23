using System;
using System.Collections.Generic;
using Sandbox.Game;
using Sandbox.ModAPI;
using VRage.Game;
using VRage.Game.Components;
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

        // Billboards are shared with every effect of the game, which has 32768 per frame
        private const int MaxQuads = 12000;

        private const float Ambient = 0.35f;
        private const float Diffuse = 0.65f;

        // Re-sort a shape's quads when the view direction turned by about 3 degrees
        private const float ResortCosine = 0.9986f;

        private static readonly MyStringId Material = MyStringId.GetOrCompute("Square");

        private readonly Dictionary<long, ClientImpostor> impostors =
            new Dictionary<long, ClientImpostor>();
        private readonly List<ClientImpostor> visible = new List<ClientImpostor>();
        private readonly ByteWriter writer = new ByteWriter(16);
        private readonly Vector3[] lefts = new Vector3[6];
        private readonly Vector3[] ups = new Vector3[6];
        private readonly bool[] facing = new bool[6];
        private long serverInstance;
        private bool active;
        private bool debug;
        private int tick;
        private int drawnQuads;

        public override void LoadData()
        {
            if (MyAPIGateway.Utilities.IsDedicated || MyAPIGateway.Multiplayer.IsServer)
                return;

            // The client plugin's copy has no mod context
            if (ModContext == null && ModLoaded())
                return;

            MyAPIGateway.Multiplayer.RegisterSecureMessageHandler(Protocol.Channel, OnMessage);
            active = true;

            // Tests put this file into the mod's local storage to get stats.txt every 5 s
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

            if (debug && tick % 300 == 0)
                WriteStats();
        }

        private void WriteStats()
        {
            var text = new System.Text.StringBuilder();
            text.AppendLine(
                "impostors=" + impostors.Count + " drawn=" + visible.Count + " quads=" + drawnQuads
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
                        + " real="
                        + (MyAPIGateway.Entities.EntityExists(state.Id) ? 1 : 0)
                        + " drawn="
                        + (visible.Contains(impostor) ? 1 : 0)
                        + " pos="
                        + state.Position.X.ToString("0.0")
                        + ","
                        + state.Position.Y.ToString("0.0")
                        + ","
                        + state.Position.Z.ToString("0.0")
                );
            }
            using (
                var file = MyAPIGateway.Utilities.WriteFileInLocalStorage(
                    "stats.txt",
                    typeof(ImpostorRenderer)
                )
            )
                file.Write(text.ToString());
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
                    if (instance != serverInstance)
                    {
                        impostors.Clear();
                        serverInstance = instance;
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
                    var id = r.Long();
                    var version = r.Int();
                    Get(id).Shape = ClientShape.Read(r, version);
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
            if (!active || impostors.Count == 0)
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

                // The real entity is replicated, it draws itself
                if (MyAPIGateway.Entities.EntityExists(impostor.State.Id))
                    continue;

                visible.Add(impostor);
            }

            visible.Sort(
                (a, b) =>
                    Vector3D
                        .DistanceSquared(eye, a.State.Position)
                        .CompareTo(Vector3D.DistanceSquared(eye, b.State.Position))
            );

            var quads = 0;
            var count = 0;
            while (count < visible.Count && quads + visible[count].Shape.Count / 2 <= MaxQuads)
                quads += visible[count++].Shape.Count / 2;

            var sun = MyVisualScriptLogicProvider.GetSunDirection();

            // Billboards of this blend mode are drawn in submission order: far to near
            drawnQuads = 0;
            for (var i = count - 1; i >= 0; i--)
                DrawImpostor(visible[i], eye, sun);
            visible.RemoveRange(count, visible.Count - count);
        }

        private void DrawImpostor(ClientImpostor impostor, Vector3D eye, Vector3 sun)
        {
            var state = impostor.State;
            var shape = impostor.Shape;

            // Extrapolate motion between state updates, a few seconds at most
            var age = Math.Min(tick - impostor.ReceivedTick, 600) / 60f;
            var position = state.Position + state.Velocity * age;

            var rotation = Matrix.CreateFromQuaternion(state.Orientation);
            var toLocal = Matrix.Transpose(rotation);
            var view = Vector3.Normalize(
                Vector3.TransformNormal((Vector3)(position - eye), toLocal)
            );
            var sunLocal = Vector3.TransformNormal(sun, toLocal);

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

                var center = position + Vector3.TransformNormal(shape.Centers[i], rotation);
                var light =
                    Ambient + Diffuse * Math.Max(0, Vector3.Dot(shape.Shading[i], sunLocal));
                var color = new Vector4(shape.Colors[i] * light, 1f);
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
