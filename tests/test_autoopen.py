#!/usr/bin/env python3
"""Offline tests for mac/autoopen.py (no network, no Safari): python3 tests/test_autoopen.py"""
import importlib.util, sys, types
from pathlib import Path

spec = importlib.util.spec_from_file_location("autoopen", Path(__file__).resolve().parent.parent / "mac" / "autoopen.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

URL1 = "https://www.apple.com/hk/shop/buy-iphone/x?product=MJXV4ZA%2FA#fastbuy=R673"
URL2 = "https://www.apple.com/hk/shop/buy-iphone/y?product=MJY04ZA%2FA#fastbuy=R485"
passed = 0


def test(fn):
    global passed
    fn()
    passed += 1
    print("ok -", fn.__name__)


def fake_run(rc=0, exc=None):
    calls = []

    def run(cmd, timeout=None):
        calls.append(cmd)
        if exc:
            raise exc
        return types.SimpleNamespace(returncode=rc)
    return run, calls


def ev(url, t):
    return {"event": "message", "message": url, "time": t, "title": "t"}


@test
def rate_limit_is_global_90s():
    assert A.RATE_SEC == 90
    run, calls = fake_run()
    logs, st = [], {"last_open": None}
    assert A.handle(ev(URL1, 1000), st, now=1000, run=run, logf=logs.append) == "opened"
    # different URL 30s later: skipped, not opened
    assert A.handle(ev(URL2, 1030), st, now=1030, run=run, logf=logs.append) == "skip"
    assert logs[-1].startswith("skipped: another flow started 30s ago")
    # same URL 89s later: skipped
    assert A.handle(ev(URL1, 1089), st, now=1089, run=run, logf=logs.append) == "skip"
    assert len(calls) == 1
    # 90s after the first open: allowed again
    assert A.handle(ev(URL2, 1090), st, now=1090, run=run, logf=logs.append) == "opened"
    assert len(calls) == 2 and calls[1] == ["open", "-a", "Safari", URL2]


@test
def failed_open_is_logged_and_does_not_start_window():
    run, calls = fake_run(rc=1)
    logs, st = [], {"last_open": None}
    assert A.handle(ev(URL1, 1000), st, now=1000, run=run, logf=logs.append) == "failed"
    assert "FAILED" in logs[-1] and "exit 1" in logs[-1] and "opened in Safari" not in logs[-1]
    assert st["last_open"] is None
    run_ok, calls_ok = fake_run(rc=0)
    assert A.handle(ev(URL2, 1005), st, now=1005, run=run_ok, logf=logs.append) == "opened"


@test
def open_exception_is_failure():
    run, _ = fake_run(exc=TimeoutError("t"))
    logs, st = [], {"last_open": None}
    assert A.handle(ev(URL1, 1000), st, now=1000, run=run, logf=logs.append) == "failed"
    assert "FAILED" in logs[-1]


@test
def not_allowed_and_stale_are_ignored():
    run, calls = fake_run()
    logs, st = [], {"last_open": None}
    assert A.handle(ev("https://evil.example/#fastbuy=R1", 1000), st, now=1000, run=run, logf=logs.append) == "ignore"
    assert A.handle(ev(URL1.replace("#fastbuy=R673", ""), 1000), st, now=1000, run=run, logf=logs.append) == "ignore"
    assert A.handle(ev(URL1, 1000 - A.MAX_AGE - 1), st, now=1000, run=run, logf=logs.append) == "ignore"
    assert calls == [] and st["last_open"] is None


print(f"{passed} tests passed")
sys.exit(0)
