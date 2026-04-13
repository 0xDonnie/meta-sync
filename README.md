# Meta Sync

Pulls photos and videos from **Ray-Ban Meta smart glasses** (via the Meta View
companion app on an Android phone) and organizes them on a destination drive
(e.g. a Synology NAS) by **date and country**, derived from EXIF data.

Photos end up in folders like:

```
Z:/
  2025.07 UAE META-AI/
  2025.08 Spain META-AI/
  2025.08 Austria META-AI/
  2026.03 France META-AI/
```

and are renamed to `YY.MM.DD hh.mm.ss Country.ext` (e.g.
`25.08.22 16.43.26 Austria.jpg`).

**Safety:** the tool **only copies**. It never deletes or moves files from the
source (phone) or the destination (NAS). The only local deletion is from the
staging folder, and only after a file has been verified on the destination with
matching size.

## Components

| File | What it does |
|---|---|
| `meta_sync.py` | Core organizer: reads EXIF, reverse-geocodes GPS to country, copies to destination folders, tracks processed files |
| `watcher.py` | Pulls new photos from the phone via ADB (one-shot `--once` or watcher mode) |
| `orchestrator.py` | After a sync, verifies every staging file against the destination, regenerates the report, and cleans staging (only if verification passed) |
| `report.py` | Generates a static HTML report grouped by month/country with a search bar |
| `manifest.py` | Caches the list of files on the destination (`manifest.json`) so the report is instant and incremental updates are cheap |
| `app.py` | Native Tkinter GUI that combines the report view with Sync buttons and on-demand phone status check |

The **Camera Samsung** flow (for regular Samsung camera photos) lives in the
separate [camera-sync](../camera-sync) repo.

## Requirements

- **Python 3.10+**
- `pip install -r requirements.txt` (Pillow, geopy, PyYAML)
- **ADB** (Android Debug Bridge) available on `PATH`
- **USB debugging** enabled on the phone
  (on Samsung: disable Auto Blocker first, then enable Developer Options → USB debugging)

## Setup

```bash
git clone https://github.com/YOUR_USER/meta-sync.git
cd meta-sync
pip install -r requirements.txt
cp config.example.yaml config.yaml
# then edit config.yaml with your paths
```

### `config.yaml` keys

```yaml
source_dir: "D:/GitHub/meta-sync/staging"   # local staging folder
destination_dir: "Z:/"                       # where organized files land
phone_path: "/sdcard/Download/Meta AI"       # Meta app photo folder on Android
fallback_city: "Unknown"                     # used when no GPS data
cluster_radius_km: 15                        # GPS clustering radius for cache
```

## Everyday usage

### The easy way — the GUI

1. Connect the phone via USB (mode **File transfer**, USB debugging allowed).
2. Double-click **`Meta Sync.bat`** — opens the native Tkinter window.
3. The top bar shows the phone status (green = connected, red = not). Click
   **Check phone** if you just plugged it in.
4. Click one of the Sync buttons:
   - **Sync Meta** → pulls new Ray-Ban Meta photos and organizes them
   - **Sync Camera** → runs the camera-sync flow for regular camera photos
   - **Sync Both** → runs both in sequence
5. Each sync opens its own console window so you can watch the progress.
6. When the sync is done, click **Refresh** in the GUI. This:
   - Runs an incremental `manifest.json` update (only stats files that weren't
     in the manifest before)
   - Updates the stats cards and the folder tree
   - Regenerates `report.html`
7. Click **Open HTML report** if you want the pretty browser view.

### Command line equivalents

```bash
python watcher.py --once      # pull new Meta photos from phone, organize them
python meta_sync.py           # organize whatever is already in the staging folder
python report.py --local      # regenerate report.html from local data
python manifest.py --build    # first-time scan of the destination
python manifest.py --update   # incremental scan of the destination
python manifest.py --show     # show manifest stats
python orchestrator.py        # full pipeline: sync → verify → report → cleanup
```

### First-time manifest build

After the very first sync, the tool doesn't yet know what's on your
destination drive. Run `python manifest.py --build` once — it walks only the
folders the tool can create (never lists the destination root), caches
filenames and sizes to `manifest.json`, and from then on every refresh is
instant + incremental.

## How it works

1. **Pull** — `watcher.py` lists files in `/sdcard/Download/Meta AI` on the
   phone via ADB and pulls any new ones into `staging/`.
2. **Organize** — `meta_sync.py` reads each file's EXIF date and GPS. A cached
   reverse-geocode (Nominatim with `zoom=3` → Photon fallback) turns GPS into a
   country. Files are copied to
   `{destination}/YYYY.MM Country META-AI/YY.MM.DD hh.mm.ss Country.ext`.
3. **Verify** — the orchestrator walks each known destination folder and
   checks every staging file is present with matching size.
4. **Cleanup** — if and only if verification has zero missing / zero size
   mismatches, the staging files are deleted to free local disk.
5. **Manifest** — `manifest.py` keeps `manifest.json` with `{folder: {file: size}}`
   so the report and the GUI don't need to stat the destination every time.
   Folder lookups are targeted (no `Z:\` root listing), using a cross product
   of dates from `synced_files.json` and countries from `geocache.json`.

## Files generated (all in `.gitignore`)

| File | Purpose |
|---|---|
| `config.yaml` | Personal paths |
| `staging/` | Files pulled from phone, awaiting organization |
| `geocache.json` | GPS → country cache |
| `processed.json` | SHA-256 hashes of copied files |
| `synced_files.json` | Filenames already pulled from phone |
| `manifest.json` | Destination folder snapshot for fast reports |
| `report.html` | Generated HTML report |
| `meta_sync.log` | Execution log |
| `orchestrator.log` | Orchestrator execution log |
| `orchestrator_status.json` | Orchestrator phase status |

Nothing personal is ever committed to the repo.
