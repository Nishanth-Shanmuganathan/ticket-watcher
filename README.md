# BookMyShow multi-show watcher

Watches several BookMyShow pages from an always-on host and pushes a phone alarm
the moment any of them opens for booking.

## Files
| File | Purpose |
|---|---|
| `bms_watch.py` | The watcher |
| `bms_browser.py` | Playwright open/closed detection |
| `targets.example.json` | Copy to `targets.json`, put your shows here |
| `.github/workflows/watch.yml` | Free hosting via GitHub Actions (5-min floor) |
| `bms-watch.service` | systemd unit for a VPS / Raspberry Pi |

## 1. Notification channel (do this first)

**ntfy** — free, no signup. Install the ntfy app, subscribe to an unguessable
topic, set `NTFY_TOPIC`. In the app: enable the topic's *Instant delivery* and add
it to Android's notification exceptions so it beats Do Not Disturb.

**Pushover** — $5 one-time, and the better choice here. The script sends
**priority 2 (Emergency)**: your phone repeats the alert every 30 seconds for a
full hour until you physically acknowledge it. That is a real alarm. Set
`PUSHOVER_USER` and `PUSHOVER_TOKEN`.

Configure either or both, then prove it works:

    python bms_watch.py --test

## 2. Targets

    cp targets.example.json targets.json    # then edit

Each entry needs `id`, `name`, `url`. Optional per-target overrides: `mode`
(`markers` / `diff` / `either`), `open_markers`, `closed_markers`,
`diff_similarity_threshold`, `enabled`.

## 3. Baseline

    python bms_watch.py --inspect

Fetches every target, writes `dumps/*.html`, saves a fingerprint of each page in
its closed state. **Open the dumps.** If you see a captcha or block page instead
of the real listing, your host's IP is being filtered — see Hosting below.

## 4. Run

    python bms_watch.py --loop    # always-on host
    python bms_watch.py --once    # cron / CI

Once a target fires it's marked `alerted` and skipped. `--reset` clears
everything; deleting one target's block from `state.json` re-arms just that one.

## What the session segment in the URL actually does

    /movies/chen/seat-layout/ET00480917/INPR/63998/20260806
                              ^event     ^venue ^session ^date

Observed behaviour, same session id `63998`:

| Date | Result | Showtime it landed on |
|---|---|---|
| 20260805 | seat layout renders | 07:45 PM |
| 20260806 | seat layout renders | 09:10 AM |
| 20260807 | modal spins forever | - |

The same id resolves to a *different showtime* on each date, so it is not a
handle on one specific show. BookMyShow resolves the actual show from the date.
That means the spinner on the 7th means "nothing bookable on this date yet", and
the URL should start resolving on its own when the date opens. Watching a
hand-edited date URL is therefore fine.

Worth confirming once, with a deliberately invalid session id on a date you know
is open:

    .../seat-layout/ET00480917/INPR/99999/20260806

If it still renders, the segment is ignored entirely and the URL is a pure date
probe. If it spins, the id matters but is stable across dates. Either way the
watcher config is unchanged.

**Caveat:** you cannot control *which* showtime it lands on. The alert tells you
the date is live; pick your own showtime from the chips when you click through.
If you need one specific showtime confirmed, watch the buytickets page instead:

    https://in.bookmyshow.com/movies/chennai/<slug>/buytickets/<eventCode>/<YYYYMMDD>

## Browser check mode

Set `"check": "browser"` on a target and `bms_browser.py` renders the page with
Playwright and scores five signals:

| Signal | Open | Closed |
|---|---|---|
| 1-10 quantity options rendered | yes | spinner only |
| price (Rs / category) | yes | absent |
| seat category (ELITE, RECLINER...) | yes | absent |
| showtime chips (07:45 PM) | yes | absent |
| Available/Sold legend | yes | absent |

Open if the quantity options render, or any two other signals fire. "Bestseller"
is ignored on purpose - it appears on the closed page too.

**It never clicks anything.** The divergence is visible on load, so there is no
risk of holding seats or starting a booking.

Verify before trusting it, using a date you know is open and one you know is not:

    python bms_browser.py probe --open "<open url>" --closed "<closed url>" --show

`--show` runs headed so you can watch. It prints PASS only if it classifies both
correctly. Tune before relying on it.

## Hosting: GitHub Actions (free, no hardware)

1. Create a **public** repo and push these files. Public repos get unlimited
   Actions minutes; private repos get 2000/month, and a 5-minute cadence with
   Chromium will exceed that. Nothing secret lives in the repo - the URLs are
   public pages and credentials go in Secrets, which stay private either way.
2. Settings -> Secrets and variables -> Actions. Add `PUSHOVER_USER` +
   `PUSHOVER_TOKEN`, or `NTFY_TOPIC`.
3. `cp targets.example.json targets.json`, set your URL and an `expires` date.
4. Actions tab -> BMS Watch -> **Run workflow**. Do this manually first.
5. Open the run log. Confirm the target reports `closed | no open-signals`
   rather than `LOAD FAILED`. `LOAD FAILED` means the runner cannot reach the
   page - download the `probe-*` artifact and look at the screenshot.
6. Once a manual run looks right, the 5-minute cron takes over.

Expectations: the cron floor is 5 minutes and GitHub deprioritises scheduled
jobs, so 5-15 minute latency is normal. Fine for date-level watching, too slow
for a seconds-long ticket rush.

### Guardrails for unattended running

- **`expires`** on each target - past that date the target is skipped, so a
  forgotten watcher stops burning minutes.
- **Health alerting** - after `HEALTH_ALERT_AFTER` (default 5) consecutive load
  failures you get a `WATCHER BROKEN` push. Silent failure is the real risk when
  nobody is watching the logs; this converts it into a loud one.
- **Screenshot artifacts** - every run uploads `probe/`, kept 2 days. This is
  how you diagnose a target that reports closed when you know it is open.
- **Chromium cache** - keyed `playwright-chromium-v1`, keeps runs near a minute.

### If the runner gets blocked

Datacenter IPs may be treated differently from a home connection - unverified
either way, so test with a manual run before trusting it. If you see
`LOAD FAILED` or a captcha in the artifact screenshot, free fallbacks with
different IP ranges: Oracle Cloud Always Free (a real 24/7 VM, use `--loop` and
the systemd unit) or Google Cloud Run + Cloud Scheduler.

## Behaviour notes

- `MIN_GAP = 4.0` rate-limits globally, so 5 targets is 5 staggered requests per
  pass, not a burst. Don't lower `BMS_INTERVAL` below 30 — getting your IP
  blocked mid-race is worse than polling slower.
- Default mode `either` fires on marker match **or** significant page change.
  That trades occasional false alarms for not missing the real one. Switch a
  noisy target to `"mode": "markers"` and tighten its markers from the dump.
- Also press BookMyShow's own "Notify Me". Slow and unreliable, but free backup.
