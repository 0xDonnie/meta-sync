#!/usr/bin/env python3
"""
MTP Pull — pulls new files from the phone via Windows MTP (no ADB, no USB
debugging). Works on Windows only; uses the Shell.Application COM object to
navigate the phone as an MTP device and copy files to a local staging folder.

After it finishes, meta_sync.py / camera_sync.py --offline organize the
pulled files into the destination.

Usage:
    python mtp_pull.py --job meta              # Ray-Ban Meta photos
    python mtp_pull.py --job camera-flat       # DCIM/<year> (archive)
    python mtp_pull.py --job camera-quarter    # DCIM/Camera (current year)
    python mtp_pull.py --job all               # all three

Config keys (in config.yaml):
    mtp_device_name:          "Fold7"       # substring match, case-insensitive
    mtp_meta_path:            "Internal storage/Download/Meta AI"
    mtp_camera_flat_path:     "Internal storage/DCIM/2025"
    mtp_camera_quarter_path:  "Internal storage/DCIM/Camera"
    mtp_camera_flat_staging:  "D:/GitHub/camera-sync/staging_camera/DCIM_2025"
    mtp_camera_quarter_staging: "D:/GitHub/camera-sync/staging_camera/DCIM_Camera"
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

try:
    import pythoncom
    import win32com.client
except ImportError:
    print("ERROR: pywin32 is required for MTP transport.")
    print("Install it with: pip install pywin32")
    sys.exit(1)


# --------------------------------------------------------------------------
# Windows Shell constants
# --------------------------------------------------------------------------

SSFDRIVES = 17  # "This PC"
# CopyHere flags (SHFileOperation FOF_*)
FOF_SILENT = 0x4
FOF_NOCONFIRMATION = 0x10
FOF_NOERRORUI = 0x400
FOF_NOCONFIRMMKDIR = 0x200
COPY_FLAGS = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: str | None = None) -> dict:
    cfg = Path(path or (Path(__file__).parent / "config.yaml"))
    if not cfg.exists():
        print(f"config.yaml not found at {cfg}")
        sys.exit(1)
    return yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------
# MTP navigation via Shell COM
# --------------------------------------------------------------------------

def find_device(shell, device_name: str):
    """Find an MTP device under 'This PC' by substring match (case-insensitive).
    Returns a Shell Folder object or None."""
    my_pc = shell.Namespace(SSFDRIVES)
    if my_pc is None:
        return None
    for item in my_pc.Items():
        if device_name.lower() in item.Name.lower():
            return item.GetFolder
    return None


def list_devices(shell) -> list[str]:
    """List all items under 'This PC' (devices, drives)."""
    my_pc = shell.Namespace(SSFDRIVES)
    if my_pc is None:
        return []
    return [item.Name for item in my_pc.Items()]


def navigate_path(root_folder, path_parts: list[str]):
    """Walk into nested folders by name. Returns a Folder object or None."""
    cur = root_folder
    for part in path_parts:
        found = None
        for it in cur.Items():
            if it.Name == part:
                found = it
                break
        if found is None:
            return None
        cur = found.GetFolder
    return cur


def list_files(folder) -> list:
    """Return a list of FolderItem objects for non-folder items."""
    out = []
    for item in folder.Items():
        if not bool(item.IsFolder):
            out.append(item)
    return out


def copy_one(item, local_dir: Path, timeout_sec: int = 300) -> bool:
    """Copy an MTP FolderItem to local_dir. Waits until the target file
    appears with stable size (CopyHere is asynchronous)."""
    local_dir.mkdir(parents=True, exist_ok=True)
    target = local_dir / item.Name
    if target.exists() and target.stat().st_size > 0:
        return True

    shell = win32com.client.Dispatch("Shell.Application")
    dest_ns = shell.Namespace(str(local_dir))
    if dest_ns is None:
        print(f"    could not open destination namespace {local_dir}")
        return False

    try:
        dest_ns.CopyHere(item, COPY_FLAGS)
    except Exception as e:
        print(f"    CopyHere failed: {e}")
        return False

    # Wait for the target to appear AND for its size to be stable
    start = time.time()
    last_size = -1
    stable = 0
    while time.time() - start < timeout_sec:
        try:
            if target.exists():
                size = target.stat().st_size
                if size > 0 and size == last_size:
                    stable += 1
                    if stable >= 2:
                        return True
                else:
                    stable = 0
                last_size = size
        except OSError:
            pass
        time.sleep(0.5)
    # Timed out but file might still be valid
    return target.exists() and target.stat().st_size > 0


# --------------------------------------------------------------------------
# Job runner
# --------------------------------------------------------------------------

def _pull(folder, names_to_pull: list[str], items_by_name: dict, local_dir: Path,
          tracker_path: Path | None = None) -> tuple[int, int]:
    copied = 0
    errors = 0
    tracker: set[str] = set()
    if tracker_path and tracker_path.exists():
        try:
            tracker = set(json.loads(tracker_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            tracker = set()

    total = len(names_to_pull)
    for i, name in enumerate(names_to_pull, 1):
        item = items_by_name[name]
        print(f"  [{i}/{total}] {name}", flush=True)
        if copy_one(item, local_dir):
            copied += 1
            tracker.add(name)
            if tracker_path:
                tracker_path.parent.mkdir(parents=True, exist_ok=True)
                tracker_path.write_text(json.dumps(sorted(tracker)), encoding="utf-8")
        else:
            errors += 1
    return copied, errors


def run_job(config: dict, phone_path: str, local_dir: Path,
            tracker_path: Path | None, label: str):
    device_name = config.get("mtp_device_name")
    if not device_name:
        print("ERROR: mtp_device_name missing in config.yaml")
        return

    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        device = find_device(shell, device_name)
        if device is None:
            print(f"ERROR: phone '{device_name}' not found under This PC.")
            print("       Make sure it is connected via USB in File Transfer mode.")
            print(f"       Devices currently visible: {list_devices(shell)}")
            return

        print(f"[{label}] device: {device.Title}")
        parts = [p for p in phone_path.split("/") if p]
        folder = navigate_path(device, parts)
        if folder is None:
            print(f"ERROR: could not navigate to '{phone_path}' on device.")
            return

        files = list_files(folder)
        items_by_name = {f.Name: f for f in files}
        print(f"[{label}] {len(files)} files on phone at {phone_path}")

        # Decide which ones to pull:
        # - skip if already present in local_dir with size > 0
        # - skip if already in tracker
        tracker: set[str] = set()
        if tracker_path and tracker_path.exists():
            try:
                tracker = set(json.loads(tracker_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass

        local_dir.mkdir(parents=True, exist_ok=True)
        to_pull: list[str] = []
        for name in sorted(items_by_name.keys()):
            if name in tracker:
                continue
            local_file = local_dir / name
            if local_file.exists() and local_file.stat().st_size > 0:
                tracker.add(name)
                continue
            to_pull.append(name)

        print(f"[{label}] already pulled: {len(files) - len(to_pull)}")
        print(f"[{label}] new to pull:    {len(to_pull)}")

        if tracker_path:
            tracker_path.parent.mkdir(parents=True, exist_ok=True)
            tracker_path.write_text(json.dumps(sorted(tracker)), encoding="utf-8")

        if not to_pull:
            return

        copied, errors = _pull(folder, to_pull, items_by_name, local_dir, tracker_path)
        print(f"[{label}] done: {copied} copied, {errors} errors")

    finally:
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

META_DIR = Path(__file__).parent
CAMERA_DIR = Path("D:/GitHub/camera-sync")


def main():
    parser = argparse.ArgumentParser(description="Pull phone files via MTP (no ADB)")
    parser.add_argument("--job", choices=["meta", "camera-flat", "camera-quarter", "all"],
                        default="all")
    parser.add_argument("-c", "--config", default=None, help="Path to config.yaml")
    parser.add_argument("--then-organize", action="store_true",
                        help="After pulling, run the organizer (meta_sync / camera_sync --offline)")
    args = parser.parse_args()

    config = load_config(args.config)

    jobs = []
    if args.job in ("meta", "all"):
        jobs.append(("meta",
                     config.get("mtp_meta_path", "Internal storage/Download/Meta AI"),
                     Path(config.get("source_dir", META_DIR / "staging")),
                     META_DIR / "synced_files.json"))
    if args.job in ("camera-flat", "all"):
        jobs.append(("camera-flat",
                     config.get("mtp_camera_flat_path", "Internal storage/DCIM/2025"),
                     Path(config.get("mtp_camera_flat_staging",
                                     CAMERA_DIR / "staging_camera/DCIM_2025")),
                     None))
    if args.job in ("camera-quarter", "all"):
        jobs.append(("camera-quarter",
                     config.get("mtp_camera_quarter_path", "Internal storage/DCIM/Camera"),
                     Path(config.get("mtp_camera_quarter_staging",
                                     CAMERA_DIR / "staging_camera/DCIM_Camera")),
                     None))

    for label, phone_path, local_dir, tracker_path in jobs:
        print(f"\n===== {label.upper()} =====")
        run_job(config, phone_path, local_dir, tracker_path, label)

    if args.then_organize:
        print("\n===== ORGANIZE =====")
        NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if args.job in ("meta", "all"):
            print("Running meta_sync.py ...")
            subprocess.run(
                [sys.executable, "-u", str(META_DIR / "meta_sync.py")],
                cwd=str(META_DIR),
            )
        if args.job in ("camera-flat", "camera-quarter", "all"):
            print("Running camera_sync.py --offline ...")
            subprocess.run(
                [sys.executable, "-u", str(CAMERA_DIR / "camera_sync.py"), "--offline"],
                cwd=str(CAMERA_DIR),
            )


if __name__ == "__main__":
    main()
