#!/usr/bin/env python3
"""Shared implied-speed sanity gate for the ETA-chain harvesters.

The monotonic-run chaining infers "same vehicle" from contiguous, non-decreasing ETAs. On express /
highway routes where consecutive stops are 12-18 km apart, two DIFFERENT buses' ETAs can line up and
get stitched into one segment — recording e.g. 15 km in 2 min (~450 km/h). HK franchised buses are
speed-governed to ~70 km/h, so any inter-stop hop implying a much higher straight-line speed cannot
be a single bus and must be a run boundary. seg_dist.json holds straight-line metres between
consecutive stops, keyed exactly like segment_times.json's route part: "route|service|dir" -> {"a-b": m}.

CTB has no distances in seg_dist.json yet, so implausible() is a no-op for it (returns False) until
ctb distances are added — safe, keeps current behaviour.
"""
import json, os

CEIL_KMH = 100.0     # straight-line ceiling; >100 is unreachable for a ~70 km/h-governed bus even
                     # after ETA-minute rounding + straight-line-vs-road slack → a stitched chain.
_TABLE = None

def _table():
    global _TABLE
    if _TABLE is None:
        try:
            _TABLE = json.load(open(os.path.join(os.path.dirname(__file__), "seg_dist.json")))
        except Exception:
            _TABLE = {}
    return _TABLE

def implausible(route, service, dircode, a, b, dt_min):
    """True if hop a->b can't be one bus: distance is known AND the implied straight-line speed is too high."""
    d = _table().get(f"{route}|{service}|{dircode}", {}).get(f"{a}-{b}")
    if d is None:
        return False                       # unknown distance → don't over-reject
    if dt_min <= 0:
        return d > 200.0                   # zero time across a real gap = stitched
    return (d / 1000.0) / (dt_min / 60.0) > CEIL_KMH
