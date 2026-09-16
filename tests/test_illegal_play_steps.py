# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""An illegal play must not corrupt the boards that follow it.

A source can record a play that broke the rules (XG flags one with
``invalid_m == 2``; sites do let them through). Such a play can use more
die-moves than the roll has -- 13/9 with a 3-1, then 12/11 -- and the natural
step expansion then needs three steps for a two-hop roll. A ply record has room
for exactly the roll's hops, so the third step used to be dropped on write; from
that ply on the replayed board was one checker off, later plies lifted checkers
off empty points, and the resulting 16-checker position segfaulted the engine's
bearoff lookup.

Run directly:
    uv run python tests/test_illegal_play_steps.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import read_gvab, write_gvab  # noqa: E402
from gvformat.export import (  # noqa: E402
    _notation_to_steps, _notation_to_steps_unsplit, _p1_to_absolute,
    _steps_per_roll,
)
from gvformat.legality import board_problems  # noqa: E402
from gvformat.reader import _absolute_to_p1, _apply_moves_p1  # noqa: E402

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


# The real ply, in the mover's own numbering: one checker 13/9 (both dice on a
# 3-1) and then a third die-move, 12/11.
_ILLEGAL_NOTATION = "13/9 12/11"


def _board_p1(**points: int) -> list[int]:
    """P1/White frame board from {point: signed count} (bar25=white bar)."""
    b = [0] * 26
    for k, v in points.items():
        b[int(k[1:])] = v
    return b


def main() -> int:
    # --- the expansion overflows, the collapse does not --------------------
    split = _notation_to_steps(_ILLEGAL_NOTATION, True, 3, 1)
    check(len(split) == 3,
          "the dice split three hops out of an illegal 3-1 play")
    check(len(split) > _steps_per_roll(3, 1),
          "which is one more step than a 3-1 ply record holds")

    collapsed = _notation_to_steps_unsplit(_ILLEGAL_NOTATION, True, 3, 1)
    check(collapsed == [{"from": 12, "pips": 4}, {"from": 13, "pips": 1}],
          "collapsed to one step per checker, the 4-pip hop kept whole")
    check(len(collapsed) <= _steps_per_roll(3, 1),
          "which fits a 3-1 ply record")

    # A pips value the dice cannot explain is still a legal *step*: the field
    # holds 1-6, and it is the only way to state a hop the roll does not allow.
    check(all(1 <= s["pips"] <= 6 for s in collapsed),
          "every collapsed step stays inside the 1-6 pips a step can encode")

    # --- and both replay to the same board ---------------------------------
    before = _board_p1(p12=1, p13=1, p20=-2)
    check(_apply_moves_p1(before, split, True) == _apply_moves_p1(before, collapsed, True),
          "collapsing the hops replays to exactly the same board")

    # --- a legal play is untouched -----------------------------------------
    legal = "13/10 13/12"
    check(_notation_to_steps(legal, True, 3, 1) == _notation_to_steps_unsplit(legal, True, 3, 1),
          "a legal play's steps are the same either way")
    check(_notation_to_steps_unsplit("13/9", True, 4, 4)
          == [{"from": 12, "pips": 4}],
          "a span the roll does explain is still one step when it fits")

    # --- the writer refuses what it cannot carry ---------------------------
    def _doc(ply: dict) -> dict:
        return {
            "match_length": 7, "player_white": "W", "player_black": "B",
            "games": [{"game_index": 0, "plies": [ply]}],
        }

    over = _doc({"color": 1, "action_id": 2, "d1": 3, "d2": 1, "moves": split})
    try:
        write_gvab(over)
        check(False, "writing an over-long ply raises instead of dropping a step")
    except ValueError as exc:
        check("room for 2" in str(exc),
              "writing an over-long ply raises instead of dropping a step")

    fits = _doc({"color": 1, "action_id": 2, "d1": 3, "d2": 1, "moves": collapsed})
    back = read_gvab(write_gvab(fits))
    check(back["games"][0]["plies"][0]["moves"] == collapsed,
          "the collapsed steps survive the .gvab round trip, 4 pips and all")

    # --- a set-position ply carries the board it states --------------------
    stated = _board_p1(p6=5, p8=3, p13=5, p24=2, p1=-2, p12=-5, p17=-3, p19=-5)
    sp = _doc({"color": 0, "action_id": 31, "d1": 3, "d2": 1,
               "set_position": _p1_to_absolute(stated)})
    sp_back = read_gvab(write_gvab(sp))["games"][0]["plies"][0]
    check(sp_back["d1"] == 3 and sp_back["d2"] == 1,
          "a set-position ply keeps the dice of the play it stands in for")
    check(_absolute_to_p1(sp_back["set_position"]) == stated,
          "and round-trips the board it states")

    # --- and the analyzer moves its running board onto it ------------------
    # (gvanalysis, so this one needs the engine installed -- it only imports
    # bgsage's board helpers, no evaluation happens here.)
    from gvanalysis.ogxm_reconstructor import reconstruct_game_decisions

    game = {
        "game_index": 0,
        "plies": [
            {"color": 1, "action_id": 31, "d1": 3, "d2": 1,
             "set_position": _p1_to_absolute(stated)},
            {"color": 0, "action_id": 2, "d1": 3, "d2": 1,
             "moves": [{"from": 13, "pips": 3}, {"from": 13, "pips": 1}]},
        ],
    }
    from bgsage.board import flip_board

    decs = reconstruct_game_decisions(game, 0, 0, 7, "W", "B")
    checker = next(d for d in decs if d["kind"] == "checker")
    check(checker["board"] == flip_board(stated),
          "the decision after a set-position ply sits on the board it stated")

    # --- the corruption this all prevents is recognisable ------------------
    check(board_problems(stated) == [],
          "a real position has nothing wrong with it")
    check(board_problems(_board_p1(p6=8, p8=8, p20=-2)),
          "16 checkers on a side is reported")
    check(board_problems(_board_p1(p6=5, p8=3, p13=5, p24=1, p25=1, p1=-2,
                                   p12=-5, p17=-3, p19=-5)) == [],
          "checkers on both bars are counted to the right side, not the mover")

    # ...and reaches the engine as an error naming the ply, never as a segfault
    # in a worker the caller can only see as "terminated abruptly".
    from gvanalysis.game_eval import _EvalCtx, _require_playable

    ctx = _EvalCtx(analyzer=None, base_analyzer=None, mid_analyzer_checker=None,
                   mid_analyzer_cube=None,
                   luck_analyzer=None, close_threshold=None, verbose=False,
                   all_moves=False, count_illegal=False, level_display="",
                   game={"game_number": 4})
    impossible = {
        "kind": "checker", "board": list(stated), "dice": [3, 1],
        "board_played": _board_p1(p6=8, p8=8, p20=-2), "player": "Nick",
    }
    try:
        _require_playable(impossible, ctx)
        check(False, "an impossible played board fails the run, naming the ply")
    except ValueError as exc:
        check("game 4" in str(exc) and "Nick's 3-1" in str(exc),
              "an impossible played board fails the run, naming the ply")

    print()
    print(f"{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
