# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Unit tests for gvformat.xg._xg_embedded_cube (no-double cube classifier).

The XG embedded cube analysis classifies a no-double decision from its
equities -- optimal action is "double" iff min(dt, dp) > nd -- rather than
XG's double_choice flag, which is an unreliable sentinel (-1 on most
no-double rows, including some genuine missed doubles). This helper takes
only equities, so the sentinel structurally cannot reach it. Values below
are drawn from a real .xg conversion (not committed; see session notes).

Run directly:
    uv run python tests/test_xg_cube.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat.xg import _xg_embedded_cube, _trivial_cube

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"FAIL  {label}")
    else:
        print(f"OK    {label}")


def main() -> int:
    # Missed double, opponent takes: loss = min(dt, dp) - nd = dt - nd.
    key, sub = _xg_embedded_cube(0.6669, 0.7358, 1.0)
    check(key == "missed_double" and abs(sub["equity_loss"] - 0.0689) < 1e-9,
          "missed double (take) -> 0.0689 loss")

    # Missed double, opponent passes (dt > dp): loss CAPPED at dp.
    key, sub = _xg_embedded_cube(0.925, 1.2652, 1.0)
    check(key == "missed_double" and abs(sub["equity_loss"] - 0.075) < 1e-9,
          "missed double (pass) -> 0.075 loss (capped at dp, not dt-nd=0.3402)")

    # Deep double/pass missed double (hid under a double_choice==-1 sentinel).
    key, sub = _xg_embedded_cube(0.674, 1.034, 1.0)
    check(key == "missed_double" and abs(sub["equity_loss"] - 0.326) < 1e-9,
          "deep missed double (pass) -> 0.326 loss (capped: 1.0 - 0.674)")

    # Correct no-double, non-trivial: cube_decision with decision=True.
    key, sub = _xg_embedded_cube(0.3782, 0.2324, 1.0)
    check(key == "cube_decision" and sub["decision"] is True and sub["equity_loss"] == 0.0,
          "correct no-double (close) -> cube_decision, decision=True")

    # Sentinel-immunity regression: this row is XG double_choice==-1 (would have
    # been a false missed double under `& 1`); equities say no-double, clearly.
    key, sub = _xg_embedded_cube(-0.0813, -0.4848, 1.0)
    check(key == "cube_decision" and sub["decision"] is False,
          "sentinel row -> cube_decision (not false missed double), trivial")

    # Boundary: best == nd exactly -> not a missed double (and trivial).
    key, sub = _xg_embedded_cube(0.5, 0.5, 1.0)
    check(key == "cube_decision" and sub["decision"] is False,
          "best == nd -> cube_decision, decision=False")

    # Unanalyzed row (drop equity 0) -> skipped entirely.
    check(_xg_embedded_cube(0.0, 0.0, 0.0) is None,
          "unanalyzed row (dp=0) -> None")

    # _trivial_cube spot checks.
    check(_trivial_cube(0.5, 0.5, 1.0) is True, "trivial: nd == min(dt,dp)")
    check(_trivial_cube(0.9, -1.5, 1.0) is True, "trivial: nd - dt > 0.2")
    check(_trivial_cube(0.3782, 0.2324, 1.0) is False, "non-trivial close cube")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
