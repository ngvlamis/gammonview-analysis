# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Validation harness for ogxm_stats.compute_aggregates.

Runs gvan_match.analyze_mat on a real match from the committed corpus
(tests/fixtures.py names which one; very_quick preset), converts the result with ogxm_export.to_ogxm_json, runs
ogxm_stats.compute_aggregates on the resulting OGXM-JSON dict, and checks
the computed aggregates against the analyzer's own authoritative in-memory
numbers (analyze_mat's "summary" block, plus its per-game entries).

Run directly:
    uv run python tests/test_ogxm_stats.py

Player-color mapping used throughout: white = player1 (color==1 in OGXM),
black = player2 (color==0).

Note on total_luck: luck is stored as a single ``luck`` field on the checker
ply's analysis (1-ply-vs-1-ply and preset-independent, at the luck-analyzer's
level), so ``total_luck`` reconstructed purely from OGXM JSON is EXACTLY
reproducible against the analyzer's authoritative in-memory total -- see
ogxm_stats.py's module docstring ("Luck / luck_mwc" section). This is
asserted to 4 decimal places below (the same rounding ``gvan_match.py``
applies to the stored summary/game luck totals), both at match level and per
game.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvanalysis import analyze_mat
from gvformat.export import to_ogxm_json
from gvformat.stats import compute_aggregates

from fixtures import sample_mat, missing_mat_message  # noqa: E402

MAT_PATH = sample_mat()

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


def approx(a: float | None, b: float | None, tol: float) -> bool:
    if a is None or b is None:
        return a == b
    return abs(a - b) <= tol


def main() -> int:
    if MAT_PATH is None:
        print(missing_mat_message())
        return 0

    result = analyze_mat(str(MAT_PATH), preset="very_quick", quiet=True)
    ogxm = to_ogxm_json(result)
    agg = compute_aggregates(ogxm)

    summary = result["summary"]
    match_agg = agg["match"]
    white = match_agg["white"]   # player1
    black = match_agg["black"]   # player2

    print("=" * 78)
    print("MATCH-LEVEL (white=player1, black=player2)")
    print("=" * 78)

    # --- PR --------------------------------------------------------------
    check(approx(white["pr"], summary["player1_pr"], 0.01),
          f"white PR {white['pr']} ~= summary player1_pr {summary['player1_pr']}")
    check(approx(black["pr"], summary["player2_pr"], 0.01),
          f"black PR {black['pr']} ~= summary player2_pr {summary['player2_pr']}")

    # --- total_error -------------------------------------------------------
    check(approx(white["total_error"], summary["player1_total_error"], 1e-3),
          f"white total_error {white['total_error']} ~= "
          f"summary player1_total_error {summary['player1_total_error']}")
    check(approx(black["total_error"], summary["player2_total_error"], 1e-3),
          f"black total_error {black['total_error']} ~= "
          f"summary player2_total_error {summary['player2_total_error']}")

    # --- total_decisions ---------------------------------------------------
    check(white["total_decisions"] == summary["player1_total_decisions"],
          f"white total_decisions {white['total_decisions']} == "
          f"summary player1_total_decisions {summary['player1_total_decisions']}")
    check(black["total_decisions"] == summary["player2_total_decisions"],
          f"black total_decisions {black['total_decisions']} == "
          f"summary player2_total_decisions {summary['player2_total_decisions']}")

    # --- cube_decisions ------------------------------------------------------
    check(white["cube_decisions"] == summary["player1_cube_decisions"],
          f"white cube_decisions {white['cube_decisions']} == "
          f"summary player1_cube_decisions {summary['player1_cube_decisions']}")
    check(black["cube_decisions"] == summary["player2_cube_decisions"],
          f"black cube_decisions {black['cube_decisions']} == "
          f"summary player2_cube_decisions {summary['player2_cube_decisions']}")

    # --- luck_rolls (exact -- a count, unaffected by the postroll-depth gap) ---
    check(white["luck_rolls"] == summary["player1_luck_rolls"],
          f"white luck_rolls {white['luck_rolls']} == "
          f"summary player1_luck_rolls {summary['player1_luck_rolls']}")
    check(black["luck_rolls"] == summary["player2_luck_rolls"],
          f"black luck_rolls {black['luck_rolls']} == "
          f"summary player2_luck_rolls {summary['player2_luck_rolls']}")

    # --- total_luck (now exactly reproducible -- 1-ply-vs-1-ply, preset-
    # independent luck) ---------------------------------------------------
    check(approx(white["total_luck"], summary["player1_total_luck"], 1e-4),
          f"white total_luck {white['total_luck']} == "
          f"summary player1_total_luck {summary['player1_total_luck']}")
    check(approx(black["total_luck"], summary["player2_total_luck"], 1e-4),
          f"black total_luck {black['total_luck']} == "
          f"summary player2_total_luck {summary['player2_total_luck']}")

    # --- total_luck_mwc (compute-on-read via gvformat.met, derived from each
    # ply's own ogid_before -- no stored per-decision anchor any more) -------
    if "player1_total_luck_mwc" in summary:
        check("total_luck_mwc" in white,
              "white total_luck_mwc present (match play, met-derived)")
        check(approx(white.get("total_luck_mwc"), summary["player1_total_luck_mwc"], 5e-3),
              f"white total_luck_mwc {white.get('total_luck_mwc')} ~= "
              f"summary player1_total_luck_mwc {summary['player1_total_luck_mwc']}")
    if "player2_total_luck_mwc" in summary:
        check("total_luck_mwc" in black,
              "black total_luck_mwc present (match play, met-derived)")
        check(approx(black.get("total_luck_mwc"), summary["player2_total_luck_mwc"], 5e-3),
              f"black total_luck_mwc {black.get('total_luck_mwc')} ~= "
              f"summary player2_total_luck_mwc {summary['player2_total_luck_mwc']}")

    # --- illegal_moves -------------------------------------------------------
    check(match_agg["illegal_moves"] == summary["illegal_moves"],
          f"illegal_moves {match_agg['illegal_moves']} == summary illegal_moves "
          f"{summary['illegal_moves']}")
    if summary["illegal_moves"] == 0:
        print("INFO  this match has 0 illegal moves -- illegal_moves counting is "
              "unexercised by this fixture.")

    # -----------------------------------------------------------------------
    print()
    print("=" * 78)
    print("PER-GAME (pr / decisions / luck / luck_rolls -- the fields analyze_mat's")
    print("public 'games' list actually exposes per game)")
    print("=" * 78)

    games_by_index = {g["game_number"] - 1: g for g in result["games"]}
    check(len(agg["games"]) == len(games_by_index),
          f"game count {len(agg['games'])} == {len(games_by_index)}")

    for g in agg["games"]:
        gi = g["game_index"]
        ref = games_by_index.get(gi)
        if ref is None:
            check(False, f"game {gi}: no matching reference game")
            continue

        check(approx(g["white"]["pr"], ref["player1_pr"], 0.01),
              f"game {gi} white PR {g['white']['pr']} ~= {ref['player1_pr']}")
        check(approx(g["black"]["pr"], ref["player2_pr"], 0.01),
              f"game {gi} black PR {g['black']['pr']} ~= {ref['player2_pr']}")

        check(g["white"]["total_decisions"] == ref["player1_decisions"],
              f"game {gi} white decisions {g['white']['total_decisions']} == "
              f"{ref['player1_decisions']}")
        check(g["black"]["total_decisions"] == ref["player2_decisions"],
              f"game {gi} black decisions {g['black']['total_decisions']} == "
              f"{ref['player2_decisions']}")

        check(g["white"]["luck_rolls"] == ref["player1_luck_rolls"],
              f"game {gi} white luck_rolls {g['white']['luck_rolls']} == "
              f"{ref['player1_luck_rolls']}")
        check(g["black"]["luck_rolls"] == ref["player2_luck_rolls"],
              f"game {gi} black luck_rolls {g['black']['luck_rolls']} == "
              f"{ref['player2_luck_rolls']}")

        check(approx(g["white"]["total_luck"], ref["player1_luck"], 1e-4),
              f"game {gi} white total_luck {g['white']['total_luck']} == "
              f"{ref['player1_luck']}")
        check(approx(g["black"]["total_luck"], ref["player2_luck"], 1e-4),
              f"game {gi} black total_luck {g['black']['total_luck']} == "
              f"{ref['player2_luck']}")

    print()
    print("=" * 78)
    print(f"{_checks} checks, {len(_failures)} failures")
    print("=" * 78)
    if _failures:
        print("\nFailures:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
