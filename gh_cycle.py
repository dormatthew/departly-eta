#!/usr/bin/env python3
"""Harvest cycles for the GitHub Actions runner (free public-repo deployment).

TWO THINGS CHANGED HERE, both to stop the repo growing without bound (it reached 176 MB / 385
commits in 30 days by committing a 4 MB file ~14x/day):

1. THE STATE IS NO LONGER THE COMMITTED FILE. The accumulator now lives in a RELEASE ASSET
   (`segment_state.json` on the `state` release). Release assets are replaced in place and
   contribute ZERO git history, and — unlike actions/cache — they are not evicted after 7 days,
   so a quiet fortnight can't wipe weeks of samples.

2. THE PUBLISHED FILE IS COMMITTED AT MOST EVERY `PUBLISH_EVERY_H` HOURS. Traffic medians move
   on a scale of weeks, so there is no user-visible difference, and it cuts git growth ~7x.

   ⚠️ `segment_times.json` MUST KEEP ITS EXACT PATH AND FORMAT. Shipped copies of the app
   (v1.8 / build 66 and earlier) hardcode
   `https://raw.githubusercontent.com/dormatthew/departly-eta/main/segment_times.json` in
   `SegmentTimeStore` and parse the nested `key -> wd|we -> band -> "a-b" -> [mins, count]`
   shape. Changing either the URL or the encoding silently strips observed ride times from
   every installed app. A compact re-encoding may only ever be published ALONGSIDE this file,
   under a new name, once a build that reads it is live.

Also: the job now runs MANY cycles instead of one. GitHub's job limit is 6 h; the old 8-minute
timeout was self-imposed, and scheduled workflows on free public repos actually fire only 13-17
times a day (measured over 200 runs), so a single-cycle job was covering ~3% of wall-clock.
Looping for ~50 minutes at the originally-intended 10-minute cadence lifts that to ~50% without
ever polling the free government endpoints faster than the `*/10` cron was always meant to.

Estimate update: a running MEAN for the first 10 samples (stable warm-up), then an EMA (alpha=0.15)
so it tracks recent traffic rather than being anchored to weeks-old data. Stored as
[minutes, sampleCount]; the app trusts a segment once count >= 3.
"""
import datetime, json, os, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from harvest_eta import segments_for, all_routes

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "segment_times.json")          # published, legacy format, committed
STATE = os.path.join(HERE, "segment_state.json")         # accumulator, release asset, gitignored

REPO = os.environ.get("GITHUB_REPOSITORY", "dormatthew/departly-eta")
STATE_TAG = "state"
STATE_URL = f"https://github.com/{REPO}/releases/download/{STATE_TAG}/segment_state.json"
# Two NEW derived datasets, published as release assets only. No shipped app reads them yet, so they
# carry no format-compatibility constraint (unlike segment_times.json) and add zero git history.
HEADWAYS = os.path.join(HERE, "headways.json")
RAIL = os.path.join(HERE, "rail_times.json")

LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "50"))     # in-job budget (job cap is 6 h)
CYCLE_EVERY_S = int(os.environ.get("CYCLE_EVERY_S", "600"))  # 10 min between cycle STARTS
PUBLISH_EVERY_H = float(os.environ.get("PUBLISH_EVERY_H", "12"))

BANDS = [(0, 6, "0"), (6, 10, "1"), (10, 16, "2"), (16, 20, "3"), (20, 24, "4")]
MAX_SEG = 20.0


def band(h):
    for s, e, b in BANDS:
        if s <= h < e:
            return b
    return "4"


# ---------------------------------------------------------------- state I/O

def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def load_state():
    """Release asset -> local file -> the committed legacy file (bootstrap) -> empty.

    Returns the whole state blob: `d` = the legacy segment estimates (and ONLY those — this is what
    becomes segment_times.json, so nothing else may ever be mixed into it), `hwbus` / `rail` = the
    newer derived layers, `meta` = publish bookkeeping.
    """
    # 1. the release asset (the real home)
    try:
        with urllib.request.urlopen(STATE_URL, timeout=60) as r:
            blob = json.loads(r.read().decode("utf-8"))
        if isinstance(blob, dict) and isinstance(blob.get("d"), dict):
            print(f"state: release asset, {len(blob['d'])} segment keys, "
                  f"{len(blob.get('hwbus', {}))} bus-headway keys")
            return blob
    except Exception as e:
        print(f"state: no release asset ({e.__class__.__name__})")

    # 2. a local copy left by an earlier step in this same job
    blob = _load_json(STATE)
    if isinstance(blob, dict) and isinstance(blob.get("d"), dict):
        print(f"state: local file, {len(blob['d'])} segment keys")
        return blob

    # 3. bootstrap from the committed published file (first run after this change)
    legacy = _load_json(FILE)
    if isinstance(legacy, dict) and legacy:
        print(f"state: bootstrapped from committed segment_times.json, {len(legacy)} keys")
        return {"v": 1, "meta": {}, "d": legacy}

    print("state: cold start")
    return {"v": 1, "meta": {}, "d": {}}


def save_state(blob):
    blob["v"] = 1
    with open(STATE, "w") as fh:
        json.dump(blob, fh, separators=(",", ":"), ensure_ascii=False)


def publish_state():
    """Upload the accumulator as a release asset. Zero git history."""
    if not os.environ.get("GITHUB_ACTIONS"):
        print("state: not on Actions, skipping upload")
        return
    subprocess.run(["gh", "release", "create", STATE_TAG, "--notes",
                    "Harvester accumulator. Not for app consumption."],
                   capture_output=True)          # no-op if it already exists
    r = subprocess.run(["gh", "release", "upload", STATE_TAG, STATE, "--clobber"],
                       capture_output=True, text=True)
    print("state: uploaded" if r.returncode == 0
          else f"state: upload FAILED {r.stderr.strip()[:200]}")


# ---------------------------------------------------------------- one cycle

def _fold_hist(node, value, cap=121):
    """Fold one observation into a 1-minute histogram. Histograms merge exactly across cycles with no
    distributional assumption and no retained history — which is what lets p50/p90 be derived later,
    and is why this is not an EMA like the segment estimates."""
    hist = node.setdefault("h", [0] * cap)
    hist[min(cap - 1, int(value))] += 1


def run_cycle(blob, now):
    """Harvest one live cycle and fold it into `state`. Returns the observation count."""
    hkt = now.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    b = band(hkt.hour)
    dt = "we" if hkt.weekday() >= 5 else "wd"

    routes = all_routes()
    if not routes:
        print("  no routes fetched — skipping cycle")
        return 0

    obs = []
    hw = []                                        # observed successive-bus gaps, same payloads
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(lambda rs: segments_for(rs[0], rs[1], now, _sink=hw), routes):
            obs.extend(res)

    # BUS HEADWAYS — zero extra HTTP calls: they come out of the route-eta payloads already fetched
    # above, whose eta_seq 2/3 rows this harvester used to discard. Aggregated at ROUTE-DIRECTION
    # level (not per stop): per-stop is real, since bunching grows downstream, but costs ~3.4M numbers
    # against 179k, and the median across a route's stops damps a single bunched stop without
    # discarding the route.
    for o in hw:
        node = (blob.setdefault("hwbus", {})
                     .setdefault(f"{o['co']}|{o['route']}|{o['st']}|{o['dir']}", {})
                     .setdefault(dt, {}).setdefault(b, {}))
        _fold_hist(node, o["gap"])

    # RAIL — 188 GETs (~37 s measured). The only source of MTR/LRT headways and real first/last-train
    # times in existence: TD's GTFS excludes rail and MTR publishes no timetable.
    try:
        import harvest_rail
        rg, rm = harvest_rail.harvest()
        harvest_rail.fold(blob.setdefault("rail", {}), rg, rm, hkt)
        print(f"  rail: {len(rg)} gaps ({sum(1 for x in rg if x[2])} live), {len(rm)} departures")
    except Exception as e:
        print(f"  rail skip: {e.__class__.__name__}: {e}")

    # Rotating CTB slice (per-stop API is heavy → a subset each cycle; covers all routes over ~hours).
    try:
        from harvest_stopwise import ctb_batch, ctb_segments
        cyc = (hkt.hour * 60 + hkt.minute) // 10
        batch = ctb_batch(cyc, size=15)
        if batch:
            ctb_obs = ctb_segments(batch, now)
            obs.extend(ctb_obs)
            print(f"  CTB batch {len(batch)} routes → {len(ctb_obs)} obs")
    except Exception as e:
        print("  CTB skip:", e)

    # Segments fold into blob["d"] and NOWHERE ELSE: that dict is published verbatim as
    # segment_times.json, which shipped apps parse, so a stray key here would corrupt every install.
    state = blob.setdefault("d", {})
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

    print(f"  cycle {b}/{dt}: {len(obs)} obs / {len(routes)} routes, {len(hw)} headway gaps")
    return len(obs)


def _summarize(hist):
    """[mean, sd, p50, p90, n] from a 1-minute histogram."""
    n = sum(hist)
    if n == 0:
        return None
    mean = sum(i * c for i, c in enumerate(hist)) / n
    var = sum(c * (i - mean) ** 2 for i, c in enumerate(hist)) / n
    def pct(q):
        want, run = q * n, 0
        for i, c in enumerate(hist):
            run += c
            if run >= want:
                return i
        return len(hist) - 1
    return [round(mean, 1), round(var ** 0.5, 1), pct(0.5), pct(0.9), n]


def _upload(path):
    if not os.environ.get("GITHUB_ACTIONS") or not os.path.exists(path):
        return
    subprocess.run(["gh", "release", "upload", STATE_TAG, path, "--clobber"], capture_output=True)


def write_headways(blob, min_samples=5):
    """Observed bus headways -> headways.json, as [mean, sd, p50, p90, n] per key/day-type/band.

    Five numbers, not one, because the average wait for a rider arriving at random is NOT headway/2
    when headways vary — the inspection paradox gives E[W] = (mean/2)(1 + cv^2). For a bunched route
    with mean 14.8 and sd 7.2 that is 9.2 min, not 7.4: every `headway/2` in the engine systematically
    FLATTERS unreliable routes, which is exactly the ranking dishonesty this dataset exists to fix.
    """
    out = {}
    for key, bydt in blob.get("hwbus", {}).items():
        dt_out = {}
        for dt, byband in bydt.items():
            b_out = {}
            for b, node in byband.items():
                s = _summarize(node.get("h", []))
                if s and s[4] >= min_samples:
                    b_out[b] = s
            if b_out:
                dt_out[dt] = b_out
        if dt_out:
            out[key] = dt_out
    with open(HEADWAYS, "w") as fh:
        json.dump({"v": 1, "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "d": out}, fh, separators=(",", ":"))
    print(f"headways.json: {len(out)} route-dirs (n>={min_samples})")
    _upload(HEADWAYS)


def write_rail(blob):
    try:
        import harvest_rail
        out = harvest_rail.publish(blob.get("rail", {}))
    except Exception as e:
        print(f"rail publish skip: {e.__class__.__name__}: {e}")
        return
    with open(RAIL, "w") as fh:
        json.dump({**out, "generated": datetime.datetime.now(datetime.timezone.utc).isoformat()},
                  fh, separators=(",", ":"))
    print(f"rail_times.json: {len(out.get('hw', {}))} headway keys, {len(out.get('svc', {}))} windows")
    _upload(RAIL)


# ---------------------------------------------------------------- main

def main():
    blob = load_state()
    meta = blob.setdefault("meta", {})
    deadline = time.monotonic() + LOOP_MINUTES * 60
    cycles = total_obs = 0

    while True:
        cycle_start = time.monotonic()
        try:
            total_obs += run_cycle(blob, datetime.datetime.now(datetime.timezone.utc))
            cycles += 1
        except Exception as e:                     # one bad cycle must not lose the whole run
            print(f"  cycle failed: {e.__class__.__name__}: {e}")

        # Checkpoint after EVERY cycle, to the release asset as well as locally. This is what makes
        # `cancel-in-progress: true` safe: a run killed mid-loop loses at most the cycle in flight,
        # not the whole loop.
        save_state(blob)
        publish_state()

        nxt = cycle_start + CYCLE_EVERY_S
        if nxt >= deadline:
            break
        remaining = nxt - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    state = blob.get("d", {})
    segs = sum(len(bb) for v in state.values() for dd in v.values() for bb in dd.values())
    trusted = sum(1 for v in state.values() for dd in v.values() for bb in dd.values()
                  for val in bb.values() if val[1] >= 3)
    print(f"{cycles} cycles, {total_obs} obs → {len(state)} keys / {segs} segments "
          f"({trusted} trusted, n≥3)")

    # ---- derived datasets: published EVERY run as release assets ---------------------
    # These are new, so no shipped app reads them and there is no format-compatibility constraint;
    # being release assets they also add zero git history, so they can refresh as often as we like.
    write_headways(blob)
    write_rail(blob)

    # ---- publish the legacy file only every PUBLISH_EVERY_H hours -------------------
    now = datetime.datetime.now(datetime.timezone.utc)
    last = meta.get("last_publish")
    due = True
    if last:
        try:
            age_h = (now - datetime.datetime.fromisoformat(last)).total_seconds() / 3600
            due = age_h >= PUBLISH_EVERY_H
            print(f"published {age_h:.1f} h ago; {'due' if due else 'not due'} "
                  f"(every {PUBLISH_EVERY_H} h)")
        except Exception:
            pass

    if due:
        # EXACT legacy shape, and blob["d"] ONLY — see the module docstring. Do not reformat, and
        # never serialise the whole blob here: the newer layers must not reach shipped parsers.
        with open(FILE, "w") as fh:
            json.dump(state, fh, separators=(",", ":"), ensure_ascii=False)
        meta["last_publish"] = now.isoformat()
        print(f"published segment_times.json ({os.path.getsize(FILE)//1024} KB)")
        save_state(blob)             # persist the new last_publish so the next run gates correctly
        publish_state()
    else:
        print("segment_times.json left untouched this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
