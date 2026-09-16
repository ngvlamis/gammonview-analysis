# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Convert a Jellyfish/GNUbg ``.mat`` match into an OGXM-JSON dict — **no
analysis**.

The ``.mat`` sibling of ``convert_xg`` / ``convert_bgf``: it parses the ``.mat``
and reconstructs every game's board/cube/score sequence (no *evaluation*
happens — only board tracking), then emits a canonical OGXM document with no
``analysis``/``analysis_info``. That makes it the engine-free front half of the
analysis pipeline: the analyzer runs *later* over this OGXM (see
``gvanalysis.ogxm_reconstructor`` + ``gvanalysis.match.analyze_ogxm``), so
``.mat`` is just one more way to obtain the OGXM every downstream stage
consumes — which is why the converter belongs in the codec layer, not the
engine layer.

The unanalyzed ``data`` dict handed to ``to_ogxm_json`` mirrors the shape
``gvanalysis.game_eval`` produces for the analyzed path, minus every eval field, so
the resulting GAME chunks (boards, OGIDs, moves, winner, points) are identical
to what the analyzed pipeline would emit for the same match — only the analysis
is absent.
"""

from __future__ import annotations

from pathlib import Path

from .export import to_ogxm_json, _flip_board as flip_board
from .mat_parser import parse_mat_file
from .game_reconstructor import find_crawford_game_index, reconstruct_decisions


def _board_p1(board: list[int], mover_is_p1: bool) -> list[int]:
    """Board (stored mover-perspective) in the fixed Player-1/White frame the
    OGXM export uses. Mirrors ``game_eval._board_p1`` so the no-analysis path
    and the analyzed path produce byte-identical GAME data."""
    return list(board) if mover_is_p1 else flip_board(board)


def _decisions_to_moves(decisions: list[dict]) -> list[dict]:
    """Turn reconstructed decision dicts into unanalyzed OGXM ``moves`` entries.

    Mirrors the entry shapes ``game_eval`` builds (``checker`` /
    ``cube_decision`` / ``cube_response``) but with no eval payload. Implicit
    "no-double" cube decisions (``doubled == False``) produce no ply — they are
    analysis-only in the analyzed path (embedded on the next checker ply) and
    are re-derived by ``ogxm_reconstructor`` when the OGXM is later analyzed.
    """
    moves: list[dict] = []
    for dec in decisions:
        if dec["kind"] == "checker":
            is_p1 = dec["is_p1"]
            moves.append({
                "player": dec["player"],
                "kind": "checker",
                "dice": list(dec["dice"]),
                "cube_value": dec["cube_value"],
                "cube_owner": dec["cube_owner"],
                "board_before": _board_p1(dec["board"], is_p1),
                "board_after": _board_p1(dec["board_played"], is_p1),
                # Carried for the illegal-play ladder only. The steps are
                # normally derived from the two boards, but a board diff cannot
                # be collapsed safely when a play overflows its ply record --
                # see export.fit_move_steps.
                "notation": dec.get("notation"),
            })
        elif dec["kind"] == "cube":
            if not dec["doubled"]:
                continue  # implicit no-double: no ply in OGXM
            board_p1 = _board_p1(dec["board"], dec["is_doubler_p1"])
            moves.append({
                "player": dec["doubler"],
                "kind": "cube_decision",
                "board": board_p1,
            })
            if dec["response"] is not None:
                moves.append({
                    "player": dec["responder"],
                    "kind": "cube_response",
                    "cube_value": dec["cube_value"] * 2,
                    "player_response": dec["response"],
                    "board": board_p1,
                })
    return moves


def mat_to_data(match_data: dict) -> dict:
    """Parsed-``.mat`` dict -> the unanalyzed ``data`` dict ``to_ogxm_json``
    consumes (``summary`` + ``games`` with per-game ``moves``, no eval fields).
    """
    p1 = match_data["player1"]
    p2 = match_data["player2"]
    ml = match_data["match_length"]
    crawford_idx = find_crawford_game_index(
        match_data["games"], ml, match_data["crawford_rule"]
    )

    games_out = []
    for i, game in enumerate(match_data["games"]):
        is_crawford = (i == crawford_idx)
        recon = reconstruct_decisions(game, ml, p1, p2, is_crawford=is_crawford)
        games_out.append({
            "game_number": game["game_number"],
            "score_start": {
                "player1": game["score1_start"],
                "player2": game["score2_start"],
            },
            "is_crawford": is_crawford,
            "result": recon["game_result"],
            "moves": _decisions_to_moves(recon["decisions"]),
        })

    summary = {
        "player1": p1,
        "player2": p2,
        "match_length": ml if ml > 0 else None,
        "crawford_rule": match_data["crawford_rule"],
        "jacoby_rule": match_data["jacoby_rule"],
        "beaver_rule": match_data["beaver_rule"],
        "cube_limit": match_data["cube_limit"],
        "event": match_data["event"] or None,
        "site": match_data["site"] or None,
        "date": match_data["date"] or None,
        "event_time": match_data["event_time"] or None,
        # No eval_level/preset key -> to_ogxm_json emits no analysis_info.
    }
    return {"summary": summary, "games": games_out}


def mat_to_ogxm(text: str) -> dict:
    """Jellyfish/GNUbg ``.mat`` text -> canonical OGXM-JSON dict, no analysis."""
    return to_ogxm_json(mat_to_data(parse_mat_file(text)))


def convert_mat(mat_path: Path) -> dict:
    """Convert a Jellyfish/GNUbg ``.mat`` file to OGXM JSON.

    Returns a dict conforming to ``OGXM_JSON_SPEC_GAMMONVIEW.md``. Sibling of
    ``convert_xg``/``convert_bgf``: same path-in, OGXM-out shape. Unlike those
    two the source is text, so ``mat_to_ogxm`` is the reusable core for callers
    that already hold the text (the JS mirror, ``convertMat``, is text-only
    since it runs browser-side).
    """
    return mat_to_ogxm(Path(mat_path).read_text(encoding="utf-8", errors="replace"))
