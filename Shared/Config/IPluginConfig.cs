using System.ComponentModel;

namespace Shared.Config;

public interface IPluginConfig : INotifyPropertyChanged
{
    // Enables the plugin
    bool Enabled { get; set; }

    // Sends impostors of grids
    bool Grids { get; set; }

    // Sends impostors of asteroids (voxel maps other than planets)
    bool Asteroids { get; set; }

    // Bakes ambient occlusion into asteroid impostor colors: pits and crevices darken a little,
    // building takes about twice as long
    bool AsteroidOcclusion { get; set; }

    // Shades asteroid impostors with normals that follow the voxels' surface; off, they follow
    // the impostor's cells and the coarse levels look blocky
    bool AsteroidSmoothNormals { get; set; }

    // Sends impostors of enabled safe zones
    bool SafeZones { get; set; }

    // Grid and asteroid impostors inside an enabled safe zone get one level of detail lower,
    // as the zone's distorting surface shows less of the real one; not from inside that zone
    bool SafeZoneLowerLevel { get; set; }

    // Grid groups with fewer blocks are not shown
    int MinBlocks { get; set; }

    // Impostors farther than this are not shown, meters (500 km); 0 means the world's view
    // distance
    float MaxDistance { get; set; }

    // Impostors less than this many pixels across on a 1080p screen with a 70 degree field of
    // view are not shown, 3 cuts off those under 3x3 pixels
    float MinPixels { get; set; }

    // Most impostors one player gets, the largest on screen win
    int MaxImpostorsPerPlayer { get; set; }

    // Most impostor quads a client draws per frame, the nearest impostors first. The game
    // draws at most 32768 billboards per frame, shared with thruster flames, glare, tracers
    // and other effects.
    int MaxQuadsPerFrame { get; set; }

    // Shape data sent to one player per second on average, bytes
    int ShapeBytesPerSecond { get; set; }

    // Shortest time between two rebuilds of the same impostor, seconds
    int RebuildSeconds { get; set; }

    // Impostors in view are rebuilt this often even without a known change, seconds, which
    // catches changes that don't alter the block count or bounds, like painting
    int RefreshSeconds { get; set; }

    // Disk space for built shapes, megabytes; a restart builds only what changed. 0 turns the
    // cache off.
    int CacheMegabytes { get; set; }

    // Cached shapes not used for this many days are dropped, 0 for no limit
    int CacheDays { get; set; }

    // Cached shapes built this many days ago are dropped even while in use, 0 for no limit
    int CacheBuildDays { get; set; }

    // Minutes between two background sweeps of the caches, which drop what CacheDays,
    // CacheBuildDays and the size caps say; 0 turns the sweeps off
    int CacheSweepMinutes { get; set; }

    // Client plugin: disk space for the shapes received from servers, megabytes; 0 turns
    // that cache off
    int ClientCacheMegabytes { get; set; }

    // Impostors are sent this much inside the sync distance already, meters, so the client has
    // them before the real entity leaves its replication range
    float ReplicationMargin { get; set; }

    // Takes asteroid shapes from the world's asteroid pack mod (see AsteroidPack) where the
    // asteroid is unchanged, on the server and, with the client plugin, on the client
    bool AsteroidPack { get; set; }

    // Workshop id of the mod with the asteroid pack, 0 for any of the world's mods that has one
    ulong AsteroidPackMod { get; set; }

    // Builds every asteroid of the world and writes the asteroid pack mod into
    // Storage/Impostors/AsteroidPack in the user data folder, once per session
    bool WriteAsteroidPack { get; set; }

    // Finest level written into the asteroid pack; with less than 64 the pack is smaller but
    // serves only clients, the server builds those asteroids
    int AsteroidPackMaxLevel { get; set; }
}
