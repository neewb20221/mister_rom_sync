"""Regenerate dat_sources_default.py from dats/dat_sources.json."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
src = json.loads((ROOT / "dats" / "dat_sources.json").read_text(encoding="utf-8"))
rels = [(s["rel"], bool(s.get("enabled", True))) for s in src.get("sources") or []]
out = ROOT / "dat_sources_default.py"
lines = [
    '"""Built-in default DAT catalogue (shown before first Update)."""',
    "from __future__ import annotations",
    "",
    "# (rel, enabled) — seeded into dats/dat_sources.json on first run",
    "DEFAULT_DAT_SOURCES = [",
]
for rel, en in rels:
    lines.append(f"    ({rel!r}, {en}),")
lines.append("]")
lines.append("")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {out.name} ({len(rels)} entries)")
