#!/usr/bin/env python3
"""One harvest cycle for the GitHub Actions runner (free public-repo deployment).

Reads the current segment_times.json (the served file IS the accumulator), harvests one live cycle
of per-segment observations, folds each into a running estimate, and writes it back. The workflow
commits the updated file — so segment_times.json is both the state and the published output, fetched
by the app over the repo's public raw URL. No database, no secrets.

Estimate update: a running MEAN for the first 10 samples (stable warm-up), then an EMA (α=0.15) so it
tracks recent traffic rather than being anchored to weeks-old data. Stored as [minutes, sampleCount];
the app trusts a segment once count ≥ 3.
"""
import datetime, json, os
from concurrent.futures import ThreadPoolExecutor
from harvest_eta import segments_for, all_routes

FILE = os.path.join(os.path.dirname(__file__), "segment_times.json")
BANDS = [(0, 6, "0"), (6, 10, "1"), (10, 16, "2"), (16, 20, "3"), (20, 24, "4")]
MAX_SEG = 20.0

def band(h):
    for s, e, b in BANDS:
        if s <= h < e: return b
    return "4"

def main():
    state = {}
    if os.path.exists(FILE):
        try: state = json.load(open(FILE))
        except Exception: state = {}

    now = datetime.datetime.now(datetime.timezone.utc)
    hkt = now.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    b = band(hkt.hour)
    dt = "we" if hkt.weekday() >= 5 else "wd"

    routes = all_routes()
    if not routes:
        print("no routes fetched — skipping cycle"); return
    obs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(lambda rs: segments_for(rs[0], rs[1], now), routes):
            obs.extend(res)

    for o in obs:
        key = f"{o['co']}|{o['route']}|{o['st']}|{o['dir']}"
        seg = f"{o['a']}-{o['b']}"
        m = min(float(o["min"]), MAX_SEG)
        node = state.setdefault(key, {}).setdefault(dt, {}).setdefault(b, {})
        if seg in node:
            ema, n = node[seg]
            alpha = 1.0 / (n + 1) if n < 10 else 0.15    # running mean → EMA
            node[seg] = [round(ema + alpha * (m - ema), 1), n + 1]
        else:
            node[seg] = [round(m, 1), 1]

    json.dump(state, open(FILE, "w"), separators=(",", ":"), ensure_ascii=False)
    segs = sum(len(bb) for v in state.values() for dd in v.values() for bb in dd.values())
    trusted = sum(1 for v in state.values() for dd in v.values() for bb in dd.values()
                  for val in bb.values() if val[1] >= 3)
    print(f"cycle {b}/{dt}: {len(obs)} obs / {len(routes)} routes → {segs} segments tracked "
          f"({trusted} trusted, n≥3), {os.path.getsize(FILE)//1024} KB")

if __name__ == "__main__":
    main()
