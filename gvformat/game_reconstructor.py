# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Reconstruct per-move decisions from parsed .mat game data.

No bgsage evaluation happens here — just board tracking and decision building,
so this lives in the engine-free codec layer alongside the other source-format
converters (``xg.py``, ``bgf.py``, ``mat.py``). ``STARTING_BOARD`` and
``flip_board`` come from ``export`` rather than bgsage: the two are
value-identical (the starting board is the same list; ``_flip_board`` matches
``bgsage.board.flip_board`` exactly), but importing them here keeps the package
stdlib-only — same reasoning as ``ogid.py``'s inlined starting board.
"""

from __future__ import annotations

from .export import _STARTING_BOARD_P1 as STARTING_BOARD, _flip_board as flip_board
from .mat_parser import _parse_action


# ---------------------------------------------------------------------------
# Board manipulation
# ---------------------------------------------------------------------------

def _apply_move_notation(board: list[int], notation: str) -> list[int]:
    """Apply move notation to board from mover's perspective. Returns new board."""
    b = list(board)
    for token in notation.split():
        token = token.rstrip("*")
        count = 1
        if "(" in token:
            token, cnt = token.split("(", 1)
            try:
                count = int(cnt.rstrip(")"))
            except ValueError:
                count = 1
        if "/" not in token:
            continue
        from_str, to_str = token.split("/", 1)
        to_str = to_str.rstrip("*")
        try:
            from_pt = 25 if from_str.lower() == "bar" else int(from_str)
            to_pt = None if to_str.lower() in ("off", "bear", "0") else int(to_str)
        except ValueError:
            continue
        for _ in range(count):
            if from_pt == 25:
                if b[25] <= 0:
                    continue  # no checker on bar to move
                b[25] -= 1
            elif 1 <= from_pt <= 24:
                if b[from_pt] <= 0:
                    continue  # notation references a point with no mover's checker
                b[from_pt] -= 1
            else:
                continue
            if to_pt is not None and 1 <= to_pt <= 24:
                if b[to_pt] == -1:  # Hit: blot → opponent's bar (slot 0, positive)
                    b[to_pt] = 0
                    b[0] += 1
                b[to_pt] += 1
    return b


# ---------------------------------------------------------------------------
# Match / game helpers
# ---------------------------------------------------------------------------

def _flip_owner(owner: str) -> str:
    return {"centered": "centered", "player": "opponent", "opponent": "player"}[owner]


def _away_for(is_mover_p1: bool, s1: int, s2: int, ml: int) -> tuple[int, int]:
    """(away_mover, away_opp). Returns (0,0) for money games."""
    if ml <= 0:
        return (0, 0)
    if is_mover_p1:
        return (max(1, ml - s1), max(1, ml - s2))
    return (max(1, ml - s2), max(1, ml - s1))


def find_crawford_game_index(
    games: list[dict],
    match_length: int,
    crawford_rule: bool | None = None,
) -> int | None:
    """Return the 0-based index of the Crawford game, or None.

    The Crawford game is the first game where exactly one player is 1-away.
    All subsequent games at that score are post-Crawford (cube available).
    Returns None for money games or when the Crawford rule is explicitly off.
    """
    if match_length <= 0 or crawford_rule is False:
        return None
    for i, game in enumerate(games):
        s1 = game["score1_start"]
        s2 = game["score2_start"]
        if (s1 == match_length - 1) != (s2 == match_length - 1):
            return i
    return None


# ---------------------------------------------------------------------------
# Decision reconstruction
# ---------------------------------------------------------------------------

def reconstruct_decisions(
    game: dict,
    match_length: int,
    p1: str,
    p2: str,
    is_crawford: bool = False,
) -> dict:
    """Walk a game's player_actions and emit a decision dict for each evaluation needed.

    Decision dicts:
      kind='cube'   — one cube-turn: doubler's no/double + optional responder take/pass.
        'board': list[int]    from doubler's perspective
        'cube_value': int     PRE-double value
        'cube_owner': str     from doubler's perspective
        'doubled': bool
        'response': 'take'|'pass'|None
        'doubler': str        player name
        'responder': str|None
        'is_doubler_p1': bool
        'away1': int          doubler's away
        'away2': int          responder's away
        'is_crawford': bool

      kind='checker' — one checker move.
        'board': list[int]    from mover's perspective
        'dice': [d1, d2]
        'board_played': list[int]  resulting board (mover's perspective)
        'cube_value': int
        'cube_owner': str     from mover's perspective
        'player': str
        'is_p1': bool
        'away1': int
        'away2': int
        'is_crawford': bool
    """
    s1 = game["score1_start"]
    s2 = game["score2_start"]

    board = list(STARTING_BOARD)
    cube_value = 1
    cube_owner = "centered"  # always from current mover's perspective

    decisions: list[dict] = []
    game_result: dict | None = None
    # How the game ended, once a resign/forfeit/timeout line has said so. Held
    # rather than acted on, because the "Wins N points" line that normally
    # follows is the authoritative one -- see the loop below.
    ended_by: dict | None = None

    # Cube-offer state
    any_move_made = False  # cube cannot be offered before the opening roll
    dbl_pending = False
    dbl_board: list[int] = []
    dbl_cube_val = 0
    dbl_cube_own = ""
    dbl_player = ""
    dbl_is_p1 = False

    for player, action_str in game["player_actions"]:
        is_p1 = player == p1
        atype, adata = _parse_action(action_str)

        if atype == "win":
            info = adata if isinstance(adata, dict) else {}
            game_result = {
                "winner": player,
                "points": info.get("points"),
                # How it ended outranks how it was scored: "Resigned Game"
                # followed by "Wins 2 points" is a resignation worth two, not
                # a plain win.
                "type": ended_by["type"] if ended_by else info.get("type", "normal"),
            }
            break
        if atype in ("resign", "forfeit", "time"):
            # Whose column the line sits in does not settle who lost:
            # OpenGammon puts "Resigned Game" in the *winner's*, and the
            # alternating two-column layout means the side it lands on is a
            # function of the ply count, not of intent. So guess from the
            # column only as a last resort, and let the "Wins N points" line
            # that normally follows overrule it -- that one names the winner
            # outright and carries the points, which a resignation otherwise
            # loses (they are not derivable from the cube: a player may resign
            # a gammon).
            ended_by = {
                "winner": p2 if is_p1 else p1,
                "points": None,
                "type": atype,
            }
            # "???" is the exception. It stands where a checker play should
            # be, so the board is unreconstructable from here and reading on
            # would replay later moves against a stale position.
            if adata == "unplayed":
                break
            continue

        if atype == "double":
            new_val = int(adata)
            dbl_pending = True
            dbl_board = list(board)
            dbl_cube_val = cube_value
            dbl_cube_own = cube_owner
            dbl_player = player
            dbl_is_p1 = is_p1
            cube_value = new_val

        elif atype == "take":
            if dbl_pending:
                away1, away2 = _away_for(dbl_is_p1, s1, s2, match_length)
                decisions.append({
                    "kind": "cube",
                    "board": dbl_board,
                    "cube_value": dbl_cube_val,
                    "cube_owner": dbl_cube_own,
                    "doubled": True,
                    "response": "take",
                    "doubler": dbl_player,
                    "responder": p2 if dbl_is_p1 else p1,
                    "is_doubler_p1": dbl_is_p1,
                    "away1": away1,
                    "away2": away2,
                    "is_crawford": is_crawford,
                })
                cube_owner = "opponent"  # responder now owns cube, from doubler's view
                dbl_pending = False

        elif atype == "drop":
            if dbl_pending:
                away1, away2 = _away_for(dbl_is_p1, s1, s2, match_length)
                decisions.append({
                    "kind": "cube",
                    "board": dbl_board,
                    "cube_value": dbl_cube_val,
                    "cube_owner": dbl_cube_own,
                    "doubled": True,
                    "response": "pass",
                    "doubler": dbl_player,
                    "responder": p2 if dbl_is_p1 else p1,
                    "is_doubler_p1": dbl_is_p1,
                    "away1": away1,
                    "away2": away2,
                    "is_crawford": is_crawford,
                })
                game_result = {
                    "winner": dbl_player,
                    "points": dbl_cube_val,
                    "type": "pass",
                }
                dbl_pending = False
            break  # Double/pass ends the game

        elif atype == "move":
            die1, die2, moves_str = adata

            # Record "no double" cube decision if player had access and didn't double
            if any_move_made and not dbl_pending and cube_owner in ("centered", "player") and not is_crawford:
                away1, away2 = _away_for(is_p1, s1, s2, match_length)
                # Skip dead cube: mover can already clinch the match at current stake
                if not (match_length > 0 and cube_value >= away1):
                    decisions.append({
                        "kind": "cube",
                        "board": list(board),
                        "cube_value": cube_value,
                        "cube_owner": cube_owner,
                        "doubled": False,
                        "response": None,
                        "doubler": player,
                        "responder": None,
                        "is_doubler_p1": is_p1,
                        "away1": away1,
                        "away2": away2,
                        "is_crawford": is_crawford,
                    })

            # Apply checker move
            board_before = list(board)
            no_legal = moves_str.strip().lower() in ("", "(none)", "none", "-")
            board_after = list(board) if no_legal else _apply_move_notation(board, moves_str)
            any_move_made = True

            away1, away2 = _away_for(is_p1, s1, s2, match_length)
            decisions.append({
                "kind": "checker",
                "board": board_before,
                "dice": [die1, die2],
                "notation": moves_str,
                "board_played": board_after,
                "cube_value": cube_value,
                "cube_owner": cube_owner,
                "player": player,
                "is_p1": is_p1,
                "away1": away1,
                "away2": away2,
                "is_crawford": is_crawford,
                "no_legal": no_legal,
            })

            board = list(flip_board(board_after))
            cube_owner = _flip_owner(cube_owner)

    # No "Wins N points" line ever came -- some writers end a resigned game on
    # the resign line alone. The column guess is all there is.
    if game_result is None and ended_by is not None:
        game_result = ended_by

    return {"decisions": decisions, "game_result": game_result, "is_crawford": is_crawford}
