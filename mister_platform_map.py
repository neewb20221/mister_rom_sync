#!/usr/bin/env python3
"""Map external DAT platform names (No-Intro, Redump, …) → MiSTer /games/ folders."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

# Folders used by MiSTer cores / MiSTer Organize (case as on SD card).
MISTER_CORE_FOLDERS = frozenset(
    {
        "AO486",
        "ATARI2600",
        "ATARI5200",
        "ATARI7800",
        "Arcadia",
        "Astrocade",
        "AtariLynx",
        "AVision",
        "C64",
        "CD-i",
        "ChannelF",
        "Coleco",
        "CreatiVision",
        "FDS",
        "GAMEBOY",
        "GAMEBOY2P",
        "GBA",
        "GBA2P",
        "GBC",
        "GBC2P",
        "Gamate",
        "GameGear",
        "Genesis",
        "Intellivision",
        "Jaguar",
        "Jaguar-CD",
        "MACPLUS",
        "MegaCD",
        "MegaDrive",
        "MegaDuck",
        "MSX",
        "N64",
        "NEOGEO",
        "NES",
        "NGP",
        "NGPC",
        "ODYSSEY2",
        "PSX",
        "PokemonMini",
        "S32X",
        "SGB",
        "SG1000",
        "SMS",
        "SNES",
        "Saturn",
        "Spectrum",
        "TGFX16",
        "TGFX16-CD",
        "TurboExpress",
        "VECTREX",
        "WonderSwan",
        "WonderSwanColor",
        "NeoGeo",
        "NeoGeo-CD",
        "NeoGeoPocket",
        "Satellaview",
        "SufamiTurbo",
        "Videopac",
        "Amiga",
        "Atari800",
        "BBCMicro",
        "X68000",
        "ZX81",
        "VIC20",
        "PET2001",
        "Apple-II",
        "ARCHIE",
        "TSConf",
        "TI-99_4A",
        "3DO",
        "Dreamcast",
        "SVI328",
        "Supervision",
        "VC4000",
        "MyVision",
        "GameNWatch",
        "Casio_PV-1000",
        "EpochGalaxyII",
        "RCA-StudioII",
        "SCV",
        "SuperVision",
        "mame",
        "hbmame",
    }
)

# Normalized platform key (see normalize_platform_key) → MiSTer folder.
_PLATFORM_TO_MISTER: Dict[str, str] = {
    # Nintendo
    "nintendo - nintendo entertainment system": "NES",
    "nintendo - family computer disk system": "FDS",
    "nintendo - game boy": "GAMEBOY",
    "nintendo - game boy color": "GBC",
    "nintendo - game boy advance": "GBA",
    "nintendo - pokemon mini": "PokemonMini",
    "nintendo - nintendo 64": "N64",
    "nintendo - super nintendo entertainment system": "SNES",
    "nintendo - satellaview": "Satellaview",
    "nintendo - sufami turbo": "SufamiTurbo",
    "nintendo - virtual boy": "VirtualBoy",
    "nintendo - gamecube": "GameCube",
    # Sega
    "sega - mega drive - genesis": "MegaDrive",
    "sega - genesis": "MegaDrive",
    "sega - mega drive": "MegaDrive",
    "sega - 32x": "S32X",
    "sega - master system - mark iii": "SMS",
    "sega - master system": "SMS",
    "sega - game gear": "GameGear",
    "sega - sg-1000": "SG1000",
    "sega - mega-cd - sega cd": "MegaCD",
    "sega - mega-cd": "MegaCD",
    "sega - sega cd": "MegaCD",
    "sega - saturn": "Saturn",
    "sega - dreamcast": "Dreamcast",
    # NEC
    "nec - pc engine - turbografx-16": "TGFX16",
    "nec - pc engine": "TGFX16",
    "nec - turbografx-16": "TGFX16",
    "nec - pc engine cd - turbografx cd": "TGFX16-CD",
    "nec - pc engine cd": "TGFX16-CD",
    "nec - super grafx": "TGFX16",
    # Sony / other handhelds
    "sony - playstation": "PSX",
    "bandai - wonderswan": "WonderSwan",
    "bandai - wonderswan color": "WonderSwanColor",
    "snk - neo-geo pocket": "NeoGeoPocket",
    "snk - neo-geo pocket color": "NGPC",
    "snk - neo-geo": "NEOGEO",
    "snk - neo geo cd": "NeoGeo-CD",
    # Atari (No-Intro / libretro naming variants)
    "atari - atari 2600": "ATARI2600",
    "atari - 2600": "ATARI2600",
    "atari - atari 5200": "ATARI5200",
    "atari - 5200": "ATARI5200",
    "atari - atari 7800": "ATARI7800",
    "atari - 7800": "ATARI7800",
    "atari - atari lynx": "AtariLynx",
    "atari - lynx": "AtariLynx",
    "atari - atari jaguar": "Jaguar",
    "atari - jaguar": "Jaguar",
    "atari - atari jaguar cd": "Jaguar-CD",
    "atari - 8-bit family": "Atari800",
    # NEC / SNK libretro names
    "nec - pc engine - turbografx 16": "TGFX16",
    "nec - pc engine supergrafx": "TGFX16",
    "nec - pc engine cd - turbografx-cd": "TGFX16-CD",
    "snk - neo geo pocket": "NeoGeoPocket",
    "snk - neo geo pocket color": "NGPC",
    "the 3do company - 3do": "3DO",
    "philips - cd-i": "CD-i",
    "casio - pv-1000": "Casio_PV-1000",
    "epoch - super cassette vision": "SCV",
    "interton - vc 4000": "VC4000",
    "rca - studio ii": "RCA-StudioII",
    "commodore - 64": "C64",
    "commodore - vic-20": "VIC20",
    # Others (common No-Intro / TOSEC-ish labels)
    "coleco - colecovision": "Coleco",
    "mattel - intellivision": "Intellivision",
    "gce - vectrex": "VECTREX",
    "magnavox - odyssey2": "ODYSSEY2",
    "philips - videopac+": "Videopac",
    "fairchild - channel f": "ChannelF",
    "bally - astrocade": "Astrocade",
    "entex - adventure vision": "AVision",
    "emerson - arcadia 2001": "Arcadia",
    "vtech - creativision": "CreatiVision",
    "bit corp - gamate": "Gamate",
    "watara - supervision": "SuperVision",
    "nintendo - game & watch": "GameNWatch",
    "panasonic - 3do interactive multiplayer": "3DO",
    "commodore - commodore 64": "C64",
    "commodore - amiga": "Amiga",
    "sinclair - zx spectrum": "Spectrum",
    "sinclair - zx81": "ZX81",
    "msx - msx": "MSX",
    "microsoft - msx": "MSX",
    "nec - pc-98": "PC98",
    "sharp - x68000": "X68000",
    "apple - apple ii": "Apple-II",
    "tiger - game.com": "GameCom",
    "hartung - game master": "GameMaster",
}

_STRIP_TRAIL = re.compile(
    r"""
    \s*\(\d{8}\)\s*$            # (20240101) dat version
    |\s*\(headerless\)\s*$
    |\s*\(parent-clone\)\s*$
    |\s*\(xml\)\s*$
    |\s*\(datfile\)\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize_platform_key(name: str) -> str:
    s = (name or "").replace("\\", "/").strip()
    p = Path(s)
    if p.suffix.casefold() in {".dat", ".xml"}:
        s = p.stem
    s = _STRIP_TRAIL.sub("", s).strip()
    s = re.sub(r"^no-intro\s*-\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^redump\s*-\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^tosec\s*-\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s)
    return s.casefold()


def platform_to_mister_folder(platform: str) -> Optional[str]:
    """Return MiSTer /games/<folder> for a DAT platform/header/filename, or None."""
    key = normalize_platform_key(platform)
    if not key:
        return None
    if key in _PLATFORM_TO_MISTER:
        return _PLATFORM_TO_MISTER[key]
    # Already a core folder name?
    for folder in MISTER_CORE_FOLDERS:
        if key == folder.casefold():
            return folder
    # Filename like "MiSTer_Console" — not a platform; ignore
    if key.startswith("mister"):
        return None
    # Fuzzy: key endswith known platform tail
    for plat, folder in _PLATFORM_TO_MISTER.items():
        if key.endswith(plat) or plat.endswith(key):
            return folder
    return None


def is_mister_core_folder(name: str) -> bool:
    if not name:
        return False
    return name.casefold() in {f.casefold() for f in MISTER_CORE_FOLDERS}


# Official docs: CD images and VHD must not stay inside zip; cart ROMs may.
# https://mister-devel.github.io/MkDocs_MiSTer/setup/games/
MISTER_CD_CORE_FOLDERS = frozenset(
    {
        "PSX",
        "Saturn",
        "MegaCD",
        "TGFX16-CD",
        "NeoGeo-CD",
        "CD-i",
        "3DO",
        "Jaguar-CD",
        "Dreamcast",
        "AmigaCD32",
        "CDTV",
    }
)

# Zip is the native game package (MAME/NeoGeo romsets).
MISTER_KEEP_ZIP_FOLDERS = frozenset(
    {
        "NEOGEO",
        "NeoGeo",
        "mame",
        "hbmame",
    }
)

# Always unpack these out of archives (CD / disc images).
_MUST_UNPACK_EXT = frozenset(
    {
        ".cue",
        ".chd",
        ".iso",
        ".gdi",
        ".toc",
        ".ccd",
        ".sub",
        ".mds",
        ".m3u",
        ".pbp",
        ".vhd",
        ".vdi",
        ".hdf",
    }
)

# Ambiguous raw dumps — only force unpack in CD context.
_CD_RAW_EXT = frozenset({".bin", ".img"})


def _folder_in(folder: str, group: frozenset) -> bool:
    if not folder:
        return False
    key = folder.casefold()
    return any(key == g.casefold() for g in group)


def default_unpack_archive(
    folder: str,
    member_name: Optional[str],
    is_archive: bool,
) -> bool:
    """
    Smart Unpack default for a planned archive row.

    - CD / disc / VHD media → unpack (MiSTer cannot use them inside zip)
    - NeoGeo / MAME romset zips → keep zip
    - Cartridge and other ROMs → keep zip (core browses .zip as a folder)
    """
    if not is_archive:
        return True

    name = Path(member_name or "").name
    ext = Path(name).suffix.casefold()
    low = name.casefold()
    folder_cd = _folder_in(folder, MISTER_CD_CORE_FOLDERS)

    if ext in _MUST_UNPACK_EXT:
        return True
    if ext in _CD_RAW_EXT and (
        folder_cd or "(track" in low or "(disc" in low
    ):
        return True
    if _folder_in(folder, MISTER_KEEP_ZIP_FOLDERS):
        return False
    if folder_cd:
        return True
    return False
