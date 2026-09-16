# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Unit tests for gvformat.bgf._cube_response_analysis equity_loss.

Regression guard: a *wrong pass* (player dropped a takeable cube) is a real
error and must be scored with its equity loss, not silently zeroed. Exercises
the take/pass response scorer directly with constructed BGF-shaped dicts, so
no engine or fixture file is needed.

Run directly:
    uv run python tests/test_bgf_cube_response.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat.bgf import (
    _cube_response_analysis,
    _cube_decision_analysis,
    _embedded_cube_analysis,
)

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


# Money-frame normalization params (emg=0, meq=0, eqDoublePass=1.0) make the
# normalization identity: eq_dt == raw eqDoubleTake, eq_dp == 1.0.
_EQ_FULL = {"emg": 0.0, "matchEquity": 0.0}


def _cd(state_other: str, has_accepted: bool, eq_double_take: float) -> dict:
    return {
        "eqNoDouble": 0.5,          # any real value (> sentinel) enables scoring
        "eqDoublePass": 1.0,
        "eqDoubleTake": eq_double_take,
        "stateOther": state_other,  # ACCEPT -> take is correct, REJECT -> pass
        "hasAccepted": has_accepted,
    }


def _cd_decision(state_on_move: str, has_doubled: bool,
                 eq_no_double: float, eq_double_take: float) -> dict:
    return {
        "eqNoDouble": eq_no_double,
        "eqDoublePass": 1.0,
        "eqDoubleTake": eq_double_take,
        "stateOnMove": state_on_move,   # DOUBLE -> double is right, NO_DOUBLE -> no_double
        "hasDoubled": has_doubled,
    }


def main() -> int:
    # -- take/pass response ---------------------------------------------------
    # Correct take (played take, take is right): no loss.
    a = _cube_response_analysis(_cd("ACCEPT", True, 0.4), _EQ_FULL, None)
    check(a is not None and a["equity_loss"] == 0.0, "correct take -> 0 loss")

    # Took a pass (played take, pass is right): loss = |eq_dt - eq_dp|.
    a = _cube_response_analysis(_cd("REJECT", True, 1.3), _EQ_FULL, None)
    check(a is not None and abs(a["equity_loss"] - 0.3) < 1e-9,
          "took a pass -> 0.3 loss")

    # Passed a take (played pass, take is right): REGRESSION -- must be nonzero.
    a = _cube_response_analysis(_cd("ACCEPT", False, 0.4), _EQ_FULL, None)
    check(a is not None and abs(a["equity_loss"] - 0.6) < 1e-9,
          "passed a take -> 0.6 loss (not silently zeroed)")

    # -- standalone cube decision (_cube_decision_analysis) -------------------
    # Correct double, played double: no loss.
    a = _cube_decision_analysis(_cd_decision("DOUBLE", True, 0.2, 0.8), _EQ_FULL, None)
    check(a is not None and a["equity_loss"] == 0.0, "correct double -> 0 loss")

    # Missed double (should double, played no-double): REGRESSION -- was 0.0.
    a = _cube_decision_analysis(_cd_decision("DOUBLE", False, 0.2, 0.8), _EQ_FULL, None)
    check(a is not None and abs(a["equity_loss"] - 0.6) < 1e-9,
          "missed double -> 0.6 loss (not silently zeroed)")

    # Missed double that is a double/PASS: realized value capped at eq_dp=1.0.
    a = _cube_decision_analysis(_cd_decision("DOUBLE", False, 0.55, 1.3), _EQ_FULL, None)
    check(a is not None and abs(a["equity_loss"] - 0.45) < 1e-9,
          "missed double/pass -> 0.45 loss (capped at eq_dp)")

    # Wrong double (should no-double, played double): loss = eq_nd - min(eq_dt, eq_dp).
    a = _cube_decision_analysis(_cd_decision("NO_DOUBLE", True, 0.2, -0.1), _EQ_FULL, None)
    check(a is not None and abs(a["equity_loss"] - 0.3) < 1e-9,
          "wrong double -> 0.3 loss")

    # -- embedded missed double (_embedded_cube_analysis) ---------------------
    # Missed double/take: loss = eq_dt - eq_nd (cap inactive).
    key, a = _embedded_cube_analysis(
        _cd_decision("DOUBLE", None, 0.55, 0.8), _EQ_FULL, 1, True, None)
    check(a is not None and abs(a["equity_loss"] - 0.25) < 1e-9,
          "embedded missed double/take -> 0.25 loss")
    check(key == "missed_double", "embedded missed double is keyed missed_double")

    # Missed double/PASS: REGRESSION -- was eq_dt-eq_nd=0.75, now capped to 0.45.
    key, a = _embedded_cube_analysis(
        _cd_decision("DOUBLE", None, 0.55, 1.3), _EQ_FULL, 1, True, None)
    check(a is not None and abs(a["equity_loss"] - 0.45) < 1e-9,
          "embedded missed double/pass -> 0.45 loss (capped, was overcounted)")

    # A missed RE-double: REGRESSION. `RE_DOUBLE` is a `double` as far as
    # `_state_action` is concerned, so it builds a missed_double -- but the
    # caller used to re-derive the key from the state and matched only
    # `"DOUBLE"`, filing this under `cube_decision`. `cube_decision`'s
    # equity_loss is zero by definition on disk (CUBE type=4), so the error
    # showed in the viewer and disappeared the moment the match was saved. It
    # cost one real 13-point match 1.65 PR.
    key, a = _embedded_cube_analysis(
        _cd_decision("RE_DOUBLE", None, 0.55, 0.8), _EQ_FULL, 2, True, None)
    check(key == "missed_double", "a missed REdouble is keyed missed_double, not cube_decision")
    check(a is not None and abs(a["equity_loss"] - 0.25) < 1e-9,
          "and keeps its 0.25 loss, which cube_decision would have zeroed")
    # No `decision`: CUBE type=2's single bit cannot express "the source said
    # nothing", so a stored value would not survive a round trip intact.
    # Readers derive counted-ness from the three equities instead.
    check("decision" not in a, "a missed double carries no decision flag")

    # The correct-no-double branch: the other key, and no loss to lose.
    key, a = _embedded_cube_analysis(
        _cd_decision("NO_DOUBLE", None, 0.55, 0.8), _EQ_FULL, 1, True, None)
    check(key == "cube_decision", "a correct no-double is keyed cube_decision")
    check(a is not None and a["equity_loss"] == 0.0,
          "and carries no error, which is the only value that key can store")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
