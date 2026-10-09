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
DEDUP_SEC = 60
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


def main():
    topic = read_topic()
    if not topic:
        sys.exit("NTFY_TOPIC not found in .env")
    opened = {}  # url -> last open time
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
                    url = (ev.get("message") or "").strip()
                    age = time.time() - ev.get("time", time.time())
                    if not allowed(url):
                        log(f"ignored (not an allowed link): {url[:80]!r}")
                    elif age > MAX_AGE:
                        log(f"ignored (stale {age:.0f}s): {url}")
                    elif time.time() - opened.get(url, 0) < DEDUP_SEC:
                        log(f"ignored (duplicate within {DEDUP_SEC}s): {url}")
                    else:
                        opened[url] = time.time()
                        subprocess.run(["open", "-a", "Safari", url], timeout=10)
                        log(f"opened in Safari ({age:.1f}s after publish) [{ev.get('title', '')}]: {url}")
                raise ConnectionError("stream closed by server")
        except KeyboardInterrupt:
            return
        except Exception as e:
            log(f"connection error: {e!r}; retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
