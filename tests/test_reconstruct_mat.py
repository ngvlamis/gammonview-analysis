# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Round-trip validation harness for reconstruct_mat.reconstruct_mat.

Runs gvan_match.analyze_mat on a real match file, converts the result to
OGXM-JSON with ogxm_export.to_ogxm_json, reconstructs a .mat file from that
JSON with reconstruct_mat.reconstruct_mat, and checks that re-parsing the
reconstructed text (with mat_parser) yields the same game count and the same
per-game dice/cube-action sequence as parsing the ORIGINAL .mat file.

Exact move *notation* text and the "Wins ..." result line are intentionally
NOT asserted byte-for-byte: the played move's notation comes from bgsage's
own move-notation renderer (which groups repeated identical spans as "(2)"
inconsistently with the site that produced the match, and represents a
forced dance as "" rather than "Cannot Move"), and the result line's
gammon/backgammon/"and the match" annotations are appended by pre-existing
reconstruct_mat logic that the match's source site apparently omits. These
are documented, expected differences -- see the module docstring in
reconstruct_mat.py and the test's printed report below.

Run directly:
    uv run python tests/test_reconstruct_mat.py
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvanalysis import analyze_mat
from gvformat.export import to_ogxm_json
from gvanalysis.reconstruct_mat import reconstruct_mat
from gvformat import mat_parser

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


# ---------------------------------------------------------------------------
# Structural signature: for each game, the sequence of (player, action-kind,
# key-detail) tuples -- dice pair for a move, cube value for a double, bare
# kind for take/drop. Move notation text and win-line text are excluded
# deliberately (see module docstring).
# ---------------------------------------------------------------------------

def _game_signature(player_actions: list[tuple[str, str]]) -> list[tuple[str, str, object]]:
    sig = []
    for player, text in player_actions:
        atype, data = mat_parser._parse_action(text)
        if atype == "move":
            d1, d2, _move_text = data
            sig.append((player, "move", (d1, d2)))
        elif atype == "double":
            sig.append((player, "double", data))
        elif atype in ("take", "drop"):
            sig.append((player, atype, None))
        # "win"/"resign"/"forfeit"/"unknown" excluded from the strict
        # per-turn signature; wins are compared separately below.
    return sig


def _last_win(player_actions: list[tuple[str, str]]) -> tuple[str, dict] | None:
    for player, text in reversed(player_actions):
        atype, data = mat_parser._parse_action(text)
        if atype == "win":
            return player, data
    return None


def main() -> int:
    if MAT_PATH is None:
        print(missing_mat_message())
        return 0

    original_text = MAT_PATH.read_text(encoding="utf-8")

    result = analyze_mat(str(MAT_PATH), preset="very_quick", quiet=True)
    ogxm = to_ogxm_json(result)
    recon_text = reconstruct_mat(ogxm)

    orig_parsed = mat_parser.parse_mat_file(original_text)
    recon_parsed = mat_parser.parse_mat_file(recon_text)

    # --- Game count -----------------------------------------------------
    check(
        len(orig_parsed["games"]) == len(recon_parsed["games"]),
        f"same game count (orig={len(orig_parsed['games'])}, "
        f"recon={len(recon_parsed['games'])})",
    )

    # --- Player identity --------------------------------------------------
    check(
        {orig_parsed["player1"], orig_parsed["player2"]}
        == {recon_parsed["player1"], recon_parsed["player2"]},
        f"same player names ({orig_parsed['player1']!r}/{orig_parsed['player2']!r})",
    )

    # --- Per-game structural (dice + cube-action) sequence ----------------
    n_games = min(len(orig_parsed["games"]), len(recon_parsed["games"]))
    win_mismatches = 0
    for i in range(n_games):
        og = orig_parsed["games"][i]
        rg = recon_parsed["games"][i]
        og_sig = _game_signature(og["player_actions"])
        rg_sig = _game_signature(rg["player_actions"])
        check(
            og_sig == rg_sig,
            f"game {i + 1}: dice/cube-action sequence matches "
            f"({len(og_sig)} actions)",
        )

        og_win = _last_win(og["player_actions"])
        rg_win = _last_win(rg["player_actions"])
        if og_win is None or rg_win is None:
            win_mismatches += 1
        else:
            og_player, og_data = og_win
            rg_player, rg_data = rg_win
            if og_player != rg_player or og_data.get("points") != rg_data.get("points"):
                win_mismatches += 1

    check(
        win_mismatches == 0,
        f"win-line winner/points match for all {n_games} games "
        f"({win_mismatches} mismatches)",
    )

    # --- Score progression (derived score_start vs. original) -------------
    score_mismatches = [
        i
        for i in range(n_games)
        if (orig_parsed["games"][i]["score1_start"], orig_parsed["games"][i]["score2_start"])
        != (recon_parsed["games"][i]["score1_start"], recon_parsed["games"][i]["score2_start"])
    ]
    check(
        not score_mismatches,
        f"derived score_start matches original for all games "
        f"(mismatches at games: {score_mismatches})",
    )

    # --- Textual closeness (informational only, not asserted) -------------
    orig_norm = [" ".join(line.split()) for line in original_text.splitlines()]
    recon_norm = [" ".join(line.split()) for line in recon_text.splitlines()]
    ratio = difflib.SequenceMatcher(a=orig_norm, b=recon_norm).ratio()
    diff_lines = list(
        difflib.unified_diff(orig_norm, recon_norm, lineterm="", n=0)
    )
    n_diff_hunks = sum(1 for l in diff_lines if l.startswith("@@"))
    print()
    print(f"Textual closeness (whitespace-normalized SequenceMatcher ratio): {ratio:.4f}")
    print(f"Unified-diff hunks: {n_diff_hunks}")
    print("Sample of differing lines (first 20):")
    sample = [l for l in diff_lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    for l in sample[:20]:
        print(f"  {l}")

    print()
    print(f"{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("Failures:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
