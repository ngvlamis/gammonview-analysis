# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Reconstruct a Jellyfish/OpenGammon .mat match file from OGXM-JSON analysis output.

The input is the hierarchical match -> game -> ply -> analysis JSON produced
by ``ogxm_export.to_ogxm_json`` (conforming to ``OGXM_JSON_SPEC_GAMMONVIEW.md``),
which records every checker move (forced or not) and every cube decision, so
the reconstructed file is a complete match record equivalent to the original
.mat.

Usage:
    python reconstruct_mat.py match.gva
    python reconstruct_mat.py match.gva.gz
    python reconstruct_mat.py match.gva -o output.mat
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from gvformat.binary import cap_points_won

# Absolute column where P2 content begins on turn lines and the win/result line.
_P2_COL = 46

# Ply action_id boundaries (see OGXM_JSON_SPEC_GAMMONVIEW.md's Action ID Table).
_ACTION_CHECKER_MAX = 20
_ACTION_DOUBLE = 21
_ACTION_TAKE = 22
_ACTION_DROP = 23
_ACTION_RESIGN_GAME = 27
_ACTION_RESIGN_MATCH = 28
_ACTION_FORFEIT = 29


def _turn_line(turn_num: int, p1_text: str = "", p2_text: str = "") -> str:
    left = f"  {turn_num}) {p1_text}"
    if not p2_text:
        return left
    pad = max(3, _P2_COL - len(left))
    return left + " " * pad + p2_text


# ---------------------------------------------------------------------------
# Move-text rendering
# ---------------------------------------------------------------------------

def _played_notation(ply: dict) -> str | None:
    """Notation of the played alternative, from the ply's inline analysis.

    Returns None if no analysis (or no is_played alternative) is present, so
    the caller can fall back to rendering from the structured ``moves`` list.
    """
    analysis = ply.get("analysis")
    if not analysis:
        return None
    for alt in analysis.get("alternatives", []):
        if alt.get("is_played"):
            return alt.get("notation", "") or ""
    return None


def _fallback_notation(ply: dict) -> str:
    """Best-effort notation rendered from ``ply['moves']`` ([{from, pips}]),
    used only when a checker ply lacks analysis. ``from`` is OGXM-absolute
    (0=white bar, 25=black bar, 1-24 white's numbering); convert to the
    mover's own point numbering, then merge consecutive hops belonging to
    the same checker (hop[i].to == hop[i+1].from) into a single span.
    """
    steps = ply.get("moves") or []
    if not steps:
        return ""
    mover_is_white = ply.get("color") == 1

    def to_mover(abs_pt: int) -> int:
        return (25 - abs_pt) if mover_is_white else abs_pt

    def fmt(pt: int) -> str:
        if pt == 25:
            return "bar"
        if pt <= 0:
            return "off"
        return str(pt)

    groups: list[list[int]] = []
    for step in steps:
        f = to_mover(step["from"])
        t = f - step["pips"]
        if groups and groups[-1][1] == f:
            groups[-1][1] = t
        else:
            groups.append([f, t])

    return " ".join(f"{fmt(a)}/{fmt(b)}" for a, b in groups)


def _checker_text(ply: dict) -> str:
    d1, d2 = ply.get("d1"), ply.get("d2")
    notation = _played_notation(ply)
    if notation is None:
        notation = _fallback_notation(ply)
    return f"{d1}{d2}: {notation}" if notation else f"{d1}{d2}:"


# ---------------------------------------------------------------------------
# Per-game reconstruction
# ---------------------------------------------------------------------------

def _game_lines(
    game: dict, player_white: str, player_black: str,
    match_length: int | None, score_start: dict,
) -> list[str]:
    plies = game.get("plies", [])
    # Turn-level actions (checker moves, doubles, takes, drops) vs. the
    # trailing game/match-end marker ply appended by the exporter.
    turn_plies = [p for p in plies if p.get("action_id", 0) <= _ACTION_DROP]
    end_plies = [p for p in plies if p.get("action_id", 0) > _ACTION_DROP]

    p1_actions: list[str] = []
    p2_actions: list[str] = []
    cube = 1
    first_color: int | None = None

    for ply in turn_plies:
        color = ply.get("color")
        is_p1 = color == 1
        if first_color is None:
            first_color = color

        action_id = ply.get("action_id")
        if action_id == _ACTION_DOUBLE:
            cube *= 2
            text = f"Doubles => {cube}"
        elif action_id == _ACTION_TAKE:
            text = "Takes"
        elif action_id == _ACTION_DROP:
            text = "Drops"
        else:
            text = _checker_text(ply)

        (p1_actions if is_p1 else p2_actions).append(text)

    # When P2 opened the game, shift P1's column by one so turn 1 shows
    # only P2's move in the right column.
    if first_color == 0:
        p1_actions.insert(0, "")

    # --- Game result / win line ---------------------------------------
    win_line: str | None = None
    win_is_p1 = False

    winner = game.get("winner", 255)
    if end_plies and winner in (0, 1):
        end_ply = end_plies[-1]
        end_action = end_ply.get("action_id")
        points = game.get("points_won", 0)
        winner_is_p1 = winner == 0

        last_turn_action = turn_plies[-1].get("action_id") if turn_plies else None
        is_resign = end_action in (_ACTION_RESIGN_GAME, _ACTION_RESIGN_MATCH)
        is_forfeit = end_action == _ACTION_FORFEIT
        # A cube-drop ending (double followed immediately by a drop): the
        # points paid are the pre-double stake, not points_won/cube_at_end,
        # so no gammon/backgammon ratio applies here.
        is_pass = (not is_resign and not is_forfeit) and last_turn_action == _ACTION_DROP

        if is_resign or is_forfeit:
            resigner_is_p1 = not winner_is_p1
            text = "Resigns" if is_resign else "Forfeits"
            (p1_actions if resigner_is_p1 else p2_actions).append(text)

        rtype = "normal"
        if not (is_pass or is_resign or is_forfeit) and cube:
            ratio = points / cube
            if abs(ratio - 3) < 0.01:
                rtype = "backgammon"
            elif abs(ratio - 2) < 0.01:
                rtype = "gammon"

        base = f"Wins {points} points" if points else "Wins"
        if rtype in ("gammon", "backgammon"):
            base += f" with {rtype}"

        if match_length:
            winner_start = score_start["player1"] if winner_is_p1 else score_start["player2"]
            if winner_start + points >= match_length:
                base += " and the match"

        win_line = base
        win_is_p1 = winner_is_p1

    lines: list[str] = []

    # Score line.
    left_score = f" {player_white}: {score_start['player1']}"
    pad = max(3, _P2_COL - len(left_score))
    lines.append(left_score + " " * pad + f"{player_black}: {score_start['player2']}")

    # Paired turn lines.
    n_turns = max(len(p1_actions), len(p2_actions))
    for i in range(n_turns):
        p1_text = p1_actions[i] if i < len(p1_actions) else ""
        p2_text = p2_actions[i] if i < len(p2_actions) else ""
        lines.append(_turn_line(i + 1, p1_text=p1_text, p2_text=p2_text))

    # Win/result line — no turn number, placed in the winner's column.
    if win_line:
        if win_is_p1:
            lines.append("   " + win_line)
        else:
            lines.append(" " * _P2_COL + win_line)

    return lines


def reconstruct_mat(data: dict) -> str:
    p1 = data["player_white"]
    p2 = data["player_black"]
    ml = data.get("match_length")

    out: list[str] = []

    # Header comments.
    out.append(f'; [Player 1 "{p1}"]')
    out.append(f'; [Player 2 "{p2}"]')

    for key, label in [
        ("crawford", "Crawford"),
        ("jacoby", "Jacoby"),
        ("beaver", "Beaver"),
    ]:
        val = data.get(key)
        if val is not None:
            out.append(f'; [{label} "{"On" if val else "Off"}"]')

    if data.get("cube_limit"):
        out.append(f'; [CubeLimit "{data["cube_limit"]}"]')
    # `event`/`site` map straight back to the two headers they were parsed
    # from. (Date is still folded into `timestamp` and is not re-emitted.)
    if data.get("event"):
        out.append(f'; [Event "{data["event"]}"]')
    if data.get("site"):
        out.append(f'; [Site "{data["site"]}"]')

    out.append(f"{ml} point match" if ml and ml > 0 else "money game")
    out.append("")

    # Each game's starting score is derived by accumulating points_won
    # across previous games to their winner (there is no stored score_start
    # in the new format).
    score_p1 = 0
    score_p2 = 0
    for game in data.get("games", []):
        score_start = {"player1": score_p1, "player2": score_p2}
        out.append(f" Game {game['game_index'] + 1}")
        out.extend(_game_lines(game, p1, p2, ml, score_start))
        out.append("")

        winner = game.get("winner", 255)
        # points_won is the game's full value; a running score stops at the
        # match length, so the deciding game contributes only what its winner
        # still needed (the "Wins 4 points" line above is unaffected).
        points = game.get("points_won", 0)
        if winner == 0:
            score_p1 += cap_points_won(points, score_p1, ml)
        elif winner == 1:
            score_p2 += cap_points_won(points, score_p2, ml)

    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bgs_file", type=Path, help=".gva or .gva.gz file (OGXM-JSON)")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output .mat file (default: <bgs_file>.mat)",
    )
    args = parser.parse_args()

    if not args.bgs_file.exists():
        print(f"File not found: {args.bgs_file}", file=sys.stderr)
        sys.exit(1)

    if args.bgs_file.suffix == ".gz":
        raw = gzip.decompress(args.bgs_file.read_bytes()).decode("utf-8")
        default_out = args.bgs_file.with_suffix("").with_suffix(".mat")
    else:
        raw = args.bgs_file.read_text(encoding="utf-8")
        default_out = args.bgs_file.with_suffix(".mat")

    data = json.loads(raw)
    mat_text = reconstruct_mat(data)

    out_path = args.output or default_out
    out_path.write_text(mat_text, encoding="utf-8")
    print(f"Written to {out_path}")


if __name__ == "__main__":
    main()
