"""Apple HK same-day pickup monitor (notify only, never buys).

Data source: /hk/shop/pickup-message-recommendations (answers plain requests with JSON).
It lists up to a few *other* similar models available today, so:
  - a target that appears  => really "Available Today" at that store
  - a target that is absent => unknown (list is capped), NOT "sold out"
To widen coverage each cycle queries several different products.

If Apple stops answering with JSON (block / change), the script notifies once and exits.
It does not retry around blocks.

  python3 monitor.py            # loop forever
  python3 monitor.py --once     # one cycle, print result
  python3 monitor.py --cron     # one cycle with saved state (for crontab)
"""
import fcntl, json, os, random, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
BASE = "https://www.apple.com/hk/shop/pickup-message-recommendations"
LOCATION = "hong kong"
BUY = "https://www.apple.com/hk/shop/buy-iphone/iphone-18-pro/6.9-inch-display-{}-burgundy"

# iPhone 18 Pro Max Burgundy (part numbers from apple.com/hk iPhone 18 Pro page source)
TARGETS = {
    "MJXQ4ZA/A": ("256GB", BUY.format("256gb")),
    "MJXV4ZA/A": ("512GB", BUY.format("512gb")),
    "MJY04ZA/A": ("1TB", BUY.format("1tb")),
}
# Each query hides its own product, so query every target plus non-target Pro Max parts.
QUERIES = list(TARGETS) + ["MJY44ZA/A", "MJXN4ZA/A"]  # 2TB Burgundy, 256GB Black

INTERVAL = int(os.environ.get("INTERVAL_SEC", "180"))
GAP = 3  # seconds between the requests inside one cycle


def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def notify(text):
    """Return False only if Telegram is configured and sending failed."""
    print(text, flush=True)
    ok = True
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15).read()
        except Exception as e:
            print(f"telegram failed: {e}", flush=True)
            ok = False
    else:
        print("(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; console + macOS only)", flush=True)
        if os.environ.get("GITHUB_ACTIONS"):  # nobody sees the console in CI: treat as not delivered
            ok = False
    try:
        subprocess.run(["osascript", "-e", f'display notification {json.dumps(text[:200])} with title "iPhone pickup" sound name "Glass"'], timeout=10)
    except Exception:
        pass
    return ok


class Blocked(Exception):
    pass


def fetch(product):
    q = urllib.parse.urlencode({"fae": "true", "mts.0": "regular", "location": LOCATION, "product": product})
    req = urllib.request.Request(f"{BASE}?{q}", headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, ctype, body = r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        if e.code in (500, 502, 503, 504):  # server hiccup: retry next run (541 = Apple's block page)
            raise RuntimeError(f"HTTP {e.code} for product={product}")
        raise Blocked(f"HTTP {e.code} for product={product}")
    if status != 200 or "json" not in ctype:
        raise Blocked(f"HTTP {status} {ctype} for product={product}")
    return json.loads(body)


def scan(targets=None, queries=None):
    """Return {(part, store): (label, quote)} for targets seen available today."""
    targets, queries = targets or TARGETS, queries or QUERIES
    found = {}
    for i, product in enumerate(queries):
        if i:
            time.sleep(GAP)
        pm = fetch(product)["body"]["PickupMessage"]
        for store in pm.get("stores", []):
            for part, info in store.get("partsAvailability", {}).items():
                if part in targets and info.get("pickupDisplay") == "available":
                    found[(part, store["storeName"])] = (targets[part][0], info.get("pickupSearchQuote", ""))
    return found


def fmt(found, targets=None):
    targets = targets or TARGETS
    lines = ["iPhone 18 Pro Max Burgundy 今日有得自取:"]
    for (part, store), (label, quote) in sorted(found.items()):
        lines.append(f"- {label} @ Apple {store} ({quote})\n  {targets[part][1]}")
    return "\n".join(lines)


STATE = ROOT / ".state.json"
BLOCKED_FLAG = ROOT / ".blocked"
LOCK = ROOT / ".lock"


def write_atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def cron_run():
    """One cycle for cron: alert only on new (part, store) pairs; remember state between runs."""
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{time.strftime('%F %T')} skipped: previous run still going", flush=True)
        return
    if BLOCKED_FLAG.exists():
        print(f"{time.strftime('%F %T')} skipped: {BLOCKED_FLAG.name} exists (delete it to resume)", flush=True)
        return
    try:
        prev = {tuple(x) for x in json.loads(STATE.read_text())}
    except Exception:
        prev = set()
    try:
        found = scan()
    except Blocked as e:
        write_atomic(BLOCKED_FLAG, f"{time.strftime('%F %T')} {e}\n")
        notify(f"iPhone monitor paused: Apple no longer answering normally ({e}). Check manually; delete .blocked to resume.")
        return
    new = {k: v for k, v in found.items() if k not in prev}
    remember = set(found)
    if new and not notify(fmt(new)):
        remember -= set(new)  # Telegram failed: keep them "new" so the next run alerts again
    write_atomic(STATE, json.dumps(sorted(remember)))
    print(f"{time.strftime('%F %T')} seen={len(found)} new={len(new)}", flush=True)


def main():
    load_env()
    if "--cron" in sys.argv:
        cron_run()
        return
    if "--once" in sys.argv:
        found = scan()
        print(fmt(found) if found else "No target seen this cycle (absence = unknown, list is capped).")
        return
    notified = set()
    print(f"monitoring {', '.join(v[0] for v in TARGETS.values())} every ~{INTERVAL}s", flush=True)
    while True:
        try:
            found = scan()
        except Blocked as e:
            notify(f"iPhone monitor stopped: Apple no longer answering normally ({e}). Check manually.")
            return
        except Exception as e:  # network blip: log and try next cycle
            print(f"{time.strftime('%H:%M:%S')} error: {e}", flush=True)
            found = None
        if found is not None:
            new = {k: v for k, v in found.items() if k not in notified}
            if new:
                notify(fmt(new))
            notified = set(found)  # re-alert if an item disappears and comes back
            print(f"{time.strftime('%H:%M:%S')} seen={len(found)} new={len(new)}", flush=True)
        time.sleep(INTERVAL + random.randint(0, 30))


if __name__ == "__main__":
    main()
