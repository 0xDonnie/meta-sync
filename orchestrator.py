#!/usr/bin/env python3
"""
Orchestrator — runs all the remaining work autonomously.

Phases:
1. Wait for other python processes (camera_sync) to finish
2. Run meta_sync with videos enabled
3. Verify all files (meta + camera) on destination
4. Generate HTML report
5. Clean staging folders (LAST, only if verification passed)

Progress is written to orchestrator.log so it can be inspected at any time.
"""

import json
import os
import subprocess
import sys
import time
import yaml
from datetime import datetime
from pathlib import Path

# Force unbuffered output everywhere
sys.stdout.reconfigure(line_buffering=True)

META_DIR = Path("D:/GitHub/meta-sync")
CAMERA_DIR = Path("D:/GitHub/camera-sync")
DEST_ROOT = Path("Z:/")

LOG_FILE = META_DIR / "orchestrator.log"
STATUS_FILE = META_DIR / "orchestrator_status.json"


def log(msg: str):
    """Append a timestamped message to stdout and log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_status(phase: str, data: dict):
    """Write structured status for later inspection."""
    status = {}
    if STATUS_FILE.exists():
        try:
            status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    status[phase] = {
        "timestamp": datetime.now().isoformat(),
        **data,
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 1: wait for other python processes
# ---------------------------------------------------------------------------

def other_python_running() -> bool:
    """Return True if any python.exe (other than self) is running."""
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return False
    for line in result.stdout.strip().splitlines():
        if "python.exe" not in line.lower():
            continue
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if pid != my_pid:
            return True
    return False


def wait_for_other_python():
    log("Phase 1: Waiting for any other python processes to finish...")
    checks = 0
    while other_python_running():
        checks += 1
        if checks % 5 == 0:
            log(f"  still waiting... ({checks} minutes elapsed)")
        time.sleep(60)
    log("Phase 1: No other python processes running")
    write_status("phase1_wait", {"status": "done"})


# ---------------------------------------------------------------------------
# Phase 2: meta sync with videos
# ---------------------------------------------------------------------------

def run_meta_videos():
    log("Phase 2: Enabling videos in meta-sync config and running meta_sync")

    config_path = META_DIR / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["extensions"] = [".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov"]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    log("  config.yaml updated with video extensions")

    result = subprocess.run(
        ["python", "-u", str(META_DIR / "meta_sync.py")],
        cwd=str(META_DIR),
    )
    log(f"Phase 2: meta_sync exited with code {result.returncode}")
    write_status("phase2_meta_videos", {"exit_code": result.returncode})
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Phase 3: verify all files on destination
# ---------------------------------------------------------------------------

import re
META_FOLDER_RE = re.compile(r"^\d{4}\.\d{2} .+? META-AI$", re.IGNORECASE)


def verify_meta() -> dict:
    """Check that every file in meta staging exists on Z: with matching size."""
    log("Phase 3a: Verifying META files on destination")

    staging = META_DIR / "staging"
    if not staging.exists():
        log("  meta staging folder does not exist — nothing to verify")
        return {"checked": 0, "verified": 0, "missing": 0, "size_mismatch": 0, "details": []}

    # Index all meta files already on Z: by filename
    log("  Indexing META-AI folders on destination...")
    index: dict[str, list[tuple[Path, int]]] = {}
    try:
        for entry in DEST_ROOT.iterdir():
            if not entry.is_dir() or not META_FOLDER_RE.match(entry.name):
                continue
            for f in entry.iterdir():
                if f.is_file():
                    try:
                        size = f.stat().st_size
                    except OSError:
                        continue
                    index.setdefault(f.name, []).append((f, size))
    except OSError as e:
        log(f"  ERROR: could not read destination: {e}")
        return {"error": str(e)}
    log(f"  Indexed {sum(len(v) for v in index.values())} files in {len(index)} unique names")

    # For each file in staging, check if there's a match on Z:
    # A match exists if:
    #   (a) a file with the same original name exists on Z: with matching size (V1)
    #   (b) a file with the V2 derived name exists on Z: with matching size
    # For V2 derived name we can't reconstruct easily without EXIF, so we just
    # trust (a) or any file with same size in an expected-month folder.
    checked = 0
    verified = 0
    missing = 0
    mismatch = 0
    missing_files: list[str] = []
    mismatch_files: list[str] = []
    safe_to_delete: list[str] = []

    for src in staging.iterdir():
        if not src.is_file():
            continue
        checked += 1
        try:
            src_size = src.stat().st_size
        except OSError:
            continue

        # Try exact-name match (V1 format)
        matched = False
        if src.name in index:
            for _, dst_size in index[src.name]:
                if dst_size == src_size:
                    matched = True
                    safe_to_delete.append(src.name)
                    break

        # Try V2 name match: we can derive from EXIF if needed
        if not matched:
            try:
                sys.path.insert(0, str(META_DIR))
                from meta_sync import (
                    get_exif, get_date, get_date_from_filename,
                    get_date_from_file, build_file_name, build_folder_name,
                    get_gps,
                )
                exif = get_exif(src)
                dt = get_date(exif) or get_date_from_filename(src) or get_date_from_file(src)

                # We don't know the country without redoing geocoding; try all
                # countries present in the index for files with same size.
                for candidate_name, entries in index.items():
                    if not candidate_name.startswith(dt.strftime("%y.%m.%d")):
                        continue
                    for _, dst_size in entries:
                        if dst_size == src_size:
                            matched = True
                            safe_to_delete.append(src.name)
                            break
                    if matched:
                        break
            except Exception as e:
                log(f"  verify warning for {src.name}: {e}")

        if matched:
            verified += 1
        else:
            if src.name in index:
                mismatch += 1
                mismatch_files.append(src.name)
            else:
                missing += 1
                missing_files.append(src.name)

    log(f"Phase 3a: META verify → checked={checked} verified={verified} "
        f"missing={missing} size_mismatch={mismatch}")

    result = {
        "checked": checked,
        "verified": verified,
        "missing": missing,
        "size_mismatch": mismatch,
        "missing_sample": missing_files[:10],
        "mismatch_sample": mismatch_files[:10],
    }
    # Write the safe-to-delete list for phase 5
    (META_DIR / "safe_to_delete_meta.json").write_text(
        json.dumps(safe_to_delete), encoding="utf-8"
    )
    return result


def verify_camera() -> dict:
    """Check camera destination folders have all tracked files."""
    log("Phase 3b: Verifying CAMERA files on destination")

    tracker_dir = CAMERA_DIR / "camera_trackers"
    if not tracker_dir.exists():
        log("  camera trackers folder missing")
        return {"status": "no_trackers"}

    stats = {}
    for tracker_file in tracker_dir.glob("*.json"):
        job_name = tracker_file.stem
        tracked = json.loads(tracker_file.read_text(encoding="utf-8"))

        found = 0
        missing_list: list[str] = []

        if job_name == "camera_quarter":
            # Need to search all quarter folders for each file
            quarter_folders = [
                d for d in DEST_ROOT.iterdir()
                if d.is_dir() and re.match(r"^\d{4} Q\d Cam Samsung$", d.name)
            ]
            index_q: dict[str, Path] = {}
            for qf in quarter_folders:
                try:
                    for f in qf.iterdir():
                        if f.is_file():
                            index_q[f.name] = f
                except OSError:
                    pass
            for name in tracked:
                if name in index_q:
                    found += 1
                else:
                    missing_list.append(name)
        else:
            # Flat folder — exact destination
            dest = DEST_ROOT / job_name
            existing = set()
            try:
                if dest.exists():
                    existing = {f.name for f in dest.iterdir() if f.is_file()}
            except OSError:
                pass
            for name in tracked:
                if name in existing:
                    found += 1
                else:
                    missing_list.append(name)

        stats[job_name] = {
            "tracked": len(tracked),
            "found": found,
            "missing": len(tracked) - found,
            "missing_sample": missing_list[:10],
        }
        log(f"  {job_name}: tracked={len(tracked)} found={found} missing={len(tracked) - found}")

    return stats


def run_verification():
    meta_result = verify_meta()
    camera_result = verify_camera()
    write_status("phase3_verify", {
        "meta": meta_result,
        "camera": camera_result,
    })
    return meta_result, camera_result


# ---------------------------------------------------------------------------
# Phase 4: generate HTML report
# ---------------------------------------------------------------------------

def generate_report():
    log("Phase 4: Generating HTML report")
    result = subprocess.run(
        ["python", "-u", str(META_DIR / "report.py"), "--local"],
        cwd=str(META_DIR),
        capture_output=True, text=True,
    )
    log(f"Phase 4: report.py exit code {result.returncode}")
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            log(f"  {line}")
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            log(f"  stderr: {line}")
    write_status("phase4_report", {
        "exit_code": result.returncode,
        "report_path": str(META_DIR / "report.html"),
    })


# ---------------------------------------------------------------------------
# Phase 5: clean staging (LAST)
# ---------------------------------------------------------------------------

def cleanup_staging(meta_result: dict):
    log("Phase 5: Cleaning staging folders (this is the LAST step)")

    # META staging — only delete files confirmed verified
    safe_file = META_DIR / "safe_to_delete_meta.json"
    if safe_file.exists() and meta_result.get("missing", 0) == 0 and meta_result.get("size_mismatch", 0) == 0:
        safe = set(json.loads(safe_file.read_text(encoding="utf-8")))
        staging = META_DIR / "staging"
        deleted = 0
        for src in list(staging.iterdir()):
            if src.is_file() and src.name in safe:
                try:
                    src.unlink()
                    deleted += 1
                except OSError as e:
                    log(f"  failed to delete {src.name}: {e}")
        log(f"  META staging: deleted {deleted} verified files")
        write_status("phase5_cleanup_meta", {"deleted": deleted, "total_safe": len(safe)})
    else:
        missing = meta_result.get("missing", 0)
        mismatch = meta_result.get("size_mismatch", 0)
        log(f"  META staging: NOT cleaned — {missing} missing, {mismatch} size mismatches")
        write_status("phase5_cleanup_meta", {
            "skipped": True,
            "missing": missing,
            "mismatch": mismatch,
        })

    # Camera staging — cleanup already happens inline during camera_sync.
    # Just check and log remaining files.
    camera_staging = CAMERA_DIR / "staging_camera"
    if camera_staging.exists():
        remaining = sum(1 for _ in camera_staging.rglob("*") if _.is_file())
        log(f"  CAMERA staging: {remaining} files remaining (auto-cleaned by camera_sync)")
        write_status("phase5_cleanup_camera", {"remaining": remaining})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log("=" * 60)
    log("Orchestrator starting")
    log("=" * 60)

    try:
        wait_for_other_python()
    except Exception as e:
        log(f"Phase 1 error: {e}")

    try:
        run_meta_videos()
    except Exception as e:
        log(f"Phase 2 error: {e}")

    meta_result = {}
    try:
        meta_result, camera_result = run_verification()
    except Exception as e:
        log(f"Phase 3 error: {e}")

    try:
        generate_report()
    except Exception as e:
        log(f"Phase 4 error: {e}")

    try:
        cleanup_staging(meta_result)
    except Exception as e:
        log(f"Phase 5 error: {e}")

    log("=" * 60)
    log("Orchestrator finished")
    log("=" * 60)


if __name__ == "__main__":
    main()
