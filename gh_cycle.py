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

    Returns (data, meta). `data` is the legacy nested dict; `meta` carries publish bookkeeping.
    """
    # 1. the release asset (the real home)
    try:
        with urllib.request.urlopen(STATE_URL, timeout=60) as r:
            blob = json.loads(r.read().decode("utf-8"))
        if isinstance(blob, dict) and isinstance(blob.get("d"), dict):
            print(f"state: release asset, {len(blob['d'])} keys")
            return blob["d"], blob.get("meta", {})
    except Exception as e:
        print(f"state: no release asset ({e.__class__.__name__})")

    # 2. a local copy left by an earlier step in this same job
    blob = _load_json(STATE)
    if isinstance(blob, dict) and isinstance(blob.get("d"), dict):
        print(f"state: local file, {len(blob['d'])} keys")
        return blob["d"], blob.get("meta", {})

    # 3. bootstrap from the committed published file (first run after this change)
    legacy = _load_json(FILE)
    if isinstance(legacy, dict) and legacy:
        print(f"state: bootstrapped from committed segment_times.json, {len(legacy)} keys")
        return legacy, {}

    print("state: cold start")
    return {}, {}


def save_state(data, meta):
    with open(STATE, "w") as fh:
        json.dump({"v": 1, "meta": meta, "d": data}, fh, separators=(",", ":"), ensure_ascii=False)


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

def run_cycle(state, now):
    """Harvest one live cycle and fold it into `state`. Returns the observation count."""
    hkt = now.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    b = band(hkt.hour)
    dt = "we" if hkt.weekday() >= 5 else "wd"

    routes = all_routes()
    if not routes:
        print("  no routes fetched — skipping cycle")
        return 0

    obs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(lambda rs: segments_for(rs[0], rs[1], now), routes):
            obs.extend(res)

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

    print(f"  cycle {b}/{dt}: {len(obs)} obs / {len(routes)} routes")
    return len(obs)


# ---------------------------------------------------------------- main

def main():
    state, meta = load_state()
    deadline = time.monotonic() + LOOP_MINUTES * 60
    cycles = total_obs = 0

    while True:
        cycle_start = time.monotonic()
        try:
            total_obs += run_cycle(state, datetime.datetime.now(datetime.timezone.utc))
            cycles += 1
        except Exception as e:                     # one bad cycle must not lose the whole run
            print(f"  cycle failed: {e.__class__.__name__}: {e}")

        # Checkpoint after EVERY cycle, to the release asset as well as locally. This is what makes
        # `cancel-in-progress: true` safe: a run killed mid-loop loses at most the cycle in flight,
        # not the whole 50 minutes. A ~4 MB upload every 10 min is negligible.
        save_state(state, meta)
        publish_state()

        nxt = cycle_start + CYCLE_EVERY_S
        if nxt >= deadline:
            break
        remaining = nxt - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    segs = sum(len(bb) for v in state.values() for dd in v.values() for bb in dd.values())
    trusted = sum(1 for v in state.values() for dd in v.values() for bb in dd.values()
                  for val in bb.values() if val[1] >= 3)
    print(f"{cycles} cycles, {total_obs} obs → {len(state)} keys / {segs} segments "
          f"({trusted} trusted, n≥3)")

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
        # EXACT legacy shape — see the module docstring. Do not reformat.
        with open(FILE, "w") as fh:
            json.dump(state, fh, separators=(",", ":"), ensure_ascii=False)
        meta["last_publish"] = now.isoformat()
        print(f"published segment_times.json ({os.path.getsize(FILE)//1024} KB)")
        save_state(state, meta)      # persist the new last_publish so the next run gates correctly
        publish_state()
    else:
        print("segment_times.json left untouched this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
