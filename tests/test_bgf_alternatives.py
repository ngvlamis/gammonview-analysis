# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Unit tests for gvformat.bgf._build_alternatives eval population.

Regression guard: _build_alternatives read a non-existent ``opt["eq"]`` field,
so every checker-play alternative was emitted without its
win/gammon/backgammon eval. The move options it receives carry a pre-computed
``probs`` vector instead. Exercises the builder directly with constructed
move-option dicts, so no engine or fixture file is needed.

Run directly:
    uv run python tests/test_bgf_alternatives.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat.bgf import _build_alternatives, _checker_analysis, _SENTINEL

# Any board works: a dance plays no checkers, so nothing is read off it.
_STARTING = [0] * 26

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


def main() -> int:
    # Options as _checker_analysis builds them: probs = [win, gWin, bgWin,
    # gLoss, bgLoss], no "eq" key. move="" parses to no steps.
    move_options = [
        {"equity": 0.5, "played": True, "probs": [0.60, 0.20, 0.05, 0.15, 0.03],
         "ply": 3, "move": "", "notation": "13/10 13/9"},
        {"equity": 0.4, "played": False, "probs": [0.55, 0.18, 0.04, 0.20, 0.05],
         "ply": 3, "move": "", "notation": "24/21 13/9"},
    ]

    alts = _build_alternatives(move_options, True, 3, 4)

    check(len(alts) == 2, "one alternative per move option")
    check(all("eval" in a for a in alts), "every alternative carries an eval")

    e = alts[0].get("eval") or {}
    check(abs(e.get("win", -1) - 0.60) < 1e-9, "eval.win from probs[0]")
    check(abs(e.get("gammon_win", -1) - 0.20) < 1e-9, "eval.gammon_win from probs[1]")
    check(abs(e.get("bg_win", -1) - 0.05) < 1e-9, "eval.bg_win from probs[2]")
    check(abs(e.get("gammon_loss", -1) - 0.15) < 1e-9, "eval.gammon_loss from probs[3]")
    check(abs(e.get("bg_loss", -1) - 0.03) < 1e-9, "eval.bg_loss from probs[4]")

    # A move option with no probs must not fabricate an eval.
    no_probs = _build_alternatives(
        [{"equity": 0.1, "played": True, "probs": [], "move": "", "notation": ""}],
        True, 1, 1)
    check("eval" not in no_probs[0], "no eval when probs are absent")

    # A dance: no candidate list, but BGBlitz evaluated the position the
    # non-play leaves behind and put it in `dancingEquity`, separately from the
    # pre-roll `equity` holding the cube decision. Both are on the record --
    # you can double when you cannot move -- and the checker analysis must take
    # the dancing one, or the ply's probabilities are the cube's.
    money = {"hasEMG": False, "matchEquity": _SENTINEL, "emg": _SENTINEL}
    pre_roll = {**money, "myWins": "0.60", "myGammon": "0.18", "myBackGammon": "0.003",
                "oppWins": "0.40", "oppGammon": "0.04", "oppBackGammon": "0.0007",
                "cubeDecision": {"eqCubeFul": "0.49"}}
    danced = {**money, "myWins": "0.41", "myGammon": "0.05", "myBackGammon": "0.0006",
              "oppWins": "0.59", "oppGammon": "0.05", "oppBackGammon": "0.001",
              "cubeDecision": {"eqCubeFul": "-0.26"}}

    a = _checker_analysis(pre_roll, [], {}, -1, 4, 6, list(_STARTING), True,
                          dancing_eq=danced, ply_raw=3)
    check(a is not None, "a dance with dancingEquity yields an analysis")
    check(len(a["alternatives"]) == 1, "exactly one 'no move' option")
    check(a["alternatives"][0]["move"] == [] and a["alternatives"][0]["notation"] == "",
          "the option moves no checkers")
    check(a["alternatives"][0]["is_played"] is True, "and is the played one")
    check(abs(a["eval"]["win"] - 0.41) < 1e-9,
          "eval.win comes from dancingEquity, not the pre-roll equity")
    check(abs(a["best_equity"] - (-0.26)) < 1e-9 and a["equity_loss"] == 0.0,
          "no play to get wrong, so no equity is lost")
    check(a["decision"] is False, "and it is not a checker decision")
    check(a["alternatives"][0].get("eval_level") == "3ply", "eval_level from the record's ply")

    check(_checker_analysis(pre_roll, [], {}, -1, 4, 6, list(_STARTING), True) is None,
          "no candidates and no dancingEquity is still nothing to report")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
