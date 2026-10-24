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
        // Spheres by on-screen radius: segments around (twice the rings), and the largest radius
        // in pixels each is for. Their outline stays within about half a pixel of a circle,
        // r * (1 - cos(pi / segments)) <= 0.5.
        private static readonly int[] SphereSegments = { 8, 16, 32 };
        private static readonly float[] SpherePixels = { 6, 25, float.MaxValue };

        public const int MaxQuads = 32 * 16;

        private static readonly ZoneQuad[][] spheres = new ZoneQuad[SphereSegments.Length][];
        private static ZoneQuad[] box;

        // The quads of a zone covering this many pixels in radius on screen
        public static ZoneQuad[] Quads(bool isBox, double pixels)
        {
            if (isBox)
                return box ?? (box = BuildBox());
            var i = 0;
            while (pixels > SpherePixels[i])
                i++;
            return spheres[i] ?? (spheres[i] = BuildSphere(SphereSegments[i]));
        }

        private static Vector3 OnSphere(int ring, int segment, int segments)
        {
            var latitude = MathHelper.Pi * ring / (segments / 2) - MathHelper.PiOver2;
            var longitude = MathHelper.TwoPi * segment / segments;
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

        private static ZoneQuad[] BuildSphere(int segments)
        {
            var rings = segments / 2;
            var quads = new ZoneQuad[segments * rings];
            var n = 0;
            for (var r = 0; r < rings; r++)
            for (var s = 0; s < segments; s++)
            {
                var p0 = OnSphere(r, s, segments);
                var p1 = OnSphere(r, s + 1, segments);
                var p2 = OnSphere(r + 1, s + 1, segments);
                var p3 = OnSphere(r + 1, s, segments);
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
