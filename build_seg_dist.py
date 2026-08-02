#!/usr/bin/env python3
"""One-time (refresh occasionally) builder for seg_dist.json — straight-line metres between
consecutive KMB stops, keyed by the LIVE route-eta seq numbering so the harvester can apply an
implied-speed sanity gate (a bus can't cross 15 km in 2 min → that "segment" is two different
vehicles' ETAs stitched together by the monotonic-run chaining).

Keyed EXACTLY like segment_times.json's route part: "route|service_type|dir" -> {"a-b": metres}
with dir in O/I and seq as ints (route-eta seq). Distances come from the LIVE route-stop feed
(NOT the app's bundled files) so the seq basis matches the harvested data even on routes whose
bundled stop list has drifted.

Usage: python3 build_seg_dist.py   (writes seg_dist.json; ~2k route-stop calls, a few minutes)
"""
import json, math, os, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://data.etabus.gov.hk/v1/transport/kmb"
OUT = os.path.join(os.path.dirname(__file__), "seg_dist.json")
BOUND = {"O": "outbound", "I": "inbound"}

def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1: return None
    return None

def haversine_m(a, b):
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla/2)**2 + math.cos(la1)*math.cos(la2)*math.sin(dlo/2)**2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))

def all_routes():
    d = get(f"{BASE}/route/")
    return sorted({(r["route"], r.get("service_type", "1")) for r in (d or {}).get("data", [])})

def stop_coords():
    d = get(f"{BASE}/stop")
    m = {}
    for s in (d or {}).get("data", []):
        try: m[s["stop"]] = (float(s["lat"]), float(s["long"]))
        except (KeyError, ValueError, TypeError): pass
    return m

def route_dir_dists(route, st, dircode, coords):
    d = get(f"{BASE}/route-stop/{route}/{BOUND[dircode]}/{st}")
    rows = [(int(r["seq"]), r["stop"]) for r in (d or {}).get("data", []) if r.get("stop")]
    rows.sort()
    out = {}
    for (sa, ida), (sb, idb) in zip(rows, rows[1:]):
        if sb != sa + 1: continue
        ca, cb = coords.get(ida), coords.get(idb)
        if ca and cb:
            out[f"{sa}-{sb}"] = round(haversine_m(ca, cb))
    return f"{route}|{st}|{dircode}", out

def main():
    print("fetching stop coords…", file=sys.stderr)
    coords = stop_coords()
    print(f"  {len(coords)} stops", file=sys.stderr)
    routes = all_routes()
    print(f"building distances for {len(routes)} route/service pairs × 2 dirs…", file=sys.stderr)
    jobs = [(r, st, dc) for (r, st) in routes for dc in ("O", "I")]
    table = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for key, dists in ex.map(lambda j: route_dir_dists(j[0], j[1], j[2], coords), jobs):
            if dists: table[key] = dists
    json.dump(table, open(OUT, "w"), separators=(",", ":"))
    nseg = sum(len(v) for v in table.values())
    print(f"WROTE {OUT}: {len(table)} route-dirs, {nseg} segment distances, {os.path.getsize(OUT)//1024} KB")

if __name__ == "__main__":
    main()
