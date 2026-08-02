#!/usr/bin/env python3
"""Per-stop-ETA harvesting for operators WITHOUT an all-stops route-eta call — currently Citybus (CTB).

KMB exposes one `route-eta` call returning every stop, so a route is 1 call. CTB (and GMB) only expose
PER-STOP eta, so a route costs ~1 call per stop (~20-40x). Harvesting all ~400 CTB routes every cycle
is infeasible on the free runner, so we take a deterministic ROTATING SUBSET each cycle (by cycle index,
no state) — every route is covered over a few hours. Output matches harvest_eta (segment observations),
keyed to the app's CTB route model: agency "CTB", service "" (empty), dir "inbound"/"outbound",
stop-sequence = the CTB route-stop seq (== the app's boardIndex+1).

GMB is intentionally omitted: its real-time is sparse/frequently "ETA unavailable" (weakest coverage)
and its per-region route-id scheme is high-effort/low-yield here — revisit on a VPS with more budget.
"""
import datetime, json, urllib.request
from concurrent.futures import ThreadPoolExecutor
from segdist import implausible

CTB = "https://rt.data.gov.hk/v2/transport/citybus"
SCHEDULED = {"Scheduled Bus", "原定班次"}
MIN_SEG, MAX_SEG = 0.05, 20.0

def _get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1: return None
    return None

def _parse(s):
    try: return datetime.datetime.fromisoformat(s) if s else None
    except ValueError: return None

def _emit(run, per, co, route, service, direction, out):
    for a, b in zip(run, run[1:]):
        dt = per[b] - per[a]
        if MIN_SEG <= dt <= MAX_SEG:
            out.append({"co": co, "route": route, "st": service, "dir": direction, "a": a, "b": b, "min": round(dt, 2)})

def _chain(per, co, route, service, direction):
    """{seq: minutes} → segment observations, splitting into per-vehicle monotonic contiguous runs."""
    out = []
    seqs = sorted(per)
    if len(seqs) < 2: return out
    run = [seqs[0]]
    for s in seqs[1:]:
        prev = run[-1]
        # same bus only if contiguous, non-decreasing eta, AND not an impossible-speed stitch
        # (seg_dist has no CTB distances yet → implausible() is a no-op; auto-activates once added)
        if s == prev + 1 and per[s] >= per[prev] and not implausible(route, service, direction, prev, s, per[s] - per[prev]):
            run.append(s)
        else:
            if len(run) > 1: _emit(run, per, co, route, service, direction, out)
            run = [s]
    if len(run) > 1: _emit(run, per, co, route, service, direction, out)
    return out

def ctb_routes():
    d = _get(f"{CTB}/route/CTB")
    return sorted({r["route"] for r in (d or {}).get("data", []) if r.get("route")})

def _ctb_dir(route, direction, now):
    d = _get(f"{CTB}/route-stop/CTB/{route}/{direction}")
    stops = [(int(r["seq"]), r["stop"]) for r in (d or {}).get("data", []) if r.get("stop")]
    if len(stops) < 2: return []
    def eta(seq_stop):
        seq, stop = seq_stop
        e = _get(f"{CTB}/eta/CTB/{stop}/{route}")
        best = None
        for row in (e or {}).get("data", []):
            if row.get("eta_seq") != 1: continue
            if (row.get("rmk_en") or "").strip() in SCHEDULED: continue
            t = _parse(row.get("eta"))
            if t is not None: best = (t - now).total_seconds() / 60.0
        return (seq, best)
    per = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for seq, mins in ex.map(eta, stops):
            if mins is not None: per[seq] = mins
    return _chain(per, "CTB", route, "", direction)

def ctb_segments(routes, now):
    """Harvest a batch of CTB routes (both directions) → segment observations."""
    out = []
    for r in routes:
        for direction in ("inbound", "outbound"):
            out += _ctb_dir(r, direction, now)
    return out

def ctb_batch(cycle_index, size=15):
    """Deterministic rotating slice of the CTB route list for this cycle (covers all over ~hours)."""
    routes = ctb_routes()
    if not routes: return []
    off = (cycle_index * size) % len(routes)
    return (routes + routes)[off:off + size]
