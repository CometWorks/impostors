# Development

`ImpostorsMod/Mod` is the mod. Building `ImpostorsMod` links it into the game's local mods
folder as `Impostors`. Its project exists for the IDE and compile checks; the game compiles
the mod's scripts itself. The plugins compile parts of it too: the server plugin
`Data/Scripts/Impostors/Common` (the protocol), the client plugin all of it.

Mod code is C# 6 and may use only what the game's mod whitelist allows. A build doesn't check
the whitelist, loading the mod in the game does.

## Tests

- `tests/builds.py` builds the client and server plugins for net48 and net10.0, net48 the
  way Pulsar Legacy and Magnetar Legacy compile it (TRACE only, no `LOCAL_BUILD`). Run it before
  a release: Linux builds only net10.0 by default, so a .NET Core-only API breaks the Legacy
  loaders unnoticed (SE-1367). Needs no game running.
- `tests/scenario.py` checks it all in game: an isolated Magnetar server with the plugin and
  a headless client that has only the mod. `IMP_ONLY=zones` runs only the safe zone checks.
  See its docstring.
- `tests/local.py` checks the client plugin's service for the local player: the test world
  offline, or with `IMP_MODE=lobby` hosted as a lobby game over DirectTransport with a second
  client joining. The world doesn't list the mod, so the plugin loads it, and the mod must
  draw instead of the plugin's copy of the renderer; `IMP_WORLD_MOD=1` lists it in the world.
  See its docstring.
- `tests/shadow_flicker.py` compares shadowed, partly shaded, and sunlit asteroids in a
  fixed view, offline or with `IMP_MODE=server` on Magnetar. The test probe records
  sunlight and drawn levels every frame, checks that only one renderer draws each
  asteroid, and compares captured pixels for flicker. Each run records the tested
  commit, staged mod location, and source hashes.
- `tests/compare.py` shoots a grid (from a blueprint), an asteroid or a safe zone next to its
  impostor from the 8 cube corners at a list of distances and puts the shots into one sheet,
  like the ones on the [Grids](Grids.md), [Asteroids](Asteroids.md) and
  [Safe zones](SafeZones.md) pages. `IMP_ZONE` puts the grid inside a safe zone. With
  `IMP_ZOOMS` it makes the zoom sheets of the [Zoomed views](Zoom.md) page instead; the
  client then runs the test probe of `tests/clientprobe`, which sets the camera's field of
  view. See its docstring.
- `tests/zoom.py` checks the finer levels of a zoomed view: the client aims down the rifle's
  iron sight (70° to 40°) on a 1920x1080 screen, and it measures how soon the finer shapes
  arrive, in client ticks.
- `tests/view_gaps.py` counts the gaps of a moving view: a ring of saved asteroids, zoomed
  pans across it and a fast spectator flight over it, flown by the client test probe
  (`camera.txt`). Every second it compares what the mod draws with the asteroids that
  should be on screen, and says why the missing ones are missing. `IMP_SURVIVAL=1` runs it
  without creative rights, where only the `View` message tells the server about the camera.
- `tests/billboards.py` checks the billboard limit: the client test probe adds PostPP and LDR
  billboards until a frame is over 32768 and reports which billboards the game cuts. With
  the mod only the impostors keep within `MaxQuadsPerFrame` (and the game still cuts other
  billboards); with `IMP_PLUGIN=1` only impostor quads go. See its docstring.
- `tests/streaming.py` checks the hand-over when the player arrives fast: it jumps from 5 km
  to next to the station, a safe zone and an asteroid, deep into the replication cube, and
  checks that the impostor stays and is drawn until the real entity draws, at the full
  level for its new distance once the next pick has sent it, then hides and goes. `IMP_WITH_PLUGIN=1` also runs the client plugin, which tells
  the mod when an asteroid's meshes show. See its docstring.
- `tests/arrival.py` arrives next to the stations of the "Octahedron - Seed 2" world, the
  way a Trade Lane drops a ship out of its tunnel: from 20 km out to 600 m from each
  station's safe zone. Until the real grids draw, the stand-ins must show the full level for
  their distance (SE-709). `IMP_ZONE_LOWER=1` turns `SafeZoneLowerLevel` on and adds an
  arrival inside each zone. See its docstring.
- `tests/ring.py` flies over the asteroid ring of the "Octahedron - Seed 2" world with its
  Workshop mods (built by `worlds/octahedron/build_world.py --seed 2`) and prints, per height,
  the voxel maps the client has, the impostors it got and drew, their quads and the frame's
  billboards. See its docstring.
- `tests/station.py` looks at the same ring from above North Station, 135 to 231 km away,
  offline with the client plugin (which loads the mod) or with `IMP_MODE=server` on a
  Magnetar server with the mod, and prints what each view has and draws, to compare the
  two. See its docstring.
- `tests/cache.py` checks the client's disk cache: the client joins, leaves and joins again,
  and the rejoin must get no shapes for the unchanged impostors. Before it, the cache file
  gets entries retention must drop, so it checks the size cap too. `IMP_PLUGIN=1` runs it
  with the client plugin.
- `tests/server_cache.py` checks the server's shape cache: the server restarts between two
  joins and must build nothing the second time, with the versions kept (the client's cache
  then sends no shapes either). It also checks retention and the size cap through the
  background sweep, and that turning asteroid occlusion off rebuilds the asteroids only.
- `tests/cache_retention.py` checks the retention of the server's shape cache offline, without
  a game (SE-711): fake entries unused, built long ago, used lately and over the cap, files
  that are no entries, the asteroid pack folder, a read's last use across a restart, the
  background sweep's timing, and the CPU and I/O cost of a sweep on 50000 files.
  `IMP_QUICK=1` skips the 2 minute wait for the background sweep.
- `tests/cache_gains.py` measures what the caches save (SE-662): joins with both caches
  empty, with the server's warm, both warm and only the client's warm, each repeated. It
  prints the builds and cache hits with their CPU time, the first-sight latency and the shape
  bytes per state, and the cold builds' table. Results in [Design](Design.md#cache-gains-and-consistency-se-662).
- `tests/cache_consistency.py` checks the caches against content changes and races (SE-662):
  two clients, layers built and ground off, repaints, an asteroid mined, a split and a merge,
  bursts of changes faster than the rebuild interval, a client killed while its cache saves,
  the server killed during rebuilds, and at the end a rebuild with the cache wiped that must
  match what the cache served. `IMP_ROUNDS` sets the rounds (4).
- `tests/grid_pack.py` writes the grid pack of the "Octahedron - Seed 2" world and checks it: hit rate,
  a join with and without it, prefabs spawned in play, the fallback, offline and a lobby game.
- `tests/asteroid_pack.py` writes the asteroid pack of the "Octahedron - Seed 2" world
  (`write`), then joins above its asteroid ring with empty shape caches without and with the
  pack (`measure`) and compares the asteroid builds and the shape bytes. `reset` follows one
  asteroid through a cut, a server restart and a voxel reset; `offline` checks the pack in an
  offline game with the client plugin. `compare` puts two packs side by side: the asteroids
  with the same shapes, and the quads and bytes per level. See its docstring.
- `tests/early_asteroids.py` times how soon the asteroids show when a client joins above
  the same ring, with an empty cache, a warm cache and the pack, each with and without the
  early asteroids, and the bytes the server sent (`measure`); `swap` checks an early shape of
  an asteroid mined since the pack was made; `offline` does the same with the client plugin
  offline. See its docstring.
- `tests/asteroid_normals.py` checks the smooth asteroid normals offline, without a game
  running (SE-710): it builds a made-up rock with flat and with smooth normals through
  `VoxelShapes`, and compares the quads, bytes and build times, and how far each level's
  normals and its image, drawn at the size the level is shown at, are from the rock itself.
  It draws them next to the rock, ray marched, into one sheet.
- `tests/asteroid_levels.py` checks how large and how bright each asteroid level is next to
  the real asteroid, offline (SE-722). `dump` has the test probe write the voxels of every
  20th asteroid of the "Octahedron - Seed 2" world; `voxels` builds their shapes through
  `VoxelShapes` and compares each level's area and light with the voxels themselves, from 6
  directions under 8 suns; `pack` does the same for an asteroid pack's levels against its
  finest one. See its docstring.
- `tests/ring_budget.py <pack> old|new` replays the client's pick of the ring asteroids'
  levels from an asteroid pack's index, no game: per camera spot and zoom the asteroids in
  view, the quads and the levels, before SE-971 (`old`) or since (`new`).
  `tests/ring_levels.py <tag>` takes the same spots and zooms in the game, offline with the
  client plugin and the pack, and saves a screenshot and the stats of each (SE-971).
- `tests/ring_still.py <pack> old|new [--used MAX]` replays a still view's picks of the same
  spots, the level each asteroid shows fed back into the next pick, and counts the level
  changes; `--used` lets the server's impostors take up to that many quads, drawn anew each
  second. `tests/still_levels.py <tag>` holds the camera still at some of the spots in the
  game for 150 seconds each and counts the drawn asteroids' level changes, PASS for none
  (SE-1097).
- `tests/radio_spectrometry.py` reads what the Radio Spectrometry mod's LCD shows for a ring
  asteroid of "Octahedron - Seed 2", loaded and drawn only as an impostor, on a server with a
  headless client (SE-984). See [Radio Spectrometry](RadioSpectrometry.md).
- `tests/level_views.py` shoots three views of the Seed 2 asteroid ring (from Gamma Station,
  and from above the ring along it both ways), once with the impostors and once with the
  real asteroids up to the game's 50 km, offline (`IMP_SETUP=offline`) or on a server. It
  times how soon the impostors in view show and settle, tables the levels drawn by distance,
  and compares the asteroids alone on screen with their real ones. `tests/cluster_views.py`
  takes the same views across a cluster on the cluster's scenario bench.
- `tests/build_throughput.py` measures how fast the server builds the asteroids of the
  "Octahedron - Seed 2" ring for a client that joins above it with empty caches (SE-678): the
  bursts of builds, the busiest 5 seconds and the game thread time per build. `logs <files>`
  prints the same summary for saved server logs. See its docstring.
- `tests/cluster_scenario.py` checks far view across a cluster on the cluster's scenario
  bench. An asteroid every node builds checks that its shapes reach the WA's cache;
  `IMP_CACHE_MB=0` turns the caches off for a baseline.
- `tests/cluster_delete.py` deletes grids on a cluster node, alone in their partition and in
  partitions a split renumbers right after, and checks that the WA's index, which every
  node's far view reads, drops them and keeps the others (SE-1090).
- `tests/cluster_removal.py` fails a handover on purpose, with the target holding the failed
  import's grids for a few seconds (the cluster test plugin's hold-import seam, SE-1167), so it
  reports and then releases them. It removes grids on the source node two ways (admin delete,
  plain close), seeds the WA's saved index with orphan records and lets two partitions go
  offline. It checks that the WA's index drops the removed grids and the orphans and keeps the
  offline grids, and prints the time each took (SE-1145). It sets the plugin's cluster timers
  (`IMPOSTORS_CLUSTER_*_SECONDS`) to seconds, so it waits seconds where the defaults wait
  minutes; `IMP_FAST=0` keeps the defaults.
- `tests/shutdown.py` checks the server's shutdown (SE-676): the server starts the service and
  gets a SIGTERM, which disposes the plugins off the game thread. The log must have no
  "Dispose failed", and the test probe must find the service's state cleared.
- `tests/build_times.py` makes a table of the build times from the `Impostor build:` lines of
  server logs, per object and level. Any run that builds impostors writes them;
  `scenario.py` and `compare.py` save the table as `build-times.md` next to their shots.
- `tests/planet_shadow.py` checks the planet shadow (SE-713): grids on the day side, the
  night side and in the band around the terminator of an EarthLike planet, each shot real,
  as its impostor, unshaded as before and against the background from 600 m and 3 km. It
  checks every impostor's sunlight against its own ray test and that the impostors in the
  shadow lose their sunlit part, and prints how bright the real grids are next to them and
  what the shade costs per impostor. See its docstring.
- `tests/asteroid_lighting.py` checks the ambient light of asteroid impostors (SE-745): a
  512 m asteroid around the EarthLike planet, in sunlight, in the shadow's band and in the
  shadow, from its sunlit and its dark side at 3 and 8 km, real and as its impostor beside
  it. It compares their luminance and color in the middle of the asteroid.
  `IMP_EXTRA_MODS=3415912276:<folder>` loads Seed 2's environment mod as a world mod (the
  rig's `IMP_EXTRA_MODS` stages any mod folder under its Workshop id). See its docstring.
  `tests/vx2_material.py` makes an asteroid of one voxel material for it (`IMP_ASTEROID`), to
  fit an ore's color (SE-1102).
- `tests/zone_mesh.py` checks the safe zone meshes offline, without a game running: the
  sphere for each size on screen, up to a camera inside the zone, infinity and NaN.

`IMP_SMOOTH=0` or `1` sets `AsteroidSmoothNormals` in every config a test writes, so any of
them runs with flat or smooth asteroid normals.

## Plugin version

The plugin version lives in `Version.Build.props`, which **is** committed and imported by
`Directory.Build.props`. Keeping the version separate from the local path overrides means it
is shared by all contributors and stays under version control. Bump the version there.

## Folder path overrides

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

## Deployment

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

## Debugging

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
  of the method's IL code before and after the change made, so you can just diff them:
  `TranspilerHelpers.RecordOriginalCode` and `RecordPatchedCode` in `Shared/Tools` do that.

