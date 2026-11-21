"""A client whose Impostors mod or asteroid pack doesn't match the server's (SE-1826): the
"Octahedron - Seed 2" world on an isolated Magnetar with the asteroid pack, a mod-only client
joining 10 km above the ring. The client's process CPU and the renderer's stats are sampled
every 10 s.

    IMP_TASK=se1826 IMP_SAVE=<"Octahedron - Seed 2" save> \\
        IMP_PACK_SERVER=<pack folder> IMP_PACK_CLIENT=<pack folder> \\
        IMP_MOD_CLIENT=<ImpostorsMod/Mod folder> \\
        uv run --no-project --with httpx --with pillow --with numpy \\
        python tests/version_mismatch.py <tag>

IMP_PACK_SERVER and IMP_PACK_CLIENT name the pack each side stages (default: <task>-pack),
IMP_MOD_CLIENT the mod the client stages (default: this working copy's). IMP_SETTLE is how
long the client stays (300 s). Prints one line per sample, then a summary line with the
median CPU of the last minute and the shape bytes received in it.

IMP_EXPECT=match or mismatch makes it a test, exit code 0 on PASS:
- match: the client draws asteroid impostors, shows no restart message and reports no
  mismatch;
- mismatch: the client draws no asteroid impostors, gets under 100 KB of shapes in the last
  minute (grids and safe zones), and the player got the restart message, as the mod's popup
  or the server's chat line.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asteroid_pack as ap  # noqa: E402
import rig  # noqa: E402
import ring  # noqa: E402

SERVER = ap.SERVER
CLIENT = ap.CLIENT
SETTLE = float(os.environ.get("IMP_SETTLE", "300"))
PACK_SERVER = Path(os.environ.get("IMP_PACK_SERVER", ap.PACK)).expanduser()
PACK_CLIENT = Path(os.environ.get("IMP_PACK_CLIENT", ap.PACK)).expanduser()
MOD_CLIENT = Path(os.environ.get("IMP_MOD_CLIENT", rig.MOD_FOLDER)).expanduser()
TICKS = os.sysconf("SC_CLK_TCK")
EXPECT = os.environ.get("IMP_EXPECT", "")
RESTART = "Restart Space Engineers"


def link(mods: Path, mod_id: int, folder: Path) -> None:
    target = mods / f"{mod_id}.sbm"
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(folder)


def cpu_seconds(pid: int) -> float:
    """User and system time of the process and its threads"""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / TICKS


def client_pid() -> int:
    """The busiest process in the client's process group: the game"""
    pid = CLIENT.pid()
    pgid = os.getpgid(pid)
    pids = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if os.getpgid(int(p.name)) == pgid:
                    pids.append(int(p.name))
            except OSError:
                pass
    return max(pids, key=cpu_seconds) if pids else pid


def wait_popup(api, tag: str, timeout: float = 60) -> bool:
    """Whether the restart popup opened, with its text, and a screenshot of it"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        screens = api.list_screens()
        for i in range(len(screens)):
            if RESTART in json.dumps([api.get_screen(i), api.get_controls(i)]):
                (rig.TEST_ROOT / f"{rig.TASK}-{tag}-popup.png").write_bytes(
                    api.screenshot_bytes()
                )
                print(f"restart popup open: {screens[i]}", flush=True)
                return True
        time.sleep(2)
    return False


def levels(impostors: dict) -> str:
    counts: dict[int, int] = {}
    for r in impostors.values():
        if r.get("kind") == "Voxel" and r.get("drawn") == "1":
            lod = int(r.get("lod", "-1"))
            counts[lod] = counts.get(lod, 0) + 1
    return ",".join(f"{k}:{v}" for k, v in sorted(counts.items()))


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "run"
    ids = ap.setup_world(True)
    link(SERVER.data / "Mods", ap.PACK_ID, PACK_SERVER)
    SERVER.write_plugin_config(
        rig.plugin_config(
            MaxDistance=500000, MaxImpostorsPerPlayer=4000, MaxQuadsPerFrame=16000
        )
    )
    center, normal, side, _ = ring.ring_geometry(SERVER.world)
    SERVER.start(timeout=3600)
    for cache in CLIENT.mod_storage.glob("cache-*"):
        cache.unlink()
    CLIENT.launch(SERVER.port)
    mods = CLIENT.appdata / "Mods"
    ap.stage(mods, ids)
    link(mods, rig.MOD_ID, MOD_CLIENT)
    link(mods, ap.PACK_ID, PACK_CLIENT)
    cpus = []
    lines = []
    kbs = []
    drawn = 0
    popup = chat = shown = False
    totals = {}
    try:
        api = CLIENT.wait_api()
        rig.wait_world(api, timeout=2400)
        if EXPECT == "mismatch":
            # The mod's popup comes 10 s into the session; focus_gameplay closes it
            popup = wait_popup(api, tag)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        base = center + side * 120000
        tangent = np.cross(normal, side)
        api.character_teleport(*(base + normal * (ap.HEIGHT + 300)))
        time.sleep(5)
        api.character_look_at(*(base + tangent * (ap.HEIGHT + 5000)), tolerance=2)
        pid = client_pid()
        start = time.monotonic()
        last = (start, cpu_seconds(pid))
        while time.monotonic() - start < SETTLE:
            time.sleep(10)
            now = (time.monotonic(), cpu_seconds(pid))
            cpu = (now[1] - last[1]) / (now[0] - last[0]) * 100
            last = now
            cpus.append((now[0] - start, cpu))
            totals, impostors = CLIENT.stats()
            kbs.append((now[0] - start, int(totals.get("shapekb", 0) or 0)))
            drawn = sum(
                1
                for r in impostors.values()
                if r.get("kind") == "Voxel" and r.get("drawn") == "1"
            )
            print(
                f"{tag} t={now[0] - start:.0f}s cpu={cpu:.0f}% "
                f"impostors={totals.get('impostors')} shapes={totals.get('shapes')} "
                f"shapekb={totals.get('shapekb')} packed={totals.get('packed')} "
                f"early={totals.get('early')} picks={totals.get('picks')} "
                f"mismatch={totals.get('mismatch')} levels={levels(impostors)}",
                flush=True,
            )
        screens = api.list_screens()
        messages = json.dumps(api.get_chat_messages())
        chat = RESTART in messages
        print(f"screens: {[x.get('type', x) for x in screens]}", flush=True)
        try:
            (rig.TEST_ROOT / f"{rig.TASK}-{tag}.png").write_bytes(
                api.screenshot_bytes()
            )
        except Exception as e:  # noqa: BLE001
            print(f"no screenshot: {e}", flush=True)
        log = CLIENT.log.read_text(errors="replace") if CLIENT.log.exists() else ""
        shown = "Impostors: restart popup shown" in log
        lines = [
            line.strip()
            for line in log.splitlines()
            if "Impostors:" in line
            and re.search(r"pack|restart|match|version", line, re.I)
        ]
    finally:
        CLIENT.stop()
        SERVER.stop()
    tail = [c for t, c in cpus if t >= SETTLE - 60] or [c for _, c in cpus] or [0]
    server_lines = re.findall(
        r".*(?:mismatch|restart|protocol version).*", ap.log_text(), re.I
    )
    print("\n".join(f"client log: {line}" for line in lines[-8:]), flush=True)
    print("\n".join(f"server log: {line}" for line in server_lines[-4:]), flush=True)
    last = [k for t, k in kbs if t >= SETTLE - 60] or [0]
    received = max(last) - min(last)
    mismatch = totals.get("mismatch") == "1"
    print(
        f"SUMMARY {tag}: client CPU median of the last minute {statistics.median(tail):.0f}% "
        f"(one core = 100%), {received} KB of shapes in it, {drawn} asteroid impostors drawn, "
        f"mismatch {int(mismatch)}, restart popup {int(popup)} (logged {int(shown)}), "
        f"chat line {int(chat)}",
        flush=True,
    )
    if EXPECT == "match":
        ok = drawn > 0 and not (popup or chat or mismatch)
    elif EXPECT == "mismatch":
        # Grids and safe zones still come; the asteroids streamed megabytes before
        ok = drawn == 0 and received < 100 and (popup or chat)
    else:
        return 0
    print(f"{'PASS' if ok else 'FAIL'} {tag}: expected {EXPECT}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
