# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Tests for the step split behind an *alternative's* ``move``.

Sibling of test_xg_move_steps.py / test_bgf_move_steps.py, which cover the same
split for the move that was actually played. The played move had to be fixed
because its steps are replayed into the running board: a hop onto a point the
opponent has made reads back as a hit and corrupts every board from that ply
on.

An alternative is never replayed, so a bad split there cannot corrupt a match
-- but the steps are exactly what the viewer draws its move arrows from, so the
rejected candidate is *shown* with a checker landing on a stack of enemy
checkers and then carrying on. Same root cause, visible instead of fatal.

Mirrors gvformat-js/test/test-alternative-move-steps.js.

Run directly:
    uv run python tests/test_alternative_move_steps.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import convert_xg, parse_ogid  # noqa: E402
from gvformat.bgf import _build_alternatives as _bgf_build_alternatives  # noqa: E402
from gvformat.export import _checker_analysis, _flip_board  # noqa: E402
from gvformat.reader import _apply_moves_p1  # noqa: E402

_SAMPLES = _REPO_ROOT / "samples" / "xg"

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


def _to_p1(ogid: str) -> list[int]:
    """A ply's OGID board in the P1/White frame ``_apply_moves_p1`` works in."""
    st = parse_ogid(ogid)
    board = list(st.board)
    return board if st.on_roll == "W" else _flip_board(board)


def _side_counts(board_p1: list[int]) -> tuple[int, int]:
    """(white, black) checker counts.

    Points 1-24 are signed (+white / -black); the two bars are unsigned counts
    at either end -- index 25 is white's, index 0 is black's (see
    ``_flip_board``, which swaps them without negating).
    """
    white, black = board_p1[25], board_p1[0]
    for i in range(1, 25):
        if board_p1[i] > 0:
            white += board_p1[i]
        else:
            black -= board_p1[i]
    return white, black


def main() -> int:
    # The same shape the played-move tests use: a checker on the mover's
    # 24-point runs to 18 with a 5-1. Going 5 first lands on 19, which the
    # opponent has five checkers on; going 1 first stops on 23, which is open.
    primed_19 = [0] * 26
    primed_19[24] = 1
    primed_19[19] = -5

    options = [{
        "equity": 0.1, "played": True, "move": "24/18", "notation": "24/18",
        "probs": [0.5, 0.1, 0.01, 0.1, 0.01],
    }]

    # --- 1. The BGF alternatives builder ---------------------------------
    alts = _bgf_build_alternatives(options, False, 5, 1, primed_19)
    check([s["pips"] for s in alts[0]["move"]] == [1, 5],
          "bgf: an alternative steps around a made intermediate (1 first, via 23)")
    alts = _bgf_build_alternatives(options, False, 5, 1)
    check([s["pips"] for s in alts[0]["move"]] == [5, 1],
          "bgf: with no board the canonical larger-die-first order is kept")

    # --- 2. The analyze-path alternatives builder ------------------------
    entry = {"move_options": options}
    analysis = _checker_analysis(entry, False, 5, 1, 0, primed_19)
    check([s["pips"] for s in analysis["alternatives"][0]["move"]] == [1, 5],
          "analyze: an alternative steps around a made intermediate")
    analysis = _checker_analysis(entry, False, 5, 1, 0)
    check([s["pips"] for s in analysis["alternatives"][0]["move"]] == [5, 1],
          "analyze: with no board the canonical larger-die-first order is kept")

    # --- 3. The whole-file invariant -------------------------------------
    # A play cannot change how many checkers the *opponent* has. Bearing off
    # only ever removes the mover's own, and a real hit moves an opponent
    # checker to the bar rather than off the board -- so the count is exactly
    # conserved.
    #
    # A hop through a made point breaks it: the replay scores it as a hit, and
    # a point holding five enemy checkers collapses to one of the mover's plus
    # a single bar checker, so the opponent silently loses four. (Counting *up*
    # to an impossible 16 is the same corruption seen several plies later, once
    # the wrong board has been played on; one ply in isolation only ever loses
    # checkers.)
    samples = sorted(_SAMPLES.glob("*.xg"))
    check(len(samples) > 0, f"XG corpus is present ({len(samples)} files)")

    total = stolen = 0
    for path in samples:
        gva = convert_xg(path)
        for game in gva["games"]:
            for ply in game["plies"]:
                alternatives = (ply.get("analysis") or {}).get("alternatives")
                if not alternatives or not ply.get("ogid_before"):
                    continue
                mover_is_white = bool(ply.get("color"))
                before = _to_p1(ply["ogid_before"])
                opp_before = _side_counts(before)[1 if mover_is_white else 0]
                for alt in alternatives:
                    if not alt.get("move"):
                        continue
                    total += 1
                    after = _apply_moves_p1(before, alt["move"], mover_is_white)
                    opp_after = _side_counts(after)[1 if mover_is_white else 0]
                    if opp_after != opp_before:
                        stolen += 1
                        if stolen <= 3:
                            print(f"      {path.name}: dice {ply['d1']}-{ply['d2']} "
                                  f"\"{alt.get('notation')}\" -> {alt['move']} "
                                  f"(opponent {opp_before} -> {opp_after})")

    check(total > 0, f"corpus alternatives replayed ({total})")
    check(stolen == 0,
          f"no alternative's steps take checkers off the opponent ({stolen} do)")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
