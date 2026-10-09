#!/usr/bin/env python3
"""MiSTer Organize DAT indexing + multi-method ROM identification."""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

# Reuse heuristics (stdlib-only module — no PyYAML)
from dat_prefs import DatPrefs, merge_prefs_with_folder
from mister_platform_map import mister_core_path_preference, platform_to_mister_folder
from rom_heuristics import (
    EXT_MAP,
    SKIP_EXT,
    AMBIGUOUS_EXT,
    detect_magic,
    snes_checksum_plausible,
    classify_chd_or_cue,
)

LOG = logging.getLogger("dat_engine")

ProgressCb = Callable[[str, float, str], None]  # stage, 0..1, detail

# SHA-1 only as fallback when CRC misses — skip re-read for huge files (CRC is enough).
SHA1_FALLBACK_MAX = 64 * 1024 * 1024
# Abort only if a single chunk read makes zero progress this long (true SMB freeze).
# Large ISO/CHD over MiSTer Samba is slow but OK — do not treat slowness as skip.
HASH_STALL_SECONDS = 120.0


def is_unc_path(path: Path) -> bool:
    s = str(path)
    return s.startswith("\\\\") or s.startswith("//")


@dataclass
class DatRom:
    rel_path: str  # path under games/ (Organize) OR basename (flat DAT)
    size: int
    crc: str
    md5: str
    sha1: str
    dat_name: str
    # True = MiSTer Organize-style path (games/CORE/…). False = flat No-Intro/etc.
    has_mister_path: bool = True
    # DAT header / set name for flat DATs (platform → folder map)
    platform: str = ""
    # Lower = higher priority (from DAT manager order)
    priority: int = 0
    # Relative path of the source DAT under the DAT folder
    source_rel: str = ""


@dataclass
class DatIndex:
    by_crc: Dict[str, List[DatRom]] = field(default_factory=dict)
    by_sha1: Dict[str, List[DatRom]] = field(default_factory=dict)
    by_md5: Dict[str, List[DatRom]] = field(default_factory=dict)
    rom_count: int = 0
    path_rom_count: int = 0
    flat_rom_count: int = 0
    dat_files: List[str] = field(default_factory=list)
    path_dat_files: List[str] = field(default_factory=list)
    flat_dat_files: List[str] = field(default_factory=list)

    def add(self, rom: DatRom) -> None:
        self.rom_count += 1
        if rom.has_mister_path:
            self.path_rom_count += 1
        else:
            self.flat_rom_count += 1
        if rom.crc:
            self.by_crc.setdefault(rom.crc, []).append(rom)
        if rom.sha1:
            self.by_sha1.setdefault(rom.sha1, []).append(rom)
        if rom.md5:
            self.by_md5.setdefault(rom.md5, []).append(rom)


@dataclass
class IdentifyResult:
    method: str
    rel_under_games: Optional[str]  # e.g. SNES/foo.sfc (preferred path if unpacked)
    core: Optional[str]
    reason: str
    confidence: str  # high|medium|low|skip|unknown
    # ZIP member matched during identification (for optional unpack on transfer)
    zip_member: Optional[str] = None
    # All DAT destinations for the same hash (GBC + GBC2P + …). Empty = only rel_under_games.
    all_rels: List[str] = field(default_factory=list)
    # DAT file basename that produced the match (empty if not DAT).
    dat_file: str = ""
    # Parallel to all_rels: DAT basename per destination ("" for extension sidecar).
    dat_files: List[str] = field(default_factory=list)


def _norm_hash(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _rom_path_under_games(raw_name: str) -> Optional[str]:
    """Convert DAT rom name to path relative to games/."""
    name = raw_name.replace("/", "\\")
    lower = name.casefold()
    if lower.startswith("games\\"):
        name = name[6:]
    elif "\\games\\" in lower:
        idx = lower.index("\\games\\") + len("\\games\\")
        name = name[idx:]
    name = name.strip("\\")
    if not name:
        return None
    return name.replace("\\", "/")


def _dat_header_name_from_text(blob: str, fallback: str) -> str:
    m = re.search(r"<header>\s*.*?<name>\s*([^<]+?)\s*</name>", blob, re.I | re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r'clrmamepro\s*\(\s*name\s+"([^"]+)"', blob, re.I | re.S)
    if m:
        return m.group(1).strip()
    return fallback


def _dat_header_name(path: Path) -> str:
    """Best-effort DAT set name from LogiqX header or clrmamepro block."""
    try:
        blob = path.read_bytes()[:16384].decode("utf-8", errors="ignore")
    except OSError:
        return path.stem
    return _dat_header_name_from_text(blob, path.stem)


def _rel_looks_nested(rel: str) -> bool:
    return "/" in rel.replace("\\", "/")


def _dat_looks_xml(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(256).lstrip().lower()
    except OSError:
        return False
    return head.startswith(b"<?xml") or head.startswith(b"<datafile")


_CMP_FIELD_Q = re.compile(r'(\w+)\s+"([^"]*)"', re.I)
_CMP_FIELD_U = re.compile(r"(\w+)\s+([^\s\"\)]+)", re.I)
_CMP_ROM_START = re.compile(r"rom\s*\(", re.I)


def _parse_clrmamepro_rom_body(body: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for m in _CMP_FIELD_Q.finditer(body):
        fields[m.group(1).casefold()] = m.group(2)
    for m in _CMP_FIELD_U.finditer(body):
        key = m.group(1).casefold()
        if key not in fields:
            fields[key] = m.group(2)
    return fields


def _iter_clrmamepro_rom_bodies(text: str):
    """Yield rom (…) bodies; parentheses inside quotes do not close the block."""
    for m in _CMP_ROM_START.finditer(text):
        i = m.end()
        depth = 1
        in_quote = False
        while i < len(text) and depth:
            ch = text[i]
            if ch == '"':
                in_quote = not in_quote
            elif not in_quote:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            i += 1
        if depth == 0:
            yield text[m.end() : i - 1], i


def _load_clrmamepro_pending(
    path: Path,
    dat_label: str,
    platform: str,
    progress: Optional[ProgressCb],
) -> List[DatRom]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    pending: List[DatRom] = []
    total = max(len(text), 1)
    for n, (body, end) in enumerate(_iter_clrmamepro_rom_bodies(text)):
        fields = _parse_clrmamepro_rom_body(body)
        rom_name = fields.get("name") or ""
        rel = _rom_path_under_games(rom_name)
        if not rel:
            continue
        try:
            size = int(fields.get("size") or 0)
        except ValueError:
            size = 0
        pending.append(
            DatRom(
                rel_path=rel,
                size=size,
                crc=_norm_hash(fields.get("crc")),
                md5=_norm_hash(fields.get("md5")),
                sha1=_norm_hash(fields.get("sha1")),
                dat_name=dat_label,
                has_mister_path=False,
                platform=platform,
            )
        )
        if progress and n and n % 2000 == 0:
            progress("dat", min(end / total, 0.99), f"{path.name}: {len(pending)} roms")
    return pending


def _arcade_games_root(dat_label: str) -> Optional[str]:
    """MiSTer Arcade MAME/HBMAME DATs → games/mame or games/hbmame."""
    low = dat_label.casefold()
    if "arcade_hbmame" in low or low.startswith("hbmame"):
        return "hbmame"
    if "arcade_mame" in low or (low.startswith("mame") and "hbmame" not in low):
        return "mame"
    return None


def _arcade_rel_under_games(rom_name: str, machine: str, arcade_root: str) -> Optional[str]:
    """
    Build games-relative path for a MAME rom chip.
    DAT may use set\\file or a bare filename under <machine name=\"set\">.
    """
    raw = (rom_name or "").replace("/", "\\").strip("\\")
    if not raw:
        return None
    if "\\" in raw:
        # Already set\\chip — keep set folder from DAT
        rel = raw.replace("\\", "/")
    else:
        set_name = (machine or "").strip() or "_unknown"
        rel = f"{set_name}/{Path(raw).name}"
    return f"{arcade_root}/{rel}"


def _load_logiqx_pending(
    path: Path,
    dat_label: str,
    platform: str,
    progress: Optional[ProgressCb],
) -> List[DatRom]:
    pending: List[DatRom] = []
    size = max(path.stat().st_size, 1)
    arcade_root = _arcade_games_root(dat_label)
    current_machine = ""
    with path.open("rb") as fh:
        context = ET.iterparse(fh, events=("start", "end"))
        for event, elem in context:
            if event == "start" and elem.tag == "machine":
                current_machine = elem.attrib.get("name") or ""
                continue
            if event == "end" and elem.tag == "machine":
                current_machine = ""
                elem.clear()
                continue
            if event != "end" or elem.tag != "rom":
                continue
            rom_name = elem.attrib.get("name") or ""
            if arcade_root:
                rel = _arcade_rel_under_games(rom_name, current_machine, arcade_root)
            else:
                rel = _rom_path_under_games(rom_name)
            if not rel:
                elem.clear()
                continue
            pending.append(
                DatRom(
                    rel_path=rel,
                    size=int(elem.attrib.get("size") or 0),
                    crc=_norm_hash(elem.attrib.get("crc")),
                    md5=_norm_hash(elem.attrib.get("md5")),
                    sha1=_norm_hash(elem.attrib.get("sha1")),
                    dat_name=dat_label,
                    has_mister_path=False,
                    platform=platform,
                )
            )
            elem.clear()
            if progress and len(pending) % 2000 == 0:
                try:
                    read = fh.tell()
                except Exception:
                    read = 0
                progress("dat", min(read / size, 0.99), f"{path.name}: {len(pending)} roms")
    return pending


def load_dat_file(
    path: Path,
    index: DatIndex,
    progress: Optional[ProgressCb] = None,
    *,
    priority: int = 0,
    source_rel: str = "",
) -> int:
    """Parse one LogiqX XML or ClrMamePro DAT into index. Returns roms added."""
    dat_label = path.name
    header_name = _dat_header_name(path)
    platform = header_name or path.stem
    src = source_rel or path.name
    arcade_root = _arcade_games_root(dat_label)

    if _dat_looks_xml(path):
        pending = _load_logiqx_pending(path, dat_label, platform, progress)
    else:
        pending = _load_clrmamepro_pending(path, dat_label, platform, progress)

    nested = sum(1 for r in pending if _rel_looks_nested(r.rel_path))
    # Organize / path DATs: majority of rom names are nested under a folder.
    # Flat No-Intro/Redump: rom name is usually a bare filename.
    # MiSTer_* filenames from Organize are always treated as path DATs.
    # Arcade MAME/HBMAME always path → games/mame/<set>/…
    is_path_dat = (
        bool(arcade_root)
        or nested * 2 >= max(len(pending), 1)
        or dat_label.casefold().startswith("mister")
    )

    if is_path_dat:
        for rom in pending:
            rom.has_mister_path = True
            rom.platform = ""
            rom.priority = priority
            rom.source_rel = src
    else:
        mapped = platform_to_mister_folder(platform) or platform_to_mister_folder(path.name)
        if not mapped:
            LOG.warning(
                "DAT %s: flat DAT with unknown platform %r — skipped",
                dat_label,
                platform,
            )
            pending = []
        else:
            for rom in pending:
                rom.has_mister_path = False
                rom.platform = platform
                rom.rel_path = Path(rom.rel_path).name
                rom.priority = priority
                rom.source_rel = src

    for rom in pending:
        index.add(rom)

    index.dat_files.append(dat_label)
    if is_path_dat:
        index.path_dat_files.append(dat_label)
    elif pending:
        index.flat_dat_files.append(dat_label)

    if progress:
        kind = "path" if is_path_dat else ("flat" if pending else "skip")
        progress("dat", 1.0, f"{path.name}: {len(pending)} roms ({kind})")
    return len(pending)


def load_dat_folder(
    folder: Path,
    progress: Optional[ProgressCb] = None,
    prefs: Optional[DatPrefs] = None,
) -> DatIndex:
    """
    Load enabled DAT files in priority order (prefs).
    If prefs is None, merge saved prefs with the folder (path DATs first by default).
    """
    index = DatIndex()
    if not folder.exists():
        raise FileNotFoundError(f"DAT folder not found: {folder}")

    merged = merge_prefs_with_folder(folder, prefs)
    pri_map = merged.priority_map()
    files: List[Tuple[Path, str, int]] = []
    for e in merged.entries:
        if not e.enabled or not e.exists:
            continue
        path = folder.joinpath(*e.rel.replace("\\", "/").split("/"))
        if not path.is_file():
            continue
        pri = pri_map.get(e.rel.replace("\\", "/").casefold(), 9999)
        files.append((path, e.rel.replace("\\", "/"), pri))

    if not files:
        raise FileNotFoundError(f"No enabled .dat/.xml files in {folder}")

    total = len(files)
    for i, (path, rel, pri) in enumerate(files):
        LOG.info("Loading DAT %s (%d/%d) pri=%d", rel, i + 1, total, pri)

        def nested(stage: str, frac: float, detail: str, _i=i, _t=total) -> None:
            if progress:
                overall = (_i + frac) / _t
                progress("dat", overall, detail)

        load_dat_file(path, index, nested, priority=pri, source_rel=rel)

    if progress:
        progress(
            "dat",
            1.0,
            f"Loaded {index.rom_count} ROMs "
            f"({index.path_rom_count} path / {index.flat_rom_count} flat) "
            f"from {len(files)} DAT(s)",
        )
    return index


def resolve_dat_rel(rom: DatRom) -> Optional[str]:
    """Final path under games/ for a DAT hit (applies platform map for flat DATs)."""
    if rom.has_mister_path:
        return rom.rel_path.replace("\\", "/")
    core = platform_to_mister_folder(rom.platform)
    if not core:
        return None
    name = Path(rom.rel_path).name
    if Path(name).suffix.casefold() == ".unh":
        name = Path(name).with_suffix(".nes").name
    return f"{core}/{name}"


def matching_roms_tiered(
    cands: List[DatRom],
    size: Optional[int] = None,
    hint_name: Optional[str] = None,
) -> Tuple[List[DatRom], str]:
    """
    Prefer higher-priority DAT sources (lower priority number).
    Within the same priority, prefer path (Organize) rows, then flat.
    """
    if not cands:
        return [], ""
    by_pri: Dict[int, List[DatRom]] = {}
    for c in cands:
        by_pri.setdefault(c.priority, []).append(c)

    for pri in sorted(by_pri):
        group = by_pri[pri]
        path_cands = [c for c in group if c.has_mister_path]
        roms = matching_roms(path_cands, size=size, hint_name=hint_name)
        if roms:
            return roms, "path"
        flat_cands = [c for c in group if not c.has_mister_path]
        roms = matching_roms(
            flat_cands, size=size, hint_name=hint_name, allow_header_slop=True
        )
        if roms:
            return roms, "flat"
    return [], ""


def file_crc32(path: Path) -> Tuple[str, int]:
    """Fast CRC-32 only (no SHA1/MD5). Returns (crc_hex, size). Uses disk cache."""
    crc, _sha1, _md5, size = file_hashes(path, want_sha1=False, want_md5=False)
    return crc, size


def zip_member_crc(path: Path, member: str) -> Optional[Tuple[str, int]]:
    """CRC/size of a ZIP member from local header (no decompression)."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            info = zf.getinfo(member)
            return f"{info.CRC & 0xFFFFFFFF:08x}", int(info.file_size)
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def peek_rom_head(path: Path, zip_member: Optional[str] = None, n: int = 128) -> bytes:
    """First bytes of a loose ROM or ZIP member (for header checks)."""
    try:
        if zip_member:
            with zipfile.ZipFile(path, "r") as zf:
                with zf.open(zip_member, "r") as fh:
                    return fh.read(n)
        with path.open("rb") as fh:
            return fh.read(n)
    except (KeyError, zipfile.BadZipFile, OSError, RuntimeError):
        return b""


def mister_unsupported_note(
    name: str,
    head: bytes = b"",
    *,
    source_path: str = "",
) -> str:
    """
    CAPS note when the ROM form is known to be a poor fit for MiSTer cores.
    Empty string if nothing notable / unknown.
    """
    ext = Path(name or "").suffix.casefold()
    src = (source_path or "").replace("/", "\\").casefold()

    if ext == ".unh":
        return "HEADERLESS NES (.UNH) — LIKELY NOT SUPPORTED BY MISTER"
    if ext == ".lyx":
        return "HEADERLESS LYNX (.LYX) — LIKELY NOT SUPPORTED BY MISTER"

    if ext == ".nes":
        if head and len(head) >= 4 and head[:4] != b"NES\x1a":
            return "HEADERLESS NES DUMP — LIKELY NOT SUPPORTED BY MISTER"
        if not head and "headerless" in src and "nintendo entertainment system" in src:
            return "HEADERLESS NES SET — LIKELY NOT SUPPORTED BY MISTER"

    if ext == ".fds":
        if head and len(head) >= 4 and head[:4] != b"FDS\x1a":
            return "HEADERLESS FDS DUMP — LIKELY NOT SUPPORTED BY MISTER"

    if ext == ".a78":
        if head and len(head) >= 9 and head[:9] != b"ATARI7800":
            return "MISSING A78 HEADER — LIKELY NOT SUPPORTED BY MISTER"

    if ext == ".lnx":
        if head and len(head) >= 4 and head[:4] != b"LYNX":
            return "HEADERLESS LYNX DUMP — LIKELY NOT SUPPORTED BY MISTER"

    return ""


def file_hashes(path: Path, want_sha1: bool = True, want_md5: bool = False,
                progress: Optional[ProgressCb] = None) -> Tuple[str, str, str, int]:
    """
    Return (crc32_hex, sha1_hex, md5_hex, size). CRC/SHA1 cached by size+mtime.

    Always computes CRC for every file (including multi‑GB on MiSTer UNC).
    Progress callbacks report per-file hashing; a true SMB freeze (no data for
    HASH_STALL_SECONDS) aborts that file only.
    """
    from crc_cache import get_crc_cache

    cache = get_crc_cache()
    # MD5 is rare and not cached; skip cache when MD5 requested
    if not want_md5:
        hit = cache.get(path, need_sha1=want_sha1)
        if hit is not None:
            crc_h, sha1_h, size_h = hit
            return crc_h, sha1_h if want_sha1 else "", "", size_h

    try:
        st = path.stat()
        total = max(int(st.st_size), 1)
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    except OSError:
        total = 1
        mtime_ns = 0

    unc = is_unc_path(path)
    crc = 0
    sha1 = hashlib.sha1() if want_sha1 else None
    md5 = hashlib.md5() if want_md5 else None
    size = 0
    # 1 MiB chunks; UNC gets progress often enough via time throttle
    chunk_size = 1024 * 1024
    stall_limit = HASH_STALL_SECONDS if unc else HASH_STALL_SECONDS * 3

    fh = path.open("rb")
    stalled = False
    try:
        last_progress_at = time.monotonic()
        while True:
            box: Dict[str, object] = {}

            def _read_once() -> None:
                try:
                    box["chunk"] = fh.read(chunk_size)
                except OSError as exc:
                    box["err"] = exc

            t = threading.Thread(target=_read_once, daemon=True)
            t.start()
            t.join(stall_limit)
            if t.is_alive():
                stalled = True
                LOG.warning(
                    "Hash stalled %ss on %s — abort (SMB/IO). "
                    "If hangs persist: disconnect \\\\MISTER in Explorer and reconnect.",
                    int(stall_limit),
                    path,
                )
                if progress:
                    progress("hash_stall", size / max(total, 1), path.name)
                _cancel_os_handle(fh)
                break
            if "err" in box:
                raise box["err"]  # type: ignore[misc]
            chunk = box.get("chunk")
            if not chunk:
                break
            assert isinstance(chunk, (bytes, bytearray))
            size += len(chunk)
            crc = zlib.crc32(chunk, crc)
            if sha1:
                sha1.update(chunk)
            if md5:
                md5.update(chunk)
            now = time.monotonic()
            if progress and (
                size == len(chunk)
                or size >= total
                or now - last_progress_at >= 0.2
                or size % (4 * 1024 * 1024) < len(chunk)
            ):
                last_progress_at = now
                progress("hash", size / total, path.name)
    finally:
        # close() can also block on a dead SMB session — never wait forever
        def _close() -> None:
            try:
                fh.close()
            except OSError:
                pass

        ct = threading.Thread(target=_close, daemon=True)
        ct.start()
        ct.join(5.0)

    if stalled or not size:
        # Do not cache partial / aborted hashes
        if stalled:
            return "", "", "", total
        # empty file
        pass

    crc_hex = f"{crc & 0xFFFFFFFF:08x}"
    sha1_hex = sha1.hexdigest() if sha1 else ""
    if not want_md5 and crc_hex and not stalled:
        cache.put(path, crc_hex, size, sha1=sha1_hex, mtime_ns=int(mtime_ns))
    return (
        crc_hex,
        sha1_hex,
        md5.hexdigest() if md5 else "",
        size,
    )


def _cancel_os_handle(fh: object) -> None:
    """Best-effort CancelIoEx so a wedged SMB read can unblock close()."""
    if sys.platform != "win32":
        return
    try:
        import msvcrt
        import ctypes

        fileno = getattr(fh, "fileno", None)
        if not callable(fileno):
            return
        handle = msvcrt.get_osfhandle(int(fileno()))
        ctypes.windll.kernel32.CancelIoEx(ctypes.c_void_p(handle), None)
    except (AttributeError, OSError, ValueError):
        pass


def _rom_score(
    rom: DatRom,
    hint_ext: str = "",
    hint_stem: str = "",
) -> tuple:
    rel = rom.rel_path.replace("\\", "/")
    core = rel.split("/", 1)[0]
    base = Path(rel).name
    ext = Path(rel).suffix.casefold()
    stem = Path(rel).stem.casefold()

    s = 0
    rel_u = rel.upper()
    core_u = core.upper()
    hint_bios = "bios" in hint_stem or "[bios]" in hint_stem

    if hint_ext and ext == hint_ext:
        s += 100
    elif hint_ext == ".unh" and ext == ".nes":
        s += 100  # headerless dump → headed Organize path
    if hint_stem:
        if stem == hint_stem:
            s += 80
        elif hint_stem in stem or stem in hint_stem:
            s += 40
        ht = {t for t in hint_stem.replace("'", "").split() if len(t) > 2}
        st = {t for t in stem.replace("'", "").split() if len(t) > 2}
        if ht and st:
            s += 15 * len(ht & st)

    # Prefer full BIOS dump paths over shared boot1.rom stubs (same CRC
    # is listed under GBC, GAMEBOY, MegaDuck, …).
    if "BIOS" in rel_u:
        s += 70
    if stem in {"boot", "boot1", "boot0", "bootrom"} or base.casefold() in {
        "boot.rom",
        "boot1.rom",
        "boot0.rom",
    }:
        s -= 50
    if hint_bios and core_u == "MEGADUCK":
        s -= 80
    if hint_ext == ".gbc" and core_u == "MEGADUCK":
        s -= 80

    if core_u.endswith("2P"):
        s -= 60
    if hint_ext == ".gbc" and core_u == "GBC":
        s += 50
    if hint_ext == ".gbc" and core_u == "GAMEBOY":
        s -= 40
    if hint_ext in {".gb", ""} and core_u == "GAMEBOY":
        s += 20

    # Official MiSTer /games/ folder beats Organize aliases on CRC ties
    # (MegaDrive > Genesis, TGFX16 > TurboExpress, NES > NES_LightGun, …)
    s += mister_core_path_preference(core)

    s += min(rel.count("/"), 6) * 3
    s += min(len(base), 96) // 8
    return (s, -len(rel), rel.casefold())


_HEADER_SLOP_EXT = {".nes", ".unh", ".fds"}


def _size_matches(rom_size: int, file_size: Optional[int], hint_name: Optional[str]) -> bool:
    if file_size is None or rom_size == 0 or rom_size == file_size:
        return True
    # iNES / FDS header is 16 bytes — headerless dumps vs headed Organize DAT
    ext = Path(hint_name or "").suffix.casefold()
    if ext in _HEADER_SLOP_EXT and abs(rom_size - file_size) == 16:
        return True
    return False


def matching_roms(
    cands: List[DatRom],
    size: Optional[int] = None,
    hint_name: Optional[str] = None,
    *,
    allow_header_slop: bool = False,
) -> List[DatRom]:
    """All size-matching DAT entries for a hash, unique by rel_path, best-first."""
    if not cands:
        return []
    if size is not None:
        if allow_header_slop:
            sized = [c for c in cands if _size_matches(c.size, size, hint_name)]
        else:
            sized = [c for c in cands if c.size == size or c.size == 0]
        if sized:
            cands = sized

    hint = Path(hint_name) if hint_name else None
    hint_ext = hint.suffix.casefold() if hint and hint.suffix else ""
    hint_stem = hint.stem.casefold() if hint else ""

    best: Dict[str, DatRom] = {}
    for rom in cands:
        key = rom.rel_path.replace("\\", "/").casefold()
        prev = best.get(key)
        if prev is None or _rom_score(rom, hint_ext, hint_stem) > _rom_score(
            prev, hint_ext, hint_stem
        ):
            best[key] = rom

    return sorted(
        best.values(),
        key=lambda r: _rom_score(r, hint_ext, hint_stem),
        reverse=True,
    )


def pick_rom(
    cands: List[DatRom],
    size: Optional[int] = None,
    hint_name: Optional[str] = None,
) -> Optional[DatRom]:
    """Pick best DAT entry among CRC/SHA1 collisions."""
    matched = matching_roms(cands, size=size, hint_name=hint_name)
    return matched[0] if matched else None


def _extension_sidecar_rel(name: str) -> Optional[str]:
    """Unambiguous extension → games/ rel (e.g. .unh → NES/….nes)."""
    hit = _extension_from_name(name)
    if not hit or not hit.rel_under_games:
        return None
    if hit.confidence in {"skip", "unknown"}:
        return None
    return hit.rel_under_games.replace("\\", "/").strip("/")


def _merge_extension_sidecar(result: IdentifyResult, hint_name: str) -> IdentifyResult:
    """
    After a DAT hit, also keep a console path when the filename extension
    clearly maps to another core (.unh/.nes → NES while DAT says mame/…).
    Ambiguous extensions (.bin, .rom, …) are ignored.
    """
    if result.method != "dat" or not result.rel_under_games or not hint_name:
        return result
    side = _extension_sidecar_rel(Path(hint_name).name)
    if not side:
        return result
    side_core = side.split("/", 1)[0]
    rels: List[str] = []
    dats: List[str] = []
    for i, rel in enumerate(result.all_rels or []):
        rel = rel.replace("\\", "/").strip("/")
        if not rel or rel in rels:
            continue
        rels.append(rel)
        if i < len(result.dat_files):
            dats.append(result.dat_files[i])
        else:
            dats.append(result.dat_file)
    primary = result.rel_under_games.replace("\\", "/").strip("/")
    if primary and primary not in rels:
        rels.insert(0, primary)
        dats.insert(0, result.dat_file)
    existing_cores = {r.split("/", 1)[0].casefold() for r in rels}
    if side_core.casefold() in existing_cores:
        result.all_rels = rels
        result.dat_files = dats
        return result
    rels.append(side)
    dats.append("")  # extension sidecar — not from DAT path row
    cores = sorted({r.split("/", 1)[0] for r in rels})
    result.all_rels = rels
    result.dat_files = dats
    result.reason = (
        f"{result.reason} + ext {Path(hint_name).suffix.casefold()}→{side_core} "
        f"→ {len(rels)} dest(s): {', '.join(cores)}"
    )
    return result


def _dat_result_from_roms(
    roms: List[DatRom],
    how: str,
    zip_member: Optional[str] = None,
    *,
    tier: str = "path",
    hint_name: str = "",
) -> IdentifyResult:
    resolved: List[str] = []
    dat_files: List[str] = []
    for r in roms:
        rel = resolve_dat_rel(r)
        if not rel or rel in resolved:
            continue
        resolved.append(rel)
        dat_files.append(r.dat_name or "")
    if not resolved:
        # Should not happen for path roms; flat without map
        return IdentifyResult("dat", None, None, f"DAT {how} (no MiSTer folder)", "unknown")
    primary_rel = resolved[0]
    core = primary_rel.split("/", 1)[0]
    cores = sorted({rel.split("/", 1)[0] for rel in resolved})
    extra = f" → {len(resolved)} dest(s): {', '.join(cores)}" if len(resolved) > 1 else ""
    conf = "high" if tier == "path" else "medium"
    dat_file = dat_files[0] if dat_files else (roms[0].dat_name or "")
    result = IdentifyResult(
        method="dat",
        rel_under_games=primary_rel,
        core=core,
        reason=f"DAT {how} via {dat_file}{extra}",
        confidence=conf,
        zip_member=zip_member,
        all_rels=resolved,
        dat_file=dat_file,
        dat_files=dat_files,
    )
    hint = hint_name or (Path(zip_member).name if zip_member else "")
    return _merge_extension_sidecar(result, hint)


def _lookup_dat_roms(
    index: DatIndex,
    *,
    crc: str = "",
    sha1: str = "",
    size: Optional[int] = None,
    hint_name: str = "",
) -> Tuple[List[DatRom], str, str]:
    """DAT match by SHA1/CRC only (priority order from DAT manager)."""
    if sha1 and sha1 in index.by_sha1:
        roms, tier = matching_roms_tiered(index.by_sha1[sha1], size, hint_name)
        if roms:
            return roms, "sha1", tier
    if crc and crc in index.by_crc:
        roms, tier = matching_roms_tiered(index.by_crc[crc], size, hint_name)
        if roms:
            return roms, "crc", tier
    return [], "", ""


def identify_by_dat(path: Path, index: DatIndex, progress: Optional[ProgressCb] = None) -> Optional[IdentifyResult]:
    """
    Match a loose file against DAT indexes by CRC-32 (always).

    SHA-1 only if CRC misses and the file is not huge (avoids a second full
    multi‑GB read over SMB when CRC already ran).
    """
    hint = path.name
    crc, _sha1, _md5, size = file_hashes(
        path, want_sha1=False, want_md5=False, progress=progress
    )
    if not crc:
        return None
    roms, how, tier = _lookup_dat_roms(
        index, crc=crc, size=size, hint_name=hint
    )
    if not roms and size <= SHA1_FALLBACK_MAX:
        _crc2, sha1, _md5, size = file_hashes(
            path, want_sha1=True, want_md5=False, progress=progress
        )
        if sha1:
            roms, how, tier = _lookup_dat_roms(
                index, sha1=sha1, size=size, hint_name=hint
            )
    if not roms:
        return None
    return _dat_result_from_roms(roms, how, tier=tier, hint_name=hint)


def identify_zip_members_dat(path: Path, index: DatIndex) -> Optional[IdentifyResult]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir() or info.file_size <= 0:
                    continue
                name = Path(info.filename).name
                if Path(name).suffix.casefold() in SKIP_EXT:
                    continue
                crc = f"{info.CRC & 0xFFFFFFFF:08x}"
                roms, how, tier = _lookup_dat_roms(
                    index, crc=crc, size=info.file_size, hint_name=name
                )
                if roms:
                    label = f"{how}(zip:{name})"
                    return _dat_result_from_roms(roms, label, info.filename, tier=tier)
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def identify_by_magic(path: Path) -> Optional[IdentifyResult]:
    try:
        with path.open("rb") as fh:
            data = fh.read(0x10100)
    except OSError:
        return None
    return _magic_from_bytes(data, path.name)


def _magic_from_bytes(data: bytes, out_name: str) -> Optional[IdentifyResult]:
    magic = detect_magic(data)
    if magic:
        return IdentifyResult("magic", f"{magic}/{out_name}", magic, f"magic→{magic}", "high")
    if snes_checksum_plausible(data):
        return IdentifyResult("magic", f"SNES/{out_name}", "SNES", "snes header", "medium")
    return None


def identify_by_extension(path: Path) -> Optional[IdentifyResult]:
    return _extension_from_name(path.name, path)


def _extension_from_name(name: str, path_hint: Optional[Path] = None) -> Optional[IdentifyResult]:
    ext = Path(name).suffix.casefold()
    if ext in SKIP_EXT:
        return IdentifyResult("extension", None, None, f"skip {ext}", "skip")
    if ext in {".chd", ".cue", ".iso"}:
        hint = path_hint or Path(name)
        core = classify_chd_or_cue(hint)
        return IdentifyResult("extension", f"{core}/{Path(name).name}", core, f"disc {ext}", "medium")
    if ext in AMBIGUOUS_EXT:
        return None
    core = EXT_MAP.get(ext)
    if core:
        out = Path(name).name
        # MiSTer NES expects .nes; No-Intro headerless uses .unh
        if ext == ".unh" and core == "NES":
            out = Path(name).with_suffix(".nes").name
        return IdentifyResult("extension", f"{core}/{out}", core, f"ext {ext}", "high")
    return None


ARCHIVE_EXT = {".zip"}


def is_archive(path: Path) -> bool:
    return path.suffix.casefold() in ARCHIVE_EXT


def _identify_zip_member(
    methods: List[str],
    index: Optional[DatIndex],
    info: "zipfile.ZipInfo",
    read_prefix: Callable[[], bytes],
) -> Optional[IdentifyResult]:
    """Identify one ZIP member with the usual DAT → magic → extension chain."""
    name = Path(info.filename).name
    if Path(name).suffix.casefold() in SKIP_EXT:
        return IdentifyResult("extension", None, None, f"skip {Path(name).suffix}", "skip")

    for method in methods:
        if method == "dat":
            if index is None:
                continue
            crc = f"{info.CRC & 0xFFFFFFFF:08x}"
            roms, how, tier = _lookup_dat_roms(
                index, crc=crc, size=info.file_size, hint_name=name
            )
            if roms:
                return _dat_result_from_roms(
                    roms, f"{how}(zip:{name})", info.filename, tier=tier
                )
            continue

        if method == "magic":
            try:
                data = read_prefix()
            except Exception:
                continue
            hit = _magic_from_bytes(data, name)
            if hit:
                hit.reason = f"zip:{name} {hit.reason}"
                hit.zip_member = info.filename
                if index is not None:
                    crc = f"{info.CRC & 0xFFFFFFFF:08x}"
                    if crc not in index.by_crc:
                        hit.reason += f" (CRC {crc} not in DAT)"
                return hit
            continue

        if method == "extension":
            hit = _extension_from_name(name, Path(info.filename))
            if hit and hit.confidence == "skip":
                return hit
            if hit and hit.rel_under_games:
                hit.reason = f"zip:{name} {hit.reason}"
                hit.zip_member = info.filename
                return hit
            continue
    return None


def identify_archive_members(
    path: Path,
    methods: List[str],
    index: Optional[DatIndex] = None,
) -> List[IdentifyResult]:
    """Identify every supported member inside a ZIP (not just the first hit)."""
    methods = [m for m in methods if m != "zip"]
    if not methods:
        methods = list(METHOD_ORDER_DEFAULT)

    try:
        zf = zipfile.ZipFile(path, "r")
    except (zipfile.BadZipFile, OSError):
        return []

    out: List[IdentifyResult] = []
    with zf:
        for info in zf.infolist():
            if info.is_dir() or info.file_size <= 0:
                continue

            def read_prefix(info=info) -> bytes:
                with zf.open(info) as fh:
                    return fh.read(0x10100)

            hit = _identify_zip_member(methods, index, info, read_prefix)
            if hit is None or hit.confidence in {"skip", "unknown"}:
                continue
            if not hit.rel_under_games and hit.confidence != "skip":
                continue
            if hit.rel_under_games:
                out.append(hit)
    return out


def identify_inside_zip(
    path: Path,
    methods: List[str],
    index: Optional[DatIndex] = None,
) -> Optional[IdentifyResult]:
    """First matching ZIP member (legacy single-result API)."""
    hits = identify_archive_members(path, methods, index)
    return hits[0] if hits else None


METHOD_ORDER_DEFAULT = ["dat", "extension", "magic"]

METHOD_FUNCS = {
    "dat": None,  # special — needs index
    "magic": identify_by_magic,
    "extension": identify_by_extension,
}


def identify_file(
    path: Path,
    methods: List[str],
    index: Optional[DatIndex] = None,
    progress: Optional[ProgressCb] = None,
) -> IdentifyResult:
    # Normalize: ignore legacy "zip" flag — archives always inspected inside
    methods = [m for m in methods if m != "zip"]
    if not methods:
        methods = list(METHOD_ORDER_DEFAULT)

    if is_archive(path):
        # Always look inside: same DAT → magic → extension chain on members
        inner = identify_inside_zip(path, methods, index)
        if inner:
            return inner
        return IdentifyResult("none", None, None, "archive unrecognized", "unknown")

    for method in methods:
        if method == "dat":
            if index is None:
                continue
            hit = identify_by_dat(path, index, progress)
            if hit:
                return hit
            continue
        fn = METHOD_FUNCS.get(method)
        if not fn:
            continue
        hit = fn(path)
        if hit and hit.confidence != "unknown":
            if hit.confidence == "skip":
                return hit
            if hit.rel_under_games:
                return hit
    return IdentifyResult("none", None, None, "unrecognized", "unknown")
