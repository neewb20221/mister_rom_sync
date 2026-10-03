#!/usr/bin/env python3
"""DAT sources manager: enable/disable, priority, update."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Optional

from dat_download import (
    add_custom_source,
    download_all_dats,
    download_custom_sources,
    ensure_default_dat_layout,
    update_source_label,
)
from dat_prefs import (
    DatPrefs,
    merge_prefs_with_folder,
    move_entry,
    save_dat_prefs,
)


class DatManagerDialog(tk.Toplevel):
    """Modal-ish window to manage DAT files in one folder."""

    def __init__(
        self,
        master: tk.Misc,
        dat_dir: Path,
        *,
        recursive: bool = False,
        on_saved: Optional[Callable[[Path, DatPrefs], None]] = None,
    ) -> None:
        super().__init__(master)
        self.title("DAT files")
        self.geometry("1120x560")
        self.minsize(960, 420)
        self.transient(master)
        self.grab_set()

        self._dat_dir = Path(dat_dir)
        self._recursive = bool(recursive)
        self._on_saved = on_saved
        ensure_default_dat_layout(self._dat_dir)
        self._prefs = merge_prefs_with_folder(self._dat_dir, recursive=self._recursive)
        save_dat_prefs(self._dat_dir, self._prefs)
        self._busy = False

        self._build()
        self._reload_list()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 6}
        root = ttk.Frame(self, padding=8)
        root.pack(fill=tk.BOTH, expand=True)

        path_row = ttk.Frame(root)
        path_row.pack(fill=tk.X, **pad)
        ttk.Label(path_row, text="DAT folder:").pack(side=tk.LEFT)
        self.var_dir = tk.StringVar(value=str(self._dat_dir))
        ent = ttk.Entry(path_row, textvariable=self.var_dir)
        ent.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(path_row, text="Browse…", command=self._browse).pack(side=tk.LEFT)

        hint = ttk.Label(
            root,
            text="Higher in the list = higher priority.",
        )
        hint.pack(fill=tk.X, padx=10)

        mid = ttk.Frame(root)
        mid.pack(fill=tk.BOTH, expand=True, **pad)

        side = ttk.Frame(mid)
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        ttk.Button(side, text="↑ Up", command=lambda: self._move(-1)).pack(fill=tk.X, pady=2)
        ttk.Button(side, text="↓ Down", command=lambda: self._move(1)).pack(fill=tk.X, pady=2)
        ttk.Separator(side, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
        ttk.Button(side, text="Toggle On/Off", command=self._toggle).pack(fill=tk.X, pady=2)
        ttk.Button(side, text="Enable all", command=lambda: self._set_all(True)).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(side, text="Disable all", command=lambda: self._set_all(False)).pack(
            fill=tk.X, pady=2
        )
        ttk.Separator(side, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
        ttk.Button(side, text="Refresh list", command=self._refresh_from_disk).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(side, text="Add custom…", command=self._add_custom).pack(
            fill=tk.X, pady=2
        )

        cols = ("on", "pri", "kind", "file", "size", "source")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("on", text="On")
        self.tree.heading("pri", text="#")
        self.tree.heading("kind", text="Type")
        self.tree.heading("file", text="DAT file")
        self.tree.heading("size", text="Size")
        self.tree.heading("source", text="Update from")
        self.tree.column("on", width=44, anchor=tk.CENTER, stretch=False)
        self.tree.column("pri", width=40, anchor=tk.CENTER, stretch=False)
        self.tree.column("kind", width=70, anchor=tk.CENTER, stretch=False)
        self.tree.column("file", width=320, anchor=tk.W)
        self.tree.column("size", width=80, anchor=tk.E, stretch=False)
        self.tree.column("source", width=420, anchor=tk.W)
        scroll = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<space>", self._on_space)

        upd = ttk.LabelFrame(root, text="Update from internet", padding=8)
        upd.pack(fill=tk.X, **pad)
        ttk.Button(upd, text="Update all", command=self._update_all).pack(side=tk.LEFT)

        self.lbl_status = ttk.Label(root, text="")
        self.lbl_status.pack(fill=tk.X, padx=10)

        bot = ttk.Frame(root)
        bot.pack(fill=tk.X, **pad)
        ttk.Button(bot, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(bot, text="Cancel", command=self._on_close).pack(side=tk.RIGHT)

    def _browse(self) -> None:
        path = filedialog.askdirectory(title="DAT folder", initialdir=self.var_dir.get() or None)
        if path:
            self.var_dir.set(path)
            self._dat_dir = Path(path)
            self._prefs = merge_prefs_with_folder(
                self._dat_dir, recursive=self._recursive
            )
            self._reload_list()

    def _reload_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self._prefs.entries):
            mark = "☑" if e.enabled else "☐"
            kind = e.kind or "?"
            size = _fmt_size(e.size) if e.exists else "missing"
            src = update_source_label(e.rel, self._dat_dir)
            self.tree.insert(
                "",
                tk.END,
                iid=str(i),
                values=(mark, str(i + 1), kind, e.rel, size, src),
            )
        on = sum(1 for e in self._prefs.entries if e.enabled and e.exists)
        missing = sum(1 for e in self._prefs.entries if not e.exists)
        self.lbl_status.configure(
            text=(
                f"{len(self._prefs.entries)} DAT(s), {on} on disk enabled, "
                f"{missing} not downloaded — {self._dat_dir}"
            )
        )

    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _on_double(self, _evt=None) -> None:
        self._toggle()

    def _on_space(self, _evt=None) -> str:
        self._toggle()
        return "break"

    def _toggle(self) -> None:
        idx = self._selected_index()
        if idx is None or idx >= len(self._prefs.entries):
            return
        e = self._prefs.entries[idx]
        e.enabled = not e.enabled
        self._reload_list()
        self.tree.selection_set(str(idx))
        self.tree.see(str(idx))

    def _set_all(self, enabled: bool) -> None:
        for e in self._prefs.entries:
            e.enabled = enabled
        self._reload_list()

    def _move(self, delta: int) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        move_entry(self._prefs, idx, delta)
        new_idx = max(0, min(len(self._prefs.entries) - 1, idx + delta))
        self._reload_list()
        self.tree.selection_set(str(new_idx))
        self.tree.see(str(new_idx))

    def _refresh_from_disk(self) -> None:
        self._dat_dir = Path(self.var_dir.get().strip() or self._dat_dir)
        self._prefs = merge_prefs_with_folder(
            self._dat_dir, self._prefs, recursive=self._recursive
        )
        self._reload_list()

    def _add_custom(self) -> None:
        """Add a custom DAT update URL (and optionally copy a local file now)."""
        if self._busy:
            return
        self._dat_dir = Path(self.var_dir.get().strip() or self._dat_dir)
        self._dat_dir.mkdir(parents=True, exist_ok=True)

        url = simpledialog.askstring(
            "Custom DAT source",
            "Direct download URL for a .dat / .xml file:",
            parent=self,
        )
        if not url or not url.strip():
            return
        url = url.strip()

        default_name = Path(url.split("?", 1)[0]).name
        if not default_name.casefold().endswith((".dat", ".xml")):
            default_name = "custom.dat"
        name = simpledialog.askstring(
            "Custom DAT source",
            "Local filename in DAT folder:",
            initialvalue=default_name,
            parent=self,
        )
        if not name or not name.strip():
            return
        name = Path(name.strip()).name
        if not name.casefold().endswith((".dat", ".xml")):
            messagebox.showerror(
                "Custom DAT source",
                "Filename must end with .dat or .xml",
                parent=self,
            )
            return

        add_custom_source(self._dat_dir, name, url)
        self.lbl_status.configure(text=f"Downloading custom {name}…")
        self._set_busy(True)

        def work() -> None:
            try:
                d, s, _r, notes = download_custom_sources(self._dat_dir)
                msg = f"Custom source: {name}"
                if notes:
                    msg += " — " + "; ".join(notes[:3])
                elif d or s:
                    msg += " — downloaded"
                self.after(0, lambda m=msg: self._custom_done(m, name))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._update_fail(e))

        threading.Thread(target=work, daemon=True).start()

    def _custom_done(self, msg: str, name: str) -> None:
        self._set_busy(False)
        self._prefs = merge_prefs_with_folder(
            self._dat_dir, self._prefs, recursive=self._recursive
        )
        for e in self._prefs.entries:
            if Path(e.rel).name.casefold() == name.casefold():
                e.enabled = True
                break
        self._reload_list()
        self.lbl_status.configure(text=msg)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy

    def _update_all(self) -> None:
        if self._busy:
            return
        self._dat_dir = Path(self.var_dir.get().strip() or self._dat_dir)
        self._dat_dir.mkdir(parents=True, exist_ok=True)
        self._set_busy(True)
        self.lbl_status.configure(text="Updating…")

        def work() -> None:
            try:

                def prog(stage: str, frac: float, detail: str) -> None:
                    self.after(0, lambda: self.lbl_status.configure(text=detail or stage))

                d, s, r, _, notes = download_all_dats(self._dat_dir, prog)
                msg = f"Updated {d}, up-to-date {s}, removed {r}"
                self.after(0, lambda m=msg, n=list(notes): self._update_done(m, n))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._update_fail(e))

        threading.Thread(target=work, daemon=True).start()

    def _update_done(self, msg: str, notes: list) -> None:
        self._set_busy(False)
        self._prefs = merge_prefs_with_folder(
            self._dat_dir, self._prefs, recursive=self._recursive
        )
        self._reload_list()
        self.lbl_status.configure(text=msg)
        if notes:
            messagebox.showinfo("DAT update", msg + "\n\n" + "\n".join(notes[:20]), parent=self)

    def _update_fail(self, err: str) -> None:
        self._set_busy(False)
        self.lbl_status.configure(text="Update failed")
        messagebox.showerror("DAT update", err, parent=self)

    def _save(self) -> None:
        self._dat_dir = Path(self.var_dir.get().strip() or self._dat_dir)
        save_dat_prefs(self._dat_dir, self._prefs)
        if self._on_saved:
            self._on_saved(self._dat_dir, self._prefs)
        self.destroy()

    def _on_close(self) -> None:
        if self._busy:
            return
        self.destroy()


def _fmt_size(n: int) -> str:
    x = float(max(0, int(n)))
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024 or unit == "GB":
            return f"{int(x)} {unit}" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024.0
    return f"{x:.1f} GB"
