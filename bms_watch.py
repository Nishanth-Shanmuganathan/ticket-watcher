#!/usr/bin/env python3
"""
bms_watch.py v2 - Watch MULTIPLE BookMyShow pages from an always-on host and
push an alarm to your phone when any of them opens for booking.

Two run modes:
    python bms_watch.py --loop     # long-running (VPS, Raspberry Pi, old phone)
    python bms_watch.py --once     # single pass, then exit (cron, GitHub Actions)

Setup:
    python bms_watch.py --inspect  # fetch every target, save baselines, report
    python bms_watch.py --test     # fire a test alert at your phone
    python bms_watch.py --reset    # clear state (alerted flags + baselines)

Targets live in targets.json. Secrets come from environment variables.
Stdlib only. Optional: pip install playwright && playwright install chromium
"""

import argparse
import gzip
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS_FILE = os.path.join(HERE, "targets.json")
STATE_FILE = os.path.join(HERE, "state.json")

# ---------------------------------------------------------------- defaults

DEFAULTS = {
    "mode": "either",                    # markers | diff | either
    "diff_similarity_threshold": 0.85,
    "open_markers": [
        "book tickets", "select seats", "seat layout", "showtimes",
        "select a cinema", "sessionid", "/seatlayout",
    ],
    "closed_markers": [
        "notify me", "coming soon", "booking opens", "book tickets soon",
        "we'll notify you",
    ],
}

LOOP_INTERVAL = int(os.environ.get("BMS_INTERVAL", "60"))   # seconds per full pass
MIN_GAP = 4.0            # minimum seconds between any two outbound requests
MAX_PARALLEL = 3
MAX_TOKENS = 5000        # cap baseline size so state.json stays small
# After this many consecutive load failures on one target, push a "watcher is
# broken" alert. Silent failure is the real risk on an unattended host.
HEALTH_ALERT_AFTER = int(os.environ.get("BMS_HEALTH_ALERT_AFTER", "5"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, identity",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

_throttle_lock = threading.Lock()
_last_request = [0.0]


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- fetching

def _throttle():
    """Global rate limit so N targets don't turn into N simultaneous hits."""
    with _throttle_lock:
        wait = _last_request[0] + MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()


def fetch_plain(url, timeout=25):
    _throttle()
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, raw.decode("utf-8", errors="replace")
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except (URLError, OSError, TimeoutError) as e:
        return 0, f"__FETCH_ERROR__ {e}"


def fetch_browser(url, timeout=45):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "__NO_PLAYWRIGHT__"
    _throttle()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            page = browser.new_page(user_agent=UA, locale="en-IN",
                                    viewport={"width": 1366, "height": 900})
            page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            html = page.content()
            browser.close()
            return 200, html
    except Exception as e:
        return 0, f"__BROWSER_ERROR__ {e}"


def fetch(url, prefer_browser=False):
    if prefer_browser:
        status, html = fetch_browser(url)
        if status == 200:
            return status, html, "browser"
    status, html = fetch_plain(url)
    if status in (403, 429, 503):
        b_status, b_html = fetch_browser(url)
        if b_status == 200:
            return b_status, b_html, "browser"
    return status, html, "plain"


# ---------------------------------------------------------------- analysis

def visible_text(html):
    txt = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = re.sub(r"&[a-z]+;|&#\d+;", " ", txt)
    return re.sub(r"\s+", " ", txt).strip().lower()


def fingerprint(html):
    tokens = re.findall(r"[a-z]{3,}", visible_text(html))
    noise = {"the", "and", "for", "you", "with", "your", "all", "are", "this", "from"}
    uniq = sorted({t for t in tokens if t not in noise})
    return set(uniq[:MAX_TOKENS])


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def setting(target, key):
    return target.get(key, DEFAULTS[key])


def analyse(target, html, baseline):
    hay = visible_text(html) + " " + html.lower()
    open_hits = [m for m in setting(target, "open_markers") if m in hay]
    closed_hits = [m for m in setting(target, "closed_markers") if m in hay]
    markers_open = bool(open_hits) and not closed_hits

    sim, diff_open = None, False
    if baseline:
        sim = jaccard(fingerprint(html), baseline)
        diff_open = sim < setting(target, "diff_similarity_threshold")

    mode = setting(target, "mode")
    if mode == "markers":
        fired = markers_open
    elif mode == "diff":
        fired = diff_open
    else:
        fired = markers_open or diff_open

    reasons = []
    if markers_open:
        reasons.append("matched: " + ", ".join(open_hits))
    if diff_open:
        reasons.append(f"page changed (similarity {sim:.3f})")

    return {"fired": fired, "open_hits": open_hits, "closed_hits": closed_hits,
            "similarity": sim, "reason": " | ".join(reasons)}


# ---------------------------------------------------------------- alerting

def notify_ntfy(title, message, url, repeat=1, gap=None):
    """ntfy has no repeat-until-acknowledged, so for real alerts we send several
    pushes spaced apart. One buzz is easy to sleep through; six is harder."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    gap = gap if gap is not None else int(os.environ.get("NTFY_REPEAT_GAP", "20"))
    sent = 0
    for i in range(max(1, repeat)):
        body = message if i == 0 else f"[{i+1}/{repeat}] {message}"
        req = Request(
            f"{server}/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "max",
                "Tags": "rotating_light",
                "Click": url,
                "Actions": f"view, Open BookMyShow, {url}",
            },
            method="POST",
        )
        try:
            urlopen(req, timeout=15).read()
            sent += 1
        except Exception as e:
            log(f"  -> ntfy push {i+1} FAILED: {e}")
        if i < repeat - 1:
            time.sleep(gap)
    if sent:
        log(f"  -> ntfy: {sent}/{repeat} push(es) sent")
    return sent > 0


def notify_pushover(title, message, url):
    """Priority 2 = Emergency: repeats every 30s for an hour until acknowledged."""
    user, token = os.environ.get("PUSHOVER_USER"), os.environ.get("PUSHOVER_TOKEN")
    if not (user and token):
        return False
    fields = {
        "token": token, "user": user, "title": title, "message": message,
        "url": url, "url_title": "Open BookMyShow",
        "priority": 2, "retry": 30, "expire": 3600,
    }
    # Pushover's Android app makes a SEPARATE notification channel per sound
    # name. Naming a sound here sends the alert into a channel the user has
    # never configured, which is silent by default even when their normal
    # notifications work. So we send no sound and inherit their default.
    # Set PUSHOVER_SOUND only after enabling that channel in Android settings.
    snd = os.environ.get("PUSHOVER_SOUND")
    if snd:
        fields["sound"] = snd
    data = urlencode(fields).encode()
    try:
        urlopen(Request("https://api.pushover.net/1/messages.json", data=data),
                timeout=15).read()
        log("  -> Pushover EMERGENCY sent (repeats until you acknowledge)")
        return True
    except Exception as e:
        log(f"  -> Pushover FAILED: {e}")
        return False


def alert(name, url, reason, kind="BOOKING OPEN"):
    title = f"{kind}: {name}"
    message = f"{reason}\n{url}"
    log("!" * 60)
    log(f"{kind} -> {name}")
    log(f"  {reason}")
    log(f"  {url}")
    log("!" * 60)
    # Repeat only for the alert that matters; a broken-watcher notice buzzes once.
    repeat = int(os.environ.get("NTFY_REPEAT", "6")) if kind == "BOOKING OPEN" else 1
    sent = notify_pushover(title, message, url)
    sent = notify_ntfy(title, message, url, repeat=repeat) or sent
    if not sent:
        log("  -> WARNING: no notification channel configured. Alert NOT delivered.")
    return sent


# ---------------------------------------------------------------- state

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def load_targets():
    targets = load_json(TARGETS_FILE, None)
    if targets is None:
        log(f"No {TARGETS_FILE}. Copy targets.example.json to targets.json and edit it.")
        sys.exit(1)
    live = [t for t in targets if t.get("enabled", True)]
    if not live:
        log("No enabled targets.")
        sys.exit(1)
    return live


def key_for(target):
    return target.get("id") or target["url"]


# ---------------------------------------------------------------- checking

def check_browser(target, entry, name):
    """Seat-layout targets: render the page and look for open-signals."""
    import bms_browser
    ok, reason = bms_browser.is_open(
        target["url"],
        wait=target.get("wait_seconds", 25),
        tag=re.sub(r"[^a-z0-9]+", "_", name.lower())[:40] or "target",
    )
    entry["last_reason"] = reason

    # A page that failed to LOAD is not the same as a page that loaded closed.
    if reason.startswith("error:"):
        entry["fails"] = entry.get("fails", 0) + 1
        if entry["fails"] >= HEALTH_ALERT_AFTER and not entry.get("health_alerted"):
            entry["health_alerted"] = True
            alert(name, target["url"],
                  f"{entry['fails']} consecutive load failures. Last: {reason}. "
                  f"You are NOT being monitored right now.",
                  kind="WATCHER BROKEN")
        return f"{name}: LOAD FAILED ({entry['fails']} in a row) | {reason}"
    entry["fails"] = 0
    entry.pop("health_alerted", None)

    if ok:
        alert(name, target["url"], reason)
        entry["alerted"] = True
        entry["alerted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return f"{name}: *** FIRED *** {reason}"
    entry["last_ok"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"{name}: closed | {reason}"


def check_one(target, state):
    k = key_for(target)
    name = target.get("name", k)
    entry = state.setdefault(k, {})

    if entry.get("alerted"):
        return f"{name}: already alerted, skipping"

    exp = target.get("expires")
    if exp and datetime.now(timezone.utc).date().isoformat() > exp:
        entry["expired"] = True
        return f"{name}: expired on {exp}, skipping"

    if target.get("check") == "browser":
        try:
            return check_browser(target, entry, name)
        except Exception as e:
            entry["fails"] = entry.get("fails", 0) + 1
            return f"{name}: browser check failed: {type(e).__name__}: {e}"

    baseline = set(entry.get("baseline", [])) or None
    status, html, via = fetch(target["url"],
                              prefer_browser=entry.get("prefer_browser", False))

    if status != 200 or len(html) < 2000:
        entry["fails"] = entry.get("fails", 0) + 1
        if entry["fails"] >= 3:
            entry["prefer_browser"] = True
        entry["last_error"] = f"HTTP {status} via {via}"
        return f"{name}: FAIL HTTP {status} via {via} (streak {entry['fails']})"

    entry["fails"] = 0
    entry.pop("last_error", None)
    entry["last_ok"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if baseline is None:
        entry["baseline"] = sorted(fingerprint(html))
        return f"{name}: baseline saved ({len(entry['baseline'])} tokens)"

    r = analyse(target, html, baseline)
    if r["fired"]:
        alert(name, target["url"], r["reason"])
        entry["alerted"] = True
        entry["alerted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry["alert_reason"] = r["reason"]
        return f"{name}: *** FIRED *** {r['reason']}"

    sim = f"{r['similarity']:.3f}" if r["similarity"] is not None else "n/a"
    return (f"{name}: ok via {via} | sim={sim} | "
            f"open={r['open_hits'] or '-'} | closed={r['closed_hits'] or '-'}")


def run_pass(targets, state):
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        for line in pool.map(lambda t: check_one(t, state), targets):
            log("  " + line)
    save_state(state)
    return sum(1 for t in targets if state.get(key_for(t), {}).get("alerted"))


# ---------------------------------------------------------------- commands

def cmd_inspect(targets, state):
    log(f"Inspecting {len(targets)} target(s)")
    os.makedirs(os.path.join(HERE, "dumps"), exist_ok=True)
    for t in targets:
        name = t.get("name", t["url"])
        status, html, via = fetch(t["url"])
        log(f"{name}: HTTP {status} via {via}, {len(html)} chars")
        safe = re.sub(r"[^a-z0-9]+", "_", name.lower())[:60] or "target"
        path = os.path.join(HERE, "dumps", f"{safe}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"  dumped -> {path}")
        if status != 200 or len(html) < 2000:
            log("  BLOCKED or empty. Open the dump. If it's a captcha/block page, "
                "install Playwright, or move to a host with a different IP.")
            continue
        r = analyse(t, html, None)
        log(f"  open_markers={r['open_hits'] or 'none'}  "
            f"closed_markers={r['closed_hits'] or 'none'}")
        if r["open_hits"] and not r["closed_hits"]:
            log("  WARNING: markers say ALREADY OPEN. Tighten open_markers for this "
                "target, or set its mode to 'diff'.")
        entry = state.setdefault(key_for(t), {})
        entry["baseline"] = sorted(fingerprint(html))
        entry.pop("alerted", None)
    save_state(state)
    log("Baselines saved. Ready.")


def cmd_loop(targets, state):
    log(f"Loop mode: {len(targets)} target(s), ~{LOOP_INTERVAL}s per pass")
    while True:
        alerted = run_pass(targets, state)
        if alerted >= len(targets):
            log("All targets alerted. Exiting.")
            return
        time.sleep(LOOP_INTERVAL + random.uniform(-4, 4))


def main():
    ap = argparse.ArgumentParser(description="Watch BookMyShow pages for booking opening.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--loop", action="store_true", help="Run continuously.")
    g.add_argument("--once", action="store_true", help="One pass, then exit (cron/CI).")
    g.add_argument("--inspect", action="store_true", help="Fetch all, dump pages, save baselines.")
    g.add_argument("--test", action="store_true", help="Send a test alert.")
    g.add_argument("--reset", action="store_true", help="Clear all saved state.")
    args = ap.parse_args()

    if args.test:
        ok = alert("TEST", "https://in.bookmyshow.com/",
                   "This is a test. If it didn't wake you, fix it now.")
        sys.exit(0 if ok else 1)

    if args.reset:
        save_state({})
        log("State cleared. Run --inspect to rebuild baselines.")
        return

    state = load_json(STATE_FILE, {})
    targets = load_targets()

    if args.inspect:
        cmd_inspect(targets, state)
    elif args.loop:
        cmd_loop(targets, state)
    else:
        log(f"Single pass over {len(targets)} target(s)")
        run_pass(targets, state)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")