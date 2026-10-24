# Safe zone impostors

Safe zones beyond the sync distance, which the server doesn't replicate to the players.
Without impostors players can't see where the protected areas are until they are within a
few kilometers. With them, every enabled zone shows out to the view distance as a translucent
bubble of its shape, size and color. The game's shield pattern is not copied: from afar the
outline and the size are what count, and a larger zone stands out more.

The sheets below compare real zones with their impostors. Each row is a distance; each column
pair is a view from one direction, the real zone on the left and its impostor on the right.
The mod's debug compare mode hides the real zone for the impostor shot. Where the real cell is
empty, the game no longer draws the zone: past about 30 km for both. The radius in the row label
is that of the zone's bounding box, about 1.7 times a sphere's own. Rendered at 2560x1440 with `tests/compare.py`.

**Sphere, 500 m radius** (`safezone:500`), 1.5 to 48 km: 512 quads out to 20 km, 128 from
30 km.

![500 m safe zone sphere, real and impostor from 2 directions, 1.5 to 48 km](images/safezone-500.jpg)

**Box, 600 x 300 x 400 m** (`safezone:600,300,400`), 1.5 to 48 km, 6 quads.

![600 x 300 x 400 m safe zone box, real and impostor from 2 directions, 1.5 to 48 km](images/safezone-box.jpg)

**IMDC Rhea v2.3 inside a 250 m sphere** (`IMP_ZONE=250`), 400 m to 83 km, from the 8 cube
corners. The impostor cells show both the ship's impostor and the zone's. The real ship is
seen through the game's distorting shield; its impostor is one level lower than outside a
zone and tinted with the zone's color. Past the game's own range the real cell is empty.
The row label's radius is the ship's.

![IMDC Rhea v2.3 in a 250 m safe zone, real and impostor from 8 directions, 400 m to 83 km](images/safezone-rhea.jpg)

## What gets one

- Every enabled safe zone: admin zones and the zones of safe zone blocks. A disabled zone
  protects nothing, so it gets none.
- Only zones outside the player's replication range, within `MaxDistance` and at least
  `MinPixels` across on screen, like grids. Zones go first among the player's
  `MaxImpostorsPerPlayer`.
- `SafeZones` in [Configuration](Configuration.md) turns them off.

## What it looks like

The server sends the zone itself, not a cell grid: sphere or box, its size and its color, 17
bytes. The mod draws a shell of flat quads, nearly clear seen face on and denser and paler
where the line of sight grazes the surface, so a sphere shows as a thin rim and a box shows
its edge-on faces, as the game's shield does.

| Zone | Radius on screen | Quads |
|---|---|---:|
| Box | any | 6 |
| Sphere | under 6 px | 32 (8 x 4) |
| Sphere | 6 to 25 px | 128 (16 x 8) |
| Sphere | over 25 px | 512 (32 x 16) |

Each size keeps the outline within about half a pixel of a circle. On a 1080p screen a 500 m
sphere is over 25 px in radius out to about 15 km, under 6 px past about 64 km. All the quads
count against `MaxQuadsPerFrame`, since both halves of the shell are drawn.

## Grids and asteroids inside a zone

The game draws a zone's surface with a distorting shield effect, which blurs and tints what
is inside. Impostors are drawn on top of it, undistorted. To make up for that, a grid or
asteroid whose center is inside an enabled zone gets an impostor one level of detail lower
than outside, and the mod blends its colors 30% towards the zone's color. The same
`MinPixels` applies as everywhere else.

`tests/scenario.py` checks it with two grids alike, as far from the player: the one inside a
500 m zone is one level lower and tinted, the one outside isn't. A third one, seen through
the zone but behind it, isn't tinted either.

## Clusters

Admin zones are on every node, so each node shows them itself. The zone of a safe zone block
travels with the block's grid between nodes, and nodes report it to the World Authority like
a grid, so players on other nodes see it too.

## Build times

A safe zone has nothing to build: snapshot and build take about a millisecond together on
polaris (AMD Ryzen 9 5950X) and under 0.1 ms on sirius (AMD Ryzen 7 9800X3D), from the
`Impostor build:` lines of the comparison and scenario runs (`tests/build_times.py`).

| Machine | Zone | Snapshot ms | Build wall ms | Build CPU ms |
|---|---|---:|---:|---:|
| polaris, Ryzen 9 5950X | sphere, 500 m | 0.4 | 1.1 | 0.8 |
| polaris, Ryzen 9 5950X | box, 600 x 300 x 400 m | 0.5 | 1.2 | 0.8 |
| sirius, Ryzen 7 9800X3D | sphere, 500 m | 0.0 | 0.0 | 0.0 |
| sirius, Ryzen 7 9800X3D | box, 600 x 300 x 400 m | 0.0 | 0.0 | 0.0 |
