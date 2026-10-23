using System.ComponentModel;

namespace Shared.Config;

public interface IPluginConfig : INotifyPropertyChanged
{
    // Enables the plugin
    bool Enabled { get; set; }

    // Sends impostors of grids
    bool Grids { get; set; }

    // Grid groups with fewer blocks are not shown
    int MinBlocks { get; set; }

    // Impostors farther than this are not shown, meters; 0 means the world's view distance
    float MaxDistance { get; set; }

    // Impostors smaller than this on a 1080p screen with a 70 degree field of view are not shown
    float MinPixels { get; set; }

    // Most impostors one player gets, the largest on screen win
    int MaxImpostorsPerPlayer { get; set; }

    // Shape data sent to one player per second, bytes
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
