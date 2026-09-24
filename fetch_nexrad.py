#!/usr/bin/env python3
"""
fetch_nexrad.py — pulls the LATEST NEXRAD Level II base-reflectivity scan
for a small, explicitly-configured list of radar sites and writes a compact
JSON summary per site to data/nexrad/<SITE>.json.

Companion to fetch_glm.py — same "raw public NOAA data, no key, publish a
slim JSON to the repo" pattern, but for ground-based radar instead of
satellite lightning.

Data source: the public "unidata-nexrad-level2" S3 bucket (AWS Open Data
Program, anonymous/no-key access; NOAA/Unidata migrated the archive here
from the older "noaa-nexrad-level2" bucket in mid-2025 -- that legacy
bucket stopped receiving new data on Sept 1, 2025, so anything still
pointed at it silently finds nothing for "today", ever. Confirmed current
as of Sept 2026 against AWS's own Registry of Open Data listing, not just
general documentation, since even NOAA's/AWS's own docs pages can lag a
migration like this). Real volume-scan files are large (10-30MB) and in a
specialized binary format, so this script decodes them with MetPy
(metpy.io.Level2File) and only publishes a small, distance-bounded,
decimated extract — NOT the raw file itself.

WHY A SITE LIST, NOT "ALL NEXRAD SITES":
Each site's latest volume scan has to be downloaded (10-30MB) and decoded
every run. Doing that for all sites on a tight schedule would burn through
GitHub Actions' free minutes fast. Instead, this script only processes the
sites listed in sites.txt (one ICAO radar code per line, e.g. "KOHX") —
edit that file (same low-friction GitHub "edit file" flow you already used
for the GLM setup) to change which site(s) get pulled. The dashboard tells
you which site is nearest to whichever venue you pick; add that code to
sites.txt to start getting real data for it.

SITE SCOPE — NWS-operated WSR-88D only:
The bundled site_coords.json covers the 122 CONUS WSR-88D radars operated
by the National Weather Service itself. Two other categories were
deliberately left out: the ~21 CONUS WSR-88D radars that are the same
hardware/network but operated by the Air Force/Army at military bases
(e.g. Beale AFB, Fort Rucker, Vandenberg), and TDWR (Terminal Doppler
Weather Radar) — a separate, smaller FAA network at airports that was
never in scope here to begin with. If you ever need one of the excluded
military sites, add it to site_coords.json the same way any other site is
listed there.

Usage:
    pip install boto3 metpy numpy
    python fetch_nexrad.py
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

try:
    from metpy.io import Level2File
except ImportError:
    print("ERROR: metpy is required (pip install metpy numpy).", file=sys.stderr)
    raise

BUCKET = "unidata-nexrad-level2"
SITES_FILE = Path(__file__).parent / "sites.txt"
OUT_DIR = Path(__file__).parent / "data" / "nexrad"

# Keep the output small: only gates within this many statute miles of the
# radar, decimated in range, azimuth capped. Tune if you want more detail
# (bigger files) or less (smaller files, faster commits).
MAX_RANGE_MI = 40.0
RANGE_GATE_STRIDE = 10   # keep every Nth range gate (native spacing ~0.25 km)
MAX_RAYS = 180           # thin azimuths down to at most this many rays

NM_TO_MI = 1.15078


def log(msg):
    print(f"[fetch_nexrad] {msg}", flush=True)


def load_sites():
    if not SITES_FILE.exists():
        log(f"No {SITES_FILE.name} found — nothing to do. "
            f"Create it with one NEXRAD site code per line (e.g. KOHX).")
        return []
    sites = []
    for line in SITES_FILE.read_text().splitlines():
        code = line.strip().upper()
        if not code or code.startswith("#"):
            continue
        sites.append(code)
    return sites


def s3_client():
    return boto3.client("s3", config=Config(signature_version=UNSIGNED))


def latest_key_for_site(s3, site, when_utc):
    """Find the most recent Level II object key for `site` on `when_utc`'s
    UTC date. Falls back to the previous UTC day if today's prefix is empty
    (handles the case where the run lands right after UTC midnight and the
    newest scan is still filed under yesterday's date)."""
    for day in (when_utc, when_utc.replace(hour=0) - _one_day()):
        prefix = f"{day.strftime('%Y/%m/%d')}/{site}/"
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
        keys = [o["Key"] for o in resp.get("Contents", [])
                if not o["Key"].endswith("_MDM")]
        if keys:
            keys.sort()
            return keys[-1]
    return None


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def haversine_mi(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def decode_site(s3, site, site_lat, site_lon):
    now = datetime.now(timezone.utc)
    key = latest_key_for_site(s3, site, now)
    if key is None:
        log(f"{site}: no recent volume scan found in the bucket — skipping.")
        return None

    log(f"{site}: downloading s3://{BUCKET}/{key}")
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    body = obj["Body"].read()

    import io
    f = Level2File(io.BytesIO(body))

    # Lowest elevation sweep (index 0) carries the base-reflectivity moment.
    sweep = f.sweeps[0]
    if not sweep or b"REF" not in sweep[0][4]:
        log(f"{site}: no REF moment in sweep 0 — skipping.")
        return None

    ref_hdr = sweep[0][4][b"REF"][0]
    # MetPy's Level2File reports first_gate/gate_width in kilometers already
    # (confirmed against MetPy's own nexrad.py source: both fields go through
    # a scaler(0.001), i.e. raw meters -> km) — convert km -> statute miles.
    KM_TO_MI = 0.621371
    gate_width_mi = ref_hdr.gate_width * KM_TO_MI
    first_gate_mi = ref_hdr.first_gate * KM_TO_MI

    n_rays = len(sweep)
    ray_stride = max(1, n_rays // MAX_RAYS)

    scan_time = None
    samples = []
    for ray in sweep[::ray_stride]:
        ray_hdr = ray[0]
        az = ray_hdr.az_angle
        if scan_time is None:
            try:
                scan_time = ray_hdr.collect_time.replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
        if b"REF" not in ray[4]:
            continue
        _, ref_data = ray[4][b"REF"]
        for gi in range(0, len(ref_data), RANGE_GATE_STRIDE):
            v = ref_data[gi]
            # MetPy marks missing/below-threshold gates as NaN or masked.
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            range_mi = first_gate_mi + gi * gate_width_mi
            if range_mi > MAX_RANGE_MI:
                break
            samples.append({
                "az": round(float(az), 1),
                "range_mi": round(range_mi, 2),
                "dbz": round(float(v), 1),
            })

    return {
        "site": site,
        "site_lat": site_lat,
        "site_lon": site_lon,
        "scan_time": scan_time,
        "generated_at": now.isoformat(),
        "product": "Base reflectivity, lowest tilt (~0.5 deg)",
        "max_range_mi": MAX_RANGE_MI,
        "source": f"s3://{BUCKET}/{key}",
        "gate_count": len(samples),
        "gates": samples,
    }


def load_site_coords():
    """Reads the site coordinate table shipped alongside this script
    (site_coords.json) so we can stamp each output file with the radar's
    own lat/lon (the dashboard needs this to place gates on a map / compute
    distance-to-venue)."""
    coords_file = Path(__file__).parent / "site_coords.json"
    if not coords_file.exists():
        return {}
    return json.loads(coords_file.read_text())


def main():
    sites = load_sites()
    if not sites:
        return
    coords = load_site_coords()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s3 = s3_client()

    for site in sites:
        c = coords.get(site)
        if not c:
            log(f"{site}: not in site_coords.json — skipping (add its lat/lon there first).")
            continue
        try:
            result = decode_site(s3, site, c["lat"], c["lon"])
        except ClientError as e:
            log(f"{site}: S3 error — {e}")
            continue
        except Exception as e:
            log(f"{site}: decode failed — {e}")
            continue
        if result is None:
            continue
        out_path = OUT_DIR / f"{site}.json"
        out_path.write_text(json.dumps(result, separators=(",", ":")))
        log(f"{site}: wrote {out_path} ({result['gate_count']} gates, "
            f"scan {result['scan_time']})")


if __name__ == "__main__":
    main()
