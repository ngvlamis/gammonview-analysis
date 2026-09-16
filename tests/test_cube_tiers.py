# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""A 3-tier preset tiers cube decisions, not just checker plays.

`mid_pass` + `close_threshold` used to gate the middle tier on checker plays
alone, so under world_class_fast a cube sitting right on its double point was
judged at the 3-ply screen -- less depth than a near-tied checker play got --
unless the player happened to get it wrong, in which case it jumped straight to
the rollout. Borderline is exactly where the screen is least reliable, so the
cube now takes the middle tier too.

Which tier a cube lands in is decided per side (doubler over its double point,
responder over its take point):

  * an error costing MORE than close_threshold -> second_pass (the rollout)
  * otherwise borderline within close_threshold -> mid_pass
  * otherwise                                   -> stay at the screen

An error inside the margin is a borderline call, not a blunder, so it goes to
mid_pass: there is nothing there for a rollout to size. A side too trivial to
score (the _trivial_cube / _trivial_take_pass rules the PR count uses) never
earns the upgrade.

Run: uv run python tests/test_cube_tiers.py   (exit 0 = all passed)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bgsage import STARTING_BOARD

from gvanalysis.game_eval import _EvalCtx, _eval_cube_decision

THR = 0.02


class _Probs:
    win = 0.5
    gammon_win = 0.1
    backgammon_win = 0.0
    gammon_loss = 0.1
    backgammon_loss = 0.0


class _Result:
    def __init__(self, level, nd, dt, dp):
        self.eval_level = level
        self.equity_nd = nd
        self.equity_dt = dt
        self.equity_dp = dp
        self.probs = _Probs()


class _FakeAnalyzer:
    """Returns the same canned equities whatever the tier, and records the call.

    The equities must not move between tiers: the point under test is which
    analyzer gets asked, and a deeper tier that changed its mind would make the
    resulting entry -- not the tier choice -- the thing being measured.
    """

    def __init__(self, level, log):
        self.level = level
        self._log = log

    def cube_action(self, board, **kw):
        self._log.append(self.level)
        return _Result(self.level, *_EQUITIES)


_EQUITIES = (0.0, 0.0, 0.0)  # set per case by tier_used()


def tier_used(nd, dt, dp, doubled, response, *, mid=True, two_pass=True):
    """Run one cube decision and return the level of the analyzer that decided it."""
    global _EQUITIES
    _EQUITIES = (nd, dt, dp)
    log: list[str] = []
    ctx = _EvalCtx(
        analyzer=_FakeAnalyzer("2T", log),
        base_analyzer=_FakeAnalyzer("3ply", log) if two_pass else None,
        mid_analyzer_checker=None,   # cube path only; the checker tier is separate
        mid_analyzer_cube=_FakeAnalyzer("4ply", log) if (mid and two_pass) else None,
        luck_analyzer=None,
        close_threshold=THR if (mid and two_pass) else None,
        verbose=False, all_moves=False, count_illegal=False,
        level_display="2T", game={"game_number": 1},
    )
    dec = {
        "kind": "cube", "board": list(STARTING_BOARD),
        "cube_value": 1, "cube_owner": "centered",
        "doubled": doubled, "response": response,
        "doubler": "White", "responder": "Black" if doubled else None,
        "is_doubler_p1": True, "away1": 0, "away2": 0, "is_crawford": False,
    }
    res = _eval_cube_decision(dec, ctx)
    entries = res.entries or ([res.pending_cube] if res.pending_cube else [])
    assert entries, "cube decision produced no entry"
    # The recorded eval_level and the last analyzer called must agree -- the
    # entry is what a reader sees, the log is what actually ran.
    assert entries[0]["eval_level"] == log[-1], (entries[0]["eval_level"], log)
    upgraded = entries[0]["upgraded"]
    assert upgraded == (len(log) > 1), (upgraded, log)
    return log[-1]


CASES = [
    # (name, nd, dt, dp, doubled, response, expected tier)
    # Clear no-double, correctly not doubled: nothing to re-examine.
    ("clear no-double played right", 0.20, 0.05, 1.0, False, None, "3ply"),
    # Same position doubled anyway -- wrong by 0.15, well past the margin.
    ("clear doubler blunder", 0.20, 0.05, 1.0, True, "take", "2T"),
    # Double point within 0.01: right answer, but the screen barely knows it.
    ("borderline no-double played right", 0.20, 0.19, 1.0, False, None, "4ply"),
    # Same borderline cube doubled: "wrong" by 0.01, which a rollout cannot
    # usefully size. Depth, not a rollout.
    ("borderline doubler error", 0.20, 0.19, 1.0, True, "take", "4ply"),
    # Doubler clear and correct; the take/pass is the borderline side.
    ("borderline take", 0.60, 0.99, 1.0, True, "take", "4ply"),
    # A real drop of a clear take (0.60 too much) -- that is a blunder to size.
    ("responder blunder", 0.30, 0.40, 1.0, True, "pass", "2T"),
    # Double and no-double within 0.001: uncounted for PR, so not worth depth
    # even though it is inside close_threshold.
    ("trivial cube", 0.50, 0.5003, 1.0, False, None, "3ply"),
]

TWO_TIER_CASES = [
    # Without a mid tier nothing is marginal: every disagreement is an error,
    # exactly as before this change.
    ("2-tier borderline error still rolls out", 0.20, 0.19, 1.0, True, "take", "2T"),
    ("2-tier clear decision stays at screen", 0.20, 0.05, 1.0, False, None, "3ply"),
]


def main() -> int:
    failures = 0
    for name, nd, dt, dp, doubled, resp, want in CASES:
        got = tier_used(nd, dt, dp, doubled, resp)
        ok = got == want
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  3-tier: {name}: {got} (want {want})")

    for name, nd, dt, dp, doubled, resp, want in TWO_TIER_CASES:
        got = tier_used(nd, dt, dp, doubled, resp, mid=False)
        ok = got == want
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {got} (want {want})")

    # Single-pass preset: no screen, so the authoritative analyzer sees every
    # cube and there is no tier to choose.
    got = tier_used(0.20, 0.19, 1.0, True, "take", two_pass=False)
    ok = got == "2T"
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  single-pass judges at the one level: {got}")

    print(f"\n{'all passed' if not failures else str(failures) + ' failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
