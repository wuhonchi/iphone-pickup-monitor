"""Tests for monitor.minute_run (run: python3 tests/test_minute.py). Uses TICK=1s to keep it fast."""
import math, pathlib, sys, tempfile, threading, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import monitor  # noqa: E402

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

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
