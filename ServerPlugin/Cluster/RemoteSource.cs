using System;
using Impostors;
using Sandbox.Game.Entities;
using Shared.Impostors;

namespace ServerPlugin.Cluster;

// The grids of the other cluster nodes, and of offline partitions, from the cluster index.
// Their shapes come from the World Authority's cache.
public sealed class RemoteSource : ImpostorSource
{
    private readonly ClusterBridge bridge;

    public RemoteSource(ClusterBridge bridge)
    {
        this.bridge = bridge;
    }

    public override ImpostorKind Kind => ImpostorKind.Grid;

    public override bool Remote => true;

    public override void Scan(ImpostorRegistry registry)
    {
        var node = bridge.Node;
        foreach (var (record, owner) in bridge.Index.Values)
        {
            // This node's own grids come from the local sources; a grid that just arrived by
            // handover may still be listed under its old node for a few seconds
            if (owner == node || MyEntities.EntityExists(record.Id))
                continue;

            var entry = registry.Touch(record.Id, this);
            entry.Kind = record.Kind;
            entry.Tag = record;
            entry.WorldMatrix = record.WorldMatrix;
            entry.WorldAabb = record.WorldAabb;
            entry.Velocity = record.Velocity;
            if (entry.Signature != record.Version)
            {
                entry.Signature = record.Version;
                entry.Stale = true;
            }
        }
    }

    public override Func<byte[][]> PrepareBuild(ImpostorEntry entry)
    {
        if (entry.Tag is not ClusterRecord record)
            return null;
        return () => bridge.FetchShapes(record.Id, record.Version);
    }
}
