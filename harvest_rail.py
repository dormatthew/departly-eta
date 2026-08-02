#!/usr/bin/env python3
"""Mine MTR heavy-rail and Light Rail headways + service hours from the live next-train feeds.

WHY. Rail is the one mode with NO published timetable anywhere: TD's GTFS excludes it, and
opendata.mtr.com.hk publishes only station lists, fares and barrier-free facilities. So the app
hardcodes `railStartMin = 315` / `railEndMin = 60` in ServiceHours -- 05:15 to 01:00, applied to
EVERY line, EVERY station, EVERY day including Sundays and public holidays -- and a rail leg whose
live fetch fails falls back to a flat 4-minute wait with no scheduled alternative at all.

But the live feed is itself a timetable oracle, exactly like the bus ETA feeds. Each call returns up
to four upcoming trains per direction with an absolute `time`, `ttnt`, `plat`, `dest`, and a `source`
flag: `-` = live, `+` = the MTR's own scheduled train. Both are worth recording, tagged:
  live gaps      -> the OBSERVED headway, including how irregular it really is
  scheduled gaps -> the published headway, which exists as a dataset nowhere else
Consecutive `time` values give the gap directly (measured TWL/CEN: 9, 7, 8 min).

COST. 120 MTR (line, station) pairs + 68 LRT stops = 188 GETs per cycle, ~15 s -- the cheapest data
in the whole system, and it needs no new endpoint, key or quota.

SERVICE HOURS come free from the same sampling: the earliest and latest departure ever observed per
(line, station, direction, day-type) converges on the real first/last train within about two weeks,
replacing three global constants with measured per-station truth.
"""
import csv, datetime, io, json, urllib.request
from concurrent.futures import ThreadPoolExecutor

MTR_LINES_CSV = "https://opendata.mtr.com.hk/data/mtr_lines_and_stations.csv"
LRT_STOPS_CSV = "https://opendata.mtr.com.hk/data/light_rail_routes_and_stops.csv"
MTR_SCHED = "https://rt.data.gov.hk/v1/transport/mtr/getSchedule.php?line={line}&sta={sta}"
LRT_SCHED = "https://rt.data.gov.hk/v1/transport/mtr/lrt/getSchedule?station_id={sid}"
HKT = datetime.timezone(datetime.timedelta(hours=8))
BANDS = [(0, 6, "0"), (6, 10, "1"), (10, 16, "2"), (16, 20, "3"), (20, 24, "4")]
MIN_GAP, MAX_GAP = 0.5, 90.0


def band(h):
    for s, e, b in BANDS:
        if s <= h < e:
            return b
    return "4"


def _get(url, tries=2):
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            pass
    return None


def _csv(url):
    try:
        with urllib.request.urlopen(url, timeout=40) as r:
            return list(csv.DictReader(io.StringIO(r.read().decode("utf-8-sig"))))
    except Exception:
        return []


def mtr_targets():
    """(line, station) pairs — 120 of them, straight from the CSV the app already bundles."""
    seen = []
    for r in _csv(MTR_LINES_CSV):
        line, sta = (r.get("Line Code") or "").strip(), (r.get("Station Code") or "").strip()
        if line and sta and (line, sta) not in seen:
            seen.append((line, sta))
    return seen


def lrt_targets():
    out = []
    for r in _csv(LRT_STOPS_CSV):
        sid = (r.get("Stop ID") or "").strip()
        if sid and sid not in out:
            out.append(sid)
    return out


def _parse(s):
    try:
        return datetime.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def mtr_one(line, sta):
    """Headway observations + observed departure minutes for one (line, station)."""
    d = _get(MTR_SCHED.format(line=line, sta=sta))
    if not d or d.get("status") != 1:
        return [], []
    gaps, mins = [], []
    for key, blob in (d.get("data") or {}).items():
        for direction in ("UP", "DOWN"):
            trains = blob.get(direction) or []
            rows = []
            for t in trains:
                ts = _parse(t.get("time") or "")
                if ts:
                    rows.append((ts, (t.get("source") or "").strip(), t.get("dest")))
            rows.sort()
            for ts, src, _ in rows:
                mins.append((f"MTR|{line}|{direction}|{sta}", ts.hour * 60 + ts.minute))
            for i in range(len(rows) - 1):
                g = (rows[i + 1][0] - rows[i][0]).total_seconds() / 60.0
                if MIN_GAP <= g <= MAX_GAP:
                    # `live` only when BOTH trains are live ('-'); a '+' pair is the published
                    # timetable, which we keep separately rather than mixing into observed reality.
                    live = rows[i][1] == "-" and rows[i + 1][1] == "-"
                    gaps.append((f"MTR|{line}|{direction}|{sta}", round(g, 2), live))
    return gaps, mins


def lrt_one(sid):
    """Light Rail is RELATIVE minutes only ('10 mins' / 'Arriving'), so gaps are +/-1 min granular."""
    d = _get(LRT_SCHED.format(sid=sid))
    if not d or not d.get("platform_list"):
        return [], []
    gaps, mins = [], []
    now = datetime.datetime.now(HKT)
    nowmin = now.hour * 60 + now.minute
    for p in d["platform_list"]:
        byroute = {}
        for r in p.get("route_list") or []:
            txt = (r.get("time_en") or "").strip().lower()
            if txt in ("arriving", "departing"):
                m = 0
            else:
                try:
                    m = int(txt.split()[0])
                except (ValueError, IndexError):
                    continue
            key = f"LRT|{r.get('route_no')}|{p.get('platform_id')}|{sid}"
            byroute.setdefault(key, []).append(m)
            mins.append((key, (nowmin + m) % 1440))
        for key, ms in byroute.items():
            ms.sort()
            for i in range(len(ms) - 1):
                g = float(ms[i + 1] - ms[i])
                if MIN_GAP <= g <= MAX_GAP:
                    gaps.append((key, g, True))     # LRT publishes no scheduled/live flag
    return gaps, mins


def harvest(workers=6):
    """One rail cycle. Returns (gaps, departure-minutes) for the caller to fold into state."""
    gaps, mins = [], []
    targets = mtr_targets()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for g, m in ex.map(lambda t: mtr_one(*t), targets):
            gaps += g
            mins += m
    stops = lrt_targets()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for g, m in ex.map(lrt_one, stops):
            gaps += g
            mins += m
    return gaps, mins


def fold(state, gaps, mins, now_hkt):
    """Merge one cycle into the running state.

    Histograms, not an EMA: they are exactly mergeable across cycles with no distributional
    assumption and no retained history, which is what lets p50/p90 be derived later. Buckets are
    1-minute up to 60, then one overflow -- rail headways above an hour are not a real case.
    """
    b, dt = band(now_hkt.hour), ("we" if now_hkt.weekday() >= 5 else "wd")
    for key, g, live in gaps:
        node = state.setdefault("hw", {}).setdefault(key, {}).setdefault(dt, {}).setdefault(b, {})
        slot = "l" if live else "s"                 # live vs scheduled, kept apart
        hist = node.setdefault(slot, [0] * 62)
        hist[min(61, int(g))] += 1
    for key, minute in mins:
        svc = state.setdefault("svc", {}).setdefault(key, {}).setdefault(dt, {})
        # Earliest/latest departure minute ever seen. Post-midnight departures (a 00:30 last train)
        # land near 0 and would corrupt a naive min, so they are folded onto the previous service day.
        m = minute + 1440 if minute < 180 else minute
        svc["f"] = min(svc.get("f", 10 ** 9), m)
        svc["l"] = max(svc.get("l", -1), m)


def summarize(hist):
    """[mean, sd, p50, p90, n] from a 1-minute histogram."""
    n = sum(hist)
    if n == 0:
        return None
    tot = sum(i * c for i, c in enumerate(hist))
    mean = tot / n
    var = sum(c * (i - mean) ** 2 for i, c in enumerate(hist)) / n
    def pct(q):
        want, run = q * n, 0
        for i, c in enumerate(hist):
            run += c
            if run >= want:
                return i
        return len(hist) - 1
    return [round(mean, 1), round(var ** 0.5, 1), pct(0.5), pct(0.9), n]


def publish(state, min_samples=3):
    """State -> the served rail_times.json shape (cells below `min_samples` are dropped)."""
    hw = {}
    for key, bydt in state.get("hw", {}).items():
        out_dt = {}
        for dt, byband in bydt.items():
            out_b = {}
            for b, slots in byband.items():
                # Prefer OBSERVED (live) headways; fall back to the operator's scheduled ones, which
                # are still far better than the hardcoded constants they replace.
                for slot in ("l", "s"):
                    s = summarize(slots.get(slot, []))
                    if s and s[4] >= min_samples:
                        out_b[b] = s + ([1] if slot == "l" else [0])   # trailing flag: observed?
                        break
            if out_b:
                out_dt[dt] = out_b
        if out_dt:
            hw[key] = out_dt
    svc = {k: {dt: [v.get("f"), v.get("l")] for dt, v in bydt.items() if v.get("f") is not None}
           for k, bydt in state.get("svc", {}).items()}
    return {"v": 1, "hw": hw, "svc": {k: v for k, v in svc.items() if v}}


if __name__ == "__main__":
    now = datetime.datetime.now(HKT)
    g, m = harvest()
    print(f"rail cycle {band(now.hour)}/{'we' if now.weekday() >= 5 else 'wd'}: "
          f"{len(g)} gaps ({sum(1 for x in g if x[2])} live), {len(m)} departure observations")
    st = {}
    fold(st, g, m, now)
    out = publish(st, min_samples=1)
    print(f"  -> {len(out['hw'])} headway keys, {len(out['svc'])} service-window keys")
    for k in list(out["hw"])[:5]:
        print(f"     {k}: {out['hw'][k]}")
