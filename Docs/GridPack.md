# Grid pack

A grid pack is a mod with the impostors of a world's own grids (stations, shipyards) and of every
prefab in the world's mods (MES encounters and the like), built in advance. Add it to the world's
mod list and Steam downloads it with the other mods. Then:

- the server takes a grid group's shapes from the pack instead of building them,
- players with the same pack get a 14 byte reference per shape and read the shape from the pack,
- players without it, and grids that differ from the pack, work as before.

It works on dedicated servers, on cluster nodes, offline and in lobby games. Offline and on a
lobby host the client plugin's service takes the shapes from the pack, and joiners read them
as above. On a cluster a player also reads far grids of other nodes from it, when their version
is in the pack.

## How a grid finds its entry

Grids spawned from a prefab get new entity ids, so unlike the [asteroid pack](AsteroidPack.md)
this pack is keyed by content. Each entry has the content hash the shapes were built from: the
blocks' positions in the main grid's frame, their sizes, colors and shading kind. It does not depend on
where the grid is or on its id. The server hashes a group anyway before every build, so a lookup costs
nothing extra.

A grid somebody built on, cut up or repainted hashes differently, misses the pack and is built as
usual. Its shapes then have a version the pack doesn't have, so they go over the network.
A damaged block that is still there doesn't change the shape or the hash.

The client tells the server the pack's id right before its Inventory. The server counts on the
client's copy only when the ids match, so a client with an old copy of the mod gets the shapes
over the network.

Some spawned groups miss the pack and are built as before:

- an encounter mod changing a prefab as it spawns it, like recoloring it or replacing blocks,
- moving subgrids whose pose differs from the pack's, and groups with two equally large grids,
  where the main grid is picked by entity id.

## Making one

`tests/grid_pack.py write` does this for "Octahedron - Seed 2", `IMP_SAVE` names the save.

1. On a copy of the world, set `WriteGridPack` to true in `Impostors.cfg`, with `Asteroids` and
   `SafeZones` off and `CacheMegabytes` at 0, and start the server.
2. The server builds the world's grids, then spawns the prefabs of the world's mods in empty space,
   32 at a time, with their functional blocks switched off, builds them and closes them.
   It logs `Grid pack: N of M prefabs spawned` and ends with `Grid pack written: ...`. The world
   is a throwaway after this, don't play on or save it.
3. Copy `Storage/Impostors/GridPack/<world name>` from the server's data folder into the game's
   local mods folder and publish it from the game like any mod. The file is
   `ImpostorsGridPack.dat`: `.dat` is not on the list of extensions the game leaves out when it
   publishes a mod.
4. Add the published mod to the world.

Make it again after changing the world's grids or its mods' prefabs, and after an Impostors
update that changes how shapes are built (the hash includes the algorithm version). Until then
whatever no longer matches is built as usual, nothing breaks.

`GridPack` (true) turns the use of the pack on or off on the server and in the client plugin.
The Impostors mod always uses the pack when the world has one.

## Numbers

"Octahedron - Seed 2" on polaris (Ryzen 9 5950X), `tests/grid_pack.py`, 2026-10-30.

Writing the pack: 1191 prefabs and 65 grids in the world gave 1129 shapes in a 15.3 MB file,
6.1 MB gzipped, in 10 to 13 minutes. The prefabs that don't appear have under `MinBlocks` blocks, or
their content is that of another one.

| Check | Result |
|---|---|
| `again`: everything spawned again, new ids | 1111 of 1147 groups from the pack, 36 built (3.1%) |
| `measure`: 30 prefabs spawned powered, as in play | 17 of 18 viewed groups from the pack |
| `measure`: joining with empty caches, without / with | 16 shapes sent, 14 grids built / 0 sent, 0 built, 16 read from the pack |
| `measure`: a layer of blocks removed | built, its shapes sent |
| `offline` and `lobby` host | 9 grids from the pack, 0 built |
| `lobby` joiner | 0 shapes sent, 6 read from the pack |

Run these again after a write: a pack only helps the grids of the world it was written from and
the prefabs it was written with.
