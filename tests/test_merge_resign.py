# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Appending an analysis when only one document records a resignation.

One converter ends a resigned game on resign plies (27, 28), another only on
its result. Checker plays and cubes still pair 1:1; the resignation is left
unpaired rather than failing the merge. 3.0.0 counted resign plies as decisions
on both sides and raised "analysis/ply count mismatch" here, on a match
imported from both an .xg and a .bgf. The JS twin is section 12 of
gvformat-js/test/test-merge.js.

Run: uv run python tests/test_merge_resign.py   (exit 0 = all passed)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gvformat import append_analysis  # noqa: E402

failures = 0


def check(ok: bool, what: str) -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def analysis(equity: float) -> dict:
    return {"eval": {"win": 0.5, "gammon_win": 0.1, "bg_win": 0.0, "gammon_loss": 0.1,
                     "bg_loss": 0.0, "equity": equity},
            "best_equity": equity, "played_equity": equity, "equity_loss": 0.0,
            "decision": True, "alternatives": []}


def match(model: str, *, resigned: bool, plays: int = 2) -> dict:
    game1 = [{"color": 1, "action_id": 6, "d1": 2, "d2": 3, "analysis": analysis(0.31)}]
    if resigned:
        game1 += [{"color": 1, "action_id": 27, "analysis": {"resign_error": 0.4, "decision": True}},
                  {"color": 0, "action_id": 28}]
    else:
        game1 += [{"color": 1, "action_id": 24}]
    game0 = [{"color": 0, "action_id": 1, "d1": 1, "d2": 2, "analysis": analysis(0.53)},
             {"color": 1, "action_id": 1, "d1": 3, "d2": 4, "analysis": analysis(0.2)}][:plays]
    return {"match_length": 7, "analysis_info": {"ply": 3, "model_id": model},
            "games": [{"game_index": 0, "plies": game0}, {"game_index": 1, "plies": game1}]}


def main() -> int:
    merged = append_analysis(match("a", resigned=True), match("b", resigned=False))
    check(len(merged["analyses_info"]) == 2, "resign on one side only: the merge goes through")
    check(any(a.get("analysis_index") == 1 for a in merged["games"][0]["plies"][0]["analyses"]),
          "resign on one side only: checker plays still pair")
    check(not any(a.get("analysis_index") == 1
                  for a in merged["games"][1]["plies"][1].get("analyses") or []),
          "resign on one side only: the resignation takes nothing from the other block")

    both = append_analysis(match("a", resigned=True), match("b", resigned=True))
    check(any(a.get("analysis_index") == 1 and a.get("resign_error") == 0.4
              for a in both["games"][1]["plies"][1]["analyses"]),
          "resign on both sides: the resignations pair")

    try:
        append_analysis(match("a", resigned=False), match("b", resigned=False, plays=1))
        check(False, "a different number of plays is still an error")
    except ValueError as e:
        check("count mismatch" in str(e), "a different number of plays is still an error")

    print(f"\n{'all passed' if not failures else str(failures) + ' failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
