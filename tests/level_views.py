"""Impostor levels next to the real asteroids in three views of the "Octahedron - Seed 2" ring
(SE-722): from Gamma Station towards the ring's center, and from 2 km above the ring looking
along it both ways, the Sun on different sides. Needs the save and its Workshop mods, like
tests/ring.py.

    IMP_TASK=se722 uv run --no-project --with httpx --with pillow --with numpy \\
        python tests/level_views.py
    IMP_SETUP=offline ...     # the world offline in a client with the client plugin
    IMP_RUNS=impostor ...     # only the run with the impostors (impostor,real)
    IMP_SETTLE=90 ...         # seconds per view at most (the real run waits that long)
    IMP_ANALYZE=1 ...         # the sheet and the tables from the last runs' files only

Two runs with the same fixed spectator views (the client probe's camera.txt), the Sun's
rotation off so both see the same light:
- impostor: the world's view distance 2 km, so beyond that every asteroid is an impostor.
  Per view a screenshot, one of the sky alone (the impostors hidden), and the client's stats.
  The first view also times the load: seconds from the world being ready and the camera in
  place until 90% and all of the impostors in view are drawn and until their levels stop
  changing. The other views time the same after the camera's jump.
- real: view distance 50 km (the most the game allows) and the impostors hidden, so the
  asteroids up to there are the game's own.

Then per view: the levels drawn by distance and pixel size, and how many are at the level
the server's estimate gives for their size (coarser ones are the quad budget's doing). Per
asteroid that is real in both runs and stands alone on screen, at least 3 pixels in radius:
the pixels brighter than the sky and their light, the impostor's over the real one's (as
tests/compare.py measures), with the medians per level. A sheet per view puts both
screenshots side by side, then up to 6 asteroids per level, real next to impostor.

Files in ~/.se-test/<task>-views/<setup>: screenshots, stats JSON, view-<name>.png sheets and
summary.md. Exits with 0; the numbers are for reading, the run has no pass mark.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import ring  # noqa: E402
from view_levels import drawn_level, level_table, timeline, timing_table  # noqa: E402

SETUP = os.environ.get("IMP_SETUP", "server")
RUNS = os.environ.get("IMP_RUNS", "impostor,real").split(",")
SETTLE = float(os.environ.get("IMP_SETTLE", "90"))
OUT = rig.TEST_ROOT / f"{rig.TASK}-views" / SETUP
HEIGHT = 2000.0
VIEW_DISTANCE = {"impostor": 2000, "real": 50000}
# Luminance over the sky's from which a pixel counts as covered, as in tests/compare.py
COVERED = 0.03
rig.CLIENT_GIB = 10.0


def zones(world: Path) -> dict[str, np.ndarray]:
    text = (world / "SANDBOX_0_0_0_.sbs").read_text(encoding="utf-8")
    found = {}
    for m in re.finditer(
        r'<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_SafeZone">(.*?)'
        r"</MyObjectBuilder_EntityBase>",
        text,
        re.S,
    ):
        name = re.search(r"<DisplayName>([^<]*)</DisplayName>", m.group(1))
        p = re.search(r'<Position x="([^"]+)" y="([^"]+)" z="([^"]+)"', m.group(1))
        if name and p:
            found[name.group(1)] = np.array([float(v) for v in p.groups()])
    return found


def views(world: Path) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """(name, eye, target) of each view"""
    center, normal, side, _ = ring.ring_geometry(world)
    gamma = zones(world)["Gamma Station"]
    inward = (center - gamma) / np.linalg.norm(center - gamma)
    tangent = np.cross(normal, side)
    base = center + side * 121000
    eye = base + normal * HEIGHT
    return [
        ("gamma", gamma + inward * 600, center),
        ("along-a", eye, base + tangent * 20000),
        ("along-b", eye, base - tangent * 20000),
    ]


def set_world(world: Path, view_distance: int) -> None:
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        f = world / name
        text = f.read_text(encoding="utf-8")
        text = re.sub(
            r"<EnableSunRotation>\w+</EnableSunRotation>",
            "<EnableSunRotation>false</EnableSunRotation>",
            text,
        )
        text = re.sub(
            r"<ViewDistance>\d+</ViewDistance>",
            f"<ViewDistance>{view_distance}</ViewDistance>",
            text,
        )
        f.write_text(text, encoding="utf-8")


def camera(client, eye, target) -> None:
    f = (target - eye) / np.linalg.norm(target - eye)
    yaw = math.degrees(math.atan2(f[0], -f[2]))
    pitch = math.degrees(math.asin(f[1]))
    (client.appdata / "camera.txt").write_text(
        f"{eye[0]:.1f} {eye[1]:.1f} {eye[2]:.1f} {yaw:.4f} {pitch:.4f} 0 0 0 0"
    )


def write_mode(client, mode: str) -> None:
    for folder in (client.mod_storage, client.loaded_mod_storage):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "mode").write_text(mode)
    (client.plugin_storage / "Impostors.mode").write_text(mode)


def offline_client():
    client = rig.Client(2, "Viktor", plugin=True, quality="medium", probe=True)
    args = client.args
    client.args = lambda _port: [
        a for a in args(0) if a not in ("--connect", "127.0.0.1:0")
    ]
    return client


def run(kind: str) -> dict:
    """One run: the world, the client, the three views; returns timings and stats per view"""
    OUT.mkdir(parents=True, exist_ok=True)
    result = {"kind": kind, "views": {}}
    server = ring.SERVER if SETUP == "server" else None
    if SETUP == "server":
        import asteroid_pack

        client = ring.CLIENT
        client.quality = "medium"
        world = server.world
        ids = asteroid_pack.setup_world(False)
        set_world(world, VIEW_DISTANCE[kind])
        server.write_plugin_config(
            rig.plugin_config(MaxDistance=300000, MaxImpostorsPerPlayer=4000)
        )
        server.start(timeout=3600)
        rig_start = time.monotonic()
        client.launch(server.port)
        ring.stage_mods(client.appdata / "Mods", ids)
    else:
        import local

        client = offline_client()
        world = client.appdata / "Saves" / ring.WORLD_NAME
        ring.PLUGIN = True  # no Impostors mod in the world, the client plugin draws
        ids = ring.copy_world(world)
        set_world(world, VIEW_DISTANCE[kind])
        for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
            f = world / name
            f.write_text(
                f.read_text(encoding="utf-8").replace(
                    "<OnlineMode>PUBLIC</OnlineMode>",
                    "<OnlineMode>OFFLINE</OnlineMode>",
                ),
                encoding="utf-8",
            )
        client.appdata.mkdir(parents=True, exist_ok=True)
        (client.appdata / "Impostors.cfg").write_text(
            rig.plugin_config(MaxDistance=300000, MaxImpostorsPerPlayer=4000),
            encoding="utf-8",
        )
        for cache in ("ImpostorsCache", "Storage/Impostors/Cache"):
            shutil.rmtree(client.appdata / cache, ignore_errors=True)
        client.launch(0)
        ring.stage_mods(client.appdata / "Mods", ids)
    for cache in client.mod_storage.glob("cache-*"):
        cache.unlink()
    (client.appdata / "camera.txt").unlink(missing_ok=True)
    write_mode(client, "real" if kind == "real" else "")
    shots = {}
    try:
        api = client.wait_api()
        start = time.monotonic()
        if SETUP == "offline":
            api.load(str(world), online_mode="OFFLINE")
            local.wait(lambda: local.past_questions(api), 900, 2)
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        result["world_ready_s"] = time.monotonic() - start
        if SETUP == "server":
            result["server_to_ready_s"] = time.monotonic() - rig_start
        print(f"{kind}: world ready after {result['world_ready_s']:.0f} s", flush=True)
        for name, eye, target in views(world):
            api.character_teleport(*eye)
            time.sleep(2)
            camera(client, eye, target)
            moved = time.monotonic()
            if kind == "impostor":
                timing = timeline(lambda: client.stats()[1], moved, SETTLE)
            else:
                time.sleep(SETTLE)
                timing = {}
            time.sleep(2)
            totals, impostors = client.stats()
            shot = OUT / f"{kind}-{name}.png"
            shot.write_bytes(api.screenshot_bytes(ignore_sprites=True))
            if kind == "impostor":
                write_mode(client, "real")
                time.sleep(3)
                (OUT / f"sky-{name}.png").write_bytes(
                    api.screenshot_bytes(ignore_sprites=True)
                )
                write_mode(client, "")
            result["views"][name] = {
                "timing": timing,
                "eye": eye.tolist(),
                "totals": totals,
                "impostors": {str(i): r for i, r in impostors.items()},
            }
            print(f"{kind} {name}: {json.dumps(timing)}", flush=True)
        (client.appdata / "camera.txt").unlink(missing_ok=True)
    finally:
        client.stop()
        if server:
            server.stop()
    (OUT / f"{kind}.json").write_text(json.dumps(result, indent=1))
    return result


def luminance(path: Path):
    from PIL import Image

    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255
    return rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def analyze() -> str:
    from PIL import Image, ImageDraw, ImageFont

    impostor = json.loads((OUT / "impostor.json").read_text())
    real_run = (
        json.loads((OUT / "real.json").read_text())
        if (OUT / "real.json").exists()
        else None
    )
    font = ImageFont.load_default(size=18)
    lines = [f"# Level views, {SETUP}", ""]
    if "world_ready_s" in impostor:
        lines.append(
            f"World ready {impostor['world_ready_s']:.0f} s after the client started."
        )
        lines.append("")
    lines += timing_table({n: v["timing"] for n, v in impostor["views"].items()})
    all_pairs: dict[int, list] = {}
    for name, v in impostor["views"].items():
        rows = {
            int(i): r
            for i, r in v["impostors"].items()
            if r.get("drawn") == "1" and "screen" in r and r["screen"] != "behind"
        }
        lines += ["", f"## {name}", ""] + level_table(v["impostors"])
        if real_run is None or not (OUT / f"real-{name}.png").exists():
            continue

        # Asteroids alone on screen, real in the real run: within its replication or far plane
        eye = np.array(v["eye"])
        sky = luminance(OUT / f"sky-{name}.png")
        real = luminance(OUT / f"real-{name}.png") - sky
        imp = luminance(OUT / f"impostor-{name}.png") - sky
        everyone = [
            (float(x), float(y), float(r["radius"]))
            for r in v["impostors"].values()
            if "screen" in r and r["screen"] != "behind"
            for x, y in [r["screen"].split(",")]
        ]
        pairs = []
        for i, r in rows.items():
            x, y = (float(c) for c in r["screen"].split(","))
            radius = float(r["radius"])
            pos = np.array([float(c) for c in r["pos"].split(",")])
            if SETUP == "server":
                near = np.all(np.abs(pos - eye) < 50000 / math.sqrt(3) - 1000)
            else:
                near = np.linalg.norm(pos - eye) < 48000
            if radius < 3 or not near or float(r["distance"]) < 3000:
                continue
            half = int(radius * 1.2) + 2
            if any(
                abs(ox - x) < half + orad * 0.8 and abs(oy - y) < half + orad * 0.8
                for ox, oy, orad in everyone
                if (ox, oy, orad) != (x, y, radius)
            ):
                continue
            box = (
                slice(int(y) - half, int(y) + half),
                slice(int(x) - half, int(x) + half),
            )
            if (
                box[0].start < 0
                or box[1].start < 0
                or box[0].stop > sky.shape[0]
                or box[1].stop > sky.shape[1]
            ):
                continue
            rc, mc = real[box], imp[box]
            rp, mp = int((rc > COVERED).sum()), int((mc > COVERED).sum())
            if rp < 4:
                continue
            rl, ml = float(rc[rc > COVERED].sum()), float(mc[mc > COVERED].sum())
            lod = drawn_level(r)
            pairs.append(
                (lod, i, x, y, half, mp / rp, ml / rl, float(r["distance"]), radius)
            )
            all_pairs.setdefault(lod, []).append((mp / rp, ml / rl))
        lines += [
            "",
            "| Level | Asteroids alone on screen | Area ratio median | Light ratio median |",
            "|---:|---:|---:|---:|",
        ]
        for lod in sorted({p[0] for p in pairs}):
            ps = np.array([(p[5], p[6]) for p in pairs if p[0] == lod])
            lines.append(
                f"| {lod} | {len(ps)} | {np.median(ps[:, 0]):.2f} | {np.median(ps[:, 1]):.2f} |"
            )

        # The sheet: both screenshots, then per level up to 6 asteroids, real | impostor
        real_image = Image.open(OUT / f"real-{name}.png").convert("RGB")
        imp_image = Image.open(OUT / f"impostor-{name}.png").convert("RGB")
        w = 960
        h = int(real_image.size[1] * w / real_image.size[0])
        cell = 140
        levels = sorted({p[0] for p in pairs})
        sheet = Image.new(
            "RGB", (2 * w + 20, h + 40 + len(levels) * (cell + 30)), "white"
        )
        draw = ImageDraw.Draw(sheet)
        draw.text((8, 8), f"{SETUP} {name}: real", fill="black", font=font)
        draw.text((w + 28, 8), "impostors", fill="black", font=font)
        sheet.paste(real_image.resize((w, h)), (0, 34))
        sheet.paste(imp_image.resize((w, h)), (w + 20, 34))
        top = h + 40
        full_real = Image.open(OUT / f"real-{name}.png").convert("RGB")
        full_imp = Image.open(OUT / f"impostor-{name}.png").convert("RGB")
        for lod in levels:
            chosen = sorted((p for p in pairs if p[0] == lod), key=lambda p: -p[8])[:6]
            draw.text((8, top + 4), f"level {lod}", fill="black", font=font)
            for k, (_, _, x, y, half, area, light, distance, radius) in enumerate(
                chosen
            ):
                left = 100 + k * (2 * cell + 24)
                for col, image in enumerate((full_real, full_imp)):
                    crop = image.crop(
                        (int(x) - half, int(y) - half, int(x) + half, int(y) + half)
                    )
                    sheet.paste(
                        crop.resize((cell, cell), Image.NEAREST),
                        (left + col * (cell + 2), top + 24),
                    )
                draw.text(
                    (left, top + 4),
                    f"{distance / 1000:.1f} km {radius:.0f} px a{area:.2f} l{light:.2f}",
                    fill="black",
                    font=ImageFont.load_default(size=14),
                )
            top += cell + 30
        sheet.save(OUT / f"view-{name}.png")
        lines += ["", f"Sheet: {OUT / f'view-{name}.png'}"]
    if all_pairs:
        lines += [
            "",
            "## All views",
            "",
            "| Level | Asteroids | Area ratio median | p10 | Light ratio median | p10 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
        for lod in sorted(all_pairs):
            ps = np.array(all_pairs[lod])
            lines.append(
                f"| {lod} | {len(ps)} | {np.median(ps[:, 0]):.2f} | {np.percentile(ps[:, 0], 10):.2f} "
                f"| {np.median(ps[:, 1]):.2f} | {np.percentile(ps[:, 1], 10):.2f} |"
            )
    text = "\n".join(lines) + "\n"
    (OUT / "summary.md").write_text(text)
    return text


def main() -> int:
    if os.environ.get("IMP_ANALYZE") != "1":
        for kind in RUNS:
            run(kind)
    print(analyze(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
