#!/usr/bin/env python3
"""
Pulls the most recent GOES-19 (GOES-East) GLM Level-2 LCFA lightning-flash
files from NOAA's public "noaa-goes19" S3 bucket (NOAA Open Data Dissemination
Program — public, no key or AWS account needed), extracts flash-level
lat/lon/time/energy, and writes a small JSON file for the Stadium Lightning
Dashboard to fetch over plain HTTP.

Meant to run on a schedule via GitHub Actions (see
.github/workflows/update-lightning.yml). Each run rebuilds the JSON from
scratch from the last WINDOW_MINUTES of S3 data — it doesn't depend on state
from previous runs, so a missed or failed run just means a slightly
different flash list next time, never drift or duplicate accumulation.

GLM L2 LCFA files publish roughly every 20 seconds per satellite. This only
reads the flash-level variables (flash_lat, flash_lon, flash_id,
flash_energy, flash_quality_flag, flash_time_offset_of_first_event) — not
event- or group-level data, which is much higher volume and unnecessary for
"where did lightning strike."

Variable names were verified against two independent, widely-used sources
(the `glmtools` package used throughout the atmospheric-science community,
and a working university Python/GOES tutorial) rather than taken from NOAA's
PDF product guide alone, since a wrong variable name would silently produce
an empty/wrong dataset. That said: this script could not be executed against
a real file before being handed off (installing boto3/netCDF4 was blocked in
the sandbox this was built in), so the FIRST scheduled/manual Actions run is
the real end-to-end test — check the Actions log and data/lightning.json
after it runs, don't assume it's right un-checked.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import boto3
from botocore import UNSIGNED
from botocore.config import Config
import netCDF4

BUCKET = "noaa-goes19"   # GOES-19 became the operational GOES-East satellite in 2025
PRODUCT = "GLM-L2-LCFA"
WINDOW_MINUTES = 15       # how far back to look, rebuilt fresh every run
# Generous CONUS-ish box — trims obviously-irrelevant flashes (e.g. mid-Atlantic,
# well off the Pacific coast) before writing, to keep the JSON small. Widen this
# if venues outside the continental US are ever added to the dashboard.
BBOX = {"lat_min": 15.0, "lat_max": 55.0, "lon_min": -130.0, "lon_max": -60.0}

REQUIRED_VARS = (
    "flash_lat", "flash_lon", "flash_energy", "flash_time_offset_of_first_event",
)

s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))


def list_recent_keys(window_minutes):
    """List GLM L2 LCFA object keys touching the last `window_minutes`.
    Files are organized in the bucket as PRODUCT/year/day-of-year/hour/..., so
    this walks the hour-prefixes the window overlaps and lists each once."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=window_minutes)
    seen_prefixes = set()
    keys = []
    t = start
    while t <= now:
        prefix = f"{PRODUCT}/{t.year}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"
        if prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        t += timedelta(minutes=30)
    return sorted(set(keys))


def parse_key_start_time(key):
    """Pull the sYYYYDDDHHMMSSS start-time token out of the filename so we can
    skip files clearly outside the window without downloading them."""
    name = key.rsplit("/", 1)[-1]
    for part in name.split("_"):
        if part.startswith("s") and len(part) == 15 and part[1:].isdigit():
            year, doy = int(part[1:5]), int(part[5:8])
            hh, mm, ss = int(part[8:10]), int(part[10:12]), int(part[12:14])
            return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(
                days=doy - 1, hours=hh, minutes=mm, seconds=ss
            )
    return None


def open_dataset(raw_bytes):
    """Open the NetCDF bytes in memory; fall back to a temp file if this
    netCDF4/HDF5 build doesn't support the in-memory driver."""
    try:
        return netCDF4.Dataset("in-memory.nc", memory=raw_bytes)
    except Exception:
        tmp = tempfile.NamedTemporaryFile(suffix=".nc", delete=False)
        tmp.write(raw_bytes)
        tmp.flush()
        return netCDF4.Dataset(tmp.name)


def read_flashes(key):
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    raw = obj["Body"].read()
    ds = open_dataset(raw)
    try:
        missing = [v for v in REQUIRED_VARS if v not in ds.variables]
        if missing:
            print(f"WARNING: {key} missing expected variable(s) {missing} — skipping file", file=sys.stderr)
            return []

        lats = ds.variables["flash_lat"][:]
        lons = ds.variables["flash_lon"][:]
        energy_var = ds.variables["flash_energy"]
        energy = energy_var[:]
        energy_units = getattr(energy_var, "units", "unknown")
        time_var = ds.variables["flash_time_offset_of_first_event"]
        # Reads the epoch/units straight off the file's own CF-compliant time
        # attribute rather than assuming one, so this stays correct even if
        # NOAA changes the reference epoch in a future product revision.
        times = netCDF4.num2date(
            time_var[:], time_var.units,
            only_use_cftime_datetimes=False, only_use_python_datetimes=True,
        )
        quality = ds.variables["flash_quality_flag"][:] if "flash_quality_flag" in ds.variables else None

        flashes = []
        for i in range(len(lats)):
            lat, lon = float(lats[i]), float(lons[i])
            if not (BBOX["lat_min"] <= lat <= BBOX["lat_max"] and BBOX["lon_min"] <= lon <= BBOX["lon_max"]):
                continue
            t = times[i]
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            flashes.append({
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "time": t.isoformat(),
                "energy": float(energy[i]),
                "energy_units": str(energy_units),
                "quality_flag": int(quality[i]) if quality is not None else None,
            })
        return flashes
    finally:
        ds.close()


def main():
    keys = list_recent_keys(WINDOW_MINUTES)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
    keys = [k for k in keys if (parse_key_start_time(k) or cutoff) >= cutoff]

    all_flashes, files_read, files_failed = [], 0, 0
    for key in keys:
        try:
            all_flashes.extend(read_flashes(key))
            files_read += 1
        except Exception as e:
            files_failed += 1
            print(f"WARNING: failed to read {key}: {e}", file=sys.stderr)

    now = datetime.now(timezone.utc)
    for f in all_flashes:
        f["age_sec"] = max(0, int((now - datetime.fromisoformat(f["time"])).total_seconds()))

    output = {
        "generated_at": now.isoformat(),
        "satellite": "GOES-19 (GOES-East)",
        "product": PRODUCT,
        "window_minutes": WINDOW_MINUTES,
        "source": f"s3://{BUCKET}/{PRODUCT}/ — NOAA Open Data Dissemination Program, public, no key",
        "files_found": len(keys),
        "files_read": files_read,
        "files_failed": files_failed,
        "flash_count": len(all_flashes),
        "flashes": all_flashes,
    }

    if files_read == 0:
        print("ERROR: read zero files successfully this run — leaving the existing data/lightning.json in place rather than overwriting it with nothing.", file=sys.stderr)
        sys.exit(1)

    with open("data/lightning.json", "w") as fh:
        json.dump(output, fh, separators=(",", ":"))

    print(f"Wrote {len(all_flashes)} flashes from {files_read}/{len(keys)} files ({files_failed} failed) to data/lightning.json")


if __name__ == "__main__":
    main()
