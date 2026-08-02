# departly-eta — observed per-segment bus travel times (free GitHub Actions deployment)

A tiny, self-contained data service for [Departly](https://apps.apple.com/hk/app/id6780520462). It
mines Hong Kong's **free live route-ETA feeds** to learn the **real, time-of-day, per-segment travel
time** of each bus route — the thing the GTFS timetable can't give (it's time-invariant and only
times a route's origin + terminus). Everything runs on **free public-repo GitHub Actions**; the output
`segment_times.json` is served over this repo's public raw URL. No server, no database, no secrets.

## How it works

The `route-eta` feed predicts arrival at **every stop** for each upcoming bus. There's no vehicle ID,
but walking up the stop sequence the soonest ETA forms a **piecewise-increasing chain** — each
contiguous non-decreasing run is one physical bus's remaining stops, and `eta[next] − eta[this]` is
that bus's **real current travel time** for that segment. `"Scheduled Bus"` rows (timetable only, no
traffic signal) are skipped. `gh_cycle.py` harvests a cycle every ~10 min and folds each observation
into a running estimate (running mean → EMA).

**Superseded belief (corrected 2026-08-02):** this used to say the workflow harvests *one* cycle per
run and that `segment_times.json` is both the state and the output. Both were true and both were
costly. Measured over 200 runs (2026-07-19 → 08-02), GitHub fires a scheduled workflow on a free
public repo only **13–17 times a day** — not 144 — so one-cycle runs covered ~3% of wall-clock. And
committing a 4 MB accumulator ~14×/day grew the repo to **176 MB in 30 days**. So now:

- the job **loops for ~50 min** per run at the same 10-min cadence (GitHub's job cap is 6 h; the old
  8-min timeout was self-imposed) → ~6–7× the coverage, without ever polling the free government
  endpoints faster than the `*/10` cron always intended;
- the accumulator lives in the **`state` release asset**, checkpointed after every cycle. Release
  assets are replaced in place and add **zero** git history, and unlike `actions/cache` they are not
  evicted after 7 days;
- `segment_times.json` is committed **at most every 12 h**. Traffic medians move on a scale of weeks,
  so there is no user-visible difference, and git growth drops ~7×.

Proven on live data: route 1 segments ranged 1.1→6.6 min, 68X 0.5→7.7 min — real congestion the
schedule is blind to.

### `segment_times.json` format

⚠️ **Frozen contract.** Shipped copies of the app (v1.8 / build 66 and earlier) hardcode the raw URL
below *and* this exact nesting in `SegmentTimeStore`. Changing the path or the encoding silently
strips observed ride times from every installed app. A compact re-encoding may only be published
**alongside** this file, under a new name, once a build that reads it is live.

```json
{ "KMB|1|1|O": { "wd": { "3": { "8-9": [6.4, 27] } } } }
```
`operator|route|serviceType|direction` → day-type (`wd`/`we`) → time-band
(`0` 00–06 · `1` 06–10 · `2` 10–16 · `3` 16–20 · `4` 20–24) → `stopSeqA-stopSeqB` → `[medianMinutes, sampleCount]`.
The app trusts a segment once `sampleCount ≥ 3`.

## Setup (one time — ~2 minutes)

```bash
cd "departly-eta"
git init && git add -A && git commit -m "departly-eta harvester"
gh repo create departly-eta --public --source . --push     # or create the public repo on github.com and push
```
Then on GitHub → the repo → **Actions** tab → enable workflows. It starts polling automatically every
~10 min (also runnable on demand via **Run workflow**). Because the repo is **public**, Actions minutes
are unlimited and the data URL is public:

```
https://raw.githubusercontent.com/<your-user>/departly-eta/main/segment_times.json
```

Only the harvester code + the derived travel-time data are public (both are benign derivatives of
open government data). Your app source stays in its private repo.

## Data maturity

Bands are 4–6 h wide, so even a ~10-min best-effort cadence gives plenty of samples per band within
**~1–2 weeks**, after which most busy segments have `count ≥ 3`. Rare routes/late-night bands fill in
more slowly; the app falls back to the GTFS scheduled speed for anything not yet trusted.

## App integration (next step, ships in an app build)

Add a `SegmentTimeStore` (mirrors `ScheduleStore`) that fetches this `segment_times.json` via the
existing OTA/RemoteDataManager path. In `JourneyEngine+Live.refineRideTimes`, for a bus leg compute the
planned time-band + day-type and **sum the observed segments** covering board→alight when they're
trusted (best), else the GTFS scheduled speed (current baseline), else live driving. Same
`rideMinsOverride` path → flows straight into the Best/Bus/Train ranking.

## Roadmap
- Extend the harvester to **Citybus** (`rt.data.gov.hk` CTB ETA) and **GMB**.
- If you outgrow the best-effort GitHub schedule, move `gh_cycle.py` to a $4 VPS cron (every 2 min) or
  Cloudflare Workers — the file format and app integration stay identical.
