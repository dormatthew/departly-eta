#!/usr/bin/env python3
"""One-time cleanup: remove already-stored segment cells whose value implies an impossible speed
(the express-chain stitching bug that existed before the run-split gate landed). The EMA accumulator
only dilutes such cells over time; it never removes them, so a poisoned trusted cell keeps corrupting
board->alight sums. This deletes them so they re-accumulate cleanly under the fixed harvester.

Uses the same seg_dist.json + CEIL_KMH gate as the live harvesters. Prunes emptied band/daytype/route
nodes. Safe to re-run (idempotent). Writes segment_times.json in place.
"""
import json, os
from segdist import _table, CEIL_KMH

FILE = os.path.join(os.path.dirname(__file__), "segment_times.json")

def bad(dist, m):
    if dist is None:
        return False
    if m <= 0:
        return dist > 200.0
    return (dist / 1000.0) / (m / 60.0) > CEIL_KMH

def main():
    st = json.load(open(FILE))
    dtab = _table()
    removed = removed_trusted = 0
    for key in list(st.keys()):
        parts = key.split("|")
        if len(parts) != 4:
            continue
        _, route, service, dircode = parts
        segd = dtab.get(f"{route}|{service}|{dircode}", {})
        for daytype in list(st[key].keys()):
            for band in list(st[key][daytype].keys()):
                cells = st[key][daytype][band]
                for sg in list(cells.keys()):
                    m, n = cells[sg]
                    if bad(segd.get(sg), m):
                        del cells[sg]
                        removed += 1
                        if n >= 3:
                            removed_trusted += 1
                if not cells:
                    del st[key][daytype][band]
            if not st[key][daytype]:
                del st[key][daytype]
        if not st[key]:
            del st[key]
    json.dump(st, open(FILE, "w"), separators=(",", ":"), ensure_ascii=False)
    print(f"purged {removed} impossible cells ({removed_trusted} were trusted); "
          f"{len(st)} route-dirs remain, {os.path.getsize(FILE)//1024} KB")

if __name__ == "__main__":
    main()
