# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Tests for gvformat.legality — the rules-based illegal-move detector.

Boards are mover-relative: index 1 = the mover's ace point, 24 = entry point,
25 = the mover's own bar. See gvformat/legality.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gvformat.legality import (  # noqa: E402
    is_play_legal, legal_plays, max_dice_playable, normalize_play,
)

_checks = 0
_failures = 0


def check(cond, label):
    global _checks, _failures
    _checks += 1
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}")


def board(**points):
    """Build a 26-slot count array from {point: count} kwargs ('bar' allowed)."""
    b = [0] * 26
    for k, v in points.items():
        b[25 if k == "bar" else int(k.lstrip("p"))] = v
    return b


# ---------------------------------------------------------------------------
# Opening position sanity
# ---------------------------------------------------------------------------

OPEN_MINE = board(p6=5, p8=3, p13=5, p24=2)
OPEN_OPP = board(p1=2, p12=5, p17=3, p19=5)

check(
    is_play_legal(OPEN_MINE, OPEN_OPP, 5, 3, [(8, 3), (6, 3)]),
    "opening 5-3: 8/3 6/3 is legal",
)
check(
    is_play_legal(OPEN_MINE, OPEN_OPP, 5, 3, [(6, 3), (8, 3)]),
    "sub-move order does not matter",
)
check(
    not is_play_legal(OPEN_MINE, OPEN_OPP, 5, 3, [(8, 3)]),
    "opening 5-3: playing only one die is illegal when both are playable",
)
check(
    not is_play_legal(OPEN_MINE, OPEN_OPP, 5, 3, [(13, 7), (8, 3)]),
    "opening 5-3: 13/7 uses a 6, not a die in hand",
)
check(
    not is_play_legal(OPEN_MINE, OPEN_OPP, 5, 3, [(24, 19), (8, 3)]),
    "cannot land on an opponent point held by 5 checkers",
)

# ---------------------------------------------------------------------------
# Maximisation: must play as many dice as possible
# ---------------------------------------------------------------------------

# Mover has one checker on the bar; opponent's home board is fully closed
# except point 24 (entered with a 1).
CLOSED_OPP = board(p19=2, p20=2, p21=2, p22=2, p23=2, p24=0)
BAR_MINE = board(bar=1, p13=5)

check(max_dice_playable(BAR_MINE, board(**{f"p{p}": 2 for p in range(19, 25)}), 6, 5) == 0,
      "fully closed board with a checker on the bar is a forced dance")
check(is_play_legal(BAR_MINE, board(**{f"p{p}": 2 for p in range(19, 25)}), 6, 5, []),
      "playing nothing is legal when it is a genuine dance")
check(not is_play_legal(BAR_MINE, CLOSED_OPP, 1, 3, []),
      "claiming a dance is illegal when entry with a 1 exists")

# Larger-die rule: when only one die can be played, it must be the larger.
# The mover's last checker is on the bar. A 6 enters on 19 and a 3 enters on
# 22, but point 16 is blocked, so whichever entry is made the other die is
# dead — exactly one die is playable, and the rules require it to be the 6.
LAST_CHECKER = board(bar=1)
BLOCK_16 = board(p16=2)
check(is_play_legal(LAST_CHECKER, BLOCK_16, 6, 3, [(25, 19)]),
      "when only one die can be played, entering with the larger (6) is legal")
check(not is_play_legal(LAST_CHECKER, BLOCK_16, 6, 3, [(25, 22)]),
      "entering with the smaller (3) is illegal when the 6 is also playable")
check(max_dice_playable(LAST_CHECKER, BLOCK_16, 6, 3) == 1,
      "only one die is playable in that position")

# ---------------------------------------------------------------------------
# Doubles: all four must be played when possible
# ---------------------------------------------------------------------------

DBL_MINE = board(p6=2, p8=2)
DBL_OPP = board()
plays, used = legal_plays(DBL_MINE, DBL_OPP, 1, 1)
check(used == 4, "doubles 1-1 with open board must use all four dice")
check(not is_play_legal(DBL_MINE, DBL_OPP, 1, 1, [(6, 5), (6, 5), (8, 7)]),
      "playing only 3 of 4 doubles is illegal (the real g2 m35 error)")
check(is_play_legal(DBL_MINE, DBL_OPP, 1, 1, [(6, 5), (6, 5), (8, 7), (8, 7)]),
      "playing all 4 doubles is legal")

# ---------------------------------------------------------------------------
# Bearing off
# ---------------------------------------------------------------------------

OFF_MINE = board(p1=1, p3=1)
OFF_OPP = board()
check(is_play_legal(OFF_MINE, OFF_OPP, 4, 1, [(3, 0), (1, 0)]),
      "bear off both: 3/off with a 4 (overshoot from highest) and 1/off")
check(is_play_legal(OFF_MINE, OFF_OPP, 4, 1, [(3, 2), (2, 0)]),
      "3/2 2/off is an alternative legal play for 4-1")
# The bare (3, off) describes the same end position as 3/2 2/off (checker on 1
# remains), which is legally reachable using both dice -- so it is legal.
# Legality is about the resulting position, not the sub-move spelling.
check(is_play_legal(OFF_MINE, OFF_OPP, 4, 1, [(3, 0)]),
      "net-form 3/off is legal: same position as 3/2 2/off, reached with both dice")
# A genuine under-play: two on the 6 with a 6-1 obliges the 1 onto the other
# checker (6/5). Stopping after one bear-off lands in a position no maximal play
# reaches, so it is illegal (the g6 m41 error shape).
check(not is_play_legal(board(p6=2), OFF_OPP, 6, 1, [(6, 0)]),
      "bearing off only one checker is illegal when the other die must still play")

# Overshoot is only legal from the highest occupied point.
HIGH_MINE = board(p2=1, p5=1)
check(not is_play_legal(HIGH_MINE, OFF_OPP, 6, 1, [(2, 0), (5, 4)]),
      "cannot bear off from point 2 with a 6 while point 5 is occupied")
check(is_play_legal(HIGH_MINE, OFF_OPP, 6, 1, [(5, 0), (2, 1)]),
      "6 bears off the highest checker (point 5)")

# Cannot bear off while a checker is outside the home board.
NOT_HOME = board(p3=1, p9=1)
check(not is_play_legal(NOT_HOME, OFF_OPP, 3, 1, [(3, 0), (9, 8)]),
      "no bearing off while a checker sits outside the home board")

# ---------------------------------------------------------------------------
# Hitting
# ---------------------------------------------------------------------------

HIT_MINE = board(p6=2, p8=1)
HIT_OPP = board(p3=1, p5=2)
check(is_play_legal(HIT_MINE, HIT_OPP, 5, 2, [(8, 3), (6, 4)]),
      "landing on a lone opponent blot is legal (a hit)")
check(not is_play_legal(HIT_MINE, HIT_OPP, 1, 2, [(6, 5), (8, 6)]),
      "landing on an opponent point of 2 is illegal")

# ---------------------------------------------------------------------------
# normalize_play
# ---------------------------------------------------------------------------

check(normalize_play([(8, 3), (6, 3)]) == ((6, 3), (8, 3)), "normalize sorts pairs")
check(normalize_play([(3, -2)]) == ((3, 0),), "normalize maps bear-off to 0")
check(normalize_play([(-1, -1), (5, 2)]) == ((5, 2),), "normalize drops empty slots")

# ---------------------------------------------------------------------------
# Combined multi-die pairs (BGBlitz records a checker moved with more than one
# die as a single from->to, e.g. 24/15 for a 6-3 rather than 24/18 18/15)
# ---------------------------------------------------------------------------

check(is_play_legal(OPEN_MINE, OPEN_OPP, 6, 3, [(24, 15)]),
      "combined 24/15 (one checker, both dice) is legal")
check(is_play_legal(OPEN_MINE, OPEN_OPP, 6, 3, [(24, 18), (18, 15)]),
      "the same play spelled out per-die is legal too")
check(not is_play_legal(
          board(p24=2, p13=2, p8=3, p6=5), board(p18=2, p21=2, p1=2, p12=5),
          6, 3, [(24, 15)]),
      "combined 24/15 is illegal when both intermediates (18 and 21) are blocked")
check(is_play_legal(board(p8=2), board(), 2, 2, [(8, 4), (8, 4)]),
      "combined doubles: both checkers 8/4 (2-2) is legal")
check(is_play_legal(board(p5=1), board(), 2, 3, [(5, 0)]),
      "combined forced two-die bear-off 5/off (2-3) is legal")

print()
print(f"{_checks - _failures}/{_checks} checks passed")
sys.exit(1 if _failures else 0)
