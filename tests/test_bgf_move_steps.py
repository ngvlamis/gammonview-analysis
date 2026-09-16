# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Tests for the step split behind a BGF ply's ``moves``.

BGBlitz records only a move's endpoints. When one checker plays both dice --
"18/7" off a 5-6 -- the intermediate point is not on the record and has to be
inferred, and only one of the two candidates may be legal. Getting it wrong is
not cosmetic: replaying a step onto a point the opponent has made reads as a
hit, which turns their two checkers into one of yours plus a bar checker. The
side gains a checker, and every board replayed from that ply onward is wrong
-- far enough wrong that bgsage segfaults indexing its bearoff table with a
16-checker home board.

Two levels here: the splitter itself, and the whole-file invariant that a ply's
``moves`` replay onto its own ``ogid_after``. The second is what actually
failed; it holds for every sample in the corpus, none of which happened to
contain the shape (which is why this went unnoticed).

Run directly:
    uv run python tests/test_bgf_move_steps.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import convert_bgf, parse_ogid  # noqa: E402
from gvformat.export import _flip_board, _notation_to_steps  # noqa: E402
from gvformat.reader import _apply_moves_p1  # noqa: E402

_SAMPLES = _REPO_ROOT / "samples" / "bgf"

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
    """A ply's OGID board in the P1/White frame ``_apply_moves_p1`` works in.

    ``parse_ogid`` hands back the board from the perspective of whoever owes
    the next action, which alternates every ply -- comparing without this flip
    makes every second ply look broken.
    """
    st = parse_ogid(ogid)
    board = list(st.board)
    return board if st.on_roll == "W" else _flip_board(board)


def main() -> int:
    # --- 1. The splitter -------------------------------------------------
    # Mover-perspective board: own checkers positive, opponent's negative.
    # A checker on 18 plays 5-6 to 7. Going 6 first lands on 12; going 5 first
    # lands on 13. Only one is open at a time below.
    blocked_12 = [0] * 26
    blocked_12[18] = 1
    blocked_12[12] = -2          # opponent's made point: 18/12 is not playable
    blocked_12[13] = 0

    steps = _notation_to_steps("18/7", False, 5, 6, board=blocked_12)
    check([s["pips"] for s in steps] == [5, 6],
          "a blocked intermediate is stepped around (5 first, via 13)")
    check([s["from"] for s in steps] == [18, 13],
          "and the hop starts where the first die left it")

    blocked_13 = [0] * 26
    blocked_13[18] = 1
    blocked_13[13] = -2          # the mirror case: now 18/13 is the illegal one
    steps = _notation_to_steps("18/7", False, 5, 6, board=blocked_13)
    check([s["pips"] for s in steps] == [6, 5],
          "the other block sends it the other way (6 first, via 12)")

    # A lone enemy checker is a blot, not a block -- landing there is a hit and
    # a perfectly legal route, so the canonical larger-die-first order stands.
    blot_12 = [0] * 26
    blot_12[18] = 1
    blot_12[12] = -1
    check([s["pips"] for s in _notation_to_steps("18/7", False, 5, 6, board=blot_12)] == [6, 5],
          "a blot does not divert the split")

    # No board (an unplayed alternative, which has only a notation string):
    # the tie-break still has to produce something deterministic.
    check([s["pips"] for s in _notation_to_steps("18/7", False, 5, 6)] == [6, 5],
          "with no board the canonical larger-die-first order is kept")

    # --- 2. The whole-file invariant -------------------------------------
    # Every ply's steps, replayed onto its own board-before, must land exactly
    # on its board-after. This is the property the bad split violated.
    samples = sorted(_SAMPLES.glob("*.bgf"))
    check(len(samples) > 0, f"BGF corpus is present ({len(samples)} files)")

    total = mismatched = 0
    for path in samples:
        gva = convert_bgf(path)
        for game in gva["games"]:
            for ply in game["plies"]:
                before, after = ply.get("ogid_before"), ply.get("ogid_after")
                if not before or not after or ply.get("d1") is None:
                    continue
                total += 1
                replayed = _apply_moves_p1(
                    _to_p1(before), ply.get("moves") or [], bool(ply.get("color")),
                )
                if replayed != _to_p1(after):
                    mismatched += 1
                    if mismatched <= 3:
                        print(f"      {path.name}: dice {ply['d1']}-{ply['d2']} "
                              f"moves {ply.get('moves')}")

    check(total > 0, f"corpus plies with dice replayed ({total})")
    check(mismatched == 0,
          f"every ply's moves replay onto its own ogid_after ({mismatched} do not)")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
