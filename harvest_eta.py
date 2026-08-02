#!/usr/bin/env python3
"""Departly ETA harvester (Phase 2 — observed per-segment travel times).

The HK GTFS gives only origin+terminus times (no per-stop, no rush-hour signal). But the live
route-ETA feeds predict arrival at EVERY stop for each upcoming bus. This harvester turns those
predictions into OBSERVED per-segment travel times:

  For a route+direction, one route-ETA call lists, per stop-sequence, the next buses' ETAs. The
  soonest ETA walking up the sequence forms a PIECEWISE-INCREASING chain — each monotonic run is
  one physical vehicle's remaining stops (a drop = a different, earlier bus). For each adjacent
  pair in a run we record segment time = eta[next] - eta[this]. Run this every ~2 min, all day:
  as buses move, successive vehicles cover every segment at every time of day, and `aggregate.py`
  medians them into a served table the journey planner sums for a real, time-of-day, per-segment
  ride estimate.

No vehicle ID is needed (we infer runs from the ETA chain) and no paid API is involved — just the
free TD real-time feeds. One cycle appends newline-delimited JSON observations to data/raw/.

Deploy: run on a cheap always-on host every ~2 min (cron / serverless timer). See README.md.
Usage:
  python3 harvest_eta.py --routes 1,6,40,68X        # test a few routes
  python3 harvest_eta.py --all                       # every KMB route (fetches the route list)
"""
import argparse, datetime, json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from segdist import implausible

BASE = "https://data.etabus.gov.hk/v1/transport/kmb"
DATA = os.path.join(os.path.dirname(__file__), "data", "raw")
SCHEDULED_RMK = {"Scheduled Bus", "原定班次"}     # timetable-only rows carry no traffic signal → skip
MIN_SEG, MAX_SEG = 0.05, 20.0                      # plausible minutes for one inter-stop hop

def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1: return None
            time.sleep(1.5 * (i + 1))
    return None

def all_routes():
    d = get(f"{BASE}/route/")
    seen = []
    for r in (d or {}).get("data", []):
        seen.append((r["route"], r.get("service_type", "1")))
    # dedup route+service_type
    return sorted(set(seen))

def parse(s):
    try: return datetime.datetime.fromisoformat(s) if s else None
    except ValueError: return None

def segments_for(route, service_type, now_hint=None):
    """Return list of observation dicts for one route (both directions)."""
    d = get(f"{BASE}/route-eta/{route}/{service_type}")
    if not d: return []
    gen = parse(d.get("generated_timestamp")) or now_hint or datetime.datetime.now(datetime.timezone.utc)
    obs = []
    for direction in ("O", "I"):
        # per stop-sequence: soonest REAL eta (minutes from feed time)
        per = {}
        for r in d.get("data", []):
            if r.get("dir") != direction or r.get("eta_seq") != 1: continue
            e = parse(r.get("eta"))
            if e is None: continue
            if (r.get("rmk_en") or "").strip() in SCHEDULED_RMK: continue      # timetable-only → no traffic info
            per[r["seq"]] = (e - gen).total_seconds() / 60.0
        if len(per) < 2: continue
        seqs = sorted(per)
        # split into monotonic-increasing runs (each run ≈ one vehicle's remaining stops)
        run = [seqs[0]]
        runs = []
        for s in seqs[1:]:
            prev = run[-1]
            # contiguous + non-decreasing eta = same bus, UNLESS the implied speed is impossible
            # (two vehicles' ETAs stitched across a long express gap → a run boundary, not a segment)
            if (s == prev + 1) and (per[s] >= per[prev]) and not implausible(route, service_type, direction, prev, s, per[s] - per[prev]):
                run.append(s)
            else:
                if len(run) > 1: runs.append(run)
                run = [s]
        if len(run) > 1: runs.append(run)
        # each adjacent pair in a run → a segment observation
        for run in runs:
            for a, b in zip(run, run[1:]):
                dt = per[b] - per[a]
                if MIN_SEG <= dt <= MAX_SEG:
                    obs.append({"co": "KMB", "route": route, "st": service_type, "dir": direction,
                                "a": a, "b": b, "min": round(dt, 2)})
    return obs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--routes", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if args.all:
        routes = all_routes()
    elif args.routes:
        routes = [(r.strip(), "1") for r in args.routes.split(",") if r.strip()]
    else:
        routes = [("1", "1"), ("6", "1"), ("40", "1"), ("68X", "1")]
    print(f"harvesting {len(routes)} route(s)…", file=sys.stderr)

    now = datetime.datetime.now(datetime.timezone.utc)
    hkt = now.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    hour = hkt.hour
    daytype = "we" if hkt.weekday() >= 5 else "wd"        # weekend vs weekday
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    all_obs = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(lambda rs: segments_for(rs[0], rs[1], now), routes):
            all_obs.extend(res)

    for o in all_obs:
        o["h"] = hour; o["dt"] = daytype; o["ts"] = stamp
    os.makedirs(DATA, exist_ok=True)
    out = os.path.join(DATA, f"eta_{stamp}.jsonl")
    with open(out, "w") as f:
        for o in all_obs:
            f.write(json.dumps(o, separators=(",", ":")) + "\n")
    print(f"WROTE {out}  ({len(all_obs)} segment obs from {len(routes)} routes, hour {hour} {daytype})")

if __name__ == "__main__":
    main()
