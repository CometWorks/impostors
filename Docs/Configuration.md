# Configuration

The server plugin reads `Impostors.cfg` from the server's data folder. It writes the file with
the defaults on the first start.

| Option | Default | Meaning |
|---|---|---|
| `Enabled` | true | Turns the plugin on |
| `Grids` | true | Impostors of grids, see [Grids](Grids.md) |
| `Asteroids` | true | Impostors of asteroids, see [Asteroids](Asteroids.md) |
| `AsteroidOcclusion` | false | Darkens pits and crevices of asteroid impostors, doubles their build time |
| `MinBlocks` | 8 | Grid groups with fewer blocks are not shown |
| `MaxDistance` | 0 | Farthest impostor in meters, 0 is the world's view distance |
| `MinPixels` | 3 | Impostors less than this many pixels across on a 1080p screen are not sent |
| `MaxImpostorsPerPlayer` | 200 | The largest on screen win |
| `MaxQuadsPerFrame` | 12000 | Most impostor quads a client draws per frame, nearest first |
| `ShapeBytesPerSecond` | 32768 | Shape data sent to one player per second, on average |
| `RebuildSeconds` | 60 | Shortest time between two rebuilds of an impostor |
| `RefreshSeconds` | 600 | Impostors in view are rebuilt this often to catch paint changes |
| `ReplicationMargin` | 500 | Impostors arrive this far inside the sync distance already |

Notes:

- `MaxDistance` may be larger than the view distance, which the game caps at 50 km.
  Impostors draw fine far beyond it.
- `MaxQuadsPerFrame` reaches the clients with the server's Welcome. The game draws at most
  32768 billboards per frame for every effect (thruster flames, glare, tracers), so leave room
  for them. The client keeps the nearest impostors within the budget.
- A larger shape than `ShapeBytesPerSecond` still goes out; the following ones wait until the
  player's next seconds have paid it off. New impostors never wait.
