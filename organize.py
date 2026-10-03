#!/usr/bin/env python3
"""
Legacy CLI helper for MiSTer ROM Sync (YAML config).

Analyze a messy dump of ROM/game files and sort them into MiSTer /games/<Core>/.
Prefer the GUI entrypoint: mister_rom_sync.py / run.bat.

Classification order:
  1) unique file extensions
  2) magic / header signatures
  3) for .zip — peek at members (extensions + magic)
  4) otherwise → unknown
"""

from __future__ import annotations

import argparse
import io
import logging
import shutil
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_ROOT = Path(__file__).resolve().parent
_VENDOR = _ROOT / "vendor"
if _VENDOR.is_dir():
    sys.path.insert(0, str(_VENDOR))

try:
    import yaml
except ImportError:
    print("PyYAML не найден. Запустите organize.bat или:", file=sys.stderr)
    print(f'  py -m pip install -r "{_ROOT / "requirements.txt"}" -t "{_VENDOR}"', file=sys.stderr)
    sys.exit(1)


LOG = logging.getLogger("organize")

# Unique / high-confidence extensions → MiSTer core folder
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
    ".chd": "PSX",  # often PSX/Saturn/MegaCD — refined by parent/name heuristics below
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

# Ambiguous extensions — only trust magic / archive peek
AMBIGUOUS_EXT = {".bin", ".rom", ".img", ".raw", ".zip", ".7z", ".rar"}

SKIP_EXT = {
    ".txt", ".nfo", ".url", ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".html", ".htm", ".pdf", ".exe", ".dll", ".bat", ".cmd", ".ps1", ".py",
    ".json", ".xml", ".csv", ".db", ".ini", ".cfg", ".log", ".mra", ".rbf",
    ".mgl", ".sav", ".srm", ".state", ".dsv", ".cht", ".ips", ".bps", ".ups",
    # NOTE: do NOT put ".md" here — Mega Drive / Genesis ROMs use .md
}


@dataclass
class Config:
    source_path: Path
    destination_path: Path
    action: str = "copy"
    dry_run: bool = True
    unknown_mode: str = "copy_to_unknown"
    inspect_archives: bool = True
    copy_workers: int = 2
    log_dir: Path = field(default_factory=lambda: _ROOT / "logs")
    ignore_names: Set[str] = field(default_factory=set)


@dataclass
class Classification:
    core: Optional[str]
    confidence: str  # high | medium | low | skip | unknown
    reason: str


@dataclass
class Planned:
    source: Path
    dest: Path
    core: str
    reason: str
    size: int


@dataclass
class Stats:
    scanned: int = 0
    classified: int = 0
    unknown: int = 0
    skipped: int = 0
    copied: int = 0
    already_ok: int = 0
    failed: int = 0
    by_core: Dict[str, int] = field(default_factory=dict)


def load_config(path: Path) -> Config:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    action = str(raw.get("action", "copy")).strip().lower()
    if action not in {"copy", "move"}:
        raise ValueError(f"Invalid action {action!r}")

    unknown_mode = str(raw.get("unknown_mode", "copy_to_unknown")).strip().lower()
    if unknown_mode not in {"skip", "copy_to_unknown"}:
        raise ValueError(f"Invalid unknown_mode {unknown_mode!r}")

    log_dir_raw = raw.get("log_dir")
    if log_dir_raw:
        log_dir = Path(log_dir_raw)
        if not log_dir.is_absolute():
            log_dir = _ROOT / log_dir
    else:
        log_dir = _ROOT / "logs"

    return Config(
        source_path=Path(raw["source_path"]),
        destination_path=Path(raw["destination_path"]),
        action=action,
        dry_run=bool(raw.get("dry_run", True)),
        unknown_mode=unknown_mode,
        inspect_archives=bool(raw.get("inspect_archives", True)),
        copy_workers=max(1, int(raw.get("copy_workers", 2))),
        log_dir=log_dir,
        ignore_names={str(x) for x in (raw.get("ignore_names") or [])},
    )


def setup_logging(verbose: bool, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"organize_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    root.addHandler(fh)
    return log_path


def is_ignored(name: str, ignore_names: Set[str]) -> bool:
    return name in ignore_names or name.startswith(".")


def read_prefix(path: Path, size: int = 512) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(size)
    except OSError:
        return b""


def detect_magic(data: bytes) -> Optional[str]:
    if len(data) < 16:
        return None

    # NES iNES
    if data[:4] == b"NES\x1a":
        return "NES"

    # FDS
    if data[:4] == b"FDS\x1a" or data[:3] == b"\x01*N":
        return "NES"

    # Game Boy logo at 0x104; CGB flag at 0x143 (0x80/0xC0 → GBC)
    if len(data) >= 0x10C and data[0x104:0x10C] == bytes.fromhex("CEED6666CC0D000B"):
        if len(data) > 0x143 and data[0x143] in (0x80, 0xC0):
            return "GBC"
        return "GAMEBOY"

    # GBA nintendo logo start at 0x04
    if len(data) >= 0xB0 and data[0x04:0x08] == bytes.fromhex("24FFAE51"):
        return "GBA"

    # Mega Drive / Genesis "SEGA" at 0x100 or 0x101
    if len(data) >= 0x108:
        if data[0x100:0x104] == b"SEGA" or data[0x101:0x105] == b"SEGA":
            if len(data) >= 0x3C4 and data[0x3C0:0x3C4] == b"MARS":
                return "S32X"
            return "MegaDrive"

    # SMD interleaved genesis
    if len(data) >= 0x284 and data[0x280:0x284] in (b"EAGN", b"EAMG"):
        return "MegaDrive"

    # PC Engine / TGFX usually no great magic; skip

    # PlayStation EXE
    if data[:8] == b"PS-X EXE":
        return "PSX"

    # N64 big-endian .z64 magic
    if data[:4] == bytes.fromhex("80371240"):
        return "N64"
    # byte-swapped .v64
    if data[:4] == bytes.fromhex("37804012"):
        return "N64"
    # little-endian .n64
    if data[:4] == bytes.fromhex("40123780"):
        return "N64"

    # SMS / GG often start with region header; weak — skip

    # WonderSwan weak; skip

    # ZIP local file header mistakenly passed — ignore
    if data[:2] == b"PK":
        return None

    return None


def snes_checksum_plausible(data: bytes) -> bool:
    """Very light SNES header checksum probe at common offsets."""
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
    """Heuristic core for disc images by path keywords."""
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


def classify_by_extension(ext: str, path: Path) -> Optional[Classification]:
    if ext in SKIP_EXT:
        return Classification(None, "skip", f"non-rom extension {ext}")
    if ext in {".chd", ".cue", ".iso"}:
        core = classify_chd_or_cue(path)
        return Classification(core, "medium", f"disc image ext {ext} → {core}")
    core = EXT_MAP.get(ext)
    if core:
        return Classification(core, "high", f"extension {ext}")
    return None


def classify_zip(path: Path) -> Classification:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                return Classification(None, "unknown", "empty zip")

            votes: Dict[str, int] = {}
            reasons: List[str] = []

            for name in names[:40]:
                inner_ext = Path(name).suffix.casefold()
                if inner_ext in SKIP_EXT:
                    continue
                mapped = EXT_MAP.get(inner_ext)
                if mapped:
                    # disc images inside zip still need path heuristic
                    if inner_ext in {".chd", ".cue", ".iso"}:
                        mapped = classify_chd_or_cue(Path(name))
                    votes[mapped] = votes.get(mapped, 0) + 3
                    reasons.append(f"{name}:{inner_ext}")
                    continue

                if inner_ext in AMBIGUOUS_EXT or not inner_ext:
                    try:
                        with zf.open(name) as member:
                            data = member.read(0x10100)
                    except Exception:  # noqa: BLE001
                        continue
                    magic = detect_magic(data)
                    if magic:
                        votes[magic] = votes.get(magic, 0) + 2
                        reasons.append(f"{name}:magic:{magic}")
                    elif snes_checksum_plausible(data):
                        votes["SNES"] = votes.get("SNES", 0) + 1
                        reasons.append(f"{name}:snes-header")

            if not votes:
                # MAME-style single zip with no known rom ext — leave unknown
                return Classification(None, "unknown", "zip without recognizable roms")

            best = max(votes.items(), key=lambda kv: kv[1])
            conf = "high" if best[1] >= 3 else "medium"
            return Classification(best[0], conf, f"zip→{best[0]} ({', '.join(reasons[:3])})")
    except zipfile.BadZipFile:
        return Classification(None, "unknown", "bad zip")
    except OSError as exc:
        return Classification(None, "unknown", f"zip error: {exc}")


def classify_file(path: Path, cfg: Config) -> Classification:
    ext = path.suffix.casefold()

    by_ext = classify_by_extension(ext, path)
    if by_ext and by_ext.confidence in {"high", "skip"}:
        return by_ext
    if by_ext and by_ext.confidence == "medium" and ext not in AMBIGUOUS_EXT:
        return by_ext

    if ext == ".zip" and cfg.inspect_archives:
        return classify_zip(path)

    if ext in {".7z", ".rar"}:
        return Classification(None, "unknown", f"archive {ext} not inspected")

    data = read_prefix(path, 0x10100)
    magic = detect_magic(data)
    if magic:
        return Classification(magic, "high", f"magic→{magic}")

    if snes_checksum_plausible(data) or (ext in {".sfc", ".smc"}):
        if snes_checksum_plausible(data):
            return Classification("SNES", "medium", "snes header checksum")

    if by_ext:
        return by_ext

    if ext in AMBIGUOUS_EXT:
        return Classification(None, "unknown", f"ambiguous {ext}, no magic")

    return Classification(None, "unknown", f"unrecognized {ext or '[no ext]'}")


def needs_copy(src: Path, dest: Path) -> bool:
    if not dest.exists():
        return True
    try:
        return src.stat().st_size != dest.stat().st_size
    except OSError:
        return True


def transfer(src: Path, dest: Path, action: str, dry_run: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return
    tmp = dest.with_name(dest.name + ".partial")
    try:
        if tmp.exists():
            tmp.unlink()
        shutil.copy2(src, tmp)
        tmp.replace(dest)
        if action == "move":
            src.unlink()
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def organize(cfg: Config) -> Stats:
    stats = Stats()
    if not cfg.source_path.exists():
        raise FileNotFoundError(f"Source not found: {cfg.source_path}")
    if not cfg.dry_run:
        cfg.destination_path.mkdir(parents=True, exist_ok=True)

    planned: List[Planned] = []

    for path in cfg.source_path.rglob("*"):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if any(is_ignored(p, cfg.ignore_names) for p in path.parts):
            continue

        stats.scanned += 1
        result = classify_file(path, cfg)

        if result.confidence == "skip":
            stats.skipped += 1
            LOG.debug("SKIP %s (%s)", path, result.reason)
            continue

        if not result.core:
            stats.unknown += 1
            LOG.info("UNKNOWN %s (%s)", path, result.reason)
            if cfg.unknown_mode == "copy_to_unknown":
                dest = cfg.destination_path / "_unknown" / path.name
                # avoid overwrite collisions
                if dest.exists() and dest.resolve() != path.resolve():
                    stem, suffix = path.stem, path.suffix
                    n = 2
                    while True:
                        cand = cfg.destination_path / "_unknown" / f"{stem}__{n}{suffix}"
                        if not cand.exists():
                            dest = cand
                            break
                        n += 1
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                planned.append(Planned(path, dest, "_unknown", result.reason, size))
            continue

        stats.classified += 1
        stats.by_core[result.core] = stats.by_core.get(result.core, 0) + 1
        dest = cfg.destination_path / result.core / path.name
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        planned.append(Planned(path, dest, result.core, result.reason, size))
        LOG.debug("CLASS %s → %s (%s)", path.name, result.core, result.reason)

    to_do: List[Planned] = []
    for item in planned:
        if needs_copy(item.source, item.dest):
            to_do.append(item)
        else:
            stats.already_ok += 1

    LOG.info(
        "Plan: transfer=%d already=%d unknown=%d skipped=%d dry_run=%s",
        len(to_do),
        stats.already_ok,
        stats.unknown,
        stats.skipped,
        cfg.dry_run,
    )

    def worker(item: Planned) -> Tuple[Planned, Optional[str]]:
        try:
            transfer(item.source, item.dest, cfg.action, cfg.dry_run)
            return item, None
        except Exception as exc:  # noqa: BLE001
            return item, str(exc)

    if not to_do:
        return stats

    workers = 1 if cfg.dry_run else cfg.copy_workers
    done = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(worker, i): i for i in to_do}
        for fut in as_completed(futs):
            item, err = fut.result()
            done += 1
            if err:
                stats.failed += 1
                LOG.error("FAIL %s: %s", item.source, err)
            else:
                stats.copied += 1
                if done == 1 or done == len(to_do) or done % 100 == 0:
                    verb = "WOULD" if cfg.dry_run else cfg.action.upper()
                    LOG.info(
                        "%s [%d/%d] %s → %s/ (%s)",
                        verb,
                        done,
                        len(to_do),
                        item.source.name,
                        item.core,
                        item.reason,
                    )
    LOG.info("Transfer finished in %.1fs", time.time() - started)
    return stats


def print_summary(cfg: Config, stats: Stats, log_path: Optional[Path]) -> None:
    status = "ОШИБКИ" if stats.failed else ("ПРОБНЫЙ ПРОГОН" if cfg.dry_run else "ГОТОВО")
    lines = [
        "",
        "=" * 60,
        f"  СВОДКА ORGANIZE: {status}",
        "=" * 60,
        f"  Просканировано     : {stats.scanned}",
        f"  Распознано         : {stats.classified}",
        f"  Неизвестно         : {stats.unknown}",
        f"  Пропущено (мусор)  : {stats.skipped}",
        f"  Уже на месте       : {stats.already_ok}",
        f"  Скопировано/перемещ: {stats.copied}"
        + (" (dry-run)" if cfg.dry_run and stats.copied else ""),
        f"  Ошибок             : {stats.failed}",
        f"  Действие           : {cfg.action}",
        f"  Источник           : {cfg.source_path}",
        f"  Назначение         : {cfg.destination_path}",
    ]
    if stats.by_core:
        lines.append("  По системам:")
        for core, count in sorted(stats.by_core.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {core:16} {count}")
    if log_path:
        lines.append(f"  Лог                : {log_path}")
    lines.extend(["=" * 60, ""])
    print("\n".join(lines), flush=True)
    for line in lines:
        if line.strip():
            LOG.info("%s", line)


def wait_for_key() -> None:
    print("Нажмите Enter, чтобы закрыть окно...", flush=True)
    try:
        input()
    except EOFError:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sort a messy ROM dump into MiSTer games folders.")
    p.add_argument("-c", "--config", default="config_mister_rom_sync.yaml")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--move", action="store_true", help="Move instead of copy")
    p.add_argument("--pause", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    exit_code = 0
    log_path: Optional[Path] = None
    try:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = Path.cwd() / cfg_path
        if not cfg_path.exists():
            print(f"\nОШИБКА: конфиг не найден: {cfg_path}\n", flush=True)
            exit_code = 1
        else:
            cfg = load_config(cfg_path)
            log_path = setup_logging(args.verbose, cfg.log_dir)
            LOG.info("Log file: %s", log_path)

            if args.apply:
                cfg.dry_run = False
            if args.dry_run:
                cfg.dry_run = True
            if args.move:
                cfg.action = "move"

            LOG.info("Source: %s", cfg.source_path)
            LOG.info("Dest  : %s", cfg.destination_path)
            LOG.info(
                "action=%s dry_run=%s unknown=%s",
                cfg.action,
                cfg.dry_run,
                cfg.unknown_mode,
            )

            try:
                stats = organize(cfg)
            except Exception as exc:  # noqa: BLE001
                LOG.error("%s", exc)
                print(f"\nОШИБКА: {exc}\n", flush=True)
                exit_code = 1
            else:
                print_summary(cfg, stats, log_path)
                exit_code = 1 if stats.failed else 0
    finally:
        if args.pause:
            wait_for_key()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
