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
    private bool asteroidSmoothNormals = true;
    private bool safeZones = true;
    private bool safeZoneLowerLevel;
    private int minBlocks = 8;
    private float maxDistance = 400_000f;
    private float minPixels = 3f;
    private int maxImpostorsPerPlayer = 4000;
    private int maxQuadsPerFrame = 16384;
    private int shapeBytesPerSecond = 32 * 1024;
    private int rebuildSeconds = 60;
    private int refreshSeconds = 600;
    private float replicationMargin = 500f;
    private int cacheMegabytes = 1024;
    private int cacheDays = 30;
    private int cacheBuildDays = 90;
    private int cacheSweepMinutes = 60;
    private int clientCacheMegabytes = 512;
    private bool gridPack = true;
    private bool writeGridPack;
    private bool asteroidPack = true;
    private ulong asteroidPackMod;
    private bool writeAsteroidPack;
    private int asteroidPackMaxLevel = 64;

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

    public bool AsteroidSmoothNormals
    {
        get => asteroidSmoothNormals;
        set => SetValue(ref asteroidSmoothNormals, value);
    }

    public bool SafeZones
    {
        get => safeZones;
        set => SetValue(ref safeZones, value);
    }

    public bool SafeZoneLowerLevel
    {
        get => safeZoneLowerLevel;
        set => SetValue(ref safeZoneLowerLevel, value);
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

    public int CacheBuildDays
    {
        get => cacheBuildDays;
        set => SetValue(ref cacheBuildDays, value);
    }

    public int CacheSweepMinutes
    {
        get => cacheSweepMinutes;
        set => SetValue(ref cacheSweepMinutes, value);
    }

    public int ClientCacheMegabytes
    {
        get => clientCacheMegabytes;
        set => SetValue(ref clientCacheMegabytes, value);
    }

    public bool GridPack
    {
        get => gridPack;
        set => SetValue(ref gridPack, value);
    }

    public bool WriteGridPack
    {
        get => writeGridPack;
        set => SetValue(ref writeGridPack, value);
    }

    public bool AsteroidPack
    {
        get => asteroidPack;
        set => SetValue(ref asteroidPack, value);
    }

    public ulong AsteroidPackMod
    {
        get => asteroidPackMod;
        set => SetValue(ref asteroidPackMod, value);
    }

    public bool WriteAsteroidPack
    {
        get => writeAsteroidPack;
        set => SetValue(ref writeAsteroidPack, value);
    }

    public int AsteroidPackMaxLevel
    {
        get => asteroidPackMaxLevel;
        set => SetValue(ref asteroidPackMaxLevel, value);
    }
}
