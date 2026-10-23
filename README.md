# Impostors

Far view for Space Engineers 1 multiplayer. The server doesn't send grids beyond the sync
distance (3 km by default) to the players, and edited asteroids only to about 8.7 km, so
players see empty space where a station or a fleet is. Impostors shows them out to the view
distance as coarse, flat shaded models, built on the server from block and voxel data.

- **Server plugin** (Magnetar): picks what each player should see, builds the impostors and
  sends them over mod networking.
- **Impostors mod**: draws them on the clients. Players need no plugin, only the mod in the
  world's mod list.
- **Client plugin** (Pulsar): draws them on servers whose world doesn't have the mod, and
  serves the players who joined when you host a lobby.

How it works, its limits and the plans for clusters: [Docs/Design.md](Docs/Design.md).

## Server setup

1. Enable the Impostors server plugin in Magnetar.
2. Add the Impostors mod to the world.
3. Optionally edit `Impostors.cfg` in the server's data folder (written with the defaults on
   the first start):

| Option | Default | Meaning |
|---|---|---|
| `Enabled` | true | Turns the plugin on |
| `Grids` | true | Impostors of grids |
| `MinBlocks` | 8 | Grid groups with fewer blocks are not shown |
| `MaxDistance` | 0 | Farthest impostor in meters, 0 is the world's view distance |
| `MinPixels` | 3 | Impostors less than this many pixels across on a 1080p screen are not sent |
| `MaxImpostorsPerPlayer` | 200 | The largest on screen win |
| `ShapeBytesPerSecond` | 32768 | Shape data sent to one player per second |
| `RebuildSeconds` | 60 | Shortest time between two rebuilds of an impostor |
| `RefreshSeconds` | 600 | Impostors in view are rebuilt this often to catch paint changes |
| `ReplicationMargin` | 500 | Impostors arrive this far inside the sync distance already |

## Development

`ImpostorsMod/Mod` is the mod. Building `ImpostorsMod` links it into the game's local mods
folder as `Impostors`. Its project exists for the IDE and compile checks; the game compiles
the mod's scripts itself. The plugins compile parts of it too: the server plugin
`Data/Scripts/Impostors/Common` (the protocol), the client plugin all of it.

Mod code is C# 6 and may use only what the game's mod whitelist allows. A build doesn't check
the whitelist, loading the mod in the game does.

`tests/scenario.py` checks it all in game: see its docstring.

## Remarks

### Plugin version

The plugin version lives in `Version.Build.props`, which **is** committed and imported by
`Directory.Build.props`. Keeping the version separate from the local path overrides means it
is shared by all contributors and stays under version control. Bump the version there.

### Folder path overrides

`Directory.Build.props` **is** committed and declares the overridable folder paths with empty
defaults:

- `Bin64` — the folder containing `SpaceEngineers.exe`
- `Dedicated64` — the folder containing `SpaceEngineersDedicated.exe`
- `Pulsar` — the Pulsar folder the client plugin is deployed into after each build, empty by
  default (see [Deployment](#deployment))
- `Magnetar` — the Magnetar installation folder, the one holding the launcher executables
  and their `Libraries`, which is where `PluginSdk.dll` is referenced from
- `MagnetarData` — the Magnetar config folder the server plugin is deployed into, the one
  holding `Local`, `Sources` and `Profiles`, empty by default (see [Deployment](#deployment))
- `Mods` — the game's local mods folder, `ImpostorsMod/Mod` is linked into it as `Impostors`
  after each build of the mod project (auto-detected: `%APPDATA%\SpaceEngineers\Mods` on
  Windows, `~/.config/SpaceEngineers/Mods` on Linux)

It optionally imports `Directory.Build.props.user` from the repository root, which is **not
committed** (matched by `*.user` in `.gitignore`), so each contributor keeps their own local
paths there.

To override a path manually, copy the first `PropertyGroup` of `Directory.Build.props` into
`Directory.Build.props.user`, wrapped into a top-level `<Project>` element, and fill in your
paths. `setup.py` writes that file for you with the auto-detected install locations, creating
it if needed and keeping any other overrides already in it.

Leaving `Bin64`, `Dedicated64` or `Magnetar` empty (or having no `Directory.Build.props.user`
at all) falls back to the auto-detection in `Directory.Build.props`, which reads the Steam
registry keys on Windows and the usual Steam locations on Linux, then resolves the game and the
Dedicated Server through Steam's `libraryfolders.vdf`, so installs on a secondary Steam library
are found as well. `Magnetar` defaults to the `Magnetar\` tree next to the server install on
Windows and to `$XDG_CONFIG_HOME/Magnetar` (`~/.config/Magnetar`) on Linux.

The build fails with a clear message if `Bin64`, `Dedicated64` or Magnetar's `PluginSdk.dll`
cannot be resolved.

`Pulsar` and `MagnetarData` are never auto-detected. Leaving them empty turns off deployment.

### Deployment

Builds don't deploy anything by default. Load your working copy through a development folder
instead: start Pulsar or Magnetar with `-sources` and add it with the Sources button. The loader
then compiles the plugin from source at startup.

A deployed DLL shows up in the loader as a separate local plugin. If you later disable the
development folder, that stale copy can still be enabled and shadow the published version of
your plugin.

To deploy anyway, set the loader folders in `Directory.Build.props.user`, or pass them to a
single build with `dotnet build -p:Pulsar=... -p:MagnetarData=...`. The usual values:

| Property       | Windows                 | Linux                    |
|----------------|-------------------------|--------------------------|
| `Pulsar`       | `$(APPDATA)\Pulsar`     | `$(HOME)/.config/Pulsar` |
| `MagnetarData` | `$(Magnetar)\Magnetar`  | `$(Magnetar)/Magnetar`   |

`$(Magnetar)` is the auto-detected Magnetar install folder. Its `Magnetar` subfolder holds
the configuration, including `Local`, and both launchers (Legacy and Interim) use it.

Each successful build then copies itself into its loader's `Local` plugin folder:

| Project        | Build     | Deployed to                                       |
|----------------|-----------|---------------------------------------------------|
| `ClientPlugin` | `net48`   | `<Pulsar>/Legacy/Local/<PluginName>/`             |
| `ClientPlugin` | `net10.0` | `<Pulsar>/Interim/Local/<PluginName>/`            |
| `ServerPlugin` | `net10.0` | `<MagnetarData>/Local/`                           |

Pulsar identifies a plugin by its folder, so the client DLL is copied as `plugin.dll`, its
symbols as `plugin.pdb` and the PluginHub registration XML from the repository root as
`plugin.xml`. Magnetar identifies a plugin by its DLL file name, so the server plugin is
copied flat as `<PluginName>.dll`, with the MagnetarHub registration XML next to it as
`<PluginName>.dll.xml`. Either way the loader shows the plugin under its friendly name and
honours the runtime and platform restrictions declared in the XML.

`Interim` is the Pulsar executable running Space Engineers 1 on .NET 10. It falls back to the
`Legacy` data folder when `<Pulsar>/Interim` does not exist, and so does the deployment.
(`<Pulsar>/Modern` belongs to Space Engineers 2 and is never a deployment target here.)
`MagnetarInterim` is its dedicated server counterpart, but Magnetar keeps one config folder
for both of its launchers, `<Magnetar>/Magnetar`, so only one build of the server plugin can
be deployed: the `net10.0` one, which is also the only one on Linux, where only the Interim
launchers exist. Run the launcher with `-useHome` or `-config` and set `MagnetarData` to
that folder to deploy there instead.

### Debugging

- Always use a debug build if you want to set breakpoints and see variable values.
- A debug build defines `DEBUG`, so you can add conditional code in `#if DEBUG` blocks.
- While debugging a specific target unload the other one. It prevents the IDE to be confused.
- If breakpoints do not "stick" or do not work, then make sure that:
  - Other projects are unloaded, only the debugged one and Shared are loaded.
  - Debugger is attached to the running process.
  - You are debugging the code which is running (no code changes made since the build).
- Transpiler patches will write a `harmony.log.txt` file to your `Desktop` while running `Debug`
  builds. Never release a debug build to your users, because that would litter their desktop
  as well.
- To debug transpiler changes to the IL code it is most practical to generate the files
  of the method's IL code before and after the change made, so you can just diff them.
  Please see the transpiler example under the `Shared/Patches` folder for the details.

