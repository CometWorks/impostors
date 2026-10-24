# Impostors: design

Far view for Space Engineers 1 multiplayer. Players see nothing beyond the sync distance
(3 km by default): the server doesn't replicate grids that far, and edited asteroids only to
about `ViewDistance/√3`. Impostors fill that gap with coarse models of far grids and asteroids,
out to the view distance.

Tickets: SE-381 (shared design), SE-382 (grids), SE-383 (asteroids) in Vikunja. This document
replaces the 2026-10-15 draft (`plans/se1-0199-far-view/PLAN.md`, attached to SE-381) where
the two disagree.

## Decisions (Viktor, 2026-10-23)

- One repository, `viktor-ferenczi/impostors`: server plugin (Magnetar), client plugin
  (Pulsar), shared code, and the Impostors mod.
- The mod draws the impostors on vanilla clients connected to a server running the plugin.
  Players need no plugin.
- The client plugin is the server part (for lobby hosts) plus the mod's renderer.
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
  dropped silently. The renderer caps itself at 12000 quads.
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

- Levels of detail: 8, 16, 32 and 64 cells along the longest axis, fewer for grids where
  that would split blocks. Grids start at 16: their level 8 saved little (a 18k block
  ship: 104 instead of 246 quads) and lost most of the detail.
- Faces of sloped armor (Slope, Corner and Round blocks) are shaded with the smoothed
  surface normal, like asteroids: as cube faces their stair steps alternate between lit
  and dark, and the whole grid came out brighter or darker than the real one depending on
  the angle. A level stops being built
  once its cells are as small as the content's own resolution (a block for grids).
- A grid's cells are a whole number of its main grid's blocks and sit on that grid's block
  lattice, so its finest level is block for block, and the cells sticking out are shared by
  both sides. A block fills the cells whose centers it contains, or the one holding its own
  center (smaller blocks of subgrids). Cells of other sizes cut blocks into uneven parts,
  which showed as stripes and a shape shifted and grown by up to a cell.
- The server sends one level per impostor and player, picked from the on-screen size on a
  1080p screen with a 70° field of view: about one cell per 2.5 pixels.
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

`ImpostorService` (in `Shared`) runs where the world is simulated and has remote players:
on the dedicated server, or on a lobby host. Single player has nothing to do, every entity
is real there.

Every second it:

1. Scans the sources. Each source adds, updates and removes its entries (an id, a world
   matrix, a velocity, a world AABB and a cheap content signature).
2. For each player whose client said hello, picks the candidates: not within the
   replication cube (half side `SyncDistance - ReplicationMargin` around the player's
   controlled entity), within the view distance, at least `MinPixels` across on screen (3
   by default, so nothing under 3x3 pixels; the size is that of the AABB's bounding
   sphere). The largest `MaxImpostorsPerPlayer` win.
3. Sends what changed for that player: removals, a state and a shape for new impostors or
   ones with a new version or level, and a state for moved ones. Shapes average
   `ShapeBytesPerSecond` per player: one goes out while the player has credit left, even
   if it is larger than that (an asteroid's finest level is about 150 KB), and the next
   ones wait until the following seconds have paid off the debt. New impostors don't wait.
4. Starts background builds for the wanted entries that have no shapes yet, or whose content
   changed, at most two at a time.

Rebuilds: an entry whose signature changed is rebuilt at most every `RebuildSeconds` (60).
Changes the signature misses, like paint, are caught by rebuilding entries in view every
`RefreshSeconds` (600). Only entries some player is looking at are ever built.

The replication margin makes the hand-off seamless: the client has the impostor while the
real grid is still replicated (and hides it), so there is no gap when the grid leaves the
replication range.

Motion: a state is resent when the impostor moved more than about a pixel compared to where
the client extrapolates it, turned by more than about 1.6° or changed its velocity. The
client extrapolates with the velocity for up to 10 seconds.

## Protocol

Mod networking on channel 47213, reliable messages up to 16 KB:

| Message | Direction | Content |
|---|---|---|
| Hello | client to server | protocol version; at load and every 30 s |
| Welcome | server to client | protocol version, server instance id |
| States | server to client | impostor states: id, kind, version, level, radius, position, orientation, velocity (70 bytes each) |
| Remove | server to client | impostor ids |
| ShapeData | server to client | id, version, shape |

The repeated hello means the server needs no state about clients across restarts, and a
cluster node a player was moved to learns about the client within 30 s. A different server
instance id makes the client drop everything it has.

## Client

`ImpostorRenderer` is a session component of the mod. It runs on multiplayer clients only.
Each frame it:

- skips impostors whose real entity exists on the client (`EntityExists` by id): within the
  replication range the real grid draws itself,
- keeps the nearest impostors within the 12000 quad budget,
- draws them far to near, each with its back faces culled and its quads sorted back to front
  for the current view direction (re-sorted when the view turns by about 3°),
- shades each quad with its normal: `0.08 + 1.6 × max(0, n·sun)`, fitted to
  screenshots of real grids and asteroids from 8 directions.

The client plugin compiles the same files. That copy has no mod context; it stands down when
a session mod contains the renderer, so it only draws on servers whose world lacks the mod.

## Grids (SE-382)

`GridSource`: one impostor per physical grid group, so docked ships and subgrids show as one.

- The frame is the group's largest grid (ties to the lowest id), the impostor id is its
  entity id. Subgrids are frozen at the pose of the last build.
- Groups below `MinBlocks` blocks (8) are left out, so are projections and previews.
- Signature: the members' ids, block counts and min/max cells.
- A build snapshots every cube of every block on the game thread (position in the main
  grid's frame, size, color) and rasterizes the levels on a background thread. A block larger
  than a cell fills every cell it covers.

Concealed grids (CometWorks Concealment) stay in the scene and replicated, so they get
impostors like any other grid; they don't change while concealed.

## Asteroids (SE-383)

Not implemented yet; the plan:

- Which voxel maps: asteroids (`MyVoxelMap`), never planets (the game already draws them from
  afar). Only those a client doesn't have by itself: procedural asteroids that were never
  edited are generated by every client within its view distance. That is the same rule as
  `MyVoxelReplicable.ShouldReplicate`: `Save || ContentChanged || BeforeContentChanged`.
- Shape: read the storage at a coarse level of detail (`IMyStorage.ReadRange` with content and
  material), fill cells above the iso level, color by the voxel material's mined ore from a
  small table.
- Signature: the storage's change events (`RangeChanged`) mark the entry stale.
- Frame: the voxel map's world matrix, asteroids don't move.

## Cluster

Not implemented yet; the plan, from the cluster code (`se1/cluster`) and the Magnetar
Plugin SDK:

- Grids of other nodes are not on the local node in any form, so far view needs their
  records from elsewhere. Partitions that have been quiet and far from players for a while
  are hosted by no node at all, which is where most far grids are.
- The server plugin uses `PluginSdk.Clustering` (`PluginCluster.ForPlugin`): each node
  reports its entries (id, frame, velocity, AABB, version) to the World Authority every 5 s
  as deltas, the WA merges them and broadcasts deltas, a node that missed one loads the whole
  index. The same shape as the cluster's `AntennaGraphService`, which learned that full resends
  don't scale.
- Shapes are fetched by `(id, version)` and cached on the WA, under the SDK's 64 KiB payload
  limit.
- The WA keeps the last record of grids in offline partitions until a node reports them again.
- On each node, remote entries are one more source. They skip the replication cube test,
  since another node's grid is never replicated to this node's players.
- Asteroids: saved voxel maps are replicas on every node already, so each node can build them
  itself.

## Budgets

| Where | Budget |
|---|---|
| Server game thread | one scan per second; a build's snapshot copies every cube of the group |
| Server background | 2 builds at a time |
| Network per player | `ShapeBytesPerSecond` (32 KB/s) for shapes, about 70 bytes per moving impostor per second |
| Client | 12000 quads per frame |

Typical shapes, measured on the test grids: see the test results in the ticket.

## Testing

`tests/scenario.py` starts an isolated Magnetar server with the plugin and a probe plugin
(`tests/probe`), and a headless client without the Impostors plugin that gets the mod from
the world's mod list. Both take the mod from a local folder under a stand-in Workshop id. The
mod writes `stats.txt` into its local storage when a `debug` file is there, which is how the
script checks what the client has and draws. See the script's docstring for running it.
