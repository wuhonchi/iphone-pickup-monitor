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
import concurrent.futures, fcntl, json, os, random, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
BASE = "https://www.apple.com/hk/shop/pickup-message-recommendations"
BAG_URL = "https://www.apple.com/hk/shop/bag"
LOCATION = "hong kong"
# Same query params the store page itself sends on Add to Bag (full price, no AppleCare), minus the session token.
BUY = ("https://www.apple.com/hk/shop/buy-iphone/iphone-18-pro/6.9-inch-display-{cap}-{color}"
       "?product={part}&purchaseOption=fullPrice&step=select&acpart=none")


def buy_url(cap, part, color="burgundy"):
    return BUY.format(cap=cap, color=color, part=part.replace("/", "%2F"))

# iPhone 18 Pro Max (part numbers from apple.com/hk iPhone 18 Pro page source)
TARGETS = {
    "MJXQ4ZA/A": ("256GB Burgundy", buy_url("256gb", "MJXQ4ZA/A")),
    "MJXV4ZA/A": ("512GB Burgundy", buy_url("512gb", "MJXV4ZA/A")),
    "MJY04ZA/A": ("1TB Burgundy", buy_url("1tb", "MJY04ZA/A")),
    "MJXN4ZA/A": ("256GB Black", buy_url("256gb", "MJXN4ZA/A", "black")),
}
# Each query hides its own product, so query every target plus non-target Pro Max parts.
QUERIES = list(TARGETS) + ["MJY44ZA/A", "MJXP4ZA/A"]  # 2TB Burgundy, 256GB Silver

INTERVAL = int(os.environ.get("INTERVAL_SEC", "180"))
GAP = 3  # seconds between the requests inside one cycle (sequential mode)
# Burst mode: inside one cron minute, poll every BURST_EVERY s for BURST_FOR s, queries in parallel.
# Only during BURST_WINDOW (HKT) -- stock was seen only 07:27-08:46 HKT on 10/08-10/09 (state commits / run logs).
BURST_WINDOW = os.environ.get("BURST_WINDOW", "07:00-09:30")
BURST_EVERY = int(os.environ.get("BURST_EVERY", "5"))
BURST_FOR = int(os.environ.get("BURST_FOR", "52"))


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
    clean = lambda v: (v or "").strip().strip("\"'").strip()
    token, chat = clean(os.environ.get("TELEGRAM_BOT_TOKEN")), clean(os.environ.get("TELEGRAM_CHAT_ID"))
    if token and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15).read()
        except Exception as e:
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = json.loads(e.read()).get("description", "")
                except Exception:
                    pass
            print(f"telegram failed: {e} {detail}", flush=True)
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


def _hits(pm, targets):
    hits = {}
    for store in pm.get("stores", []):
        for part, info in store.get("partsAvailability", {}).items():
            if part in targets and info.get("pickupDisplay") == "available":
                hits[(part, store["storeName"])] = (targets[part][0], info.get("pickupSearchQuote", ""), store.get("storeNumber", ""))
    return hits


def scan_parallel(targets=None, queries=None, on_hit=None):
    """Same result as scan(), but all queries at once (no GAP)."""
    targets, queries = targets or TARGETS, queries or QUERIES
    found = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(queries)) as ex:
        futs = [ex.submit(fetch, q) for q in queries]
        for f in concurrent.futures.as_completed(futs):
            hits = _hits(f.result()["body"]["PickupMessage"], targets)
            found.update(hits)
            if hits and on_hit:
                on_hit(hits)
    return found


def in_burst_window(now=None):
    hkt = time.gmtime((now or time.time()) + 8 * 3600)
    hm = f"{hkt.tm_hour:02d}:{hkt.tm_min:02d}"
    start, end = BURST_WINDOW.split("-")
    return start <= hm < end


def scan(targets=None, queries=None, on_hit=None):
    """Return {(part, store): (label, quote)} for targets seen available today.

    on_hit(hits) is called right after each query that saw targets, so alerts
    need not wait for the remaining queries.
    """
    targets, queries = targets or TARGETS, queries or QUERIES
    found = {}
    for i, product in enumerate(queries):
        if i:
            time.sleep(GAP)
        hits = _hits(fetch(product)["body"]["PickupMessage"], targets)
        found.update(hits)
        if hits and on_hit:
            on_hit(hits)
    return found


def fmt(found, targets=None):
    targets = targets or TARGETS
    hkt = time.strftime("%H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    lines = [f"iPhone 18 Pro Max 今日有得自取 (見到 {hkt} HKT，未留貨):"]
    for (part, store), (label, quote, *rest) in sorted(found.items()):
        num = rest[0] if rest else ""
        link = targets[part][1] + (f"#fastbuy={num}" if num else "")
        lines.append(f"- {label} @ Apple {store} ({quote})\n  {link}")
    lines.append(f"Bag: {BAG_URL}  -> Check out -> Pick up -> 揀返上面間舖")
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

    burst = in_burst_window()
    deadline = time.time() + (BURST_FOR if burst else 0)
    passes = 0
    while True:
        alerted, failed = {}, set()

        def on_hit(hits):  # alert as soon as one query sees something new
            new_now = {k: v for k, v in hits.items() if k not in prev and k not in alerted}
            if not new_now:
                return
            alerted.update(new_now)
            if not notify(fmt(new_now)):
                failed.update(new_now)  # Telegram failed: keep them "new" so the next pass alerts again

        try:
            found = (scan_parallel if burst else scan)(on_hit=on_hit)
        except Blocked as e:
            write_atomic(BLOCKED_FLAG, f"{time.strftime('%F %T')} {e}\n")
            notify(f"iPhone monitor paused: Apple no longer answering normally ({e}). Check manually; delete .blocked to resume.")
            return
        passes += 1
        prev = set(found) - failed  # an item that disappears and comes back alerts again
        write_atomic(STATE, json.dumps(sorted(prev)))
        print(f"{time.strftime('%F %T')} pass={passes} burst={burst} seen={len(found)} new={len(alerted)}", flush=True)
        # Next pass on the next BURST_EVERY-second boundary (…:00, :05, :10 …) while time remains.
        nxt = (int(time.time()) // BURST_EVERY + 1) * BURST_EVERY
        if not burst or nxt >= deadline:
            return
        time.sleep(max(0, nxt - time.time()))


def main():
    load_env()
    if "--selftest" in sys.argv:
        # Detect a model known to be in stock (2TB Burgundy at time of writing) and send a test Telegram.
        t = {"MJY44ZA/A": ("2TB (SELF-TEST)", buy_url("2tb", "MJY44ZA/A"))}
        found = scan(targets=t, queries=["MJXQ4ZA/A"])
        msg = ("[TEST] " + fmt(found, t)) if found else "[TEST] monitor reachable, but 2TB stand-in not seen now"
        sys.exit(0 if notify(msg) else 1)
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
