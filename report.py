#!/usr/bin/env python3
"""
Meta Sync Report — Generates a static HTML report of processed Meta AI photos.

Usage:
    python report.py --local              # uses staging + EXIF + geocache (no Z: access)
    python report.py                       # scans Z: destination (slower)
    python report.py -o out.html           # custom output path

--local mode reads local staging files, derives their destination folder
via EXIF date + country geocache, and builds the report without touching Z:.
Use --local while other processes are writing to Z: to avoid contention.
"""

import html
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))


FOLDER_PATTERN = re.compile(r"^(\d{4})\.(\d{2}) (.+?) META-AI$", re.IGNORECASE)


def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        print(f"Error: {path} not found")
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Remote mode: scan Z:
# ---------------------------------------------------------------------------

def scan_destination(dest: Path) -> list[dict]:
    """Find all META-AI folders and collect their contents."""
    folders = []
    if not dest.exists():
        return folders
    for entry in sorted(dest.iterdir()):
        if not entry.is_dir():
            continue
        m = FOLDER_PATTERN.match(entry.name)
        if not m:
            continue
        year, month, country = m.group(1), m.group(2), m.group(3)
        files = []
        total_size = 0
        for f in sorted(entry.iterdir()):
            if f.is_file():
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                files.append({"name": f.name, "size": size})
                total_size += size
        folders.append({
            "folder": entry.name,
            "year": year,
            "month": month,
            "country": country,
            "count": len(files),
            "size": total_size,
            "files": files,
        })
    return folders


# ---------------------------------------------------------------------------
# Local mode: derive destination from staging + EXIF + geocache
# ---------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def lookup_country(lat: float, lon: float, geocache: list[dict], radius_km: float = 15) -> str | None:
    for entry in geocache:
        if haversine(lat, lon, entry["lat"], entry["lon"]) <= radius_km:
            return entry["city"]
    return None


def scan_synced_via_destination(config: dict) -> list[dict]:
    """Fallback when staging has been cleaned: list Z: META-AI folder NAMES
    (not contents — fast over WiFi) and count files from synced_files.json
    grouped by the month in each filename."""
    synced_path = Path(config.get("synced_file", "synced_files.json"))
    if not synced_path.exists():
        return []
    try:
        synced_names = json.loads(synced_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    dest_root = Path(config["destination_dir"])
    if not dest_root.exists():
        return []

    # List only folder names (no per-file stat — fast)
    folder_by_ym: dict[str, list[str]] = defaultdict(list)
    try:
        for entry in dest_root.iterdir():
            if not entry.is_dir():
                continue
            m = FOLDER_PATTERN.match(entry.name)
            if m:
                ym = f"{m.group(1)}.{m.group(2)}"
                folder_by_ym[ym].append(entry.name)
    except OSError:
        return []

    # Group filenames by year.month parsed from filename
    files_by_ym: dict[str, list[str]] = defaultdict(list)
    for name in synced_names:
        stem = Path(name).stem
        for part in stem.replace("-", "_").split("_"):
            if len(part) == 8 and part.isdigit():
                try:
                    dt = datetime.strptime(part, "%Y%m%d")
                    files_by_ym[dt.strftime("%Y.%m")].append(name)
                    break
                except ValueError:
                    continue

    folders = []
    for ym, folder_names in sorted(folder_by_ym.items()):
        files_this_ym = sorted(files_by_ym.get(ym, []))
        # If there are multiple country folders for this month, just list them
        # without per-file breakdown (since we can't know which file is in which).
        for fname in sorted(folder_names):
            m = FOLDER_PATTERN.match(fname)
            if not m:
                continue
            country = m.group(3)
            folders.append({
                "folder": fname,
                "year": m.group(1),
                "month": m.group(2),
                "country": country,
                "count": len(files_this_ym) if len(folder_names) == 1 else 0,
                "size": 0,
                "files": [{"name": n, "size": 0} for n in files_this_ym]
                         if len(folder_names) == 1 else [],
            })
    return folders


def scan_local(config: dict) -> list[dict]:
    """Derive the organized folder structure from local staging files.

    If staging has been cleaned up, falls back to scanning Z: folder names +
    synced_files.json for the META listing.
    """
    from meta_sync import (
        get_exif, get_date, get_date_from_filename, get_date_from_file, get_gps
    )

    staging = Path(config["source_dir"])
    if not staging.exists() or not any(staging.iterdir()):
        print(f"  staging empty or missing → falling back to destination scan")
        return scan_synced_via_destination(config)

    if not staging.exists():
        print(f"  staging folder does not exist: {staging}")
        return []

    cache_path = Path(config.get("cache_file", "geocache.json"))
    geocache = []
    if cache_path.exists():
        try:
            geocache = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    cluster_radius = config.get("cluster_radius_km", 15)
    fallback_city = config.get("fallback_city", "Unknown")

    print(f"  scanning staging: {staging}")
    files = [f for f in staging.iterdir() if f.is_file()]
    print(f"  {len(files)} files to analyze")

    # First pass: build a day → country map from files with GPS
    day_country: dict[str, str] = {}
    per_file: list[dict] = []

    for f in files:
        exif = get_exif(f)
        dt = get_date(exif) or get_date_from_filename(f) or get_date_from_file(f)
        gps = get_gps(exif)
        country = None
        if gps:
            country = lookup_country(gps[0], gps[1], geocache, cluster_radius)
        if country:
            day_country[dt.strftime("%Y-%m-%d")] = country
        per_file.append({
            "path": f,
            "name": f.name,
            "dt": dt,
            "country": country,
            "gps": gps is not None,
            "size": f.stat().st_size,
        })

    # Second pass: fill missing countries from day map
    for item in per_file:
        if not item["country"]:
            item["country"] = day_country.get(item["dt"].strftime("%Y-%m-%d"), fallback_city)

    # Group by (year.month, country)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in per_file:
        year = item["dt"].strftime("%Y")
        month = item["dt"].strftime("%m")
        key = (f"{year}.{month}", item["country"])
        groups[key].append({"name": item["name"], "size": item["size"]})

    folders = []
    for (ym, country), items in sorted(groups.items()):
        year, month = ym.split(".")
        folder_name = f"{ym} {country} META-AI"
        total_size = sum(i["size"] for i in items)
        folders.append({
            "folder": folder_name,
            "year": year,
            "month": month,
            "country": country,
            "count": len(items),
            "size": total_size,
            "files": sorted(items, key=lambda x: x["name"]),
        })
    return folders


# ---------------------------------------------------------------------------
# Camera local scan — uses camera_trackers + staging for sizes
# ---------------------------------------------------------------------------

CAMERA_TRACKER_DIR = Path("D:/GitHub/camera-sync/camera_trackers")
CAMERA_STAGING_DIR = Path("D:/GitHub/camera-sync/staging_camera")


def _quarter_of_date(year: int, month: int) -> int:
    return (month - 1) // 3 + 1


def _parse_date_from_camera_name(name: str) -> datetime | None:
    stem = Path(name).stem
    for part in stem.replace("-", "_").split("_"):
        if len(part) == 8 and part.isdigit():
            try:
                return datetime.strptime(part, "%Y%m%d")
            except ValueError:
                continue
    return None


def _camera_file_size(filename: str, staging_roots: list[Path]) -> int:
    """Look up a camera filename in any staging root. Returns 0 if not found."""
    for root in staging_roots:
        p = root / filename
        if p.exists() and p.is_file():
            try:
                return p.stat().st_size
            except OSError:
                pass
    return 0


def scan_camera_local() -> list[dict]:
    """Derive the camera folder structure from trackers + any staging files.

    Flat tracker files (named after the destination folder) represent a
    flat sync like "2025 CAM Samsung" — all files live in one folder.

    The quarter tracker splits files into Qn folders based on the
    date parsed from the filename.
    """
    if not CAMERA_TRACKER_DIR.exists():
        return []

    # Collect staging roots to look up file sizes
    staging_roots: list[Path] = []
    if CAMERA_STAGING_DIR.exists():
        staging_roots.append(CAMERA_STAGING_DIR)
        for sub in CAMERA_STAGING_DIR.iterdir():
            if sub.is_dir():
                staging_roots.append(sub)

    folders: list[dict] = []

    for tracker_file in sorted(CAMERA_TRACKER_DIR.glob("*.json")):
        try:
            names = json.loads(tracker_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        if tracker_file.stem == "camera_quarter":
            # Group by (year, quarter)
            groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
            for name in names:
                dt = _parse_date_from_camera_name(name)
                if not dt:
                    continue
                q = _quarter_of_date(dt.year, dt.month)
                size = _camera_file_size(name, staging_roots)
                groups[(dt.year, q)].append({"name": name, "size": size})
            for (year, q), items in sorted(groups.items()):
                total_size = sum(i["size"] for i in items)
                folders.append({
                    "folder": f"{year} Q{q} Cam Samsung",
                    "year": str(year),
                    "month": f"Q{q}",
                    "country": "Camera",
                    "count": len(items),
                    "size": total_size,
                    "files": sorted(items, key=lambda x: x["name"]),
                })
        else:
            # Flat folder — tracker name = destination folder
            flat_folder = tracker_file.stem  # e.g. "2025 CAM Samsung"
            items = []
            year = "?"
            for name in names:
                size = _camera_file_size(name, staging_roots)
                items.append({"name": name, "size": size})
                if year == "?":
                    dt = _parse_date_from_camera_name(name)
                    if dt:
                        year = str(dt.year)
            total_size = sum(i["size"] for i in items)
            folders.append({
                "folder": flat_folder,
                "year": year,
                "month": "flat",
                "country": "Camera",
                "count": len(items),
                "size": total_size,
                "files": sorted(items, key=lambda x: x["name"]),
            })

    return folders


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #0b0b0f;
  --panel: #141420;
  --border: #23233a;
  --text: #e7e7f0;
  --muted: #8a8aa0;
  --accent: #5b9dff;
  --accent-2: #8b6dff;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg); color: var(--text);
  margin: 0; padding: 2rem; line-height: 1.5;
}
h1 { font-size: 1.8rem; margin: 0 0 0.25rem; }
h2 { font-size: 1.2rem; margin-top: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 0.5rem; }
.meta { color: var(--muted); font-size: 0.9rem; margin-bottom: 2rem; }
.banner { background: #1a1a2a; border: 1px solid var(--border); padding: 0.8rem 1rem;
          border-radius: 10px; margin-bottom: 1.5rem; color: var(--accent); font-size: 0.9rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 1rem; }
.card .big { font-size: 2rem; font-weight: 700; color: var(--accent); }
.card .label { color: var(--muted); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; }
.chips { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.chip { background: var(--panel); border: 1px solid var(--border);
        padding: 0.3rem 0.7rem; border-radius: 999px; font-size: 0.85rem; }
.chip b { color: var(--accent); }
details { background: var(--panel); border: 1px solid var(--border);
          border-radius: 10px; margin-bottom: 0.75rem; }
details summary {
  padding: 0.9rem 1.1rem; cursor: pointer; font-weight: 600;
  display: flex; justify-content: space-between; align-items: center;
}
details summary:hover { background: #1a1a2a; }
details summary::-webkit-details-marker { display: none; }
details .summary-right { color: var(--muted); font-weight: 400; font-size: 0.9rem; }
details > div.body { padding: 0 1.1rem 1rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 500; }
td.num { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
.empty { color: var(--muted); padding: 1rem; text-align: center; }
.search { width: 100%; max-width: 400px; padding: 0.6rem 0.9rem;
          background: var(--panel); border: 1px solid var(--border); color: var(--text);
          border-radius: 8px; font-size: 0.95rem; margin-bottom: 1rem; }
.search:focus { outline: 2px solid var(--accent); }
.hidden { display: none !important; }
"""


def render_html(folders: list[dict], source_label: str) -> str:
    total_files = sum(f["count"] for f in folders)
    total_size = sum(f["size"] for f in folders)
    by_country: dict[str, int] = {}
    by_month: dict[str, int] = {}
    for f in folders:
        by_country[f["country"]] = by_country.get(f["country"], 0) + f["count"]
        mk = f"{f['year']}.{f['month']}"
        by_month[mk] = by_month.get(mk, 0) + f["count"]

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="en"><head><meta charset="UTF-8">')
    parts.append("<title>Meta Sync Report</title>")
    parts.append(f"<style>{CSS}</style></head><body>")
    parts.append("<h1>Meta Sync Report</h1>")
    parts.append(f'<p class="meta">Generated {generated} · Source: {html.escape(source_label)}</p>')
    parts.append('<input class="search" type="text" id="q" placeholder="Search filename or folder…"/>')
    parts.append('<div class="grid">')
    parts.append(f'<div class="card"><div class="label">Total files</div><div class="big">{total_files}</div></div>')
    parts.append(f'<div class="card"><div class="label">Total size</div><div class="big">{human_size(total_size)}</div></div>')
    parts.append(f'<div class="card"><div class="label">Folders</div><div class="big">{len(folders)}</div></div>')
    parts.append(f'<div class="card"><div class="label">Countries</div><div class="big">{len(by_country)}</div></div>')
    parts.append('</div>')
    parts.append("<h2>By country</h2>")
    parts.append('<div class="chips">')
    for c, n in sorted(by_country.items(), key=lambda x: -x[1]):
        parts.append(f'<span class="chip">{html.escape(c)} <b>{n}</b></span>')
    parts.append('</div>')
    parts.append("<h2>By month</h2>")
    parts.append('<div class="chips">')
    for m in sorted(by_month.keys()):
        parts.append(f'<span class="chip">{html.escape(m)} <b>{by_month[m]}</b></span>')
    parts.append('</div>')
    parts.append("<h2>Folders</h2>")
    if not folders:
        parts.append('<div class="empty">Nothing to show.</div>')
    for f in folders:
        parts.append(f'<details data-folder="{html.escape(f["folder"].lower())}">')
        parts.append(
            f'<summary>{html.escape(f["folder"])}'
            f'<span class="summary-right">{f["count"]} files · {human_size(f["size"])}</span>'
            f'</summary>'
        )
        parts.append('<div class="body"><table><thead><tr>')
        parts.append("<th>Filename</th><th class='num'>Size</th>")
        parts.append("</tr></thead><tbody>")
        for file in f["files"]:
            parts.append(
                f'<tr data-name="{html.escape(file["name"].lower())}">'
                f"<td>{html.escape(file['name'])}</td>"
                f"<td class='num'>{human_size(file['size'])}</td></tr>"
            )
        parts.append("</tbody></table></div>")
        parts.append("</details>")

    # Search script
    parts.append("""<script>
const q = document.getElementById('q');
q.addEventListener('input', () => {
  const term = q.value.toLowerCase().trim();
  document.querySelectorAll('details').forEach(det => {
    const folderMatch = det.dataset.folder.includes(term);
    let anyRow = false;
    det.querySelectorAll('tr[data-name]').forEach(tr => {
      const show = !term || folderMatch || tr.dataset.name.includes(term);
      tr.classList.toggle('hidden', !show);
      if (show) anyRow = true;
    });
    det.classList.toggle('hidden', term && !folderMatch && !anyRow);
    if (term && (folderMatch || anyRow)) det.open = true;
  });
});
</script>""")

    parts.append("</body></html>")
    return "\n".join(parts)


HTA_CSS = """
html, body { margin: 0; padding: 0; background: #0b0b0f; color: #e7e7f0;
             font-family: "Segoe UI", sans-serif; font-size: 14px; }
body { padding: 20px 30px; }
h1 { font-size: 26px; margin: 0 0 4px; color: #e7e7f0; font-weight: 600; }
h2 { font-size: 18px; margin-top: 30px; border-bottom: 1px solid #23233a;
     padding-bottom: 6px; color: #e7e7f0; font-weight: 600; }
.meta { color: #8a8aa0; font-size: 13px; margin-bottom: 20px; }

#toolbar {
  position: sticky; top: 0; background: #0b0b0f;
  padding: 12px 0; margin: -20px -30px 20px;
  border-bottom: 1px solid #23233a; z-index: 100;
}
#toolbar .inner { padding: 0 30px; }
#toolbar strong { margin-right: 12px; font-size: 15px; }
.tb-btn {
  background: #141420; border: 1px solid #23233a; color: #e7e7f0;
  padding: 8px 16px; border-radius: 6px; font-size: 13px;
  cursor: pointer; margin-right: 6px; font-family: inherit;
}
.tb-btn:hover { background: #1a1a2a; }

.grid { display: -ms-flexbox; display: flex; -ms-flex-wrap: wrap; flex-wrap: wrap; }
.card {
  background: #141420; border: 1px solid #23233a; border-radius: 8px;
  padding: 14px 18px; margin: 0 10px 10px 0;
  min-width: 180px; -ms-flex: 1 1 180px; flex: 1 1 180px;
}
.card .label { color: #8a8aa0; font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; }
.card .big { font-size: 28px; font-weight: 700; color: #5b9dff; margin-top: 4px; }

.chips { line-height: 2.2; }
.chip {
  display: inline-block;
  background: #141420; border: 1px solid #23233a;
  padding: 4px 12px; border-radius: 999px; font-size: 13px;
  margin: 0 4px 4px 0;
}
.chip b { color: #5b9dff; }

.folder {
  background: #141420; border: 1px solid #23233a;
  border-radius: 8px; margin-bottom: 8px;
}
.folder .head {
  padding: 12px 16px; cursor: pointer; font-weight: 600;
  display: -ms-flexbox; display: flex; -ms-flex-align: center; align-items: center;
  -ms-flex-pack: justify; justify-content: space-between;
}
.folder .head:hover { background: #1a1a2a; }
.folder .summary-right { color: #8a8aa0; font-weight: 400; font-size: 13px; }
.folder .body { padding: 0 16px 12px; display: none; }
.folder.open .body { display: block; }
.folder table { width: 100%; border-collapse: collapse; font-size: 12px; }
.folder th, .folder td { text-align: left; padding: 5px 8px; border-bottom: 1px solid #23233a; }
.folder th { color: #8a8aa0; font-weight: 500; }
.folder td.num { text-align: right; color: #8a8aa0; }

.search {
  width: 380px; padding: 8px 12px;
  background: #141420; border: 1px solid #23233a; color: #e7e7f0;
  border-radius: 6px; font-size: 14px; font-family: inherit;
  margin-bottom: 14px;
}
.hidden { display: none !important; }
"""

HTA_SCRIPT = """
<script language="JScript">
function runCmd(cmd) {
  var shell = new ActiveXObject("WScript.Shell");
  shell.Run('cmd /c ' + cmd + ' & pause', 1, false);
}
function runMeta() {
  runCmd('cd /d "D:\\\\GitHub\\\\meta-sync" && python -u watcher.py --once');
}
function runCamera() {
  runCmd('cd /d "D:\\\\GitHub\\\\camera-sync" && python -u camera_sync.py --offline');
}
function refreshReport() {
  var shell = new ActiveXObject("WScript.Shell");
  shell.Run('cmd /c cd /d "D:\\\\GitHub\\\\meta-sync" && python -u report.py --hta', 1, true);
  window.location.reload();
}
function toggleFolder(el) {
  if (el.className.indexOf('open') >= 0) {
    el.className = el.className.replace(/\\bopen\\b/, '').replace(/\\s+$/, '');
  } else {
    el.className = el.className + ' open';
  }
}
function filterList() {
  var q = document.getElementById('q').value.toLowerCase();
  var folders = document.getElementsByClassName('folder');
  for (var i = 0; i < folders.length; i++) {
    var folder = folders[i];
    var folderName = folder.getAttribute('data-folder').toLowerCase();
    var folderMatch = folderName.indexOf(q) >= 0;
    var rows = folder.getElementsByTagName('tr');
    var anyRow = false;
    for (var j = 0; j < rows.length; j++) {
      var tr = rows[j];
      if (!tr.getAttribute('data-name')) continue;
      var show = !q || folderMatch || tr.getAttribute('data-name').toLowerCase().indexOf(q) >= 0;
      tr.style.display = show ? '' : 'none';
      if (show) anyRow = true;
    }
    folder.style.display = (q && !folderMatch && !anyRow) ? 'none' : '';
    if (q && (folderMatch || anyRow)) {
      if (folder.className.indexOf('open') < 0) folder.className = folder.className + ' open';
    }
  }
}
</script>
"""


def render_hta(folders: list[dict], source_label: str) -> str:
    """Render an HTA (HTML Application) with working buttons for Windows.

    Self-contained IE11-compatible HTML (not reusing render_html because
    HTA/MSHTML doesn't support modern CSS and HTML5 details element).
    """
    total_files = sum(f["count"] for f in folders)
    total_size = sum(f["size"] for f in folders)
    by_country: dict[str, int] = {}
    by_month: dict[str, int] = {}
    for f in folders:
        by_country[f["country"]] = by_country.get(f["country"], 0) + f["count"]
        mk = f"{f['year']}.{f['month']}"
        by_month[mk] = by_month.get(mk, 0) + f["count"]

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p: list[str] = []
    p.append("<!DOCTYPE html>")
    p.append('<html><head>')
    p.append('<meta http-equiv="X-UA-Compatible" content="IE=edge">')
    p.append('<meta charset="UTF-8">')
    p.append('<title>Meta Sync</title>')
    p.append('<hta:application id="MetaSync" applicationname="Meta Sync" '
             'border="thin" caption="yes" showintaskbar="yes" '
             'singleinstance="yes" windowstate="maximize" scroll="yes" />')
    p.append(f'<style>{HTA_CSS}</style>')
    p.append(HTA_SCRIPT)
    p.append('</head><body>')

    # Toolbar
    p.append('<div id="toolbar"><div class="inner">')
    p.append('<strong>Meta Sync</strong>')
    p.append('<button class="tb-btn" onclick="runMeta()">Sync Meta</button>')
    p.append('<button class="tb-btn" onclick="runCamera()">Sync Camera</button>')
    p.append('<button class="tb-btn" onclick="refreshReport()">Refresh Report</button>')
    p.append('</div></div>')

    p.append('<h1>Meta Sync Report</h1>')
    p.append(f'<p class="meta">Generated {generated} &middot; Source: {html.escape(source_label)}</p>')
    p.append('<input class="search" type="text" id="q" placeholder="Search filename or folder..." onkeyup="filterList()"/>')

    # Summary cards
    p.append('<div class="grid">')
    p.append(f'<div class="card"><div class="label">Total files</div><div class="big">{total_files}</div></div>')
    p.append(f'<div class="card"><div class="label">Total size</div><div class="big">{human_size(total_size)}</div></div>')
    p.append(f'<div class="card"><div class="label">Folders</div><div class="big">{len(folders)}</div></div>')
    p.append(f'<div class="card"><div class="label">Countries</div><div class="big">{len(by_country)}</div></div>')
    p.append('</div>')

    # Country chips
    p.append("<h2>By country</h2>")
    p.append('<div class="chips">')
    for c, n in sorted(by_country.items(), key=lambda x: -x[1]):
        p.append(f'<span class="chip">{html.escape(c)} <b>{n}</b></span>')
    p.append('</div>')

    # Month chips
    p.append("<h2>By month</h2>")
    p.append('<div class="chips">')
    for m in sorted(by_month.keys()):
        p.append(f'<span class="chip">{html.escape(m)} <b>{by_month[m]}</b></span>')
    p.append('</div>')

    # Folder list
    p.append("<h2>Folders</h2>")
    if not folders:
        p.append('<div class="meta">Nothing to show.</div>')
    for f in folders:
        folder_key = html.escape(f["folder"].lower())
        p.append(f'<div class="folder" data-folder="{folder_key}">')
        p.append(f'<div class="head" onclick="toggleFolder(this.parentNode)">'
                 f'<span>{html.escape(f["folder"])}</span>'
                 f'<span class="summary-right">{f["count"]} files &middot; {human_size(f["size"])}</span>'
                 f'</div>')
        p.append('<div class="body"><table><thead><tr>')
        p.append("<th>Filename</th><th class='num'>Size</th>")
        p.append("</tr></thead><tbody>")
        for file in f["files"]:
            name_key = html.escape(file["name"].lower())
            p.append(
                f'<tr data-name="{name_key}">'
                f"<td>{html.escape(file['name'])}</td>"
                f"<td class='num'>{human_size(file['size'])}</td></tr>"
            )
        p.append("</tbody></table></div>")
        p.append("</div>")

    p.append('</body></html>')
    return "\n".join(p)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate HTML report of organized Meta AI photos")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("-o", "--output", default=None, help="Output path (default: report.html or report.hta)")
    parser.add_argument("--local", action="store_true",
                        help="Use local staging + EXIF + geocache (no Z: access)")
    parser.add_argument("--hta", action="store_true",
                        help="Generate Windows HTA with working buttons (requires --local implied)")
    args = parser.parse_args()

    config = load_config(args.config)

    # HTA mode implies local
    use_local = args.local or args.hta

    if use_local:
        print("Building report from local data (no Z: access)...")
        folders = scan_local(config)
        source_label = "local staging + EXIF + geocache"
    else:
        dest = Path(config["destination_dir"])
        print(f"Scanning {dest} for META-AI folders...")
        folders = scan_destination(dest)
        source_label = f"destination {dest}"

    print(f"Got {len(folders)} folders, {sum(f['count'] for f in folders)} files")

    if args.hta:
        out_path = Path(args.output or "report.hta")
        out_path.write_text(render_hta(folders, source_label), encoding="utf-8")
    else:
        out_path = Path(args.output or "report.html")
        out_path.write_text(render_html(folders, source_label), encoding="utf-8")
    print(f"Report written to: {out_path.absolute()}")


if __name__ == "__main__":
    main()
