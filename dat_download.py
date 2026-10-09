#!/usr/bin/env python3
"""Download / update MiSTer Organize DAT files into the local dats/ folder."""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ProgressCb = Callable[[str, float, str], None]

GITHUB_API = (
    "https://api.github.com/repos/MiSTerOrganize/MiSTer_Organize/contents/DatRoot"
)
USER_AGENT = "mister-rom-sync/1.0"
MANIFEST_NAME = "manifest.json"

# MiSTer_Console (20260807).dat  →  set=MiSTer_Console, date=20260807
_DAT_NAME_RE = re.compile(
    r"^(?P<set>.+?)\s*\((?P<date>\d{8})\)\.(?P<ext>dat|xml)$",
    re.IGNORECASE,
)


def default_dats_dir(root: Path) -> Path:
    return root / "dats"


@dataclass
class DatRef:
    name: str
    url: str
    size: int
    sha: str
    set_key: str
    date: int  # YYYYMMDD or 0


def parse_dat_name(name: str) -> Tuple[str, int]:
    m = _DAT_NAME_RE.match(name.strip())
    if m:
        return m.group("set").strip(), int(m.group("date"))
    stem = Path(name).stem
    return stem, 0


def list_remote_dats() -> List[DatRef]:
    req = urllib.request.Request(
        GITHUB_API,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected GitHub API response: {type(data)}")

    files: List[DatRef] = []
    for item in data:
        name = item.get("name") or ""
        url = item.get("download_url")
        if not url:
            continue
        if not name.casefold().endswith((".dat", ".xml")):
            continue
        set_key, date = parse_dat_name(name)
        files.append(
            DatRef(
                name=name,
                url=url,
                size=int(item.get("size") or 0),
                sha=str(item.get("sha") or ""),
                set_key=set_key,
                date=date,
            )
        )
    if not files:
        raise RuntimeError("No DAT files found in MiSTerOrganize/DatRoot")
    return sorted(files, key=lambda x: x.name.casefold())


def _load_manifest(dest_dir: Path) -> Dict[str, dict]:
    path = dest_dir / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(dest_dir: Path, manifest: Dict[str, dict]) -> None:
    path = dest_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _local_set_files(dest_dir: Path, set_key: str) -> List[Path]:
    out: List[Path] = []
    for p in dest_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.casefold() not in {".dat", ".xml"}:
            continue
        local_set, _ = parse_dat_name(p.name)
        if local_set.casefold() == set_key.casefold():
            out.append(p)
    return out


def needs_update(dest_dir: Path, remote: DatRef, manifest: Dict[str, dict]) -> Tuple[bool, str]:
    """
    Return (should_download, reason).
    Freshness rules:
      1) missing local file with this exact name → download
      2) remote sha/size differs from manifest or file → update
      3) local has older dated pack for same set → download newer, drop old
    """
    dest = dest_dir / remote.name
    entry = manifest.get(remote.name) or {}

    # Newer dated pack for same set already locally under a different filename?
    locals_for_set = _local_set_files(dest_dir, remote.set_key)
    if remote.date:
        for lp in locals_for_set:
            _, local_date = parse_dat_name(lp.name)
            if local_date and local_date > remote.date:
                return False, f"local newer dated pack {lp.name}"
            if local_date and local_date < remote.date and lp.name != remote.name:
                return True, f"newer than {lp.name}"

    if not dest.exists():
        # maybe only older dated file exists
        if locals_for_set:
            return True, "new dated filename"
        return True, "missing"

    # Same filename present — compare sha / size
    local_size = dest.stat().st_size
    if remote.sha and entry.get("sha") == remote.sha and local_size == remote.size:
        return False, "up-to-date (sha)"
    if remote.sha and entry.get("sha") and entry.get("sha") != remote.sha:
        return True, "sha changed on GitHub"
    if remote.size and local_size != remote.size:
        return True, "size mismatch"
    if not entry.get("sha") and remote.size and local_size == remote.size:
        # No manifest yet but size matches — treat as OK, record later
        return False, "up-to-date (size)"
    if not entry.get("sha"):
        return True, "no manifest — refresh"
    return False, "up-to-date"


def _download_one(
    url: str,
    dest: Path,
    expected_size: int = 0,
    progress: Optional[ProgressCb] = None,
    label: str = "",
) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or expected_size or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(
                        "download",
                        done / total,
                        f"{label} {done // (1024 * 1024)}MB",
                    )
        tmp.replace(dest)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _purge_older_set_files(dest_dir: Path, remote: DatRef, manifest: Dict[str, dict]) -> List[str]:
    """Delete older dated packs of the same set after a successful download."""
    removed: List[str] = []
    if not remote.date:
        return removed
    for lp in _local_set_files(dest_dir, remote.set_key):
        if lp.name == remote.name:
            continue
        _, local_date = parse_dat_name(lp.name)
        if local_date and local_date < remote.date:
            try:
                lp.unlink()
                removed.append(lp.name)
                manifest.pop(lp.name, None)
            except OSError:
                pass
    return removed


def download_mister_organize_dats(
    dest_dir: Path,
    progress: Optional[ProgressCb] = None,
) -> Tuple[int, int, int, Path, List[str]]:
    """
    Sync local dats/ with GitHub DatRoot.
    Only downloads when remote is newer / changed / missing.

    Returns (downloaded_or_updated, skipped_up_to_date, removed_old, dest_dir, notes)
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("list", 0.0, "Query GitHub MiSTerOrganize/DatRoot…")

    files = list_remote_dats()
    if progress:
        progress("list", 1.0, f"Found {len(files)} DAT file(s)")

    manifest = _load_manifest(dest_dir)
    downloaded = 0
    skipped = 0
    removed_old = 0
    notes: List[str] = []
    total = len(files)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for i, remote in enumerate(files):
        overall_base = i / total

        def file_prog(stage: str, frac: float, detail: str, _base=overall_base) -> None:
            if progress:
                progress("download", _base + frac / total, detail)

        do_dl, reason = needs_update(dest_dir, remote, manifest)
        if not do_dl:
            skipped += 1
            # refresh manifest entry if we only matched by size
            dest = dest_dir / remote.name
            if dest.exists():
                manifest[remote.name] = {
                    "sha": remote.sha,
                    "size": remote.size,
                    "set": remote.set_key,
                    "date": remote.date,
                    "checked_at": now,
                }
            if progress:
                progress("download", (i + 1) / total, f"OK {remote.name} ({reason})")
            continue

        if progress:
            progress("download", overall_base, f"UPD {remote.name} — {reason}")
        _download_one(remote.url, dest_dir / remote.name, remote.size, file_prog, remote.name)
        downloaded += 1
        notes.append(f"{remote.name}: {reason}")

        purged = _purge_older_set_files(dest_dir, remote, manifest)
        removed_old += len(purged)
        for old in purged:
            notes.append(f"removed old {old}")

        manifest[remote.name] = {
            "sha": remote.sha,
            "size": remote.size,
            "set": remote.set_key,
            "date": remote.date,
            "checked_at": now,
            "downloaded_at": now,
        }
        if progress:
            progress("download", (i + 1) / total, f"saved {remote.name}")

    _save_manifest(dest_dir, manifest)

    _write_source_txt(dest_dir, organize_checked=now)
    return downloaded, skipped, removed_old, dest_dir, notes


def _write_source_txt(
    dest_dir: Path,
    *,
    organize_checked: str = "",
    flat_checked: str = "",
) -> None:
    """Single SOURCE.txt for everything in the DAT folder."""
    path = dest_dir / "SOURCE.txt"
    prev_org = ""
    prev_flat = ""
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("Organize last check:"):
                prev_org = s.split(":", 1)[1].strip()
            elif s.startswith("Flat last check:"):
                prev_flat = s.split(":", 1)[1].strip()
            elif s.startswith("Last check:"):
                prev_org = prev_org or s.split(":", 1)[1].strip()
    org = organize_checked or prev_org or "—"
    flat = flat_checked or prev_flat or "—"
    path.write_text(
        "MiSTer DAT folder (all .dat/.xml + manifests live here)\n"
        "\n"
        "Organize (paths):\n"
        "  https://github.com/MiSTerOrganize/MiSTer_Organize/tree/main/DatRoot\n"
        f"  Organize last check: {org}\n"
        "\n"
        "No-Intro / Redump (flat, libretro-database):\n"
        "  https://github.com/libretro/libretro-database\n"
        f"  Flat last check: {flat}\n"
        "\n"
        "Prefs: dat_sources.json | Manifests: manifest.json, manifest_extra.json\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Flat No-Intro / Redump DATs (same catalogs DATVault redistributes).
# DATVault itself is Patreon-gated; we pull the public libretro-database mirror.
# ---------------------------------------------------------------------------

LIBRETRO_API = (
    "https://api.github.com/repos/libretro/libretro-database/contents/{path}"
)
LIBRETRO_RAW = (
    "https://raw.githubusercontent.com/libretro/libretro-database/master/{path}/{name}"
)
EXTRA_MANIFEST = "manifest_extra.json"
CUSTOM_SOURCES_NAME = "custom_sources.json"

ORGANIZE_UPDATE_URL = (
    "https://github.com/MiSTerOrganize/MiSTer_Organize/tree/main/DatRoot"
)
LIBRETRO_UPDATE_URL = "https://github.com/libretro/libretro-database"


def load_custom_sources(dats_dir: Path) -> List[dict]:
    """User-added DAT update entries: [{rel, url}, ...]."""
    path = dats_dir / CUSTOM_SOURCES_NAME
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    out: List[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        rel = str(e.get("rel") or "").strip()
        url = str(e.get("url") or "").strip()
        if rel and url:
            out.append({"rel": rel, "url": url})
    return out


def save_custom_sources(dats_dir: Path, entries: List[dict]) -> None:
    dats_dir.mkdir(parents=True, exist_ok=True)
    path = dats_dir / CUSTOM_SOURCES_NAME
    clean = []
    for e in entries:
        rel = str(e.get("rel") or "").strip()
        url = str(e.get("url") or "").strip()
        if rel and url:
            clean.append({"rel": Path(rel).name, "url": url})
    path.write_text(
        json.dumps({"entries": clean}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def add_custom_source(dats_dir: Path, rel: str, url: str) -> List[dict]:
    entries = load_custom_sources(dats_dir)
    name = Path(rel).name
    entries = [e for e in entries if Path(e["rel"]).name.casefold() != name.casefold()]
    entries.append({"rel": name, "url": url.strip()})
    save_custom_sources(dats_dir, entries)
    return entries


def update_source_label(rel: str, dats_dir: Optional[Path] = None) -> str:
    """Human-readable update origin for a DAT file in the manager list."""
    name = Path(rel).name
    if dats_dir is not None:
        for e in load_custom_sources(dats_dir):
            if Path(e["rel"]).name.casefold() == name.casefold():
                return e["url"]
    if name.casefold().startswith("mister"):
        return ORGANIZE_UPDATE_URL
    return LIBRETRO_UPDATE_URL


def download_custom_sources(
    dats_dir: Path,
    progress: Optional[ProgressCb] = None,
) -> Tuple[int, int, int, List[str]]:
    """Download user-added DAT URLs. Returns (downloaded, skipped, removed, notes)."""
    entries = load_custom_sources(dats_dir)
    if not entries:
        return 0, 0, 0, []
    downloaded = 0
    skipped = 0
    notes: List[str] = []
    total = max(len(entries), 1)
    for i, e in enumerate(entries):
        name = Path(e["rel"]).name
        url = e["url"]
        dest = dats_dir / name
        if progress:
            progress("download", i / total, f"UPD custom {name}")
        try:
            _download_one(url, dest, 0, progress, name)
            downloaded += 1
            notes.append(f"custom {name}")
        except Exception as exc:
            notes.append(f"custom {name} FAILED: {exc}")
            skipped += 1
        if progress:
            progress("download", (i + 1) / total, f"custom {name}")
    return downloaded, skipped, 0, notes

# libretro-database remote paths (files land flat in dats/)
_LIBRETRO_KINDS = (
    ("no-intro", "metadat/no-intro"),
    ("redump", "metadat/redump"),
)


def _remove_legacy_extra_tree(dats_dir: Path, notes: Optional[List[str]] = None) -> None:
    """Drop old dats/extra/ layout; everything lives in dats/ now."""
    import shutil

    legacy = dats_dir / "extra"
    if not legacy.exists():
        return
    try:
        shutil.rmtree(legacy)
        if notes is not None:
            notes.append("removed legacy extra/ folder")
    except OSError:
        pass


def _list_libretro_dat_dir(api_path: str) -> List[dict]:
    url = LIBRETRO_API.format(path=api_path)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected GitHub API response for {api_path}")
    return [
        item
        for item in data
        if item.get("type") == "file"
        and str(item.get("name") or "").casefold().endswith(".dat")
    ]


def _mister_mapped_dat_name(name: str) -> Optional[str]:
    """Return MiSTer folder if this DAT platform is supported, else None."""
    from mister_platform_map import MISTER_CORE_FOLDERS, platform_to_mister_folder

    folder = platform_to_mister_folder(name)
    if not folder:
        return None
    if folder.casefold() not in {f.casefold() for f in MISTER_CORE_FOLDERS}:
        return None
    return folder


def download_libretro_platform_dats(
    dats_dir: Path,
    progress: Optional[ProgressCb] = None,
) -> Tuple[int, int, int, Path, List[str]]:
    """
    Download No-Intro/Redump DATs for MiSTer-mapped platforms into the same
    dats/ folder as Organize packs (flat filenames).
    Returns (downloaded, skipped, removed_unused, dats_dir, notes).
    """
    dats_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest_named(dats_dir, EXTRA_MANIFEST)
    downloaded = 0
    skipped = 0
    removed = 0
    notes: List[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    keep_names: set = set()

    all_jobs: List[Tuple[str, str, dict, str]] = []  # kind, api_path, item, mister_folder
    for kind, api_path in _LIBRETRO_KINDS:
        if progress:
            progress("list", 0.0, f"Query libretro {kind}…")
        items = _list_libretro_dat_dir(api_path)
        for item in items:
            name = item.get("name") or ""
            folder = _mister_mapped_dat_name(name)
            if not folder:
                continue
            all_jobs.append((kind, api_path, item, folder))

    if progress:
        progress("list", 1.0, f"Found {len(all_jobs)} MiSTer-relevant flat DAT(s)")

    total = max(len(all_jobs), 1)
    for i, (kind, api_path, item, folder) in enumerate(all_jobs):
        name = item["name"]
        sha = str(item.get("sha") or "")
        size = int(item.get("size") or 0)
        dest = dats_dir / name
        keep_names.add(name.casefold())
        key = name
        entry = manifest.get(key) or manifest.get(f"{kind}/{name}") or {}

        up_to_date = (
            dest.exists()
            and sha
            and entry.get("sha") == sha
            and dest.stat().st_size == size
        )
        if up_to_date:
            skipped += 1
            manifest[key] = {
                "sha": sha,
                "size": size,
                "mister": folder,
                "kind": kind,
                "checked_at": now,
            }
            if progress:
                progress("download", (i + 1) / total, f"OK {name}")
            continue

        from urllib.parse import quote

        url = (item.get("download_url") or "").strip()
        if not url or " " in url:
            url = LIBRETRO_RAW.format(path=api_path, name=quote(name, safe=""))

        def file_prog(stage: str, frac: float, detail: str, _i=i) -> None:
            if progress:
                progress("download", (_i + frac) / total, detail)

        if progress:
            progress("download", i / total, f"UPD {name} -> {folder}")
        _download_one(url, dest, size, file_prog, name)
        downloaded += 1
        notes.append(f"{name} -> {folder}")
        manifest[key] = {
            "sha": sha,
            "size": size,
            "mister": folder,
            "kind": kind,
            "checked_at": now,
            "downloaded_at": now,
        }
    _remove_legacy_extra_tree(dats_dir, notes)

    # Drop obsolete managed flat DATs (not MiSTer_* Organize packs)
    for key in list(manifest.keys()):
        base = Path(key).name
        if base.casefold() in keep_names:
            continue
        if base.casefold().startswith("mister"):
            continue
        p = dats_dir / base
        if p.is_file() and not base.casefold().startswith("mister"):
            # Only auto-remove if we previously tracked it as flat
            if manifest.get(key, {}).get("kind") in {"no-intro", "redump"}:
                try:
                    p.unlink()
                    removed += 1
                    notes.append(f"removed obsolete {base}")
                except OSError:
                    pass
        manifest.pop(key, None)

    _save_manifest_named(dats_dir, EXTRA_MANIFEST, manifest)
    _write_source_txt(dats_dir, flat_checked=now)
    return downloaded, skipped, removed, dats_dir, notes


def _load_manifest_named(dest_dir: Path, name: str) -> Dict[str, dict]:
    path = dest_dir / name
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest_named(dest_dir: Path, name: str, manifest: Dict[str, dict]) -> None:
    path = dest_dir / name
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def download_all_dats(
    dest_dir: Path,
    progress: Optional[ProgressCb] = None,
) -> Tuple[int, int, int, Path, List[str]]:
    """Organize path DATs + flat No-Intro/Redump + custom URLs."""
    notes_legacy: List[str] = []
    _remove_legacy_extra_tree(dest_dir, notes_legacy)
    d1, s1, r1, path, n1 = download_mister_organize_dats(dest_dir, progress)
    d2, s2, r2, _flat, n2 = download_libretro_platform_dats(dest_dir, progress)
    d3, s3, r3, n3 = download_custom_sources(dest_dir, progress)
    notes = (
        [f"[cleanup] {x}" for x in notes_legacy]
        + [f"[Organize] {x}" for x in n1]
        + [f"[flat] {x}" for x in n2]
        + [f"[custom] {x}" for x in n3]
    )
    return d1 + d2 + d3, s1 + s2 + s3, r1 + r2 + r3, path, notes
