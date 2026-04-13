#!/usr/bin/env python3
"""
Meta Sync — Organize Ray-Ban Meta photos by date and location.

Reads EXIF data from photos, extracts date and GPS coordinates,
reverse-geocodes to city name, and copies into folders like:
    2026.02 Vienna
    2026.02 Dubai
    2026.03 Rome

SAFETY: This script ONLY copies files. It NEVER deletes or moves originals.
"""

import hashlib
import json
import logging
import math
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from geopy.geocoders import Nominatim
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: str = None) -> dict:
    """Load configuration from YAML file."""
    if config_path is None:
        config_path = Path(__file__).parent / "config.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        print(f"Error: config file not found at {config_path}")
        print("Copy config.example.yaml to config.yaml and edit it.")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Processed files tracker (avoid duplicate copies)
# ---------------------------------------------------------------------------

class ProcessedTracker:
    """Track which files have already been copied using SHA-256 hashes."""

    def __init__(self, tracker_file: str):
        self.tracker_file = Path(tracker_file)
        self.hashes: set[str] = set()
        self._load()

    def _load(self):
        if self.tracker_file.exists():
            with open(self.tracker_file, "r", encoding="utf-8") as f:
                self.hashes = set(json.load(f))

    def _save(self):
        with open(self.tracker_file, "w", encoding="utf-8") as f:
            json.dump(sorted(self.hashes), f)

    def is_processed(self, filepath: Path) -> bool:
        """Check if file was already processed (by content hash)."""
        h = self._hash_file(filepath)
        return h in self.hashes

    def mark_processed(self, filepath: Path):
        """Mark file as processed."""
        h = self._hash_file(filepath)
        self.hashes.add(h)
        self._save()

    @staticmethod
    def _hash_file(filepath: Path) -> str:
        """SHA-256 hash of file content."""
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest()


# ---------------------------------------------------------------------------
# Geocoding cache
# ---------------------------------------------------------------------------

class GeoCache:
    """Cache reverse-geocoding results to avoid repeated API calls."""

    def __init__(self, cache_file: str, cluster_radius_km: float = 15):
        self.cache_file = Path(cache_file)
        self.cluster_radius_km = cluster_radius_km
        self.entries: list[dict] = []
        self._load()

    def _load(self):
        if self.cache_file.exists():
            with open(self.cache_file, "r", encoding="utf-8") as f:
                self.entries = json.load(f)

    def save(self):
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self.entries, f, indent=2, ensure_ascii=False)

    def lookup(self, lat: float, lon: float) -> str | None:
        """Return cached city if a nearby point was already geocoded."""
        for entry in self.entries:
            dist = haversine(lat, lon, entry["lat"], entry["lon"])
            if dist <= self.cluster_radius_km:
                return entry["city"]
        return None

    def store(self, lat: float, lon: float, city: str):
        self.entries.append({"lat": lat, "lon": lon, "city": city})
        self.save()


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in km between two GPS points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# EXIF helpers
# ---------------------------------------------------------------------------

def get_exif(filepath: Path) -> dict:
    """Extract EXIF data from an image file."""
    try:
        img = Image.open(filepath)
        exif_raw = img._getexif()
        if not exif_raw:
            return {}
        return {TAGS.get(k, k): v for k, v in exif_raw.items()}
    except Exception:
        return {}


def get_date(exif: dict) -> datetime | None:
    """Extract capture date from EXIF data."""
    for field in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        val = exif.get(field)
        if val:
            try:
                return datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue
    return None


def get_date_from_filename(filepath: Path) -> datetime | None:
    """Try to extract date from common filename patterns as fallback."""
    name = filepath.stem
    for part in name.replace("-", "_").split("_"):
        if len(part) == 8 and part.isdigit():
            try:
                return datetime.strptime(part, "%Y%m%d")
            except ValueError:
                continue
    return None


def get_gps(exif: dict) -> tuple[float, float] | None:
    """Extract GPS coordinates from EXIF data. Returns (lat, lon) or None."""
    gps_info = exif.get("GPSInfo")
    if not gps_info:
        return None

    gps_data = {}
    for k, v in gps_info.items():
        tag = GPSTAGS.get(k, k)
        gps_data[tag] = v

    try:
        lat = _dms_to_decimal(gps_data["GPSLatitude"], gps_data["GPSLatitudeRef"])
        lon = _dms_to_decimal(gps_data["GPSLongitude"], gps_data["GPSLongitudeRef"])
        return (lat, lon)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def _dms_to_decimal(dms, ref: str) -> float:
    """Convert degrees/minutes/seconds to decimal degrees."""
    degrees = float(dms[0])
    minutes = float(dms[1])
    seconds = float(dms[2])
    decimal = degrees + minutes / 60 + seconds / 3600
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


# ---------------------------------------------------------------------------
# Reverse geocoding
# ---------------------------------------------------------------------------

_last_geocode_time = 0.0

def _normalize_country(name: str) -> str:
    """Normalize long country names to short forms."""
    mapping = {
        "United Arab Emirates": "UAE",
        "United States of America": "USA",
        "United States": "USA",
        "United Kingdom": "UK",
    }
    return mapping.get(name, name)


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Get country name from GPS coordinates.

    Uses Nominatim with zoom=3 (country-level admin boundary, ignores POIs).
    Falls back to Photon with country-field extraction if Nominatim fails.
    """
    global _last_geocode_time

    # Strict rate limit for Nominatim: 1 req/sec minimum
    elapsed = time.time() - _last_geocode_time
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    _last_geocode_time = time.time()

    # Primary: Nominatim with zoom=3 — returns the country by admin boundary,
    # never a POI. This fixes the "French museum in Spain" bug.
    try:
        geolocator = Nominatim(user_agent="meta-sync-photo-organizer/1.0 (photo-organizer)")
        location = geolocator.reverse(
            f"{lat}, {lon}",
            language="en",
            zoom=3,
            timeout=15,
        )
        if location and location.raw.get("address"):
            country = location.raw["address"].get("country")
            if country:
                return _normalize_country(country)
    except Exception as e:
        logging.info(f"Nominatim failed for ({lat}, {lon}): {e} — trying Photon")

    # Fallback: Photon (in case Nominatim is rate-limited)
    try:
        from geopy.geocoders import Photon
        geolocator = Photon(user_agent="meta-sync-photo-organizer", timeout=10)
        location = geolocator.reverse(f"{lat}, {lon}", language="en")
        if location and location.raw.get("properties"):
            country = location.raw["properties"].get("country")
            if country:
                return _normalize_country(country)
    except Exception as e:
        logging.warning(f"Geocoding failed for ({lat}, {lon}): {e}")

    return None


# ---------------------------------------------------------------------------
# File date fallback (filesystem)
# ---------------------------------------------------------------------------

def get_date_from_file(filepath: Path) -> datetime:
    """Use file modification time as last resort."""
    mtime = filepath.stat().st_mtime
    return datetime.fromtimestamp(mtime)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def build_folder_name(dt: datetime, city: str) -> str:
    """Build folder name like '2026.02 UAE META-AI'."""
    month_str = dt.strftime("%Y.%m")
    return f"{month_str} {city} META-AI"


def build_file_name(dt: datetime, city: str, suffix: str) -> str:
    """Build file name like '25.07.04 10.50.31 UAE.jpg'."""
    stamp = dt.strftime("%y.%m.%d %H.%M.%S")
    return f"{stamp} {city}{suffix}"


def process_file(
    filepath: Path,
    dest_dir: Path,
    fallback_city: str,
    geocache: GeoCache,
    tracker: ProcessedTracker,
    day_city_map: dict[str, str] | None = None,
) -> bool:
    """Process a single photo/video file. COPY only, never deletes. Returns True if copied."""
    logger = logging.getLogger("meta_sync")

    # Skip already-processed files
    if tracker.is_processed(filepath):
        logger.debug(f"Skipping (already processed): {filepath.name}")
        return False

    logger.info(f"Processing: {filepath.name}")

    # 1. Get date
    exif = get_exif(filepath)
    dt = get_date(exif)
    if dt is None:
        dt = get_date_from_filename(filepath)
    if dt is None:
        dt = get_date_from_file(filepath)
        logger.info(f"  Date from file mtime: {dt}")
    else:
        logger.info(f"  Date from EXIF/filename: {dt}")

    # 2. Get city
    city = None
    gps = get_gps(exif)
    if gps:
        lat, lon = gps
        city = geocache.lookup(lat, lon)
        if city:
            logger.info(f"  City from cache: {city}")
        else:
            city = reverse_geocode(lat, lon)
            if city:
                geocache.store(lat, lon, city)
                logger.info(f"  City from geocoding: {city}")

    # If no GPS (common for videos), try to infer city from nearby photos same day
    if not city and day_city_map:
        day_key = dt.strftime("%Y-%m-%d")
        city = day_city_map.get(day_key)
        if city:
            logger.info(f"  City inferred from same-day photos: {city}")

    if not city:
        city = fallback_city
        logger.info(f"  No GPS data, using fallback: {city}")

    # 3. Build destination
    folder_name = build_folder_name(dt, city)
    target_dir = dest_dir / folder_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Skip if the original filename (V1) is already in the target folder
    # — this preserves files copied before the rename logic was added
    if (target_dir / filepath.name).exists():
        logger.info(f"  Skipping — original name already copied: {filepath.name}")
        tracker.mark_processed(filepath)
        return False

    # New filename format (V2): "YY.MM.DD hh.mm.ss COUNTRY.ext"
    new_name = build_file_name(dt, city, filepath.suffix.lower())
    target_file = target_dir / new_name
    # Handle name collisions (two photos with same timestamp)
    if target_file.exists():
        stem = Path(new_name).stem
        suffix = Path(new_name).suffix
        counter = 2
        while target_file.exists():
            target_file = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    # 4. COPY only — originals are NEVER touched. Retry on transient network errors.
    last_err = None
    for attempt in range(5):
        try:
            shutil.copy2(str(filepath), str(target_file))
            logger.info(f"  Copied to: {target_file}")
            last_err = None
            break
        except OSError as e:
            last_err = e
            wait = 5 * (attempt + 1)  # 5, 10, 15, 20, 25 seconds
            logger.warning(f"  Copy failed (attempt {attempt + 1}/5): {e} — retrying in {wait}s")
            time.sleep(wait)
            # Ensure target dir still exists (may have been disconnected)
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
    if last_err is not None:
        raise last_err

    # 5. Mark as processed so we don't copy again next run
    tracker.mark_processed(filepath)

    return True


def find_photos(source_dir: Path, extensions: list[str]) -> list[Path]:
    """Find all matching files in source directory (recursive)."""
    files = []
    ext_set = {e.lower() for e in extensions}
    for f in source_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in ext_set:
            files.append(f)
    return sorted(files)


def setup_logging(log_file: str | None):
    """Configure logging to file and console."""
    logger = logging.getLogger("meta_sync")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Organize Ray-Ban Meta photos by date and location")
    parser.add_argument("-c", "--config", help="Path to config.yaml", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without copying files")
    args = parser.parse_args()

    config = load_config(args.config)

    setup_logging(config.get("log_file"))
    logger = logging.getLogger("meta_sync")

    source_dir = Path(config["source_dir"])
    dest_dir = Path(config["destination_dir"])
    fallback_city = config.get("fallback_city", "Unknown")
    extensions = config.get("extensions", [".jpg", ".jpeg", ".png"])
    cache_file = config.get("cache_file", "geocache.json")
    cluster_radius = config.get("cluster_radius_km", 15)
    tracker_file = config.get("tracker_file", "processed.json")

    if not source_dir.exists():
        logger.error(f"Source directory does not exist: {source_dir}")
        sys.exit(1)

    dest_dir.mkdir(parents=True, exist_ok=True)

    geocache = GeoCache(cache_file, cluster_radius)
    tracker = ProcessedTracker(tracker_file)

    photos = find_photos(source_dir, extensions)
    logger.info(f"Found {len(photos)} files in source")

    if not photos:
        logger.info("Nothing to do.")
        return

    if args.dry_run:
        logger.info("=== DRY RUN — no files will be copied ===")
        skipped = 0
        for photo in photos:
            if tracker.is_processed(photo):
                skipped += 1
                continue
            exif = get_exif(photo)
            dt = get_date(exif) or get_date_from_filename(photo) or get_date_from_file(photo)
            gps = get_gps(exif)
            city = None
            if gps:
                city = geocache.lookup(gps[0], gps[1])
                if not city:
                    city = reverse_geocode(gps[0], gps[1])
            city = city or fallback_city
            folder = build_folder_name(dt, city)
            logger.info(f"  {photo.name} -> {folder}/")
        if skipped:
            logger.info(f"  ({skipped} files already processed, would be skipped)")
        return

    # First pass: build day → city map from photos that have GPS
    # This lets us assign a city to videos (no GPS) based on same-day photos
    logger.info("Scanning GPS data to build day-city map...")
    day_city_map: dict[str, str] = {}
    for photo in photos:
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

    # Second pass: group files by month and process month by month
    from collections import defaultdict
    by_month: dict[str, list[Path]] = defaultdict(list)
    for photo in photos:
        exif = get_exif(photo)
        dt = get_date(exif) or get_date_from_filename(photo) or get_date_from_file(photo)
        month_key = dt.strftime("%Y.%m")
        by_month[month_key].append(photo)

    total_copied = 0
    total_skipped = 0
    total_errors = 0
    months_sorted = sorted(by_month.keys())

    for i, month_key in enumerate(months_sorted, 1):
        month_photos = by_month[month_key]
        logger.info(f"")
        logger.info(f"=== Month {i}/{len(months_sorted)}: {month_key} ({len(month_photos)} files) ===")

        copied = 0
        skipped = 0
        errors = 0
        for photo in month_photos:
            try:
                if process_file(photo, dest_dir, fallback_city, geocache, tracker, day_city_map):
                    copied += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f"  Error processing {photo.name}: {e}")
                errors += 1

        logger.info(f"--- {month_key}: {copied} copied, {skipped} already synced, {errors} errors ---")
        total_copied += copied
        total_skipped += skipped
        total_errors += errors

    logger.info(f"")
    logger.info(f"ALL DONE! Total: {total_copied} copied, {total_skipped} already synced, {total_errors} errors.")


if __name__ == "__main__":
    main()
