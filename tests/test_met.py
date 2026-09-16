# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The MET conversions, pinned so the Python and JS copies cannot drift apart.

``gvformat/met.py`` and ``gvformat-js/src/met.js`` carry the same table
(KR-XG2) and the same four conversions. Nothing links them at runtime, so the
only thing keeping them equal is a pair of tests asserting the same numbers.
This file is one half; ``gvformat-js/test/test-met.js`` is the other, and the
literals below appear in both.

What is checked:

  * ``score_mwc`` across its three regimes -- ordinary, Crawford (which reads
    the pre-Crawford table's 1-away row), and post-Crawford (cube live);
  * ``mwc2eq`` inverts ``eq2mwc`` at every score, cube and equity tried;
  * the opening-roll baseline (``gvanalysis.game_eval._opening_baseline``, which
    is ``score_mwc`` pushed through ``mwc2eq``) -- exactly 0.0 at a symmetric
    score, where the anchors are complementary, and the published values
    elsewhere. This is the one consumer that cares about the pair.

Run directly:
    uv run python tests/test_met.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat.met import eq2mwc, mwc2eq, score_mwc

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"   FAIL: {label}")
    else:
        print(f"   ok:   {label}")


def close(a: float, b: float, tol: float, label: str) -> None:
    check(abs(a - b) < tol, f"{label} ({a} ~= {b})")


def _baseline(away1: int, away2: int, is_crawford: bool = False) -> float:
    """What `_opening_baseline` computes, without importing the engine package."""
    return mwc2eq(score_mwc(away1, away2, is_crawford), away1, away2, 1, is_crawford)


def main() -> int:
    print("1. score_mwc across the regimes")
    for away1, away2, crawford, want in [
        (3, 5, False, 0.64795),   # ordinary, leader on roll
        (5, 3, False, 0.35205),   # its mirror
        (5, 5, False, 0.5),       # symmetric
        (1, 5, True, 0.84179),    # Crawford: the pre-Crawford table's 1-away row
        (1, 5, False, 0.80988),   # post-Crawford: cube live, so worth less
        (2, 7, False, 0.84225),
        (0, 0, False, 0.5),       # money
    ]:
        close(score_mwc(away1, away2, crawford), want, 1e-9,
              f"score_mwc({away1}, {away2}, {crawford})")

    print("2. mwc2eq inverts eq2mwc")
    for away1, away2, crawford in [(3, 5, False), (5, 3, False), (7, 2, False), (1, 4, True)]:
        for cube in (1, 2, 4):
            for equity in (-1.5, -0.3, 0.0, 0.25, 1.0):
                back = mwc2eq(eq2mwc(equity, away1, away2, cube, crawford),
                              away1, away2, cube, crawford)
                close(back, equity, 1e-9,
                      f"round trip {equity} at {away1}a-{away2}a cube {cube}"
                      f"{' crawford' if crawford else ''}")

    print("3. the opening-roll baseline")
    for away in (1, 2, 3, 5, 7, 11):
        check(_baseline(away, away) == 0.0, f"baseline at {away}a-{away}a is exactly 0")
    check(_baseline(0, 0) == 0.0, "baseline for money is exactly 0")

    close(_baseline(3, 5), -0.111511, 1e-6, "baseline 3a-5a")
    close(_baseline(5, 3), +0.111511, 1e-6, "baseline 5a-3a (mirror)")
    close(_baseline(1, 5, True), -0.020644, 1e-6, "baseline 1a-5a Crawford")
    close(_baseline(1, 5, False), -0.226502, 1e-6, "baseline 1a-5a post-Crawford")
    close(_baseline(2, 7), -0.205304, 1e-6, "baseline 2a-7a")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
