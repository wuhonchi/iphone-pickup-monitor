"""Offline tests for monitor.push_open (no network: urlopen is faked)."""
import os, sys, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import monitor

sent, results = [], []
monitor.urllib.request.urlopen = lambda req, timeout=None: (sent.append((req.full_url, req.data.decode(), req.get_header("Title"), timeout)), type("R", (), {"read": lambda s: b""})())[1]


def check(name, cond):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'} {name}")


def run(found, topic="t-test"):
    sent.clear()
    os.environ.pop("NTFY_TOPIC", None)
    if topic:
        os.environ["NTFY_TOPIC"] = topic
    monitor.push_open(found)
    for t in threading.enumerate():
        if t is not threading.current_thread() and not t.daemon:
            t.join(5)
    return list(sent)


two = {("MJXV4ZA/A", "Causeway Bay"): ("512GB Burgundy", "Today", "R428"),
       ("MJXQ4ZA/A", "IFC Mall"): ("256GB Burgundy", "Today", "R485")}
s = run(two)
check("one message for several new items", len(s) == 1)
url, body, title, timeout = s[0]
check("posts to ntfy topic", url == "https://ntfy.sh/t-test")
check("body = first item's #fastbuy link (fmt order)", body == monitor.TARGETS["MJXQ4ZA/A"][1] + "#fastbuy=R485")
check("title = model @ store", title == "256GB Burgundy @ IFC Mall")
check("timeout <= 2s", timeout == 2)
check("no topic -> nothing sent", run(two, topic=None) == [])
check("no store number -> nothing sent", run({("MJXQ4ZA/A", "IFC Mall"): ("256GB Burgundy", "Today", "")}) == [])
monitor.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
try:
    run(two); check("network error swallowed", True)
except Exception:
    check("network error swallowed", False)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
