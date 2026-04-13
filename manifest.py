#!/usr/bin/env python3
"""
Manifest — caches filename → size data for folders on the destination drive.

The manifest lets the report (and the GUI) show accurate file counts and
total sizes without stat()-ing every file on Z: every time. It relies on
the fact that the destination is write-only: files are only added, never
deleted or renamed, so incremental updates are cheap.

Data shape:
    {
      "generated": "2026-04-13T21:00:00",
      "folders": {
        "2025.07 UAE META-AI":       {"file1.jpg": 1234567, "file2.mp4": ...},
        "2025 CAM Samsung":           {"20250101_xxx.jpg": 54321, ...},
        "2026 Q1 Cam Samsung":        {...},
        ...
      }
    }

Usage:
    python manifest.py --build             # full scan of Z:, write manifest.json
    python manifest.py --update            # incremental: only stat new files
    python manifest.py --show              # print stats about current manifest
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yaml


MANIFEST_PATH = Path(__file__).parent / "manifest.json"

# Folder name patterns we track — CASE-SENSITIVE so we don't pick up the
# user's historical archives like "2011 Cam Samsung" (lowercase Cam).
# We only index folders produced by our own sync scripts.
META_FOLDER_RE = re.compile(r"^\d{4}\.\d{2} .+? META-AI$")
CAMERA_FLAT_RE = re.compile(r"^\d{4} CAM Samsung$")   # "CAM" uppercase only
CAMERA_QUARTER_RE = re.compile(r"^\d{4} Q\d Cam Samsung$")  # "Cam Samsung"


def is_tracked_folder(name: str) -> bool:
    return bool(
        META_FOLDER_RE.match(name)
        or CAMERA_FLAT_RE.match(name)
        or CAMERA_QUARTER_RE.match(name)
    )


def load_config() -> dict:
    cfg_path = Path(__file__).parent / "config.yaml"
    if not cfg_path.exists():
        print("config.yaml not found")
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"generated": None, "folders": {}}


def save_manifest(data: dict):
    data["generated"] = datetime.now().isoformat()
    MANIFEST_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _stat_file(f: Path) -> tuple[str, int] | None:
    """Helper for parallel stat calls."""
    try:
        return (f.name, f.stat().st_size)
    except OSError:
        return None


CAMERA_TRACKER_DIR = Path("D:/GitHub/camera-sync/camera_trackers")


def get_known_folders(config: dict) -> list[str]:
    """Return the list of folder names WE'VE CREATED (via our sync scripts),
    derived from:
      - camera_trackers/*.json (one flat tracker per job + one quarter tracker)
      - synced_files.json + geocache.json for META-AI folder candidates

    NEVER lists Z:\\ itself. Returns folder names only; caller verifies
    existence on the destination.
    """
    candidates: list[str] = []

    # --- Camera flat jobs — tracker filename = destination folder name ---
    # --- Camera quarter — parse dates from tracker file entries         ---
    if CAMERA_TRACKER_DIR.exists():
        for tf in CAMERA_TRACKER_DIR.glob("*.json"):
            try:
                names = json.loads(tf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if tf.stem == "camera_quarter":
                yq_set: set[tuple[int, int]] = set()
                for fname in names:
                    stem = Path(fname).stem
                    for part in stem.replace("-", "_").split("_"):
                        if len(part) == 8 and part.isdigit():
                            try:
                                dt = datetime.strptime(part, "%Y%m%d")
                                q = (dt.month - 1) // 3 + 1
                                yq_set.add((dt.year, q))
                                break
                            except ValueError:
                                continue
                for y, q in sorted(yq_set):
                    candidates.append(f"{y} Q{q} Cam Samsung")
            else:
                # flat tracker stem IS the destination folder name
                candidates.append(tf.stem)

    # --- META folders: cross (year.month) × (country) ---
    synced_path = Path(config.get("synced_file", "synced_files.json"))
    geocache_path = Path(config.get("cache_file", "geocache.json"))

    ym_set: set[str] = set()
    if synced_path.exists():
        try:
            synced = json.loads(synced_path.read_text(encoding="utf-8"))
            for name in synced:
                stem = Path(name).stem
                for part in stem.replace("-", "_").split("_"):
                    if len(part) == 8 and part.isdigit():
                        try:
                            dt = datetime.strptime(part, "%Y%m%d")
                            ym_set.add(dt.strftime("%Y.%m"))
                            break
                        except ValueError:
                            continue
        except json.JSONDecodeError:
            pass

    countries: list[str] = []
    if geocache_path.exists():
        try:
            geocache = json.loads(geocache_path.read_text(encoding="utf-8"))
            countries = sorted({e["city"] for e in geocache if "city" in e})
        except json.JSONDecodeError:
            pass
    if "Unknown" not in countries:
        countries.append("Unknown")

    for ym in sorted(ym_set):
        for country in countries:
            candidates.append(f"{ym} {country} META-AI")

    # De-duplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def full_scan(dest_root: Path, config: dict, workers: int = 32) -> dict:
    """Full scan of Z: — lists every tracked folder and stat()s every file.
    Uses a thread pool to parallelize stat calls across the network.
    Saves incrementally after each folder so the manifest is readable
    even while the build is still running."""
    out: dict[str, dict[str, int]] = {}
    # Preserve any existing manifest data (so we don't lose partial work)
    existing = load_manifest()
    if existing.get("folders"):
        out.update(existing["folders"])

    if not dest_root.exists():
        return out

    # Get candidate folder names from trackers + synced files — NO Z:\ listing.
    candidates = get_known_folders(config)
    print(f"  Checking {len(candidates)} candidate folders on destination...", flush=True)

    # Parallel is_dir() checks to find which candidates actually exist
    def _check(name: str) -> tuple[str, bool]:
        p = dest_root / name
        try:
            return (name, p.is_dir())
        except OSError:
            return (name, False)

    existing: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, is_dir in pool.map(_check, candidates):
            if is_dir:
                existing.append(name)

    tracked = [dest_root / name for name in sorted(existing)]
    print(f"  Found {len(tracked)} tracked folders", flush=True)

    for i, folder in enumerate(tracked, 1):
        files: dict[str, int] = {}
        print(f"  [{i}/{len(tracked)}] {folder.name} ...", end="", flush=True)
        # os.scandir returns DirEntry objects with cached size info from the
        # initial SMB directory query on Windows — no extra round trip per file.
        try:
            with os.scandir(str(folder)) as it:
                for entry in it:
                    try:
                        if entry.is_file():
                            files[entry.name] = entry.stat().st_size
                    except OSError:
                        continue
        except OSError as e:
            print(f" scandir error: {e}", flush=True)
            continue

        out[folder.name] = files
        print(f" {len(files)} files", flush=True)
        # Save after every folder so reports see live progress
        save_manifest({"folders": out})
    return out


def incremental_update(dest_root: Path, existing: dict, config: dict = None) -> tuple[dict, int]:
    """Go directly to each known folder (no Z:\\ root listing). For each,
    list its contents and only stat() files that aren't already in the
    manifest. Returns (updated_manifest, new_file_count)."""
    folders = dict(existing)  # copy
    new_total = 0

    if not dest_root.exists():
        return folders, 0

    if config is None:
        config = load_config()

    candidates = get_known_folders(config)

    # Also include any folders we already have in the manifest (in case the
    # user added some manually or they were created outside our heuristics)
    all_names = list(set(candidates) | set(existing.keys()))

    for name in sorted(all_names):
        folder = dest_root / name
        try:
            if not folder.is_dir():
                continue
            file_list = [f for f in folder.iterdir() if f.is_file()]
        except OSError:
            continue

        folder_data = folders.get(name, {})
        folder_new = 0
        for f in file_list:
            if f.name in folder_data:
                continue
            try:
                folder_data[f.name] = f.stat().st_size
                folder_new += 1
            except OSError:
                continue
        if folder_new or name not in folders:
            folders[name] = folder_data
        new_total += folder_new
        if folder_new:
            print(f"  {name}: +{folder_new} new files")

    return folders, new_total


def cmd_build():
    config = load_config()
    dest = Path(config["destination_dir"])
    print(f"Building manifest from destination — direct path lookup, no Z: root listing")
    data = {"folders": full_scan(dest, config)}
    save_manifest(data)
    total_files = sum(len(f) for f in data["folders"].values())
    total_size = sum(sum(f.values()) for f in data["folders"].values())
    print(f"\nManifest built: {len(data['folders'])} folders, "
          f"{total_files} files, {total_size / 1024**3:.2f} GB")
    print(f"Saved to: {MANIFEST_PATH}")


def cmd_update():
    config = load_config()
    dest = Path(config["destination_dir"])
    existing = load_manifest()
    if not existing["folders"]:
        print("No existing manifest — switching to full build")
        cmd_build()
        return
    print(f"Incremental update from {MANIFEST_PATH.name}...")
    updated, new = incremental_update(dest, existing["folders"])
    save_manifest({"folders": updated})
    total_files = sum(len(f) for f in updated.values())
    print(f"Manifest updated: +{new} new files (total now: {total_files})")


def cmd_show():
    m = load_manifest()
    if not m["folders"]:
        print("No manifest yet. Run: python manifest.py --build")
        return
    print(f"Manifest generated: {m.get('generated', '?')}")
    total_files = sum(len(f) for f in m["folders"].values())
    total_size = sum(sum(f.values()) for f in m["folders"].values())
    print(f"Folders: {len(m['folders'])}")
    print(f"Files:   {total_files}")
    print(f"Size:    {total_size / 1024**3:.2f} GB")
    print()
    for name in sorted(m["folders"].keys()):
        files = m["folders"][name]
        sz = sum(files.values())
        print(f"  {name}: {len(files)} files, {sz / 1024**2:.1f} MB")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Destination manifest cache")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--build", action="store_true", help="Full scan of Z: (slow)")
    g.add_argument("--update", action="store_true", help="Incremental scan (fast)")
    g.add_argument("--show", action="store_true", help="Show manifest stats")
    args = parser.parse_args()

    if args.build:
        cmd_build()
    elif args.update:
        cmd_update()
    elif args.show:
        cmd_show()


if __name__ == "__main__":
    main()
