# Impostors

Far view for Space Engineers 1 multiplayer. The server doesn't send grids beyond the sync
distance (3 km by default) to the players, and edited asteroids only to about 8.7 km, so
players see empty space where a station or a fleet is. Impostors shows them as coarse, sun
shaded models, built on the server from block and voxel data.
Safe zones show from afar too, as translucent bubbles in their color.

- **Server plugin** (Magnetar): picks what each player should see, builds the impostors and
  sends them over mod networking.
- **Impostors mod**: draws them on the clients. Players need no plugin, only the mod in the
  world's mod list.
- **Client plugin** (Pulsar): draws them on servers whose world doesn't have the mod, and
  serves the players who joined when you host a lobby.

## Server setup

1. Enable the Impostors server plugin in Magnetar.
2. Add the Impostors mod to the world.
3. Optionally edit `Impostors.cfg` in the server's data folder: see
   [Configuration](Docs/Configuration.md).

## Documentation

- [Grids](Docs/Grids.md): what ship and station impostors look like, with comparison
  screenshots.
- [Asteroids](Docs/Asteroids.md): the same for asteroids, with and without ambient
  occlusion.
- [Zoomed views](Docs/Zoom.md): finer levels through a zoomed camera, with comparison
  screenshots.
- [Safe zones](Docs/SafeZones.md): safe zone impostors, and the stricter cut-off for small
  impostors seen through a zone.
- [Configuration](Docs/Configuration.md): the options of `Impostors.cfg`.
- [Design](Docs/Design.md): how it works inside, the protocol, clusters and the limits.
- [Development](Docs/Development.md): building, deployment, debugging and tests.
