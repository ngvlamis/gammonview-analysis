# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The ``illegal_move`` flag, from the binary through to the aggregates.

The corpus contains one genuine illegal play -- ``5nqfGw9bWG3deTaU``, game 1,
``14/10 13/12`` off a 1-3: three die-moves for a two-hop roll -- and it is the
only file that exercises this path end to end. ``tests/test_read_gvab.py``
already decodes the flag off a synthetic file; what nothing asserted is that a
*real* analysed match carries it, that a rewrite keeps it, and that
``compute_aggregates`` then counts it.

That last one matters more than it looks. ``test_ogxm_stats`` compares
``illegal_moves`` against the authoritative summary on a match that has none,
so ``0 == 0`` passes whether the counter works or not. Here the count is 1, and
a 0 fails.

Reads ``tests/golden/5nqfGw9bWG3deTaU.fast.gvab`` as *data*, not as an
expectation: only structural fields are touched (a flag, a dice pair, a
decision boolean, a count), never a float the engine produced, so unlike
``test_ogxm_pipeline``'s byte comparison this runs anywhere. It needs no
engine.

Run: uv run python tests/test_illegal_move.py   (exit 0 = all passed)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gvformat import read_gvab, write_gvab, compute_aggregates
from gvformat.binary import DICE_TABLE

#: The corpus match with the illegal play, and the golden analysis of it.
STEM = "5nqfGw9bWG3deTaU"
GOLDEN = _ROOT / "tests" / "golden" / f"{STEM}.fast.gvab"

_passed = 0
_failed = 0


def check(cond: bool, msg: str) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"OK    {msg}")
    else:
        _failed += 1
        print(f"FAIL  {msg}")


def _flagged(doc: dict) -> list[tuple[int, int, dict]]:
    """(game_index, ply_index, ply) for every ply carrying the flag."""
    out = []
    for gi, game in enumerate(doc.get("games") or ()):
        for pi, ply in enumerate(game.get("plies") or ()):
            analysis = ply.get("analysis") or {}
            if analysis.get("illegal_move") or ply.get("illegal_move"):
                out.append((gi, pi, ply))
    return out


def main() -> int:
    if not GOLDEN.exists():
        print(f"SKIP: {GOLDEN} not found")
        return 0

    raw = GOLDEN.read_bytes()
    doc = read_gvab(raw)

    # --- 1. The corpus still holds the illegal play ---------------------------
    #
    # Everything below is vacuous without this, so it is asserted rather than
    # assumed. Losing the flag by re-analysing or re-exporting the corpus would
    # leave the rest of this file passing on an empty set.
    flags = _flagged(doc)
    check(len(flags) == 1,
          f"exactly one ply is flagged illegal (got {len(flags)})")
    if not flags:
        print(f"\n{_passed}/{_passed + _failed} checks passed.")
        return 1
    gi, pi, ply = flags[0]

    check((ply.get("analysis") or {}).get("illegal_move") is True,
          "the flag lives on the ply's analysis, where the format puts it")
    check((ply.get("action_id") or 0) <= 20,
          f"and it is a checker ply (action_id {ply.get('action_id')})")
    check({ply.get("d1"), ply.get("d2")} == {1, 3},
          f"the known case: the 1-3 in game {gi + 1} "
          f"(got {ply.get('d1')}-{ply.get('d2')})")

    # The steps were made to fit by export.fit_move_steps: the played move used
    # three die-moves, one more than a 1-3 allows, and was restated as one step
    # per span. A ply that kept all three could not be encoded at all.
    _, _, room = DICE_TABLE[ply["action_id"]]
    steps = ply.get("moves") or []
    check(len(steps) <= room,
          f"its steps fit what the roll allows ({len(steps)} of {room})")

    # --- 2. It survives the binary -------------------------------------------
    again = read_gvab(write_gvab(doc))
    flags2 = _flagged(again)
    check([(g, p) for g, p, _ in flags2] == [(gi, pi)],
          "write_gvab -> read_gvab keeps the flag on the same ply")

    # --- 3. compute_aggregates counts it -------------------------------------
    agg = compute_aggregates(doc)
    check(agg["match"]["illegal_moves"] == 1,
          f"the match reports 1 illegal move (got {agg['match']['illegal_moves']})")
    per_game = {g["game_index"]: g["illegal_moves"] for g in agg["games"]}
    check(per_game.get(doc["games"][gi].get("game_index", gi)) == 1,
          "attributed to the game it happened in")
    check(sum(per_game.values()) == 1,
          f"and to no other game ({per_game})")
    check(compute_aggregates(again)["match"]["illegal_moves"] == 1,
          "the count survives the rewrite too")

    # --- 4. The flag is not the PR denominator -------------------------------
    #
    # Two independent axes: `illegal_move` says the played board matched no
    # legal move, `decision` says whether the ply counts toward PR. By default
    # an illegal play is excluded, but what excludes it is `decision` -- so
    # flipping that one field has to move the total, or the counter is reading
    # the wrong flag.
    analysis = ply["analysis"]
    color = "white" if ply.get("color") == 1 else "black"
    check(analysis.get("decision") is False,
          "by default the illegal ply is not counted as a decision")

    before = agg["match"][color]["total_decisions"]
    analysis["decision"] = True
    after = compute_aggregates(doc)["match"][color]["total_decisions"]
    check(after == before + 1,
          f"and the denominator follows `decision`, not the flag "
          f"({color}: {before} -> {after})")
    check(compute_aggregates(doc)["match"]["illegal_moves"] == 1,
          "while the illegal-move count is unmoved by it")

    print(f"\n{_passed}/{_passed + _failed} checks passed.")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
