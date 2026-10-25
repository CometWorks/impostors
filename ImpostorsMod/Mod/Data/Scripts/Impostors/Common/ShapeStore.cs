using System.Collections.Generic;

namespace Impostors
{
    // Every shape the server sent to a client, by impostor id, level and version, so the server
    // can switch an impostor to a level the client has, or show a removed one again, with a
    // state only. The server and the client run the same store over the same ordered
    // ShapeData messages, so both evict the same shapes without telling each other: the
    // earliest added first, once their encoded sizes add up to more than MaxBytes. The server
    // only checks for a shape before this update's shapes are added, the client looks up the
    // states' shapes before them too, so whatever the server finds the client has.
    public class ShapeStore<T>
    {
        public const int MaxBytes = 8 * 1024 * 1024;

        // Level 0 (a safe zone) and 2 to 64
        private const int Slots = 7;

        private struct Item
        {
            public bool Present;
            public T Value;
            public int Version;
            public int Bytes;
            public long Order;
        }

        private struct Added
        {
            public long Id;
            public int Slot;
            public long Order;
        }

        private readonly Dictionary<long, Item[]> items = new Dictionary<long, Item[]>();

        // Shapes in the order they were added; stale when the shape was added again since
        private readonly Queue<Added> order = new Queue<Added>();
        private long next;

        public int Bytes { get; private set; }

        public int Count { get; private set; }

        public void Add(long id, byte lod, int version, int bytes, T value)
        {
            Item[] slots;
            if (!items.TryGetValue(id, out slots))
                slots = new Item[Slots];
            var slot = Slot(lod);
            Drop(id, slots, slot);
            items[id] = slots;
            slots[slot] = new Item
            {
                Present = true,
                Value = value,
                Version = version,
                Bytes = bytes,
                Order = next,
            };
            Count++;
            Bytes += bytes;
            order.Enqueue(
                new Added
                {
                    Id = id,
                    Slot = slot,
                    Order = next++,
                }
            );

            while (Bytes > MaxBytes && order.Count > 0)
            {
                var oldest = order.Dequeue();
                if (
                    items.TryGetValue(oldest.Id, out slots)
                    && slots[oldest.Slot].Order == oldest.Order
                )
                    Drop(oldest.Id, slots, oldest.Slot);
            }

            if (order.Count > 2 * Count + 64)
                Compact();
        }

        public bool TryGet(long id, byte lod, int version, out T value)
        {
            Item[] slots;
            value = default(T);
            if (!items.TryGetValue(id, out slots))
                return false;
            var item = slots[Slot(lod)];
            if (!item.Present || item.Version != version)
                return false;
            value = item.Value;
            return true;
        }

        public void Clear()
        {
            items.Clear();
            order.Clear();
            Bytes = 0;
            Count = 0;
        }

        private static int Slot(byte lod)
        {
            var slot = 0;
            while (lod > 1)
            {
                lod >>= 1;
                slot++;
            }
            return slot < Slots ? slot : Slots - 1;
        }

        private void Drop(long id, Item[] slots, int slot)
        {
            if (!slots[slot].Present)
                return;
            Bytes -= slots[slot].Bytes;
            Count--;
            slots[slot] = default(Item);
            foreach (var item in slots)
            {
                if (item.Present)
                    return;
            }
            items.Remove(id);
        }

        private void Compact()
        {
            var live = new List<Added>();
            foreach (var added in order)
            {
                Item[] slots;
                if (
                    items.TryGetValue(added.Id, out slots)
                    && slots[added.Slot].Order == added.Order
                )
                    live.Add(added);
            }
            order.Clear();
            foreach (var added in live)
                order.Enqueue(added);
        }
    }
}
