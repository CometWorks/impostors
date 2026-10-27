"""What tests/level_views.py and tests/cluster_views.py read from the mod's stats.txt: the
impostors in view, how long they took to show and settle, and the levels they were drawn at
by distance. No game or rig here."""

from __future__ import annotations

import time

import numpy as np

# The server's estimate (Protocol.PixelsPerRadian, ImpostorService.PickLod): a 1080p screen
# with a 70 degree view, one cell per 2.5 pixels of the impostor's diameter
PIXELS_PER_RADIAN = 540 / 0.7002075382097097
LODS = (2, 4, 8, 16, 32, 64)


def in_view(impostors: dict) -> dict[int, dict]:
    """Impostors whose center is on screen"""
    out = {}
    for i, row in impostors.items():
        if "screen" not in row or row["screen"] == "behind":
            continue
        x, y = (float(v) for v in row["screen"].split(","))
        w, h = (float(v) for v in row["viewport"].split("x"))
        if 0 <= x < w and 0 <= y < h:
            out[int(i)] = row
    return out


def timeline(stats, start: float, limit: float, hold: float = 15) -> dict:
    """Samples stats() (returning the impostors by id) every second until the impostors in
    view kept their levels and drawn state for `hold` seconds, or `limit` seconds after
    start: seconds to 90% and to all of the impostors finally drawn, and to their last
    level change"""
    samples = []
    while time.monotonic() - start < limit:
        rows = in_view(stats())
        drawn = frozenset(i for i, r in rows.items() if r.get("drawn") == "1")
        levels = {i: r.get("shape", "none") for i, r in rows.items()}
        samples.append((time.monotonic() - start, len(rows), drawn, levels))
        if len(samples) > 1 and drawn:
            changed = next(
                (
                    t
                    for t, _, d, l in reversed(samples[:-1])
                    if l != levels or d != drawn
                ),
                0,
            )
            if samples[-1][0] - changed > hold:
                break
        time.sleep(1)
    if not samples:
        return {}
    _, count, final, last = samples[-1]
    first = next((t for t, _, d, _ in samples if d), None)
    ninety = next(
        (t for t, _, d, _ in samples if final and len(d & final) >= 0.9 * len(final)),
        None,
    )
    every = next((t for t, _, d, _ in samples if final and final <= d), None)
    settled = next(
        (
            samples[k][0]
            for k in range(len(samples) - 1, 0, -1)
            if samples[k - 1][3] != samples[k][3]
        ),
        samples[0][0],
    )
    return {
        "inview": count,
        "drawn": len(final),
        "first": first,
        "ninety": ninety,
        "all": every,
        "levels_settled": settled,
        "sampled": samples[-1][0],
    }


def expected_level(row: dict) -> int:
    """The level the server's estimate gives an impostor this large this far, without the
    hysteresis and the quad budget"""
    pixels = float(row["r"]) / max(float(row["distance"]), 1) * PIXELS_PER_RADIAN
    wanted = 2 * pixels / 2.5
    return next((lod for lod in LODS if lod >= wanted), LODS[-1])


def drawn_level(row: dict) -> int:
    return int(row["shape"].split("/")[1]) if row.get("shape", "none") != "none" else 0


def timing_table(timings: dict[str, dict]) -> list[str]:
    fmt = lambda v: "-" if v is None else f"{v:.1f}"
    lines = [
        "| View | Impostors | In view | Drawn | First drawn s | 90% drawn s | All drawn s "
        "| Levels settled s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, t in timings.items():
        lines.append(
            f"| {name} | {t.get('impostors', '-')} | {t.get('inview')} | {t.get('drawn')} | {fmt(t.get('first'))} "
            f"| {fmt(t.get('ninety'))} | {fmt(t.get('all'))} | {fmt(t.get('levels_settled'))} |"
        )
    return lines


def level_table(impostors: dict) -> list[str]:
    """Per level drawn in view: count, distances, pixel radius and how many are at the
    level the estimate gives, coarser (the quad budget) or finer (the hysteresis)"""
    rows = [r for r in in_view(impostors).values() if r.get("drawn") == "1"]
    lines = [
        "| Level | Drawn | Distance km min / median / max | Radius px median "
        "| At the estimate's level | Coarser | Finer |",
        "|---:|---:|---|---:|---:|---:|---:|",
    ]
    by_level: dict[int, list] = {}
    for r in rows:
        by_level.setdefault(drawn_level(r), []).append(r)
    for lod in sorted(by_level):
        rs = by_level[lod]
        d = np.array([float(r["distance"]) for r in rs]) / 1000
        px = np.array([float(r["radius"]) for r in rs])
        exp = [expected_level(r) for r in rs]
        lines.append(
            f"| {lod} | {len(rs)} | {d.min():.1f} / {np.median(d):.1f} / {d.max():.1f} "
            f"| {np.median(px):.1f} | {sum(e == lod for e in exp)} "
            f"| {sum(e > lod for e in exp)} | {sum(e < lod for e in exp)} |"
        )
    return lines
