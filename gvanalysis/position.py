# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Analyze a backgammon position from an XGID or OGID string.

Usage:
    uv run gvan-position "XGID=-b----E-C---eE---c-e----B-:0:0:1:31:0:0:0:0"
    uv run gvan-position "11ccccchhhjjjjj:66666888dddddoo:N0N:13:B:R:0:0:0:0"
    uv run gvan-position "<id>" --level 3ply

Which format was given is detected from the string itself (an explicit
``XGID=``/``OGID=`` label wins); see ``gvformat.ogid.looks_like_ogid``.

``analyze_position`` is the library entry point behind that CLI, and the one a
server calls: it returns the analysis as an OGXM-shaped dict rather than
printing it. A single position is fast enough to answer in the request itself
(~0.07s at 3ply, ~1.2s worst case at 4ply), which is why there is no job queue
here as there is for a whole match.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from gvformat.export import (
    _build_alternatives,
    _normalize_eval_level,
    _probs_to_eval,
)
from gvformat.ogid import looks_like_ogid, parse_ogid
from .progress import ProgressBar


# --- XGID parser (standalone, no bgsage dependency) -------------------------


@dataclass
class PositionState:
    """A position to analyze, in the on-roll player's perspective."""

    board: list[int]
    die1: int
    die2: int
    cube_value: int
    cube_owner: str   # "centered", "player", or "opponent"
    away1: int        # 0 = money game
    away2: int
    is_crawford: bool
    #: True to analyze the cube even when dice are set (OGID cube states).
    force_cube: bool = False
    #: Whether the on-roll player is White. The board above is in the mover's
    #: perspective either way; this is what maps a move back to the absolute
    #: point numbering the OGXM step form (and every consumer of it) uses.
    mover_is_white: bool = True


#: Back-compat alias -- this was XGID-only before OGID input was added.
XGIDState = PositionState


def parse_xgid(xgid: str) -> PositionState:
    s = xgid.strip()
    if s.upper().startswith("XGID="):
        s = s[5:]

    parts = s.split(":")
    if len(parts) < 9:
        raise ValueError(f"XGID needs at least 9 fields, got {len(parts)}: {xgid!r}")

    board_str = parts[0]
    if len(board_str) != 26:
        raise ValueError(f"XGID board must be 26 chars, got {len(board_str)}: {board_str!r}")

    board = [0] * 26
    for i, ch in enumerate(board_str):
        if ch == "-":
            board[i] = 0
        elif "A" <= ch <= "O":
            board[i] = ord(ch) - ord("A") + 1
        elif "a" <= ch <= "o":
            board[i] = -(ord(ch) - ord("a") + 1)
        else:
            raise ValueError(f"Invalid XGID character {ch!r} at position {i}")
    board[0] = abs(board[0])
    board[25] = abs(board[25])

    cube_value = 1 << int(parts[1])  # log2 encoding: 0→1, 1→2, 2→4 …
    cube_position = int(parts[2])
    turn = int(parts[3])

    # XGID field 3 is the cube position: 0 = centered, +1 = player 1 owns it,
    # -1 = player 2 owns it. Field 4 (turn) uses the same +1/-1 player codes,
    # and the 26-point board is written from the on-roll player's perspective,
    # so the cube is the *mover's* exactly when the two signs agree. (Both
    # halves of gammonview's board.js to_xgid/from_xgid pair encode it this
    # way; a centered cube is what every opening XGID's ":0:0:" says.)
    if cube_position == 0:
        cube_owner = "centered"
    elif (cube_position > 0) == (turn > 0):
        cube_owner = "player"
    else:
        cube_owner = "opponent"

    dice_str = parts[4]
    die1 = int(dice_str[0]) if dice_str and dice_str[0].isdigit() else 0
    die2 = int(dice_str[1]) if len(dice_str) > 1 and dice_str[1].isdigit() else 0

    match_len = int(parts[8]) if parts[8].lstrip("-").isdigit() else 0
    is_crawford = parts[7] == "1"

    if match_len > 0:
        score_a, score_b = int(parts[5]), int(parts[6])
        if turn >= 0:
            away1, away2 = match_len - score_a, match_len - score_b
        else:
            away1, away2 = match_len - score_b, match_len - score_a
    else:
        away1 = away2 = 0

    # An XGID names no colours: its board is simply the on-roll player's. The
    # turn field is the only handle on who that is, and gammonview's own
    # to_xgid/from_xgid pair reads -1 as White on roll (see board.js), so
    # follow it -- otherwise a move's absolute point numbering comes back
    # mirrored from what the client that sent the id would draw.
    return PositionState(
        board, die1, die2, cube_value, cube_owner, away1, away2, is_crawford,
        mover_is_white=(turn == -1),
    )


# --- OGID adapter ------------------------------------------------------------

def state_from_ogid(ogid: str) -> PositionState:
    """Parse an OGID into the same analyzable state an XGID produces.

    ``gvformat.parse_ogid`` already hands back a mover-perspective board and
    mover-relative cube owner; the work here is picking *which* decision the
    position poses, mirroring ``normalizeForAnalysis`` in gammonview's
    ``ogid.js``:

      - a cube state (C/D/A/P) is always a cube decision, evaluated from the
        doubler's side. ``color`` records whoever acted, so the doubler is
        normally the on-roll player -- except for a *pending* double ("D"),
        where the action that produced the position was the double itself, so
        the doubler is ``color`` and the board has to be flipped onto them.
      - otherwise two dice mean a checker play.

    A dead cube (owner "D") has no doubling decision; it is analyzed as a
    centered cube so the checker play still evaluates, since bgsage derives
    cube deadness from the score anyway.
    """
    state = parse_ogid(ogid)
    force_cube = state.is_cube_decision
    if state.game_state == "D":
        state = state.flipped()

    cube_owner = state.cube_owner
    if cube_owner == "dead":
        cube_owner = "centered"

    die1, die2 = (0, 0) if force_cube else (state.die1, state.die2)

    return PositionState(
        board=state.board,
        die1=die1,
        die2=die2,
        cube_value=state.cube_value,
        cube_owner=cube_owner,
        away1=state.away1,
        away2=state.away2,
        is_crawford=state.crawford,
        force_cube=force_cube,
        mover_is_white=state.mover_is_white,
    )


def parse_position_id(text: str) -> PositionState:
    """Parse either an XGID or an OGID, detecting which was given."""
    return state_from_ogid(text) if looks_like_ogid(text) else parse_xgid(text)


# --- Analysis ----------------------------------------------------------------

#: Eval levels a caller may name -- closed rather than open-ended, so an
#: unknown string fails here instead of inside bgsage's analyzer constructor.
#: A *server* must narrow this further: "rollout" is minutes of work, fine for
#: a CLI run deliberately, wrong for a request someone is waiting on. The
#: truncated rollouts are the ones that stay interactive -- 360 trials, ~0.7s
#: (truncated2) and ~2.1s (truncated3) for a checker play on an M-series Mac --
#: which is why they sit on this side of that line.
EVAL_LEVELS = ("1ply", "2ply", "3ply", "4ply",
               "truncated1", "truncated2", "truncated3", "rollout")

#: How many ranked moves to return. The OGXM alternatives cap, which is also
#: well past what any panel shows -- a bear-in roll can have 99 legal plays.
MAX_ALTERNATIVES = 50


#: Levels that run trials, and so have progress worth reporting. Anything
#: full-width finishes before a bar could draw twice.
ROLLOUT_LEVELS = ("truncated1", "truncated2", "truncated3", "rollout")


def _checker_play_params() -> frozenset:
    """Parameter names this bgsage's ``checker_play`` accepts."""
    import inspect
    from bgsage import BgBotAnalyzer
    return frozenset(inspect.signature(BgBotAnalyzer.checker_play).parameters)


def analyze_position(
    position_id: str,
    *,
    level: str = "3ply",
    max_alternatives: int = MAX_ALTERNATIVES,
    analyzer=None,
    on_progress: "Callable[[int, int, str], None] | None" = None,
) -> dict:
    """Analyze one position and return the result as an OGXM-shaped dict.

    ``position_id`` is an XGID or an OGID (detected; see ``parse_position_id``).
    Which decision gets analyzed is the position's own, not the caller's
    choice: an OGID in a cube state is a cube decision, a position with dice is
    a checker play, and a position with neither is a cube decision too (that is
    what "no dice yet" means). This mirrors ``normalizeForAnalysis`` in
    gammonview's ``ogid.js``, so client and server agree on what was asked
    without the question having to travel over the wire.

    The returned ``alternatives`` / ``cube`` objects use the OGXM analysis
    vocabulary -- structured ``move`` steps, named ``eval`` objects, the same
    ``no_double_equity``/``double_take_equity``/``double_pass_equity`` triple a
    stored cube analysis carries -- so a viewer already rendering match
    analysis can render these with the same code.

    ``analyzer`` lets a caller pass a prebuilt ``BgBotAnalyzer`` (constructing
    one costs ~40ms, worth reusing across requests); it must have been built at
    ``level``, which is reported back as ``eval_level``.

    ``on_progress(completed, total, phase)`` reports a rollout's progress; it is
    never called at the full-width levels, which return too fast to report.
    ``phase`` is ``"trials"`` for the planned work and ``"finalizing"`` for the
    tail where bgsage promotes moves its filter dropped and rolls them out one
    at a time. The two phases need separate reporting because the trial
    denominator is fixed before any promotion is known, so bgsage deliberately
    counts *past* its own total there; each promotion's own 0 -> n_trials arc
    arrives on the finalizing phase instead. Treat a ``"finalizing"`` call as
    "restart the bar", not "continue it".
    """
    if level not in EVAL_LEVELS:
        raise ValueError(f"level must be one of {list(EVAL_LEVELS)}, got {level!r}")

    state = parse_position_id(position_id)
    is_checker = bool(state.die1 and state.die2) and not state.force_cube

    if analyzer is None:
        from bgsage import BgBotAnalyzer
        analyzer = BgBotAnalyzer(eval_level=level, cubeful=True)

    match_kwargs = dict(
        cube_value=state.cube_value,
        cube_owner=state.cube_owner,
        away1=state.away1,
        away2=state.away2,
        is_crawford=state.is_crawford,
    )

    started = time.perf_counter()
    result: dict = {
        "position_id": position_id.strip(),
        "kind": "checker" if is_checker else "cube",
        "eval_level": level,
        "mover_is_white": state.mover_is_white,
        "cube_value": state.cube_value,
        "cube_owner": state.cube_owner,
        "away1": state.away1,
        "away2": state.away2,
        "is_crawford": state.is_crawford,
        "dice": [state.die1, state.die2] if is_checker else None,
    }

    progress_kwargs: dict = {}
    if on_progress is not None and level in ROLLOUT_LEVELS:
        progress_kwargs["progress_callback"] = (
            lambda done, total, partial=None: on_progress(done, total, "trials"))

    if is_checker:
        # finalize_progress arrived in bgsage 2.0; an older engine rolls out
        # the same moves, just without saying so.
        if progress_kwargs and "finalize_progress" in _checker_play_params():
            progress_kwargs["finalize_progress"] = (
                lambda done, total: on_progress(done, total, "finalizing"))
        played = analyzer.checker_play(
            state.board, state.die1, state.die2,
            **match_kwargs, **progress_kwargs)
        result["alternatives"] = _alternatives(
            state, played.moves[:max_alternatives])
    else:
        result["cube"] = _cube(analyzer.cube_action(
            state.board, **match_kwargs, **progress_kwargs))

    result["elapsed"] = round(time.perf_counter() - started, 3)
    return result


def _alternatives(state: PositionState, moves) -> list[dict]:
    """Ranked moves, as OGXM ``Alt`` objects.

    The hand-off to ``_build_alternatives`` is deliberate: it is the same
    function that writes a stored match's alternatives, so a position analysed
    here and a ply analysed by ``gvan-match`` produce byte-identical move
    structures -- notation, ``{from, pips}`` steps and all -- instead of two
    step encoders that drift.
    """
    from bgsage.text_export import compute_move_notation

    options = [
        {
            "move": compute_move_notation(
                state.board, list(m.board), state.die1, state.die2),
            "equity": round(float(m.equity), 4),
            "eval_level": _normalize_eval_level(m.eval_level),
            "probs": _probs(m.probs),
        }
        for m in moves
    ]
    # ``state.board`` is already the pre-move board in the mover's own
    # perspective (own checkers positive, opponent's negative), which is exactly
    # the frame the step splitter wants. Without it a one-checker two-die move
    # is split larger-die-first and can be routed through a point the opponent
    # has made -- and the position editor draws its board arrows straight from
    # these steps, so the move is shown landing on a stack of enemy checkers and
    # carrying on.
    return _build_alternatives(
        options, state.mover_is_white, state.die1, state.die2, state.board)


def _cube(action) -> dict:
    """A cube action, in the vocabulary an OGXM cube analysis uses.

    ``equity_loss`` and ``played_action`` are the two fields a stored cube
    analysis carries that are absent here, and they are absent for a reason:
    an edited position has no action to score, only a correct one to name.
    """
    should_double = bool(action.should_double)
    return {
        "correct_action": "double" if should_double else "no_double",
        # The responder's half. A stored analysis records the take/pass a
        # player actually chose; with nobody to have chosen, the useful thing
        # is what the opponent *should* do if doubled -- which is also what
        # makes a double/take vs double/pass reading possible at all.
        "correct_response": "take" if action.should_take else "pass",
        "no_double_equity": round(float(action.equity_nd), 4),
        "double_take_equity": round(float(action.equity_dt), 4),
        "double_pass_equity": round(float(action.equity_dp), 4),
        "eval": _probs_to_eval(_probs(action.probs)),
        "eval_level": _normalize_eval_level(action.eval_level),
        "is_beaver": bool(getattr(action, "is_beaver", False)),
    }


def _probs(p) -> list[float]:
    """Flat ``[win, gammon_win, bg_win, gammon_loss, bg_loss]``.

    Four decimals, not the three ``game_eval`` stores: that rounding is the
    binary format's business (it quantises on write anyway), and a live panel
    showing one position has no reason to throw away a digit.
    """
    return [
        round(float(p.win), 4),
        round(float(p.gammon_win), 4),
        round(float(p.backgammon_win), 4),
        round(float(p.gammon_loss), 4),
        round(float(p.backgammon_loss), 4),
    ]


# --- Analysis output ---------------------------------------------------------

def print_checker_play(result: dict) -> None:
    d1, d2 = result["dice"]
    print(f"\nChecker play ({result['eval_level']})  die1={d1} die2={d2}")
    print(f"  {'Equity':>8}  {'Diff':>7}  {'Win':>6}  {'GW':>6}  {'GL':>6}  Move")
    for i, alt in enumerate(result["alternatives"][:10]):
        marker = " *" if i == 0 else "  "
        ev = alt.get("eval") or {}
        print(
            f"{marker} {alt['equity']:+8.4f}  {alt['diff']:+7.4f}"
            f"  {ev.get('win', 0):6.1%}  {ev.get('gammon_win', 0):6.1%}"
            f"  {ev.get('gammon_loss', 0):6.1%}  {alt['notation']}"
        )


def print_cube_action(result: dict) -> None:
    cube = result["cube"]
    print(f"\nCube action ({result['eval_level']})")
    print(f"  ND equity : {cube['no_double_equity']:+.4f}")
    print(f"  DT equity : {cube['double_take_equity']:+.4f}")
    print(f"  DP equity : {cube['double_pass_equity']:+.4f}")
    print(f"  Action    : {cube['correct_action']}"
          + (f"/{cube['correct_response']}" if cube["correct_action"] == "double" else ""))
    ev = cube["eval"]
    print(f"  Win {ev['win']:.1%}  GW {ev['gammon_win']:.1%}  GL {ev['gammon_loss']:.1%}")


# --- Main --------------------------------------------------------------------

def main() -> None:
    t0 = time.perf_counter()
    parser = argparse.ArgumentParser(
        description="Analyze a position from an XGID or OGID string")
    parser.add_argument(
        "position_id",
        help="XGID or OGID string (format detected; XGID=/OGID= prefix optional)")
    parser.add_argument(
        "--level", default="3ply", choices=EVAL_LEVELS,
        help="Eval level (default: 3ply)")
    parser.add_argument(
        "--no-progress", action="store_true",
        help="Suppress the rollout progress bar (it is stderr-only and already "
             "a no-op when stderr is not a TTY)")
    args = parser.parse_args()

    fmt = "OGID" if looks_like_ogid(args.position_id) else "XGID"
    state = parse_position_id(args.position_id)
    context = "money game" if state.away1 == 0 else f"match {state.away1}-away vs {state.away2}-away"
    print(f"Position ({fmt}): cube={state.cube_value} ({state.cube_owner})  {context}"
          + ("  [Crawford]" if state.is_crawford else ""))

    # A rollout at this CLI is a deliberate minutes-long run, and until now it
    # printed nothing at all until it finished. The bar lives on stderr, so
    # piping the report somewhere stays clean.
    bar = None
    if not args.no_progress and args.level in ROLLOUT_LEVELS:
        bar = ProgressBar(0, label="Rolling out", unit="trials")

    def _progress(done: int, total: int, phase: str) -> None:
        if bar is None:
            return
        if phase == "finalizing":
            # Each promoted move restarts its own 0 -> n_trials arc; bgsage
            # signals the restart with done == 0.
            if done == 0 or bar.total != total:
                bar.reset(total, label="Finalizing")
            bar.set(done)
        else:
            if bar.total != total:
                bar.reset(total, label="Rolling out")
            # The trial phase deliberately counts past its own total once
            # promotions begin; clamp so the bar never reads over 100%.
            bar.set(min(done, total))

    try:
        result = analyze_position(args.position_id, level=args.level,
                                  on_progress=_progress)
    finally:
        if bar is not None:
            bar.clear()

    if result["kind"] == "checker":
        print_checker_play(result)
    else:
        print_cube_action(result)

    print(f"\nAnalysis time: {time.perf_counter() - t0:.2f}s")


if __name__ == "__main__":
    main()
