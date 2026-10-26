"""The test rig: an isolated Magnetar server running Impostors from this
working copy, and headless game clients that join it over DirectTransport and
are driven through the Remote plugin. The clients run without the Impostors
plugin unless asked for it: they draw impostors through the Impostors mod,
which the world lists under a stand-in Workshop id (MOD_ID). Both sides take
it from a local folder instead of the Workshop: the server through the test
probe, the clients through Remote's LocalWorldModsPatch.

Every port, client id and folder comes from a test slot of the workspace
(testing/parallel-test-work/README.md in the se workspace), so a run doesn't
collide with other tests on the machine. IMP_TASK names the slot; it is claimed
on first use and kept until released with slot.sh. IMP_SLOT skips the claim.

The server is server 0 of the slot: Magnetar config folder
~/.se-test/<task>-ds0/magnetar and DS data folder ~/.se-test/<task>-ds0/data.
It compiles Impostors, the test probe (tests/probe) and DirectTransport as
dev folders. The clients are clients 1 and up of the slot:
Pulsar folder ~/.se-test/<task>-c<i>, made on first use with
new-pulsar-instance.sh, user
data ~/.se-test/<task>-c<i>-data. Each compiles Remote and DirectTransport as
dev folders. Every process starts under run-test.sh, which waits for the RAM
floor (SE_RAM_FLOOR_GIB) and holds the shared test lock.

Paths, all overridable by environment variables:

    IMP_WORKSPACE             the se workspace, for its testing/ (default: the
                              folder that holds se1/plugins/impostors)
    IMP_MAGNETAR              Magnetar install (~/.config/Magnetar)
    IMP_PULSAR                Pulsar install the client folders are copied from
    IMP_DS64                  DedicatedServer64 of the Steam install
    IMP_BIN64                 Bin64 of the game client
    IMP_GAME_CFG              SpaceEngineers.cfg the clients start from
    IMP_REMOTE_REPO           Remote plugin working copy
    IMP_DIRECT_TRANSPORT_REPO DirectTransport working copy
    IMP_MOD_FOLDER            the mod the worlds list (ImpostorsMod/Mod of this
                              working copy), for another version of it

IMP_RESOLUTION sets the clients' screen size (1280x720). IMP_DISPLAY=:N gives
the clients a real window on that X display instead of --headless. Headless
clients can't render on the NVIDIA GPU (Remote drops its Vulkan driver),
windowed ones can; with the XWayland display of a headless weston (weston
--backend=headless --xwayland) nothing shows on the desktop.

The server and the clients download hub plugins from GitHub. On a machine
GitHub's API rate limits, export PULSAR_GITHUB_TOKEN: Pulsar and Magnetar read
it from the environment the rig passes on. Or put the plugins Magnetar can't
download into IMP_SERVER_DEV_FOLDERS as dev folders: name|folder|file entries
separated by ";".
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
PROBE = TESTS / "probe"
WORKSPACE = Path(os.environ.get("IMP_WORKSPACE", REPO.parents[2]))
MOD_FOLDER = Path(os.environ.get("IMP_MOD_FOLDER", REPO / "ImpostorsMod" / "Mod"))
PARALLEL = WORKSPACE / "testing" / "parallel-test-work"
NEW_PULSAR = WORKSPACE / "testing" / "pulsar-dev-instances" / "new-pulsar-instance.sh"
MAGNETAR = Path(os.environ.get("IMP_MAGNETAR", HOME / ".config/Magnetar"))
PULSAR = Path(os.environ.get("IMP_PULSAR", HOME / ".config/Pulsar"))
STEAM = HOME / ".steam/steam/steamapps/common"
if not STEAM.exists():
    STEAM = HOME / ".steam/debian-installation/steamapps/common"
DS64 = Path(
    os.environ.get(
        "IMP_DS64", STEAM / "SpaceEngineersDedicatedServer/DedicatedServer64"
    )
)
BIN64 = Path(os.environ.get("IMP_BIN64", STEAM / "SpaceEngineers/Bin64"))
EMPTY_WORLD = DS64.parent / "Content" / "CustomWorlds" / "Empty World"
GAME_CFG = Path(
    os.environ.get("IMP_GAME_CFG", HOME / ".config/SpaceEngineers/SpaceEngineers.cfg")
)
REMOTE_REPO = Path(os.environ.get("IMP_REMOTE_REPO", HOME / "ws/se/se1/plugins/remote"))
DIRECT_TRANSPORT_REPO = Path(
    os.environ.get("IMP_DIRECT_TRANSPORT_REPO", WORKSPACE / "se1" / "direct-transport")
)

sys.path.insert(0, str(REMOTE_REPO / "skills" / "se-remote"))

from se_remote import RemoteAPI  # noqa: E402

TASK = os.environ.get("IMP_TASK", "impostors")
DISPLAY = os.environ.get("IMP_DISPLAY")
# Screen size of the clients, WIDTHxHEIGHT
RESOLUTION = os.environ.get("IMP_RESOLUTION", "1280x720")
TEST_ROOT = HOME / ".se-test"

IMPOSTORS_ID = "66AA3D09-1732-445D-A2B6-A377B31AE76C"
PROBE_ID = "5B1E3C7A-9D2F-4E61-8A47-2C0F9B6D1E35"
CLIENT_PROBE = TESTS / "clientprobe"
CLIENT_PROBE_ID = "7B028E03-D161-477F-BF54-F6B8AD94BFDE"

# Stand-in Workshop id of the Impostors mod in the test world
MOD_ID = 900000199

# Measured RSS (testing/parallel-test-work/README.md): rendering headless
# client, the server is a guess
CLIENT_GIB = 5.5
SERVER_GIB = 4.0


def _claim_slot() -> dict[str, int]:
    slot = os.environ.get("IMP_SLOT") or _run(PARALLEL / "slot.sh", "claim", TASK)
    text = _run(PARALLEL / "slot.sh", "env", slot)
    values = {k: int(v) for k, v in re.findall(r"(\w+_BASE)=(\d+)", text)}
    values["SLOT"] = int(slot)
    return values


def _run(*command) -> str:
    return subprocess.run(
        [str(c) for c in command], check=True, capture_output=True, text=True
    ).stdout.strip()


SLOT = _claim_slot()


# ---------------------------------------------------------------------------
# Processes. Each runs in a session of its own under run-test.sh, so the whole
# process group is ours to stop.
# ---------------------------------------------------------------------------


def _alive(pid_file: Path, marker: str) -> int | None:
    try:
        pid = int(pid_file.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if marker.encode() in cmdline else None


def start_process(
    command, need_gib: float, cwd: Path, log: Path, pid_file: Path, env=None
):
    with open(log, "w") as out:
        process = subprocess.Popen(
            [str(PARALLEL / "run-test.sh"), str(need_gib), "--", *map(str, command)],
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    pid_file.write_text(str(process.pid))


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_process(pid_file: Path, marker: str, timeout: float = 60.0) -> None:
    """SIGTERM to the process group we started, SIGKILL after the timeout"""
    pid = _alive(pid_file, marker)
    if pid is None:
        pid_file.unlink(missing_ok=True)
        return
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while _group_alive(pid):
        if time.monotonic() > deadline:
            os.killpg(pid, signal.SIGKILL)
            break
        time.sleep(1)
    pid_file.unlink(missing_ok=True)


def profile_xml(dev_folders) -> str:
    folders = "".join(
        f"<LocalFolderConfig><Id>{i}</Id><DebugBuild>false</DebugBuild></LocalFolderConfig>"
        for i in dev_folders
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<Profile xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        f"<Name>Current</Name><GitHub /><DevFolder>{folders}</DevFolder>"
        "<Local /><Mods /></Profile>\n"
    )


def write_sources(sources: Path, plugins: dict[str, tuple[Path, str]]) -> None:
    """The loader's dev folder sources: exactly these, name -> (folder, file)"""
    tree = ET.parse(sources)
    root = tree.getroot()
    local = root.find("LocalPluginSources")
    if local is not None:
        root.remove(local)
    local = ET.SubElement(root, "LocalPluginSources")
    for name, (folder, file) in plugins.items():
        plugin = ET.SubElement(local, "LocalPlugin")
        for tag, value in (
            ("Name", name),
            ("Folder", str(folder)),
            ("File", file),
            ("Enabled", "true"),
        ):
            ET.SubElement(plugin, tag).text = value
    tree.write(sources, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Plugin config
# ---------------------------------------------------------------------------


def _value(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def plugin_config(**options) -> str:
    """Impostors.cfg with these options, by option name; the rest keep their
    defaults"""
    values = "".join(f"<{k}>{_value(v)}</{k}>" for k, v in options.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<PluginConfig xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        f"{values}</PluginConfig>\n"
    )


# The server's cache of built shapes (and a WA's index), relative to its data folder: one
# subfolder per first 2 hex digits of the id (SE-700), so look for files with rglob
CACHE_FOLDER = Path("Storage", "Impostors", "Cache")


def cache_folder(data: Path) -> Path:
    return data / CACHE_FOLDER


def clear_cache(data: Path) -> None:
    """Wipes the shape cache, and the folder before SE-700, which a start would move in"""
    shutil.rmtree(cache_folder(data), ignore_errors=True)
    shutil.rmtree(data / "ImpostorsCache", ignore_errors=True)


def write_build_times(server, target: Path) -> Path | None:
    """The server's build times as a Markdown table (tests/build_times.py)"""
    log = server.game_log()
    if log is None:
        return None
    result = subprocess.run(
        [sys.executable, str(TESTS / "build_times.py"), str(log)],
        capture_output=True,
        text=True,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.stdout or result.stderr)
    return target


def stage_mod(mods_folder: Path) -> None:
    """Links the mod of this working copy into a Mods folder under its
    stand-in Workshop id"""
    mods_folder.mkdir(parents=True, exist_ok=True)
    link = mods_folder / f"{MOD_ID}.sbm"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(MOD_FOLDER)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class Server:
    """Server 0 of the slot"""

    def __init__(self):
        self.root = TEST_ROOT / f"{TASK}-ds0"
        self.config = self.root / "magnetar"
        self.data = self.root / "data"
        self.pid_file = self.root / "server.pid"
        self.log = self.root / "server.log"
        self.port = SLOT["DS_SERVER_PORT_BASE"]
        self.world = self.data / "Saves" / "ImpostorsTest"
        self.plugin_cfg = self.data / "Impostors.cfg"

    def game_log(self) -> Path | None:
        logs = sorted(self.data.glob("SpaceEngineersDedicated*.log"))
        return logs[-1] if logs else None

    def prepare(self, admins) -> None:
        """Magnetar's config, the dedicated config and the staged mod. The
        world is written separately, see world.prepare."""
        if not (self.config / "Sources").exists():
            # The install's plugin sources, hub cache and compiled hub plugins
            self.config.mkdir(parents=True, exist_ok=True)
            template = MAGNETAR / "Magnetar"
            shutil.copytree(template / "Sources", self.config / "Sources")
            shutil.copy(template / "config.xml", self.config / "config.xml")
            if (template / "GitHub").exists():
                shutil.copytree(template / "GitHub", self.config / "GitHub")
        sources = {
            "impostors": (REPO, "ImpostorsServer.xml"),
            "impostors-probe": (PROBE, "ImpostorsProbe.xml"),
            "direct-transport": (DIRECT_TRANSPORT_REPO, "DirectTransportServer.xml"),
        }
        for entry in filter(
            None, os.environ.get("IMP_SERVER_DEV_FOLDERS", "").split(";")
        ):
            name, folder, file = entry.split("|")
            sources[name] = (Path(folder), file)
        write_sources(self.config / "Sources" / "sources.xml", sources)
        (self.config / "Profiles").mkdir(exist_ok=True)
        (self.config / "Profiles" / "Current.xml").write_text(
            profile_xml(["direct-transport", IMPOSTORS_ID, PROBE_ID]),
            encoding="utf-8",
        )
        admins_xml = "".join(f"<unsignedLong>{a}</unsignedLong>" for a in admins)
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "SpaceEngineers-Dedicated.cfg").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<MyConfigDedicated xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
            f"<IP>127.0.0.1</IP><SteamPort>{SLOT['DS_STEAM_PORT_BASE']}</SteamPort>"
            f"<ServerPort>{self.port}</ServerPort>"
            f"<Administrators>{admins_xml}</Administrators><Banned />"
            f"<ServerName>Impostors Test {TASK}</ServerName>"
            "<WorldName>ImpostorsTest</WorldName>"
            "<PauseGameWhenEmpty>false</PauseGameWhenEmpty>"
            "<AutoRestartEnabled>false</AutoRestartEnabled>"
            "<IgnoreLastSession>true</IgnoreLastSession>"
            "<RemoteApiEnabled>false</RemoteApiEnabled>"
            f"<LoadWorld>{self.world}</LoadWorld>"
            "</MyConfigDedicated>\n",
            encoding="utf-8",
        )
        stage_mod(self.data / "Mods")

    def write_plugin_config(self, text: str | None) -> None:
        """Impostors.cfg in the server's data folder, None for no file"""
        if text is None:
            self.plugin_cfg.unlink(missing_ok=True)
        else:
            self.plugin_cfg.write_text(text, encoding="utf-8")

    def pid(self) -> int | None:
        return _alive(self.pid_file, str(self.config))

    def start(self, timeout: float = 2400.0) -> None:
        """Starts the server and waits for its world. The RAM guard may hold
        the start for a while."""
        if self.pid():
            raise RuntimeError(f"The test server is already running, pid {self.pid()}")
        # The game log of the last start, kept for looking into a failure
        old_logs = self.root / "old-logs"
        old_logs.mkdir(exist_ok=True)
        for old in self.data.glob("SpaceEngineersDedicated*.log"):
            old.replace(old_logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{old.name}")
        start_process(
            [
                MAGNETAR / "MagnetarInterim.bin",
                "-multiInstance",
                "-stableLogs",
                "-noimplicitmod",
                "-consent",
                "deny",
                "-config",
                self.config,
                "-ds64",
                DS64,
                "-path",
                self.data,
            ],
            SERVER_GIB,
            MAGNETAR,
            self.log,
            self.pid_file,
            {"SE_DIRECT_TRANSPORT": "1"},
        )
        deadline = time.monotonic() + timeout
        while "Game ready" not in self.log.read_text(errors="replace"):
            # run-test.sh execs flock, which execs the server: /proc/<pid>/cmdline can
            # be unreadable for a moment, so a single miss isn't an exit
            if self.pid() is None:
                time.sleep(2)
                if self.pid() is None:
                    raise RuntimeError(f"The server exited, see {self.log}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"The server did not get ready, see {self.log}")
            time.sleep(2)

    def stop(self) -> None:
        """SIGTERM makes Magnetar save the world and quit"""
        stop_process(self.pid_file, str(self.config), timeout=180)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class Client:
    """Client i of the slot. With plugin=True it also runs the Impostors client
    plugin, otherwise it is vanilla as far as Impostors goes. With probe=True it runs
    the test probe of tests/clientprobe, which set_fov needs."""

    def __init__(
        self,
        index: int,
        name: str,
        plugin: bool = False,
        quality: str = "minimal",
        probe: bool = False,
    ):
        self.index = index
        self.name = name
        self.plugin = plugin
        self.probe = probe
        self.quality = quality
        self.pulsar = TEST_ROOT / f"{TASK}-c{index}"
        self.launcher = self.pulsar / "Interim.bin"
        self.appdata = TEST_ROOT / f"{TASK}-c{index}-data"
        self.port = SLOT["REMOTE_PORT_BASE"] + index
        self.steam_id = SLOT["CLIENT_ID_BASE"] + index
        self.pid_file = self.appdata / "game.pid"
        self.launch_log = self.appdata / "launch.log"
        self.log = self.appdata / "SpaceEngineers.log"
        # Local storage of the mod, named after its script assembly
        self.mod_storage = self.appdata / "Storage" / f"{MOD_ID}.sbm_Impostors"
        # The same for the mod the client plugin loads offline and on a lobby host
        self.loaded_mod_storage = self.appdata / "Storage" / "local_Impostors_Impostors"
        # The client plugin's copy of the renderer: the global storage, its files prefixed
        self.plugin_storage = self.appdata / "Storage"

    def args(self, server_port: int) -> list:
        return [
            "-multiInstance",
            "-bin64",
            BIN64,
            "-lazySteam",
            "-noprompt",
            "-noupdate",
            "-nosplash",
            "-stablelogs",
            "-sources",
            "--no-steam",
            "--client-id",
            self.steam_id,
            "--client-name",
            self.name,
            "-appdata",
            self.appdata,
            "--quality",
            self.quality,
            "--resolution",
            RESOLUTION,
            "--no-audio",
            *([] if DISPLAY else ["--headless"]),
            "--connect",
            f"127.0.0.1:{server_port}",
        ]

    def _ensure_pulsar(self) -> None:
        if not self.launcher.exists():
            subprocess.run(
                [str(NEW_PULSAR), str(self.pulsar), str(PULSAR)],
                check=True,
                stdout=subprocess.DEVNULL,
            )

    def _write_configs(self) -> None:
        legacy = self.pulsar / "Legacy"
        dev_folders = ["remote", "direct-transport"]
        sources = {
            "remote": (REMOTE_REPO, "Remote.xml"),
            "direct-transport": (DIRECT_TRANSPORT_REPO, "DirectTransportClient.xml"),
        }
        if self.plugin:
            dev_folders.append(IMPOSTORS_ID)
            sources["impostors"] = (REPO, "ImpostorsClient.xml")
        if self.probe:
            dev_folders.append(CLIENT_PROBE_ID)
            sources["impostors-client-probe"] = (
                CLIENT_PROBE,
                "ImpostorsClientProbe.xml",
            )
        (legacy / "Profiles" / "Current.xml").write_text(
            profile_xml(dev_folders), encoding="utf-8"
        )
        write_sources(legacy / "Sources" / "sources.xml", sources)
        stage_mod(self.appdata / "Mods")
        # The mod writes stats.txt into its local storage when this file is there
        self.mod_storage.mkdir(parents=True, exist_ok=True)
        (self.mod_storage / "debug").write_text("")
        (self.mod_storage / "stats.txt").unlink(missing_ok=True)
        # compare.py's last mode would carry over into the next run of any test
        (self.mod_storage / "mode").unlink(missing_ok=True)
        self.loaded_mod_storage.mkdir(parents=True, exist_ok=True)
        (self.loaded_mod_storage / "debug").write_text("")
        (self.loaded_mod_storage / "stats.txt").unlink(missing_ok=True)
        (self.loaded_mod_storage / "mode").unlink(missing_ok=True)
        (self.plugin_storage / "Impostors.stats.txt").unlink(missing_ok=True)
        (self.plugin_storage / "Impostors.mode").unlink(missing_ok=True)
        if self.plugin:
            (self.plugin_storage / "Impostors.debug").write_text("")
        self.set_fov(None)
        self.appdata.mkdir(parents=True, exist_ok=True)
        (self.appdata / "Remote.cfg").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<PluginConfig><Enabled>true</Enabled><ListenIP>127.0.0.1</ListenIP>"
            f"<ListenPort>{self.port}</ListenPort>"
            "<AdminPassword>SpaceEngineers</AdminPassword>"
            "<GridGetRateLimit>1000</GridGetRateLimit><GridGetBurstSize>2000</GridGetBurstSize>"
            "<GridSetRateLimit>200</GridSetRateLimit><GridSetBurstSize>400</GridSetBurstSize>"
            "</PluginConfig>\n",
            encoding="utf-8",
        )
        game_cfg = self.appdata / "SpaceEngineers.cfg"
        if not game_cfg.exists():
            shutil.copy(GAME_CFG, game_cfg)

    def pid(self) -> int | None:
        return _alive(self.pid_file, str(self.launcher))

    def launch(self, server_port: int) -> None:
        if self.pid():
            raise RuntimeError(f"Client {self.index} is already running")
        self._ensure_pulsar()
        self._write_configs()
        self.log.unlink(missing_ok=True)
        start_process(
            [self.launcher, *self.args(server_port)],
            CLIENT_GIB,
            self.pulsar,
            self.launch_log,
            self.pid_file,
            env=(
                {
                    "DISPLAY": DISPLAY,
                    "SDL_VIDEO_DRIVER": "x11",
                    "SDL_VIDEODRIVER": "x11",
                }
                if DISPLAY
                else None
            ),
        )

    def stop(self) -> None:
        stop_process(self.pid_file, str(self.launcher))

    def stats_files(self) -> tuple[Path, ...]:
        return (
            self.mod_storage / "stats.txt",
            self.loaded_mod_storage / "stats.txt",
            self.plugin_storage / "Impostors.stats.txt",
        )

    def stats(self) -> tuple[dict, dict[int, dict]]:
        """The renderer's stats.txt, the mod's, the one the plugin loaded or else the
        client plugin's: the totals and each impostor by id"""
        for path in self.stats_files():
            try:
                lines = path.read_text().splitlines()
                break
            except FileNotFoundError:
                continue
        else:
            return {}, {}
        parse = lambda line: dict(kv.split("=", 1) for kv in line.split())
        impostors = {}
        for line in lines[1:]:
            fields = parse(line)
            impostors[int(fields["id"])] = fields
        return parse(lines[0]) if lines else {}, impostors

    def set_fov(self, degrees: float | None) -> None:
        """The camera's vertical field of view through the client probe, None for the
        player's own; applied within half a second"""
        file = self.appdata / "fov.txt"
        if degrees is None:
            file.unlink(missing_ok=True)
        else:
            file.write_text(f"{degrees:.4f}")

    def api(self) -> RemoteAPI:
        return RemoteAPI(
            f"http://127.0.0.1:{self.port}", username="admin", password="SpaceEngineers"
        )

    def wait_api(self, timeout: float = 2400.0) -> RemoteAPI:
        """The RAM guard may hold the launch for a while, so this waits as long
        as the process lives"""
        api = self.api()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.pid():
                raise RuntimeError(f"Client {self.index} exited, see {self.launch_log}")
            try:
                api.ping()
                return api
            except Exception:  # noqa: BLE001 -- not listening yet
                time.sleep(3)
        raise TimeoutError(f"Client {self.index} has no Remote API")


def wait_world(api: RemoteAPI, timeout: float = 900.0) -> None:
    """Waits until the session is ready, clicking OK on any message box"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            boxes = [
                s
                for s in api.list_screens()
                if s.get("type") == "MyGuiScreenMessageBox"
            ]
            for box in boxes:
                api.control_click(text="OK", screen=box["index"])
            if not boxes and api.get_state().get("ready"):
                return
        except Exception:  # noqa: BLE001 -- the API times out while a world loads
            pass
    raise TimeoutError("The world did not become ready")


def ensure_character(api: RemoteAPI, timeout: float = 300.0) -> dict:
    """Respawns at the first spawn option until there is a live character"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            character = api.get_character()
            if character.get("state") not in (None, "dead"):
                return character
        except Exception:  # noqa: BLE001 -- 503 without a character
            pass
        try:
            screens = api.list_screens()
            medical = next(
                (i for i, s in enumerate(screens) if "Medical" in s.get("type", "")),
                None,
            )
            if medical is not None:
                for button in ("Join", "Respawn"):
                    try:
                        api.control_click(text=button, screen=medical)
                        break
                    except Exception:  # noqa: BLE001 -- the other page of the screen
                        continue
        except Exception:  # noqa: BLE001 -- slow while the world streams in
            pass
        time.sleep(3)
    raise TimeoutError("No live character")


def focus_gameplay(api: RemoteAPI, timeout: float = 30.0) -> None:
    """Closes the welcome screen and anything else above the gameplay screen"""
    keep = ("MyGuiScreenGamePlay", "MyGuiScreenHudSpace")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        screens = [s.get("type") for s in api.list_screens()]
        extra = [i for i, kind in enumerate(screens) if kind not in keep]
        if not extra:
            return
        if "MyGuiScreenLoading" not in screens:
            api.close_screen(extra[-1])
        time.sleep(0.5)
    raise TimeoutError("Could not get back to the gameplay screen")
