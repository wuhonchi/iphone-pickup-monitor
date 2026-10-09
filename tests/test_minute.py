"""Tests for monitor.minute_run (run: python3 tests/test_minute.py). Uses TICK=1s to keep it fast."""
import contextlib, io, math, os, pathlib, sys, tempfile, threading, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import monitor  # noqa: E402

# Real values, captured before setup() replaces them with fakes.
REAL_TARGETS, REAL_QUERIES, REAL_IN_BURST = dict(monitor.TARGETS), list(monitor.QUERIES), monitor.in_burst_window
REAL_PUSH, REAL_SUBPROCESS_RUN = monitor.push_blocked, monitor.subprocess.run

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
    monitor.push_blocked = lambda reason: (pushed.append(str(reason)), True)[1]
    monitor._gh_api = lambda path: (None, None)  # never hit the network
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

# M10: shared block state unknown mid-run (tick 3 check) -> fail closed: no further fetches this minute
calls = []; rb_calls = []
def rb10():
    rb_calls.append(1)
    return False if len(rb_calls) <= 2 else None  # start + tick 0 check OK, tick 3 check unknown
setup(lambda q, timeout=20: (calls.append(q), EMPTY)[1], remote_blocked=rb10)
monitor.REMOTE_BLOCK_EVERY = 3
out = io.StringIO()
with contextlib.redirect_stdout(out):
    monitor.minute_run(owned_start=start_soon(), ticks_per_min=6)
check("M10 block state unknown mid-run -> ticks 0-2 run, ticks 3-5 make no request",
      len(calls) == 3 * 3 and len(rb_calls) == 3 and "stop: block state unknown" in out.getvalue(),
      f"queries={len(calls)} rb_calls={len(rb_calls)}")

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

# M15: push_blocked returns False when a git command fails -> paused notification says so
def blocked_fetch(q, timeout=20):
    raise monitor.Blocked("HTTP 541")
for rc, want_flag in ((1, True), (0, False)):
    sent = []; git = []
    setup(blocked_fetch, notify=lambda t: (sent.append(t), True)[1])
    monitor.push_blocked = REAL_PUSH
    monitor.subprocess.run = lambda cmd, timeout=None, rc=rc: (git.append(cmd), type("P", (), {"returncode": rc})())[1]
    os.environ["GITHUB_ACTIONS"] = "true"
    with contextlib.redirect_stdout(io.StringIO()):
        ret = REAL_PUSH("probe")
        monitor.BLOCKED_FLAG.unlink()  # probe wrote the local flag; clear it so the run below starts
        monitor.minute_run(owned_start=start_soon(), ticks_per_min=2)
    os.environ.pop("GITHUB_ACTIONS"); monitor.subprocess.run = REAL_SUBPROCESS_RUN
    paused = [t for t in sent if "paused" in t]
    check(f"M15 git rc={rc}: push_blocked={ret}, notification {'has' if want_flag else 'lacks'} '(.blocked push FAILED)'",
          ret is (rc == 0) and len(paused) == 1 and (("(.blocked push FAILED)" in paused[0]) == want_flag) and len(git) == 8,
          f"ret={ret} paused={paused} git_cmds={len(git)}")

# Fake GitHub API for M16-M18
def fake_api(routes):
    def api(path):
        r = routes.get(path.split("?")[0])
        return r(path) if callable(r) else (r or (None, None))
    return api
RUNS = "actions/workflows/monitor.yml/runs"
os.environ["GITHUB_RUN_ID"] = "500"
T0 = "2026-10-10T00:00:30Z"; T0_EPOCH = 1791590430  # created_at of run 500 -> owns 00:01:00 UTC

# M16 (3a): created_at unknown -> run skipped, no Apple request, no fallback to now
calls = []
setup(lambda q, timeout=20: (calls.append(q), EMPTY)[1])
monitor._gh_api = fake_api({"actions/runs/500": (500, None), RUNS: (200, {"workflow_runs": []})})
out = io.StringIO(); t_start = time.time()
with contextlib.redirect_stdout(out):
    monitor.minute_run()
check("M16 created_at unknown -> run_created_epoch None, run skipped",
      monitor.run_created_epoch() is None and len(calls) == 0 and "skipped: created_at unknown" in out.getvalue() and time.time() - t_start < 1,
      f"queries={len(calls)} log={out.getvalue().strip()!r}")

# M17 (3b): duplicate dispatch claim
def runs_of(*rs):
    return (200, {"workflow_runs": [{"id": i, "created_at": c} for i, c in rs]})
own = {"actions/runs/500": (200, {"created_at": T0})}
calls = []
setup(lambda q, timeout=20: (calls.append(q), EMPTY)[1])
monitor._gh_api = fake_api({**own, RUNS: runs_of((501, "2026-10-10T00:00:50Z"), (500, T0), (499, "2026-10-10T00:00:05Z"))})
out = io.StringIO()
with contextlib.redirect_stdout(out):
    monitor.minute_run()
check("M17a older run (smaller id) in same minute -> skipped",
      len(calls) == 0 and "skipped: duplicate dispatch for this minute" in out.getvalue(), out.getvalue().strip())
monitor._gh_api = fake_api({**own, RUNS: runs_of((501, "2026-10-10T00:00:50Z"), (500, T0), (499, "2026-10-09T23:59:59Z"))})
with contextlib.redirect_stdout(io.StringIO()):
    got = monitor.resolve_owned_start()
check("M17b smaller id only in previous minute / larger id same minute -> owns next minute", got == T0_EPOCH - 30 + 60, f"got={got}")
for name, resp in (("HTTP 500", (500, None)), ("no network", (None, None)), ("malformed", (200, {"x": 1}))):
    monitor._gh_api = fake_api({**own, RUNS: resp})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        got = monitor.resolve_owned_start()
    check(f"M17c listing failure ({name}) -> proceed, logged", got == T0_EPOCH - 30 + 60 and "duplicate check unknown" in out.getvalue(), f"got={got}")

# M18 (4): --fallback
monitor.GAP = 0
NOW = T0_EPOCH + 600
def fb(latest_age, rb, listing=None):
    calls = []; rbn = []
    setup(lambda q, timeout=20: (calls.append(q), EMPTY)[1], window=False,
          remote_blocked=lambda: (rbn.append(1), rb)[1])
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - latest_age)) if latest_age is not None else None
    monitor._gh_api = fake_api({RUNS: listing or runs_of(*([(9, created)] if created else []))})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        monitor.fallback_run(now=lambda: NOW)
    return len(calls), len(rbn), out.getvalue()
n, nrb, log = fb(60, False)
check("M18a dispatch 60s ago -> exit, no Apple request", n == 0 and nrb == 0 and "fallback not needed: dispatch active" in log, log.strip())
n, nrb, log = fb(600, False)
check("M18b last dispatch 600s ago -> exactly one pass (3 queries)", n == 3 and nrb == 1, f"queries={n} log={log.strip()!r}")
n, nrb, log = fb(None, False)
check("M18c no dispatch runs at all -> one pass", n == 3, f"queries={n}")
n, _, log = fb(600, True)
check("M18d shared .blocked -> skipped", n == 0 and "fallback skipped" in log, log.strip())
n, _, log = fb(600, None)
check("M18e block state unknown -> skipped", n == 0 and "fallback skipped: block state unknown" in log, log.strip())
n, _, log = fb(None, False, listing=(None, None))
check("M18f listing failed -> runs one pass (logged unknown)", n == 3 and "dispatch state unknown" in log, log.strip())
os.environ.pop("GITHUB_RUN_ID")

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
