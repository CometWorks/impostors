# PR images

Comparison screenshots linked from the far view PRs. Not code: never merge this branch.

Made by `tests/compare.py` with headless clients on AMD GPUs (Mesa RADV, Linux) at the
medium preset, 1280x720, and put together by a script that merges the runs. The runs were
split by view direction over 20 parallel workers. Each row is a distance, each column group
a view from one cube corner. Rows go out to where the server's default `MinPixels` (3
pixels across at 1080p) drops the impostor; the label says "cut off by default" there.
Every impostor cell names its level of detail.

- `rhea.jpg`: the IMDC Rhea v2.3 ship (18.6k blocks, 40 grids), 300 m to 83 km
- `asteroid512-ao-off.jpg`, `asteroid512-ao-on.jpg`: a 512 m asteroid, 800 m to 227 km,
  without and with `AsteroidOcclusion`
- `asteroid256-ao-off.jpg`, `asteroid256-ao-on.jpg`: a 256 m asteroid, 400 m to 113 km
- `asteroid512-ao-book.jpg`, `asteroid256-ao-book.jpg`: real, impostor without and impostor
  with ambient occlusion side by side

Past 20 km (grids) or about 150 km (these asteroids) the client no longer has the real
object; the real column is empty there, as it is in the game without impostors.
