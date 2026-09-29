#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""regions.py — F2: GPS/regional auto-configuration for the mesh radios.

Design requirement (2026-08-13): software must not care which frequency is
active — detect and respond on the regional frequency (GPS) for the set national
region. MeshCore follows the national region profile; Meshtastic always LongFast
for the region. GPS also sets the local clock. No GPS on a fresh install: scan
the radio waves at boot, report what is active, then ask.

Usage:
  regions.py detect [--lat L --lon L]      # GPS (or args) -> national region -> profile
  regions.py apply  [--port PORT] [--lat L --lon L] [--region US]   # apply to a MeshCore companion node
  regions.py scan   [--port PORT] [--seconds S]                     # no-GPS: probe known profiles

Examples:
  python3 tools/regions.py detect --lat 43.07 --lon -89.40     # your lat/lon -> national region
  python3 tools/regions.py apply --port /dev/ttyACM0 --region US
"""
import argparse, asyncio, sys, time

# ---------------------------------------------------------------------------
# National region table (MeshCore profile + Meshtastic preset)
# MeshCore values are the compile/runtime defaults for each region's LoRa plan.
# ---------------------------------------------------------------------------
REGIONS = {
    "US": {"meshcore": {"freq": 910.525, "bw": 62.5, "sf": 7, "cr": 5, "txp": 22},
           "meshtastic": {"preset": "LONG_FAST", "region": "US"}},
    "CA": {"meshcore": {"freq": 910.525, "bw": 62.5, "sf": 7, "cr": 5, "txp": 22},
           "meshtastic": {"preset": "LONG_FAST", "region": "US"}},
    "MX": {"meshcore": {"freq": 906.875, "bw": 62.5, "sf": 7, "cr": 5, "txp": 14},
           "meshtastic": {"preset": "LONG_FAST", "region": "US"}},
    "EU": {"meshcore": {"freq": 869.525, "bw": 125.0, "sf": 7, "cr": 5, "txp": 14},
           "meshtastic": {"preset": "LONG_FAST", "region": "EU_868"}},
    "UK": {"meshcore": {"freq": 869.525, "bw": 125.0, "sf": 7, "cr": 5, "txp": 14},
           "meshtastic": {"preset": "LONG_FAST", "region": "EU_868"}},
    "AU": {"meshcore": {"freq": 916.000, "bw": 125.0, "sf": 7, "cr": 5, "txp": 14},
           "meshtastic": {"preset": "LONG_FAST", "region": "AU_915"}},
    "NZ": {"meshcore": {"freq": 916.000, "bw": 125.0, "sf": 7, "cr": 5, "txp": 14},
           "meshtastic": {"preset": "LONG_FAST", "region": "NZ_915"}},
    "JP": {"meshcore": {"freq": 920.000, "bw": 125.0, "sf": 7, "cr": 5, "txp": 14},
           "meshtastic": {"preset": "LONG_FAST", "region": "JP_923"}},
}

# Offline national bounding boxes: (region, lat_min, lat_max, lon_min, lon_max)
BOXES = [
    ("US", 24.5, 49.4, -125.0, -66.9),
    ("CA", 41.7, 70.0, -141.0, -52.0),
    ("MX", 14.5, 32.7, -117.1, -86.7),
    ("EU", 36.0, 71.0, -10.0, 31.0),
    ("UK", 49.9, 60.9, -8.6, 1.8),
    ("AU", -43.6, -10.7, 112.9, 153.6),
    ("NZ", -47.3, -34.4, 166.4, 178.6),
    ("JP", 24.0, 45.5, 122.9, 145.8),
]


def region_from_gps(lat: float, lon: float) -> str:
    for region, la1, la2, lo1, lo2 in BOXES:
        if la1 <= lat <= la2 and lo1 <= lon <= lo2:
            return region
    return "US"  # safe default; refine as needed


def profile_for(region: str) -> dict:
    r = REGIONS.get(region, REGIONS["US"])
    return {"region": region, "meshcore": r["meshcore"], "meshtastic": r["meshtastic"]}


# ---------------------------------------------------------------------------
# Apply to a MeshCore companion node via the meshcore python lib
# ---------------------------------------------------------------------------
async def apply_to_node(port: str, region: str) -> None:
    # (run with the mesh-relay venv python so meshcore is importable)
    from meshcore import MeshCore

    mc = await MeshCore.create_serial(port, auto_reconnect=True, max_reconnect_attempts=5)
    if mc is None:
        print("ERROR: cannot connect to node"); return
    for _ in range(300):
        if mc.is_connected:
            break
        await asyncio.sleep(0.1)

    p = REGIONS[region]["meshcore"]
    print(f"[apply] {region}: set_radio({p['freq']}, {p['bw']}, {p['sf']}, {p['cr']})")
    ev = await mc.commands.set_radio(p["freq"], p["bw"], p["sf"], p["cr"])
    print(f"[apply] radio -> {ev.type if ev else '?'}")

    now = int(time.time())
    print(f"[apply] set_time({now})")
    ev2 = await mc.commands.set_time(now)
    print(f"[apply] clock -> {ev2.type if ev2 else '?'}")

    si = mc.self_info or {}
    print(f"[apply] node: {si.get('name')} freq={si.get('radio_freq')} sf={si.get('radio_sf')}")
    await mc.disconnect()


# ---------------------------------------------------------------------------
# No-GPS fallback: passive scan across known profiles (documented; the node
# listens on each profile briefly; report which one shows traffic, then ask).
# Full implementation needs per-profile RSSI/packet-activity sampling — TODO.
# ---------------------------------------------------------------------------
async def scan(port: str, seconds: int = 15) -> None:
    print("[scan] TODO: iterate profiles (set_radio each), listen for activity, report")
    print("[scan] candidate order: US -> CA -> EU -> AU -> NZ -> JP")
    for region in ("US", "CA", "EU", "AU", "NZ", "JP"):
        p = REGIONS[region]["meshcore"]
        print(f"[scan] {region}: {p['freq']} MHz / SF{p['sf']} / {p['bw']} kHz — sample {seconds}s")


def detect(lat=None, lon=None):
    if lat is None or lon is None:
        try:  # try gpsd if present
            import gps
            session = gps.gps(mode=gps.WATCH_ENABLE)
            for _ in range(5):
                session.next()
                if hasattr(session.fix, "lat") and session.fix.lat:
                    lat, lon = session.fix.lat, session.fix.lon
                    break
        except Exception:
            pass
    if lat is None:
        print("no GPS fix available — use --lat/--lon or run 'scan'")
        return None
    region = region_from_gps(lat, lon)
    prof = profile_for(region)
    print(f"[detect] GPS {lat:.4f},{lon:.4f} -> region {region}")
    print(f"[detect] MeshCore: {prof['meshcore']}")
    print(f"[detect] Meshtastic: {prof['meshtastic']}")
    return prof


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["detect", "apply", "scan"])
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--region", default=None)
    ap.add_argument("--seconds", type=int, default=15)
    a = ap.parse_args()

    if a.cmd == "detect":
        detect(a.lat, a.lon)
    elif a.cmd == "apply":
        if a.region:
            region = a.region
        else:
            prof = detect(a.lat, a.lon)
            region = prof["region"] if prof else None
        if region:
            asyncio.run(apply_to_node(a.port, region))
    elif a.cmd == "scan":
        asyncio.run(scan(a.port, a.seconds))


if __name__ == "__main__":
    main()
