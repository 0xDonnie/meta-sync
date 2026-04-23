#!/usr/bin/env python3
"""
Meta Sync Watcher — Detects phone via ADB, checks for new photos,
pulls only new ones, and organizes them on the destination drive.

Usage:
    python watcher.py              # start watching for phone connection
    python watcher.py --once       # run once and exit
    python watcher.py --status     # show what's on phone vs already synced

SAFETY: ONLY copies files. NEVER deletes anything from phone or destination.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from meta_sync import (
    load_config,
    setup_logging,
    find_photos,
    process_file,
    get_exif,
    get_date,
    get_date_from_filename,
    get_date_from_file,
    get_gps,
    reverse_geocode,
    GeoCache,
    ProcessedTracker,
)
import logging


SYNCED_FILE = "synced_files.json"


def load_synced(config: dict) -> set[str]:
    """Load set of filenames already pulled from phone."""
    path = Path(config.get("synced_file", SYNCED_FILE))
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_synced(config: dict, synced: set[str]):
    """Save set of filenames already pulled from phone."""
    path = Path(config.get("synced_file", SYNCED_FILE))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(synced), f)


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

# Avoid a flashing console window for every ADB call when we're running
# under pythonw (no parent console).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def adb_available() -> bool:
    """Check if a phone is connected via ADB."""
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True, text=True, timeout=10,
            creationflags=NO_WINDOW,
        )
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def adb_list_files(phone_path: str) -> list[str]:
    """List files on phone at given path."""
    try:
        env = os.environ.copy()
        env["MSYS_NO_PATHCONV"] = "1"
        result = subprocess.run(
            ["adb", "shell", f"ls '{phone_path}'"],
            capture_output=True, text=True, timeout=15, env=env,
            creationflags=NO_WINDOW,
        )
        if result.returncode != 0:
            return []
        return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def adb_pull_file(phone_path: str, filename: str, local_dir: Path) -> bool:
    """Pull a single file from phone. Returns True if successful."""
    remote = f"{phone_path}/{filename}"
    local = local_dir / filename
    try:
        env = os.environ.copy()
        env["MSYS_NO_PATHCONV"] = "1"
        result = subprocess.run(
            ["adb", "pull", remote, str(local)],
            capture_output=True, text=True, timeout=120, env=env,
            creationflags=NO_WINDOW,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def check_status(config: dict, logger: logging.Logger) -> tuple[list[str], list[str]]:
    """Compare phone files vs already synced. Returns (new_files, already_synced)."""
    phone_path = config.get("phone_path", "/sdcard/Download/Meta AI")

    logger.info(f"Scanning phone: {phone_path}")
    phone_files = adb_list_files(phone_path)
    logger.info(f"  Files on phone: {len(phone_files)}")

    synced = load_synced(config)
    logger.info(f"  Already synced: {len(synced)}")

    new_files = [f for f in phone_files if f not in synced]
    already = [f for f in phone_files if f in synced]

    logger.info(f"  New to download: {len(new_files)}")

    return new_files, already


def pull_and_organize(config: dict, logger: logging.Logger):
    """Full sync: check → pull new → organize → update tracker."""
    phone_path = config.get("phone_path", "/sdcard/Download/Meta AI")
    staging_dir = Path(config.get("staging_dir", "staging"))
    dest_dir = Path(config["destination_dir"])
    fallback_city = config.get("fallback_city", "Unknown")
    extensions = config.get("extensions", [".jpg", ".jpeg", ".png"])
    cache_file = config.get("cache_file", "geocache.json")
    cluster_radius = config.get("cluster_radius_km", 15)
    tracker_file = config.get("tracker_file", "processed.json")

    staging_dir.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Check what's new
    new_files, already = check_status(config, logger)

    if not new_files:
        logger.info("Everything is already synced!")
        return

    # Step 2: Pull only new files
    logger.info(f"Downloading {len(new_files)} new files from phone...")
    synced = load_synced(config)
    pulled = []
    for i, filename in enumerate(new_files, 1):
        logger.info(f"  [{i}/{len(new_files)}] {filename}")
        if adb_pull_file(phone_path, filename, staging_dir):
            pulled.append(filename)
            synced.add(filename)
        else:
            logger.warning(f"  Failed to pull: {filename}")

    # Save synced list immediately after pulling
    save_synced(config, synced)
    logger.info(f"Downloaded {len(pulled)} files")

    # Step 3: Build day→city map from photos with GPS
    geocache = GeoCache(cache_file, cluster_radius)
    tracker = ProcessedTracker(tracker_file)

    pulled_paths = [staging_dir / f for f in pulled if (staging_dir / f).exists()]
    ext_set = {e.lower() for e in extensions}
    pulled_paths = [p for p in pulled_paths if p.suffix.lower() in ext_set]

    logger.info("Scanning GPS data...")
    day_city_map: dict[str, str] = {}
    for photo in pulled_paths:
        exif = get_exif(photo)
        gps = get_gps(exif)
        if gps:
            lat, lon = gps
            city = geocache.lookup(lat, lon)
            if not city:
                city = reverse_geocode(lat, lon)
                if city:
                    geocache.store(lat, lon, city)
            if city:
                dt = get_date(exif) or get_date_from_filename(photo) or get_date_from_file(photo)
                day_key = dt.strftime("%Y-%m-%d")
                day_city_map[day_key] = city
    logger.info(f"Found cities for {len(day_city_map)} days")

    # Step 4: Organize into destination
    copied = 0
    errors = 0
    for photo in pulled_paths:
        try:
            if process_file(photo, dest_dir, fallback_city, geocache, tracker, day_city_map):
                copied += 1
        except Exception as e:
            logger.error(f"Error processing {photo.name}: {e}")
            errors += 1

    logger.info(f"Done! {copied} organized on {dest_dir}, {errors} errors.")


def watch(config: dict, poll_interval: int = 5):
    """Watch for phone connection and sync when detected."""
    logger = logging.getLogger("meta_sync")

    print("=" * 50)
    print("  Meta Sync Watcher")
    print("  Waiting for phone connection...")
    print("  (connect your phone via USB)")
    print("=" * 50)

    was_connected = False

    while True:
        is_connected = adb_available()

        if is_connected and not was_connected:
            print()
            logger.info("Phone connected!")
            pull_and_organize(config, logger)
            print()
            print("Sync complete. You can disconnect your phone.")
            print("Waiting for next connection...")
            print()

        elif not is_connected and was_connected:
            logger.info("Phone disconnected.")

        was_connected = is_connected
        time.sleep(poll_interval)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Watch for phone and sync Ray-Ban Meta photos")
    parser.add_argument("-c", "--config", help="Path to config.yaml", default=None)
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--status", action="store_true", help="Show sync status and exit")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_file"))
    logger = logging.getLogger("meta_sync")

    if not adb_available():
        logger.error("No phone connected. Connect via USB and try again.")
        if not args.once and not args.status:
            print("\nStarting watcher mode — will sync when phone is connected.\n")
            watch(config)
        else:
            sys.exit(1)
        return

    if args.status:
        check_status(config, logger)
    elif args.once:
        pull_and_organize(config, logger)
    else:
        watch(config)


if __name__ == "__main__":
    main()
