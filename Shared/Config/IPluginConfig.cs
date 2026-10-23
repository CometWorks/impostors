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

    // Grid groups with fewer blocks are not shown
    int MinBlocks { get; set; }

    // Impostors farther than this are not shown, meters; 0 means the world's view distance
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

    // Impostors are sent this much inside the sync distance already, meters, so the client has
    // them before the real entity leaves its replication range
    float ReplicationMargin { get; set; }
}
