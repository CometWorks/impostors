# Asteroid pack

An asteroid pack is a mod with the impostors of every asteroid of one world, built in
advance at all levels of detail. Add it to the world's mod list and Steam downloads it to
the players with the world's other mods. Then:

- the server takes an asteroid's shapes from the pack instead of building them, as long as
  the asteroid is unchanged,
- players with the pack get no asteroid shapes over the network, only a 14 byte reference
  per shape, which the Impostors mod reads from the pack,
- players with the pack see its asteroids as soon as the world is loaded, before the server
  sends them (see [Early asteroids](#early-asteroids)), and pick their levels themselves: the
  server sends them an asteroid only when it changed, all of its levels at once (see
  [Design](Design.md#client-picked-asteroids-se-739)),
- players without it, and asteroids somebody mined, work as before.

To find an asteroid's pack entry the server hashes its voxels, which takes about 40% of a
build. It does that once per asteroid: it remembers the hash and forgets it when the voxels
change. A mined asteroid is read and built once, and from then on built from its remembered
hash, from the shape cache, until it changes again. After a voxel reset it is read once more,
hashes as it did before anybody mined it, and gets the pack's original shapes back. The
remembered hashes are saved with the world (`Storage/Impostors.asteroids` in the save's
folder).

Offline and on a lobby host the client plugin runs the same service for the local player,
so the pack works there the same way: with the pack mod in the world, the asteroids come from
the pack instead of being built, and the hashes are saved with the offline world.

It pays off on worlds with many saved asteroids, like an asteroid ring. Asteroids that
nobody edited and that the world doesn't save get no impostors anyway (see
[Asteroids](Asteroids.md)), so they need no pack.

## Making one

1. On a copy of the world, set `WriteAsteroidPack` to true in `Impostors.cfg` (see
   [Configuration](Configuration.md)) and start the server. With `Grids` and `SafeZones`
   off and `CacheMegabytes` at 0 nothing else gets built. The client plugin can do the same
   in an offline game.
2. The server builds every asteroid, several at a time, logging
   `Asteroid pack: N of M asteroids built` every 10 seconds, then
   `Asteroid pack written: ...` with the folder: `Storage/Impostors/AsteroidPack/<world name>`
   in the server's data folder (the game's user data folder for the client plugin).
3. Stop the server and set `WriteAsteroidPack` back to false.
4. Copy that folder into the game's local mods folder (`%APPDATA%\SpaceEngineers\Mods` on
   Windows) and publish it to the Workshop from the game, like any mod. Add the published mod
   to the world.

The pack file is `ImpostorsAsteroidPack.dat`, next to a `Data` folder with a readme in it.
The game fails to load a mod without a `Data` folder, and a player whose client doesn't run on
Linux gets a message box saying the pack wasn't loaded. The pack still works, but for a pack
written without that folder, add a `Data` folder with any file in it (not an `.sbc`) and
publish it again.

Packs written before were named
`ImpostorsAsteroidPack.bin`: they still load from a local mod, but the game leaves `.bin` files
out when it publishes a mod, so the Workshop item gets no pack. Write such a pack again, or
rename the file to `.dat`, before publishing it.

Packs written before the early asteroids (formats 1 and 2) have no positions, so their asteroids
don't show early; write the pack again to get that.

Since format 4 the pack also has the voxel materials of every asteroid, which other mods can
ask the Impostors mod for, like [Radio Spectrometry](RadioSpectrometry.md) for the asteroids
the game hasn't loaded. Writing them takes about 25 s more for "Octahedron - Seed 2" and adds
about 250 KB. Older packs have none; write the pack again to get them.

The pack belongs to that world: it is keyed by the asteroids' entity ids and the voxels they
were built from. Make it again after changing the world's asteroids, after changing
`AsteroidOcclusion` or `AsteroidSmoothNormals` and after an Impostors update that changes how
asteroids are built (the release notes say so); until then the server builds what no longer
matches. The pack keeps the asteroid settings it was built with, and the log warns at the
start when they differ from the server's. Packs written before `AsteroidSmoothNormals` came
have flat normals: they match only with it off.

`tests/asteroid_pack.py write` does all of this for the "Octahedron - Seed 2" test world.

## Size

All levels of a 512 m asteroid take about 190 KB, most of it level 64. For the 2004 asteroids
of "Octahedron - Seed 2" that is about 375 MB, half of that compressed for the download.
`AsteroidPackMaxLevel` 32 leaves level 64 out, about 84 MB for the same world. Clients then
get level 64 shapes over the network, as before, and the server builds those asteroids
itself when it needs them.

## Settings

- `AsteroidPack` (true) turns the use of the pack on or off on the server, and in the client
  plugin.
- `AsteroidPackMod` (0) names the mod with the pack by its Workshop id. 0 takes the first of
  the world's mods that has one.

The Impostors mod always uses the pack when the world has one.

## Numbers

"Octahedron - Seed 2" on sirius (Ryzen 7 9800X3D), `tests/asteroid_pack.py`, 2026-10-25.

Writing the pack: 2004 asteroids built in 44 s with 8 builds at a time (84 s CPU), a
368 MB file.

A client joining 10 km above the asteroid ring with empty shape caches, server and client,
until nothing changed for 2 minutes (1479 impostors):

| | Without the pack | With the pack |
|---|---:|---:|
| Asteroid builds on the server | 1670, 59.2 s CPU | 0 |
| Asteroids taken from the pack | 0 | 1670, 23.1 s CPU |
| Shapes sent to the client | 2273, 943 KB | 8 (grids and safe zones), 0 KB |
| Shapes the client read from the pack | 0 | 2276 |
| Impostors on the client after 10 / 60 / 90 s | 743 / 1341 / 1479 | 757 / 1355 / 1479 |

- The first start reads every asteroid's voxels once to hash them, about 14 ms against 35 ms
  for a whole build. After a restart with the hashes saved with the world, the same pack hits
  took 16 ms CPU together, for 1562 asteroids.
- The impostors arrived just as fast with the pack: the server then started at most two
  builds every 10 ticks, pack hits included, so the pace was that of the build queue, not of
  the building. Since SE-678 a build starts as soon as a slot frees, see
  [Design](Design.md#server).
- The client read the pack's index in 4 to 5 ms when the world loaded.

Offline with the client plugin, the same place over the ring, empty shape cache (1442
impostors):

| | Without the pack | With the pack |
|---|---:|---:|
| Asteroid builds | 1439, 48.8 s CPU | 0 |
| Asteroids taken from the pack | 0 | 1439, 20.6 s CPU |
| Impostors after 10 / 60 / 120 s | 186 / 786 / 1442 | 186 / 798 / 1442 |

Saved and loaded again, the offline world knew the hashes of all 1442 asteroids, and their
pack hits took 0.08 s CPU together instead of 22 s.
