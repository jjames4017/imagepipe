"""Progress line: rate and ETA from the recent rate (#13)."""

import io

from imagepipe import progress as prog
from imagepipe.progress import Progress


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(monkeypatch, **kw):
    clock = Clock()
    monkeypatch.setattr(prog.time, "monotonic", clock)
    p = Progress("Plan", stream=io.StringIO(), interval=3600, **kw)
    return p, clock


def field(line: str, start: str) -> str:
    return next(x for x in line.split(" | ") if x.startswith(start))


def test_early_rate_is_the_average(monkeypatch):
    p, clock = make(monkeypatch, total=1000)
    clock.t += 10
    p.update(100)
    line = p.line()
    assert "10 files/s" in line and field(line, "ETA") == "ETA 00:01:30"
    p.close()


def test_eta_follows_the_recent_rate(monkeypatch):
    p, clock = make(monkeypatch, total=10_000)
    # fast for 10 minutes: 10 files/s
    for _ in range(600):
        clock.t += 1
        p.update(10)
        p.line()
    # then slow for 5 minutes: 1 file/s
    for _ in range(300):
        clock.t += 1
        p.update(1)
        p.line()
    line = p.line()
    # the average since the start would say ~6.9 files/s; the recent rate is 1 file/s
    assert "1 files/s" in line, line
    remaining = 10_000 - 6300
    assert field(line, "ETA") == f"ETA {prog.fmt_duration(remaining)}"
    # and speeding up again brings the ETA straight down
    for _ in range(200):
        clock.t += 1
        p.update(10)
        p.line()
    assert "10 files/s" in p.line()
    p.close()


def test_byte_rate_uses_the_window_too(monkeypatch):
    p, clock = make(monkeypatch, total=10, total_bytes=1_000_000)
    for _ in range(300):
        clock.t += 1
        p.update(0, 1000)
        p.line()
    for _ in range(200):
        clock.t += 1
        p.update(0, 100)
        p.line()
    line = p.line()
    assert "@ 100 B/s" in line, line
    p.close()


def test_paused_time_is_not_elapsed(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time

    script = ("import io, sys, time\n"
              "from imagepipe.progress import Progress\n"
              "p = Progress('Work', total=10, stream=io.StringIO(), interval=0.2)\n"
              "print('ready', flush=True)\n"
              "time.sleep(8)\n"
              "print(round(p.paused), flush=True)\n"
              "p.close()\n")
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    time.sleep(0.5)
    os.kill(proc.pid, signal.SIGSTOP)
    time.sleep(6)
    os.kill(proc.pid, signal.SIGCONT)
    out, _ = proc.communicate(timeout=30)
    assert 5 <= int(out.strip()) <= 7, out
