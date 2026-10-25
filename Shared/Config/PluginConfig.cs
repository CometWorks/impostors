using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace Shared.Config;

public class PluginConfig : IPluginConfig
{
    public event PropertyChangedEventHandler PropertyChanged;

    private void SetValue<T>(ref T field, T value, [CallerMemberName] string propName = "")
    {
        if (EqualityComparer<T>.Default.Equals(field, value))
            return;

        field = value;

        OnPropertyChanged(propName);
    }

    private void OnPropertyChanged([CallerMemberName] string propName = "")
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propName));
    }

    private bool enabled = true;
    private bool grids = true;
    private bool asteroids = true;
    private bool asteroidOcclusion = true;
    private bool safeZones = true;
    private int minBlocks = 8;
    private float maxDistance = 500_000f;
    private float minPixels = 3f;
    private int maxImpostorsPerPlayer = 4000;
    private int maxQuadsPerFrame = 16000;
    private int shapeBytesPerSecond = 32 * 1024;
    private int rebuildSeconds = 60;
    private int refreshSeconds = 600;
    private float replicationMargin = 500f;
    private int cacheMegabytes = 256;
    private int cacheDays = 30;

    public bool Enabled
    {
        get => enabled;
        set => SetValue(ref enabled, value);
    }

    public bool Grids
    {
        get => grids;
        set => SetValue(ref grids, value);
    }

    public bool Asteroids
    {
        get => asteroids;
        set => SetValue(ref asteroids, value);
    }

    public bool AsteroidOcclusion
    {
        get => asteroidOcclusion;
        set => SetValue(ref asteroidOcclusion, value);
    }

    public bool SafeZones
    {
        get => safeZones;
        set => SetValue(ref safeZones, value);
    }

    public int MinBlocks
    {
        get => minBlocks;
        set => SetValue(ref minBlocks, value);
    }

    public float MaxDistance
    {
        get => maxDistance;
        set => SetValue(ref maxDistance, value);
    }

    public float MinPixels
    {
        get => minPixels;
        set => SetValue(ref minPixels, value);
    }

    public int MaxImpostorsPerPlayer
    {
        get => maxImpostorsPerPlayer;
        set => SetValue(ref maxImpostorsPerPlayer, value);
    }

    public int MaxQuadsPerFrame
    {
        get => maxQuadsPerFrame;
        set => SetValue(ref maxQuadsPerFrame, value);
    }

    public int ShapeBytesPerSecond
    {
        get => shapeBytesPerSecond;
        set => SetValue(ref shapeBytesPerSecond, value);
    }

    public int RebuildSeconds
    {
        get => rebuildSeconds;
        set => SetValue(ref rebuildSeconds, value);
    }

    public int RefreshSeconds
    {
        get => refreshSeconds;
        set => SetValue(ref refreshSeconds, value);
    }

    public float ReplicationMargin
    {
        get => replicationMargin;
        set => SetValue(ref replicationMargin, value);
    }

    public int CacheMegabytes
    {
        get => cacheMegabytes;
        set => SetValue(ref cacheMegabytes, value);
    }

    public int CacheDays
    {
        get => cacheDays;
        set => SetValue(ref cacheDays, value);
    }
}
