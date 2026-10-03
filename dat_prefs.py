#!/usr/bin/env python3
"""Enabled DAT list + priority order (lower index = higher priority)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dat_sources_default import DEFAULT_DAT_SOURCES

PREFS_NAME = "dat_sources.json"


def kind_from_rel(rel: str) -> str:
    name = Path(rel).name.casefold()
    if name.startswith("mister"):
        return "path"
    return "flat"


def builtin_default_prefs() -> DatPrefs:
    """Hard-coded catalogue shipped with the app (before first DAT download)."""
    prefs = DatPrefs()
    for rel, enabled in DEFAULT_DAT_SOURCES:
        prefs.entries.append(
            DatSourceEntry(
                rel=str(rel).replace("\\", "/"),
                enabled=bool(enabled),
                kind=kind_from_rel(str(rel)),
                size=0,
                exists=False,
            )
        )
    return prefs


@dataclass
class DatSourceEntry:
    rel: str  # path relative to DAT folder (forward slashes)
    enabled: bool = True
    # Detected when scanning; not required in JSON
    kind: str = ""  # path | flat | unknown
    size: int = 0
    exists: bool = True


@dataclass
class DatPrefs:
    """Ordered list: index 0 is tried first when CRC collisions span DATs."""

    entries: List[DatSourceEntry] = field(default_factory=list)

    def enabled_rels(self) -> List[str]:
        return [e.rel for e in self.entries if e.enabled and e.exists]

    def priority_map(self) -> Dict[str, int]:
        """rel.casefold() → priority (0 = highest)."""
        out: Dict[str, int] = {}
        pri = 0
        for e in self.entries:
            if not e.enabled or not e.exists:
                continue
            out[e.rel.replace("\\", "/").casefold()] = pri
            pri += 1
        return out

    def to_json(self) -> dict:
        return {
            "sources": [
                {"rel": e.rel.replace("\\", "/"), "enabled": bool(e.enabled)}
                for e in self.entries
            ]
        }

    @classmethod
    def from_json(cls, raw: dict) -> "DatPrefs":
        prefs = cls()
        for item in raw.get("sources") or []:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("rel") or "").replace("\\", "/").strip()
            if not rel:
                continue
            prefs.entries.append(
                DatSourceEntry(rel=rel, enabled=bool(item.get("enabled", True)))
            )
        return prefs


def prefs_path(dat_dir: Path) -> Path:
    return dat_dir / PREFS_NAME


def load_dat_prefs(dat_dir: Path) -> DatPrefs:
    path = prefs_path(dat_dir)
    if not path.exists():
        return builtin_default_prefs()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return builtin_default_prefs()
    if not isinstance(raw, dict):
        return builtin_default_prefs()
    prefs = DatPrefs.from_json(raw)
    if not prefs.entries:
        return builtin_default_prefs()
    for e in prefs.entries:
        if not e.kind:
            e.kind = kind_from_rel(e.rel)
    return prefs


def save_dat_prefs(dat_dir: Path, prefs: DatPrefs) -> None:
    dat_dir.mkdir(parents=True, exist_ok=True)
    path = prefs_path(dat_dir)
    path.write_text(
        json.dumps(prefs.to_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _detect_kind(path: Path) -> str:
    """Cheap peek: nested games/CORE → path, else flat."""
    name = path.name.casefold()
    if name.startswith("mister"):
        return "path"
    try:
        head = path.read_bytes()[:65536].decode("utf-8", errors="ignore")
    except OSError:
        return "unknown"
    low = head.casefold()
    if "games\\" in low or "games/" in low:
        return "path"
    if low.lstrip().startswith("clrmamepro"):
        return "flat"
    if low.lstrip().startswith("<?xml") or low.lstrip().startswith("<datafile"):
        return "flat"
    return "unknown"


def list_dat_files(dat_dir: Path, *, recursive: bool = False) -> List[Path]:
    """DAT/XML files in the DAT folder (optionally including subfolders)."""
    if not dat_dir.exists():
        return []
    skip = {
        PREFS_NAME.casefold(),
        "manifest.json",
        "manifest_extra.json",
        "source.txt",
    }
    it = dat_dir.rglob("*") if recursive else dat_dir.iterdir()
    files = [
        p
        for p in it
        if p.is_file()
        and p.suffix.casefold() in {".dat", ".xml"}
        and p.name.casefold() not in skip
    ]
    return sorted(files, key=lambda p: p.as_posix().casefold())


def _default_sort_key(rel: str, kind: str) -> Tuple[int, str]:
    # Path / MiSTer Organize first, then flat, then unknown; alpha within group
    group = 0 if kind == "path" or rel.casefold().startswith("mister") else (
        1 if kind == "flat" else 2
    )
    return (group, rel.casefold())


def merge_prefs_with_folder(
    dat_dir: Path,
    prefs: Optional[DatPrefs] = None,
    *,
    recursive: bool = False,
) -> DatPrefs:
    """
    Refresh prefs against files on disk.
    Keeps user order/enabled for catalog entries even if the .dat is not
    downloaded yet (exists=False). Appends newly found files on disk.
    """
    dat_dir = dat_dir.resolve()
    old = prefs or load_dat_prefs(dat_dir)
    if not old.entries:
        old = builtin_default_prefs()
    old_by_rel = {e.rel.replace("\\", "/").casefold(): e for e in old.entries}

    discovered: Dict[str, DatSourceEntry] = {}
    for path in list_dat_files(dat_dir, recursive=recursive):
        try:
            rel = path.relative_to(dat_dir).as_posix()
        except ValueError:
            rel = path.name
        kind = _detect_kind(path)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        key = rel.casefold()
        prev = old_by_rel.get(key)
        discovered[key] = DatSourceEntry(
            rel=rel,
            enabled=prev.enabled if prev else True,
            kind=kind,
            size=size,
            exists=True,
        )

    ordered: List[DatSourceEntry] = []
    seen: set = set()
    for e in old.entries:
        key = e.rel.replace("\\", "/").casefold()
        if key in seen:
            continue
        seen.add(key)
        if key in discovered:
            ordered.append(discovered[key])
        else:
            ordered.append(
                DatSourceEntry(
                    rel=e.rel.replace("\\", "/"),
                    enabled=e.enabled,
                    kind=e.kind or kind_from_rel(e.rel),
                    size=0,
                    exists=False,
                )
            )

    newcomers = [discovered[k] for k in discovered if k not in seen]
    newcomers.sort(key=lambda e: _default_sort_key(e.rel, e.kind))
    ordered.extend(newcomers)

    return DatPrefs(entries=ordered)


def move_entry(prefs: DatPrefs, index: int, delta: int) -> DatPrefs:
    """Move entry up (delta=-1) or down (delta=+1); returns same prefs mutated."""
    j = index + delta
    if index < 0 or index >= len(prefs.entries) or j < 0 or j >= len(prefs.entries):
        return prefs
    prefs.entries[index], prefs.entries[j] = prefs.entries[j], prefs.entries[index]
    return prefs
