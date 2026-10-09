#!/usr/bin/env python3
"""Mac side of the monitor: open stock alerts in Safari the moment they arrive (stdlib only, Python 3.9).

Subscribes to ntfy.sh/<NTFY_TOPIC>/json (topic from ../.env) and runs `open -a Safari <url>` for each
message whose body is an Apple HK shop link with #fastbuy=. Anything else is logged and ignored.
Safari's userscript then prepares checkout; paying is always manual.

  python3 mac/autoopen.py
  AUTOOPEN_TEST_URL_PREFIX=https://example.com/ python3 mac/autoopen.py   # testing only
"""
import json, os, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

# Installed copy reads the .env next to itself (~/Library/Application Support/iphone-autoopen/),
# because launchd jobs cannot read ~/Documents without Full Disk Access.
_here = Path(__file__).resolve().parent / ".env"
ENV = _here if _here.exists() else Path("/Users/wuhonchi/Documents/iphone/.env")
PREFIXES = ["https://www.apple.com/hk/shop/"]
if os.environ.get("AUTOOPEN_TEST_URL_PREFIX"):
    PREFIXES.append(os.environ["AUTOOPEN_TEST_URL_PREFIX"])
RATE_SEC = 90     # open at most ONE link per this many seconds (global, any URL): one checkout flow at a time
MAX_AGE = 120     # on reconnect, skip messages older than this (stale stock)
READ_TIMEOUT = 90  # ntfy sends a keepalive every ~45 s; silence longer than this = dead connection


def log(msg):
    hkt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    print(f"{hkt} HKT {msg}", flush=True)


def read_topic():
    for line in ENV.read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "NTFY_TOPIC":
            return v.strip().strip("\"'")
    return ""


def allowed(url):
    return (any(url.startswith(p) for p in PREFIXES) and "#fastbuy=" in url
            and not any(c.isspace() for c in url))


def decide(url, age, now, last_open):
    """Return (action, reason) for one message. action: "open" | "ignore" | "skip"."""
    if not allowed(url):
        return "ignore", f"ignored (not an allowed link): {url[:80]!r}"
    if age > MAX_AGE:
        return "ignore", f"ignored (stale {age:.0f}s): {url}"
    if last_open is not None and now - last_open < RATE_SEC:
        return "skip", f"skipped: another flow started {now - last_open:.0f}s ago: {url}"
    return "open", ""


def open_in_safari(url, run=subprocess.run):
    """Run `open -a Safari <url>`; return (ok, detail)."""
    try:
        r = run(["open", "-a", "Safari", url], timeout=10)
    except Exception as e:
        return False, repr(e)
    rc = getattr(r, "returncode", None)
    return rc == 0, f"exit {rc}"


def handle(ev, state, now=None, run=subprocess.run, logf=None):
    """Process one ntfy message event. state = {"last_open": float|None}. Returns the action taken."""
    logf = logf or log
    now = time.time() if now is None else now
    url = (ev.get("message") or "").strip()
    age = now - ev.get("time", now)
    action, reason = decide(url, age, now, state.get("last_open"))
    if action != "open":
        logf(reason)
        return action
    ok, detail = open_in_safari(url, run)
    if ok:
        state["last_open"] = now
        logf(f"opened in Safari ({age:.1f}s after publish) [{ev.get('title', '')}]: {url}")
        return "opened"
    logf(f"FAILED to open in Safari ({detail}): {url}")
    return "failed"


def main():
    topic = read_topic()
    if not topic:
        sys.exit("NTFY_TOPIC not found in .env")
    state = {"last_open": None}
    last_id, backoff = None, 1
    log(f"listening (allowed prefixes: {', '.join(PREFIXES)})")
    while True:
        # After a drop, ask for what we missed since the last message (stale ones are skipped below).
        q = "?" + urllib.parse.urlencode({"since": last_id}) if last_id else ""
        try:
            with urllib.request.urlopen(f"https://ntfy.sh/{topic}/json{q}", timeout=READ_TIMEOUT) as r:
                log("connected")
                backoff = 1
                for raw in r:
                    ev = json.loads(raw)
                    if ev.get("event") != "message":
                        continue
                    last_id = ev.get("id") or last_id
                    handle(ev, state)
                raise ConnectionError("stream closed by server")
        except KeyboardInterrupt:
            return
        except Exception as e:
            log(f"connection error: {e!r}; retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
