"""Tests for monitor.minute_run (run: python3 tests/test_minute.py). Uses TICK=1s to keep it fast."""
import math, pathlib, sys, tempfile, threading, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import monitor  # noqa: E402

# Real values, captured before setup() replaces them with fakes.
REAL_TARGETS, REAL_QUERIES, REAL_IN_BURST = dict(monitor.TARGETS), list(monitor.QUERIES), monitor.in_burst_window

HIT = {"body": {"PickupMessage": {"stores": [{"storeName": "ifc mall", "storeNumber": "R428",
        "partsAvailability": {"T1": {"pickupDisplay": "available", "pickupSearchQuote": "Available Today"}}}]}}}
EMPTY = {"body": {"PickupMessage": {"stores": []}}}
results = []


def setup(fetch, notify=None, window=True, remote_blocked=lambda: False):
    d = pathlib.Path(tempfile.mkdtemp())
    monitor.STATE, monitor.BLOCKED_FLAG, monitor.LOCK = d / "s.json", d / "b", d / "l"
    monitor.TARGETS = {"T1": ("Target", "https://x/t1")}
    monitor.QUERIES = ["a", "b", "c"]
    monitor.TICK = 1
    monitor.fetch = fetch
    monitor.notify = notify or (lambda t: True)
    monitor.in_burst_window = lambda now=None: window
    monitor.remote_blocked = remote_blocked
    monitor.push_blocked = lambda reason: pushed.append(str(reason))
    return d


def start_soon():
    return math.ceil(time.time()) + 1


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


pushed = []

# M1: every tick starts on its scheduled second (within 0.3s), even when each fetch takes 0.8s
starts = []
def slow_fetch(q, timeout=20):
    starts.append(time.time()); time.sleep(0.8); return EMPTY
setup(slow_fetch)
s0 = start_soon(); monitor.minute_run(owned_start=s0, ticks_per_min=6)
tick_starts = sorted(starts)[::3]
offs = [round(t - (s0 + i), 2) for i, t in enumerate(tick_starts)]
check("M1 6 ticks, each starts on schedule despite slow fetch", len(tick_starts) == 6 and all(0 <= o < 0.3 for o in offs), f"offsets={offs}")

# M2: outside the window only the first tick runs
calls = []
setup(lambda q, timeout=20: (calls.append(q), EMPTY)[1], window=False)
monitor.minute_run(owned_start=start_soon(), ticks_per_min=6)
check("M2 outside window -> exactly 1 tick (3 queries)", len(calls) == 3, f"calls={len(calls)}")

# M3: runner ready late -> past ticks reported missed, remaining ticks still run on time
calls = []
setup(lambda q, timeout=20: (calls.append(time.time()), EMPTY)[1])
s0 = math.floor(time.time()) - 2  # ticks 0,1 already >1s in the past
monitor.minute_run(owned_start=s0, ticks_per_min=6)
check("M3 late start: missed ticks skipped, later ticks run", 9 <= len(calls) <= 12, f"queries={len(calls)}")

# M4: 503 on one query + 541 on another in the same tick -> treated as blocked, stop
pushed.clear(); calls = []
def mixed(q, timeout=20):
    calls.append(q)
    if q == "a":
        raise RuntimeError("HTTP 503")
    if q == "b":
        time.sleep(0.2); raise monitor.Blocked("HTTP 541")
    return EMPTY
sent = []
setup(mixed, notify=lambda t: (sent.append(t), True)[1])
monitor.minute_run(owned_start=start_soon(), ticks_per_min=6)
check("M4 541 wins over 503; pushed .blocked once; no further ticks", len(pushed) == 1 and len(calls) == 3 and any("paused" in x for x in sent), f"pushed={len(pushed)} calls={len(calls)}")

# M5: shared .blocked set by another run -> this run stops at its next check
calls = []; flag = {"on": False}
def fetch5(q, timeout=20):
    calls.append(q); flag["on"] = True; return EMPTY
setup(fetch5, remote_blocked=lambda: flag["on"])
monitor.REMOTE_BLOCK_EVERY = 3
monitor.minute_run(owned_start=start_soon(), ticks_per_min=6)
check("M5 shared .blocked stops other runs (checked every 3 ticks)", len(calls) == 3 * 3, f"queries={len(calls)}")

# M6: item visible on every tick -> one alert per minute; already in state -> no alert
sent = []
setup(lambda q, timeout=20: HIT if q == "a" else EMPTY, notify=lambda t: (sent.append(t), True)[1])
monitor.minute_run(owned_start=start_soon(), ticks_per_min=6)
check("M6 persisting item alerted once in the minute", len(sent) == 1, f"alerts={len(sent)}")
sent.clear()
monitor.minute_run(owned_start=start_soon(), ticks_per_min=3)
check("M6b item already in saved state -> no repeat next minute", len(sent) == 0, f"alerts={len(sent)}")

# M7: a slow Telegram send does not delay ticks
starts = []
def fetch7(q, timeout=20):
    starts.append(time.time()); return HIT if q == "a" else EMPTY
setup(fetch7, notify=lambda t: (time.sleep(3), True)[1])
s0 = start_soon(); monitor.minute_run(owned_start=s0, ticks_per_min=5)
offs = [round(t - (s0 + i), 2) for i, t in enumerate(sorted(starts)[::3])]
check("M7 slow Telegram (3s) does not delay ticks", all(0 <= o < 0.3 for o in offs), f"offsets={offs}")

# M8: a failed Telegram send keeps the item 'new' for the next minute
sent = []
setup(lambda q, timeout=20: HIT if q == "a" else EMPTY, notify=lambda t: (sent.append(t), False)[1])
monitor.minute_run(owned_start=start_soon(), ticks_per_min=2)
monitor.notify = lambda t: (sent.append(t), True)[1]
monitor.minute_run(owned_start=start_soon(), ticks_per_min=2)
check("M8 failed send -> alerted again next minute", len(sent) == 2, f"sends={len(sent)}")

# M9: a transient error on one query does not stop the minute and does not clear state
calls = []
def flaky(q, timeout=20):
    calls.append(q)
    if q == "c":
        raise RuntimeError("timeout")
    return EMPTY
setup(flaky)
monitor.minute_run(owned_start=start_soon(), ticks_per_min=4)
check("M9 transient errors: all ticks still run", len(calls) == 12, f"queries={len(calls)}")

# M10: shared block state unknown on a tick check -> that tick makes no Apple request, run continues
calls = []; rb_calls = []
def rb10():
    rb_calls.append(1)
    return False if len(rb_calls) == 1 else None  # start check OK, every tick check unknown
setup(lambda q, timeout=20: (calls.append(q), EMPTY)[1], remote_blocked=rb10)
monitor.REMOTE_BLOCK_EVERY = 3
monitor.minute_run(owned_start=start_soon(), ticks_per_min=6)
check("M10 block state unknown -> ticks 0,3 skipped, ticks 1,2,4,5 still run", len(calls) == 4 * 3 and len(rb_calls) == 3, f"queries={len(calls)} rb_calls={len(rb_calls)}")

# M11: shared block state unknown at run start -> whole run skipped, no Apple request, state untouched
calls = []
d = setup(lambda q, timeout=20: (calls.append(q), EMPTY)[1], remote_blocked=lambda: None)
monitor.minute_run(owned_start=start_soon(), ticks_per_min=6)
check("M11 block state unknown at start -> run skipped", len(calls) == 0 and not monitor.STATE.exists(), f"queries={len(calls)}")

# M12: stop set (by another tick's Blocked) while a tick is in its remote check -> that tick does not fetch
calls = []; rb_n = []; pushed.clear()
def rb12():
    rb_n.append(1)
    if len(rb_n) > 1:
        time.sleep(0.6)  # tick checks are slow; tick 1 is still checking when tick 0 hits a block
    return False
def fetch12(q, timeout=20):
    calls.append((q, time.time()))
    if q == "b":
        time.sleep(0.8); raise monitor.Blocked("HTTP 541")
    return EMPTY
setup(fetch12, remote_blocked=rb12)
monitor.REMOTE_BLOCK_EVERY = 1
s0 = start_soon(); monitor.minute_run(owned_start=s0, ticks_per_min=6)
monitor.REMOTE_BLOCK_EVERY = 3
check("M12 stop set mid-minute -> no later tick starts a fetch", len(calls) == 3 and len(pushed) == 1 and all(t < s0 + 1 for _, t in calls), f"queries={len(calls)} pushed={len(pushed)}")

# M13: start == end burst window is never active (used by the */30 schedule fallback)
monitor.in_burst_window = REAL_IN_BURST
monitor.BURST_WINDOW = "00:00-00:00"
day0 = 1760000000 - 1760000000 % 86400  # a UTC midnight
never = not any(monitor.in_burst_window(day0 + m * 60) for m in range(0, 1440))
monitor.BURST_WINDOW = "07:00-09:30"
normal = monitor.in_burst_window(day0 + 0 * 3600) and not monitor.in_burst_window(day0 + 2 * 3600)  # 08:00 / 10:00 HKT
check("M13 BURST_WINDOW 00:00-00:00 never true; 07:00-09:30 still works", never and normal)

# M14: target / query composition
want = {"MJXV4ZA/A": ("512GB Burgundy", "512gb", "burgundy"), "MJY04ZA/A": ("1TB Burgundy", "1tb", "burgundy"),
        "MJXT4ZA/A": ("512GB Black", "512gb", "black"), "MJXX4ZA/A": ("1TB Black", "1tb", "black")}
ok_t = set(REAL_TARGETS) == set(want) and all(
    REAL_TARGETS[p][0] == lab and f"-{cap}-{col}?" in REAL_TARGETS[p][1] and "product=" + p.replace("/", "%2F") in REAL_TARGETS[p][1]
    for p, (lab, cap, col) in want.items())
ok_q = (len(REAL_QUERIES) == 6 and len(set(REAL_QUERIES)) == 6 and set(REAL_TARGETS) <= set(REAL_QUERIES)
        and set(REAL_QUERIES) - set(REAL_TARGETS) == {"MJY44ZA/A", "MJXQ4ZA/A"})
check("M14 4 targets w/ right labels+links; 6 unique queries incl. every target + 2 helpers", ok_t and ok_q, f"targets={sorted(REAL_TARGETS)} queries={REAL_QUERIES}")

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
