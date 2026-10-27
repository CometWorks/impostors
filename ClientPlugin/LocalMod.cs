using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using HarmonyLib;
using Impostors;
using Sandbox.Definitions;
using Sandbox.Game.Multiplayer;
using Sandbox.Game.World;
using Sandbox.ModAPI;
using Shared.Impostors;
using VRage.FileSystem;
using VRage.Game;
using VRage.GameServices;

namespace ClientPlugin;

// Offline and on a lobby host the Impostors mod draws for the local player (SE-670), the same
// renderer as on a server: the plugin loads it from its own asset folder when the world doesn't
// list it, the way Pulsar loads client mods, so it never goes into the world's mod list. Its
// renderer and the service talk through mod messages. Without the asset folder, or with an
// older mod in the world, the plugin's own copy of the renderer draws instead.
public static class LocalMod
{
    // The mod's folder, ImpostorsMod/Mod of the plugin's sources (the ImpostorsMod asset)
    public static string Folder;

    // The renderer's source, its text tells a mod with the local messages from an older one
    private const string MarkerFile = "Data/Scripts/Impostors/Client/ImpostorRenderer.cs";

    // Whether the plugin loads the mod into this session
    private static bool loading;

    public static void Init()
    {
        // MyAPIGateway.Utilities may not be set yet, its handlers live in this instance
        MyAPIUtilities.Static.RegisterMessageHandler(
            ImpostorRenderer.ToLocalServerMessage,
            FromRenderer
        );
        ImpostorService.LocalClient = ToRenderer;
        ImpostorRenderer.ToLocalServer = ImpostorService.FromLocalClient;
    }

    private static void ToRenderer(byte[] message)
    {
        if (ImpostorRenderer.ModDrawsLocally)
            MyAPIGateway.Utilities.SendModMessage(ImpostorRenderer.FromLocalServerMessage, message);
        else
            ImpostorRenderer.FromLocalServer(message);
    }

    private static void FromRenderer(object message)
    {
        if (ImpostorRenderer.ModDrawsLocally && message is byte[] bytes)
            ImpostorService.FromLocalClient(bytes);
    }

    // A session loads the scripts of its mods first, then their definitions
    [HarmonyPatch(typeof(MyScriptManager), "LoadData")]
    private static class LoadScripts
    {
        private static readonly Action<MyScriptManager, string, MyModContext> load =
            AccessTools.MethodDelegate<Action<MyScriptManager, string, MyModContext>>(
                AccessTools.DeclaredMethod(
                    typeof(MyScriptManager),
                    "LoadScripts",
                    [typeof(string), typeof(MyModContext)]
                )
            );

        private static void Prefix()
        {
            loading = false;
            ImpostorRenderer.ModDrawsLocally = false;
            // Mod API file checks reject Data/Scripts; the plugin can inspect the marker.
            ImpostorRenderer.WorldHasMod =
                MySession.Static?.Mods?.Any(mod => Listed(mod, out _)) ?? false;
            if (MySession.Static == null || !Sync.IsServer)
                return;
            if (Plugin.Instance?.Config is not { Enabled: true })
                return;

            foreach (var mod in MySession.Static.Mods ?? [])
            {
                if (!Listed(mod, out var current))
                    continue;
                ImpostorRenderer.ModDrawsLocally = current;
                Plugin.Instance.Log.Info(
                    current
                        ? $"The world has the Impostors mod ({mod}), it draws for the local player"
                        : $"The world has an older Impostors mod ({mod}), the plugin draws for the local player"
                );
                return;
            }

            if (Folder == null || !Directory.Exists(Folder))
            {
                Plugin.Instance.Log.Warning(
                    $"No Impostors mod folder ({Folder}), the plugin draws for the local player"
                );
                return;
            }
            loading = true;
        }

        // After the scripts of the world's mods
        private static void Postfix(MyScriptManager __instance)
        {
            if (!loading)
                return;
            Plugin.Instance.Log.Info($"Loading the Impostors mod from {Folder}");
            try
            {
                var context = new MyModContext();
                context.Init(Item());
                load(__instance, Folder, context);

                // A script the game didn't compile leaves no assembly, the plugin's copy
                // draws then
                ImpostorRenderer.ModDrawsLocally = __instance.ScriptsPerMod.ContainsKey(context);
            }
            catch (Exception e)
            {
                Plugin.Instance.Log.Error(e, "Failed to load the Impostors mod's scripts");
            }
            if (!ImpostorRenderer.ModDrawsLocally)
            {
                loading = false;
                Plugin.Instance.Log.Warning(
                    "The Impostors mod's scripts didn't load, the plugin draws for the local player"
                );
            }
        }
    }

    // Its definitions, the material of the impostor quads
    [HarmonyPatch(typeof(MyDefinitionManager), nameof(MyDefinitionManager.LoadData))]
    private static class LoadDefinitions
    {
        private static void Prefix(ref List<MyObjectBuilder_Checkpoint.ModItem> mods)
        {
            if (loading)
                mods = new List<MyObjectBuilder_Checkpoint.ModItem>(mods) { Item() };
        }
    }

    private static MyObjectBuilder_Checkpoint.ModItem Item()
    {
        var item = new MyObjectBuilder_Checkpoint.ModItem("Impostors", 0, "local", "Impostors");
        item.SetModData(new ModFolder(Folder));
        return item;
    }

    // Whether a mod of the world is the Impostors mod, and one whose renderer talks to the
    // service through mod messages
    private static bool Listed(MyObjectBuilder_Checkpoint.ModItem mod, out bool current)
    {
        current = false;
        try
        {
            var path = Path.Combine(mod.GetPath(), MarkerFile);
            if (!MyFileSystem.FileExists(path))
                return false;
            using var reader = new StreamReader(MyFileSystem.OpenRead(path));
            current = reader.ReadToEnd().Contains(nameof(ImpostorRenderer.ToLocalServerMessage));
            return true;
        }
        catch (Exception)
        {
            // A mod not downloaded or without a folder
            return false;
        }
    }

    private class ModFolder : MyWorkshopItem
    {
        public ModFolder(string folder)
        {
            Folder = folder;
        }
    }
}
