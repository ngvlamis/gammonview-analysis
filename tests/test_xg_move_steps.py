# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Tests for the step split behind an XG ply's ``moves``.

XG records a played move as from/to endpoints, so a checker that plays both
dice -- "24/18" off a 5-1 -- leaves the intermediate point off the record. It
has to be inferred, and only one of the two routes may be open. Getting it
wrong is not cosmetic: a step onto a point the opponent has *made* replays as a
hit, turning their five checkers into one of yours plus a bar checker. From
that ply on every board is wrong -- far enough wrong that a later ply lifts a
checker off an empty point and mints checkers until the position is impossible,
which is where bgsage segfaults indexing its bearoff table.

The BGF converter passes the pre-move board to ``_notation_to_steps`` for
exactly this reason (see test_bgf_move_steps.py); the XG one did not, and a 5-1
run off the midpoint past a made 5-prime is the shape that exposes it.

Run directly:
    uv run python tests/test_xg_move_steps.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import convert_xg, parse_ogid  # noqa: E402
from gvformat.export import _flip_board, _notation_to_steps  # noqa: E402
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
    # The real shape, from game 4 of a 13-point match: a checker on the mover's
    # 24-point runs to 18 with a 5-1. Going 5 first lands on 19, which the
    # opponent has five checkers on; going 1 first stops on 23, which is open.
    primed_19 = [0] * 26
    primed_19[24] = 1
    primed_19[19] = -5           # opponent's made point: 24/19 is not playable

    steps = _notation_to_steps("24/18", False, 5, 1, board=primed_19)
    check([s["pips"] for s in steps] == [1, 5],
          "a made intermediate is stepped around (1 first, via 23)")
    check([s["from"] for s in steps] == [24, 23],
          "and the hop starts where the first die left it")

    # Without the board the tie-break takes the larger die first and routes the
    # checker straight through the made point -- the bug this guards.
    check([s["pips"] for s in _notation_to_steps("24/18", False, 5, 1)] == [5, 1],
          "with no board the canonical larger-die-first order is kept")

    # A lone enemy checker is a blot, not a block: landing there is a hit and a
    # perfectly legal route, so the canonical order stands.
    blot_19 = [0] * 26
    blot_19[24] = 1
    blot_19[19] = -1
    check([s["pips"] for s in _notation_to_steps("24/18", False, 5, 1, board=blot_19)] == [5, 1],
          "a blot does not divert the split")

    # --- 2. The whole-file invariant -------------------------------------
    # Every ply's steps, replayed onto its own board-before, must land exactly
    # on its board-after. This is the property the bad split violated.
    samples = sorted(_SAMPLES.glob("*.xg"))
    check(len(samples) > 0, f"XG corpus is present ({len(samples)} files)")

    total = mismatched = 0
    for path in samples:
        gva = convert_xg(path)
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
