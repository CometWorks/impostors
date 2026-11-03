"""The asteroid levels drawn over the Octahedron ring in the game (SE-971): the "Octahedron -
Seed 2" world offline with the client plugin and the world's asteroid pack, so the client
picks the asteroids' levels itself as multiplayer clients with the pack do. The character
stops at tests/ring_budget.py's spots in first person with the HUD hidden (the client probe's
look.txt), each at the zooms 1x, 2x and 8x (its fov.txt, as the rifle's sight, a camera block
or a turret narrow the view). Per spot
and zoom, once the drawn levels stopped changing: a screenshot and the stats.

    IMP_TASK=se971 IMP_RESOLUTION=1920x1080 IMP_SAVE=<"Octahedron - Seed 2" save> \\
        uv run --no-project --with httpx --with pillow --with numpy \\
        python tests/ring_levels.py <tag>
    ... python tests/ring_levels.py --table <tag>   # the table of a finished run only
    ... python tests/ring_levels.py --sheets <before> <after>   # comparison sheets

The pack is taken from ~/.se-test/<task>-pack (tests/asteroid_pack.py). Files in
~/.se-test/<task>-levels/<tag>: <spot>-<zoom>x.png and .json, and table.md with the same
columns as ring_budget.py: the asteroids in view, drawn, the quads, the impostors left out
over the budget, the drawn ones per level level 8 or finer. Exits with 0; the
numbers are for reading.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asteroid_pack as ap  # noqa: E402
import rig  # noqa: E402
import ring  # noqa: E402
import ring_budget as rb  # noqa: E402

OUT = rig.TEST_ROOT / f"{rig.TASK}-levels"
# Seconds per view at most, and how long the drawn levels must stay the same
SETTLE = float(os.environ.get("IMP_SETTLE", "60"))
STABLE = 8.0
# Only these spots (IMP_SPOTS, comma separated), to spread a run over hosts
SPOTS = set(filter(None, os.environ.get("IMP_SPOTS", "").split(",")))
rig.CLIENT_GIB = 10.0


def look(client, eye, forward) -> None:
    yaw = math.degrees(math.atan2(forward[0], -forward[2]))
    pitch = math.degrees(math.asin(forward[1]))
    (client.appdata / "look.txt").write_text(
        f"{eye[0]:.1f} {eye[1]:.1f} {eye[2]:.1f} {yaw:.4f} {pitch:.4f}"
    )


def drawn(impostors: dict) -> dict[int, dict]:
    return {
        i: r
        for i, r in impostors.items()
        if r.get("kind") == "Voxel"
        and r.get("drawn") == "1"
        and r.get("shape", "none") != "none"
    }


def settle(client) -> tuple[dict, dict, float]:
    """Stats once the drawn asteroids and their levels stayed the same for STABLE seconds"""
    start = time.monotonic()
    last, since = None, start
    while True:
        time.sleep(2)
        totals, impostors = client.stats()
        now = {i: r["shape"] for i, r in drawn(impostors).items()}
        if now != last:
            last, since = now, time.monotonic()
        elif now and time.monotonic() - since >= STABLE:
            return totals, impostors, since - start
        if time.monotonic() - start > SETTLE:
            return totals, impostors, -1


def row(name: str, zoom: int, totals: dict, impostors: dict, settled: float) -> str:
    hist = dict.fromkeys(rb.LODS, 0)
    starved = 0
    shown = drawn(impostors)
    for r in shown.values():
        lod = int(r["shape"].split("/")[1])
        hist[lod] = hist.get(lod, 0) + 1
        # The stats' radius is in pixels of this screen, zoom included
        height = float(r["viewport"].split("x")[1]) if "viewport" in r else 1080
        wanted = 2 * float(r.get("radius", 0)) * 1080 / height * rb.CELLS_PER_PIXEL
        if lod == 2 and wanted > 4:
            starved += 1
    return (
        f"| {name} | {zoom}x | {totals.get('inview')} | {len(shown)} | "
        f"{totals.get('quads')} | {totals.get('overbudget')} | "
        + " | ".join(str(hist[l]) for l in rb.LODS)
        + f" | {starved} | {settled:.0f} |"
    )


HEADER = [
    "| spot | zoom | in view | drawn | quads | over budget |"
    + "".join(f" L{l} |" for l in rb.LODS)
    + " cubes that want 8+ | settled s |",
    "|---" * (8 + len(rb.LODS)) + "|",
]


def table(out: Path) -> str:
    lines = list(HEADER)
    for name, _, _ in rb.spots(rb.read_index(ap.PACK / ap.PACK_FILE)[0]):
        for zoom in rb.ZOOMS:
            f = out / f"{name}-{zoom}x.json"
            if f.exists():
                d = json.loads(f.read_text())
                lines.append(row(name, zoom, d["totals"], d["impostors"], d["settled"]))
    text = "\n".join(lines) + "\n"
    (out / "table.md").write_text(text)
    return text


def starved_center(impostors: dict, size: tuple[int, int]) -> tuple[int, int]:
    """Screen point amid the drawn level-2 cubes that want level 8 or finer, or the center"""
    points = []
    for r in drawn(impostors).values():
        if r["shape"].split("/")[1] != "2" or "screen" not in r:
            continue
        height = float(r["viewport"].split("x")[1])
        if 2 * float(r["radius"]) * 1080 / height * rb.CELLS_PER_PIXEL > 4:
            points.append([float(v) for v in r["screen"].split(",")])
    if not points:
        return size[0] // 2, size[1] // 2
    x, y = np.median(points, axis=0)
    return int(x), int(y)


def sheets(before: Path, after: Path, out: Path) -> None:
    """Per spot: for each zoom both screenshots, then both cropped to a quarter around the
    starved cubes of the run before and enlarged twice, pixel for pixel"""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=22)
    out.mkdir(parents=True, exist_ok=True)
    spots = dict.fromkeys(
        f.stem.rsplit("-", 1)[0] for f in sorted(before.glob("*.png"))
    )
    for spot in spots:
        rows = []
        for zoom in rb.ZOOMS:
            name = f"{spot}-{zoom}x"
            if not (after / f"{name}.png").exists():
                continue
            images = [
                Image.open(d / f"{name}.png").convert("RGB") for d in (before, after)
            ]
            stats = [
                json.loads((d / f"{name}.json").read_text()) for d in (before, after)
            ]
            w, h = images[0].size
            cx, cy = starved_center(stats[0]["impostors"], (w, h))
            cw, ch = w // 4, h // 4
            left = min(max(cx - cw // 2, 0), w - cw)
            top = min(max(cy - ch // 2, 0), h - ch)
            labels = []
            for d in stats:
                cubes = (
                    row("", 0, d["totals"], d["impostors"], 0).split("|")[-3].strip()
                )
                labels.append(
                    f"{d['totals'].get('quads')} quads, {cubes} starved cubes"
                )
            rows.append(
                (
                    f"{zoom}x",
                    [i.resize((w // 2, h // 2), Image.LANCZOS) for i in images],
                    [
                        i.crop((left, top, left + cw, top + ch)).resize(
                            (w // 2, h // 2), Image.NEAREST
                        )
                        for i in images
                    ],
                    labels,
                )
            )
        if not rows:
            continue
        cw, ch = rows[0][1][0].size
        bar = 34
        sheet = Image.new("RGB", (2 * cw + 10, len(rows) * 2 * (ch + bar)), "white")
        draw = ImageDraw.Draw(sheet)
        y = 0
        for zoom, full, crop, labels in rows:
            for k, (tag, label) in enumerate(zip(("before", "after"), labels)):
                draw.text(
                    (k * (cw + 10) + 8, y + 6),
                    f"{spot} {zoom} {tag}: {label}",
                    fill="black",
                    font=font,
                )
                sheet.paste(full[k], (k * (cw + 10), y + bar))
                draw.text(
                    (k * (cw + 10) + 8, y + ch + bar + 6),
                    f"{spot} {zoom} {tag}, cropped, 2x",
                    fill="black",
                    font=font,
                )
                sheet.paste(crop[k], (k * (cw + 10), y + ch + 2 * bar))
            y += 2 * (ch + bar)
        sheet.save(out / f"{spot}.jpg", quality=88)


def run(out: Path) -> None:
    import local

    pos, _, _ = rb.read_index(ap.PACK / ap.PACK_FILE)
    client = rig.Client(2, "Viktor", plugin=True, quality="medium", probe=True)
    world = client.appdata / "Saves" / ring.WORLD_NAME
    ring.PLUGIN = True
    ids = ring.copy_world(world)
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        f = world / name
        f.write_text(
            f.read_text(encoding="utf-8").replace(
                "<OnlineMode>PUBLIC</OnlineMode>", "<OnlineMode>OFFLINE</OnlineMode>"
            ),
            encoding="utf-8",
        )
    ap.add_pack(world)
    ids.append(ap.PACK_ID)
    client.appdata.mkdir(parents=True, exist_ok=True)
    (client.appdata / "Impostors.cfg").write_text(
        rig.plugin_config(AsteroidPack=True), encoding="utf-8"
    )
    shutil.rmtree(client.appdata / "Storage/Impostors/Cache", ignore_errors=True)
    (client.loaded_mod_storage / "noearly").unlink(missing_ok=True)
    for name in ("camera.txt", "look.txt"):
        (client.appdata / name).unlink(missing_ok=True)
    client.set_fov(None)
    args = client.args
    client.args = lambda _port: [
        a for a in args(0) if a not in ("--connect", "127.0.0.1:0")
    ]
    client.launch(0)
    ap.stage(client.appdata / "Mods", ids)
    try:
        api = client.wait_api()
        api.load(str(world), online_mode="OFFLINE")
        local.wait(lambda: local.past_questions(api), 900, 2)
        rig.ensure_character(api)
        rig.wait_world(api, timeout=1200)
        rig.focus_gameplay(api)
        for name, eye, forward in rb.spots(pos):
            if SPOTS and name not in SPOTS:
                continue
            look(client, eye, forward)
            for zoom in rb.ZOOMS:
                client.set_fov(math.degrees(rb.fov_for(zoom)))
                totals, impostors, settled = settle(client)
                (out / f"{name}-{zoom}x.png").write_bytes(
                    api.screenshot_bytes(ignore_sprites=True)
                )
                (out / f"{name}-{zoom}x.json").write_text(
                    json.dumps(
                        {"totals": totals, "impostors": impostors, "settled": settled}
                    )
                )
                print(row(name, zoom, totals, impostors, settled), flush=True)
    finally:
        client.stop()


def main() -> int:
    if sys.argv[1:2] == ["--sheets"] and len(sys.argv) == 4:
        before, after = OUT / sys.argv[2], OUT / sys.argv[3]
        sheets(before, after, OUT / "sheets")
        return 0
    args = [a for a in sys.argv[1:] if a != "--table"]
    if len(args) != 1:
        raise SystemExit(__doc__)
    out = OUT / args[0]
    if "--table" not in sys.argv:
        out.mkdir(parents=True, exist_ok=True)
        run(out)
    print(table(out), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
