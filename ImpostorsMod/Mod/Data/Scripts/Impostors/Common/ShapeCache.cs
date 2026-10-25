using System.Collections.Generic;

namespace Impostors
{
    // The shapes of the impostors the server removed from a player, which the client keeps so
    // the server can show them again with a state only. The server and the client run the same
    // cache over the same ordered messages, so both evict the same entries without telling each
    // other: the earliest removed first, once their encoded shapes add up to more than MaxBytes.
    public class ShapeCache<T>
    {
        public const int MaxBytes = 4 * 1024 * 1024;

        private struct Item
        {
            public T Value;
            public int Version;
            public int Bytes;
            public long Order;
        }

        private readonly Dictionary<long, Item> items = new Dictionary<long, Item>();

        // Ids in the order they were added; an entry is stale when its id was taken or added
        // again since
        private readonly Queue<KeyValuePair<long, long>> order =
            new Queue<KeyValuePair<long, long>>();
        private long next;

        public int Bytes { get; private set; }

        public int Count
        {
            get { return items.Count; }
        }

        public void Add(long id, int version, int bytes, T value)
        {
            Drop(id);
            items[id] = new Item
            {
                Value = value,
                Version = version,
                Bytes = bytes,
                Order = next,
            };
            order.Enqueue(new KeyValuePair<long, long>(id, next++));
            Bytes += bytes;

            while (Bytes > MaxBytes && order.Count > 0)
            {
                var oldest = order.Dequeue();
                Item item;
                if (items.TryGetValue(oldest.Key, out item) && item.Order == oldest.Value)
                    Drop(oldest.Key);
            }

            if (order.Count > 2 * items.Count + 64)
                Compact();
        }

        // Takes the shape of the impostor out of the cache, and gives it back if it is still of
        // that version
        public bool Take(long id, int version, out T value)
        {
            Item item;
            value = default(T);
            if (!items.TryGetValue(id, out item))
                return false;
            Drop(id);
            if (item.Version != version)
                return false;
            value = item.Value;
            return true;
        }

        public void Clear()
        {
            items.Clear();
            order.Clear();
            Bytes = 0;
        }

        private void Drop(long id)
        {
            Item item;
            if (!items.TryGetValue(id, out item))
                return;
            items.Remove(id);
            Bytes -= item.Bytes;
        }

        private void Compact()
        {
            var live = new List<KeyValuePair<long, long>>();
            foreach (var entry in order)
            {
                Item item;
                if (items.TryGetValue(entry.Key, out item) && item.Order == entry.Value)
                    live.Add(entry);
            }
            order.Clear();
            foreach (var entry in live)
                order.Enqueue(entry);
        }
    }
}
