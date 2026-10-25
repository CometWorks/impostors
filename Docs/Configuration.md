# Configuration

The server plugin reads `Impostors.cfg` from the server's data folder. It writes the file with
the defaults on the first start; a server that already has the file keeps the values in it.
The file is read only when the server starts, so changes take effect after a restart.

| Option | Default | Meaning |
|---|---|---|
| `Enabled` | true | Turns the plugin on |
| `Grids` | true | Impostors of grids, see [Grids](Grids.md) |
| `Asteroids` | true | Impostors of asteroids, see [Asteroids](Asteroids.md) |
| `AsteroidOcclusion` | true | Darkens pits and crevices of asteroid impostors, doubles their build time |
| `SafeZones` | true | Impostors of enabled safe zones, see [Safe zones](SafeZones.md) |
| `MinBlocks` | 8 | Grid groups with fewer blocks are not shown |
| `MaxDistance` | 500000 | Farthest impostor in meters (500 km), 0 is the world's view distance |
| `MinPixels` | 3 | Impostors less than this many pixels across on a 1080p screen are not sent |
| `MaxImpostorsPerPlayer` | 4000 | The largest on screen win |
| `MaxQuadsPerFrame` | 16000 | Most impostor quads a client draws per frame; the server picks coarser levels to fit |
| `ShapeBytesPerSecond` | 32768 | Shape data sent to one player per second, on average |
| `RebuildSeconds` | 60 | Shortest time between two rebuilds of an impostor |
| `RefreshSeconds` | 600 | Impostors in view are rebuilt this often to catch paint changes |
| `ReplicationMargin` | 500 | Impostors arrive this far inside the sync distance already |

Notes:

- `MaxDistance` may be larger than the view distance, which the game caps at 50 km.
  Impostors draw fine far beyond it. `MinPixels` and `MaxImpostorsPerPlayer` still cut off
  the ones too small to see.
- `MaxQuadsPerFrame` is the clients' budget, sent by the server. The game draws at most 32768
  billboards per frame for every effect (thruster flames, glare, tracers), so leave room for
  them. The server picks levels whose quads fit the budget together, and the client keeps the
  nearest impostors in its view within it. How the value gets there:
  - When a client joins, the mod says hello and the server answers with a Welcome that
    carries `MaxQuadsPerFrame`. The mod takes it, capped at 32768.
  - The mod says hello every 30 seconds and every Welcome carries the value again, which
    also covers a client moved to another cluster node.
  - Until the first Welcome the mod uses its own starting value, the same as the default
    (16000). It hardly matters: the server sends impostors only after the Welcome.
  - The client plugin draws with the same code, so it gets the budget the same way.
  - The client counts the quads it really draws, those facing the camera, and never draws
    more than the budget.
  - The client plugin lowers the budget further, to the room the frame's other billboards
    leave, and when a frame is over 32768 anyway it drops impostor quads before any other
    billboard. A client with only the mod can't: there the other billboards are safe only up
    to 32768 minus `MaxQuadsPerFrame`. See
    [Billboard limit](Design.md#billboard-limit-se-664).
- A larger shape than `ShapeBytesPerSecond` still goes out; the following ones wait until the
  player's next seconds have paid it off. New impostors never wait, but while the player is in
  debt they start with a small level. Levels the client got earlier in the session cost no
  shape credit (see [Moving views](Design.md#moving-views-se-671)).
