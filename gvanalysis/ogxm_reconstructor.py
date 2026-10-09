# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Reconstruct engine decisions from an OGXM document.

The reverse of ``game_reconstructor.reconstruct_decisions`` (which reads a
``.mat``): this walks the OGXM plies of an already-parsed match and emits the
same per-decision dicts the bgsage evaluators consume
(``game_eval._eval_checker_decision`` / ``_eval_cube_decision``). Because every
supported input (``.mat`` via ``mat_to_ogxm``, ``.gva``/``.ogxm`` JSON,
``.gvab``) becomes an OGXM document first, this is the single decision source
for the analyzer.

Board/cube/score tracking mirrors ``gvformat.reader._derive_ogids`` (the same
P1/White running board via ``_apply_moves_p1``, the same per-game start scores
via ``_game_start_scores``), so a decision reconstructed here sits on exactly
the position the OGID replay would show for that ply.

Implicit "no-double" cube decisions are **not** OGXM plies (they are
analysis-only, embedded on the following checker ply). They are re-derived here
from the cube/score state with the same predicate ``reconstruct_decisions``
uses, so the analyzed output matches the ``.mat`` path decision-for-decision.

Decisions are returned in OGXM ply order; each checker/double/take/drop ply
yields exactly one decision entry (a checker or cube decision), so the caller
can align the resulting per-ply analysis back onto the base plies positionally.
"""

from __future__ import annotations

from bgsage.board import flip_board

from gvformat.reader import _absolute_to_p1, _apply_moves_p1, _game_start_scores
from gvformat.export import variant_opening_p1


class UnsupportedMatch(ValueError):
    """The match is valid OGXM, but the engine cannot analyze it."""


def _mover_frame(board_p1: list[int], is_white: bool) -> list[int]:
    """P1/White-frame board -> current mover's perspective (own checkers +,
    own bar 25). White keeps the frame; Black flips it."""
    return list(board_p1) if is_white else flip_board(board_p1)


def _cube_owner_rel(owner_abs: str | None, mover_is_white: bool) -> str:
    """Absolute cube owner ('W'/'B'/None) -> the mover's perspective label
    ('centered'/'player'/'opponent') the evaluators expect."""
    if owner_abs is None:
        return "centered"
    mover = "W" if mover_is_white else "B"
    return "player" if owner_abs == mover else "opponent"


def _away(sw: int, sb: int, match_length: int, mover_is_white: bool) -> tuple[int, int]:
    """(away_mover, away_opp) from per-game start scores; (0, 0) for money."""
    if match_length <= 0:
        return (0, 0)
    mover_score = sw if mover_is_white else sb
    opp_score = sb if mover_is_white else sw
    return (max(1, match_length - mover_score), max(1, match_length - opp_score))


def reconstruct_game_decisions(
    game: dict, sw: int, sb: int, match_length: int,
    player_white: str, player_black: str,
) -> list[dict]:
    """Decisions for one OGXM game (start scores ``sw``/``sb`` in points)."""
    crawford = bool(game.get("is_crawford", False))

    def name(is_white: bool) -> str:
        return player_white if is_white else player_black

    board_p1 = list(variant_opening_p1(0))
    # The cube a game opens with (OGXM v2, 5.3): its initial value doubled once
    # per automatic double, held by the owner after them -- not always a
    # centred 1.
    initial = int(game.get("initial_cube_value") or 1)
    if initial < 1 or initial & (initial - 1):
        initial = 1                     # not a cube value; v2 cannot hold one either
    cube_value = initial * 2 ** max(0, int(game.get("auto_doubles") or 0))
    cube_owner_abs: str | None = {0: "W", 1: "B"}.get(game.get("initial_cube_owner"))
    any_move_made = False

    # Pending-double state (captured at the 21 ply, emitted at the take/drop).
    dbl_board_p1: list[int] = []
    dbl_value = 0
    dbl_owner_abs: str | None = None
    dbl_is_white = False

    decisions: list[dict] = []

    for ply in game.get("plies") or []:
        aid = ply.get("action_id")
        is_white = bool(ply.get("color"))

        # A set-position ply carrying dice is a *restated play*: an illegal play
        # no dice ply could encode, written as the board it produced (see
        # ``gvformat.export.set_position_ply``). It is a turn like any other --
        # the player was on roll, faced the cube and played something -- so it
        # reconstructs as a checker decision, with the stated board standing in
        # for the play. ``game_eval`` then finds no legal move that reaches that
        # board and takes its illegal-play path, which is exactly right: that is
        # what the ply records. The dice are what tell it from the set-position
        # ply that opens an exported position, which states where a game starts
        # and is no turn at all.
        is_restated_play = aid == 31 and ply.get("d1") is not None

        if (aid is not None and 0 <= aid <= 20) or is_restated_play:  # checker move
            away_m, away_o = _away(sw, sb, match_length, is_white)
            owner_rel = _cube_owner_rel(cube_owner_abs, is_white)
            board_mover = _mover_frame(board_p1, is_white)

            # Implicit "no-double": mover had cube access and rolled instead of
            # doubling. Not before the opening roll, not in Crawford, not when
            # the cube is already dead (mover can clinch at the current stake).
            dead_cube = match_length > 0 and cube_value >= away_m
            if (any_move_made and owner_rel in ("centered", "player")
                    and not crawford and not dead_cube):
                decisions.append({
                    "kind": "cube",
                    "board": board_mover,
                    "cube_value": cube_value,
                    "cube_owner": owner_rel,
                    "doubled": False,
                    "response": None,
                    "doubler": name(is_white),
                    "responder": None,
                    "is_doubler_p1": is_white,
                    "away1": away_m,
                    "away2": away_o,
                    "is_crawford": crawford,
                })

            # A restated play has no steps -- nothing could express them -- so it
            # states its board instead, and that board is the play.
            moves = ply.get("moves") or []
            if is_restated_play:
                new_board_p1 = _absolute_to_p1(
                    list(ply.get("set_position") or [0] * 26))
            else:
                new_board_p1 = _apply_moves_p1(board_p1, moves, is_white)
            board_played = _mover_frame(new_board_p1, is_white)
            dec: dict = {
                "kind": "checker",
                "board": board_mover,
                "dice": [ply.get("d1"), ply.get("d2")],
                "board_played": board_played,
                "cube_value": cube_value,
                "cube_owner": owner_rel,
                "player": name(is_white),
                "is_p1": is_white,
                "away1": away_m,
                "away2": away_o,
                "is_crawford": crawford,
                # A restated play is not a dance: there were legal moves, the
                # player just made none of them. ``no_legal`` would route it to
                # the forced-move branch and score it as one.
                "no_legal": not moves and not is_restated_play,
            }
            # The ply's own steps, where it has them. They are the authoritative
            # record of the play -- they came *out* of a ply record, so they fit
            # one by construction -- and handing them to the exporter is what
            # stops it re-deriving the play from a board diff that cannot always
            # be split back into the hops that made it (a 4-4 bear-off matching
            # as one 11-pip span, say). Without this the exporter restates a
            # perfectly legal play as a set position, and the result no longer
            # lines up with the document it was analyzed from.
            if moves:
                dec["move_steps"] = [dict(m) for m in moves]
            decisions.append(dec)
            board_p1 = new_board_p1
            any_move_made = True

        elif aid == 21:                                   # double offered
            dbl_board_p1 = list(board_p1)
            dbl_value = cube_value
            dbl_owner_abs = cube_owner_abs
            dbl_is_white = is_white
            cube_value *= 2

        elif aid in (22, 23):                             # take (22) / drop (23)
            resp_is_white = is_white                      # responder is on this ply
            away_m, away_o = _away(sw, sb, match_length, dbl_is_white)
            decisions.append({
                "kind": "cube",
                "board": _mover_frame(dbl_board_p1, dbl_is_white),
                "cube_value": dbl_value,
                "cube_owner": _cube_owner_rel(dbl_owner_abs, dbl_is_white),
                "doubled": True,
                "response": "take" if aid == 22 else "pass",
                "doubler": name(dbl_is_white),
                "responder": name(resp_is_white),
                "is_doubler_p1": dbl_is_white,
                "away1": away_m,
                "away2": away_o,
                "is_crawford": crawford,
            })
            if aid == 22:                                 # taker now owns the cube
                cube_owner_abs = "W" if resp_is_white else "B"

        elif aid == 32:                                   # beaver: the responder redoubles
            # A beaver answers the double (spec M6): the cube is doubled again,
            # to 4x its value before the double, and the beaverer owns it. The
            # engine has no beaver verdict -- it judges take or pass -- so the
            # responder's decision is recorded as the take it implies, and what
            # comes after is analyzed at the beavered cube.
            resp_is_white = is_white
            away_m, away_o = _away(sw, sb, match_length, dbl_is_white)
            decisions.append({
                "kind": "cube",
                "board": _mover_frame(dbl_board_p1, dbl_is_white),
                "cube_value": dbl_value,
                "cube_owner": _cube_owner_rel(dbl_owner_abs, dbl_is_white),
                "doubled": True,
                "response": "take",
                "played": "beaver",       # what the ply says; the judgement stays a take
                "doubler": name(dbl_is_white),
                "responder": name(resp_is_white),
                "is_doubler_p1": dbl_is_white,
                "away1": away_m,
                "away2": away_o,
                "is_crawford": crawford,
            })
            cube_value = dbl_value * 4
            cube_owner_abs = "W" if resp_is_white else "B"

        elif aid == 33:                                   # raccoon: the doubler redoubles
            # A raccoon answers the beaver (M6): 8x the value before the double,
            # owned by the raccooner, the original doubler. The engine knows no
            # such decision, so there is nothing to analyze -- only the cube to
            # carry forward.
            cube_value = dbl_value * 8
            cube_owner_abs = "W" if is_white else "B"

        elif aid == 36:                                   # cube set by hand
            cube_value = int(ply.get("cube_value") or cube_value)
            cube_owner_abs = {0: "W", 1: "B"}.get(ply.get("cube_owner"))

        elif aid == 31:                                   # set position, no dice
            # The opening position of an exported saved position: no turn, no
            # decision. The board it states is still the one every later ply
            # moves from, so it has to land on the running board -- skipping it
            # would leave every decision after it on a stale position. (A
            # set-position ply *with* dice is a restated play and was handled as
            # a checker decision above, board included.)
            board_p1 = _absolute_to_p1(list(ply.get("set_position") or [0] * 26))

        # terminal (24-30): no decision.

    return decisions


def reconstruct_game_result(game: dict, player_white: str, player_black: str) -> dict | None:
    """Recover the game-result dict (``winner``/``points``/``type``) an OGXM
    game encodes, in the shape ``game_reconstructor`` produces for the ``.mat``
    path. ``None`` for an incomplete game (winner 255).

    ``winner``/``points_won`` are read straight off the game; ``type`` is
    inferred from the ply stream (a drop => cube ``pass``; a resign/forfeit
    terminal action => ``resign``/``forfeit``; otherwise ``normal``). Gammon vs
    backgammon is not a distinct result ``type`` — the multiplier lives in
    ``points`` — so it needs no recovery here.
    """
    winner = game.get("winner")
    if winner not in (0, 1):
        return None
    winner_name = player_white if winner == 0 else player_black
    aids = [p.get("action_id") for p in (game.get("plies") or [])]
    if 23 in aids:                         # a take/drop drop => cube pass
        gtype = "pass"
    elif 29 in aids:
        gtype = "forfeit"
    elif any(a in (27, 28) for a in aids):
        gtype = "resign"
    else:
        gtype = "normal"
    return {"winner": winner_name, "points": game.get("points_won"), "type": gtype}


def reconstruct_decisions_from_ogxm(ogxm: dict) -> list[dict]:
    """Per-game decision lists for a whole OGXM match, in game order.

    Returns ``[{"decisions": [...], "game_result": {...}|None, "is_crawford":
    bool, "sw": int, "sb": int}, ...]`` — one entry per OGXM game, aligned to
    ``ogxm["games"]`` and shaped for ``game_eval.evaluate_game`` (which reads
    ``decisions`` + ``game_result``).
    """
    if int(ogxm.get("variant") or 0) != 0:
        raise UnsupportedMatch(
            "This match is a backgammon variant (nackgammon, hypergammon or "
            "longgammon); the engine plays backgammon only, so it cannot be analyzed.")
    match_length = int(ogxm.get("match_length", 0) or 0)
    player_white = ogxm.get("player_white") or "White"
    player_black = ogxm.get("player_black") or "Black"
    games = ogxm.get("games") or []
    # The match opens at its own score (a mid-match start), and every game's
    # points are capped at what its winner still needed, as the OGID replay does.
    start_scores = _game_start_scores(games, match_length, ogxm.get("score_start"))

    out = []
    for game, (sw, sb) in zip(games, start_scores):
        out.append({
            "decisions": reconstruct_game_decisions(
                game, sw, sb, match_length, player_white, player_black),
            "game_result": reconstruct_game_result(game, player_white, player_black),
            "is_crawford": bool(game.get("is_crawford", False)),
            "sw": sw,
            "sb": sb,
        })
    return out
