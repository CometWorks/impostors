"""The test world: the game's Empty World with the test player, the Impostors
mod under its stand-in Workshop id and a few grids at known distances from
HOME, where the player waits."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import rig

PLAYER = 144115188075870001

HOME = (0.0, 0.0, 0.0)

# Sync distance and view distance of the test world, meters
SYNC_M = 3000
VIEW_M = 15000

SETTINGS = {
    "GameMode": "Creative",
    "OnlineMode": "PUBLIC",
    "MaxPlayers": "8",
    "EnableRespawnShips": "false",
    "EnableSpaceSuitRespawn": "true",
    "TrashRemovalEnabled": "false",
    "EnableCopyPaste": "true",
    "BlockLimitsEnabled": "NONE",
    "EnableResearch": "false",
    "AutoSaveInMinutes": "0",
    "ExperimentalMode": "true",
    "EnableSunRotation": "false",
    "EnableEncounters": "false",
    "GlobalEncounterCap": "0",
    "CargoShipsEnabled": "false",
    "EnableContainerDrops": "false",
    "EnableDrones": "false",
    "EnablePlanetaryEncounters": "false",
    "ProceduralDensity": "0",
    "SyncDistance": str(SYNC_M),
    "ViewDistance": str(VIEW_M),
}

# Hues of ColorMaskHSV (x). Painted with saturation 0.5 and value 0.9 (offsets -0.3 and
# +0.45, see MyColorPickerConstants), bright enough to tell apart on a screenshot.
RED, GREEN, BLUE, YELLOW = 0.0, 0.33, 0.6, 0.15


@dataclass
class Grid:
    name: str
    entity_id: int
    position: tuple[float, float, float]
    size: tuple[int, int, int]
    static: bool
    hue: float
    grid_size: str = "Large"


# Near replicates normally, the others are beyond the sync distance
NEAR = Grid("Near", 120000000000000001, (1500.0, 0.0, 0.0), (4, 3, 6), False, YELLOW)
SHIP = Grid("Ship", 120000000000000002, (0.0, 0.0, -5000.0), (6, 4, 14), False, RED)
STATION = Grid(
    "Station", 120000000000000003, (8000.0, 0.0, 0.0), (60, 16, 40), True, BLUE
)
FAR = Grid("Far", 120000000000000004, (0.0, 2000.0, 12000.0), (10, 8, 24), True, GREEN)
GRIDS = (NEAR, SHIP, STATION, FAR)


def _vec(tag: str, v) -> str:
    return f'<{tag} x="{v[0]}" y="{v[1]}" z="{v[2]}" />'


def _grid_xml(g: Grid) -> str:
    """A hollow box of armor blocks, two tones so the shape reads at a distance"""
    armor = "LargeBlockArmorBlock" if g.grid_size == "Large" else "SmallBlockArmorBlock"
    sx, sy, sz = g.size
    blocks = []
    for x in range(sx):
        for y in range(sy):
            for z in range(sz):
                if 0 < x < sx - 1 and 0 < y < sy - 1 and 0 < z < sz - 1:
                    continue
                # A darker band across the middle
                value = 0.15 if abs(z - sz // 2) <= 1 else 0.45
                blocks.append(
                    '<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_CubeBlock">'
                    f"<SubtypeName>{armor}</SubtypeName>{_vec('Min', (x, y, z))}"
                    f'<ColorMaskHSV x="{g.hue}" y="-0.3" z="{value}" />'
                    "</MyObjectBuilder_CubeBlock>"
                )
    return (
        '<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_CubeGrid">'
        f"<SubtypeName /><EntityId>{g.entity_id}</EntityId>"
        "<PersistentFlags>CastShadows InScene</PersistentFlags>"
        "<PositionAndOrientation>"
        + _vec("Position", g.position)
        + _vec("Forward", (0, 0, -1))
        + _vec("Up", (0, 1, 0))
        + "</PositionAndOrientation>"
        f"<GridSizeEnum>{g.grid_size}</GridSizeEnum>"
        f"<CubeBlocks>{''.join(blocks)}</CubeBlocks>"
        f"<IsStatic>{'true' if g.static else 'false'}</IsStatic>"
        f"<IsUnsupportedStation>{'true' if g.static else 'false'}</IsUnsupportedStation>"
        f"<DisplayName>{g.name}</DisplayName>"
        "<DestructibleBlocks>true</DestructibleBlocks>"
        "</MyObjectBuilder_EntityBase>"
    )


def _insert(text: str, tag: str, content: str) -> str:
    """Puts content at the start of the first <tag> element, opening an empty
    <tag /> if need be"""
    if f"<{tag}>" in text:
        return text.replace(f"<{tag}>", f"<{tag}>{content}", 1)
    new, count = re.subn(rf"<{tag}\s*/>", f"<{tag}>{content}</{tag}>", text, count=1)
    assert count == 1, tag
    return new


def _set(text: str, name: str, value: str) -> str:
    new, count = re.subn(
        rf"<{name}>[^<]*</{name}>|<{name}\s*/>", f"<{name}>{value}</{name}>", text
    )
    if count:
        return new
    return text.replace("</Settings>", f"<{name}>{value}</{name}></Settings>", 1)


def _mods_xml() -> str:
    return (
        '<ModItem FriendlyName="Impostors">'
        f"<Name>{rig.MOD_ID}.sbm</Name><PublishedFileId>{rig.MOD_ID}</PublishedFileId>"
        "<PublishedServiceName>Steam</PublishedServiceName>"
        "</ModItem>"
    )


def prepare(
    world: Path, steam_id: int, name: str, grids=GRIDS, mod: bool = True
) -> None:
    """A fresh copy of the Empty World with the player, the grids and the mod
    unless mod is False"""
    if world.exists():
        shutil.rmtree(world)
    shutil.copytree(rig.EMPTY_WORLD, world)
    (world / "SANDBOX_0_0_0_.sbsB5").unlink(missing_ok=True)
    (world / "Sandbox.sbcB5").unlink(missing_ok=True)

    sector = world / "SANDBOX_0_0_0_.sbs"
    text = sector.read_text(encoding="utf-8")
    text = _insert(text, "SectorObjects", "".join(_grid_xml(g) for g in grids))
    sector.write_text(text, encoding="utf-8")

    for file in ("Sandbox.sbc", "Sandbox_config.sbc"):
        path = world / file
        text = path.read_text(encoding="utf-8")
        for key, value in SETTINGS.items():
            text = _set(text, key, value)
        # The server takes the mods from Sandbox_config.sbc, the clients from Sandbox.sbc
        if mod:
            text = _insert(text, "Mods", _mods_xml())
        if file == "Sandbox.sbc":
            text = _insert(
                text,
                "Identities",
                "<MyObjectBuilder_Identity>"
                f"<IdentityId>{PLAYER}</IdentityId><DisplayName>{name}</DisplayName>"
                "<CharacterEntityId>0</CharacterEntityId>"
                "<BlockLimitModifier>0</BlockLimitModifier>"
                "</MyObjectBuilder_Identity>",
            )
            text, count = re.subn(
                r"<AllPlayersData>(\s*)<dictionary>",
                lambda m: "<AllPlayersData><dictionary>"
                '<item><Key xsi:type="PlayerId">'
                f"<ClientId>{steam_id}</ClientId><SerialId>0</SerialId></Key>"
                '<Value xsi:type="MyObjectBuilder_Player">'
                f"<DisplayName>{name}</DisplayName><IdentityId>{PLAYER}</IdentityId>"
                "<Connected>false</Connected></Value></item>",
                text,
                count=1,
            )
            assert count == 1, "AllPlayersData"
        path.write_text(text, encoding="utf-8")
