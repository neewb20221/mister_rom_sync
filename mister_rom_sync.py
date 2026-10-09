#!/usr/bin/env python3
"""
MiSTer ROM Sync — graphical UI (tkinter).

Frozen EXE uses only Python stdlib + tkinter (bundled). No PyYAML required.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import shutil
import stat as stat_mod
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app_paths import app_dir, bundle_dir
from dat_download import default_dats_dir, download_all_dats
from dat_manager_ui import DatManagerDialog
from dat_prefs import merge_prefs_with_folder, save_dat_prefs
from dat_engine import (
    METHOD_ORDER_DEFAULT,
    DatIndex,
    IdentifyResult,
    file_crc32,
    identify_archive_members,
    identify_file,
    is_archive,
    load_dat_folder,
    mister_unsupported_note,
    peek_rom_head,
)
from crc_cache import get_crc_cache
from mister_platform_map import MISTER_CORE_FOLDERS, default_unpack_archive
from rom_heuristics import is_bios_like_path

_ROOT = app_dir()
DEFAULT_DATS = default_dats_dir(_ROOT)
DEFAULT_GAMES = Path(r"\\MISTER\sdcard\games")
CONFIG_PATH = _ROOT / "config_mister_rom_sync.json"
APP_NAME = "MiSTer ROM Sync"
APP_VERSION = "0.2.0"
GITHUB_REPO_URL = "https://github.com/neewb20221/mister_rom_sync"
GITHUB_RELEASES_URL = f"{GITHUB_REPO_URL}/releases"
DONATE_URL = "https://donatepay.ru/don/neewb20221"
_LEGACY_CONFIG_PATHS = (
    _ROOT / "config_organize_ui.json",
    _ROOT / "config_organize.yaml",
)

METHOD_LABELS = [
    ("dat", "DAT files (CRC / SHA1)"),
    ("extension", "File extension"),
    ("magic", "ROM headers / signatures"),
]
METHOD_LABEL_BY_KEY = {k: label for k, label in METHOD_LABELS}

MARK_ON = "☑"
MARK_OFF = "☐"
MARK_SOME = "☒"

STATUS_NEW = "new"
STATUS_SAME = "same"
STATUS_DIFF = "different"
STATUS_OTHER = "crc elsewhere"


@dataclass
class Settings:
    source_path: str = r"C:\temp\rom_dump"
    destination_path: str = str(DEFAULT_GAMES)
    dat_path: str = str(DEFAULT_DATS)
    methods: List[str] = field(default_factory=lambda: list(METHOD_ORDER_DEFAULT))
    # Full identification order (enabled + disabled); methods = enabled subset in priority order
    method_order: List[str] = field(
        default_factory=lambda: [k for k, _ in METHOD_LABELS]
    )
    source_recursive: bool = True
    dest_recursive: bool = True
    dat_recursive: bool = True
    dry_run: bool = True
    unknown_mode: str = "copy_to_unknown"
    skip_bios: bool = True
    # One primary MiSTer folder per file (drop GBC2P / boot1.rom / MegaDuck twins, keep ext sidecar)
    primary_only: bool = True
    # Move same-CRC files already on MiSTer into the planned path (no re-copy)
    relocate_on_mister: bool = True
    # After transfer, remove empty nested folders under each platform
    prune_empty_dirs: bool = True


@dataclass
class PlannedItem:
    source: Path
    folder: str
    rel_packed: str
    rel_unpacked: str
    method: str
    reason: str
    zip_member: Optional[str]
    packed_bytes: int
    unpacked_bytes: int
    is_archive: bool
    include: bool = True
    unpack: bool = True
    crc_packed: str = ""
    crc_unpacked: str = ""
    dest_status: str = STATUS_NEW
    dest_rel: str = ""
    dest_bytes: int = 0
    dest_crc: str = ""
    dat_name: str = ""  # DAT file basename (e.g. MiSTer_Console….dat); empty if not DAT
    bios_like: bool = False
    notes: str = ""  # CAPS warnings (e.g. headerless / MiSTer unsupported)

    def active_rel(self) -> str:
        return self.rel_unpacked if (self.unpack and self.is_archive) else self.rel_packed

    def active_bytes(self) -> int:
        return self.unpacked_bytes if (self.unpack and self.is_archive) else self.packed_bytes

    def active_crc(self) -> str:
        return self.crc_unpacked if (self.unpack and self.is_archive) else self.crc_packed

    def source_label(self) -> str:
        """Actual on-disk source name (+ inner member for archives)."""
        if self.is_archive and self.zip_member:
            return f"{self.source.name} › {Path(self.zip_member).name}"
        return self.source.name


@dataclass
class FolderPlan:
    name: str
    items: List[PlannedItem] = field(default_factory=list)
    unpack: bool = True  # default applied to children when toggled on folder

    @property
    def file_count(self) -> int:
        return len(self.items)

    @property
    def archive_count(self) -> int:
        return sum(1 for it in self.items if it.is_archive)

    def selected_items(self) -> List[PlannedItem]:
        return [it for it in self.items if it.include]

    def include_mark(self) -> str:
        n = len(self.items)
        if n == 0:
            return MARK_OFF
        sel = sum(1 for it in self.items if it.include)
        if sel == 0:
            return MARK_OFF
        if sel == n:
            return MARK_ON
        return MARK_SOME

    def unpack_mark(self) -> str:
        archives = [it for it in self.items if it.is_archive]
        if not archives:
            return "—"
        on = sum(1 for it in archives if it.unpack)
        if on == 0:
            return MARK_OFF
        if on == len(archives):
            return MARK_ON
        return MARK_SOME

    def plan_bytes(self) -> int:
        """Selected payload size; zip kept as archive counted once per dest path."""
        total = 0
        seen_packed: set = set()
        for it in self.items:
            if not it.include:
                continue
            if it.unpack and it.is_archive:
                total += it.unpacked_bytes
            else:
                key = (str(it.source), it.rel_packed)
                if key in seen_packed:
                    continue
                seen_packed.add(key)
                total += it.packed_bytes
        return total

    def dest_present_bytes(self) -> int:
        return sum(it.dest_bytes for it in self.items if it.dest_status in (STATUS_SAME, STATUS_DIFF))


@dataclass
class DestEntry:
    rel: str
    path: Path
    size: int
    crc: str


def _migrate_legacy_config() -> None:
    """Rename old config filenames to config_mister_rom_sync.json once."""
    if CONFIG_PATH.exists():
        return
    for legacy in _LEGACY_CONFIG_PATHS:
        if not legacy.is_file():
            continue
        if legacy.suffix.lower() == ".json":
            try:
                legacy.replace(CONFIG_PATH)
            except OSError:
                pass
            return


def load_settings() -> Settings:
    DEFAULT_DATS.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_config()
    if not CONFIG_PATH.exists():
        return Settings()
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh) or {}
    except (OSError, json.JSONDecodeError):
        return Settings()
    s = Settings()
    s.source_path = str(raw.get("source_path", s.source_path))
    s.destination_path = str(raw.get("destination_path", s.destination_path))
    s.dat_path = str(raw.get("dat_path", s.dat_path))
    dp = Path(s.dat_path)
    if not dp.is_absolute():
        s.dat_path = str(_ROOT / dp)
    s.methods = [m for m in (raw.get("methods") or METHOD_ORDER_DEFAULT) if m != "zip"]
    if not s.methods:
        s.methods = list(METHOD_ORDER_DEFAULT)
    raw_order = [m for m in (raw.get("method_order") or []) if m != "zip"]
    if not raw_order:
        raw_order = list(s.methods) + [k for k, _ in METHOD_LABELS]
    # normalize against known keys
    order: List[str] = []
    for m in raw_order:
        if m in METHOD_LABEL_BY_KEY and m not in order:
            order.append(m)
    for k, _ in METHOD_LABELS:
        if k not in order:
            order.append(k)
    s.method_order = order
    s.source_recursive = bool(raw.get("source_recursive", True))
    s.dest_recursive = bool(raw.get("dest_recursive", True))
    s.dat_recursive = bool(raw.get("dat_recursive", True))
    s.dry_run = bool(raw.get("dry_run", True))
    s.unknown_mode = str(raw.get("unknown_mode", "copy_to_unknown"))
    s.skip_bios = bool(raw.get("skip_bios", True))
    s.primary_only = bool(raw.get("primary_only", True))
    s.relocate_on_mister = bool(raw.get("relocate_on_mister", True))
    s.prune_empty_dirs = bool(raw.get("prune_empty_dirs", True))
    return s


def save_settings(s: Settings) -> None:
    data = {
        "source_path": s.source_path,
        "destination_path": s.destination_path,
        "dat_path": s.dat_path,
        "methods": s.methods,
        "method_order": s.method_order,
        "source_recursive": s.source_recursive,
        "dest_recursive": s.dest_recursive,
        "dat_recursive": s.dat_recursive,
        "dry_run": s.dry_run,
        "unknown_mode": s.unknown_mode,
        "skip_bios": s.skip_bios,
        "primary_only": s.primary_only,
        "relocate_on_mister": s.relocate_on_mister,
        "prune_empty_dirs": s.prune_empty_dirs,
    }
    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def format_size(n: int) -> str:
    n = max(0, int(n))
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{n} B"


def is_unc_path(path: Path) -> bool:
    """True for \\\\server\\share paths (MiSTer Samba, etc.)."""
    s = str(path)
    return s.startswith("\\\\") or s.startswith("//")


# Only one large-file CRC/hash at a time (ISO/CHD); small files keep full parallelism.
LARGE_FILE_BYTES = 32 * 1024 * 1024  # 32 MiB


def worker_count(path: Path, kind: str) -> int:
    """I/O-bound pools; fewer threads for UNC/SMB (MiSTer share)."""
    cpu = os.cpu_count() or 4
    unc = is_unc_path(path)
    if kind == "scan":
        return max(4, min(8 if unc else 20, cpu * 2))
    return max(2, min(3 if unc else 8, cpu))


def scan_overall(phase: str, frac: float = 0.0) -> float:
    """
    Map a scan sub-step into overall 0..1.
    phase frac is local 0..1 within that step.
    """
    spans = {
        "dat": (0.00, 0.08),
        "list_src": (0.08, 0.16),
        "identify": (0.16, 0.62),
        "src_crc": (0.62, 0.78),
        "dest": (0.78, 1.00),
        "done": (1.00, 1.00),
    }
    lo, hi = spans.get(phase, (0.0, 1.0))
    f = max(0.0, min(1.0, float(frac)))
    return lo + (hi - lo) * f


def _norm_path_key(path: Path) -> str:
    try:
        s = str(path.resolve())
    except OSError:
        s = str(path)
    return s.replace("/", "\\").casefold()


def paths_same_tree(a: Path, b: Path) -> bool:
    """True when a and b refer to the same directory (Source == Output)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return _norm_path_key(a) == _norm_path_key(b)


def source_file_crc_for_dest(item: PlannedItem) -> Tuple[str, int]:
    """
    CRC/size of the source *file on disk* (zip payload or loose ROM).
    Used to seed MiSTer dest matching without re-hashing the same path.
    """
    if item.is_archive:
        return item.crc_packed or "", int(item.packed_bytes or 0)
    crc = item.crc_unpacked or item.crc_packed or ""
    size = int(item.unpacked_bytes or item.packed_bytes or 0)
    return crc, size


def build_known_source_crcs(items: List[PlannedItem]) -> Dict[str, Tuple[str, int]]:
    """Map normalized source path → (crc, size) after the Source CRC phase."""
    out: Dict[str, Tuple[str, int]] = {}
    for it in items:
        crc, size = source_file_crc_for_dest(it)
        if not crc:
            continue
        key = _norm_path_key(it.source)
        prev = out.get(key)
        if prev is None or (not prev[0] and crc):
            out[key] = (crc, size)
    return out


def zip_member_size(path: Path, member: Optional[str]) -> Optional[int]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if member:
                return int(zf.getinfo(member).file_size)
            total = 0
            for info in zf.infolist():
                if not info.is_dir() and info.file_size > 0:
                    total += int(info.file_size)
            return total
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def item_is_bios_like(item: PlannedItem) -> bool:
    return is_bios_like_path(
        item.rel_unpacked,
        item.rel_packed,
        item.dat_name,
        item.source.name,
        item.zip_member or "",
    )


def packed_rel_from_unpacked(
    unpacked_rel: str, archive_name: str, folder: str
) -> str:
    """
    Keep-archive dest path: place the zip in the same Organize folder as the ROM.
    SNES/1 SNES/2 Japan/Game.sfc + Game.zip → SNES/1 SNES/2 Japan/Game.zip
    Flat DAT → SNES/Game.zip
    """
    rel = (unpacked_rel or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[:-1] + [archive_name])
    core = (folder or "").strip() or (parts[0] if parts else "_unknown")
    return f"{core}/{archive_name}"


def plan_from_identify(
    source: Path,
    result: IdentifyResult,
    unknown_mode: str,
    *,
    skip_bios: bool = True,
    primary_only: bool = True,
) -> List[PlannedItem]:
    """Build one planned item per destination (DAT may list the same CRC in several folders)."""
    if result.confidence == "skip":
        return []

    packed_bytes = source.stat().st_size
    archive = is_archive(source)
    member = result.zip_member if archive else None
    unpacked = packed_bytes
    if archive:
        unpacked = zip_member_size(source, member) or packed_bytes

    rels: List[str] = []
    for rel in result.all_rels or []:
        rel = rel.replace("\\", "/").strip("/")
        if rel and rel not in rels:
            rels.append(rel)
    primary_rel = ""
    if result.rel_under_games:
        primary_rel = result.rel_under_games.replace("\\", "/").strip("/")
        if primary_rel and primary_rel not in rels:
            rels.insert(0, primary_rel)
    if not primary_rel and rels:
        primary_rel = rels[0]

    dat_by_rel = {
        rel.replace("\\", "/").strip("/"): (result.dat_files[i] if i < len(result.dat_files) else result.dat_file)
        for i, rel in enumerate(result.all_rels or [])
        if rel
    }
    default_dat = result.dat_file if result.method == "dat" else ""

    if primary_only and primary_rel:
        # One destination only: drop twin cores (GBC2P/…) and extension sidecars
        # (e.g. DAT→GAMEBOY + .gbc→GBC), which used to create duplicate rows.
        rels = [primary_rel]

    def _make(
        *,
        folder: str,
        rel_packed: str,
        rel_unpacked: str,
        reason: str,
        item_dat: str,
        method: str,
    ) -> PlannedItem:
        item = PlannedItem(
            source=source,
            folder=folder,
            rel_packed=rel_packed,
            rel_unpacked=rel_unpacked,
            method=method,
            reason=reason,
            zip_member=member,
            packed_bytes=packed_bytes,
            unpacked_bytes=unpacked,
            is_archive=archive,
            dat_name=item_dat,
            unpack=default_unpack_archive(folder, member, archive),
        )
        item.bios_like = item_is_bios_like(item)
        if skip_bios and item.bios_like:
            item.include = False
        return item

    if not rels:
        if unknown_mode != "copy_to_unknown":
            return []
        folder = "_unknown"
        rel_unpacked = f"_unknown/{source.name}"
        if archive and member:
            rel_unpacked = f"_unknown/{Path(member).name}"
        return [
            _make(
                folder=folder,
                rel_packed=f"_unknown/{source.name}",
                rel_unpacked=rel_unpacked,
                reason=result.reason,
                item_dat="",
                method=result.method,
            )
        ]

    items: List[PlannedItem] = []
    for rel in rels:
        folder = (rel.split("/", 1)[0] or "_unknown").strip() or "_unknown"
        item_dat = dat_by_rel.get(rel, "") or default_dat
        # Extension sidecar kept alongside DAT hit
        if result.method == "dat" and not item_dat:
            method = "extension"
            reason = f"ext (with DAT {default_dat})" if default_dat else result.reason
        else:
            method = result.method
            reason = result.reason
            if len(rels) > 1:
                reason = f"{result.reason} [dest {folder}]"
        items.append(
            _make(
                folder=folder,
                rel_packed=packed_rel_from_unpacked(rel, source.name, folder),
                rel_unpacked=rel,
                reason=reason,
                item_dat=item_dat if method == "dat" else "",
                method=method,
            )
        )
    return items


_CD_MEMBER_EXT = {
    ".cue",
    ".bin",
    ".chd",
    ".iso",
    ".img",
    ".toc",
    ".gdi",
    ".sbi",
    ".ccd",
    ".sub",
    ".mds",
}


def _cd_member_name(item: PlannedItem) -> str:
    if item.zip_member:
        return Path(item.zip_member).name
    return Path(item.rel_unpacked).name


def _is_cd_set_item(item: PlannedItem) -> bool:
    return Path(_cd_member_name(item)).suffix.casefold() in _CD_MEMBER_EXT


def _cd_path_score(item: PlannedItem) -> Tuple[int, int, int, int]:
    """Prefer DAT Organize nested paths over flat extension hits."""
    rel = (item.rel_unpacked or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p]
    depth = max(0, len(parts) - 1)
    is_dat = 1 if item.method == "dat" and item.dat_name else 0
    # Nested game folder (core/…/file) beats core/file
    nested = 1 if depth >= 2 else 0
    return (is_dat, nested, depth, len(rel))


def coalesce_cd_set_paths(items: List[PlannedItem]) -> List[PlannedItem]:
    """
    CD cue-set rule: members of the same archive (or folder) share one game directory.
    Path comes from the best DAT hit; orphans (e.g. .cue by extension) inherit that folder.
    """
    if len(items) < 2:
        return items

    groups: Dict[str, List[PlannedItem]] = {}
    for it in items:
        if not _is_cd_set_item(it):
            continue
        if it.is_archive:
            key = f"zip:{it.source}"
        else:
            key = f"dir:{it.source.parent}"
        groups.setdefault(key, []).append(it)

    for group in groups.values():
        if len(group) < 2:
            continue
        # Need a cue/chd/iso or at least one DAT path to anchor
        has_anchor_ext = any(
            Path(_cd_member_name(it)).suffix.casefold() in {".cue", ".chd", ".iso", ".gdi", ".toc"}
            for it in group
        )
        has_dat = any(it.method == "dat" and it.dat_name for it in group)
        if not has_anchor_ext and not has_dat:
            continue

        best = max(group, key=_cd_path_score)
        best_rel = (best.rel_unpacked or "").replace("\\", "/").strip("/")
        parts = [p for p in best_rel.split("/") if p]
        if len(parts) < 2:
            continue
        game_dir = "/".join(parts[:-1])
        core = parts[0]

        for it in group:
            name = _cd_member_name(it)
            new_rel = f"{game_dir}/{name}"
            old_rel = (it.rel_unpacked or "").replace("\\", "/").strip("/")
            if old_rel == new_rel and it.folder == core:
                continue
            it.folder = core
            it.rel_unpacked = new_rel
            it.rel_packed = packed_rel_from_unpacked(new_rel, it.source.name, core)
            if it.is_archive:
                it.unpack = default_unpack_archive(core, it.zip_member, True)
            # Path only — do not invent DAT match / dat_name for non-DAT members
            if old_rel != new_rel:
                it.reason = f"{it.reason} → CD set path aligned"

    return items


def plan_source(
    source: Path,
    methods: List[str],
    index: Optional[DatIndex],
    unknown_mode: str,
    *,
    skip_bios: bool = True,
    primary_only: bool = True,
    progress: Optional[Callable[[str, float, str], None]] = None,
) -> List[PlannedItem]:
    """Plan loose file or every supported member inside an archive."""
    if is_archive(source):
        results = identify_archive_members(source, methods, index)
        if not results:
            return plan_from_identify(
                source,
                IdentifyResult("none", None, None, "archive unrecognized", "unknown"),
                unknown_mode,
                skip_bios=skip_bios,
                primary_only=primary_only,
            )
        items: List[PlannedItem] = []
        for result in results:
            items.extend(
                plan_from_identify(
                    source,
                    result,
                    unknown_mode,
                    skip_bios=skip_bios,
                    primary_only=primary_only,
                )
            )
        return coalesce_cd_set_paths(items)

    result = identify_file(source, methods, index, progress=progress)
    return plan_from_identify(
        source,
        result,
        unknown_mode,
        skip_bios=skip_bios,
        primary_only=primary_only,
    )


def _fill_loose_item_crc(item: PlannedItem) -> None:
    try:
        crc, size = file_crc32(item.source)
        item.crc_packed = crc
        item.crc_unpacked = crc
        if size:
            item.unpacked_bytes = size
    except OSError:
        item.crc_packed = ""
        item.crc_unpacked = ""
    head = peek_rom_head(item.source)
    item.notes = mister_unsupported_note(
        item.source.name, head, source_path=str(item.source)
    )


def _fill_zip_group_crcs(group: List[PlannedItem]) -> None:
    """One ZipFile open; packed CRC at most once, and only if Unpack is off."""
    path = group[0].source
    # active_crc uses packed only when not unpacking — skip full-zip hash otherwise
    need_packed = any(not it.unpack for it in group)
    packed_crc = ""
    if need_packed:
        try:
            packed_crc, _ = file_crc32(path)
        except OSError:
            packed_crc = ""

    try:
        with zipfile.ZipFile(path, "r") as zf:
            for it in group:
                it.crc_packed = packed_crc
                if it.zip_member:
                    try:
                        info = zf.getinfo(it.zip_member)
                        it.crc_unpacked = f"{info.CRC & 0xFFFFFFFF:08x}"
                        it.unpacked_bytes = int(info.file_size)
                    except KeyError:
                        it.crc_unpacked = packed_crc
                else:
                    it.crc_unpacked = packed_crc
                hint_name = (
                    Path(it.zip_member).name if it.zip_member else it.source.name
                )
                head = b""
                if it.zip_member:
                    try:
                        with zf.open(it.zip_member) as fh:
                            head = fh.read(128)
                    except (KeyError, OSError, RuntimeError):
                        head = b""
                it.notes = mister_unsupported_note(
                    hint_name, head, source_path=str(it.source)
                )
    except (zipfile.BadZipFile, OSError):
        for it in group:
            it.crc_packed = packed_crc
            it.crc_unpacked = packed_crc
            hint_name = Path(it.zip_member).name if it.zip_member else it.source.name
            it.notes = mister_unsupported_note(
                hint_name, b"", source_path=str(it.source)
            )


def fill_items_crcs(items: List[PlannedItem]) -> None:
    """
    Fill CRC fields for planned items.
    Archives: one open per zip; ZipInfo CRC for members; full packed hash
    only once per zip and only when Unpack is off for at least one row.
    """
    if not items:
        return
    by_zip: Dict[str, List[PlannedItem]] = {}
    loose: List[PlannedItem] = []
    for it in items:
        if it.is_archive:
            by_zip.setdefault(str(it.source), []).append(it)
        else:
            loose.append(it)
    for group in by_zip.values():
        _fill_zip_group_crcs(group)
    for it in loose:
        _fill_loose_item_crc(it)


def fill_item_crcs(item: PlannedItem) -> None:
    """Compute CRC for one planned item (see fill_items_crcs)."""
    fill_items_crcs([item])


def status_label(status: str) -> str:
    return {
        STATUS_NEW: "not on MiSTer",
        STATUS_SAME: "same CRC",
        STATUS_DIFF: "different CRC",
        STATUS_OTHER: "CRC elsewhere",
    }.get(status, status)


def match_item_against_dest(
    item: PlannedItem,
    by_rel: Dict[str, DestEntry],
    by_crc: Dict[str, List[DestEntry]],
) -> None:
    rel = item.active_rel().replace("\\", "/")
    crc = item.active_crc()
    entry = by_rel.get(rel.casefold())
    if entry is not None:
        item.dest_rel = entry.rel
        item.dest_bytes = entry.size
        item.dest_crc = entry.crc
        if crc and entry.crc == crc:
            item.dest_status = STATUS_SAME
        else:
            item.dest_status = STATUS_DIFF
        return

    item.dest_rel = ""
    item.dest_bytes = 0
    item.dest_crc = ""
    if crc and crc in by_crc:
        other = by_crc[crc][0]
        item.dest_status = STATUS_OTHER
        item.dest_rel = other.rel
        item.dest_bytes = other.size
        item.dest_crc = other.crc
        return
    item.dest_status = STATUS_NEW


def apply_dest_matches(folders: Dict[str, FolderPlan], by_rel: Dict[str, DestEntry], by_crc: Dict[str, List[DestEntry]]) -> None:
    for plan in folders.values():
        for item in plan.items:
            match_item_against_dest(item, by_rel, by_crc)
            # Already on MiSTer with same CRC → default off
            if item.dest_status == STATUS_SAME:
                item.include = False


def _item_dest_path(item: PlannedItem, dest_root: Path) -> Path:
    rel = item.active_rel().replace("\\", "/").strip("/")
    return dest_root / Path(*rel.split("/"))


def _skip_if_dest_same(
    dest: Path, crc_want: str, expected: int
) -> Optional[str]:
    if not dest.exists():
        return None
    try:
        if crc_want:
            crc_have, _size = file_crc32(dest)
            if crc_have == crc_want:
                return "SKIP_SAME_CRC"
        elif dest.stat().st_size == expected:
            return "SKIP_EXISTS"
    except OSError:
        pass
    return None


def _mister_elsewhere_path(item: PlannedItem, dest_root: Path) -> Optional[Path]:
    """Path of same-CRC file already on MiSTer but under a different rel."""
    if item.dest_status != STATUS_OTHER:
        return None
    rel = (item.dest_rel or "").replace("\\", "/").strip("/")
    if not rel or not item.active_crc():
        return None
    path = dest_root / Path(*rel.split("/"))
    if not path.is_file():
        return None
    dest = _item_dest_path(item, dest_root)
    try:
        if path.resolve() == dest.resolve():
            return None
    except OSError:
        if path == dest:
            return None
    return path


def try_mister_relocate(
    item: PlannedItem,
    dest_root: Path,
    dry_run: bool,
    *,
    claimed: Optional[set] = None,
    claimed_lock: Optional[threading.Lock] = None,
) -> Optional[str]:
    """
    If the payload already exists on MiSTer under another path (same CRC),
    move it into the planned location instead of copying from source again.
    Returns log tag, or None if relocate is not possible.
    """
    elsewhere = _mister_elsewhere_path(item, dest_root)
    if elsewhere is None:
        return None
    claim_key = str(elsewhere)
    dest = _item_dest_path(item, dest_root)

    def _claim() -> bool:
        if claimed is None:
            return True
        if claimed_lock:
            with claimed_lock:
                if claim_key in claimed:
                    return False
                claimed.add(claim_key)
                return True
        if claim_key in claimed:
            return False
        claimed.add(claim_key)
        return True

    if dry_run:
        if not _claim():
            return None
        return "RELOCATE"
    try:
        skip = _skip_if_dest_same(dest, item.active_crc(), item.active_bytes())
        if skip:
            if not _claim():
                return None
            try:
                elsewhere.unlink()
            except OSError:
                pass
            return skip
        if not _claim():
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        shutil.move(str(elsewhere), str(dest))
        return "RELOCATE"
    except OSError:
        return None


def prune_empty_dirs_under(root: Path) -> int:
    """
    Remove empty nested directories under a platform folder.
    Keeps `root` itself even if it becomes empty.
    """
    if not root.is_dir():
        return 0
    removed = 0
    try:
        root_res = root.resolve()
    except OSError:
        root_res = root
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        try:
            if p.resolve() == root_res:
                continue
        except OSError:
            if p == root:
                continue
        try:
            next(p.iterdir())
        except StopIteration:
            try:
                p.rmdir()
                removed += 1
            except OSError:
                pass
        except OSError:
            pass
    return removed


def _write_via_partial(dest: Path, write_fn: Callable[[Path], None]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        tmp.unlink()
    write_fn(tmp)
    tmp.replace(dest)


def _extract_zip_member(zf: zipfile.ZipFile, member: str, dest: Path) -> None:
    def _write(tmp: Path) -> None:
        with zf.open(member) as src_fh, tmp.open("wb") as out_fh:
            shutil.copyfileobj(src_fh, out_fh, length=1024 * 1024)

    _write_via_partial(dest, _write)


def group_transfer_units(items: List[PlannedItem]) -> List[List[PlannedItem]]:
    """
    CD cue-sets sharing a game folder transfer as one atomic unit.
    Other unpack members from the same archive share one ZipFile open.
    """
    used: set = set()
    units: List[List[PlannedItem]] = []

    cd_map: Dict[str, List[PlannedItem]] = {}
    for it in items:
        if not _is_cd_set_item(it):
            continue
        rel = it.active_rel().replace("\\", "/").strip("/")
        parts = [p for p in rel.split("/") if p]
        if len(parts) < 2:
            continue
        key = "/".join(parts[:-1]).casefold()
        cd_map.setdefault(key, []).append(it)
        used.add(id(it))
    units.extend(cd_map.values())

    zip_map: Dict[str, List[PlannedItem]] = {}
    for it in items:
        if id(it) in used:
            continue
        if it.unpack and it.is_archive:
            zip_map.setdefault(str(it.source), []).append(it)
            used.add(id(it))
    units.extend(zip_map.values())

    for it in items:
        if id(it) not in used:
            units.append([it])
    return units


def transfer_unit(
    unit: List[PlannedItem],
    dest_root: Path,
    dry_run: bool,
    *,
    check: Optional[Callable[[], None]] = None,
    claimed_relocate: Optional[set] = None,
    claimed_lock: Optional[threading.Lock] = None,
    relocate_on_mister: bool = True,
) -> List[Tuple[PlannedItem, str]]:
    """
    Copy/extract one unit (source is never deleted).
    CD sets: rollback newly written files on failure.
    Unpack archives: one ZipFile open per source path.
    Same-CRC files already on MiSTer elsewhere are relocated (not re-copied).
    """
    if not unit:
        return []

    atomic_cd = bool(unit) and all(_is_cd_set_item(it) for it in unit)
    results: List[Tuple[PlannedItem, str]] = []
    written: List[Path] = []
    # (original_elsewhere, new_dest) for atomic CD rollback of relocates
    relocated_pairs: List[Tuple[Path, Path]] = []
    # Keep-archive: same ZIP row may repeat — copy packed once
    packed_done: set = set()
    claimed = claimed_relocate if claimed_relocate is not None else set()

    unpack_groups: Dict[str, List[PlannedItem]] = {}
    other: List[PlannedItem] = []
    for it in unit:
        if it.unpack and it.is_archive:
            unpack_groups.setdefault(str(it.source), []).append(it)
        else:
            other.append(it)

    def _rollback_written() -> None:
        if not atomic_cd or dry_run:
            return
        for path in written:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        written.clear()
        for old, new in reversed(relocated_pairs):
            try:
                if new.exists() and not old.exists():
                    old.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(new), str(old))
            except OSError:
                pass
        relocated_pairs.clear()

    def _fail_rollback(exc: BaseException) -> List[Tuple[PlannedItem, str]]:
        _rollback_written()
        out: List[Tuple[PlannedItem, str]] = []
        done_ids: set = set()
        for it, tag in results:
            done_ids.add(id(it))
            if tag.startswith("SKIP"):
                out.append((it, tag))
            else:
                out.append((it, f"FAIL:{exc}"))
        for it in unit:
            if id(it) not in done_ids:
                out.append((it, f"FAIL:{exc}"))
        return out

    def _place_one(it: PlannedItem, *, default_tag: str) -> str:
        dest = _item_dest_path(it, dest_root)
        skip = _skip_if_dest_same(dest, it.active_crc(), it.active_bytes())
        if skip:
            return skip
        if relocate_on_mister:
            elsewhere = _mister_elsewhere_path(it, dest_root)
            tag = try_mister_relocate(
                it,
                dest_root,
                dry_run,
                claimed=claimed,
                claimed_lock=claimed_lock,
            )
            if tag:
                if tag == "RELOCATE" and not dry_run and elsewhere is not None:
                    relocated_pairs.append((elsewhere, dest))
                    written.append(dest)
                return tag
        if dry_run:
            return default_tag
        return ""  # caller performs extract/copy

    try:
        for _src_key, members in unpack_groups.items():
            if check:
                check()
            src = members[0].source
            need_zip = False
            pending_extract: List[PlannedItem] = []
            for it in members:
                if check:
                    check()
                tag = _place_one(it, default_tag="EXTRACT")
                if tag:
                    results.append((it, tag))
                else:
                    pending_extract.append(it)
                    need_zip = True

            if need_zip and not dry_run:
                with zipfile.ZipFile(src, "r") as zf:
                    for it in pending_extract:
                        if check:
                            check()
                        dest = _item_dest_path(it, dest_root)
                        if it.zip_member:
                            _extract_zip_member(zf, it.zip_member, dest)
                            written.append(dest)
                            results.append((it, "EXTRACT"))
                        else:
                            parent = dest.parent
                            parent.mkdir(parents=True, exist_ok=True)
                            for info in zf.infolist():
                                if info.is_dir() or info.file_size <= 0:
                                    continue
                                out = parent / Path(info.filename).name
                                _extract_zip_member(zf, info.filename, out)
                                written.append(out)
                            results.append((it, "EXTRACT_ALL"))
            elif need_zip and dry_run:
                for it in pending_extract:
                    results.append((it, "EXTRACT"))

        for it in other:
            if check:
                check()
            tag = _place_one(it, default_tag="COPY")
            if tag:
                results.append((it, tag))
                continue
            key = (str(it.source), it.rel_packed)
            if key in packed_done:
                results.append((it, "SKIP_DUP_ZIP"))
                continue
            packed_done.add(key)

            def _copy(tmp: Path, source=it.source) -> None:
                shutil.copy2(source, tmp)

            dest = _item_dest_path(it, dest_root)
            _write_via_partial(dest, _copy)
            written.append(dest)
            results.append((it, "COPY"))

        return results
    except InterruptedError:
        _rollback_written()
        raise
    except Exception as exc:
        return _fail_rollback(exc)


def transfer_one(
    item: PlannedItem,
    dest_root: Path,
    dry_run: bool,
) -> str:
    """Copy/extract one planned item. Returns log tag."""
    pairs = transfer_unit([item], dest_root, dry_run)
    return pairs[0][1] if pairs else "FAIL"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1100x960")
        self.minsize(900, 820)

        self.settings = load_settings()
        self._busy = False
        self._paused = False
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._icon_refs: List[tk.PhotoImage] = []
        self._folders: Dict[str, FolderPlan] = {}
        self._node_meta: Dict[str, Tuple] = {}
        self._folder_iids: Dict[str, str] = {}
        self._file_iids: Dict[Tuple[str, int], str] = {}
        self._dest_by_rel: Dict[str, DestEntry] = {}
        self._dest_by_crc: Dict[str, List[DestEntry]] = {}
        self._loading = True
        self._sort_col: str = "#0"
        self._sort_reverse: bool = False
        self._search_var = tk.StringVar(value="")
        self._heading_titles: Dict[str, str] = {
            "#0": "Source File",
            "archive": "Source Archive",
            "include": "Transfer",
            "unpack": "Unpack",
            "platform": "Platform",
            "plan_size": "Size",
            "mister_path": "Output MiSTer path",
            "dest_status": "On MiSTer",
            "dest_size": "Size on MiSTer",
            "dat_name": "DAT file",
            "source": "Source path",
            "info": "Match / method",
            "notes": "Notes",
        }
        self._scan_snapshot: Optional[Dict[str, Any]] = None

        self._apply_icon()
        self._build()
        self._bind_clipboard_shortcuts()
        self._load_into_form()
        self.after(100, self._poll_queue)

    def _icon_candidates(self) -> List[Path]:
        names = ("mister.ico", "mister_48.png", "mister_32.png", "mister_favicon.png")
        bases = (bundle_dir() / "assets", _ROOT / "assets", Path(__file__).resolve().parent / "assets")
        out: List[Path] = []
        seen = set()
        for base in bases:
            for name in names:
                p = base / name
                key = str(p.resolve()) if p.exists() else str(p)
                if p.exists() and key not in seen:
                    seen.add(key)
                    out.append(p)
        return out

    def _apply_icon(self) -> None:
        candidates = self._icon_candidates()
        ico = next((p for p in candidates if p.suffix.lower() == ".ico"), None)
        if ico is not None:
            ico_s = str(ico.resolve())
            try:
                self.iconbitmap(default=ico_s)
            except tk.TclError:
                try:
                    self.iconbitmap(ico_s)
                except tk.TclError:
                    pass
            # Taskbar uses python.exe icon unless we set WM_SETICON on the HWND
            self.after(50, lambda p=ico_s: self._windows_set_taskbar_icon(p))
            self.after(400, lambda p=ico_s: self._windows_set_taskbar_icon(p))
        pngs = [p for p in candidates if p.suffix.lower() == ".png"]
        photos: List[tk.PhotoImage] = []
        for p in pngs:
            try:
                photos.append(tk.PhotoImage(file=str(p)))
            except tk.TclError:
                continue
        if photos:
            self._icon_refs = photos
            try:
                self.iconphoto(True, *photos)
            except tk.TclError:
                try:
                    self.iconphoto(True, photos[0])
                except tk.TclError:
                    pass

    def _windows_set_taskbar_icon(self, ico_path: str) -> None:
        """Force window/taskbar icon when running under python.exe / py launcher."""
        if sys.platform != "win32":
            return
        try:
            self.update_idletasks()
            hwnd = int(self.winfo_id())
            # Tk child → real toplevel HWND
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            LR_DEFAULTSIZE = 0x0040
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            user32 = ctypes.windll.user32
            hicon_big = user32.LoadImageW(
                0, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE
            )
            hicon_small = user32.LoadImageW(
                0, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
            )
            if not hicon_big:
                hicon_big = user32.LoadImageW(
                    0, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
                )
            if hicon_big:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
            if hicon_small:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
            # Keep handles alive for process lifetime
            if not hasattr(self, "_win_hicons"):
                self._win_hicons = []
            for h in (hicon_big, hicon_small):
                if h:
                    self._win_hicons.append(h)
        except (AttributeError, OSError, tk.TclError, ValueError):
            pass

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}
        self._loading = True
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Button(header, text="About", command=self.on_about).pack(
            side=tk.RIGHT
        )

        paths = ttk.LabelFrame(root, text="Paths", padding=10)
        paths.pack(fill=tk.X, **pad)
        self.var_source = tk.StringVar()
        self.var_dest = tk.StringVar()
        self.var_dat = tk.StringVar()
        self.var_source_recursive = tk.BooleanVar(value=True)
        self.var_dest_recursive = tk.BooleanVar(value=True)
        self.var_dat_recursive = tk.BooleanVar(value=True)
        self._path_row(
            paths,
            0,
            "Source",
            self.var_source,
            self._browse_source,
            recursive_var=self.var_source_recursive,
        )
        self._path_row(
            paths,
            1,
            "Output (MiSTer library)",
            self.var_dest,
            self._browse_dest,
            recursive_var=self.var_dest_recursive,
        )
        self._path_row(
            paths,
            2,
            "DATs Folder",
            self.var_dat,
            self._browse_dat,
            recursive_var=self.var_dat_recursive,
            extra=("DAT files…", self.on_dat_manager),
        )
        paths.columnconfigure(1, weight=1, minsize=120)

        # Methods (narrow) + Options side-by-side → shorter window
        mid = ttk.Frame(root)
        mid.pack(fill=tk.X, **pad)

        methods = ttk.LabelFrame(mid, text="Identification methods", padding=8)
        methods.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        self.method_vars: Dict[str, tk.BooleanVar] = {
            key: tk.BooleanVar(value=True) for key, _ in METHOD_LABELS
        }
        self._method_order: List[str] = [key for key, _ in METHOD_LABELS]
        meth_mid = ttk.Frame(methods)
        meth_mid.pack(fill=tk.BOTH, expand=True)
        self.method_tree = ttk.Treeview(
            meth_mid,
            columns=("on", "pri", "method"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        self.method_tree.heading("on", text="On")
        self.method_tree.heading("pri", text="#")
        self.method_tree.heading("method", text="Method")
        self.method_tree.column("on", width=36, anchor=tk.CENTER, stretch=False)
        self.method_tree.column("pri", width=28, anchor=tk.CENTER, stretch=False)
        self.method_tree.column("method", width=168, anchor=tk.W, stretch=True)
        meth_side = ttk.Frame(meth_mid)
        meth_side.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 0))
        ttk.Button(meth_side, text="↑", width=3, command=lambda: self._move_method(-1)).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(meth_side, text="↓", width=3, command=lambda: self._move_method(1)).pack(
            fill=tk.X, pady=2
        )
        self.method_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.method_tree.bind("<Button-1>", self._on_method_click)
        self.method_tree.bind("<space>", lambda _e: self._toggle_method())

        opts = ttk.LabelFrame(mid, text="Options", padding=8)
        opts.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.var_dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts,
            text="Test-run (don't write files)",
            variable=self.var_dry,
            command=self._on_dry_run_toggled,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W)

        self.var_skip_bios = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts,
            text="Don't mark for transfer BIOS / boot ROMs",
            variable=self.var_skip_bios,
            command=self._on_setting_changed,
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(2, 0))

        self.var_primary_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts,
            text="Primary folder only (no multi-platform duplicates)",
            variable=self.var_primary_only,
            command=self._on_setting_changed,
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(2, 0))

        self.var_relocate = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts,
            text="Relocate same-CRC files already on MiSTer (no re-copy)",
            variable=self.var_relocate,
            command=self._autosave,
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(2, 0))

        self.var_prune_empty = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts,
            text="Remove empty nested folders under platforms after transfer",
            variable=self.var_prune_empty,
            command=self._autosave,
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(2, 0))

        ttk.Label(opts, text="Unknown files:").grid(row=5, column=0, sticky=tk.W, pady=(4, 0))
        self.var_unknown = tk.StringVar(value="copy_to_unknown")
        unk = ttk.Frame(opts)
        unk.grid(row=5, column=1, sticky=tk.W, pady=(4, 0))
        ttk.Radiobutton(
            unk,
            text="Copy to _unknown",
            value="copy_to_unknown",
            variable=self.var_unknown,
            command=self._on_setting_changed,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            unk,
            text="Skip",
            value="skip",
            variable=self.var_unknown,
            command=self._on_setting_changed,
        ).pack(side=tk.LEFT)

        # Actions to the right of Options (vertical stack / 2 columns)
        actions = ttk.LabelFrame(mid, text="Actions", padding=8)
        actions.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        self.btn_dl = ttk.Button(
            actions, text="1. Update all DATs", command=self.on_download
        )
        self.btn_dl.grid(row=0, column=0, columnspan=2, sticky="ew", pady=2)
        self.btn_scan = ttk.Button(
            actions, text="2. Scan Source + Output", command=self.on_scan
        )
        self.btn_scan.grid(row=1, column=0, columnspan=2, sticky="ew", pady=2)
        self.btn_run = ttk.Button(
            actions, text="3. Run transfer", command=self.on_run, state=tk.DISABLED
        )
        self.btn_run.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        self.btn_pause = ttk.Button(
            actions, text="Pause", command=self.on_pause, state=tk.DISABLED
        )
        self.btn_pause.grid(row=3, column=0, sticky="ew", padx=(0, 4), pady=2)
        self.btn_stop = ttk.Button(
            actions, text="Stop", command=self.on_stop, state=tk.DISABLED
        )
        self.btn_stop.grid(row=3, column=1, sticky="ew", pady=2)
        ttk.Button(actions, text="Default settings", command=self.on_defaults).grid(
            row=4, column=0, sticky="ew", padx=(0, 4), pady=2
        )
        ttk.Button(actions, text="Exit", command=self.destroy).grid(
            row=4, column=1, sticky="ew", pady=2
        )
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)

        # Draggable splitters between Scan results / Progress / Log
        self.main_paned = ttk.Panedwindow(root, orient=tk.VERTICAL)
        self.main_paned.pack(fill=tk.BOTH, expand=True, **pad)

        planf = ttk.LabelFrame(self.main_paned, text="Scan results", padding=8)
        self.main_paned.add(planf)

        plan_btns = ttk.Frame(planf)
        plan_btns.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(plan_btns, text="Select all", command=self._select_all).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(plan_btns, text="Select none", command=self._select_none).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(plan_btns, text="Select new/diff only", command=self._select_needed).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(plan_btns, text="Unpack all", command=lambda: self._set_unpack_all(True)).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(plan_btns, text="Keep archives", command=lambda: self._set_unpack_all(False)).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(plan_btns, text="Expand all", command=self._expand_all).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(plan_btns, text="Collapse all", command=self._collapse_all).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(plan_btns, text="Copy results", command=self._copy_scan_results).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self.btn_revert_scan = ttk.Button(
            plan_btns,
            text="Revert last scan",
            command=self.on_revert_last_scan,
            state=tk.DISABLED,
        )
        self.btn_revert_scan.pack(side=tk.LEFT)
        search_fr = ttk.Frame(plan_btns)
        search_fr.pack(side=tk.RIGHT)
        ttk.Label(search_fr, text="Search:").pack(side=tk.LEFT, padx=(0, 4))
        self.ent_search = ttk.Entry(search_fr, textvariable=self._search_var, width=28)
        self.ent_search.pack(side=tk.LEFT)
        self._search_var.trace_add("write", lambda *_: self._on_search_changed())

        tree_wrap = ttk.Frame(planf)
        tree_wrap.pack(fill=tk.BOTH, expand=True)
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        cols = (
            "archive",
            "include",
            "unpack",
            "platform",
            "plan_size",
            "mister_path",
            "dest_status",
            "dest_size",
            "dat_name",
            "source",
            "info",
            "notes",
        )
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=cols,
            show="tree headings",
            height=8,
            selectmode="extended",
        )
        self._tree_data_cols = cols
        self._bind_tree_headings()
        self.tree.column("#0", width=260, anchor=tk.W, stretch=False)
        self.tree.column("archive", width=220, anchor=tk.W, stretch=False)
        self.tree.column("include", width=70, anchor=tk.CENTER, stretch=False)
        self.tree.column("unpack", width=70, anchor=tk.CENTER, stretch=False)
        self.tree.column("platform", width=110, anchor=tk.W, stretch=False)
        self.tree.column("plan_size", width=80, anchor=tk.E, stretch=False)
        self._platform_editor: Optional[ttk.Combobox] = None
        self.tree.column("mister_path", width=300, anchor=tk.W, stretch=False)
        self.tree.column("dest_status", width=95, anchor=tk.W, stretch=False)
        self.tree.column("dest_size", width=80, anchor=tk.E, stretch=False)
        self.tree.column("dat_name", width=220, anchor=tk.W, stretch=False)
        self.tree.column("source", width=260, anchor=tk.W, stretch=False)
        self.tree.column("info", width=260, anchor=tk.W, stretch=False)
        self.tree.column("notes", width=360, anchor=tk.W, stretch=False)
        tree_scroll_y = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        tree_scroll_x = ttk.Scrollbar(tree_wrap, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(
            yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self._build_tree_menu()

        self.lbl_summary = ttk.Label(planf, text="", foreground="#333")
        self.lbl_summary.pack(fill=tk.X, pady=(8, 0))

        # Bottom pane: fixed-height Progress + resizable Log
        bottom = ttk.Frame(self.main_paned)
        self.main_paned.add(bottom)

        prog = ttk.LabelFrame(bottom, text="Progress", padding=10)
        prog.pack(fill=tk.X, pady=(0, 6))
        prog.columnconfigure(0, weight=1)

        # Current = this file / sub-step (bytes, name, …)
        ttk.Label(prog, text="Current", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky=tk.W
        )
        self.lbl_current = ttk.Label(prog, text="Idle", foreground="#333")
        self.lbl_current.grid(row=1, column=0, sticky=tk.W)
        self.progress_op = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress_op.grid(row=2, column=0, sticky="ew", pady=(2, 6))
        # Back-compat alias
        self.progress = self.progress_op
        self.lbl_stage = self.lbl_current  # older pause paths
        self.lbl_detail = self.lbl_current

        # Overall = which scan/transfer phase + total %
        ttk.Label(prog, text="Overall", font=("Segoe UI", 9, "bold")).grid(
            row=3, column=0, sticky=tk.W
        )
        self.lbl_overall = ttk.Label(prog, text="", foreground="#333")
        self.lbl_overall.grid(row=4, column=0, sticky=tk.W)
        self.progress_all = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress_all.grid(row=5, column=0, sticky="ew", pady=(2, 0))
        self._progress_indeterminate = False

        logf = ttk.LabelFrame(bottom, text="Log", padding=8)
        logf.pack(fill=tk.BOTH, expand=True)
        self.log = tk.Text(logf, height=8, wrap=tk.WORD, font=("Consolas", 9))
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(logf, command=self.log.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.configure(yscrollcommand=scroll.set)
        self._build_log_menu()

        for pane, weight in ((planf, 3), (bottom, 2)):
            try:
                self.main_paned.pane(pane, weight=weight)
            except tk.TclError:
                pass

        self.var_source.trace_add("write", lambda *_: self._on_setting_changed())
        self.var_dest.trace_add("write", lambda *_: self._on_setting_changed())
        self.var_dat.trace_add("write", lambda *_: self._on_setting_changed())
        self.var_source_recursive.trace_add("write", lambda *_: self._on_setting_changed())
        self.var_dest_recursive.trace_add("write", lambda *_: self._on_setting_changed())
        self.var_dat_recursive.trace_add("write", lambda *_: self._on_setting_changed())
        # dry-run uses command=_on_dry_run_toggled (warns + autosave)
        self.var_skip_bios.trace_add("write", lambda *_: self._on_setting_changed())
        self.var_primary_only.trace_add("write", lambda *_: self._on_setting_changed())
        self.var_unknown.trace_add("write", lambda *_: self._on_setting_changed())
        self._loading = False

    def _warn_live_output(self, *, confirm: bool) -> bool:
        """
        Big warning about writing to Output.
        confirm=False → OK-only (Test-run unchecked); confirm=True → Yes/No before transfer.
        """
        dest = self._norm_display_path(self.var_dest.get())
        dlg = tk.Toplevel(self)
        dlg.title("WARNING — live write" if confirm else "WARNING — Test-run off")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=20)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frm,
            text="⚠ WARNING",
            font=("Segoe UI", 22, "bold"),
            foreground="#b00020",
        ).pack(anchor=tk.W)
        ttk.Label(
            frm,
            text="MAY DAMAGE OR OVERWRITE DATA\nIN THE OUTPUT FOLDER",
            font=("Segoe UI", 14, "bold"),
            foreground="#b00020",
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 12))
        body = (
            "Test-run is OFF.\n\n"
            "Real copy / extract will modify files here:\n\n"
            "(Source files are never deleted.)\n\n"
            f"{dest}\n\n"
        )
        if confirm:
            body += "Continue with a real transfer?"
        else:
            body += "Keep Test-run ON unless you intend to write for real."
        ttk.Label(frm, text=body, font=("Segoe UI", 10), justify=tk.LEFT, wraplength=520).pack(
            anchor=tk.W
        )
        result = {"ok": False}

        def on_yes() -> None:
            result["ok"] = True
            dlg.destroy()

        def on_no() -> None:
            result["ok"] = False
            dlg.destroy()

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=(18, 0))
        if confirm:
            ttk.Button(btns, text="Yes — write for real", command=on_yes).pack(
                side=tk.RIGHT, padx=(8, 0)
            )
            ttk.Button(btns, text="No — cancel", command=on_no).pack(side=tk.RIGHT)
        else:
            ttk.Button(btns, text="OK", command=on_yes).pack(side=tk.RIGHT)
        dlg.protocol("WM_DELETE_WINDOW", on_no)
        dlg.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dlg.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dlg.winfo_height()) // 3)
        dlg.geometry(f"+{x}+{y}")
        self.wait_window(dlg)
        return bool(result["ok"])

    def _on_dry_run_toggled(self) -> None:
        if self._loading:
            return
        if not bool(self.var_dry.get()):
            self._warn_live_output(confirm=False)
        self._autosave()

    def _confirm_live_transfer(self) -> bool:
        """Extra yes/no when Test-run is off."""
        return self._warn_live_output(confirm=True)

    def _build_tree_menu(self) -> None:
        self._tree_menu = tk.Menu(self.tree, tearoff=0)
        self._tree_menu.add_command(
            label="Open source location", command=self._tree_open_source_location
        )
        self._tree_menu.add_command(
            label="Open MiSTer location", command=self._tree_open_mister_location
        )
        self.tree.bind("<Button-3>", self._show_tree_menu)
        self.tree.bind("<App>", self._show_tree_menu)
        self.tree.bind("<Shift-F10>", self._show_tree_menu)

    def _show_tree_menu(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.tree.focus(row)
        try:
            self._tree_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._tree_menu.grab_release()

    def _focused_plan_item(self) -> Optional[PlannedItem]:
        row = self.tree.focus() or (
            self.tree.selection()[0] if self.tree.selection() else ""
        )
        if not row:
            return None
        meta = self._node_meta.get(row)
        if not meta:
            return None
        if meta[0] == "file":
            _, folder, idx = meta
            plan = self._folders.get(folder)
            if plan and 0 <= idx < len(plan.items):
                return plan.items[idx]
            return None
        if meta[0] == "folder":
            plan = self._folders.get(meta[1])
            if plan and plan.items:
                return plan.items[0]
        return None

    def _tree_open_source_location(self) -> None:
        row = self.tree.focus() or (
            self.tree.selection()[0] if self.tree.selection() else ""
        )
        meta = self._node_meta.get(row) if row else None
        if meta and meta[0] == "folder":
            plan = self._folders.get(meta[1])
            if plan and plan.items:
                # Common parent of sources when possible; else first source folder
                self._reveal_in_explorer(plan.items[0].source.parent)
            return
        item = self._focused_plan_item()
        if item is None:
            return
        self._reveal_in_explorer(item.source)

    def _tree_open_mister_location(self) -> None:
        row = self.tree.focus() or (
            self.tree.selection()[0] if self.tree.selection() else ""
        )
        meta = self._node_meta.get(row) if row else None
        dest_root = Path(self._norm_display_path(self.var_dest.get()))
        if meta and meta[0] == "folder":
            self._reveal_in_explorer(dest_root / meta[1])
            return
        item = self._focused_plan_item()
        if item is None:
            return
        rel = item.active_rel().replace("\\", "/").strip("/")
        if not rel:
            messagebox.showerror("MiSTer path", "No MiSTer path for this row.")
            return
        path = dest_root / Path(*rel.split("/"))
        self._reveal_in_explorer(path)

    def _bind_tree_headings(self) -> None:
        for col, title in self._heading_titles.items():
            self.tree.heading(
                col,
                text=title,
                command=lambda c=col: self._on_sort_heading(c),
            )
        self._refresh_heading_labels()

    def _refresh_heading_labels(self) -> None:
        for col, title in self._heading_titles.items():
            if col == self._sort_col:
                mark = " ▼" if self._sort_reverse else " ▲"
                self.tree.heading(col, text=title + mark)
            else:
                self.tree.heading(col, text=title)

    def _on_sort_heading(self, col: str) -> None:
        if self._busy:
            return
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        self._refresh_heading_labels()
        self._refresh_tree()

    def _on_search_changed(self) -> None:
        if self._loading or self._busy:
            return
        if not self._folders:
            return
        self._refresh_tree()

    def _item_matches_search(self, item: PlannedItem, query: str) -> bool:
        if not query:
            return True
        q = query.casefold()
        chunks = [
            self._file_label(item),
            self._archive_label(item),
            item.folder,
            item.active_rel(),
            status_label(item.dest_status),
            item.dat_name,
            str(item.source),
            item.method,
            item.reason,
            item.notes,
            format_size(item.active_bytes()),
            format_size(item.dest_bytes) if item.dest_bytes else "",
            MARK_ON if item.include else MARK_OFF,
            MARK_ON if item.unpack else MARK_OFF,
            item.dest_rel,
        ]
        return any(q in str(c).casefold() for c in chunks if c)

    def _item_sort_key(self, item: PlannedItem, col: str):
        if col == "#0":
            return self._file_label(item).casefold()
        if col == "plan_size":
            return item.active_bytes()
        if col == "dest_size":
            return item.dest_bytes
        if col == "include":
            return 0 if item.include else 1
        if col == "unpack":
            return 0 if (item.unpack if item.is_archive else 2) else 1
        if col == "platform":
            return item.folder.casefold()
        vals = self._file_values(item)
        try:
            idx = self._tree_data_cols.index(col)
        except ValueError:
            return self._file_label(item).casefold()
        return str(vals[idx]).casefold()

    def _reveal_in_explorer(self, path: Path) -> None:
        try:
            target = path if path.exists() else path.parent
            if not target.exists():
                messagebox.showerror("Not found", f"Path does not exist:\n{path}")
                return
            if target.is_file():
                subprocess.run(
                    ["explorer", "/select,", str(target.resolve())],
                    check=False,
                )
            else:
                subprocess.run(["explorer", str(target.resolve())], check=False)
        except OSError as exc:
            messagebox.showerror("Explorer", str(exc))

    def _build_log_menu(self) -> None:
        self._log_menu = tk.Menu(self.log, tearoff=0)
        self._log_menu.add_command(label="Copy selected", command=self._log_copy_selected)
        self._log_menu.add_command(label="Copy all", command=self._log_copy_all)
        self._log_menu.add_separator()
        self._log_menu.add_command(label="Clear", command=self._log_clear)
        self.log.bind("<Button-3>", self._show_log_menu)
        self.log.bind("<App>", self._show_log_menu)
        self.log.bind("<Shift-F10>", self._show_log_menu)

    def _show_log_menu(self, event: tk.Event) -> None:
        try:
            self._log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._log_menu.grab_release()

    def _log_copy_selected(self) -> None:
        try:
            text = self.log.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(text)

    def _log_copy_all(self) -> None:
        text = self.log.get("1.0", tk.END)
        if text.endswith("\n"):
            text = text[:-1]
        self.clipboard_clear()
        self.clipboard_append(text)

    def _log_clear(self) -> None:
        self.log.delete("1.0", tk.END)

    @staticmethod
    def _norm_display_path(raw: str) -> str:
        """Windows-style separators for display (\\\\server\\share\\…)."""
        p = (raw or "").strip()
        if not p:
            return p
        # Preserve UNC: //server/share → \\server\share
        if p.startswith("//"):
            p = "\\\\" + p[2:]
        return p.replace("/", "\\")

    def _path_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        var: tk.StringVar,
        browse: Callable[[], None],
        *,
        recursive_var: Optional[tk.BooleanVar] = None,
        extra: Optional[Tuple[str, Callable[[], None]]] = None,
    ) -> None:
        # Columns aligned on every row:
        # 0 label | 1 path cell (entry [+ DAT files flush right]) | 2 Browse | 3 Subfolders
        # Right edge of DAT files… = right edge of Source/Output path entries.
        ttk.Label(parent, text=label, width=22).grid(row=row, column=0, sticky=tk.W, pady=3)
        path_cell = ttk.Frame(parent)
        path_cell.grid(row=row, column=1, sticky=tk.EW, padx=6, pady=3)
        if extra:
            text, cmd = extra
            btn = ttk.Button(path_cell, text=text, width=11, command=cmd)
            btn.pack(side=tk.RIGHT)
            if text.startswith("DAT"):
                self.btn_dats = btn
            ent = ttk.Entry(path_cell, textvariable=var)
            ent.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        else:
            ent = ttk.Entry(path_cell, textvariable=var)
            ent.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(parent, text="Browse…", width=10, command=browse).grid(
            row=row, column=2, pady=3, padx=(0, 4)
        )
        if recursive_var is not None:
            ttk.Checkbutton(
                parent, text="Subfolders", variable=recursive_var
            ).grid(row=row, column=3, sticky=tk.W, pady=3)

    def _browse_initial_dir(self, raw: str) -> Optional[str]:
        """Open the currently selected folder (even if UNC / slow)."""
        p = self._norm_display_path(raw)
        if not p:
            return None
        return p

    def _browse_source(self) -> None:
        path = filedialog.askdirectory(
            title="Source folder",
            initialdir=self._browse_initial_dir(self.var_source.get()),
        )
        if path:
            self.var_source.set(self._norm_display_path(path))

    def _browse_dest(self) -> None:
        path = filedialog.askdirectory(
            title="Output (MiSTer library)",
            initialdir=self._browse_initial_dir(self.var_dest.get()),
        )
        if path:
            self.var_dest.set(self._norm_display_path(path))

    def _browse_dat(self) -> None:
        path = filedialog.askdirectory(
            title="DATs Folder",
            initialdir=self._browse_initial_dir(self.var_dat.get()),
        )
        if path:
            self.var_dat.set(self._norm_display_path(path))

    def _list_files(
        self,
        root: Path,
        *,
        recursive: bool,
        on_progress: Optional[Callable[[int, Path], None]] = None,
        with_sizes: bool = False,
    ) -> Union[List[Path], List[Tuple[Path, int]]]:
        """
        List files under root; optional subfolders. Skips dotfiles.
        If with_sizes: return List[Tuple[Path, int]] (size from one stat).
        """
        out_paths: List[Path] = []
        out_sized: List[Tuple[Path, int]] = []
        if not root.exists():
            return out_sized if with_sizes else out_paths
        try:
            iterator = root.rglob("*") if recursive else root.iterdir()
        except OSError:
            return out_sized if with_sizes else out_paths
        n = 0
        for p in iterator:
            self._check_control()
            if p.name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            if with_sizes:
                out_sized.append((p, int(st.st_size)))
            else:
                out_paths.append(p)
            n += 1
            if on_progress and (n == 1 or n % 25 == 0):
                on_progress(n, p)
        if on_progress and n:
            last = out_sized[-1][0] if with_sizes else out_paths[-1]
            on_progress(n, last)
        return out_sized if with_sizes else out_paths

    def _normalize_method_order(self, preferred: List[str]) -> List[str]:
        order: List[str] = []
        for key in preferred:
            if key in METHOD_LABEL_BY_KEY and key not in order:
                order.append(key)
        for key, _ in METHOD_LABELS:
            if key not in order:
                order.append(key)
        return order

    def _reload_method_list(self, *, select_key: Optional[str] = None) -> None:
        selected = select_key
        if selected is None:
            cur = self.method_tree.selection()
            if cur:
                selected = cur[0]
        self.method_tree.delete(*self.method_tree.get_children())
        for i, key in enumerate(self._method_order):
            on = MARK_ON if self.method_vars[key].get() else MARK_OFF
            self.method_tree.insert(
                "",
                tk.END,
                iid=key,
                values=(on, str(i + 1), METHOD_LABEL_BY_KEY.get(key, key)),
            )
        if selected and self.method_tree.exists(selected):
            self.method_tree.selection_set(selected)
            self.method_tree.focus(selected)

    def _move_method(self, delta: int) -> None:
        sel = self.method_tree.selection()
        if not sel:
            return
        key = sel[0]
        try:
            idx = self._method_order.index(key)
        except ValueError:
            return
        j = idx + delta
        if j < 0 or j >= len(self._method_order):
            return
        self._method_order[idx], self._method_order[j] = (
            self._method_order[j],
            self._method_order[idx],
        )
        self._reload_method_list(select_key=key)
        self._on_setting_changed()

    def _toggle_method(self) -> None:
        sel = self.method_tree.selection()
        if not sel:
            return
        key = sel[0]
        self.method_vars[key].set(not self.method_vars[key].get())
        self._reload_method_list(select_key=key)
        self._on_setting_changed()

    def _on_method_click(self, event: tk.Event) -> Optional[str]:
        """Single-click On column toggles; otherwise just select the row."""
        region = self.method_tree.identify_region(event.x, event.y)
        if region != "cell":
            return None
        row = self.method_tree.identify_row(event.y)
        col = self.method_tree.identify_column(event.x)
        if not row:
            return None
        self.method_tree.selection_set(row)
        self.method_tree.focus(row)
        if col == "#1":  # On
            self.method_vars[row].set(not self.method_vars[row].get())
            self._reload_method_list(select_key=row)
            self._on_setting_changed()
            return "break"
        return None

    def _autosave(self, *_args) -> None:
        if getattr(self, "_loading", False) or self._busy:
            return
        self.settings = self._form_to_settings()
        save_settings(self.settings)

    def _on_setting_changed(self, *_args) -> None:
        if getattr(self, "_loading", False):
            return
        # Keep scan results; user can re-scan when ready.
        self._autosave()

    def _load_into_form(self) -> None:
        self._loading = True
        try:
            s = self.settings
            self.var_source.set(self._norm_display_path(s.source_path))
            self.var_dest.set(self._norm_display_path(s.destination_path))
            self.var_dat.set(self._norm_display_path(s.dat_path))
            self.var_source_recursive.set(s.source_recursive)
            self.var_dest_recursive.set(s.dest_recursive)
            self.var_dat_recursive.set(s.dat_recursive)
            self.var_dry.set(s.dry_run)
            self.var_skip_bios.set(s.skip_bios)
            self.var_primary_only.set(s.primary_only)
            self.var_relocate.set(s.relocate_on_mister)
            self.var_prune_empty.set(s.prune_empty_dirs)
            self.var_unknown.set(s.unknown_mode)
            enabled = set(s.methods)
            self._method_order = self._normalize_method_order(list(s.method_order))
            for key, var in self.method_vars.items():
                var.set(key in enabled)
            self._reload_method_list()
        finally:
            self._loading = False

    def _form_to_settings(self) -> Settings:
        order = self._normalize_method_order(list(self._method_order))
        methods = [k for k in order if self.method_vars[k].get()]
        if not methods:
            methods = list(METHOD_ORDER_DEFAULT)
        return Settings(
            source_path=self._norm_display_path(self.var_source.get()),
            destination_path=self._norm_display_path(self.var_dest.get()),
            dat_path=self._norm_display_path(self.var_dat.get()) or str(DEFAULT_DATS),
            methods=methods,
            method_order=order,
            source_recursive=bool(self.var_source_recursive.get()),
            dest_recursive=bool(self.var_dest_recursive.get()),
            dat_recursive=bool(self.var_dat_recursive.get()),
            dry_run=bool(self.var_dry.get()),
            unknown_mode=self.var_unknown.get(),
            skip_bios=bool(self.var_skip_bios.get()),
            primary_only=bool(self.var_primary_only.get()),
            relocate_on_mister=bool(self.var_relocate.get()),
            prune_empty_dirs=bool(self.var_prune_empty.get()),
        )

    def log_line(self, text: str) -> None:
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)

    def set_progress(
        self,
        stage: str,
        frac: float,
        detail: str = "",
        *,
        overall: Optional[float] = None,
        indeterminate: bool = False,
    ) -> None:
        """
        Current bar = frac of the current phase unit (files done/total, CRC of file, …).
        Current label = detail (must match what frac represents).
        Overall bar/label = phase name (stage) + overall fraction.
        """
        cur = max(0.0, min(1.0, float(frac)))
        # Always determinate for Current — indeterminate hid real % while labels moved
        if self._progress_indeterminate:
            self.progress_op.stop()
            self.progress_op.configure(mode="determinate", maximum=100)
            self._progress_indeterminate = False
        if indeterminate and cur <= 0.0:
            # Unknown total: keep bar at 0, still show live detail text
            self.progress_op["value"] = 0.0
            self.lbl_current.configure(text=detail or "…")
        else:
            self.progress_op["value"] = cur * 100.0
            pct = f"{cur * 100.0:.0f}%"
            text = detail or "…"
            if text and not text.rstrip().endswith("%"):
                text = f"{text}  ·  {pct}"
            self.lbl_current.configure(text=text)
        if overall is not None:
            ov = max(0.0, min(1.0, float(overall)))
            self.progress_all["value"] = ov * 100.0
            self.lbl_overall.configure(text=f"{stage}  ·  {ov * 100.0:.0f}%")
        else:
            self.lbl_overall.configure(text=stage)

    def _emit_progress(
        self,
        stage: str,
        frac: float,
        detail: str = "",
        *,
        overall: Optional[float] = None,
        indeterminate: bool = False,
    ) -> None:
        self._emit(
            "progress",
            {
                "stage": stage,
                "frac": frac,
                "detail": detail,
                "overall": overall,
                "indeterminate": indeterminate,
            },
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.btn_dl.configure(state=state)
        self.btn_scan.configure(state=state)
        if hasattr(self, "btn_dats"):
            self.btn_dats.configure(state=state)
        if hasattr(self, "btn_revert_scan"):
            self.btn_revert_scan.configure(
                state=tk.DISABLED
                if busy or not self._scan_snapshot
                else tk.NORMAL
            )
        ctrl = tk.NORMAL if busy else tk.DISABLED
        self.btn_pause.configure(state=ctrl)
        self.btn_stop.configure(state=ctrl)
        if not busy:
            self._paused = False
            self._pause.clear()
            self._stop.clear()
            self.btn_pause.configure(text="Pause")
            self._refresh_run_state()
        else:
            self.btn_run.configure(state=tk.DISABLED)

    def _reset_control(self) -> None:
        self._stop.clear()
        self._pause.clear()
        self._paused = False
        self.btn_pause.configure(text="Pause")

    def _check_control(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.12)
        if self._stop.is_set():
            raise InterruptedError("Stopped by user")

    def _refresh_run_state(self) -> None:
        has = any(it.include for f in self._folders.values() for it in f.items)
        self.btn_run.configure(state=tk.NORMAL if has and not self._busy else tk.DISABLED)

    def _transferable_items(self, items: List[PlannedItem]) -> List[PlannedItem]:
        """Items Transfer-on may select (BIOS skipped when that option is on)."""
        if not bool(self.var_skip_bios.get()):
            return list(items)
        return [it for it in items if not it.bios_like]

    def _folder_include_mark(self, plan: FolderPlan) -> str:
        """
        Folder Transfer mark. With skip-BIOS, bios rows stay off and are ignored
        so the platform can show fully on/off instead of stuck partial ☒.
        """
        items = self._transferable_items(plan.items)
        if not items:
            items = plan.items
        if not items:
            return MARK_OFF
        sel = sum(1 for it in items if it.include)
        if sel == 0:
            return MARK_OFF
        if sel == len(items):
            return MARK_ON
        return MARK_SOME

    def _folder_values(self, plan: FolderPlan) -> tuple:
        same = sum(1 for it in plan.items if it.dest_status == STATUS_SAME)
        diff = sum(1 for it in plan.items if it.dest_status == STATUS_DIFF)
        missing = sum(1 for it in plan.items if it.dest_status == STATUS_NEW)
        other = sum(1 for it in plan.items if it.dest_status == STATUS_OTHER)
        status = f"new {missing} · same {same} · diff {diff}"
        if other:
            status += f" · else {other}"
        return (
            "",
            self._folder_include_mark(plan),
            plan.unpack_mark(),
            "",
            format_size(plan.plan_bytes()),
            "",
            status,
            format_size(plan.dest_present_bytes()),
            "",
            "",
            f"{plan.file_count} files, {plan.archive_count} archives",
            "",
        )

    def _file_label(self, item: PlannedItem) -> str:
        if item.is_archive and item.zip_member:
            return Path(item.zip_member).name
        return item.source.name

    def _archive_label(self, item: PlannedItem) -> str:
        if item.is_archive:
            return item.source.name
        return "—"

    def _file_values(self, item: PlannedItem) -> tuple:
        unpack = MARK_ON if item.unpack else MARK_OFF
        if not item.is_archive:
            unpack = "—"
        info = item.method or "—"
        if item.dest_status == STATUS_OTHER and item.dest_rel:
            info = f"{info} · also {item.dest_rel}"
        elif item.active_crc():
            info = f"{info} · crc {item.active_crc()}"
        return (
            self._archive_label(item),
            MARK_ON if item.include else MARK_OFF,
            unpack,
            item.folder,
            format_size(item.active_bytes()),
            item.active_rel(),
            status_label(item.dest_status),
            format_size(item.dest_bytes) if item.dest_bytes else "—",
            item.dat_name or "—",
            str(item.source),
            info,
            item.notes or "",
        )

    def _refresh_tree(self) -> None:
        """Rebuild tree; platform groups stay alpha-sorted, files sorted/filtered inside."""
        open_folders = {
            self.tree.item(iid, "text")
            for iid in self.tree.get_children("")
            if self.tree.item(iid, "open")
        }
        query = (self._search_var.get() or "").strip()
        force_open = bool(query)
        self.tree.delete(*self.tree.get_children())
        self._node_meta.clear()
        self._folder_iids.clear()
        self._file_iids.clear()
        sort_col = self._sort_col or "#0"
        reverse = bool(self._sort_reverse)
        for name in sorted(self._folders.keys(), key=lambda s: s.casefold()):
            plan = self._folders[name]
            pairs = [
                (idx, item)
                for idx, item in enumerate(plan.items)
                if self._item_matches_search(item, query)
            ]
            if not pairs:
                continue
            pairs.sort(
                key=lambda pair: self._item_sort_key(pair[1], sort_col),
                reverse=reverse,
            )
            fid = self.tree.insert(
                "",
                tk.END,
                text=plan.name,
                values=self._folder_values(plan),
                open=force_open or (plan.name in open_folders),
            )
            self._node_meta[fid] = ("folder", name)
            self._folder_iids[name] = fid
            for idx, item in pairs:
                cid = self.tree.insert(
                    fid,
                    tk.END,
                    text=self._file_label(item),
                    values=self._file_values(item),
                )
                self._node_meta[cid] = ("file", name, idx)
                self._file_iids[(name, idx)] = cid
        self._refresh_heading_labels()
        self._update_summary()
        self._refresh_run_state()

    def _close_platform_editor(self) -> None:
        ed = getattr(self, "_platform_editor", None)
        if ed is not None:
            try:
                ed.destroy()
            except tk.TclError:
                pass
            self._platform_editor = None

    def _platform_combo_values(self) -> List[str]:
        """Scan platforms first, then the rest of games/ folders (native combobox list)."""
        present = sorted(self._folders.keys(), key=lambda s: s.casefold())
        present_cf = {p.casefold() for p in present}
        other = sorted(
            (f for f in MISTER_CORE_FOLDERS if f.casefold() not in present_cf),
            key=lambda s: s.casefold(),
        )
        if "_unknown".casefold() not in present_cf:
            other = ["_unknown", *other]
        if not other:
            return present
        return [*present, "──────────", *other]

    def _folder_insert_index(self, folder: str) -> int:
        """Root index to keep folder blocks alphabetically sorted."""
        keys = sorted(self._folders.keys(), key=lambda s: s.casefold())
        try:
            return keys.index(folder)
        except ValueError:
            return tk.END

    def _ensure_folder_row(self, folder: str) -> str:
        fid = self._folder_iids.get(folder)
        if fid and self.tree.exists(fid):
            return fid
        plan = self._folders[folder]
        fid = self.tree.insert(
            "",
            self._folder_insert_index(folder),
            text=plan.name,
            values=self._folder_values(plan),
            open=True,
        )
        self._node_meta[fid] = ("folder", folder)
        self._folder_iids[folder] = fid
        return fid

    def _shift_file_indexes(self, folder: str, removed_idx: int) -> None:
        """After removing one item, compact file index keys for that folder."""
        updates: List[Tuple[Tuple[str, int], str]] = []
        for key, cid in list(self._file_iids.items()):
            if key[0] != folder:
                continue
            _f, i = key
            del self._file_iids[key]
            if i == removed_idx:
                self._node_meta.pop(cid, None)
                continue
            new_i = i - 1 if i > removed_idx else i
            updates.append(((folder, new_i), cid))
        for key, cid in updates:
            self._file_iids[key] = cid
            self._node_meta[cid] = ("file", key[0], key[1])

    def _update_file_row(self, folder: str, idx: int) -> None:
        plan = self._folders.get(folder)
        if not plan or idx >= len(plan.items):
            return
        item = plan.items[idx]
        iid = self._file_iids.get((folder, idx))
        if not iid:
            return
        self.tree.item(iid, text=self._file_label(item), values=self._file_values(item))

    def _update_folder_row(self, folder: str) -> None:
        plan = self._folders.get(folder)
        iid = self._folder_iids.get(folder)
        if not plan or not iid:
            return
        self.tree.item(iid, values=self._folder_values(plan))

    def _update_folder_and_children(self, folder: str) -> None:
        plan = self._folders.get(folder)
        if not plan:
            return
        self._update_folder_row(folder)
        for idx in range(len(plan.items)):
            self._update_file_row(folder, idx)

    def _update_all_rows(self) -> None:
        for name in self._folders:
            self._update_folder_and_children(name)
        self._update_summary()
        self._refresh_run_state()

    def _rematch_items(self, items: List[PlannedItem], *, auto_uncheck_same: bool = True) -> None:
        """In-memory CRC rematch only (no I/O)."""
        for item in items:
            prev = item.include
            match_item_against_dest(item, self._dest_by_rel, self._dest_by_crc)
            if auto_uncheck_same and item.dest_status == STATUS_SAME:
                item.include = False
            elif not auto_uncheck_same:
                item.include = prev
            elif item.dest_status != STATUS_SAME:
                item.include = prev

    def _update_summary(self) -> None:
        selected = [it for f in self._folders.values() for it in f.items if it.include]
        same = sum(
            1 for f in self._folders.values() for it in f.items if it.dest_status == STATUS_SAME
        )
        plan_sz = 0
        seen_packed: set = set()
        for it in selected:
            if it.unpack and it.is_archive:
                plan_sz += it.unpacked_bytes
            else:
                key = (str(it.source), it.rel_packed)
                if key in seen_packed:
                    continue
                seen_packed.add(key)
                plan_sz += it.packed_bytes
        dest_sz = sum(
            it.dest_bytes
            for f in self._folders.values()
            for it in f.items
            if it.dest_status in (STATUS_SAME, STATUS_DIFF)
        )
        self.lbl_summary.configure(
            text=(
                f"Transfer: {len(selected)} item(s) → {format_size(plan_sz)}   |   "
                f"Already same CRC: {same}   |   "
                f"Matched on MiSTer now: {format_size(dest_sz)}"
            )
        )

    @staticmethod
    def _swap_platform_root(rel: str, new_folder: str) -> str:
        """Replace first path segment (platform); keep the rest unchanged."""
        rel = (rel or "").replace("\\", "/").strip("/")
        if not rel:
            return new_folder
        parts = rel.split("/", 1)
        if len(parts) == 1:
            return f"{new_folder}/{parts[0]}"
        return f"{new_folder}/{parts[1]}"

    def _selected_file_items(self, clicked_row: str = "") -> List[PlannedItem]:
        """File items from tree selection; folder rows expand to all children."""
        iids = list(self.tree.selection())
        if clicked_row and clicked_row not in iids:
            iids = [clicked_row]
        items: List[PlannedItem] = []
        seen: set = set()
        for iid in iids:
            meta = self._node_meta.get(iid)
            if not meta:
                continue
            if meta[0] == "file":
                folder, idx = meta[1], meta[2]
                plan = self._folders.get(folder)
                if not plan or idx >= len(plan.items):
                    continue
                item = plan.items[idx]
                key = id(item)
                if key not in seen:
                    seen.add(key)
                    items.append(item)
            elif meta[0] == "folder":
                plan = self._folders.get(meta[1])
                if not plan:
                    continue
                for item in plan.items:
                    key = id(item)
                    if key not in seen:
                        seen.add(key)
                        items.append(item)
        return items

    def _ensure_row_in_selection(self, row: str, event: tk.Event) -> None:
        """Keep Windows-like multi-select when clicking action cells."""
        if row in self.tree.selection():
            return
        ctrl = bool(event.state & 0x4)
        if ctrl:
            self.tree.selection_add(row)
        else:
            self.tree.selection_set(row)
        self.tree.focus(row)

    def _touch_item_rows(self, items: List[PlannedItem]) -> None:
        for folder in {it.folder for it in items}:
            self._update_folder_and_children(folder)

    def _reassign_platforms(self, items: List[PlannedItem], new_folder: str) -> None:
        """Move many files to another platform; one tree rebuild at the end."""
        new_folder = (new_folder or "").strip()
        if not new_folder or not items:
            return
        moving = [it for it in items if it.folder != new_folder]
        if not moving:
            return

        for item in moving:
            old = item.folder
            plan = self._folders.get(old)
            if plan and item in plan.items:
                plan.items.remove(item)
            if plan is not None and not plan.items and old in self._folders:
                del self._folders[old]
            item.folder = new_folder
            item.rel_unpacked = self._swap_platform_root(item.rel_unpacked, new_folder)
            item.rel_packed = self._swap_platform_root(item.rel_packed, new_folder)
            if item.is_archive:
                item.unpack = default_unpack_archive(
                    new_folder, item.zip_member, True
                )

        dest = self._folders.get(new_folder)
        if dest is None:
            dest = FolderPlan(name=new_folder)
            self._folders[new_folder] = dest
        for item in moving:
            dest.items.append(item)
        self._rematch_items(moving, auto_uncheck_same=False)
        self._refresh_tree()
        # Reselect moved rows
        sel: List[str] = []
        for folder_name, plan in self._folders.items():
            for idx, it in enumerate(plan.items):
                if it in moving:
                    cid = self._file_iids.get((folder_name, idx))
                    if cid:
                        sel.append(cid)
        if sel:
            self.tree.selection_set(sel)
            self.tree.focus(sel[0])
            self.tree.see(sel[0])
            fid = self._folder_iids.get(new_folder)
            if fid:
                self.tree.item(fid, open=True)
        self._update_summary()
        self._refresh_run_state()

    def _edit_platform_cell(self, row: str, items: List[PlannedItem]) -> None:
        """Native Windows-style combobox; applies to all given items."""
        self._close_platform_editor()
        if not items:
            return
        bbox = self.tree.bbox(row, "platform")
        if not bbox:
            return
        x, y, w, h = bbox
        current = items[0].folder
        cb = ttk.Combobox(
            self.tree, values=self._platform_combo_values(), state="readonly"
        )
        cb.set(current)
        cb.place(x=x, y=y, width=max(w, 120), height=max(h, 22))
        cb.focus_set()
        self._platform_editor = cb
        applied = {"done": False}
        # Snapshot items now — tree may rebuild after apply
        targets = list(items)

        def apply(_evt=None) -> None:
            if applied["done"]:
                return
            choice = (cb.get() or "").strip()
            if not choice or choice.startswith("─"):
                cb.set(current)
                return
            applied["done"] = True
            self._close_platform_editor()
            self._reassign_platforms(targets, choice)

        def cancel(_evt=None) -> None:
            if applied["done"]:
                return
            applied["done"] = True
            self._close_platform_editor()

        cb.bind("<<ComboboxSelected>>", apply)
        cb.bind("<Return>", apply)
        cb.bind("<Escape>", cancel)
        self.after(1, lambda: cb.event_generate("<Button-1>"))

    def _on_tree_click(self, event: tk.Event) -> Optional[str]:
        if self._busy:
            return None
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            self._close_platform_editor()
            return None
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return None
        meta = self._node_meta.get(row)
        if not meta:
            return None

        # Action columns (#2 Transfer, #3 Unpack, #4 Platform) — #1 is Archive
        if col in {"#2", "#3", "#4"}:
            self._close_platform_editor()
            self._ensure_row_in_selection(row, event)
            items = self._selected_file_items(row)
            if not items:
                return "break"

            if col == "#2":
                skip_bios = bool(self.var_skip_bios.get())
                if meta[0] == "folder":
                    plan = self._folders.get(meta[1])
                    mark = (
                        self._folder_include_mark(plan) if plan else MARK_OFF
                    )
                    # OFF → select; ON or partial → clear (so ☒ is never stuck)
                    turn_on = mark == MARK_OFF
                elif len(items) == 1:
                    turn_on = not items[0].include
                else:
                    xfer = self._transferable_items(items)
                    pool = xfer if xfer else items
                    turn_on = not any(it.include for it in pool)
                for it in items:
                    if turn_on and skip_bios and it.bios_like:
                        it.include = False
                    else:
                        it.include = turn_on
                self._touch_item_rows(items)
                self._update_summary()
                self._refresh_run_state()
                return "break"

            if col == "#3":
                archives = [it for it in items if it.is_archive]
                if not archives:
                    return "break"
                turn_on = not all(it.unpack for it in archives)
                for it in archives:
                    it.unpack = turn_on
                self._rematch_items(archives)
                self._touch_item_rows(items)
                self._update_summary()
                self._refresh_run_state()
                return "break"

            if col == "#4":
                self._edit_platform_cell(row, items)
                return "break"

        self._close_platform_editor()
        return None

    def _copy_scan_results(self) -> None:
        """Copy full scan results as tab-separated text."""
        headers = [
            "Source File",
            "Source Archive",
            "Transfer",
            "Unpack",
            "Platform",
            "Size",
            "Output MiSTer path",
            "On MiSTer",
            "Size on MiSTer",
            "DAT file",
            "Source path",
            "Match / method",
            "Notes",
        ]
        lines = ["\t".join(headers)]
        for name in sorted(self._folders.keys(), key=lambda s: s.casefold()):
            plan = self._folders[name]
            for item in sorted(plan.items, key=lambda it: self._file_label(it).casefold()):
                unpack = "yes" if item.unpack else "no"
                if not item.is_archive:
                    unpack = "—"
                info = item.method or "—"
                if item.dest_status == STATUS_OTHER and item.dest_rel:
                    info = f"{info} · also {item.dest_rel}"
                elif item.active_crc():
                    info = f"{info} · crc {item.active_crc()}"
                cells = [
                    self._file_label(item),
                    self._archive_label(item),
                    "yes" if item.include else "no",
                    unpack,
                    item.folder,
                    format_size(item.active_bytes()),
                    item.active_rel(),
                    status_label(item.dest_status),
                    format_size(item.dest_bytes) if item.dest_bytes else "—",
                    item.dat_name or "—",
                    str(item.source),
                    info,
                    item.notes or "",
                ]
                lines.append("\t".join(cells))
        text = "\n".join(lines)
        if len(lines) <= 1:
            messagebox.showinfo("Copy results", "Scan results are empty.", parent=self)
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
        except tk.TclError as exc:
            messagebox.showerror("Copy results", str(exc), parent=self)
            return
        self.log_line(f"Copied scan results: {len(lines) - 1} row(s)")

    def _select_all(self) -> None:
        skip_bios = bool(self.var_skip_bios.get())
        for plan in self._folders.values():
            for it in plan.items:
                it.include = not (skip_bios and it.bios_like)
        self._update_all_rows()

    def _select_none(self) -> None:
        for plan in self._folders.values():
            for it in plan.items:
                it.include = False
        self._update_all_rows()

    def _select_needed(self) -> None:
        skip_bios = bool(self.var_skip_bios.get())
        for plan in self._folders.values():
            for it in plan.items:
                needed = it.dest_status in (STATUS_NEW, STATUS_DIFF, STATUS_OTHER)
                it.include = needed and not (skip_bios and it.bios_like)
        self._update_all_rows()

    def _set_unpack_all(self, unpack: bool) -> None:
        changed: List[PlannedItem] = []
        for plan in self._folders.values():
            plan.unpack = unpack
            for it in plan.items:
                if it.is_archive:
                    it.unpack = unpack
                    changed.append(it)
        self._rematch_items(changed)
        self._update_all_rows()

    def _expand_all(self) -> None:
        for iid in self.tree.get_children(""):
            self.tree.item(iid, open=True)

    def _collapse_all(self) -> None:
        for iid in self.tree.get_children(""):
            self.tree.item(iid, open=False)

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self.log_line(payload)
                elif kind == "progress":
                    if isinstance(payload, dict):
                        self.set_progress(
                            str(payload.get("stage") or ""),
                            float(payload.get("frac") or 0.0),
                            str(payload.get("detail") or ""),
                            overall=payload.get("overall"),
                            indeterminate=bool(payload.get("indeterminate")),
                        )
                    else:
                        stage, frac, detail = payload[0], payload[1], payload[2]
                        overall = payload[3] if len(payload) > 3 else None
                        self.set_progress(stage, frac, detail, overall=overall)
                elif kind == "scan_done":
                    self._set_busy(False)
                    folders, by_rel, by_crc = payload
                    self._folders = folders
                    self._dest_by_rel = by_rel
                    self._dest_by_crc = by_crc
                    self._store_scan_snapshot()
                    self._refresh_tree()
                    n_files = sum(f.file_count for f in folders.values())
                    same = sum(
                        1
                        for f in folders.values()
                        for it in f.items
                        if it.dest_status == STATUS_SAME
                    )
                    self.log_line(
                        f"Scan done: {len(folders)} folder(s), {n_files} file(s), "
                        f"{same} already same CRC on MiSTer"
                    )
                    messagebox.showinfo(
                        "Scan complete",
                        f"Folders: {len(folders)}\nFiles: {n_files}\n"
                        f"Already same CRC: {same}\n\n"
                        "Expand folders, tick files, then 3. Run transfer.",
                    )
                elif kind == "done":
                    self._set_busy(False)
                    title, msg = payload
                    if title == "Transfer finished":
                        messagebox.showinfo(
                            title,
                            msg + "\n\nStarting a new scan…",
                        )
                        self.after(50, self.on_scan)
                    else:
                        messagebox.showinfo(title, msg)
                elif kind == "error":
                    self._set_busy(False)
                    messagebox.showerror("Error", payload)
                elif kind == "stopped":
                    self._set_busy(False)
                    self.set_progress("Stopped", self.progress["value"] / 100.0, "")
                    messagebox.showinfo("Stopped", payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _emit(self, kind: str, payload) -> None:
        self._queue.put((kind, payload))

    def on_defaults(self) -> None:
        if self._busy:
            return
        if not messagebox.askyesno(
            "Default settings",
            "Reset options and identification methods to defaults?\n"
            "Paths (Source / Output / DATs) are kept.\n\n"
            f"This updates {CONFIG_PATH.name}.",
            parent=self,
        ):
            return
        # Keep current paths
        src = self.var_source.get().strip()
        dst = self.var_dest.get().strip()
        dat = self.var_dat.get().strip() or str(DEFAULT_DATS)
        self.settings = Settings(
            source_path=src or Settings().source_path,
            destination_path=dst or Settings().destination_path,
            dat_path=dat,
        )
        save_settings(self.settings)
        self._load_into_form()
        self.log_line("Settings reset to defaults (paths kept)")

    def on_pause(self) -> None:
        if not self._busy:
            return
        if self._paused:
            self._paused = False
            self._pause.clear()
            self.btn_pause.configure(text="Pause")
            self._emit("log", "Resumed")
            cur = float(self.progress_op["value"]) / 100.0 if not self._progress_indeterminate else 0.0
            ov = float(self.progress_all["value"]) / 100.0
            self._emit_progress("Processing…", cur, "resumed", overall=ov)
        else:
            self._paused = True
            self._pause.set()
            self.btn_pause.configure(text="Resume")
            self._emit("log", "Paused")
            cur = float(self.progress_op["value"]) / 100.0 if not self._progress_indeterminate else 0.0
            ov = float(self.progress_all["value"]) / 100.0
            self._emit_progress("Paused", cur, "waiting…", overall=ov)

    def on_stop(self) -> None:
        if not self._busy:
            return
        self._stop.set()
        self._pause.clear()
        self._paused = False
        self.btn_pause.configure(text="Pause", state=tk.DISABLED)
        self.btn_stop.configure(state=tk.DISABLED)
        self._emit("log", "Stop requested…")

    def on_dat_manager(self) -> None:
        if self._busy:
            return
        self.settings = self._form_to_settings()
        dat = Path(self.settings.dat_path.strip() or DEFAULT_DATS)

        def on_saved(path: Path, _prefs) -> None:
            self.var_dat.set(str(path))
            self.settings = self._form_to_settings()
            save_settings(self.settings)
            self.log_line(f"DAT prefs saved: {path}")

        DatManagerDialog(
            self,
            dat,
            recursive=bool(self.settings.dat_recursive),
            on_saved=on_saved,
        )

    def on_about(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title(f"About {APP_NAME}")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.grab_set()

        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(anchor=tk.W)
        ttk.Label(frame, text=f"Version {APP_VERSION}").pack(anchor=tk.W, pady=(2, 10))
        ttk.Label(
            frame,
            text=(
                "Identify ROMs and sync them into MiSTer games/.\n"
                "DAT catalogues are downloaded on demand; they are not\n"
                "bundled inside the executable."
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        credits = (
            "Acknowledgments\n"
            "- MiSTer Organize - path DAT packs for MiSTer layouts\n"
            "- No-Intro - console ROM DAT standards\n"
            "- Redump - optical disc dump DAT standards\n"
            "- libretro-database - public No-Intro / Redump mirror\n"
            "- MiSTer FPGA community; icon: MiSTer-kun / MkDocs_MiSTer\n"
            "\n"
            "License: MIT"
        )

        ttk.Label(frame, text=credits, justify=tk.LEFT).pack(anchor=tk.W, pady=(12, 10))

        donate_row = ttk.Frame(frame)
        donate_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(donate_row, text="Donate:").pack(side=tk.LEFT)
        donate_link = ttk.Label(
            donate_row, text=DONATE_URL, foreground="#0b57d0", cursor="hand2"
        )
        donate_link.pack(side=tk.LEFT, padx=(6, 0))
        donate_link.bind("<Button-1>", lambda _e: webbrowser.open(DONATE_URL))

        qr_path = self._asset_path("donate_qr.png")
        if qr_path is not None:
            try:
                qr_img = tk.PhotoImage(file=str(qr_path))
                # Keep reference so Tk does not GC the image
                dlg._donate_qr_img = qr_img  # type: ignore[attr-defined]
                qr_box = ttk.Frame(frame)
                qr_box.pack(anchor=tk.W, pady=(10, 4))
                ttk.Label(qr_box, text="Donate QR:").pack(anchor=tk.W)
                ttk.Label(qr_box, image=qr_img).pack(anchor=tk.W, pady=(4, 0))
            except tk.TclError:
                pass

        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(
            btns,
            text="Latest Releases",
            command=lambda: webbrowser.open(GITHUB_RELEASES_URL),
        ).pack(side=tk.LEFT)
        ttk.Button(
            btns, text="Donate", command=lambda: webbrowser.open(DONATE_URL)
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btns, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)

        dlg.update_idletasks()
        x = self.winfo_rootx() + max(40, (self.winfo_width() - dlg.winfo_width()) // 2)
        y = self.winfo_rooty() + max(40, (self.winfo_height() - dlg.winfo_height()) // 3)
        dlg.geometry(f"+{x}+{y}")

    def on_download(self) -> None:
        if self._busy:
            return
        self.settings = self._form_to_settings()
        save_settings(self.settings)
        self._reset_control()
        self._set_busy(True)
        self.log_line("Updating DATs: MiSTer Organize + No-Intro/Redump…")
        threading.Thread(target=self._worker_download, daemon=True).start()

    def _clone_folders(
        self, folders: Dict[str, FolderPlan]
    ) -> Dict[str, FolderPlan]:
        out: Dict[str, FolderPlan] = {}
        for name, plan in folders.items():
            out[name] = FolderPlan(
                name=plan.name,
                unpack=plan.unpack,
                items=[replace(it) for it in plan.items],
            )
        return out

    def _store_scan_snapshot(self) -> None:
        """Remember post-scan state for Revert last scan."""
        self._scan_snapshot = {
            "folders": self._clone_folders(self._folders),
            "by_rel": dict(self._dest_by_rel),
            "by_crc": {k: list(v) for k, v in self._dest_by_crc.items()},
        }
        self.btn_revert_scan.configure(state=tk.NORMAL)

    def on_revert_last_scan(self) -> None:
        """Restore Transfer/Unpack/Platform/etc. to the last completed scan."""
        if self._busy:
            return
        snap = self._scan_snapshot
        if not snap:
            messagebox.showinfo(
                "Revert last scan",
                "No completed scan to revert to.",
                parent=self,
            )
            return
        self._close_platform_editor()
        self._folders = self._clone_folders(snap["folders"])
        self._dest_by_rel = dict(snap["by_rel"])
        self._dest_by_crc = {k: list(v) for k, v in snap["by_crc"].items()}
        self._refresh_tree()
        self.log_line("Reverted scan results to last scan state")

    def _clear_scan_results(self) -> None:
        """Empty scan results UI (called when a new scan starts)."""
        self._close_platform_editor()
        self._folders.clear()
        self._node_meta.clear()
        self._folder_iids.clear()
        self._file_iids.clear()
        self._dest_by_rel.clear()
        self._dest_by_crc.clear()
        for iid in self.tree.get_children(""):
            self.tree.delete(iid)
        self.lbl_summary.configure(text="")
        self.btn_run.configure(state=tk.DISABLED)

    def _bind_clipboard_shortcuts(self) -> None:
        """
        Windows clipboard shortcuts that work with any keyboard layout.
        Tk's default Control-c/v/x/a bindings break on RU layout; keycodes stay stable.
        """
        # Physical keycodes (US QWERTY): A=65 C=67 V=86 X=88
        for cls in (
            "Entry",
            "TEntry",
            "Text",
            "TCombobox",
            "Combobox",
            "Spinbox",
            "TSpinbox",
            "Treeview",
        ):
            self.bind_class(cls, "<Control-KeyPress>", self._on_ctrl_keypress, add="+")
            self.bind_class(cls, "<Control-Insert>", self._on_ctrl_copy, add="+")
            self.bind_class(cls, "<Shift-Insert>", self._on_ctrl_paste, add="+")
            self.bind_class(cls, "<Shift-Delete>", self._on_ctrl_cut, add="+")

    def _on_ctrl_keypress(self, event: tk.Event) -> Optional[str]:
        kc = int(getattr(event, "keycode", 0) or 0)
        if kc == 67:  # C
            return self._on_ctrl_copy(event)
        if kc == 86:  # V
            return self._on_ctrl_paste(event)
        if kc == 88:  # X
            return self._on_ctrl_cut(event)
        if kc == 65:  # A
            return self._on_ctrl_select_all(event)
        return None

    def _on_ctrl_copy(self, event: tk.Event) -> Optional[str]:
        w = event.widget
        text = self._copy_text_from_widget(w)
        if text is None:
            return None
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
        except tk.TclError:
            return "break"
        return "break"

    def _on_ctrl_paste(self, event: tk.Event) -> Optional[str]:
        w = event.widget
        cls = w.winfo_class()
        if cls == "Treeview":
            return "break"
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return "break"
        try:
            if cls == "Text":
                try:
                    w.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass
                w.insert("insert", text)
            elif cls in {"Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Combobox"}:
                try:
                    if w.selection_present():
                        w.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass
                w.insert("insert", text)
            else:
                return None
        except tk.TclError:
            return "break"
        return "break"

    def _on_ctrl_cut(self, event: tk.Event) -> Optional[str]:
        w = event.widget
        cls = w.winfo_class()
        if cls == "Treeview":
            return "break"
        try:
            if cls == "Text":
                try:
                    text = w.get("sel.first", "sel.last")
                except tk.TclError:
                    return "break"
                self.clipboard_clear()
                self.clipboard_append(text)
                w.delete("sel.first", "sel.last")
            elif cls in {"Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Combobox"}:
                if not w.selection_present():
                    return "break"
                text = w.selection_get()
                self.clipboard_clear()
                self.clipboard_append(text)
                w.delete("sel.first", "sel.last")
            else:
                return None
        except tk.TclError:
            return "break"
        self.update_idletasks()
        return "break"

    def _on_ctrl_select_all(self, event: tk.Event) -> Optional[str]:
        w = event.widget
        cls = w.winfo_class()
        try:
            if cls == "Text":
                w.tag_add("sel", "1.0", "end-1c")
                w.mark_set("insert", "1.0")
                w.see("insert")
            elif cls in {"Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Combobox"}:
                w.select_range(0, tk.END)
                w.icursor(tk.END)
            elif cls == "Treeview":
                children = w.get_children("")
                if children:
                    w.selection_set(children)
            else:
                return None
        except tk.TclError:
            return "break"
        return "break"

    def _copy_text_from_widget(self, w: tk.Misc) -> Optional[str]:
        cls = w.winfo_class()
        try:
            if cls in {"Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Combobox"}:
                try:
                    if w.selection_present():
                        return w.selection_get()
                except tk.TclError:
                    pass
                return str(w.get())
            if cls == "Text":
                try:
                    return w.get("sel.first", "sel.last")
                except tk.TclError:
                    return ""
            if cls == "Treeview":
                return self._tree_selection_copy_text(w)
        except tk.TclError:
            return ""
        return None

    def _tree_selection_copy_text(self, tree: ttk.Treeview) -> str:
        cols = list(tree.cget("columns"))
        lines: List[str] = []
        for iid in tree.selection():
            label = tree.item(iid, "text")
            vals = tree.item(iid, "values")
            cells = [str(label)]
            for i, _c in enumerate(cols):
                cells.append(str(vals[i]) if i < len(vals) else "")
            lines.append("\t".join(cells))
        return "\n".join(lines)

    def on_scan(self) -> None:
        if self._busy:
            return
        self.settings = self._form_to_settings()
        save_settings(self.settings)
        src = Path(self.settings.source_path)
        if not src.exists():
            messagebox.showerror("Error", f"Source folder not found:\n{src}")
            return
        if "dat" in self.settings.methods and not Path(self.settings.dat_path).exists():
            if not messagebox.askyesno(
                "DATs Folder missing",
                "DATs Folder not found. Continue without DAT matching?",
            ):
                return
        self._reset_control()
        self._clear_scan_results()
        self._set_busy(True)
        self.log_line("Scanning source + MiSTer (parallel)…")
        threading.Thread(target=self._worker_scan, daemon=True).start()

    def on_run(self) -> None:
        if self._busy:
            return
        selected: List[PlannedItem] = []
        for f in self._folders.values():
            for it in f.items:
                if it.include:
                    selected.append(it)
        if not selected:
            messagebox.showwarning("Nothing selected", "Tick at least one file to transfer.")
            return
        self.settings = self._form_to_settings()
        if not self.settings.dry_run and not self._confirm_live_transfer():
            return
        save_settings(self.settings)
        self._reset_control()
        self._set_busy(True)
        # deep-ish snapshot
        snapshot = [
            PlannedItem(
                source=it.source,
                folder=it.folder,
                rel_packed=it.rel_packed,
                rel_unpacked=it.rel_unpacked,
                method=it.method,
                reason=it.reason,
                zip_member=it.zip_member,
                packed_bytes=it.packed_bytes,
                unpacked_bytes=it.unpacked_bytes,
                is_archive=it.is_archive,
                include=True,
                unpack=it.unpack,
                crc_packed=it.crc_packed,
                crc_unpacked=it.crc_unpacked,
                dest_status=it.dest_status,
                dest_rel=it.dest_rel,
                dest_bytes=it.dest_bytes,
                dest_crc=it.dest_crc,
                dat_name=it.dat_name,
                bios_like=it.bios_like,
                notes=it.notes,
            )
            for it in selected
        ]
        self.log_line(f"Transfer started: {len(snapshot)} item(s)")
        threading.Thread(target=self._worker_transfer, args=(snapshot,), daemon=True).start()

    def _worker_download(self) -> None:
        try:
            dest = Path(self.settings.dat_path) if self.settings.dat_path.strip() else DEFAULT_DATS
            old_hint = "MiSTer Organize\\DatRoot"
            if old_hint.casefold() in str(dest).replace("/", "\\").casefold():
                dest = DEFAULT_DATS
            dest.mkdir(parents=True, exist_ok=True)

            def prog(stage: str, frac: float, detail: str) -> None:
                self._check_control()
                label = "Listing GitHub…" if stage == "list" else "Downloading / updating…"
                self._emit_progress(label, frac, detail, overall=frac)
                if detail:
                    self._emit("log", detail)

            downloaded, skipped, removed, path, notes = download_all_dats(dest, prog)
            self._check_control()
            # Refresh enable/priority list; new flat DATs append after existing order
            prefs = merge_prefs_with_folder(
                path, recursive=bool(self.settings.dat_recursive)
            )
            save_dat_prefs(path, prefs)
            self.settings.dat_path = str(path)
            save_settings(self.settings)
            self._emit_progress("DAT update done", 1.0, str(path), overall=1.0)
            for n in notes:
                self._emit("log", f"  ! {n}")
            from dat_prefs import list_dat_files

            files = list_dat_files(path, recursive=bool(self.settings.dat_recursive))
            msg = (
                f"Updated: {downloaded}\n"
                f"Already up-to-date: {skipped}\n"
                f"Old removed: {removed}\n"
                f"DAT files in folder: {len(files)}\n\n"
                f"{path}"
            )
            self._emit("log", f"DAT update finished: +{downloaded} / skip {skipped}")
            self._emit("done", ("DAT update", msg))
        except InterruptedError:
            self._emit("log", "DAT update stopped")
            self._emit("stopped", "DAT update was stopped.")
        except Exception as exc:
            self._emit("log", f"ERROR: {exc}")
            self._emit("error", str(exc))

    def _worker_scan(self) -> None:
        try:
            settings = self.settings
            src = Path(settings.source_path)
            dst = Path(settings.destination_path)
            dat = Path(settings.dat_path)
            methods = list(settings.methods)

            index: Optional[DatIndex] = None
            if "dat" in methods:

                def dat_prog(stage: str, frac: float, detail: str) -> None:
                    self._check_control()
                    self._emit_progress(
                        "Loading DAT",
                        frac,
                        detail or "reading DAT…",
                        overall=scan_overall("dat", frac),
                    )

                try:
                    prefs = merge_prefs_with_folder(
                        dat, recursive=bool(settings.dat_recursive)
                    )
                    save_dat_prefs(dat, prefs)  # persist discovered files
                    index = load_dat_folder(dat, dat_prog, prefs=prefs)
                    enabled = len(prefs.enabled_rels())
                    self._emit(
                        "log",
                        f"DAT loaded: {index.rom_count} entries "
                        f"({index.path_rom_count} path / {index.flat_rom_count} flat) "
                        f"from {enabled}/{len(prefs.entries)} enabled DAT(s)"
                        f"{' (subfolders)' if settings.dat_recursive else ''}",
                    )
                except InterruptedError:
                    raise
                except Exception as exc:
                    self._emit("log", f"DAT load failed: {exc}")
                    if methods == ["dat"]:
                        raise
                    methods = [m for m in methods if m != "dat"]

            self._check_control()
            crc_cache = get_crc_cache()
            crc_cache.load()
            self._emit(
                "log",
                f"CRC cache: {crc_cache.entry_count} entr(y/ies) "
                f"({_ROOT / 'crc_cache.json'})",
            )

            unc_src = is_unc_path(src)
            list_tick = [0.0]

            def on_list_src(n: int, p: Path) -> None:
                # Unknown total → asymptotic bar so it moves with the file count
                soft = min(0.97, 1.0 - 1.0 / (1.0 + n / 250.0))
                now = time.monotonic()
                if now - list_tick[0] < 0.15 and n % 100 != 0:
                    return
                list_tick[0] = now
                try:
                    rel = p.relative_to(src).as_posix()
                except ValueError:
                    rel = p.name
                self._emit_progress(
                    "Listing source",
                    soft,
                    f"found {n} · {rel}",
                    overall=scan_overall("list_src", soft),
                )

            self._emit_progress(
                "Listing source",
                0.0,
                str(src),
                overall=scan_overall("list_src", 0.0),
            )
            sized = cast(
                List[Tuple[Path, int]],
                self._list_files(
                    src,
                    recursive=bool(settings.source_recursive),
                    on_progress=on_list_src,
                    with_sizes=True,
                ),
            )
            files = [p for p, _ in sized]
            self._emit_progress(
                "Listing source",
                1.0,
                f"{len(files)} files",
                overall=scan_overall("list_src", 1.0),
            )
            self._emit(
                "log",
                f"Source files: {len(files)}"
                f"{'' if settings.source_recursive else ' (top level only)'}",
            )

            # Parallel identify: full worker pool; large files serialized (1 CRC at a time)
            scan_workers = worker_count(src, "scan")
            folders: Dict[str, FolderPlan] = {}
            skipped = 0
            done = 0
            done_bytes = 0
            lock = threading.Lock()
            in_flight: Dict[str, float] = {}  # name -> start monotonic
            large_gate = threading.Semaphore(1)

            # Small files first so the pool stays busy; large ISO/CHD one-by-one via gate
            sized.sort(key=lambda t: (t[1] >= LARGE_FILE_BYTES, t[1]))
            total = max(len(sized), 1)
            total_bytes = max(sum(sz for _, sz in sized), 1)
            n_large = sum(1 for _, sz in sized if sz >= LARGE_FILE_BYTES)
            self._emit(
                "log",
                f"Identify workers: {scan_workers}"
                f"{' (UNC/SMB)' if unc_src else ''}"
                + (
                    f"; large files (≥{LARGE_FILE_BYTES // (1024 * 1024)} MB): "
                    f"{n_large} — serial CRC"
                    if n_large
                    else ""
                ),
            )

            def identify_one(path: Path, size: int) -> List[PlannedItem]:
                self._check_control()
                name = path.name
                t0 = time.monotonic()
                is_large = size >= LARGE_FILE_BYTES
                if is_large:
                    large_gate.acquire()
                with lock:
                    in_flight[name] = t0

                def hash_prog(stage: str, frac: float, detail: str) -> None:
                    self._check_control()
                    mb = max(size, 1) / (1024 * 1024)
                    done_mb = frac * mb
                    # Current bar = files progress (done + this file's CRC fraction)
                    files_frac = min(1.0, (done + max(0.0, frac)) / total)
                    if stage == "hash_stall":
                        msg = (
                            f"{done}/{total} · {name} SMB stalled "
                            f"at {done_mb:.0f}/{mb:.0f} MB"
                        )
                    else:
                        tag = "CRC serial" if is_large else "CRC"
                        msg = (
                            f"{done}/{total} · {name} · {tag} "
                            f"{done_mb:.0f}/{mb:.0f} MB"
                        )
                    overall = scan_overall(
                        "identify",
                        min(
                            1.0,
                            (done_bytes + size * max(0.0, frac)) / total_bytes,
                        ),
                    )
                    self._emit_progress(
                        "Identifying",
                        files_frac,
                        msg,
                        overall=overall,
                    )

                try:
                    return plan_source(
                        path,
                        methods,
                        index,
                        settings.unknown_mode,
                        skip_bios=settings.skip_bios,
                        primary_only=settings.primary_only,
                        progress=hash_prog,
                    )
                finally:
                    elapsed = time.monotonic() - t0
                    with lock:
                        in_flight.pop(name, None)
                    if is_large:
                        large_gate.release()
                    if elapsed >= 20.0:
                        self._emit(
                            "log",
                            f"Slow identify ({elapsed:.0f}s, {size // (1024 * 1024)} MB): {name}",
                        )

            def _identify_detail() -> str:
                with lock:
                    live = list(in_flight.keys())[:2]
                live_s = f" · {', '.join(live)}" if live else ""
                return f"{done}/{total} files · {done_bytes // (1024 * 1024)} MB done{live_s}"

            # Sliding window; at most one large file in flight so workers aren't stuck on the gate
            window = max(scan_workers * 2, scan_workers + 1)
            pending = list(sized)
            with ThreadPoolExecutor(max_workers=scan_workers) as pool:
                futs: Dict[Any, Tuple[Path, int]] = {}

                def _submit_more() -> None:
                    while pending and len(futs) < window:
                        large_inflight = any(
                            sz >= LARGE_FILE_BYTES for _, sz in futs.values()
                        )
                        pick_i = None
                        for i, (_p, sz) in enumerate(pending):
                            if sz >= LARGE_FILE_BYTES and large_inflight:
                                continue
                            pick_i = i
                            break
                        if pick_i is None:
                            break
                        path, size = pending.pop(pick_i)
                        futs[pool.submit(identify_one, path, size)] = (path, size)

                _submit_more()
                while futs:
                    self._check_control()
                    finished, _ = wait(
                        tuple(futs.keys()),
                        timeout=0.6,
                        return_when=FIRST_COMPLETED,
                    )
                    if not finished:
                        files_frac = done / total
                        self._emit_progress(
                            "Identifying",
                            files_frac,
                            _identify_detail() + " (waiting on SMB/IO…)",
                            overall=scan_overall(
                                "identify", done_bytes / total_bytes
                            ),
                        )
                        continue
                    for fut in finished:
                        path, size = futs.pop(fut)
                        done += 1
                        done_bytes += size
                        every = 1 if unc_src else 5
                        if done % every == 0 or done == total or not futs:
                            files_frac = done / total
                            self._emit_progress(
                                "Identifying",
                                files_frac,
                                _identify_detail(),
                                overall=scan_overall(
                                    "identify", done_bytes / total_bytes
                                ),
                            )
                        try:
                            planned = fut.result()
                        except InterruptedError:
                            raise
                        except Exception as exc:
                            self._emit("log", f"identify fail: {path.name}: {exc}")
                            skipped += 1
                            continue
                        if not planned:
                            skipped += 1
                            continue
                        with lock:
                            for item in planned:
                                plan = folders.get(item.folder)
                                if plan is None:
                                    plan = FolderPlan(name=item.folder)
                                    folders[item.folder] = plan
                                plan.items.append(item)
                    _submit_more()

            # CD cue-sets: align orphan .cue/.bin paths to best DAT game folder
            all_items = [it for f in folders.values() for it in f.items]
            before_paths = {(id(it), it.rel_unpacked) for it in all_items}
            coalesce_cd_set_paths(all_items)
            aligned = sum(
                1 for it in all_items if (id(it), it.rel_unpacked) not in before_paths
            )
            if aligned:
                self._emit("log", f"CD set path align: {aligned} file(s)")
                folders = {}
                for item in all_items:
                    plan = folders.get(item.folder)
                    if plan is None:
                        plan = FolderPlan(name=item.folder)
                        folders[item.folder] = plan
                    plan.items.append(item)

            # CRC: one zip open / optional packed hash per archive (not per member)
            all_items = [it for f in folders.values() for it in f.items]
            by_zip: Dict[str, List[PlannedItem]] = {}
            loose_items: List[PlannedItem] = []
            for it in all_items:
                if it.is_archive:
                    by_zip.setdefault(str(it.source), []).append(it)
                else:
                    loose_items.append(it)
            crc_jobs: List[List[PlannedItem]] = list(by_zip.values()) + [
                [it] for it in loose_items
            ]
            self._emit(
                "log",
                f"Source CRC: {len(all_items)} file(s) in {len(crc_jobs)} job(s) "
                f"({len(by_zip)} archive(s), ZipInfo / packed only if Unpack off)",
            )
            done = 0
            total = max(len(crc_jobs), 1)

            def crc_job(group: List[PlannedItem]) -> None:
                self._check_control()
                it0 = group[0]
                sz = int(it0.packed_bytes or it0.unpacked_bytes or 0)
                gate = sz >= LARGE_FILE_BYTES
                if gate:
                    large_gate.acquire()
                try:
                    if it0.is_archive:
                        _fill_zip_group_crcs(group)
                    else:
                        _fill_loose_item_crc(it0)
                finally:
                    if gate:
                        large_gate.release()

            with ThreadPoolExecutor(max_workers=scan_workers) as pool:
                futs = [pool.submit(crc_job, g) for g in crc_jobs]
                for fut in as_completed(futs):
                    self._check_control()
                    done += 1
                    if done % 5 == 0 or done == total:
                        frac = done / total
                        self._emit_progress(
                            "Source CRC",
                            frac,
                            f"{done}/{total} jobs",
                            overall=scan_overall("src_crc", frac),
                        )
                    fut.result()

            # Destination: list ALL of Output (cheap size/name/path), CRC only hits
            by_rel: Dict[str, DestEntry] = {}
            by_crc: Dict[str, List[DestEntry]] = {}
            if dst.exists():
                needed_sizes: set = set()
                needed_rels: set = set()
                needed_names: set = set()
                for it in all_items:
                    needed_sizes.add(int(it.packed_bytes or 0))
                    needed_sizes.add(int(it.unpacked_bytes or 0))
                    needed_sizes.discard(0)
                    for rel in (it.rel_packed, it.rel_unpacked):
                        r = rel.replace("\\", "/").strip("/")
                        if not r:
                            continue
                        needed_rels.add(r.casefold())
                        needed_names.add(Path(r).name.casefold())
                    needed_names.add(it.source.name.casefold())
                    if it.zip_member:
                        needed_names.add(Path(it.zip_member).name.casefold())

                def _cheap_dest_hit(rel: str, size: int) -> bool:
                    """Size / basename / exact rel — no file read."""
                    if size and size in needed_sizes:
                        return True
                    rf = rel.casefold()
                    if rf in needed_rels:
                        return True
                    return Path(rel).name.casefold() in needed_names

                known_src_crc = build_known_source_crcs(all_items)
                same_tree = paths_same_tree(src, dst)
                dest_recursive = bool(settings.dest_recursive)
                # Source is the whole Output tree (or identical recurse) → reuse list
                reuse_listing = same_tree and (
                    bool(settings.source_recursive) == dest_recursive
                )

                self._emit_progress(
                    "Listing MiSTer",
                    0.0,
                    str(dst),
                    overall=scan_overall("dest", 0.0),
                )
                dest_candidates: List[Tuple[str, Path, int]] = []
                listed_n = 0

                if reuse_listing:
                    self._emit(
                        "log",
                        "Source = Output: reusing source file list for MiSTer match",
                    )
                    size_by_src = {
                        _norm_path_key(it.source): int(it.packed_bytes or 0)
                        for it in all_items
                        if it.packed_bytes
                    }
                    for p in files:
                        self._check_control()
                        listed_n += 1
                        try:
                            rel = p.relative_to(dst).as_posix()
                            size = size_by_src.get(_norm_path_key(p))
                            if size is None:
                                size = p.stat().st_size
                        except (OSError, ValueError):
                            continue
                        if _cheap_dest_hit(rel, size):
                            dest_candidates.append((rel, p, size))
                        if listed_n % 100 == 0:
                            self._emit_progress(
                                "Listing MiSTer",
                                listed_n / max(len(files), 1),
                                f"listed {listed_n}/{len(files)} · "
                                f"cheap hits {len(dest_candidates)}",
                                overall=scan_overall(
                                    "dest",
                                    0.15 * listed_n / max(len(files), 1),
                                ),
                            )
                else:
                    # Full Output tree (all cores), not only planned folders
                    self._emit(
                        "log",
                        "MiSTer match: list all Output, CRC only cheap hits "
                        "(size / name / planned path)",
                    )

                    def on_list_dst(n: int, p: Path) -> None:
                        try:
                            rel = p.relative_to(dst).as_posix()
                        except ValueError:
                            rel = p.name
                        soft = min(0.97, 1.0 - 1.0 / (1.0 + n / 250.0))
                        self._emit_progress(
                            "Listing MiSTer",
                            soft,
                            f"listed {n} · {rel}",
                            overall=scan_overall("dest", min(0.2, soft)),
                        )

                    listed = cast(
                        List[Tuple[Path, int]],
                        self._list_files(
                            dst,
                            recursive=dest_recursive,
                            on_progress=on_list_dst,
                            with_sizes=True,
                        ),
                    )
                    listed_n = len(listed)
                    for i, (p, size) in enumerate(listed, 1):
                        self._check_control()
                        try:
                            rel = p.relative_to(dst).as_posix()
                        except ValueError:
                            continue
                        if _cheap_dest_hit(rel, size):
                            dest_candidates.append((rel, p, size))
                        if i % 200 == 0 or i == listed_n:
                            self._emit_progress(
                                "Listing MiSTer",
                                i / max(listed_n, 1),
                                f"cheap filter {i}/{listed_n} · "
                                f"hits {len(dest_candidates)}",
                                overall=scan_overall(
                                    "dest", 0.2 + 0.05 * i / max(listed_n, 1)
                                ),
                            )

                reused = 0
                to_hash: List[Tuple[str, Path, int]] = []
                for rel, path, size in dest_candidates:
                    hit = known_src_crc.get(_norm_path_key(path))
                    if hit and hit[0]:
                        entry = DestEntry(
                            rel=rel, path=path, size=hit[1] or size, crc=hit[0]
                        )
                        by_rel[entry.rel.casefold()] = entry
                        by_crc.setdefault(entry.crc, []).append(entry)
                        reused += 1
                    else:
                        to_hash.append((rel, path, size))

                self._emit(
                    "log",
                    f"MiSTer: listed {listed_n} file(s), cheap hits "
                    f"{len(dest_candidates)} → CRC {len(to_hash)} "
                    f"(reuse {reused} source CRC"
                    f"{'' if dest_recursive else ', top level only'}"
                    f"{', same tree' if same_tree else ''})",
                )
                dest_workers = worker_count(dst, "scan")
                done = 0
                total = max(len(to_hash), 1)

                def hash_dest(rel: str, path: Path, size: int) -> Optional[DestEntry]:
                    self._check_control()
                    gate = size >= LARGE_FILE_BYTES
                    if gate:
                        large_gate.acquire()
                    try:
                        crc, got = file_crc32(path)
                        return DestEntry(rel=rel, path=path, size=got or size, crc=crc)
                    except OSError:
                        return None
                    finally:
                        if gate:
                            large_gate.release()

                if to_hash:
                    with ThreadPoolExecutor(max_workers=dest_workers) as pool:
                        futs = [
                            pool.submit(hash_dest, rel, path, size)
                            for rel, path, size in to_hash
                        ]
                        for fut in as_completed(futs):
                            self._check_control()
                            done += 1
                            if done % 5 == 0 or done == total:
                                frac = done / total
                                self._emit_progress(
                                    "MiSTer CRC",
                                    frac,
                                    f"{done}/{total} candidates",
                                    overall=scan_overall("dest", 0.25 + 0.75 * frac),
                                )
                            entry = fut.result()
                            if entry is None:
                                continue
                            by_rel[entry.rel.casefold()] = entry
                            by_crc.setdefault(entry.crc, []).append(entry)
                else:
                    self._emit_progress(
                        "MiSTer CRC",
                        1.0,
                        "reused source CRCs",
                        overall=scan_overall("dest", 1.0),
                    )
            else:
                self._emit("log", f"MiSTer path missing (OK for first fill): {dst}")

            apply_dest_matches(folders, by_rel, by_crc)
            if settings.skip_bios:
                bios_off = sum(
                    1
                    for f in folders.values()
                    for it in f.items
                    if it.bios_like and not it.include
                )
                if bios_off:
                    self._emit(
                        "log",
                        f"BIOS/boot ROMs: Transfer off for {bios_off} item(s)",
                    )
            get_crc_cache().save(force=True)
            self._emit_progress(
                "Scan done",
                1.0,
                f"{len(folders)} folders",
                overall=1.0,
            )
            self._emit("log", f"Skipped (not planned): {skipped}")
            self._emit("scan_done", (folders, by_rel, by_crc))
        except InterruptedError:
            get_crc_cache().save(force=True)
            self._emit("log", "Scan stopped")
            self._emit("stopped", "Scan was stopped.")
        except Exception as exc:
            get_crc_cache().save(force=True)
            self._emit("log", traceback.format_exc())
            self._emit("error", str(exc))

    def _worker_transfer(self, items: List[PlannedItem]) -> None:
        try:
            settings = self.settings
            dst = Path(settings.destination_path)
            log_path = (
                _ROOT / "logs" / f"mister_rom_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = log_path.open("w", encoding="utf-8")
            log_lock = threading.Lock()

            stats = {
                "ok": 0,
                "skip": 0,
                "failed": 0,
                "extract": 0,
                "copy": 0,
                "relocate": 0,
            }
            stats_lock = threading.Lock()
            claimed_relocate: set = set()
            claimed_lock = threading.Lock()
            units = group_transfer_units(items)
            total = max(len(items), 1)
            done_items = 0
            stopped_early = False
            copy_workers = worker_count(dst, "copy")
            self._emit(
                "log",
                f"Transfer workers: {copy_workers}; units: {len(units)} "
                f"(CD atomic, zip reuse, relocate same-CRC on MiSTer)",
            )

            def one_unit(unit: List[PlannedItem]) -> None:
                self._check_control()
                try:
                    pairs = transfer_unit(
                        unit,
                        dst,
                        dry_run=settings.dry_run,
                        check=self._check_control,
                        claimed_relocate=claimed_relocate,
                        claimed_lock=claimed_lock,
                        relocate_on_mister=bool(settings.relocate_on_mister),
                    )
                except InterruptedError:
                    raise
                except Exception as exc:
                    pairs = [(it, f"FAIL:{exc}") for it in unit]

                with log_lock, stats_lock:
                    for item, tag in pairs:
                        rel = item.active_rel()
                        if tag.startswith("FAIL"):
                            log_fh.write(f"FAIL\t{item.source}\t→\t{rel}\t{tag}\n")
                            stats["failed"] += 1
                        elif tag.startswith("SKIP"):
                            log_fh.write(f"{tag}\t{item.source}\t→\t{rel}\n")
                            stats["skip"] += 1
                        else:
                            from_rel = (
                                f"\tfrom\t{item.dest_rel}"
                                if tag.startswith("RELOCATE") and item.dest_rel
                                else ""
                            )
                            log_fh.write(
                                f"{tag}\t{item.method}\t{item.source}\t→\t{rel}"
                                f"{from_rel}\t"
                                f"unpack={item.unpack}\tmember={item.zip_member or ''}\t"
                                f"crc={item.active_crc()}\t{item.reason}\n"
                            )
                            stats["ok"] += 1
                            if tag.startswith("RELOCATE"):
                                stats["relocate"] += 1
                            elif "EXTRACT" in tag:
                                stats["extract"] += 1
                            else:
                                stats["copy"] += 1

            try:
                with ThreadPoolExecutor(max_workers=copy_workers) as pool:
                    futs = {pool.submit(one_unit, u): u for u in units}
                    for fut in as_completed(futs):
                        self._check_control()
                        unit = futs[fut]
                        done_items += len(unit)
                        if done_items % 5 == 0 or done_items >= total:
                            frac = min(done_items, total) / total
                            self._emit_progress(
                                "Transferring…",
                                frac,
                                f"{min(done_items, total)}/{total}",
                                overall=frac,
                            )
                        fut.result()
            except InterruptedError:
                stopped_early = True
                log_fh.write("STOPPED\tby user\n")

            pruned = 0
            if (
                settings.prune_empty_dirs
                and not settings.dry_run
                and not stopped_early
            ):
                platforms = sorted({it.folder for it in items if it.folder})
                for name in platforms:
                    pruned += prune_empty_dirs_under(dst / name)
                if pruned:
                    log_fh.write(f"PRUNE_EMPTY\t{pruned}\n")
                    self._emit("log", f"Empty platform subfolders removed: {pruned}")

            log_fh.close()
            get_crc_cache().save(force=True)
            mode = "TEST-RUN" if settings.dry_run else "COPY"
            summary = (
                f"Mode: {mode}\n"
                f"Workers: {copy_workers}\n"
                f"OK: {stats['ok']}\n"
                f"  copied as-is: {stats['copy']}\n"
                f"  extracted: {stats['extract']}\n"
                f"  relocated on MiSTer: {stats['relocate']}\n"
                f"Skipped (same CRC/exists): {stats['skip']}\n"
                f"Failed: {stats['failed']}\n"
                f"Empty folders pruned: {pruned}\n\n"
                f"Log: {log_path}"
            )
            self._emit("log", summary.replace("\n", " | "))
            if stopped_early:
                cur = float(self.progress_op["value"]) / 100.0
                self._emit_progress("Stopped", cur, "", overall=cur)
                self._emit("stopped", "Transfer stopped.\n\n" + summary)
            else:
                self._emit_progress("Done", 1.0, "", overall=1.0)
                self._emit("done", ("Transfer finished", summary))
        except InterruptedError:
            self._emit("log", "Transfer stopped")
            self._emit("stopped", "Transfer was stopped.")
        except Exception as exc:
            self._emit("log", traceback.format_exc())
            self._emit("error", str(exc))


def _windows_set_app_user_model_id(app_id: str = "Sergey.MiSTerROMSync") -> None:
    """Separate taskbar identity from python.exe so custom icons can apply."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        pass


def main() -> int:
    _windows_set_app_user_model_id()
    DEFAULT_DATS.mkdir(parents=True, exist_ok=True)
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
