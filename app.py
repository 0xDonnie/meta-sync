#!/usr/bin/env python3
"""
Meta Sync App — a single native GUI that combines:
  - Phone detection status
  - Sync buttons (Meta, Camera, Both)
  - Stats summary (total files, folders, countries, size)
  - Folder browser with search
  - Button to open the full HTML report in the browser

Runs as a standalone tkinter app. No server, no polling.
Click "Check phone" to detect ADB device (on-demand only).
Click "Refresh" to rescan local staging and update the stats.

Usage:
    pythonw app.py       # no console
    python app.py        # with console for debugging
"""

import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).parent))
from report import load_config, scan_from_manifest, render_html, human_size
from manifest import load_manifest, incremental_update, save_manifest, MANIFEST_PATH

META_DIR = Path("D:/GitHub/meta-sync")
CAMERA_DIR = Path("D:/GitHub/camera-sync")
REPORT_HTML = META_DIR / "report.html"

# Colors (dark theme)
BG = "#0b0b0f"
PANEL = "#141420"
BORDER = "#23233a"
HOVER = "#1a1a2a"
TEXT = "#e7e7f0"
MUTED = "#8a8aa0"
ACCENT = "#5b9dff"
GREEN = "#5fd48e"
RED = "#ff6b6b"


def is_phone_connected() -> bool:
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def launch(cmd: list, cwd: str):
    subprocess.Popen(
        cmd, cwd=cwd,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


class MetaSyncApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Meta Sync")
        self.root.geometry("1100x720")
        self.root.configure(bg=BG)
        self.root.minsize(900, 600)

        self.folders: list[dict] = []
        self.phone_state = "unknown"
        self._loading = False

        self._build()
        # Defer heavy startup work so the window appears immediately.
        # Both tasks run in background threads.
        self.root.after(100, self.refresh_data_async)
        self.root.after(150, self.check_phone_async)

    # -------- UI construction --------

    def _build(self):
        # Top toolbar
        bar = tk.Frame(self.root, bg=BG, padx=20, pady=16)
        bar.pack(fill="x")

        tk.Label(bar, text="Meta Sync", font=("Segoe UI", 16, "bold"),
                 fg=TEXT, bg=BG).pack(side="left")

        self.phone_status = tk.Label(
            bar, text="● checking...", font=("Segoe UI", 10),
            fg=MUTED, bg=BG, padx=16,
        )
        self.phone_status.pack(side="left")

        # Buttons on the right
        self._btn(bar, "Check phone", self.check_phone).pack(side="right", padx=3)
        self._btn(bar, "Refresh", self.refresh_data).pack(side="right", padx=3)
        self._btn(bar, "Open HTML report", self.open_report).pack(side="right", padx=3)

        # Sync buttons row
        sync_bar = tk.Frame(self.root, bg=BG, padx=20)
        sync_bar.pack(fill="x", pady=(0, 14))
        self.btn_meta = self._btn(sync_bar, "Sync Meta", lambda: self.run_sync("meta"), primary=True)
        self.btn_meta.pack(side="left", padx=3)
        self.btn_camera = self._btn(sync_bar, "Sync Camera", lambda: self.run_sync("camera"), primary=True)
        self.btn_camera.pack(side="left", padx=3)
        self.btn_both = self._btn(sync_bar, "Sync Both", lambda: self.run_sync("both"), primary=True)
        self.btn_both.pack(side="left", padx=3)

        # Stats cards
        self.stats_frame = tk.Frame(self.root, bg=BG, padx=20)
        self.stats_frame.pack(fill="x")

        # Search bar
        search_bar = tk.Frame(self.root, bg=BG, padx=20, pady=12)
        search_bar.pack(fill="x")
        tk.Label(search_bar, text="Search", fg=MUTED, bg=BG,
                 font=("Segoe UI", 10)).pack(side="left", padx=(0, 8))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.apply_filter())
        entry = tk.Entry(
            search_bar, textvariable=self.search_var,
            bg=PANEL, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=("Segoe UI", 10),
            highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 12))

        # Folder browser (treeview)
        tree_frame = tk.Frame(self.root, bg=BG, padx=20)
        tree_frame.pack(fill="both", expand=True, pady=(0, 20))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Dark.Treeview",
            background=PANEL, fieldbackground=PANEL,
            foreground=TEXT, borderwidth=0,
            rowheight=26, font=("Segoe UI", 10),
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=BG, foreground=MUTED,
            borderwidth=0, relief="flat",
            font=("Segoe UI", 9, "bold"),
        )
        style.map("Dark.Treeview",
                  background=[("selected", HOVER)],
                  foreground=[("selected", ACCENT)])
        style.map("Dark.Treeview.Heading", background=[("active", BG)])

        self.tree = ttk.Treeview(
            tree_frame, style="Dark.Treeview",
            columns=("size",), show="tree headings",
        )
        self.tree.heading("#0", text="Folder / File")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=700, anchor="w")
        self.tree.column("size", width=120, anchor="e")

        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _btn(self, parent, text, command, primary: bool = False):
        bg = ACCENT if primary else PANEL
        fg = BG if primary else TEXT
        b = tk.Button(
            parent, text=text, command=command,
            bg=bg, fg=fg, activebackground=HOVER if not primary else ACCENT,
            activeforeground=ACCENT if not primary else BG,
            relief="flat", bd=0, padx=14, pady=7,
            font=("Segoe UI", 10, "bold" if primary else "normal"),
            cursor="hand2",
        )
        if not primary:
            b.bind("<Enter>", lambda e: b.config(bg=HOVER))
            b.bind("<Leave>", lambda e: b.config(bg=PANEL))
        return b

    # -------- Data --------

    def refresh_data_async(self):
        """Load from manifest (instant) + incremental update in background."""
        if self._loading:
            return
        self._loading = True
        self.phone_status.config(text="● loading from manifest...", fg=MUTED)

        def worker():
            folders: list[dict] = []
            try:
                # Step 1: load manifest (instant)
                folders = scan_from_manifest()
            except Exception as e:
                print(f"manifest load error: {e}")
            # Update UI with manifest data first (fast)
            self.root.after(0, lambda: self._finalize_refresh(folders))

            # Step 2: try incremental update (scan Z: for new files only)
            try:
                config = load_config(str(META_DIR / "config.yaml"))
                dest_root = Path(config["destination_dir"])
                existing = load_manifest()
                updated, new_count = incremental_update(dest_root, existing.get("folders", {}), config)
                if new_count > 0:
                    save_manifest({"folders": updated})
                    # Reload UI with updated data
                    folders2 = scan_from_manifest()
                    self.root.after(0, lambda: self._finalize_refresh(folders2))
                    print(f"manifest incremental: +{new_count} new files")
            except Exception as e:
                print(f"manifest update error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _finalize_refresh(self, folders: list[dict]):
        self.folders = folders
        self._render_stats()
        self._render_tree()
        self._loading = False
        # Re-run the phone check to restore the real status label
        self.check_phone_async()
        # Regenerate HTML report in background too
        def regen():
            try:
                source_label = "local staging + EXIF + geocache + camera trackers"
                REPORT_HTML.write_text(render_html(self.folders, source_label), encoding="utf-8")
            except Exception as e:
                print(f"html regen error: {e}")
        threading.Thread(target=regen, daemon=True).start()

    def refresh_data(self):
        """Synchronous version for the toolbar button; delegates to async."""
        self.refresh_data_async()

    def _render_stats(self):
        for w in self.stats_frame.winfo_children():
            w.destroy()
        total_files = sum(f["count"] for f in self.folders)
        total_size = sum(f["size"] for f in self.folders)
        countries = len({f["country"] for f in self.folders})

        stats = [
            ("Total files", f"{total_files}"),
            ("Total size", human_size(total_size)),
            ("Folders", f"{len(self.folders)}"),
            ("Countries", f"{countries}"),
        ]

        for label, value in stats:
            card = tk.Frame(self.stats_frame, bg=PANEL,
                            highlightthickness=1, highlightbackground=BORDER)
            card.pack(side="left", fill="x", expand=True, padx=4, ipadx=6, ipady=10)
            tk.Label(card, text=label.upper(),
                     font=("Segoe UI", 8), fg=MUTED, bg=PANEL).pack(pady=(6, 2))
            tk.Label(card, text=value, font=("Segoe UI", 18, "bold"),
                     fg=ACCENT, bg=PANEL).pack(pady=(0, 6))

    def _render_tree(self):
        # Clear
        for item in self.tree.get_children():
            self.tree.delete(item)
        # Populate
        for f in self.folders:
            folder_id = self.tree.insert(
                "", "end",
                text=f'{f["folder"]}   ({f["count"]} files)',
                values=(human_size(f["size"]),),
                open=False,
            )
            for file in f["files"]:
                self.tree.insert(
                    folder_id, "end",
                    text=file["name"],
                    values=(human_size(file["size"]),),
                )

    # -------- Filter --------

    def apply_filter(self):
        term = self.search_var.get().lower().strip()
        # Collapse & re-render with filter
        for item in self.tree.get_children():
            self.tree.delete(item)
        for f in self.folders:
            folder_match = term in f["folder"].lower()
            matching_files = [
                file for file in f["files"]
                if not term or folder_match or term in file["name"].lower()
            ]
            if not term or folder_match or matching_files:
                folder_id = self.tree.insert(
                    "", "end",
                    text=f'{f["folder"]}   ({len(matching_files)} files)',
                    values=(human_size(f["size"]),),
                    open=bool(term),
                )
                for file in matching_files:
                    self.tree.insert(
                        folder_id, "end",
                        text=file["name"],
                        values=(human_size(file["size"]),),
                    )

    # -------- Phone --------

    def check_phone_async(self):
        """Non-blocking phone check via a worker thread."""
        self.phone_status.config(text="● checking...", fg=MUTED)

        def worker():
            connected = is_phone_connected()
            self.root.after(0, lambda: self._apply_phone_state(connected))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_phone_state(self, connected: bool):
        self.phone_state = "connected" if connected else "disconnected"
        if connected:
            self.phone_status.config(text="● Phone connected", fg=GREEN)
        else:
            self.phone_status.config(text="● No phone detected", fg=RED)
        for b in (self.btn_meta, self.btn_camera, self.btn_both):
            b.config(state="normal")

    def check_phone(self):
        """Sync wrapper used by the button."""
        self.check_phone_async()

    # -------- Sync actions --------

    def run_sync(self, action: str):
        if action == "meta":
            launch([sys.executable, "-u", str(META_DIR / "watcher.py"), "--once"], str(META_DIR))
        elif action == "camera":
            launch([sys.executable, "-u", str(CAMERA_DIR / "camera_sync.py")], str(CAMERA_DIR))
        elif action == "both":
            launch([sys.executable, "-u", str(META_DIR / "watcher.py"), "--once"], str(META_DIR))
            launch([sys.executable, "-u", str(CAMERA_DIR / "camera_sync.py")], str(CAMERA_DIR))

    def open_report(self):
        if not REPORT_HTML.exists():
            self.refresh_data()
        webbrowser.open(REPORT_HTML.as_uri())


def main():
    root = tk.Tk()
    MetaSyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
