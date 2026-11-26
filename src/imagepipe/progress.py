"""Live progress output that always reaches the screen.

A background thread redraws the progress line on a timer (every second on a
terminal, every 15 seconds when output is going to a log), so the display
keeps moving even while one huge file is being hashed or a long database
query is running. Output goes to stderr, never to a pipe that is thrown away.
"""

from __future__ import annotations

import collections
import logging
import os
import sys
import threading
import time

log = logging.getLogger("imagepipe")

# The rate and ETA use the last few minutes, so a slow patch early on doesn't keep
# the ETA climbing for the rest of the run. Until a run is WARMUP seconds old the
# average since the start is used.
RATE_WINDOW = 180.0
WARMUP = 30.0


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds == float("inf"):
        return "--:--:--"
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


class Progress:
    def __init__(self, label: str, total: int | None = None, total_bytes: int | None = None,
                 stream=None, interval: float | None = None, unit: str = "files"):
        self.label = label
        self.total = total
        self.total_bytes = total_bytes
        self.unit = unit
        self.stream = stream or sys.stderr
        self.tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        if interval is None:
            env = os.environ.get("IMAGEPIPE_PROGRESS_INTERVAL")
            try:
                interval = float(env) if env else None
            except ValueError:
                interval = None
        self.interval = interval if interval is not None else (1.0 if self.tty else 15.0)
        self.count = 0
        self.bytes = 0
        self.extra = ""
        self.start = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_log = self.start
        self._samples: collections.deque = collections.deque()  # (time, count, bytes)
        self.paused = 0.0  # seconds the process spent stopped (paused) - not counted as elapsed
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._render()
        self._thread.start()

    # -- public ----------------------------------------------------------
    def update(self, n: int = 1, nbytes: int = 0, extra: str | None = None) -> None:
        with self._lock:
            self.count += n
            self.bytes += nbytes
            if extra is not None:
                self.extra = extra

    def set_extra(self, extra: str) -> None:
        with self._lock:
            self.extra = extra

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._render(final=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals -------------------------------------------------------
    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(self.interval):
            now = time.monotonic()
            # The timer thread wakes every `interval`. If far more time went by, the process
            # was stopped (paused with SIGSTOP or Ctrl+Z): that time isn't elapsed work time.
            gap = now - last - self.interval
            if gap > max(5.0, self.interval):
                with self._lock:
                    self.paused += gap
                    self._samples.clear()
            last = now
            self._render()

    def line(self) -> str:
        with self._lock:
            count, nbytes, extra = self.count, self.bytes, self.extra
        now = time.monotonic()
        elapsed = now - self.start - self.paused
        rate, bps = self._rates(now, elapsed, count, nbytes)
        parts = [f"[{self.label}]"]
        if self.total is not None:
            pct = (count / self.total * 100) if self.total else 100.0
            parts.append(f"{count:,}/{self.total:,} {self.unit} ({pct:5.1f}%)")
        else:
            parts.append(f"{count:,} {self.unit}")
        parts.append(f"{rate:,.0f} {self.unit}/s")
        if nbytes or self.total_bytes:
            if self.total_bytes:
                parts.append(f"{fmt_bytes(nbytes)}/{fmt_bytes(self.total_bytes)} @ {fmt_bytes(bps)}/s")
            else:
                parts.append(f"{fmt_bytes(nbytes)} @ {fmt_bytes(bps)}/s")
        parts.append(f"elapsed {fmt_duration(elapsed)}")
        if self.total is not None:
            eta = None
            if self.total_bytes and bps > 0:
                eta = (self.total_bytes - nbytes) / bps
            elif rate > 0:
                eta = (self.total - count) / rate
            elif count >= self.total:
                eta = 0
            parts.append(f"ETA {fmt_duration(eta)}")
        if extra:
            parts.append(extra)
        return " | ".join(parts)

    def _rates(self, now: float, elapsed: float, count: int, nbytes: int) -> tuple[float, float]:
        """Items/s and bytes/s over the last RATE_WINDOW seconds (whole run while warming up)."""
        with self._lock:
            samples = self._samples
            samples.append((now, count, nbytes))
            while len(samples) > 2 and samples[1][0] <= now - RATE_WINDOW:
                samples.popleft()
            t0, c0, b0 = samples[0]
        if elapsed < WARMUP or now - t0 < WARMUP:
            return (count / elapsed, nbytes / elapsed) if elapsed > 0 else (0.0, 0.0)
        span = now - t0
        return (count - c0) / span, (nbytes - b0) / span

    def _render(self, final: bool = False) -> None:
        text = self.line()
        try:
            if self.tty:
                self.stream.write("\r\x1b[2K" + text + ("\n" if final else ""))
            else:
                self.stream.write(text + "\n")
            self.stream.flush()
        except Exception:
            pass
        now = time.monotonic()
        if final or now - self._last_log >= 60:
            self._last_log = now
            log.info(text)
