# Radio Spectrometry

[[QoL] Radio Spectrometry](https://steamcommunity.com/sharedfiles/filedetails/?id=2687324923)
shows what an asteroid is made of on an LCD, from an ore detector pointed at it. It runs on the
client: it casts a 25 km ray from the detector, takes the nearest asteroid entity on it and
reads that asteroid's voxel materials at voxel level of detail 2.

A client only has the asteroids the server replicates to it, within `ViewDistance/√3` per axis
(8.7 km at the default 15 km). A saved or mined asteroid farther out is only drawn by Impostors,
and the mod finds nothing there: "No Asteroid Detected". Procedural asteroids nobody touched
every client generates within its view distance, so those work without Impostors.

## Asteroids drawn as impostors

Since format 4 the [asteroid pack](AsteroidPack.md) has every asteroid's voxel materials, counted
the way Radio Spectrometry counts them on a loaded asteroid. The Impostors mod hands them to other
mods through a mod message:

- On channel `4721300005` the mod sends a
  `Func<Vector3D, Vector3D, Dictionary<string, int>, long>` when it starts, and again whenever
  anyone sends a string on that channel. Register a handler, then send a string, and it arrives
  whichever mod loaded first.
- Called with the two ends of a ray and an empty dictionary, it fills in the voxel counts by
  material subtype of the nearest asteroid impostor on the ray (hit on its bounding sphere) and
  returns its entity id. It returns 0 when there is none, when the world has no pack, or when the
  asteroid was mined since the pack was made, since its materials changed. Call it on the game
  thread.

Radio Spectrometry doesn't know about it. [RadioSpectrometry.patch](RadioSpectrometry.patch)
makes it ask, published as
[[QoL] Radio Spectrometry (Impostors)](https://steamcommunity.com/sharedfiles/filedetails/?id=3833294660)
(unlisted), which the Octahedron world uses instead of the original: when the ray hits no loaded asteroid, it takes the impostor's materials and draws
the spectrum from them. Same counts and the same entity id as the noise seed, so the LCD shows
the same spectrum as from up close. Apply it with `patch -p1` to a copy of the Workshop item
with its `.cs` files converted to LF line endings.

## Test

`tests/radio_spectrometry.py` runs "Octahedron - Seed 2" on a test server with a headless client,
pastes a station (ore detector, battery, LCD with the "Radio Spectrometry Reader" script) 1.2 km
above a ring asteroid and then another one 14 km above it, and reads the LCD's sprites. A case
passes when the LCD shows "Scanning" and an asteroid; the last line checks that both spectra are
the same. `IMP_RS_PACK` puts a pack in place of the world's, `IMP_RS_PATCH` the patched mod in
place of the Workshop one.
