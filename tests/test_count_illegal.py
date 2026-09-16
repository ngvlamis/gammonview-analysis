# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""``--count-illegal``: what the switch does to PR.

An illegal play is one whose played board matched no move the engine
generated -- usually a transcription error rather than a rules violation. By
default it is *detected* but not *counted*: the ply carries
``analysis.illegal_move``, its ``decision`` is false, and it stays out of the
PR denominator, because scoring a move nobody made would attribute an error to
a player who did not commit it. ``--count-illegal`` includes it.

Nothing tested the switch, so nothing pinned which of the two things it
changes. It changes counting, not detection: the flag is set either way.

The one corpus match with a genuine illegal play is analysed twice, and the
assertions are relational -- decisions +1, error + that ply's own
``equity_loss`` -- never an absolute PR, since bgsage is not bit-reproducible
across CPUs (see ``tests/fixtures.py``). Needs the engine and analyses one
match twice, so it costs ~15s.

Run: uv run python tests/test_count_illegal.py   (exit 0 = all passed)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gvanalysis.match import analyze_file
from gvformat import compute_aggregates

#: The corpus match holding the illegal play (14/10 13/12 off a 1-3).
MAT = _ROOT / "samples" / "mat" / "5nqfGw9bWG3deTaU.mat"
PRESET = "fast"

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


def _analyze(count_illegal: bool) -> dict:
    return analyze_file(str(MAT), preset=PRESET, quiet=True,
                        show_progress=False, jobs=1, count_illegal=count_illegal)


def _flagged(doc: dict) -> list[tuple[int, int, dict]]:
    return [(gi, pi, ply)
            for gi, game in enumerate(doc.get("games") or ())
            for pi, ply in enumerate(game.get("plies") or ())
            if (ply.get("analysis") or {}).get("illegal_move")]


def main() -> int:
    if not MAT.exists():
        print(f"SKIP: {MAT} not found")
        return 0

    off = _analyze(False)
    on = _analyze(True)

    # --- 1. Detection is not what the switch controls ------------------------
    off_flags, on_flags = _flagged(off), _flagged(on)
    check(len(off_flags) == 1 and len(on_flags) == 1,
          f"the illegal play is found either way "
          f"({len(off_flags)} off, {len(on_flags)} on)")
    if not (off_flags and on_flags):
        print(f"\n{_passed}/{_passed + _failed} checks passed.")
        return 1
    check([(g, p) for g, p, _ in off_flags] == [(g, p) for g, p, _ in on_flags],
          "and on the same ply")

    _, _, ply_off = off_flags[0]
    _, _, ply_on = on_flags[0]
    color = "white" if ply_off.get("color") == 1 else "black"
    other = "black" if color == "white" else "white"

    # --- 2. Counting is -------------------------------------------------------
    check(ply_off["analysis"].get("decision") is False,
          "by default the ply is not a counted decision")
    check(ply_on["analysis"].get("decision") is True,
          "with --count-illegal it is")

    agg_off = compute_aggregates(off)["match"]
    agg_on = compute_aggregates(on)["match"]

    check(agg_on[color]["total_decisions"] == agg_off[color]["total_decisions"] + 1,
          f"which puts exactly one more decision in the denominator "
          f"({color}: {agg_off[color]['total_decisions']} -> "
          f"{agg_on[color]['total_decisions']})")

    # The error added is the ply's own equity_loss -- stored either way, so
    # this is the exact relation and not an approximation. On this match it is
    # 0.0: the illegal play reached a better position than any legal move
    # could, so max(0, best - played) floors. That is worth knowing rather than
    # hiding -- the switch moves the denominator here and the numerator not at
    # all, which is precisely why PR falls.
    loss = ply_on["analysis"].get("equity_loss") or 0.0
    check(abs(agg_on[color]["total_error"]
              - (agg_off[color]["total_error"] + loss)) < 1e-6,
          f"and adds that ply's own equity_loss ({loss}) to the numerator")

    check(agg_on[color]["pr"] != agg_off[color]["pr"],
          f"so the player's PR moves ({agg_off[color]['pr']} -> {agg_on[color]['pr']})")

    # --- 3. The opponent is untouched ----------------------------------------
    check(agg_on[other] == agg_off[other],
          f"the other player's figures are identical ({other})")
    check(agg_on["illegal_moves"] == agg_off["illegal_moves"] == 1,
          "and the illegal-move count is the same either way")

    print(f"\n{_passed}/{_passed + _failed} checks passed.")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
