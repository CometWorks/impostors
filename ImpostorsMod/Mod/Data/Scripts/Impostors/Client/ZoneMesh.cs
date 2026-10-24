using System;
using VRageMath;

namespace Impostors
{
    // The bubble of a safe zone as flat quads around the unit sphere or cube, scaled by the
    // zone's half extents when drawn
    public class ZoneQuad
    {
        public Vector3 P0;
        public Vector3 P1;
        public Vector3 P2;
        public Vector3 P3;
        public Vector3 Center;
        public Vector3 Normal;
    }

    public static class ZoneMesh
    {
        // Enough for a round outline at the few hundred pixels a far zone covers at most
        private const int Segments = 32;
        private const int Rings = 16;

        private static ZoneQuad[] sphere;
        private static ZoneQuad[] box;

        public static ZoneQuad[] Quads(bool isBox)
        {
            if (isBox)
                return box ?? (box = BuildBox());
            return sphere ?? (sphere = BuildSphere());
        }

        private static Vector3 OnSphere(int ring, int segment)
        {
            var latitude = MathHelper.Pi * ring / Rings - MathHelper.PiOver2;
            var longitude = MathHelper.TwoPi * segment / Segments;
            var c = (float)Math.Cos(latitude);
            return new Vector3(
                c * (float)Math.Cos(longitude),
                (float)Math.Sin(latitude),
                c * (float)Math.Sin(longitude)
            );
        }

        private static ZoneQuad Quad(Vector3 p0, Vector3 p1, Vector3 p2, Vector3 p3, Vector3 normal)
        {
            return new ZoneQuad
            {
                P0 = p0,
                P1 = p1,
                P2 = p2,
                P3 = p3,
                Center = (p0 + p1 + p2 + p3) / 4,
                Normal = normal,
            };
        }

        private static ZoneQuad[] BuildSphere()
        {
            var quads = new ZoneQuad[Segments * Rings];
            var n = 0;
            for (var r = 0; r < Rings; r++)
            for (var s = 0; s < Segments; s++)
            {
                var p0 = OnSphere(r, s);
                var p1 = OnSphere(r, s + 1);
                var p2 = OnSphere(r + 1, s + 1);
                var p3 = OnSphere(r + 1, s);
                quads[n++] = Quad(p0, p1, p2, p3, Vector3.Normalize(p0 + p1 + p2 + p3));
            }
            return quads;
        }

        private static ZoneQuad[] BuildBox()
        {
            var quads = new ZoneQuad[6];
            for (var f = 0; f < 6; f++)
            {
                int n,
                    u,
                    v;
                Shape.Axes(f, out n, out u, out v);
                var normal = ClientShape.Normals[f];
                var du = ClientShape.Axis[u];
                var dv = ClientShape.Axis[v];
                quads[f] = Quad(
                    normal - du - dv,
                    normal + du - dv,
                    normal + du + dv,
                    normal - du + dv,
                    normal
                );
            }
            return quads;
        }
    }
}
