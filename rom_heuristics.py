#!/usr/bin/env python3
"""ROM identification heuristics (stdlib only — safe for frozen EXE)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional, Set

# Organize folder "6 BIOS" / bare "BIOS", or any segment with BIOS as a word.
_BIOS_FOLDER = re.compile(r"^(?:\d+\s+)?bios$", re.IGNORECASE)
_BIOS_WORD = re.compile(r"(?:^|[^a-z0-9])bios(?:[^a-z0-9]|$)", re.IGNORECASE)
# Core firmware stubs used across MiSTer cores (all platforms).
_BOOT_ROM_NAME = re.compile(r"^boot\d*\.(?:rom|bin)$", re.IGNORECASE)
_BOOT_ROM_STEM = re.compile(r"^boot\d*$", re.IGNORECASE)
_FW_BIOS_NAME = re.compile(
    r"^(?:.*[_-])?bios(?:[_-].*)?\.(?:rom|bin)$|"
    r"^uni-bios.*\.(?:rom|bin)$|"
    r"^bootrom\.(?:rom|bin)$",
    re.IGNORECASE,
)


def is_bios_like_path(*parts: str) -> bool:
    """True for BIOS dumps / boot*.rom firmware on any MiSTer core."""
    chunks = [p.replace("\\", "/").strip() for p in parts if p and str(p).strip()]
    if not chunks:
        return False
    joined = " / ".join(chunks)
    low = joined.casefold()
    if "[bios]" in low:
        return True
    for chunk in chunks:
        for seg in chunk.replace("\\", "/").split("/"):
            if not seg:
                continue
            seg = seg.strip()
            if _BIOS_FOLDER.match(seg) or _BIOS_WORD.search(seg):
                return True
    name = Path(chunks[-1]).name
    if _BOOT_ROM_NAME.match(name) or _FW_BIOS_NAME.match(name):
        return True
    stem = Path(name).stem.casefold()
    suf = Path(name).suffix.casefold()
    if _BOOT_ROM_STEM.match(stem) and suf in {".rom", ".bin", ""}:
        return True
    # ngpcbios.rom, stvbios.nv, …
    if stem.endswith("bios") and suf in {".rom", ".bin", ".nv", ""}:
        return True
    return False

EXT_MAP: Dict[str, str] = {
    ".nes": "NES",
    ".unh": "NES",  # No-Intro headerless NES dumps
    ".fds": "NES",
    ".nsf": "NES",
    ".sfc": "SNES",
    ".smc": "SNES",
    ".fig": "SNES",
    ".swc": "SNES",
    ".gb": "GAMEBOY",
    ".gbc": "GBC",
    ".sgb": "SGB",
    ".gba": "GBA",
    ".n64": "N64",
    ".z64": "N64",
    ".v64": "N64",
    ".md": "MegaDrive",
    ".gen": "MegaDrive",
    ".smd": "MegaDrive",
    ".32x": "S32X",
    ".sms": "SMS",
    ".gg": "GameGear",
    ".sg": "SG1000",
    ".pce": "TGFX16",
    ".sgx": "TGFX16",
    ".a26": "Atari2600",
    ".a52": "ATARI5200",
    ".a78": "ATARI7800",
    ".lnx": "AtariLynx",
    ".jag": "Jaguar",
    ".j64": "Jaguar",
    ".col": "Coleco",
    ".int": "Intellivision",
    ".vec": "VECTREX",
    ".ws": "WonderSwan",
    ".wsc": "WonderSwanColor",
    ".ngp": "NGP",
    ".ngc": "NGPC",
    ".vb": "VirtualBoy",
    ".min": "PokemonMini",
    ".neo": "NEOGEO",
    ".chd": "PSX",
    ".cue": "PSX",
    ".iso": "PSX",
    ".p": "ZX81",
    ".tap": "Spectrum",
    ".tzx": "Spectrum",
    ".z80": "Spectrum",
    ".sna": "Spectrum",
    ".d64": "C64",
    ".t64": "C64",
    ".crt": "C64",
    ".adf": "Amiga",
    ".ipf": "Amiga",
}

AMBIGUOUS_EXT = {".bin", ".rom", ".img", ".raw", ".zip", ".7z", ".rar"}

SKIP_EXT = {
    ".txt", ".nfo", ".url", ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".html", ".htm", ".pdf", ".exe", ".dll", ".bat", ".cmd", ".ps1", ".py",
    ".json", ".xml", ".csv", ".db", ".ini", ".cfg", ".log", ".mra", ".rbf",
    ".mgl", ".sav", ".srm", ".state", ".dsv", ".cht", ".ips", ".bps", ".ups",
    # NOTE: do NOT put ".md" here — Mega Drive / Genesis ROMs use .md
}


def detect_magic(data: bytes) -> Optional[str]:
    if len(data) < 16:
        return None

    if data[:4] == b"NES\x1a":
        return "NES"
    if data[:4] == b"FDS\x1a" or data[:3] == b"\x01*N":
        return "NES"
    if len(data) >= 0x10C and data[0x104:0x10C] == bytes.fromhex("CEED6666CC0D000B"):
        # Cartridge header CGB flag @ 0x143:
        #   0x00 = DMG (Game Boy)
        #   0x80 = CGB-compatible (works on GB + GBC) — treat as GBC
        #   0xC0 = CGB-only
        if len(data) > 0x143 and data[0x143] in (0x80, 0xC0):
            return "GBC"
        return "GAMEBOY"
    if len(data) >= 0xB0 and data[0x04:0x08] == bytes.fromhex("24FFAE51"):
        return "GBA"
    if len(data) >= 0x108:
        if data[0x100:0x104] == b"SEGA" or data[0x101:0x105] == b"SEGA":
            if len(data) >= 0x3C4 and data[0x3C0:0x3C4] == b"MARS":
                return "S32X"
            return "MegaDrive"
    if len(data) >= 0x284 and data[0x280:0x284] in (b"EAGN", b"EAMG"):
        return "MegaDrive"
    if data[:8] == b"PS-X EXE":
        return "PSX"
    if data[:4] == bytes.fromhex("80371240"):
        return "N64"
    if data[:4] == bytes.fromhex("37804012"):
        return "N64"
    if data[:4] == bytes.fromhex("40123780"):
        return "N64"
    if data[:2] == b"PK":
        return None
    return None


def snes_checksum_plausible(data: bytes) -> bool:
    if len(data) < 0x8000:
        return False
    for offset in (0x7FDC, 0xFFDC, 0x40FFDC):
        if offset + 4 > len(data):
            continue
        csum = int.from_bytes(data[offset : offset + 2], "little")
        comp = int.from_bytes(data[offset + 2 : offset + 4], "little")
        if (csum ^ comp) == 0xFFFF and csum not in (0, 0xFFFF):
            return True
    return False


def classify_chd_or_cue(path: Path) -> str:
    blob = str(path).casefold()
    rules = [
        (("saturn", "sega saturn"), "Saturn"),
        (("megacd", "mega-cd", "sega cd", "segacd"), "MegaCD"),
        (("pc engine", "tgfx", "turbografx", "pcecd"), "TGFX16-CD"),
        (("neogeo", "neo-geo", "neocd"), "NeoGeo-CD"),
        (("dreamcast",), "Dreamcast"),
        (("psx", "playstation", "ps1", "psone"), "PSX"),
    ]
    for keys, core in rules:
        if any(k in blob for k in keys):
            return core
    return "PSX"
