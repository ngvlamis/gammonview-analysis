# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Equity loss is not capped at one point of equity.

The field is a uint16 at 1e-4, so the representable ceiling is 6.5535 -- above
anything backgammon can produce, since equity runs [-3, +3] and no decision can
cost more than 6.0. Until 2026-08 two independent layers capped it at 1.0:

  1. binary._enc_equity_loss, the encoder.
  2. export._cube_sub_analysis / _checker_analysis, which build the OGXM-JSON
     the analysis worker writes -- upstream of the encoder, so fixing (1) alone
     left every analysed match still truncated.

Truncation did more than spoil a statistic: played_equity is derived on read
(best_equity - equity_loss) rather than stored, so a capped loss moved the
played equity too, leaving a record that disagreed with its own alternatives.

The floor at zero stays: a negative loss is nonsense and would encode as a huge
unsigned value.

Run: uv run python tests/test_equity_loss.py   (exit 0 = all passed)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gvformat import read_gvab, write_gvab
from gvformat.binary import MAX_EQUITY_LOSS, _enc_equity_loss
from gvformat.export import _checker_analysis, _cube_sub_analysis

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


def check_eq(actual, expected, msg: str) -> None:
    check(actual == expected, f"{msg}" if actual == expected
          else f"{msg}  (expected {expected!r}, got {actual!r})")


EV = {
    "win": 0.30, "gammon_win": 0.05, "bg_win": 0.002,
    "gammon_loss": 0.30, "bg_loss": 0.02, "equity": -0.968,
}
PROBS = [0.30, 0.05, 0.002, 0.30, 0.02]


def _doc_with_checker_loss(best_equity: float, played_equity: float) -> dict:
    """A one-ply document whose single checker decision lost the difference."""
    loss = round(best_equity - played_equity, 4)
    return {
        "match_length": 5, "player_white": "a", "player_black": "b",
        "white_score": 0, "black_score": 0, "crawford": True,
        "analysis_info": {"ply": 2, "eval_level": "2ply", "model_id": "x", "timestamp": 0},
        "games": [{
            "game_index": 0, "winner": 0, "points_won": 1,
            "plies": [
                {
                    "color": 0, "action_id": 9, "d1": 2, "d2": 5,
                    "moves": [{"from": 13, "pips": 5}, {"from": 13, "pips": 2}],
                    "ogid_before": "11ccccchhhjjjjj:66666888dddddoo:N0N:25:W:IB:0:0:5:0",
                    "analysis": {
                        "eval": EV,
                        "best_equity": best_equity,
                        "played_equity": played_equity,
                        "equity_loss": loss,
                        "decision": True,
                        "alternatives": [
                            {"move": [{"from": 13, "pips": 5}, {"from": 13, "pips": 2}],
                             "equity": best_equity, "is_played": False,
                             "eval": EV, "eval_level": "2ply"},
                            {"move": [{"from": 24, "pips": 5}, {"from": 24, "pips": 2}],
                             "equity": played_equity, "is_played": True,
                             "eval": EV, "eval_level": "2ply"},
                        ],
                    },
                },
                {"color": 1, "action_id": 24, "moves": []},
            ],
        }],
    }


def main() -> int:
    # --- the encoder's bounds ---------------------------------------------
    check_eq(MAX_EQUITY_LOSS, 6.5535, "the ceiling is the full uint16 range at 1e-4")
    check_eq(_enc_equity_loss(0.0), 0, "zero encodes to zero")
    check_eq(_enc_equity_loss(-0.5), 0, "a negative loss floors at zero")
    check_eq(_enc_equity_loss(0.0047), 47, "a small loss keeps 1e-4 resolution")
    check_eq(_enc_equity_loss(1.0), 10000, "one point of equity is no longer the ceiling")
    check_eq(_enc_equity_loss(1.8096), 18096, "a loss past 1.0 survives instead of truncating")
    check_eq(_enc_equity_loss(6.5535), 65535, "the ceiling encodes to the uint16 maximum")
    check_eq(_enc_equity_loss(9.9), 65535, "past the ceiling it saturates rather than wrapping")
    # Wrapping would be far worse than clamping: 6.6 * 10000 is 66000, which
    # would come back as 464 -- a catastrophic blunder read as a rounding error.
    check(_enc_equity_loss(6.6) > 60000,
          "an over-range loss stays large rather than wrapping to a small one")

    # --- round-trip through the binary ------------------------------------
    a = read_gvab(write_gvab(_doc_with_checker_loss(0.9, -0.6)))["games"][0]["plies"][0]["analysis"]
    check_eq(round(a["equity_loss"], 4), 1.5, "a 1.5 checker blunder round-trips intact")
    check_eq(round(a["played_equity"], 4), -0.6, "played_equity, being derived, is right again")
    played = next(x for x in a["alternatives"] if x["is_played"])
    check(abs(played["equity"] - a["played_equity"]) < 1e-4,
          "the record agrees with its own played alternative")

    # The ordinary case must be undisturbed -- it is nearly every decision.
    a = read_gvab(write_gvab(_doc_with_checker_loss(0.0145, 0.0098)))["games"][0]["plies"][0]["analysis"]
    check_eq(round(a["equity_loss"], 4), 0.0047, "an ordinary small loss is unchanged")
    check_eq(round(a["played_equity"], 4), 0.0098, "and its played_equity still reconstructs")

    # --- the export layer, upstream of the encoder ------------------------
    key, sub = _cube_sub_analysis({
        "lost_equity": 1.8096, "optimal_action": "double",
        "equity_no_double": 0.9967, "equity_double_take": 2.8096,
        "equity_double_pass": 1.0,
    })
    check_eq(key, "missed_double", "a cube that should have turned reads as missed_double")
    check_eq(sub["equity_loss"], 1.8096, "a missed double past 1.0 survives to_ogxm_json")

    _, sub = _cube_sub_analysis({
        "lost_equity": 2.5, "optimal_action": "no_double", "counted": True,
        "equity_no_double": 0.5, "equity_double_take": -0.2, "equity_double_pass": 1.0,
    })
    check_eq(sub["equity_loss"], 2.5, "a cube_decision loss past 1.0 survives too")

    entry = {
        "lost_equity": 1.5, "counted": True,
        "move_options": [
            {"move": "13/8 13/11", "equity": 0.9, "probs": PROBS},
            {"move": "24/19 24/22", "equity": -0.6, "probs": PROBS, "played": True},
        ],
    }
    a = _checker_analysis(entry, True, 2, 5, 2)
    check_eq(a["equity_loss"], 1.5, "the engine's own lost_equity is not capped at 1.0")
    check_eq(a["played_equity"], -0.6, "played_equity is untouched by the loss")

    entry = {
        "move_options": [
            {"move": "13/8 13/11", "equity": 0.9, "probs": PROBS},
            {"move": "24/19 24/22", "equity": -0.6, "probs": PROBS, "played": True},
        ],
    }
    a = _checker_analysis(entry, True, 2, 5, 2)
    check_eq(a["equity_loss"], 1.5, "a derived loss past 1.0 is not capped either")
    check_eq(a["decision"], False, "an unanalysed ply still counts toward nothing")

    entry = {
        "lost_equity": -0.25, "counted": True,
        "move_options": [{"move": "13/8 13/11", "equity": 0.5, "probs": PROBS, "played": True}],
    }
    check_eq(_checker_analysis(entry, True, 2, 5, 2)["equity_loss"], 0.0,
             "a negative loss still floors at zero")

    print()
    print(f"{_passed}/{_passed + _failed} checks passed.")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
