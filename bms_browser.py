#!/usr/bin/env python3
"""
bms_browser.py - Decide whether a BookMyShow seat-layout page is really OPEN.

Based on observed behaviour:
  OPEN  -> seat grid renders behind the modal; "How many seats?" shows the 1-10
           options, a price, a category (ELITE etc), showtime chips, and the
           Available/Sold legend.
  SHUT  -> modal renders but the quantity options never arrive; a spinner sits
           there forever. Page behind is empty. No price, no legend, no chips.

The divergence is visible ON LOAD, so this NEVER CLICKS ANYTHING. It cannot
accidentally hold seats or start a booking.

    python bms_browser.py check  --url "<url>"           # OPEN or CLOSED
    python bms_browser.py probe  --open "<u>" --closed "<u>"   # compare two
    python bms_browser.py check  --url "<url>" --show    # watch it happen

Requires: pip install playwright && playwright install chromium
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ARTIFACTS = os.path.join(HERE, "probe")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# "Bestseller" is deliberately absent: it appears on the CLOSED page too
# ("Book the Bestseller Seats in this cinema at no extra cost!").
LEGEND_WORDS = ["available", "sold", "selected"]
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(am|pm)\b", re.I)
PRICE_RE = re.compile(r"(₹|\bRs\.?\s?)\s?\d")
CATEGORY_RE = re.compile(r"\b(elite|recliner|prime|classic|platinum|gold|silver|"
                         r"executive|balcony|premium|lounge)\b", re.I)

JS_SIGNALS = """
() => {
  const digits = new Set(['1','2','3','4','5','6','7','8','9','10']);
  const scope = document.querySelector('[role="dialog"]') || document.body;
  let qty = 0;
  for (const el of scope.querySelectorAll('*')) {
    if (el.children.length) continue;
    const t = (el.textContent || '').trim();
    if (digits.has(t)) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) qty++;
    }
  }
  return {
    qty_options: qty,
    body_text: (document.body.innerText || '').slice(0, 12000),
    body_len: (document.body.innerText || '').length,
    has_dialog: !!document.querySelector('[role="dialog"]'),
  };
}
"""


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def score(sig):
    """Turn raw page signals into an OPEN/CLOSED verdict."""
    text = sig.get("body_text", "")
    low = text.lower()

    checks = {
        "quantity options rendered": sig.get("qty_options", 0) >= 3,
        "price shown": bool(PRICE_RE.search(text)),
        "seat category named": bool(CATEGORY_RE.search(text)),
        "showtime chips present": len(TIME_RE.findall(text)) >= 1,
        "available/sold legend": sum(w in low for w in LEGEND_WORDS) >= 2,
    }
    hits = [k for k, v in checks.items() if v]

    # Quantity options are the single most reliable tell: on the closed page
    # that region is a spinner. Anything else needs corroboration.
    is_open = checks["quantity options rendered"] or len(hits) >= 2
    return is_open, checks, hits


def observe(url, wait=25, headless=True, tag="check", poll=1.0):
    """Load the page and watch for open-signals. Returns as soon as it's sure."""
    from playwright.sync_api import sync_playwright

    os.makedirs(ARTIFACTS, exist_ok=True)
    out = {"url": url, "tag": tag, "error": None, "elapsed": None,
           "signals": {}, "checks": {}, "hits": [], "is_open": False}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(user_agent=UA, locale="en-IN",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            deadline = wait
            waited = 0.0
            sig, is_open, checks, hits = {}, False, {}, []
            while waited < deadline:
                page.wait_for_timeout(int(poll * 1000))
                waited += poll
                try:
                    sig = page.evaluate(JS_SIGNALS)
                except Exception:
                    continue
                is_open, checks, hits = score(sig)
                if is_open:
                    break
            out.update(signals={k: v for k, v in sig.items() if k != "body_text"},
                       checks=checks, hits=hits, is_open=is_open,
                       elapsed=round(waited, 1), text=sig.get("body_text", "")[:4000])
            shot = os.path.join(ARTIFACTS, f"{tag}.png")
            page.screenshot(path=shot)
            out["screenshot"] = shot
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
        finally:
            browser.close()

    with open(os.path.join(ARTIFACTS, f"{tag}.json"), "w") as f:
        json.dump(out, f, indent=1)
    return out


def is_open(url, wait=25, headless=True, tag="check"):
    """Entry point used by bms_watch.py. Returns (bool, reason)."""
    r = observe(url, wait=wait, headless=headless, tag=tag)
    if r["error"]:
        return False, f"error: {r['error']}"
    if r["is_open"]:
        return True, "signals: " + ", ".join(r["hits"])
    missing = [k for k, v in r["checks"].items() if not v]
    return False, f"no open-signals after {r['elapsed']}s (missing: {', '.join(missing)})"


def report(r, label):
    print(f"\n--- {label} ---")
    print(f"  {r['url']}")
    if r["error"]:
        print(f"  ERROR: {r['error']}")
        return
    print(f"  verdict: {'OPEN' if r['is_open'] else 'CLOSED'}  (after {r['elapsed']}s)")
    print(f"  qty options rendered: {r['signals'].get('qty_options')}   "
          f"body text length: {r['signals'].get('body_len')}")
    for k, v in r["checks"].items():
        print(f"    [{'x' if v else ' '}] {k}")
    print(f"  screenshot: {r.get('screenshot')}")


def cmd_check(a):
    r = observe(a.url, wait=a.wait, headless=not a.show, tag="check")
    report(r, "CHECK")
    sys.exit(0 if r["is_open"] else 2)


def cmd_probe(a):
    o = observe(a.open, wait=a.wait, headless=not a.show, tag="open")
    c = observe(a.closed, wait=a.wait, headless=not a.show, tag="closed")
    report(o, "KNOWN OPEN")
    report(c, "KNOWN CLOSED")
    ok = o["is_open"] and not c["is_open"]
    print("\n" + ("PASS - detection separates the two correctly."
                  if ok else
                  "FAIL - check probe/*.png. Detection needs tuning before you "
                  "trust it."))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--url", required=True)
    c.add_argument("--wait", type=int, default=25)
    c.add_argument("--show", action="store_true")
    c.set_defaults(func=cmd_check)
    p = sub.add_parser("probe")
    p.add_argument("--open", required=True)
    p.add_argument("--closed", required=True)
    p.add_argument("--wait", type=int, default=25)
    p.add_argument("--show", action="store_true")
    p.set_defaults(func=cmd_probe)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
