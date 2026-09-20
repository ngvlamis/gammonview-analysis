# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""A 3-tier preset tiers cube decisions, not just checker plays.

`mid_pass` + the thresholds used to gate the middle tier on checker plays
alone, so under world_class_fast a cube sitting right on its double point was
judged at the 3-ply screen -- less depth than a near-tied checker play got --
unless the player happened to get it wrong, in which case it jumped straight to
the rollout. Borderline is exactly where the screen is least reliable, so the
cube now takes the middle tier too.

Which tier a cube lands in is decided per side (doubler over its double point,
responder over its take point), against two independent thresholds:

  * an error costing MORE than error_threshold  -> second_pass (the rollout)
  * otherwise borderline within close_threshold -> mid_pass
  * otherwise                                   -> stay at the screen

...and then, crucially, the first test is asked AGAIN of the middle tier's own
numbers. `real_error` off the screen is only as good as the screen: a cube it
calls borderline can turn out, on the closer look, to be an error worth sizing.
Measured over 4,972 cube decisions, 0.4% of them are exactly that, by a median
of 0.07 equity and up to 0.22.

An error inside the cutoff is a borderline call, not a blunder, so it goes to
mid_pass: there is nothing there for a rollout to size. A side too trivial to
score (the _trivial_cube / _trivial_take_pass rules the PR count uses) never
earns the upgrade.

The two thresholds used to be one number, which forced one answer to two
different questions: how near a tie before the screen stops being trusted
(cheap to act on -- a middle tier) and how costly an error before its size is
worth measuring (expensive -- a rollout).

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


class _ShiftingAnalyzer(_FakeAnalyzer):
    """A tier that disagrees with the screen -- its own canned equities.

    The screen can badly understate a cube's margin; over 4,972 real cube
    decisions, 0.4% were called borderline by the 2-ply screen and then found
    by the middle tier to be genuine errors, by a median of 0.07. This is that
    situation in miniature.
    """

    def __init__(self, level, log, equities):
        super().__init__(level, log)
        self._equities = equities

    def cube_action(self, board, **kw):
        self._log.append(self.level)
        return _Result(self.level, *self._equities)


_EQUITIES = (0.0, 0.0, 0.0)  # set per case by tier_used()


def tier_used(nd, dt, dp, doubled, response, *, mid=True, two_pass=True,
              err_thr=None, close=None, mid_equities=None, mid_is_second=False):
    """Run one cube decision and return the level of the analyzer that decided it.

    `mid_equities`, when given, is what the MIDDLE tier reports instead of the
    screen's -- the case the re-check exists for, where a closer look revises a
    margin the screen misjudged.
    """
    global _EQUITIES
    _EQUITIES = (nd, dt, dp)
    if err_thr is None:
        err_thr = THR if (mid and two_pass) else 0.0
    log: list[str] = []
    top = _FakeAnalyzer("2T", log)
    if mid_is_second:
        mid_analyzer = top
    elif mid_equities is None:
        mid_analyzer = _FakeAnalyzer("4ply", log)
    else:
        mid_analyzer = _ShiftingAnalyzer("4ply", log, mid_equities)
    ctx = _EvalCtx(
        analyzer=top,
        base_analyzer=_FakeAnalyzer("3ply", log) if two_pass else None,
        mid_analyzer_checker=None,   # cube path only; the checker tier is separate
        mid_analyzer_cube=mid_analyzer if (mid and two_pass) else None,
        luck_analyzer=None,
        close_threshold_checker=None,
        close_threshold_cube=((THR if close is None else close)
                              if (mid and two_pass) else None),
        # The old single knob meant "an error inside the mid-tier margin is a
        # borderline call, not a blunder". Reproduce it exactly (err == close
        # with a mid tier, err == 0 without) so these cases still assert the
        # routing they were written for; the split is exercised by SPLIT_CASES.
        error_threshold_checker=err_thr,
        error_threshold_cube=err_thr,
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
    return log[-1], log


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

# The split: `close_threshold` and `error_threshold` are independent. These
# run close=0.08 (generous, the middle tier is cheap) with error=0.02 (the
# standard cutoff), which is the shape the shipped presets now use.
SPLIT_CASES = [
    # (name, nd, dt, dp, doubled, response, expected tier)
    # Wrong by 0.05: inside the close window, but well past the error cutoff,
    # so it is a real error to size. Under one shared knob at 0.08 this was
    # capped at the middle tier.
    ("error inside the close window still rolls out", 0.20, 0.15, 1.0, True, "take", "2T"),
    # Wrong by 0.01: inside both. Borderline, nothing for a rollout to size.
    ("error under the cutoff takes the middle tier", 0.20, 0.19, 1.0, True, "take", "4ply"),
    # Right, and near the double point: depth, as before.
    ("close and played right takes the middle tier", 0.20, 0.15, 1.0, False, None, "4ply"),
]

TWO_TIER_CASES = [
    # Without a mid tier nothing is marginal: every disagreement is an error,
    # exactly as before this change.
    ("2-tier borderline error still rolls out", 0.20, 0.19, 1.0, True, "take", "2T"),
    ("2-tier clear decision stays at screen", 0.20, 0.05, 1.0, False, None, "3ply"),
]


def main() -> int:
    failures = 0

    def check(label, got, want):
        nonlocal failures
        ok = got == want
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {got} (want {want})")

    for name, nd, dt, dp, doubled, resp, want in CASES:
        check(f"3-tier: {name}", tier_used(nd, dt, dp, doubled, resp)[0], want)

    for name, nd, dt, dp, doubled, resp, want in SPLIT_CASES:
        got, _ = tier_used(nd, dt, dp, doubled, resp, err_thr=0.02, close=0.08)
        check(f"split: {name}", got, want)

    for name, nd, dt, dp, doubled, resp, want in TWO_TIER_CASES:
        check(name, tier_used(nd, dt, dp, doubled, resp, mid=False)[0], want)

    # Single-pass preset: no screen, so the authoritative analyzer sees every
    # cube and there is no tier to choose.
    check("single-pass judges at the one level",
          tier_used(0.20, 0.19, 1.0, True, "take", two_pass=False)[0], "2T")

    # --- the re-check -------------------------------------------------------
    # The screen calls this borderline (nd 0.20 vs dt 0.19 -- wrong by 0.01,
    # inside the cutoff) and routes it to the middle tier. The middle tier says
    # the doubler was wrong by 0.10. That is a real error the screen understated
    # by 10x, and it must reach the rollout rather than being sized at 4-ply.
    got, log = tier_used(0.20, 0.19, 1.0, True, "take", mid_equities=(0.20, 0.10, 1.0))
    check("mid tier finds an error the screen missed -> rollout", got, "2T")
    check("  and it paid every tier on the way", log, ["3ply", "4ply", "2T"])

    # Same shape, but the middle tier agrees with the screen: no escalation,
    # and no wasted rollout.
    got, log = tier_used(0.20, 0.19, 1.0, True, "take", mid_equities=(0.20, 0.195, 1.0))
    check("mid tier confirms borderline -> stays", got, "4ply")

    # world_class_fast names second_pass's own level in mid_pass, so the middle
    # tier IS the authoritative analyzer. Re-running it would buy the same
    # object's same answer twice; the identity guard must skip it.
    got, log = tier_used(0.20, 0.19, 1.0, True, "take", mid_is_second=True)
    check("mid tier == second pass runs once, not twice", log, ["3ply", "2T"])

    print(f"\n{'all passed' if not failures else str(failures) + ' failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
