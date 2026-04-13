# Meta Sync

Pulls photos and videos from **Ray-Ban Meta smart glasses** (via the companion
app on an Android phone) and organizes them on a destination drive (e.g. a
Synology NAS) by **date and country**, derived from EXIF data.

Photos end up in folders like:

```
Z:/
  2025.07 UAE META-AI/
  2025.08 Spain META-AI/
  2025.08 Austria META-AI/
  2026.02 Italy META-AI/
```

and are renamed to `YY.MM.DD hh.mm.ss Country.ext` (e.g.
`25.08.22 16.43.26 Austria.jpg`).

**Safety:** the tool **only copies**. It never deletes or moves files from the
source (phone) or destination (NAS). The only deletion happens from the local
staging folder, and only after a file has been verified to exist at the
destination with matching size (done by the orchestrator).

## Components

| File | What it does |
|---|---|
| `meta_sync.py` | Core organizer: reads EXIF, reverse-geocodes to country, copies to destination folders, tracks processed files |
| `watcher.py` | Pulls new photos from the phone via ADB (one-shot mode `--once` or watcher mode) |
| `camera_sync.py` (separate repo) | Similar flow for regular Samsung camera photos into yearly / quarterly folders |
| `orchestrator.py` | After a sync, verifies every staging file against the destination, regenerates the report, and cleans staging (only if verification passed) |
| `report.py` | Generates a static HTML report grouped by month/country with a search bar |
| `app.py` | Native Tkinter GUI that combines the report view with Sync buttons and phone status check |

## Requirements

- **Python 3.10+**
- Pillow, geopy, PyYAML (`pip install -r requirements.txt`)
- **ADB** (Android Debug Bridge) in `PATH`
- **USB debugging** enabled on the phone (with Samsung Auto Blocker off if applicable)

## Setup

```bash
git clone https://github.com/YOUR_USER/meta-sync.git
cd meta-sync
pip install -r requirements.txt
cp config.example.yaml config.yaml
# edit config.yaml with your paths
```

### `config.yaml`

```yaml
source_dir: "D:/GitHub/meta-sync/staging"     # local staging (pulled from phone)
destination_dir: "Z:/"                         # organized files land here
phone_path: "/sdcard/Download/Meta AI"         # Meta app folder on Android
fallback_city: "Unknown"                       # used when no GPS data
cluster_radius_km: 15                          # GPS clustering radius for cache
```

## Usage

### GUI (recommended)

Double-click **`Meta Sync.bat`** (Windows, starts `pythonw app.py` — no console).

You get a native window with:

- **Phone status** (green = connected, red = not)
- **Sync Meta / Sync Camera / Sync Both** buttons
- **Stats** (total files, size, folders, countries)
- A **folder browser** with a search bar
- An **Open HTML report** button

The GUI checks ADB **only when you click "Check phone"** — no background polling.

### Command line

```bash
python watcher.py --once     # pull new photos from phone and organize
python meta_sync.py          # organize whatever is already in staging
python report.py --local     # generate report.html from local data
python orchestrator.py       # full pipeline: sync → verify → report → cleanup
```

## How it works

1. **Pull** — `watcher.py` lists files in `/sdcard/Download/Meta AI` on the
   phone via ADB and pulls any new ones into `staging/`.
2. **Organize** — `meta_sync.py` reads each file's EXIF date and GPS. A cached
   reverse-geocode (Nominatim zoom=3 → Photon fallback) turns GPS into a
   country. Files are copied to
   `Z:/YYYY.MM Country META-AI/YY.MM.DD hh.mm.ss Country.ext`.
3. **Verify** — the orchestrator indexes destination folders and checks each
   staging file is present with matching size.
4. **Cleanup** — if and only if verification has zero missing / zero size
   mismatches, staging files are deleted to free local disk.

## Files generated (all in `.gitignore`)

| File | Purpose |
|---|---|
| `config.yaml` | Your personal paths (never committed) |
| `staging/` | Files pulled from phone, awaiting organization |
| `geocache.json` | GPS → country cache (speeds up repeat runs) |
| `processed.json` | SHA-256 hashes of copied files |
| `synced_files.json` | Filenames already pulled from phone |
| `meta_sync.log` | Execution log |
| `report.html` | Generated HTML report |
| `orchestrator.log` | Orchestrator execution log |
| `orchestrator_status.json` | Orchestrator phase status JSON |
