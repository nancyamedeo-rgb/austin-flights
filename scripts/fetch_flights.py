#!/usr/bin/env python3
"""
AUS Flight Tracker — data fetcher

Runs server-side (GitHub Actions), hits airplanes.live (free, no API key,
no rate-limit auth required) for live ADS-B aircraft positions around
Austin-Bergstrom (KAUS), classifies each aircraft as an arrival, departure,
or overflight using altitude / vertical-rate / distance heuristics, and
writes a single static flights.json that the widget fetches same-origin.

No paid APIs. No keys. Nothing for the browser to hit cross-origin.

Important honesty note (also documented in README):
This is NOT airline schedule data. There are no gates, terminals, or
"scheduled" times — those live in airline reservation systems behind paid
APIs (AviationStack, FlightAware AeroAPI, etc). What we have here is
real, live aircraft derived from ADS-B broadcasts: actual flight/callsign,
actual altitude & speed, and a best-effort arrival/departure/overflight
classification based on physics, not a schedule feed.
"""

import json
import math
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────
AUS_LAT, AUS_LON = 30.1944, -97.6699
AUS_ELEV_FT = 542

SEARCH_RADIUS_NM = 50          # airplanes.live point search radius
GROUND_PROXIMITY_NM = 3.5      # "at the airport" radius for on-ground classification
LOW_ALT_FT = 5000              # below this + near airport => likely arrival/departure
CLIMB_FPM_THRESHOLD = 300      # climbing faster than this near airport => departure
DESCENT_FPM_THRESHOLD = -300   # descending faster than this near airport => arrival

API_URL = f"https://api.airplanes.live/v2/point/{AUS_LAT}/{AUS_LON}/{SEARCH_RADIUS_NM}"
USER_AGENT = "aus-flight-tracker/1.0 (+github actions; non-commercial; contact via repo issues)"

OUTPUT_PATH = "docs/flights.json"


def haversine_nm(lat1, lon1, lat2, lon2):
    R_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R_nm * math.asin(math.sqrt(a))


def fetch_aircraft():
    req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"airplanes.live returned HTTP {resp.status}")
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("ac", [])


def classify(ac):
    """
    Returns one of: 'arrival', 'departure', 'ground', 'overflight', None (skip)
    Heuristic, derived purely from position/altitude/vertical-rate — there is
    no schedule or intent data available from ADS-B.
    """
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return None

    dist_nm = haversine_nm(AUS_LAT, AUS_LON, lat, lon)

    alt_raw = ac.get("alt_baro")
    on_ground = alt_raw == "ground" or ac.get("gs", 0) is not None and alt_raw == "ground"
    alt_ft = 0 if on_ground or not isinstance(alt_raw, (int, float)) else alt_raw

    vrate = ac.get("baro_rate")
    vrate = vrate if isinstance(vrate, (int, float)) else 0

    if on_ground:
        # On the ground within a tight radius of KAUS = taxiing in/out
        if dist_nm <= GROUND_PROXIMITY_NM:
            return "ground"
        return None  # on ground elsewhere, irrelevant

    # Airborne, low altitude, near the airport
    if dist_nm <= 15 and alt_ft < LOW_ALT_FT:
        if vrate <= DESCENT_FPM_THRESHOLD:
            return "arrival"
        if vrate >= CLIMB_FPM_THRESHOLD:
            return "departure"
        # low & near but level — likely just-landed rollout or about to rotate
        return "arrival" if alt_ft < 1500 else "departure"

    # Higher altitude / farther out but still descending toward the field
    if dist_nm <= 35 and vrate <= DESCENT_FPM_THRESHOLD and alt_ft < 12000:
        return "arrival"

    if dist_nm <= 35 and vrate >= CLIMB_FPM_THRESHOLD and alt_ft < 12000:
        return "departure"

    return "overflight"


def build_record(ac, category):
    lat, lon = ac.get("lat"), ac.get("lon")
    alt_raw = ac.get("alt_baro")
    on_ground = alt_raw == "ground"
    alt_ft = 0 if on_ground or not isinstance(alt_raw, (int, float)) else alt_raw

    return {
        "icao24": ac.get("hex"),
        "callsign": (ac.get("flight") or "").strip() or None,
        "registration": ac.get("r"),
        "aircraft_type": ac.get("t"),
        "category": category,                 # arrival | departure | ground | overflight
        "lat": lat,
        "lon": lon,
        "altitude_ft": alt_ft,
        "on_ground": on_ground,
        "ground_speed_kt": ac.get("gs"),
        "track_deg": ac.get("track"),
        "vertical_rate_fpm": ac.get("baro_rate"),
        "squawk": ac.get("squawk"),
        "distance_nm": round(haversine_nm(AUS_LAT, AUS_LON, lat, lon), 1) if lat is not None else None,
    }


def main():
    fetched_at = datetime.now(timezone.utc)

    try:
        aircraft = fetch_aircraft()
        error = None
    except (urllib.error.URLError, RuntimeError, TimeoutError, json.JSONDecodeError) as e:
        aircraft = []
        error = str(e)

    records = []
    for ac in aircraft:
        category = classify(ac)
        if category is None:
            continue
        records.append(build_record(ac, category))

    # Sort: closest first within each useful category, overflights last
    priority = {"ground": 0, "arrival": 1, "departure": 1, "overflight": 2}
    records.sort(key=lambda r: (priority.get(r["category"], 3), r["distance_nm"] or 999))

    output = {
        "airport": "KAUS",
        "airport_name": "Austin-Bergstrom International",
        "generated_at": fetched_at.isoformat(),
        "generated_at_unix": int(fetched_at.timestamp()),
        "source": "airplanes.live (ADS-B), classified server-side — not airline schedule data",
        "fetch_error": error,
        "counts": {
            "arrivals": sum(1 for r in records if r["category"] == "arrival"),
            "departures": sum(1 for r in records if r["category"] == "departure"),
            "ground": sum(1 for r in records if r["category"] == "ground"),
            "overflights": sum(1 for r in records if r["category"] == "overflight"),
            "total_tracked": len(records),
        },
        "flights": records,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(records)} aircraft records to {OUTPUT_PATH}")
    if error:
        print(f"WARNING: fetch had an error: {error}", file=sys.stderr)
        # Don't hard-fail the Action — keep the last-good JSON in place if this
        # run produced nothing usable. A transient failure shouldn't blank the widget.
        if not records:
            sys.exit(1)


if __name__ == "__main__":
    main()
