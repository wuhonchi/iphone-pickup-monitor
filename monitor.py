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
import calendar, concurrent.futures, fcntl, json, os, queue, threading, random, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
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
    "MJXV4ZA/A": ("512GB Burgundy", buy_url("512gb", "MJXV4ZA/A")),
    "MJY04ZA/A": ("1TB Burgundy", buy_url("1tb", "MJY04ZA/A")),
    "MJXT4ZA/A": ("512GB Black", buy_url("512gb", "MJXT4ZA/A", "black")),
    "MJXX4ZA/A": ("1TB Black", buy_url("1tb", "MJXX4ZA/A", "black")),
}
# Each query hides its own product, so query every target plus two non-target Pro Max helper parts
# (they are never alerted on; they only widen the recommendation lists).
QUERIES = list(TARGETS) + ["MJY44ZA/A", "MJXQ4ZA/A"]  # helpers: 2TB Burgundy, 256GB Burgundy

INTERVAL = int(os.environ.get("INTERVAL_SEC", "180"))
GAP = 3  # seconds between the requests inside one cycle (sequential mode)
# Burst mode: inside one cron minute, poll every BURST_EVERY s for BURST_FOR s, queries in parallel.
# Only during BURST_WINDOW (HKT) -- stock was seen only 07:27-08:46 HKT on 10/08-10/09 (state commits / run logs).
BURST_WINDOW = os.environ.get("BURST_WINDOW", "07:00-09:30")
BURST_EVERY = int(os.environ.get("BURST_EVERY", "5"))
# One GitHub run polls ~4.5 min; the next cron-triggered run is queued and starts right after,
# so runner start-up (observed ~19s, run 37984743911) is paid once per run, not once per minute.
BURST_FOR = int(os.environ.get("BURST_FOR", "270"))


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


def fetch(product, timeout=20):
    q = urllib.parse.urlencode({"fae": "true", "mts.0": "regular", "location": LOCATION, "product": product})
    req = urllib.request.Request(f"{BASE}?{q}", headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
    if start == end:  # e.g. "00:00-00:00" = never (used for the */30 schedule fallback)
        return False
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


def item_link(part, rest, targets):
    num = rest[0] if rest else ""
    return targets[part][1] + (f"#fastbuy={num}" if num else "")


def fmt(found, targets=None):
    targets = targets or TARGETS
    hkt = time.strftime("%H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    lines = [f"iPhone 18 Pro Max 今日有得自取 (見到 {hkt} HKT，未留貨):"]
    for (part, store), (label, quote, *rest) in sorted(found.items()):
        link = item_link(part, rest, targets)
        lines.append(f"- {label} @ Apple {store} ({quote})\n  {link}")
    lines.append(f"Bag: {BAG_URL}  -> Check out -> Pick up -> 揀返上面間舖")
    return "\n".join(lines)


def push_open(found, targets=None):
    """POST the first #fastbuy link to ntfy (Mac auto-opens it). Background thread, 2 s timeout, never raises."""
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    targets = targets or TARGETS
    links = [(item_link(part, rest, targets), f"{label} @ {store}")
             for (part, store), (label, quote, *rest) in sorted(found.items()) if rest and rest[0]]
    if not (topic and links):
        return
    url, title = links[0]

    def send():
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", url.encode(),
                                     headers={"Title": title.encode("ascii", "replace").decode()})
        try:
            urllib.request.urlopen(req, timeout=2).read()
        except Exception as e:
            print(f"ntfy failed: {e}", flush=True)

    threading.Thread(target=send).start()  # non-daemon: a short-lived run still finishes the POST


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
    passes, last_pass = 0, None
    while True:
        alerted, failed = {}, set()

        def on_hit(hits):  # alert as soon as one query sees something new
            new_now = {k: v for k, v in hits.items() if k not in prev and k not in alerted}
            if not new_now:
                return
            alerted.update(new_now)
            push_open(new_now)
            if not notify(fmt(new_now)):
                failed.update(new_now)  # Telegram failed: keep them "new" so the next pass alerts again

        try:
            found = (scan_parallel if burst else scan)(on_hit=on_hit)
        except Blocked as e:
            write_atomic(BLOCKED_FLAG, f"{time.strftime('%F %T')} {e}\n")
            notify(f"iPhone monitor paused: Apple no longer answering normally ({e}). Check manually; delete .blocked to resume.")
            return
        passes += 1
        gap = f" gap={time.time() - last_pass:.1f}s" if last_pass else ""
        last_pass = time.time()
        prev = set(found) - failed  # an item that disappears and comes back alerts again
        write_atomic(STATE, json.dumps(sorted(prev)))
        print(f"{time.strftime('%F %T')} pass={passes}{gap} burst={burst} seen={len(found)} new={len(alerted)}", flush=True)
        # Next pass on the next BURST_EVERY-second boundary (…:00, :05, :10 …) while time remains.
        nxt = (int(time.time()) // BURST_EVERY + 1) * BURST_EVERY
        if not burst or nxt >= deadline:
            return
        time.sleep(max(0, nxt - time.time()))


# ---------------------------------------------------------------------------
# Minute-owner mode (--minute): cron-job.org dispatches a run every minute; each run owns the
# NEXT wall-clock minute after its creation and polls its 12 ticks (:00,:05,...,:55) on time.
# Runs overlap only while the next one warms up, so there is no hand-off gap (design X, review round 9).
TICK = 5
TICK_TIMEOUT = 4.0           # per Apple request, so a tick finishes before the next one
REMOTE_BLOCK_EVERY = 3       # check the shared .blocked flag every 3 ticks (15 s)


def _gh_api(path):
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and tok):
        return None, None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/{path}",
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def run_created_epoch():
    """Creation time of this workflow run (fixed reference for minute ownership); falls back to now."""
    rid = os.environ.get("GITHUB_RUN_ID")
    if rid:
        status, body = _gh_api(f"actions/runs/{rid}")
        if status == 200 and body and body.get("created_at"):
            return calendar.timegm(time.strptime(body["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
    print(f"{time.strftime('%F %T')} created_at unknown, using now", flush=True)
    return time.time()


def remote_blocked():
    """True = shared .blocked exists, False = confirmed absent (404), None = unknown (error / other status)."""
    status, _ = _gh_api("contents/.blocked?ref=main")
    if status == 200:
        return True
    if status == 404:
        return False
    return None


def push_blocked(reason):
    write_atomic(BLOCKED_FLAG, f"{time.strftime('%F %T')} {reason}\n")
    if os.environ.get("GITHUB_ACTIONS"):
        for cmd in (["git", "add", ".blocked"], ["git", "commit", "-q", "-m", "monitor blocked"],
                    ["git", "pull", "-q", "--rebase"], ["git", "push", "-q"]):
            try:
                rc = subprocess.run(cmd, timeout=30).returncode
            except Exception as e:
                print(f"push_blocked: {' '.join(cmd)} failed: {e}", flush=True)
                continue
            if rc != 0:
                print(f"push_blocked: {' '.join(cmd)} exited {rc}", flush=True)


def fetch_all(queries, timeout):
    """All queries in parallel. A Blocked response wins over any other error."""
    results, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(queries)) as ex:
        futs = [ex.submit(fetch, q, timeout) for q in queries]
        for f in concurrent.futures.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:  # classified below
                errors.append(e)
    blocked = [e for e in errors if isinstance(e, Blocked)]
    if blocked:
        raise blocked[0]
    return results, errors


class Notifier:
    """Sends Telegram messages on a background thread so polling never waits for it."""

    def __init__(self, send):
        self.q, self.send, self.results = queue.Queue(), send, []
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            keys, text = item
            self.results.append((keys, self.send(text)))

    def put(self, keys, text):
        self.q.put((keys, text))

    def close(self, timeout=20):
        self.q.put(None)
        self.t.join(timeout)


def minute_run(owned_start=None, ticks_per_min=60 // TICK, now=time.time, sleep=time.sleep):
    if BLOCKED_FLAG.exists():
        print(f"{time.strftime('%F %T')} skipped: blocked", flush=True)
        return
    rb = remote_blocked()
    if rb is True:
        print(f"{time.strftime('%F %T')} skipped: blocked (shared .blocked)", flush=True)
        return
    if rb is None:
        print(f"{time.strftime('%F %T')} skipped: block state unknown", flush=True)
        return
    try:
        prev = {tuple(x) for x in json.loads(STATE.read_text())}
    except Exception:
        prev = set()
    if owned_start is None:
        owned_start = (int(run_created_epoch()) // 60 + 1) * 60
    lock = threading.Lock()
    alerted, stop = set(), threading.Event()
    last_seen = {"found": None}
    notifier = Notifier(notify)
    missed = []

    def do_tick(i, t):
        if stop.is_set():
            return
        if i % REMOTE_BLOCK_EVERY == 0:
            rb = remote_blocked()
            if rb is True:
                stop.set()
                print(f"tick {i} stop: shared .blocked found", flush=True)
                return
            if rb is None:
                print(f"tick {i} skipped: block state unknown", flush=True)
                return
        if stop.is_set():  # another tick may have hit a block while we checked
            return
        t0 = now()
        try:
            results, errors = fetch_all(QUERIES, TICK_TIMEOUT)
        except Blocked as e:
            if not stop.is_set():
                stop.set()
                push_blocked(e)
                notifier.put(set(), f"iPhone monitor paused: Apple no longer answering normally ({e}). Check manually; delete .blocked to resume.")
            return
        found = {}
        for r in results:
            found.update(_hits(r["body"]["PickupMessage"], TARGETS))
        with lock:
            new = {k: v for k, v in found.items() if k not in prev and k not in alerted}
            alerted.update(new)
            if not errors:  # only a complete scan may mark items as gone
                last_seen["found"] = found
        if new:
            # ntfy failure is only logged; a failed Telegram send keeps the item 'new' so it is retried next minute.
            push_open(new)
            notifier.put(set(new), fmt(new))
        hms = time.strftime("%H:%M:%S", time.gmtime(t))
        print(f"tick {hms} start+{t0 - t:.2f}s took={now() - t0:.2f}s seen={len(found)} new={len(new)} errors={len(errors)}", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for i in range(ticks_per_min):
            t = owned_start + i * TICK
            if stop.is_set():
                break
            if i > 0 and not in_burst_window(t):
                break  # outside the window: one check per minute is enough
            wait = t - now()
            if wait > 0:
                sleep(wait)
            elif wait < -1:
                missed.append(i)
                print(f"tick {time.strftime('%H:%M:%S', time.gmtime(t))} missed (runner ready {-wait:.1f}s late)", flush=True)
                continue
            pool.submit(do_tick, i, t)
    notifier.close()
    failed = set().union(*[k for k, ok in notifier.results if not ok]) if notifier.results else set()
    final = last_seen["found"]
    if final is not None:
        write_atomic(STATE, json.dumps(sorted(set(final) - failed)))
    print(f"minute {time.strftime('%H:%M', time.gmtime(owned_start))} done alerted={len(alerted)} missed_ticks={len(missed)}", flush=True)


def main():
    load_env()
    if "--selftest" in sys.argv:
        # Detect a model known to be in stock (2TB Burgundy at time of writing) and send a test Telegram.
        t = {"MJY44ZA/A": ("2TB (SELF-TEST)", buy_url("2tb", "MJY44ZA/A"))}
        found = scan(targets=t, queries=["MJXQ4ZA/A"])
        msg = ("[TEST] " + fmt(found, t)) if found else "[TEST] monitor reachable, but 2TB stand-in not seen now"
        sys.exit(0 if notify(msg) else 1)
    if "--minute" in sys.argv:
        minute_run()
        return
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
