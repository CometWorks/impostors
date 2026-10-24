"""In-game check of finer levels for a zoomed view: the rifle's iron sight narrows
the client's field of view from 70 to 40 degrees, 1.92 times the size estimate of
the server's default 1080p screen. Same rig as scenario.py, with a 1920x1080
client. Prints one PASS or FAIL line per check and exits with the number of
failures. Screenshots land in ~/.se-test/<task>-shots.

    uv run --no-project --with httpx --with pillow python tests/zoom.py
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

from PIL import Image

os.environ.setdefault("IMP_RESOLUTION", "1920x1080")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import world  # noqa: E402
from scenario import CLIENT, SERVER, SHOTS, check, drawn, wait_stats  # noqa: E402

import scenario  # noqa: E402

# 6 km from the station: level 16 at a 70 degree view, 32 zoomed. Off the x axis, where
# the Near grid hides the far asteroid.
EYE = (world.STATION.position[0] - 6000, 0.0, 400.0)
RIFLE = "AutomaticRifleItem"


def sight_shot(api, name: str, target) -> Path:
    """A screenshot with the target 8 degrees above the screen center, clear of the
    rifle's iron sight, and a crop around it (name-crop.png)"""
    d = [t - e for t, e in zip(target, EYE)]
    distance = math.sqrt(sum(v * v for v in d))
    d = [v / distance for v in d]
    # Down, across the view direction
    down = [d[0] * d[1], d[1] * d[1] - 1, d[2] * d[1]]
    length = math.sqrt(sum(v * v for v in down))
    offset = distance * math.tan(math.radians(8))
    aim = [t + v / length * offset for t, v in zip(target, down)]
    # The character turns over a few frames; the second call settles the aim
    for _ in range(2):
        api.character_look_at(*aim, tolerance=0.2)
        time.sleep(2)
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{name}.png"
    path.write_bytes(api.screenshot_bytes(ignore_sprites=True))

    image = Image.open(path)
    w, h = image.size
    fov = math.radians(float(CLIENT.stats()[0].get("fov", 70)))
    y = h / 2 - math.tan(math.radians(8)) / math.tan(fov / 2) * h / 2
    image.crop((w // 2 - 150, int(y) - 150, w // 2 + 150, int(y) + 150)).save(
        SHOTS / f"{name}-crop.png"
    )
    return path


def lod(impostors: dict, entity_id: int) -> int:
    return int(impostors.get(entity_id, {}).get("lod", 0))


def toggle_zoom(api) -> None:
    """The rifle's secondary action toggles the iron sight"""
    api.set_input_state(mouse_right=True)
    time.sleep(0.3)
    api.clear_input_state()
    time.sleep(0.5)


def zoomed(api, on: bool) -> dict:
    """Toggles the zoom until the mod reports the narrow view (or the wide one)"""
    for _ in range(3):
        totals, _ = CLIENT.stats()
        if (float(totals.get("fov", 0)) < 50) == on:
            return totals
        toggle_zoom(api)
        time.sleep(1.5)
    return CLIENT.stats()[0]


def latency(entity_id: int, finer_than: int, since: int | None = None, timeout=10.0):
    """Seconds from the View message (or from the client tick since) to a shape finer
    than the given level, or None"""
    totals, impostors = wait_stats(
        lambda i: lod(i, entity_id) > finer_than, timeout, interval=0.5
    )
    i = impostors.get(entity_id, {})
    if lod(impostors, entity_id) <= finer_than:
        return None, i
    start = int(totals["viewtick"]) if since is None else since
    return (int(i["shapetick"]) - start) / 60, i


def run(api):
    api.character_teleport(*EYE)
    station, rock, ship = world.STATION, world.ROCK_FAR, world.SHIP
    totals, impostors = wait_stats(
        lambda i: all(drawn(i, e) for e in (station, ship))
        and rock.entity_id in i
        and i[rock.entity_id]["shape"] != "none",
        180,
    )
    station_lod = lod(impostors, station.entity_id)
    rock_lod = lod(impostors, rock.entity_id)
    check(
        "station level 16 unzoomed", station_lod == 16, impostors.get(station.entity_id)
    )
    check("asteroid level 32 unzoomed", rock_lod == 32, impostors.get(rock.entity_id))

    if api.get_view().get("thirdPerson"):
        api.key("V")
    # A creative mode toolbar slot equips the rifle without one in the inventory
    api.set_toolbar_slot(0, f"MyObjectBuilder_PhysicalGunObject/{RIFLE}")
    time.sleep(1)
    api.key("D1")
    time.sleep(3)

    print("screenshot", sight_shot(api, "zoom-Station-wide", station.position))

    # Zooming in while looking at the asteroid: one View message
    print("screenshot", sight_shot(api, "zoom-RockFar-wide", rock.center))
    totals = zoomed(api, True)
    check("narrow view", float(totals.get("fov", 0)) < 50, totals)
    check("mod reports the zoom", totals.get("zoomed") == "1", totals)
    seconds, i = latency(rock.entity_id, rock_lod)
    check(
        "asteroid finer level within 1 s of the zoom",
        seconds is not None and seconds <= 1,
        f"{seconds} s {i}",
    )
    time.sleep(2)
    print("screenshot", sight_shot(api, "zoom-RockFar-narrow", rock.center))

    # Turning onto the station while zoomed sends new View messages. The client tick
    # read before the turn is at most a second old, so this measures a bit long.
    before = int(CLIENT.stats()[0]["tick"])
    api.character_look_at(*station.position, tolerance=1)
    seconds, i = latency(station.entity_id, station_lod, since=before)
    check(
        "station finer level within 1 s of turning to it, plus up to 1 s",
        seconds is not None and seconds <= 2,
        f"{seconds} s {i}",
    )
    time.sleep(2)
    print("screenshot", sight_shot(api, "zoom-Station-narrow", station.position))

    # The others keep streaming: still drawn, with their shapes
    _, impostors = CLIENT.stats()
    check("ship still drawn", drawn(impostors, ship), impostors.get(ship.entity_id))

    # Leaving the zoom keeps the finer levels for a while. The station stays at 32
    # anyway: level 16 is within the hysteresis of 32 (PickLod).
    totals = zoomed(api, False)
    check("wide view again", float(totals.get("fov", 0)) > 60, totals)
    check("mod reports the end of the zoom", totals.get("zoomed") == "0", totals)
    time.sleep(5)
    _, impostors = CLIENT.stats()
    check(
        "asteroid finer level kept after the zoom",
        lod(impostors, rock.entity_id) > rock_lod,
        impostors.get(rock.entity_id),
    )
    print("screenshot", sight_shot(api, "zoom-RockFar-wide-kept", rock.center))

    # ... and the normal level comes back after ZoomKeepSeconds (30)
    _, impostors = wait_stats(lambda i: lod(i, rock.entity_id) == rock_lod, 45)
    check(
        "asteroid normal level after the keep time",
        lod(impostors, rock.entity_id) == rock_lod,
        impostors.get(rock.entity_id),
    )


def sheet() -> Path:
    """The wide crops scaled up by the zoom next to the zoomed ones: what the zoom shows
    with the unzoomed level, and with the finer one"""
    names = [
        ("zoom-Station-wide", True),
        ("zoom-Station-narrow", False),
        ("zoom-RockFar-wide", True),
        ("zoom-RockFar-narrow", False),
    ]
    zoom = math.tan(math.radians(35)) / math.tan(math.radians(20))
    out = Image.new("RGB", (300 * len(names), 300))
    for k, (name, wide) in enumerate(names):
        crop = Image.open(SHOTS / f"{name}-crop.png").convert("RGB")
        if wide:
            size = int(300 / zoom)
            crop = crop.crop(
                (150 - size // 2, 150 - size // 2, 150 + size // 2, 150 + size // 2)
            ).resize((300, 300), Image.NEAREST)
        out.paste(crop, (300 * k, 0))
    path = SHOTS / "zoom-sheet.png"
    out.save(path)
    return path


def main():
    try:
        api = scenario.start()
        run(api)
        print("sheet", sheet())
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()
    print(
        f"{'PASSED' if scenario.failures == 0 else 'FAILED'}: {scenario.failures} failure(s)"
    )
    sys.exit(scenario.failures)


if __name__ == "__main__":
    main()
