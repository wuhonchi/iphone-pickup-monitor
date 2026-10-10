#!/usr/bin/env python3
"""Mac side of the monitor: open stock alerts in Safari the moment they arrive (stdlib only, Python 3.9).

Subscribes to ntfy.sh/<NTFY_TOPIC>/json (topic from ../.env) and runs `open -a Safari <url>` for each
message whose body is an Apple HK shop link with #fastbuy=. Anything else is logged and ignored.
Safari's userscript then prepares checkout; paying is always manual.

  python3 mac/autoopen.py
  AUTOOPEN_TEST_URL_PREFIX=https://example.com/ python3 mac/autoopen.py   # testing only
"""
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request
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


# Standby (埋伏) tabs: a Safari checkout tab titled "[STANDBY <part>] ..." waits with its bag preloaded.
# For an alert of that part, append #fastbuy=fire&store=<R...> to that tab's URL and bring it to the front
# instead of opening a new tab. Only if Safari is already running (never launches it).
FIRE_SCRIPT = """on run argv
  set mark to item 1 of argv
  set frag to item 2 of argv
  if application "Safari" is not running then return "none"
  tell application "Safari"
    repeat with w in windows
      repeat with t in tabs of w
        if (name of t) starts with mark then
          set u to URL of t
          set AppleScript's text item delimiters to "#"
          set base to text item 1 of u
          set AppleScript's text item delimiters to ""
          set URL of t to base & frag
          set current tab of w to t
          set index of w to 1
          activate
          return "fired"
        end if
      end repeat
    end repeat
  end tell
  return "none"
end run"""


def alert_part_store(url):
    """(part, store) from an alert link, e.g. ('MJXV4ZA/A', 'R499'); (None, None) if absent."""
    u = urllib.parse.urlsplit(url)
    part = urllib.parse.parse_qs(u.query).get("product", [None])[0]
    m = re.search(r"fastbuy=(R\d+)", u.fragment)
    return part, (m.group(1) if m else None)


def fire_standby(part, store, run=subprocess.run):
    """Fire the standby tab for `part`. Returns (status, detail): status "fired" | "none" | "error"."""
    if not part or not re.fullmatch(r"[A-Z0-9]{5}ZA/A", part):
        return "none", "no part"
    frag = "#fastbuy=fire&store=" + (store or "R0")
    try:
        r = run(["osascript", "-e", FIRE_SCRIPT, "[STANDBY " + part + "]", frag], capture_output=True, text=True, timeout=10)
    except Exception as e:
        return "error", repr(e)
    out = (getattr(r, "stdout", "") or "").strip()
    if getattr(r, "returncode", 1) != 0:
        return "error", ("exit %s %s" % (getattr(r, "returncode", None), (getattr(r, "stderr", "") or "").strip()[:120]))
    return ("fired" if out == "fired" else "none"), out


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
    if allowed(url) and age <= MAX_AGE:
        # A standby tab for this model handles the alert in place (no new tab, no global rate limit).
        part, store = alert_part_store(url)
        status, detail = fire_standby(part, store, run)
        if status == "fired":
            logf(f"fired standby tab ({age:.1f}s after publish) [{ev.get('title', '')}]: {part} {store}")
            return "fired"
        if status == "error":
            logf(f"standby fire error ({detail}); falling back to opening the link")
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
    # Ask for Safari automation permission now (macOS prompt) rather than at the first real alert.
    st, detail = fire_standby("TEST0ZA/A", None)
    log(f"standby check: {st} {detail}")
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
