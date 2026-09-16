# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The stderr progress bar, shared by the CLIs that have something to report.

Lives here rather than in `match.py` because `position.py` needs it too, and a
module reaching into another module's privates for a widget is how a CLI
helper quietly becomes an API. Nothing in here touches the engine.
"""

from __future__ import annotations

import sys
import time


class ProgressBar:
    """Minimal in-place progress bar drawn on a stream (default stderr).

    Disabled (a no-op) when the total is unknown/zero or the stream is not a TTY,
    so redirected output and non-interactive runs stay clean. Interleaves with
    line-based output: call clear() before printing, then redraw() to restore.
    """

    def __init__(self, total: int, stream=None, width: int = 20,
                 label: str = "Analyzing", unit: str = "dec"):
        self.total = total
        self.n = 0
        self.stream = stream if stream is not None else sys.stderr
        self.width = width
        self.label = label
        self.unit = unit
        self.t0 = time.perf_counter()
        self._last_len = 0
        try:
            tty = self.stream.isatty()
        except (AttributeError, ValueError):
            tty = False
        self.tty = tty
        self.enabled = total > 0 and tty

    def reset(self, total: int, label: str | None = None) -> None:
        """Start a new arc on the same line: counter back to 0, new total.

        For a phase whose size is only known once it begins -- a rollout's
        finalizing pass restarts 0 -> n_trials for each move it promotes, so
        without this the bar would sit at 100% through all of them.
        """
        self.total = total
        self.n = 0
        if label is not None:
            self.label = label
        self.enabled = total > 0 and self.tty
        self._draw() if self.enabled else None

    def _line(self) -> str:
        frac = self.n / self.total if self.total else 1.0
        filled = round(self.width * frac)
        bar = "█" * filled + "░" * (self.width - filled)
        mins, secs = divmod(int(time.perf_counter() - self.t0), 60)
        return (f"{self.label}  [{bar}]  {frac * 100:3.0f}%  "
                f"{self.n}/{self.total} {self.unit}  {mins}:{secs:02d}")

    def _draw(self, newline: bool = False) -> None:
        line = self._line()
        pad = " " * max(0, self._last_len - len(line))
        self.stream.write("\r" + line + pad + ("\n" if newline else ""))
        self.stream.flush()
        self._last_len = 0 if newline else len(line)

    def advance(self, k: int = 1) -> None:
        if not self.enabled:
            return
        self.n += k
        self._draw()

    def set(self, n: int) -> None:
        """Absolute position, for a callback that reports a running count
        rather than an increment."""
        if not self.enabled:
            return
        self.n = n
        self._draw()

    def clear(self) -> None:
        if not self.enabled:
            return
        self.stream.write("\r" + " " * self._last_len + "\r")
        self.stream.flush()
        self._last_len = 0

    def redraw(self) -> None:
        if self.enabled:
            self._draw()

    def close(self) -> None:
        if self.enabled:
            self._draw(newline=True)
