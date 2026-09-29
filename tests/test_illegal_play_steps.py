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

Three shapes of illegal play reach here, and only the first fits a ply record:
too many die-moves (collapsed to one step per checker), a hop that runs
*backwards*, and a hop longer than the three bits ``pips`` has. The last two are
restated as the position they produced.

The matches they came from are in the corpus as ``hQ8sVn2LbTdF4wRm`` (the
backwards hop) and ``rK7pXm4TqLb9NzWd`` (the ten-pip one), each in both of the
forms it was reported in -- see the last section, and samples/README.md.

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
    _flip_board, _notation_to_steps, _notation_to_steps_unsplit, _p1_to_absolute,
    _steps_per_roll, board_diff_has_non_forward_hop, fit_move_steps,
    notation_has_non_forward_hop,
)
from gvformat.mat import mat_to_ogxm  # noqa: E402
from gvformat.notation import canonical_notation  # noqa: E402
from gvformat.ogid import parse_ogid  # noqa: E402
from gvformat.xg import convert_xg  # noqa: E402
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


def _mover_board_of(ogid: str, color: int) -> list[int]:
    """The ply's board from the mover's own side of it."""
    st = parse_ogid(ogid)
    board = list(st.board)
    if st.on_roll != "W":
        board = _flip_board(board)
    return board if color else _flip_board(board)


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

    # --- a hop that runs backwards -----------------------------------------
    #
    # The other illegal shape, and the one no step can hold: one real match
    # plays a 6-5 as ``14/8 15/10 6/8`` -- both dice forward, then a checker 2
    # pips the wrong way, which the site let through. XG and HedgeHog both
    # record it. ``pips`` is an unsigned 3-bit *forward* distance, so the hop has
    # to reach the set-position rung, and it cannot get there on the step count:
    # the splitters answer a non-forward span with no steps at all.
    backwards = "14/8 15/10 6/8"

    check(len(_notation_to_steps(backwards, True, 6, 5)) == 2,
          "the dice split drops the backwards hop, leaving two steps for three")
    check(len(_notation_to_steps_unsplit(backwards, True, 6, 5)) == 2,
          "and so does one-step-per-span -- neither count can report the loss")

    check(notation_has_non_forward_hop(backwards),
          "the backwards hop is recognised from the notation instead")
    check(not notation_has_non_forward_hop(_ILLEGAL_NOTATION),
          "an over-long but all-forward illegal play is not confused with one")
    check(not notation_has_non_forward_hop("13/7 8/7"), "nor is a legal play")
    check(not notation_has_non_forward_hop("bar/22* 13/8"),
          "nor an entry from the bar")
    check(not notation_has_non_forward_hop("2/off 1/off"), "nor a bear-off")

    check(fit_move_steps(_notation_to_steps(backwards, True, 6, 5),
                         backwards, True, 6, 5) is None,
          "so no checker ply is offered for it -- straight to set position")
    check(fit_move_steps(_notation_to_steps(_ILLEGAL_NOTATION, True, 3, 1),
                         _ILLEGAL_NOTATION, True, 3, 1) is not None,
          "while the all-forward illegal play still collapses into a ply")

    # The same judgement from a board diff alone, for a converter with no
    # notation in hand. This is the real ply: white on 3(2) 5(2) 6(3) 7(4) 8(2)
    # 14 15.
    back_before = _board_p1(p3=2, p5=2, p6=3, p7=4, p8=2, p14=1, p15=1,
                            p2=-2, p16=-1, p20=-1, p21=-3, p22=-4, p23=-2, p24=-2)
    back_after = _board_p1(p3=2, p5=2, p6=2, p7=4, p8=4, p10=1,
                           p2=-2, p16=-1, p20=-1, p21=-3, p22=-4, p23=-2, p24=-2)
    check(board_diff_has_non_forward_hop(back_before, back_after, True),
          "the board diff says the same: no all-forward play reaches that board")
    legal_after = _board_p1(p3=2, p5=2, p6=3, p7=4, p8=3, p10=1,
                            p2=-2, p16=-1, p20=-1, p21=-3, p22=-4, p23=-2, p24=-2)
    check(not board_diff_has_non_forward_hop(back_before, legal_after, True),
          "and clears the legal 14/8 15/10 from the same board")

    # --- end to end, through the .mat converter ----------------------------
    #
    # A `.mat` states the play in notation and replays it verbatim, so its board
    # is right and only the ply record was losing the hop. Every board for the
    # rest of the game used to sit one checker out of place, with nothing
    # downstream able to tell.
    def _synthetic(white2: str) -> str:
        return f"""; [Site "test"]

 1 point match

 Game 1
 A : 0                                 B : 0
  1) 31: 8/5 6/5                          42: 24/22 13/9
  2) {white2}                    31: 9/6 22/21
  3) 11: 8/7 8/7 7/6 7/6
   Wins 1 point
"""

    illegal_doc = mat_to_ogxm(_synthetic("65: 13/7 13/8 6/8"))
    illegal_ply = illegal_doc["games"][0]["plies"][2]
    check(illegal_ply["action_id"] == 31,
          "the .mat converter states the backwards play as a set position")
    check(illegal_ply["d1"] == 6 and illegal_ply["d2"] == 5,
          "keeping the roll it stands in for")
    stated_board = _absolute_to_p1(illegal_ply["set_position"])
    check(stated_board[8] == 4 and stated_board[6] == 3,
          "and the board it states has the backwards checker on 8, not on 6")

    legal_ply = mat_to_ogxm(_synthetic("65: 13/7 13/8"))["games"][0]["plies"][2]
    check(legal_ply["action_id"] == 19 and len(legal_ply["moves"]) == 2
          and "set_position" not in legal_ply,
          "the same play without the hop stays an ordinary 6-5 checker ply")

    # The dice are what tell a restated *play* from the set-position ply that
    # opens an exported saved position, so the reader gives one OGIDs and the
    # other none.
    illegal_back = read_gvab(write_gvab(illegal_doc))["games"][0]
    check(illegal_back["plies"][2].get("ogid_before"),
          "a set-position ply with dice reads back with the OGIDs of its turn")

    def _checkers(ogid: str) -> str:
        return ":".join(ogid.split(":")[:2])

    check(_checkers(illegal_back["plies"][3]["ogid_before"])
          == _checkers(illegal_back["plies"][2]["ogid_after"]),
          "and the ply after it carries on from the board it stated")
    check(not read_gvab(write_gvab(_doc({
              "color": 1, "action_id": 31,
              "set_position": _p1_to_absolute(stated),
          })))["games"][0]["plies"][0].get("ogid_before"),
          "a set-position ply with no dice opens a game and gets none")

    # --- a hop longer than ``pips`` can count -------------------------------
    #
    # The third illegal shape, and the one that hid the longest. A real 3-3 was
    # played ``13/3 7/4``: the first span is ten pips, which the dice cannot
    # explain, so the splitters keep it whole and hand over a single step of
    # ``pips: 10``. ``pips`` is three bits, so ten was written as two and the
    # play read back out of the file as ``13/11`` -- a move nobody made, on a
    # ply whose step count and replayed board both looked right.
    long_span = "13/3 7/4"

    long_split = _notation_to_steps(long_span, True, 3, 3)
    check(long_split == [{"from": 12, "pips": 10}, {"from": 18, "pips": 3}],
          "the ten-pip span survives the dice split whole -- 3-3 cannot divide it")
    check(len(long_split) <= _steps_per_roll(3, 3),
          "so the step count reports nothing wrong: two steps for a four-hop roll")
    check(not notation_has_non_forward_hop(long_span),
          "and every hop runs forward, so that check clears it too")

    check(fit_move_steps(long_split, long_span, True, 3, 3) is None,
          "it still reaches the set-position rung -- on the hop length alone")
    check(fit_move_steps(_notation_to_steps("13/7 8/7", True, 3, 3),
                         "13/7 8/7", True, 3, 3) is not None,
          "while a 3-3 the dice do explain splits into hops that fit")
    check(fit_move_steps(_notation_to_steps("13/6", True, 4, 3),
                         "13/6", True, 4, 3)
          == [{"from": 12, "pips": 4}, {"from": 16, "pips": 3}],
          "and a seven-pip span is split by its dice, not refused for its length")

    long_threw = None
    try:
        write_gvab(_doc({"color": 1, "action_id": 11, "d1": 3, "d2": 3,
                         "moves": long_split}))
    except ValueError as err:
        long_threw = err
    check(long_threw is not None and "at most 7" in str(long_threw),
          "and the writer refuses the ten-pip step rather than wrapping it to two")

    # End to end: a `.mat` states the play in notation, so the converter sees
    # the span at full length and never builds the step that cannot hold it.
    long_mat = """; [Site "test"]

 1 point match

 Game 1
 A : 0                                 B : 0
  1) 61: 13/7 8/7                         42: 13/9 13/11
  2) 33: 13/3 7/4                         31: 11/8 9/8
  3) 11: 8/7 8/7 7/6 7/6
   Wins 1 point
"""

    long_doc = mat_to_ogxm(long_mat)
    long_ply = long_doc["games"][0]["plies"][2]
    check(long_ply["action_id"] == 31,
          "the .mat converter states the ten-pip play as a set position")
    check(long_ply["d1"] == 3 and long_ply["d2"] == 3,
          "keeping the 3-3 it stands in for")
    check(canonical_notation(_mover_board_of(long_ply["ogid_before"], long_ply["color"]),
                             _mover_board_of(long_ply["ogid_after"], long_ply["color"]),
                             3, 3) == long_span,
          "and the board it states reads back out as 13/3 7/4, not 13/11 7/4")

    long_back = read_gvab(write_gvab(long_doc))["games"][0]["plies"][2]
    check(long_back["action_id"] == 31
          and long_back["ogid_after"] == long_ply["ogid_after"],
          "and it survives the .gvab round trip that used to corrupt it")

    # --- the real match, in both of the forms it was reported in -----------
    #
    # `hQ8sVn2LbTdF4wRm` is where this came from: a HedgeHog transcription where
    # white played a 6-5 as `14/8 15/10 6/8`, the last hop running two pips
    # *backwards*. It is in the corpus twice over, and the two files reach the
    # set-position ply along different routes -- the .mat states the play in
    # notation, so `fit_move_steps` refuses it on the notation alone, while the
    # .xg's step list is simply short and it is the played candidate's own stored
    # position that gives the board away (`invalid_m == 2`, see
    # `_xg_candidate_board`). Landing on the same board is the whole point: that
    # board is what a user can see, since XG draws this play wrong and then
    # draws the next one right.
    mat_path = _REPO_ROOT / "samples" / "mat" / "hQ8sVn2LbTdF4wRm.mat"
    xg_path = _REPO_ROOT / "samples" / "xg" / "hQ8sVn2LbTdF4wRm.xg"
    if not mat_path.is_file() or not xg_path.is_file():
        print("SKIP  the real match (hQ8sVn2LbTdF4wRm is not in samples/)")
    else:
        pair = {
            "the .mat": mat_to_ogxm(mat_path.read_text()),
            "the .xg": convert_xg(xg_path),
        }
        restated = {}
        for name, doc in pair.items():
            found = [(gi, pi, ply)
                     for gi, game in enumerate(doc["games"])
                     for pi, ply in enumerate(game["plies"])
                     if ply.get("action_id") == 31]
            check(len(found) == 1,
                  f"{name} of the real match holds exactly one set-position ply")
            gi, pi, ply = found[0]
            restated[name] = (gi, pi, ply, doc)
            check((gi, pi) == (1, 37),
                  f"{name} puts it at game 2's twentieth play, where the 6-5 was")
            check(ply["d1"] == 6 and ply["d2"] == 5,
                  f"{name} keeps the roll it stands in for")
            check(canonical_notation(_mover_board_of(ply["ogid_before"], ply["color"]),
                                     _mover_board_of(ply["ogid_after"], ply["color"]),
                                     ply["d1"], ply["d2"]) == "15/10 14/8 6/8",
                  f"{name} states a board the backwards hop reads back out of")
            check(not ply.get("analysis"),
                  f"{name} carries no analysis on it -- the known cost of the encoding")
            nxt = doc["games"][gi]["plies"][pi + 1]
            check(_checkers(nxt["ogid_before"]) == _checkers(ply["ogid_after"]),
                  f"{name} has the next play carry on from the board it stated")

        (_, _, mat_ply, _), (gi, pi, xg_ply, xg_doc) = (restated["the .mat"],
                                                         restated["the .xg"])
        check(mat_ply["ogid_before"] == xg_ply["ogid_before"]
              and mat_ply["ogid_after"] == xg_ply["ogid_after"],
              "and the two files, read by two different routes, agree on both boards")

        # Through the binary, where the hop was being dropped. The synthetic
        # case above covers the encoding; this covers it on a ply deep inside a
        # game, with a live cube and a full analysis block around it.
        round_tripped = read_gvab(write_gvab(xg_doc))["games"][gi]["plies"][pi]
        check(round_tripped["action_id"] == 31
              and round_tripped["ogid_before"] == xg_ply["ogid_before"]
              and round_tripped["ogid_after"] == xg_ply["ogid_after"],
              "and a saved match brings that ply back with its boards intact")

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
                   luck_analyzer=None,
                   close_threshold_checker=None, close_threshold_cube=None,
                   error_threshold_checker=0.0, error_threshold_cube=0.0,
                   verbose=False,
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
