# Impostors: design

Far view for Space Engineers 1 multiplayer. Players see nothing beyond the sync distance
(3 km by default): the server doesn't replicate grids that far, and edited asteroids only to
about `ViewDistance/√3`. Impostors fill that gap with coarse models of far grids and asteroids,
out to the view distance.

For what they look like, with screenshots, see [Grids](Grids.md) and
[Asteroids](Asteroids.md). This page is about how it works inside.

Tickets: SE-381 (shared design), SE-382 (grids), SE-383 (asteroids) in Vikunja. This document
replaces the 2026-10-15 draft (`plans/se1-0199-far-view/PLAN.md`, attached to SE-381) where
the two disagree.

## Decisions (Viktor, 2026-10-23)

- One repository, `viktor-ferenczi/impostors`: server plugin (Magnetar), client plugin
  (Pulsar), shared code, and the Impostors mod.
- The mod draws the impostors on vanilla clients connected to a server running the plugin.
  Players need no plugin.
- The client plugin is the server part (for lobby hosts and offline games) plus the mod's
  renderer.
- Cluster compatible.

## What the client can draw

A mod can't show a texture made at runtime: `MyRenderProxy` isn't on the mod whitelist and
transparent materials come from `.sbc` files. What it can do is add billboards every frame
(`MyTransparentGeometry.AddBillboardOriented`, the whole `VRage.Game` namespace is
whitelisted). So an impostor is not a sprite but a small model made of flat colored quads.

Billboard facts this design depends on (decompiled game 1.210):

- No distance culling, and the projection has no far plane: billboards at 50 km draw fine,
  vertices are made camera relative in double precision.
- 32768 billboards per frame for everything, vanilla effects included. The overflow is
  dropped silently, the last blend types first (see [Billboard limit](#billboard-limit-se-664)).
  The renderer caps itself at `MaxQuadsPerFrame` quads (16000 by default), which the server
  sends with its Welcome. It counts only the impostors in the camera's frustum, nearest
  first, and skips one that doesn't fit while farther ones still may.
- Billboards are depth tested against the scene but never write depth. The `Standard` blend
  is weighted blended OIT, so overlapping opaque quads mix and look see-through. The `LDR`
  blend draws in submission order with premultiplied alpha after tone mapping. Impostors use
  `LDR`, cull back faces and draw their quads back to front.
- Billboards are unlit, so the renderer shades each face from the sun direction
  (`MyVisualScriptLogicProvider.GetSunDirection`, which points towards the sun).
- `Draw` of a mod's session component runs once per update (60 Hz) and its billboards live
  for that one frame.

## Impostor shapes

The server rasterizes the content into a cell grid in the impostor's own frame: each cell is
filled or empty and has an average color. The surface faces of the filled cells are merged
greedily into rectangles of one color, so a flat painted side is a single quad.

- Levels of detail: 2, 4, 8, 16, 32 and 64 cells along the longest axis, fewer for grids
  where that would split blocks. A level stops being built once its cells are as small as
  the content's own resolution (a block for grids). Levels 2 and 4 are for the many far
  ones that cover a few pixels, where only the color is left to see: an asteroid ring
  shows hundreds at once.
- A grid's levels under 16 are downsampled from 16, a cell filled when 2 of its 8
  children are, with their average color: built from the blocks again, level 8 lost most
  of the detail (a 18k block ship: 104 quads against 246 at 16).
- Faces of sloped armor (Slope, Corner and Round blocks) are shaded with the smoothed
  surface normal, like asteroids: as cube faces their stair steps alternate between lit
  and dark, and the whole grid came out brighter or darker than the real one depending on
  the angle.
- A grid's cells are a whole number of its main grid's blocks and sit on that grid's block
  lattice, so its finest level is block for block, and the cells sticking out are shared by
  both sides. A block fills the cells whose centers it contains, or the one holding its own
  center (smaller blocks of subgrids). Cells of other sizes cut blocks into uneven parts,
  which showed as stripes and a shape shifted and grown by up to a cell.
- The server sends one level per impostor and player, picked from the on-screen size on a
  1080p screen with a 70° field of view: about one cell per 2.5 pixels. Grids keep level
  16 down to half that size, as their coarser levels lose the shape fast. A zoomed view
  gets finer levels inside it, see "Zoomed views" below.
- The levels of a player's impostors together stay within `MaxQuadsPerFrame`, counting half
  of each shape's quads as facing the camera. While the player zooms only those in the
  zoomed view count; otherwise the server doesn't know the view direction and counts all. When the picks are over it, the server picks
  them all again as if the screen were smaller, so the near ones lose detail instead of the
  client dropping the far ones. Above the Octahedron asteroid ring (SE-666) the few
  asteroids just outside the replication range took level 32, about 3600 quads each, and
  the client drew 4 of 137 impostors.
- Encoding (`Protocol.cs`): a 19 byte header (level, cell size, origin) and 10 bytes per quad
  (face, min cell, width, height, RGB565 color, normal).
- Each quad carries the normal it is shaded with, octahedral with 6 bits per coordinate
  (within 4°). Grids use the face's own normal. Curved surfaces can use a smoothed one, the
  gradient of the occupancy blurred by a Gaussian (σ = 1.5 cells): seen along an axis, a
  stair-stepped surface shows only faces of one direction and would shade as one flat color.
  Quads merge only when color and normal both match.
- Only faces towards empty space reachable from outside are emitted (a flood fill from the
  volume's border): enclosed pockets, like caves or the rooms of a hollow grid, are never
  seen, and drawn they would cost quads and break the back to front order. Smoothed surfaces
  merge into quads of at most 4 cells for the same reason: a large quad deep in a crater
  could sort in front of the rock before it.
- Shapes can bake ambient occlusion into their colors: 32 rays over the hemisphere around each
  surface cell's smoothed normal, reaching a fifth of the shape's size, darken the cell by the
  cosine weighted share they find blocked. It stands in for the shadows and baked occlusion that
  darken pits and crevices in the game's own rendering, view independently. On a 128 cell
  cratered sphere it darkens the craters by about 12% against the open surface and doubles the
  meshing time (230 ms instead of 115 ms, background thread).
- Normal precision trades speckle against quads. On a bumpy voxel sphere, 16° steps (the
  first version, 4 bits and a coarser estimate) left 19% of neighboring quads with a visible
  shade jump; 6 bits leave 0.3% at 50% more quads than that version, 8 bits none at 80% more.
- The version of a shape is a hash of the encoded shapes, so a rebuild that changes nothing
  sends nothing.

Colors: every block takes its paint color (`ColorMaskHSV` the way the color picker converts
it), since block models are painted over most of their surface. Windows are dark. For
asteroids, see the SE-383 section.

## Server

`ImpostorService` (in `Shared`) runs where the world is simulated: on the dedicated server,
or with the client plugin on a lobby host and in an offline game. The mod can't run it.

Every entity is real where the world is simulated, but the game draws only those within
its camera's far plane, which `MySector.LoadData` sets to the view distance (50 km at most)
offline and to the sync distance on a lobby host. The voxel clipmaps stop there too
(`MyRenderVoxelActor`). So the client plugin's service serves the local player as well
(SE-668): offline the far grids and asteroids (an asteroid ring 135 to 231 km away showed
nothing at all before), and on a lobby host everything beyond the sync distance, as the
players who joined get it.

- The local player is a viewer of its own. Its renderer and the service exchange the same
  messages, Hello and View included, as byte arrays in the same process, in the order they
  are sent, so the picking, the levels, the zoom and the shape store work the same without
  mod networking.
- The renderer is the mod's, as on a server (SE-670). The plugin loads the mod from its
  `ImpostorsMod` asset (`ImpostorsMod/Mod` of its sources, see `ImpostorsClient.xml`) when
  the world doesn't list it, the way Pulsar loads client mods: its scripts after those of
  the world's mods (`MyScriptManager.LoadData`) and its definitions with theirs
  (`MyDefinitionManager.LoadData`). It never goes into the world's mod list, so a save
  doesn't get it. The mod's renderer can't see the plugin, so the two talk through mod
  messages (`ImpostorRenderer.ToLocalServerMessage` and `FromLocalServerMessage`).
- The plugin's own copy of the renderer draws for the local player only when the mod's
  can't: Pulsar gave the plugin no asset folder, the mod's scripts didn't compile, or the
  world lists an older Impostors mod without those messages (told by the text of its
  `ImpostorRenderer.cs`). It talks to the service through delegates
  (`ImpostorService.LocalClient`, `ImpostorRenderer.ToLocalServer`).
- Its eye is the camera, not the controlled entity, and it gets no impostor for an entry
  whose AABB reaches into the far plane's sphere, where the game draws the real one. Its
  renderer shows every impostor it gets, as every entity exists locally.
- Its shapes don't wait for the shape credit, there is no network to protect.
- A player with only the mod gets nothing on a server, without the plugin nothing runs the
  service there and nothing answers the mod's Hello.

Every second it:

1. Scans the sources. Each source adds, updates and removes its entries (an id, a world
   matrix, a velocity, a world AABB and a cheap content signature).
2. For each player whose client said hello, picks the candidates: not within the
   replication cube (half side `SyncDistance - ReplicationMargin` around where the game
   replicates to the player, see "Moving views") unless held there (see "Streaming in"),
   within the view distance, at least
   `MinPixels` across on screen (3 by default, so nothing under 3x3 pixels; the size is
   that of the AABB's bounding sphere). The largest `MaxImpostorsPerPlayer` win.
3. Sends what changed for that player: removals, a state and a shape for new impostors or
   ones with a new version or level, and a state for moved ones. A level whose shape the
   client has already, from earlier in the session, costs a state only (see "Shape store").
   One in the client's disk cache costs 14 bytes of ShapeData and no credit (see "Disk
   cache").
   Shapes average `ShapeBytesPerSecond` per player: one goes out while the player has credit
   left, even if it is larger than that (an asteroid's finest level is about 150 KB), and the
   next ones wait until the following seconds have paid off the debt. New impostors don't
   wait, but while the player is in debt they get a level the client has or one of at most
   2 KB first. The states go first, then the shapes, small ones sharing a message up to 16 KB.
4. Starts background builds for the wanted entries that have no shapes yet, or whose content
   changed, at most two at a time. Each finished build logs an `Impostor build:` line with
   the snapshot time on the game thread and the wall clock time, thread CPU time and quads
   of each level; `tests/build_times.py` turns server logs into a table.

Rebuilds: an entry whose signature changed is rebuilt at most every `RebuildSeconds` (60).
Changes the signature misses, like paint, are caught by rebuilding entries in view every
`RefreshSeconds` (600). Only entries some player is looking at are ever built.

Shape cache (SE-648): a build first hashes what it builds from, after the snapshot and before
the rasterizing: every cube's position (to a sixteenth of a block, summed over the cubes, as
the blocks come in no fixed order), size, color and slope flag for a grid; the voxel content
and materials of every level read, the material colors and the occlusion setting for an
asteroid; the shape itself for a safe zone. The hash also covers `DiskShapeCache.Algorithm`,
a number to bump whenever a change to the building gives other shapes for the same content.

- Content with the same hash as the entry's last build keeps its shapes: a refresh of an
  unpainted grid costs the snapshot only.
- Otherwise the build looks in the disk cache, one file per entity id with its newest build,
  in a subfolder named after the first 2 hex digits of the id (`01/012baf749a4ba168.shapes`),
  in `Storage/Impostors/Cache` of the server's data folder (the game's user data folder for
  the client plugin). Versions before SE-700 used `ImpostorsCache`, which the first start moves
  there, or leaves with a log hint to delete it when the new folder already exists. Files of the
  older flat layout move into their subfolders on start. The same hash gives the cached shapes; a restart builds only what changed. The
  version is the hash of the shapes as before, so an unchanged impostor keeps its version
  across a restart, and the clients' disk caches (see "Disk cache") keep working.
- Every new build replaces the file of its entity. An entity gone from the scan for a minute
  that no longer exists in the world loses its file (not at once: grids leave the scan while
  a world unloads, before they are closed).
- Retention (SE-711) runs on a background thread of lowest priority, about 2 minutes after
  the cache opened and then every `CacheSweepMinutes` (60), give or take a quarter. Files
  unused for `CacheDays` (30) or built more than `CacheBuildDays` (90) ago go, then the least
  recently used ones while the folder is over `CacheMegabytes` (256). A sweep deletes at most
  200 files, 5 ms apart, each under the cache's lock on its own; with more to do the next
  one comes after a minute. A build that puts the cache over its cap brings the sweep
  forward instead of deleting files on the build thread. A file's write time is its build
  time; a read only notes the time in memory, and `uses.bin` keeps those times across
  restarts (saved after each sweep and every 10 minutes while they change). Without an
  entry there, a file counts as unused since its build. Measured offline with
  `tests/cache_retention.py` on 50000 files: an idle sweep takes about 1 ms of CPU, one that
  deletes 200 files about 7 ms of CPU (and a second of pauses) with no file reads, saving
  `uses.bin` about 10 ms and 0.8 MB of writes. Opening the cache scans the files as before,
  about 100 ms for 50000 files with a warm file system cache.
- While builds come from the cache the service starts the next ones every 10 ticks instead of
  every second.
- Once a minute the log counts the shapes built, taken from the cache and found unchanged
  (`Impostor shapes in the last minute: ...`). Only real builds write `Impostor build:` lines.

The replication margin makes the hand-off seamless: the client has the impostor while the
real grid is still replicated (and hides it), so there is no gap when the grid leaves the
replication range.

Streaming in (SE-672): arriving fast, a grid crosses the 500 m margin in a fraction of a
second (0.05 s at 10 km/s), long before the server has streamed it to the client and the
client has built its render cells. So an impostor the client already has is held inside the
replication cube instead of removed:

- The server keeps it at the level it was sent at, the finest one from the approach, and
  sends no new shapes for it, only states when it moves. Held impostors sort right after the
  safe zones, so `MaxImpostorsPerPlayer` doesn't drop them.
- The client tells the server with a `Ready` message when the real entity draws itself: in
  the scene with render objects (`IMyEntity.Render.RenderObjectIDs`; a grid has them once its
  render cells are built, a safe zone once its model is) for a second, the longest the game
  fades new entities in. It checks every 10 ticks, and reports an entity that stops drawing
  too. The client hides the impostor from then on.
- An asteroid has its render object (the clipmap) at once, but draws nothing until the
  clipmap's first meshing pass is done, then fades its cells in over a second. The mod can't
  see that pass, so it keeps an asteroid's impostor for 5 seconds. The client plugin
  subscribes to the clipmap's `Loaded` event and sends the asteroid's id to the renderer as
  a mod message when the pass is done; the impostor then hides within 10 ticks. With a
  subscriber the game shows that pass's cells at once instead of fading them in, as on its
  load screen.
- The server removes a held impostor once the client reported it ready, or after 60 s.

A grid group's impostor waits for the group's largest grid only; its subgrids stream on
their own and may show a moment later.

Zoomed views (SE-659): a rifle's iron sight, a spectator or camera zoom or a telescope mod
narrows the field of view, and the impostors in view show larger than the default estimate.
The mod reads the camera's `FovWithZoom` and viewport size. While the view shows things at
least 1.5 times larger than a 1080p screen at 70° does, it sends a `View` message: when the
zoom starts, when it changes by 5%, when the view turns by an eighth of its field of view,
and once more when the zoom ends. It checks every 10 ticks, so that is six messages per
second at most. For that player the server then:

- multiplies the on-screen size of the impostors inside the view (the angle off its
  direction, less the impostor's angular radius, within the half diagonal) by the zoom,
  32 times at most. They sort first, so they get the shape credit first and their finer
  shapes go out ahead of the rest.
- picks the player's impostors again within 10 ticks instead of at the next full second,
  six times per second at most. The credit is paid by the time since the last pick, so the
  extra picks don't raise the bandwidth.
- takes a zoomed level only while its shape has at most `MaxQuadsPerFrame` quads, about
  half of them facing the camera, so one zoomed impostor can't take the client's whole
  budget; with the others they fit the budget together (see "Billboard limit").
- puts the normal level back when the zoom ends. The client keeps the zoomed one in its
  shape store, so looking through the zoom again costs a state only.

## Moving views (SE-671)

Turning a zoomed view over an asteroid ring, or flying a spectator camera fast, left gaps:
impostors that should be on screen and weren't drawn. `tests/view_gaps.py` measures them.
What the server and the client do about it:

- Shape store: the client keeps every shape the server sent, by impostor id, level and
  version, up to 8 MB of encoded shapes (`ShapeStore`, about 40 MB once decoded). The server
  keeps the same store for each player, without the shapes: both add the shapes in the order
  of the ShapeData messages and drop the earliest ones first when over the limit, so they
  agree on what the client has without telling each other. A level the client has costs a
  state only: an impostor removed when it left a zoomed view and shown again, or the finer
  level of a zoom when the view zooms in again. That replaced keeping a zoomed level for 30 s
  after the zoom, which over a ring left the near rocks with so many quads that a wide view
  after a zoom dropped most of the others.
- Debt: a turning zoom brings dozens of new impostors at once. Sent at their finest levels,
  that was megabytes of debt and no finer level for any other impostor for minutes; now new
  ones start small while the player is in debt, and the finer level follows like any other.
- Waiting: when a finer level waits for credit, the server sends a state naming it once. The
  client keeps drawing the shape it has; `stats.txt` counts these (`waiting`).
- Quad budget: the levels fit `MaxQuadsPerFrame` together (SE-666, see "Billboard limit").
  Over a ring two or three asteroids at zoomed level 64 took the whole budget before. On the
  client an impostor that doesn't fit is drawn at the finest coarser level it has in its
  shape store that fits, or left out without stopping the ones behind it.
- The zoomed cone the server uses is a quarter of the field of view wider than the view,
  which covers the eighth of a turn before the client reports the view again, plus what a
  turning view turns in half a second (up to a field of view), estimated from the last two
  `View` messages.
- Eye: the server picks from where the game replicates to the player (`MyClientState`): the
  controlled entity, or the spectator camera of a player with creative rights. A spectator
  camera also goes into the `View` message, with its position, every 100 m it moves, and
  the server sizes the impostors from there when the player may spectate (`EnableSpectator`
  or spectator rights). The replication cube stays where the game replicates.

## Disk cache (SE-647)

The client keeps the shapes it got on disk, so rejoining a server, or meeting the same far
grids again after the shape store let them go, costs no shapes for unchanged impostors.
Versions are content hashes of all levels, so (id, version, level) names a shape for good.

- Storage: one file per world, by a hash of the world's name, in the mod's local storage
  (`cache-<hash>.bin`), or in the global storage with the `Impostors.` prefix for the client
  plugin's copy. `cache-index.txt` lists the files with their size and the day they were
  used last. The local player's renderer copy has no cache, its shapes never cross a network.
- The client loads the world's file while the game loads, and after every Welcome of a new
  server instance sends an `Inventory`: id, version and a mask of the levels it has, about
  13 bytes per impostor, in messages up to 16 KB. Its Hello says that one will follow, and a
  server that starts over with the player holds its impostors back until the last part
  arrived, 10 s at most; otherwise the first second's shapes would go out before the server
  knew the client has them. The server keeps the inventory per player and adds every shape
  it sends from then on, as the client caches all of them.
- A shape the client's store lacks but its disk cache has goes into ShapeData as the id, the
  version, a 1 and the level, instead of the shape. The client decodes it from the cache and
  adds it to the shape store with the shape's real size, so both copies of the store stay
  in step.
- The server counts on what the client announced, so nothing leaves the world's file during
  a session. Its retention runs when the file is loaded and when it is saved at the end of
  the session: an impostor keeps only the version it used last, entries unused for 30 days
  or received more than 90 days ago go, and the file keeps under 64 MB with the least
  recently used entries dropped first. A background sweep (SE-711), about 2 minutes after
  the load and then every hour, deletes the files of other worlds unused for 30 days, then
  those used least recently while all of them are over 64 MB together, at most 4 per sweep.
  The client plugin takes these limits from its config (see
  [Configuration](Configuration.md)), the mod keeps the defaults. A session that grew the
  cache over twice the cap prunes it and starts over with the server, as does a client
  asked for a shape its cache doesn't have.
- Saving: on a background thread 30 s after the last new shape, every 10 minutes while they
  keep coming, and at the end of the session. A game that quits without unloading the
  session, or crashes, loses only the last seconds. The file has a checksum; a damaged one
  is dropped. Format 2 (SE-711) adds the day an entry was received; a format 1 file loads
  with the day it was used last in its place.
- Compatibility: a server without the cache ignores the Inventory, an older client never
  sends one, so the cache needs no new protocol version.

`tests/cache.py` checks it in game: join, leave, add entries retention must drop, join
again. The rejoin must get no shapes for the unchanged impostors. Both sessions also check
what the sweep deletes of the other worlds' files.

A third person or cockpit camera is close enough to the controlled entity for impostors
kilometers away.

Motion: a state is resent when the impostor moved more than about a pixel compared to where
the client extrapolates it, turned by more than about 1.6° or changed its velocity. The
client extrapolates with the velocity for up to 10 seconds.

## Cache gains and consistency (SE-662)

Measured on 2026-10-25 on Linux, after the client cache (SE-647) and the server cache (SE-648)
were merged. SE-661's runs never happened, so the baseline is a cold start: both caches
empty, which is what a server without the caches does on every start.

Single server, `tests/cache_gains.py` on factory@ai (AMD Ryzen AI MAX+ 395): the scenario
world (six grids of 240 to 7544 blocks, two safe zones, a 512 m asteroid with occlusion), one
client with only the mod, three joins per state, medians. CPU is the build thread's CPU time;
the snapshot runs on the game thread before it.

| State | Server work | CPU ms | Game thread ms | Client got | First sight |
|---|---|---:|---:|---|---:|
| Cold (no caches) | 9 built | 112 | 8.6 | 9 shapes, 36 KB | 2.2 s |
| Server cache warm | 9 from the cache | 18 | 9.2 | 9 shapes, 36 KB | 2.2 s |
| Both caches warm | 9 from the cache | 17 | 9.7 | 9 from its cache, 0 KB | 2.2 s |
| Client cache warm | 9 built | 107 | 8.9 | 9 from its cache, 0 KB | 2.2 s |

Per kind, cold build against cache hit: the six grids 18.8 against 2.9 ms, the asteroid 92 against
13.7 ms, the two zones 1.3 against 1.7 ms. A hit still hashes its input, which for an asteroid
means reading every voxel level it is built from, about 15 % of the build. Most of the
asteroid's build is its finest level (L64, 15454 quads, 79 ms). Cache hits don't touch the
game thread's snapshot.

- First sight is the time from the session being ready until every far impostor has a shape.
  It is the same in all states: the hello every 30 s, the Welcome and the first view pick
  dominate it, a build of this world takes 0.1 s. The first join of every run took 66 s,
  also when its shapes all came from the cache: it is the client's first session after the
  rig started, not the builds. The table leaves it out of the cold medians.
- The client cache saves all the shape bytes of the impostors it saw before, whatever the
  server's cache. Builds are deterministic, so a server that lost its cache still builds the
  versions the clients have.
- The caches pay most on servers with many or large grids and asteroids: a server restart with
  a warm cache skips every unchanged build, about 85 % of the CPU here.

Cluster, `tests/cluster_scenario.py` on factory@polaris (AMD Ryzen 9 5950X, cluster 0.1.21):
WA, two nodes, two stations of 3760 blocks and a 512 m asteroid. One run with
`IMP_CACHE_MB=0`, three with the caches.

| Event | Caches off | Caches on |
|---|---|---|
| A station's partition loads on a node (node change) | built, 4.2 ms CPU, 0.13 s wall | from the WA, 1.4 ms CPU, 1.5 s wall |
| WA restart, its own asteroid | built, 120 ms CPU | from its cache, 23 ms CPU |
| WA restart, offline stations | shown from the saved index | the same |
| First sight for a joining client | 0.2 s | 0.2 s |

A cache hit on a node saves CPU but not time: `cache-get` asks for one level per round trip,
six in a row through the WA's game thread, about 0.25 s each. For a grid this small building
it is faster; it pays for large grids and asteroids (and see SE-704 for those).

Before SE-662 the WA opened its store only once its world was ready, so its own asteroid build
a few seconds earlier was never stored, and after a restart it built it again. It opens the
store on first use now. Each node still builds the asteroid itself: a shape level over the
SDK's 64 KiB payload can't cross the cluster, see SE-704.

Consistency, `tests/cache_consistency.py` on factory@ai, two runs of four rounds: every change
reached both clients within its limit, in 2 to 11 s for block changes (limit RebuildSeconds
plus 20 s) and 9 to 31 s for repaints, which show at the next refresh (RefreshSeconds 30 in
the rig). A layer built and ground off again gave the station its first input and version
back. After a client was killed while saving its cache and after the server was killed
during rebuilds, the clients, the server's entries and the cache files agreed on every
version. A start with the cache wiped built the same inputs and versions the cache had
served. No duplicated impostors, no leftover temporary files, no cache errors in the logs.
One race was found and fixed: the client's save at the end of a session could write the cache
file while the background save was still writing it; both now take the same lock.

## Asteroid pack (SE-701)

A world's asteroids rarely change, yet every server builds them and sends them to every new
player: above the Octahedron asteroid ring that is hundreds of shapes per player. An asteroid
pack is a mod with the impostors of all of a world's asteroids, at every level, built once
(see [Asteroid pack](AsteroidPack.md) for making one).

- The pack is one file, `ImpostorsAsteroidPack.bin` (`AsteroidPack` in the mod's `Common`):
  per asteroid its entity id, the hash of the content it was built from (the shape cache's
  input hash), the version and the shapes. The index is read at load; shapes are read from
  the file when needed.
- Server: `AsteroidPackCache` sits in front of the shape cache. A build whose content hashes
  the same as the pack's entry takes the pack's shapes. A mined asteroid hashes differently
  and is built and cached as before; reset to its original voxels, it hits the pack again.
  Nothing from the pack goes into the disk cache.
- Known hashes: reading the voxels for the hash is about 40% of a build, so `VoxelSource`
  keeps the content hash of every asteroid nobody edited since it was hashed and builds those
  from the hash alone, from the pack or the shape cache. Any storage change (mining, a voxel
  hand, a voxel reset, the game's voxel revert) drops the asteroid's hash: its next build
  reads and hashes the voxels again. After a reset they hash as they did before anybody
  mined, which is the pack entry's hash, so the pack's shapes come back. A finished build
  records the hash of what it read, unless the asteroid was edited during the build.
- So each asteroid is read once, at its first build, and again only after it changed. The
  game's own `ContentChanged` can't stand in for that: an asteroid saved mined comes back
  with it false after a restart.
- The known hashes are saved with the world, as `Storage/Impostors.asteroids` in the save's
  folder (a save deletes the other files at the top of that folder). They are written while
  the world saves, so they describe the voxels saved with them, and not for the checkpoint a
  joining client gets. They are used only with that save (the game time of its
  `Sandbox.sbc`): an older backup of the world, or a world that crashed before its next
  save, may hold other voxels. They also carry a hash of what the shapes depend on besides
  the voxels (`VoxelSource.Settings`: the occlusion setting and the material colors); with
  other settings they are not used, and every asteroid is read again.
- Client: the mod adds the pack's asteroids (id, version, level mask) to its Inventory (see
  "Disk cache"), so the server sends the 14 byte reference instead of a shape, and the client
  reads the shape from the pack. A client without the pack gets the shapes as before. A
  server without the pack still saves the bytes as long as it builds the same shapes, as the
  version is a hash of the shapes.
- No protocol change: a pack's asteroid looks to the server like a shape in the client's disk
  cache.
- A pack written with `AsteroidPackMaxLevel` below 64 has entries without their finest levels.
  Clients use what is there, the server builds those asteroids itself.

## Protocol

Mod networking on channel 47213, reliable messages up to 16 KB:

| Message | Direction | Content |
|---|---|---|
| Hello | client to server | protocol version, whether the client has nothing from the server yet, whether an Inventory follows; at load and every 30 s |
| Welcome | server to client | protocol version, server instance id, `MaxQuadsPerFrame` |
| States | server to client | impostor states: id, kind (grid, voxel or safe zone), version, level, radius, position, orientation, velocity, safe zone tint (74 bytes each); a level from the shape store switches the impostor to it |
| Remove | server to client | impostor ids |
| ShapeData | server to client | count, then id, version and shape per impostor; or a 1 and the level for a shape in the client's disk cache |
| View | client to server | field of view with zoom, viewport width and height, view direction, spectator camera and its position; while zoomed or spectating |
| Ready | client to server | ids of impostors whose real entity started drawing, then those whose entity stopped |
| Inventory | client to server | whether more follow, count, then id, version and level mask per impostor in the client's disk cache or the world's asteroid pack; after each Welcome of a new server instance |

The repeated hello means the server needs no state about clients across restarts, and a
cluster node a player was moved to learns about the client within 30 s. A different server
instance id makes the client drop everything it has, shape store included. A hello from a
client without a Welcome yet makes the server start over with that player, so a quick rejoin
can't leave the server's copy of the store out of step.

## Client

`ImpostorRenderer` is a session component of the mod. It runs on multiplayer clients only.
Each frame it:

- skips impostors whose real entity draws on the client (see "Streaming in" above): within
  the replication range the real grid draws itself, but while it streams in the impostor
  stands in for it,
- keeps the nearest impostors within the quad budget, counting the quads each one really
  draws: those on the faces towards the camera, all of a safe zone's,
- draws them far to near, each with its back faces culled and its quads sorted back to front
  for the current view direction (re-sorted when the view turns by about 3°),
- shades each quad with its normal: `0.08 + 1.6 × max(0, n·sun)`, fitted to
  screenshots of real grids and asteroids from 8 directions. Under 27 pixels in radius on a
  1080p screen it brightens grids (up to 1.35 times under 13 pixels) and dims asteroids (down
  to 0.82), where the real ones carry more or less light than their impostors.

The client plugin compiles the same files. That copy has no mod context; on a multiplayer
client it stands down when a session mod contains the renderer, so it only draws on servers
whose world lacks the mod. In an offline game or on a lobby host the mod's copy draws for
the local player, and the plugin's only when the mod's can't (see "Server"). The plugin
updates its copy itself: the game keeps the session components to update in a set ordered
by priority and type name (`MySession.ComponentComparer`), where the plugin's copy and the
mod's compare equal, so with the mod loaded the game updates only the mod's copy, which it
registers first. The mod the plugin loads keeps its files in the local storage folder
`local_Impostors_Impostors`. The plugin copy's debug files are `Impostors.debug`,
`Impostors.stats.txt` and `Impostors.mode` in the global storage (`<appdata>/Storage`), since
Pulsar compiles the plugin under a random assembly name, which the local storage folder
would take.

## Billboard limit (SE-664)

The impostors share the 32768 billboards of a frame with the game's effects and every other
mod. When there are more, impostor quads should be the ones to go: players notice a missing
thruster flame, tracer or HUD marker far more than a coarser far view.

How the game cuts (`MyBillboardRenderer.PrepareList`, decompiled 1.210): it groups the frame's
billboards by blend type in the order `Standard`, `AdditiveBottom`, `AdditiveTop`, `LDR`,
`PostPP` and draws only the first 32768. `LDR` and `PostPP` keep the order they were added in.
So effects of the first three blend types always win over the impostors, which are `LDR`, but
`PostPP` billboards (the game's hints, some mods' HUD markers) and the `LDR` billboards a mod
adds after the Impostors mod's `Draw` are cut first. Mods draw in the order their session
components were registered, not by their `Priority`.

What each client gets:

- The mod and the client plugin draw at most the budget, `MaxQuadsPerFrame` from the server.
  They count the quads that really face the camera and keep the nearest impostors that fit,
  while still drawing far to near. A vanilla client with the mod only relies on this: other
  billboards are safe up to `32768 - MaxQuadsPerFrame` of them. The mod can't see how many
  the frame has (`MyRenderProxy` isn't whitelisted), so it can't do more.
- The client plugin patches `PrepareList`. When a frame is over the limit, it takes as many
  impostor quads out of the `LDR` bucket as the frame is over, the first ones (the farthest,
  as they are drawn far to near), so every other billboard moves into the drawn range. It
  knows impostor quads by their material, `ImpostorSquare`: a copy of the game's `Square`
  that the mod defines in `Data/TransparentMaterials.sbc` and the plugin adds itself when the
  world has no mod.
- The client plugin also lowers the budget to the room the last frame left:
  `32768 - other billboards - 2048`, never above `MaxQuadsPerFrame`. It sends that to the
  renderer as a mod message every tick, which reaches the mod's copy too. In a steady frame
  the impostors then fit, and the patch only has to step in when effects come in a burst.
  The renderer goes back to `MaxQuadsPerFrame` when the messages stop.
- Once a minute the client plugin logs the frames' billboard totals while impostors are drawn:
  the peak count, the peak of other billboards, impostor quads dropped and the time its patch
  took (`Billboards in the last minute: ...`).

## Grids (SE-382)

`GridSource`: one impostor per physical grid group, so docked ships and subgrids show as one.

- The frame is the group's largest grid (ties to the lowest id), the impostor id is its
  entity id. Subgrids are frozen at the pose of the last build.
- Groups below `MinBlocks` blocks (8) are left out, so are projections and previews.
- Signature: the members' ids, block counts and min/max cells.
- A build snapshots every cube of every block on the game thread (position in the main
  grid's frame, size, color) and rasterizes the levels on a background thread.

Concealed grids (CometWorks Concealment) stay in the scene and replicated, so they get
impostors like any other grid; they don't change while concealed.

## Asteroids (SE-383)

`VoxelSource`: one impostor per asteroid (`MyVoxelMap`), never planets, which the game draws
from afar anyway.

- Only the asteroids a client doesn't have by itself. Procedural asteroids nobody edited are
  generated by every client within its view distance, so they are skipped. The rule is the one
  of `MyVoxelReplicable.ShouldReplicate`: `Save || ContentChanged || BeforeContentChanged`.
- Saved and edited asteroids replicate farther than grids: `MyVoxelReplicable` inflates their
  AABB to `ViewDistance/√3` per axis around the player (8.66 km at the default 15 km). The
  server uses that range, minus the margin, instead of the sync distance for them.
- Shape: the storage is read at the coarsest voxel level of detail that still has the wanted
  number of voxels along the longest axis (`IMyStorage.ReadRange` with content and material),
  one cell per voxel above the iso level. Colors come from the material's mined ore through a
  small table (stone, iron, ice and the rest), on a background thread.
- Quads are shaded with smoothed normals: an asteroid seen along an axis would otherwise be
  one flat color.
- The coarsest level is pooled from voxels two levels finer: a cell is filled when at least
  half of its voxels are. The game's own coarse voxels erode thin parts, and level 8 looked
  half as large as level 16. That level also gets at least 6 cells across a rock that is
  small for its voxel storage. Levels 4 and 2 are downsampled from 8, a cell filled when
  half of its 8 children are (or any, if the rock would vanish). Level 16 costs about 15
  times the quads of level 8 (513 instead of 34 for a 512 m one), so far rocks stay coarse.
- The mod dims asteroid impostors that are small on screen: from 27 pixels in radius on a
  1080p screen down to 0.82 times as bright under 13 pixels, where the real asteroids look
  darker than their lit impostors.
- With `AsteroidOcclusion` on, their colors get ambient occlusion baked in, so pits and
  crevices darken like the real ones. It's on by default. In 8-direction comparisons it made
  the impostors closer to the real asteroids at some distances and farther at others, for twice
  the build time.
- Signature: a counter of the storage's `RangeChanged` events, so mining or a blast rebuilds it
  (at most every `RebuildSeconds`).
- Frame: the voxel map's world matrix; asteroids don't move.

Quads of a 512 m asteroid without occlusion: 6, 38, 192, 513, 3635 and 14858 for levels 2
to 64, 10 bytes each, about 3% more with occlusion. Asteroids are much more detailed than box-like grids, so they take most of the quad
budget when large on screen.

## Safe zones (SE-660)

`SafeZoneSource`: one impostor per enabled safe zone, so players see from afar where the
protected areas are. Disabled zones protect nothing and the game draws them white with
another texture, so they are left out. The zone builder's `IsVisible` flag is ignored: the
game never reads it and draws those zones anyway.

- The shape is not a cell grid but the zone itself (`ZoneShape` in `Protocol.cs`, 17 bytes):
  a leading 0 where other shapes have their level, sphere or box, the half extents and the
  zone's color. The frame is the zone's world matrix.
- The mod draws it as a translucent shell of flat quads in the zone's color: one per side of
  a box; around a sphere 8 by 4 (32 quads) under 6 pixels in radius on screen, 16 by 8 (128)
  under 25 and 32 by 16 (512) above, which keeps the outline within about half a pixel of a
  circle. Seen face on a quad covers 2% of the light behind it;
  where the line of sight grazes it, up to 82% and paler, so the zone shows as a thin rim
  like the game's shield (fitted to comparison sheets, see [Safe zones](SafeZones.md)). The
  far half is drawn before the impostors inside the zone, the near half after them. Both
  halves are drawn, so a zone counts with all its quads against `MaxQuadsPerFrame`, where
  grids and asteroids count with those on their faces towards the camera.
- Zones go first among a player's `MaxImpostorsPerPlayer`. They are always built, even when
  nobody is shown them, because the impostors inside them need them.

The game draws a zone's surface with a distorting shield effect, which blurs and tints what
is inside. Impostors are billboards drawn after tone mapping, so they show undistorted on top
of it. A grid or asteroid impostor whose AABB center is inside an enabled zone therefore gets
one level of detail lower than it would outside, and its state carries the zone's color
(`Tint`, 4 bytes, alpha 0 outside of zones). The mod blends its quad colors 35% towards the
zone's surface color, the zone's color 60% of the way to white (`ZoneTint`, `ZoneTintPale` in
the renderer), fitted to comparison sheets. The level picked
before the drop is kept for the hysteresis, so the drop doesn't repeat every second.

On a cluster, admin zones are on every node (the cluster plugin's `AdminSafeZones`), so each
node shows them itself, like asteroids. The zone of a safe zone block travels with the
block's grid, so nodes report it to the World Authority like a grid entry, located by the
partition of the block's grid. Other nodes show it through `RemoteSource` and use it for the
impostors inside it.

## Cluster

Grids of other nodes are not on the local node in any form. Partitions that have been quiet
and far from players for a while are hosted by no node at all, which is where most far grids
are. So far view needs their records from elsewhere. `ServerPlugin/Cluster` does it with the
Magnetar Plugin SDK (`PluginSdk.Clustering`); the client side is unchanged.

- Every 5 s each node reports its grid entries to the World Authority (topic `report`): records
  (id, version, frame, velocity, AABB) that changed noticeably since the WA acknowledged the
  last report, and the ids that are gone. Gone means released when the entry's partition left
  the node (handover or offline), deleted otherwise. The shapes of new versions go first
  (`shape`, one message per level of detail).
- The WA merges the reports into the index. A released record stays, without an owner, until
  some node reports the grid again: that is how grids of offline partitions stay visible. Every
  5 s the WA broadcasts the changes to the nodes (`index`) as consecutive revisions.
- A node that missed a revision, or started after the last one, loads the whole index in pages
  (`snapshot`).
- On each node, `RemoteSource` turns the other nodes' records into entries. They skip the
  replication cube test, another node's grid is never replicated to this node's players. Their
  shapes come from the WA's cache by id and version (`shape-get`).
- Nodes build every local grid entry, not only the ones their own players see, since players on
  other nodes may need them.
- The reply to a report carries the WA's instance id. A new one (a WA restart) makes the nodes
  report everything again.
- The WA saves its index (`Storage/Impostors/Cache/index.bin` in its data folder, at most once a minute
  after a change) and loads it when its world is loaded, if the file is of the same world.
  The loaded records are offline until a node reports them, which keeps the grids of offline
  partitions visible across a WA restart. Reports carry each record's partition. A node whose
  entries are all built and which knows the WA's instance adds the partitions it has owned for
  30 s to its report, and the WA drops the loaded records of those partitions it didn't
  report: grids deleted while the WA was down.
- Asteroids are not reported: saved voxel maps are replicas on every node already, each node
  builds them itself.
- The WA's shape cache is the cluster's only one (SE-648). It keeps the shapes on disk like a
  single server's cache, and a node's build asks it (`cache-get`, by id and content hash)
  before building. Grid shapes reach it with the reports, which carry the content hash now;
  nodes send the shapes of asteroids and admin safe zones they built too, so the next node to
  build one finds it. A deleted grid leaves the cache, a released one stays. After a WA
  restart, `shape-get` reads shapes the WA no longer has in memory from the disk.
- Safe zones of blocks are reported like grids, admin safe zones are not (see "Safe zones").

The same shape as the cluster's `AntennaGraphService`, which learned that full resends don't
scale: deltas, a periodic cadence and rare full loads. Records are about 100 bytes, 500 go in
one message under the SDK's 64 KiB payload limit.

## Budgets

| Where | Budget |
|---|---|
| Server game thread | one scan per second; a build's snapshot copies every cube of the group |
| Server background | 2 builds at a time |
| Network per player | `ShapeBytesPerSecond` (32 KB/s) for shapes, about 74 bytes per moving impostor per second |
| Client | `MaxQuadsPerFrame` quads per frame (16000) |
| Client plugin, render thread | its `PrepareList` patch: 7 µs per frame, 25 µs while every 14th frame is over the limit by 1182 impostor quads, 0.7 ms at most (`tests/billboards.py`, about 33000 billboards, Ryzen 9800X3D) |

Typical shapes, measured on the test grids: see the test results in the ticket.

## Testing

`tests/local.py` checks the client plugin's service for the local player, offline and on a
lobby host with a joiner (`IMP_MODE=lobby`, over DirectTransport).

`tests/scenario.py` starts an isolated Magnetar server with the plugin and a probe plugin
(`tests/probe`), and a headless client without the Impostors plugin that gets the mod from
the world's mod list. Both take the mod from a local folder under a stand-in Workshop id. The
mod writes `stats.txt` into its local storage when a `debug` file is there, which is how the
script checks what the client has and draws. See the script's docstring for running it.
