# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Tests for who a terminal ply belongs to.

Every end-of-game marker (24 game over, 26 final, 29 forfeit) is stamped with
the winner: nobody *does* a game-over, and ogxm_replay.cpp just carries the
winner through. Actions 27/28 are the exception -- resigning is an act, and the
player who resigns is the one who lost -- so those carry the resigner, both as
the ply's color and as the player on roll.

Mirrors gvformat-js/test/test-mat.js, which pins the same behaviour on the JS
side. The two are otherwise only compared through the sample corpus, which is
gitignored and therefore absent from a fresh clone.

Run directly:
    uv run python tests/test_resign_actor.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gvformat import mat_to_ogxm  # noqa: E402

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


def mat_with(turns, match_length=7):
    """A minimal two-player .mat whose game 1 is `turns`. alice sorts before
    bob, so alice is canonical white (winner code 0, ply color 1)."""
    return "\n".join([
        '; [Player 1 "alice"]',
        '; [Player 2 "bob"]',
        "",
        f"{match_length} point match",
        "",
        " Game 1",
        " alice: 0                                 bob: 0",
        *turns,
    ])


def end_actor(text):
    """(color, on_roll) of game 1's terminal ply, as "W"/"B" apiece.

    A ply is white at color 1. The OGID's field 5 is the *complement* of
    on-roll (it names the player who reached the position), so reading on-roll
    back out means flipping it.
    """
    game = mat_to_ogxm(text)["games"][0]
    end = game["plies"][-1]
    reached = end["ogid_before"].split(":")[4]
    return ("W" if end["color"] == 1 else "B", "B" if reached == "W" else "W")


RESIGNED = mat_with([
    "  1) 31: 8/5 6/5                              42: 8/4 6/4",
    "  2)                                          Resigned Game",
    "                                            Wins 2 points",
])

WON = mat_with([
    "  1) 31: 8/5 6/5                              42: 8/4 6/4",
    "  2) 65: 13/7 13/8                            Wins 1 points",
])

# ---------------------------------------------------------------------------
# A resignation belongs to the loser
# ---------------------------------------------------------------------------

game = mat_to_ogxm(RESIGNED)["games"][0]
check(game["plies"][-1]["action_id"] == 27,
      f'"Resigned Game" ends the game on action 27, got {game["plies"][-1]["action_id"]}')
check(game["winner"] == 1, f'precondition: bob won (got code {game["winner"]})')

color, on_roll = end_actor(RESIGNED)
check(color == "W", f"the resign ply belongs to the resigner, got {color}")
check(on_roll == "W",
      f"and the resigner is on roll, facing the roll they declined, got {on_roll}")

# ---------------------------------------------------------------------------
# Every other terminal marker still belongs to the winner
# ---------------------------------------------------------------------------

game = mat_to_ogxm(WON)["games"][0]
check(game["plies"][-1]["action_id"] == 24,
      f'an ordinary win still ends on action 24, got {game["plies"][-1]["action_id"]}')

color, _ = end_actor(WON)
check(color == "B", f"a game-over marker has no actor, so it names the winner, got {color}")

print()
print(f"{_checks - _failures}/{_checks} checks passed")
sys.exit(1 if _failures else 0)
