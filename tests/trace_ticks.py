"""Collect every tick from GitHub minute-owner runs after a start time and report gaps.

usage: python3 tests/trace_ticks.py 2026-10-09T20:27:00Z [repo]
"""
import datetime, json, re, subprocess, sys

since = sys.argv[1]
repo = sys.argv[2] if len(sys.argv) > 2 else "wuhonchi/iphone-pickup-monitor"
runs = json.loads(subprocess.run(["gh", "run", "list", "--repo", repo, "--workflow", "monitor.yml", "--limit", "60",
                                  "--json", "databaseId,createdAt,event,status,conclusion"], capture_output=True, text=True).stdout)
runs = [r for r in runs if r["createdAt"] >= since and r["status"] == "completed"]
ticks, missed, per_run = [], [], []
for r in sorted(runs, key=lambda r: r["createdAt"]):
    log = subprocess.run(["gh", "run", "view", str(r["databaseId"]), "--repo", repo, "--log"], capture_output=True, text=True).stdout
    n = 0
    for line in log.splitlines():
        if "\x1b[36;1m" in line:
            continue
        m = re.search(r"(\d{4}-\d\d-\d\dT[\d:.]+)Z tick (\d\d:\d\d:\d\d) start\+([\d.]+)s took=([\d.]+)s .*errors=(\d+)", line)
        if m:
            day = m.group(1)[:10]
            sched = datetime.datetime.fromisoformat(f"{day}T{m.group(2)}+00:00")
            ticks.append((sched + datetime.timedelta(seconds=float(m.group(3))), sched, float(m.group(4)), int(m.group(5)), r["databaseId"]))
            n += 1
        if " missed (" in line:
            missed.append((r["databaseId"], line.split("Z ", 1)[-1]))
    per_run.append((r["databaseId"], r["event"], r["createdAt"][11:19], r["conclusion"], n))

for p in per_run:
    print("run %s %-17s created %s %-9s ticks=%d" % p)
ticks.sort()
scheds = [t[1] for t in ticks]
dups = len(scheds) - len(set(scheds))
gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(ticks, ticks[1:])]
lates = [t[0].timestamp() - t[1].timestamp() for t in ticks]
if ticks:
    print(f"\nticks={len(ticks)} from {ticks[0][1].strftime('%H:%M:%S')} to {ticks[-1][1].strftime('%H:%M:%S')} UTC")
    expected = int((ticks[-1][1] - ticks[0][1]).total_seconds() // 5) + 1
    print(f"expected ticks in that span={expected} unique ticks={len(set(scheds))} duplicate ticks={dups} missed-log-lines={len(missed)}")
    print(f"start delay vs schedule: max={max(lates):.2f}s")
    print(f"gap between consecutive tick starts: min={min(gaps):.2f}s max={max(gaps):.2f}s; outside 4-6s: {sum(1 for g in gaps if not 4 <= g <= 6)}")
    print(f"ticks with any query error: {sum(1 for t in ticks if t[3])}; slowest tick took {max(t[2] for t in ticks):.2f}s")
    for m in missed:
        print("missed:", m)
