"""In-game check of the client plugin's own draw pass (SE-1541): it draws the impostor
quads outside the game's 32768 billboards per frame, up to PluginQuadsPerFrame of them.
Offline, a headless client with the Impostors client plugin and the test probe, like
tests/local.py.

    IMP_TASK=se1541 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/plugin_quads.py
    IMP_PLUGIN_QUADS=16000 ...   # PluginQuadsPerFrame of the client (64000)
    IMP_STRESS=16000,64000 ...   # the stress counts (16000,64000,128000,256000), "" for none
    IMP_TAG, IMP_KEEP as in tests/transparent.py

First the renderer: twelve asteroids 60 km away fill a zoomed view, and their impostors
together stay within PluginQuadsPerFrame, over 32768 when that allows, while the game
counts none of them as billboards.

Then the pass alone: the probe adds a block of magenta quads with the impostors' material,
as many as each stress count, next to 30000 other billboards. Every quad must show (the
block's area on the screenshot grows with the count) and no other billboard may be cut.
Prints the plugin's time per frame, the frame rate and the simulation speed at each count,
one PASS or FAIL line per check, and exits with the number of failures.
"""

from __future__ import annotations

import io
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local  # noqa: E402
import rig  # noqa: E402
import world  # noqa: E402

TAG = os.environ.get("IMP_TAG", "shot")
QUADS = int(os.environ.get("IMP_PLUGIN_QUADS", "64000"))
STRESS = [
    int(n)
    for n in os.environ.get("IMP_STRESS", "16000,64000,128000,256000").split(",")
    if n
]
LIMIT = 32768
# Other billboards next to the stress quads, half PostPP and half LDR
OTHERS = 30000
SHOTS = rig.TEST_ROOT / f"{rig.TASK}-shots"
HOST = rig.Client(1, "Alpha", plugin=True, probe=True)
WORLD = HOST.appdata / "Saves" / "Impostors quads"
BILLBOARDS = HOST.appdata / "billboards.txt"
STATS = HOST.appdata / "billboard-stats.txt"

# Four by three, 60 km away
ROCKS = tuple(
    world.Asteroid(
        f"Rock{i}",
        130000000000000051 + i,
        (60000.0 - 256, (i // 4 - 1) * 440.0 - 256, (i % 4 - 1.5) * 440.0 - 256),
        ("Asteroid512-001.vx2", "Asteroid512-013.vx2")[i % 2],
    )
    for i in range(12)
)
CENTER = (60000.0, 0.0, 0.0)
AWAY = (0.0, 0.0, -60000.0)
SUMMARY = re.compile(
    r"Impostor quads drawn by the plugin in the last \d+ s: (\d+) per frame, "
    r"(\d+) at most; ([\d.]+) ms per frame, ([\d.]+) ms at most; "
    r"([\d.]+) frames per second"
)


def shoot(api, name: str) -> np.ndarray:
    data = api.screenshot_bytes(ignore_sprites=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    (SHOTS / f"{TAG}-{name}.png").write_bytes(data)
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.int16)


def samples(seconds: float) -> list[dict]:
    """The probe's billboard stats, one row a second"""
    rows, last = [], None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            text = STATS.read_text()
        except FileNotFoundError:
            text = ""
        if text and text != last:
            last = text
            fields = dict(kv.split("=", 1) for kv in text.split())
            fields.pop("impostorsdrawn", None)
            rows.append({k: int(v) for k, v in fields.items()})
        time.sleep(0.3)
    return rows


def summaries() -> list[tuple]:
    """The plugin's summaries, one every 10 seconds: quads per frame, the most in one,
    its ms per frame, the most in one, frames per second"""
    return [
        (int(a), int(b), float(c), float(d), float(e))
        for a, b, c, d, e in SUMMARY.findall(HOST.log.read_text(errors="replace"))
    ]


def next_summary(timeout: float = 120) -> tuple | None:
    """The third summary from now on: the first one covers frames from before, and the
    second one the frames while the game's pools grew"""
    before = len(summaries())
    found = local.wait(lambda: len(summaries()) >= before + 3, timeout, 2)
    return summaries()[-1] if found else None


def sim_speed(api) -> float:
    return float(api.get_state().get("simSpeed", -1))


def run_renderer(api) -> None:
    impostors = local.shown(HOST, (r.entity_id for r in ROCKS), timeout=900)
    missing = [r.name for r in ROCKS if not local.has_shape(impostors, r.entity_id)]
    local.check("every asteroid has an impostor", not missing, missing)

    api.character_look_at(*CENTER, tolerance=0.2)
    HOST.set_fov(2.0)
    time.sleep(30)
    totals, _ = HOST.stats()
    rows = samples(5)
    print(
        f"asteroids: plugin {next_summary()}, sim speed {sim_speed(api):.2f}",
        flush=True,
    )
    shoot(api, "asteroids")
    HOST.set_fov(None)

    quads, budget = int(totals.get("quads", 0)), int(totals.get("budget", 0))
    detail = (
        f"quads={quads} budget={budget} drawn={totals.get('drawn')} "
        f"inview={totals.get('inview')} overbudget={totals.get('overbudget')} "
        f"coarser={totals.get('coarser')} drawus={totals.get('drawus')}"
    )
    local.check("the renderer's cap is PluginQuadsPerFrame", budget == QUADS, detail)
    local.check(
        "the impostors stay within it"
        + (", over the game's limit" if QUADS > LIMIT else ""),
        0 < quads <= QUADS and (QUADS <= LIMIT or quads > LIMIT),
        detail,
    )
    counted = max((r["maxtotal"] for r in rows), default=-1)
    local.check(
        "the game counts none of them as billboards",
        0 <= counted < 1000,
        f"{counted} billboards in the game's frame",
    )


def run_stress(api) -> None:
    api.character_look_at(*AWAY, tolerance=1)
    HOST.set_fov(70.0)
    areas = []
    for count in STRESS:
        BILLBOARDS.write_text(
            f"impostor {count} postpp {OTHERS // 2} ldr {OTHERS // 2}\n"
        )
        summary = next_summary()
        rows = samples(10)
        shot = shoot(api, f"stress-{count}")
        magenta = (shot[:, :, 0] > 150) & (shot[:, :, 2] > 150) & (shot[:, :, 1] < 100)
        areas.append(int(magenta.sum()))
        print(
            f"stress {count}: plugin {summary}, sim speed {sim_speed(api):.2f}, "
            f"game billboards {max((r['maxtotal'] for r in rows), default=-1)}, "
            f"{areas[-1]} magenta pixels",
            flush=True,
        )
        local.check(
            f"{count}: the plugin drew every quad",
            summary is not None and summary[1] == count,
            summary,
        )
        local.check(
            f"{count}: no other billboard cut",
            bool(rows)
            and all(r["cutothers"] == 0 and r["maxtotal"] <= LIMIT for r in rows),
            rows[-1:] if rows else "no stats",
        )
    BILLBOARDS.write_text("postpp 0 ldr 0\n")
    HOST.set_fov(None)
    if len(STRESS) > 1:
        ratios = [a / max(1, areas[0]) for a in areas]
        wanted = [n / STRESS[0] for n in STRESS]
        local.check(
            "the block's area on screen grows with the count",
            all(abs(r / w - 1) < 0.2 for r, w in zip(ratios, wanted)),
            f"areas {areas}, ratios {[round(r, 2) for r in ratios]}, wanted {wanted}",
        )


def main() -> int:
    world.prepare(WORLD, HOST.steam_id, HOST.name, grids=(), asteroids=ROCKS, mod=False)
    HOST.appdata.mkdir(parents=True, exist_ok=True)
    (HOST.appdata / "Impostors.cfg").write_text(
        rig.plugin_config(PluginQuadsPerFrame=QUADS), encoding="utf-8"
    )
    (HOST.plugin_storage / "Impostors.ontop").unlink(missing_ok=True)
    BILLBOARDS.write_text("postpp 0 ldr 0\n")
    args = HOST.args
    HOST.args = lambda _port: [
        a for a in args(0) if a not in ("--connect", "127.0.0.1:0")
    ]
    HOST.launch(0)
    try:
        api = HOST.wait_api()
        api.load(str(WORLD), online_mode="OFFLINE")
        local.wait(lambda: local.past_questions(api), 600, 2)
        rig.ensure_character(api)
        rig.wait_world(api)
        rig.focus_gameplay(api)
        api.character_teleport(*world.HOME)
        if api.get_view().get("thirdPerson"):
            api.key("V")
        run_renderer(api)
        if STRESS:
            run_stress(api)
    finally:
        BILLBOARDS.unlink(missing_ok=True)
        if os.environ.get("IMP_KEEP") != "1":
            HOST.stop()
    print(
        f"{'PASSED' if local.failures == 0 else 'FAILED'}: {local.failures} failure(s)"
    )
    return local.failures


if __name__ == "__main__":
    sys.exit(main())
