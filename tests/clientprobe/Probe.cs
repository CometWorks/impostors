using System.Globalization;
using System.IO;
using Sandbox.Game.World;
using VRage.FileSystem;
using VRage.Plugins;
using VRageMath;

namespace ImpostorsClientProbe;

// Test hooks of the Impostors suite on the client: fov.txt in the user data folder sets the
// camera's vertical field of view in degrees, like a zoom; without the file it goes back.
public class Probe : IPlugin
{
    private int tick;
    private float? original;

    public void Init(object gameInstance) { }

    public void Update()
    {
        var camera = MySector.MainCamera;
        if (++tick % 30 != 0 || camera == null)
            return;

        var file = Path.Combine(MyFileSystem.UserDataPath, "fov.txt");
        if (
            File.Exists(file)
            && float.TryParse(
                File.ReadAllText(file).Trim(),
                NumberStyles.Float,
                CultureInfo.InvariantCulture,
                out var degrees
            )
        )
        {
            original ??= camera.FieldOfView;
            camera.FieldOfView = MathHelper.ToRadians(degrees);
        }
        else if (original != null)
        {
            camera.FieldOfView = original.Value;
            original = null;
        }
    }

    public void Dispose() { }
}
