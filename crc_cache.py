#!/usr/bin/env python3
"""Persistent CRC/SHA1 cache keyed by path + size + mtime."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from app_paths import app_dir

_CACHE_NAME = "crc_cache.json"
_SAVE_EVERY = 64  # persist after this many new/updated entries


def _cache_path() -> Path:
    return app_dir() / _CACHE_NAME


def _norm_key(path: Path) -> str:
    try:
        s = str(path.resolve())
    except OSError:
        s = str(path)
    # Windows paths are case-insensitive
    return s.replace("/", "\\").casefold()


def _stat_sig(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    return int(st.st_size), int(mtime_ns)


class CrcCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, dict] = {}
        self._dirty = 0
        self._loaded = False

    @property
    def entry_count(self) -> int:
        self.load()
        with self._lock:
            return len(self._entries)

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            path = _cache_path()
            if not path.is_file():
                return
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                return
            if isinstance(raw, dict):
                # support {"entries": {...}} or flat map
                data = raw.get("entries", raw)
                if isinstance(data, dict):
                    self._entries = {
                        str(k): v for k, v in data.items() if isinstance(v, dict)
                    }

    def save(self, *, force: bool = False) -> None:
        with self._lock:
            if not self._loaded:
                return
            if not force and self._dirty <= 0:
                return
            path = _cache_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "version": 1,
                    "entries": self._entries,
                }
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                tmp.replace(path)
                self._dirty = 0
            except OSError:
                pass

    def get(
        self, path: Path, *, need_sha1: bool = False
    ) -> Optional[Tuple[str, str, int]]:
        """
        Return (crc_hex, sha1_hex, size) if cache hit and signature matches.
        sha1_hex may be "" unless need_sha1 and it was stored.
        """
        self.load()
        sig = _stat_sig(path)
        if sig is None:
            return None
        size, mtime_ns = sig
        key = _norm_key(path)
        with self._lock:
            ent = self._entries.get(key)
            if not ent:
                return None
            if int(ent.get("size", -1)) != size or int(ent.get("mtime_ns", -1)) != mtime_ns:
                return None
            crc = str(ent.get("crc") or "")
            sha1 = str(ent.get("sha1") or "")
            if not crc:
                return None
            if need_sha1 and not sha1:
                return None
            return crc, sha1, size

    def put(
        self,
        path: Path,
        crc: str,
        size: int,
        sha1: str = "",
        *,
        mtime_ns: Optional[int] = None,
    ) -> None:
        if not crc:
            return
        self.load()
        if mtime_ns is None:
            sig = _stat_sig(path)
            if sig is None:
                return
            size, mtime_ns = sig[0], sig[1]
        key = _norm_key(path)
        with self._lock:
            prev = self._entries.get(key)
            # Keep existing sha1 if new put is CRC-only
            if prev and not sha1 and prev.get("sha1"):
                if (
                    int(prev.get("size", -1)) == size
                    and int(prev.get("mtime_ns", -1)) == mtime_ns
                    and prev.get("crc") == crc
                ):
                    sha1 = str(prev.get("sha1") or "")
            self._entries[key] = {
                "size": int(size),
                "mtime_ns": int(mtime_ns),
                "crc": crc.lower(),
                "sha1": (sha1 or "").lower(),
            }
            self._dirty += 1
            dirty = self._dirty
        if dirty >= _SAVE_EVERY:
            self.save(force=True)


_CACHE = CrcCache()


def get_crc_cache() -> CrcCache:
    return _CACHE
