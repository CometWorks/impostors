using System;
using System.IO;
using System.Threading;
using HarmonyLib;
using Impostors;
using Sandbox;
using Shared.Config;
using Shared.Impostors;
using Shared.Logging;
using Shared.Patches;
using Shared.Plugin;
using VRage.FileSystem;
using VRage.Game;
using VRage.Plugins;
// Define assembly version when compiled by Pulsar
#if !LOCAL_BUILD
using System.Reflection;

[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

#endif

namespace ClientPlugin;

// ReSharper disable once UnusedType.Global
public class Plugin : IPlugin, ICommonPlugin
{
    public const string Name = "Impostors";
    public static Plugin Instance { get; private set; }
    public long Tick { get; private set; }
    private static bool failed;

    public IPluginLogger Log => Logger;
    private static readonly IPluginLogger Logger = new PluginLogger(Name);

    public IPluginConfig Config => config?.Data;
    private PersistentConfig<PluginConfig> config;
    private static readonly string ConfigFileName = $"{Name}.cfg";

    [System.Runtime.CompilerServices.MethodImpl(
        System.Runtime.CompilerServices.MethodImplOptions.NoInlining
    )]
    public void Init(object gameInstance)
    {
#if DEBUG
        // Allow the debugger some time to connect once the plugin assembly is loaded
        Thread.Sleep(100);
#endif

        Instance = this;

        Log.Info("Loading");

        var configPath = Path.Combine(MyFileSystem.UserDataPath, ConfigFileName);
        config = PersistentConfig<PluginConfig>.Load(Log, configPath);

        var gameVersion = MyFinalBuildConstants.APP_VERSION_STRING.ToString();
        Common.SetPlugin(this, gameVersion, MyFileSystem.UserDataPath);

        var harmony = new Harmony(Name);
        if (!PatchHelpers.HarmonyPatchAll(Log, harmony))
        {
            failed = true;
            return;
        }
        BillboardPriority.Patch(harmony, Log);

        // Offline and on a lobby host the service serves the local player too, straight to
        // the renderer
        ImpostorService.LocalClient = ImpostorRenderer.FromLocalServer;
        ImpostorRenderer.ToLocalServer = ImpostorService.FromLocalClient;

        // The renderer copy of the plugin takes the asteroid pack as configured; the mod's own
        // copy always does
        ImpostorRenderer.UsePack = Config.AsteroidPack;
        ImpostorRenderer.PackMod = Config.AsteroidPackMod;

        Log.Debug("Successfully loaded");
    }

    public void Dispose()
    {
        try
        {
            ImpostorService.Stop();
        }
        catch (Exception ex)
        {
            Log.Critical(ex, "Dispose failed");
        }

        Instance = null;
    }

    public void Update()
    {
        if (failed)
            return;

#if DEBUG
        CustomUpdate();
        Tick++;
#else
        try
        {
            CustomUpdate();
            Tick++;
        }
        catch (Exception e)
        {
            Log.Critical(e, "Update failed");
            failed = true;
        }
#endif
    }

    private void CustomUpdate()
    {
        PatchHelpers.PatchUpdates();
        BillboardPriority.Update();
        VoxelReadiness.Update();

        // Serves the local player in an offline game, and on a lobby host the players who
        // joined as well
        ImpostorService.UpdateStatic(Config, Log);
        if (!MySandboxGame.IsPaused)
            ImpostorRenderer.UpdateLocal();
    }
}
