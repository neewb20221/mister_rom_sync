#!/usr/bin/env python3
"""Writable application directory (works for source and frozen .exe)."""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Folder next to the .exe (frozen) or project root (dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """Read-only bundle dir (PyInstaller _MEIPASS) or project root."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent
