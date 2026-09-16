# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis
#
# Portions of this file are ported from HedgeHog's C++ codec
# (MIT, Copyright (c) 2026 Eran Lambooij). See THIRD-PARTY-NOTICES.md,
# whose notices must be preserved in copies of this file.

"""Pure-Python converter: this repo's match-analysis result -> OGXM-JSON.

``to_ogxm_json(result)`` takes exactly what ``gvan_match.analyze_mat(...)``
returns (the ad-hoc GVA-shaped dict: a top-level ``"summary"`` plus
``"games"[i]["moves"]``) and produces a dict conforming to
``OGXM_JSON_SPEC_GAMMONVIEW.md`` -- the hierarchical match -> game -> ply ->
analysis JSON interchange format.

This module is a pure JSON-conversion layer: it does not call bgsage, the
neural-net evaluator, or any compiled engine extension (``bgbot_cpp``). It
only reshapes/renames fields, derives OGID position strings via ``ogid.py``
(also stdlib-only), and reconstructs structured checker-move steps from the
``board_before``/``board_after`` arrays already present in the input.

Board convention (see ``game_eval.py``'s ``_board_p1``): every
``board_before``/``board_after``/cube ``board`` in the input is already
normalized to a fixed **Player-1 / White perspective** -- index 0 = Black's
bar, index 25 = White's bar, index 1-24 signed count at White's own point i
(positive = White checkers, negative = Black checkers). This is exactly the
frame ``ogid.board_to_ogid`` expects when called with ``mover_is_white=True``.

Each ply's ``ogid_before``/``ogid_after`` ``game_state`` and cube-action
fields are produced by ``_TurnState``, a port of the OGID turn-phase state
machine in ``ogxm_replay.cpp``'s ``replay_game()`` -- HedgeHog is at
https://gitlab.com/eranlambooij/hedgehog-public (see
HedgeHog's ``src/match/ogxm_replay.cpp`` and the string
constants it uses from ``ogid.h``). This tracks, across a game's ply
sequence, ``cur_state``/``cube_action``/``cube_owner``/``cube_log2``/
``move_id``/``awaiting_response``/``is_first_ply`` exactly as the reference
does. The synthetic game-end ply this exporter appends per game (from
``game_result``) mirrors the reference exactly: the reference never
specializes ``game_state``/``cube_action`` for a terminal action id, so the
end ply carries ``cur_state``/``cube_action`` forward from the last real
ply, and its ``ogid_after`` flips the color field to the opponent like any
real ply. Per-ply OGIDs are byte-identical to ``libogxm`` on ``filias.mat``
(603/603 ``ogid_before`` and ``ogid_after``).

Notes:

- Equity<->MWC conversion is compute-on-read (see ``gvformat.met`` and
  ``gvformat.stats``): MWC is derived from ``(equity, score, cube)`` via the
  one shipped Kazaross-XG2 MET, so this module stores no per-decision MWC
  anchors and needs no engine call to produce OGXM output.
- ``Alt.move`` (structured steps for *alternative*, non-played moves) is
  derived by parsing the alternative's notation string (no board array is
  available for alternatives), reusing the same die-splitting rules as the
  played move's board-diff derivation. The played move's steps -- the ones
  the validation harness checks -- come from an authoritative board diff.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .binary import RESIGN_ACTIONS, cap_points_won
from .ogid import board_to_ogid
from .place import clean_place

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Standard starting position, Player-1/White perspective (index 0 = Black's
#: bar, 25 = White's bar, 1-24 signed count at White's own point).  Used only
#: as a fallback initial board for a game that (unusually) has zero plies.
_STARTING_BOARD_P1 = [
    0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5,
    5, 0, 0, 0, -3, 0, -5, 0, 0, 0, 0, 2, 0,
]

#: Ascending dice-pair ordering used both by game_eval.py's luck loop and by
#: the OGXM Action ID Table (0=Dice 11 ... 20=Dice 66).
_DICE_PAIRS: list[tuple[int, int]] = [
    (d1, d2) for d1 in range(1, 7) for d2 in range(d1, 7)
]
_DICE_ACTION_ID: dict[tuple[int, int], int] = {
    pair: i for i, pair in enumerate(_DICE_PAIRS)
}

#: OGID turn-phase state/action string constants, ported byte-for-byte from
#: HedgeHog's ``src/ogid.h`` (``OGID_STATE_*`` / ``OGID_ACTION_*``
#: / ``OGID_CUBE_*`` #defines). These drive the per-ply state machine in
#: ``_TurnState`` below, itself a port of ``ogxm_replay.cpp``'s ``replay_game``.
_OGID_STATE_INITIAL_WHITE = "IW"   # OGID_STATE_INITIAL_WHITE  (unused by this port: see module docstring)
_OGID_STATE_INITIAL_BOTH = "IB"    # OGID_STATE_INITIAL_BOTH
_OGID_STATE_ROLLED = "R"           # OGID_STATE_ROLLED
_OGID_STATE_CHECKER_DONE = "C"     # OGID_STATE_CHECKER_DONE
_OGID_STATE_AFTER_TAKE = "A"       # OGID_STATE_AFTER_TAKE
_OGID_STATE_DOUBLE_OFFERED = "D"   # OGID_STATE_DOUBLE_OFFERED
_OGID_STATE_PASSED = "P"           # OGID_STATE_PASSED (unused by this port: see module docstring)
_OGID_STATE_PRE_ROLL = "PR"        # OGID_STATE_PRE_ROLL (unused by this port: see module docstring)
_OGID_STATE_GAME_OVER = "G"        # OGID_STATE_GAME_OVER
_OGID_STATE_MATCH_OVER = "M"       # OGID_STATE_MATCH_OVER (unused by this port: see module docstring)
_OGID_STATE_FINISHED = "F"         # OGID_STATE_FINISHED (unused by this port: see module docstring)
_OGID_STATE_RESIGNED_GAME = "RG"   # OGID_STATE_RESIGNED_GAME (unused by this port: see module docstring)
_OGID_STATE_RESIGNED_MATCH = "RM"  # OGID_STATE_RESIGNED_MATCH (unused by this port: see module docstring)
_OGID_STATE_FORFEIT = "FF"         # OGID_STATE_FORFEIT (unused by this port: see module docstring)

_OGID_ACTION_NONE = "N"    # OGID_ACTION_NONE
_OGID_ACTION_DOUBLE = "O"  # OGID_ACTION_DOUBLE (cube "offered")
_OGID_ACTION_TAKE = "T"    # OGID_ACTION_TAKE
_OGID_ACTION_PASS = "P"    # OGID_ACTION_PASS

_OGID_CUBE_CENTERED = "N"  # OGID_CUBE_CENTERED
_OGID_CUBE_WHITE = "W"     # OGID_CUBE_WHITE
_OGID_CUBE_BLACK = "B"     # OGID_CUBE_BLACK


#: Engine-reported eval-level strings (e.g. "2-ply") and this repo's own
#: truncated-rollout display codes (e.g. "2T") -> canonical OGXM eval_level
#: strings ("2ply", "truncated2", ...). Empirically observed raw values for
#: the `very_quick` preset are "1-ply"/"2-ply"; the "NT" forms come from
#: game_eval.py's `_LEVEL_DISPLAY` when a rollout's raw level is "Rollout".
_TRUNCATED_DISPLAY_TO_CANON = {"1T": "truncated1", "2T": "truncated2", "3T": "truncated3"}
_HYPHEN_PLY_RE = re.compile(r"^(\d)-ply$", re.IGNORECASE)

_CANONICAL_EVAL_LEVELS = {
    "1ply", "2ply", "3ply", "4ply",
    "truncated1", "truncated2", "truncated3", "rollout",
}


def _normalize_eval_level(raw: str | None) -> str | None:
    """Map an engine/display eval-level string to the canonical OGXM form."""
    if raw is None:
        return None
    if raw in _CANONICAL_EVAL_LEVELS:
        return raw
    if raw in _TRUNCATED_DISPLAY_TO_CANON:
        return _TRUNCATED_DISPLAY_TO_CANON[raw]
    if raw.strip().lower() == "rollout":
        return "rollout"
    m = _HYPHEN_PLY_RE.match(raw.strip())
    if m:
        return f"{m.group(1)}ply"
    return raw  # unknown form: pass through unchanged rather than drop data


def _eval_depth(eval_level: str | None) -> int:
    """Best-effort plain integer depth for AnalysisInfo.ply."""
    if not eval_level:
        return 0
    m = re.search(r"(\d+)", eval_level)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Small pure helpers: board flip, cube-action label, eval-object build
# ---------------------------------------------------------------------------

def _flip_board(board: list[int]) -> list[int]:
    """Mirror a 26-slot board to the other player's perspective.

    Reimplements ``bgsage.board.flip_board``'s documented behaviour (see
    ``ogid.py``'s module docstring, itself cross-validated against golden
    vectors): index 0 <-> 25 swap (unsigned bar counts), and for 1-24,
    ``flipped[25 - i] = -board[i]``.
    """
    flipped = [0] * 26
    flipped[0] = board[25]
    flipped[25] = board[0]
    for i in range(1, 25):
        flipped[25 - i] = -board[i]
    return flipped


def _canonical_key(name: str) -> tuple:
    """Sort key for the canonical white/black rule: case-insensitive,
    Unicode-aware primary order (``casefold``), raw-string tiebreak for names
    differing only by case."""
    return (name.casefold(), name)


def _canonical_orientation(name1: str, name2: str) -> tuple[str, str, bool]:
    """Canonical (white, black, name1_is_white) for two source player names.

    White = the alphabetically-first name by ``_canonical_key``. A tie
    (byte-identical names) is genuinely ambiguous, so it keeps source order:
    name1 stays white.
    """
    if _canonical_key(name2) < _canonical_key(name1):
        return name2, name1, False
    return name1, name2, True


def _flip_game_orientation(game: dict) -> dict:
    """Return a copy of ``game`` with every board mirrored to the other
    player's perspective (``_flip_board``) and ``score_start``'s
    player1/player2 swapped.

    Used when canonical white is the source's player-2: every board in the
    input is in a fixed player-1/white frame (see module docstring), so
    flipping every board here is what makes the rest of the (unmodified)
    converter body -- which assumes player-1 == white -- produce canonical
    output. Leaves ``entry["player"]`` (a name, matched against
    ``player_white`` downstream) and move-notation strings untouched: they
    are name-keyed / mover-relative, not frame-dependent.
    """
    new_game = dict(game)
    if "score_start" in game:
        ss = game["score_start"]
        new_game["score_start"] = {
            "player1": ss.get("player2"),
            "player2": ss.get("player1"),
        }
    new_moves = []
    for entry in game.get("moves", []):
        new_entry = dict(entry)
        if "board_before" in entry:
            new_entry["board_before"] = _flip_board(entry["board_before"])
        if "board_after" in entry:
            new_entry["board_after"] = _flip_board(entry["board_after"])
        if "board" in entry:
            new_entry["board"] = _flip_board(entry["board"])
        new_moves.append(new_entry)
    new_game["moves"] = new_moves
    return new_game


def _cube_action_label(should_double: bool, double_take_equity: float, double_pass_equity: float) -> str:
    """Derive the OGXM cube `action` label from the correct decision.

    ``"no_double"`` when not doubling is correct; otherwise ``"double_take"``
    when the opponent's correct response is to take (``double_pass_equity >=
    double_take_equity``) else ``"double_pass"``.
    """
    if not should_double:
        return "no_double"
    return "double_take" if double_pass_equity >= double_take_equity else "double_pass"


def _probs_to_eval(probs: list[float]) -> dict:
    """flat [win, gammon_win, bg_win, gammon_loss, bg_loss] -> named Eval object."""
    win, gwin, bgwin, gloss, bgloss = probs[0], probs[1], probs[2], probs[3], probs[4]
    equity = win + gwin + bgwin - gloss - bgloss
    return {
        "win": win, "gammon_win": gwin, "bg_win": bgwin,
        "gammon_loss": gloss, "bg_loss": bgloss, "equity": round(equity, 4),
    }


def _dice_action_id(d1: int, d2: int) -> int:
    return _DICE_ACTION_ID[(min(d1, d2), max(d1, d2))]


# ---------------------------------------------------------------------------
# Structured move steps: board-diff -> mover-frame spans -> single-die hops
# -> OGXM absolute {"from", "pips"} steps.
#
# The diff/matching phase is a hit-marker-free port of
# bgsage.text_export.compute_move_notation's matching algorithm (that
# function is the reference this repo already uses to render `player_move`;
# see its docstring). OGXM's Move.pips is constrained to a single die
# (1-6), so any span the matcher produces spanning more than one die
# (typically only possible with doubles, or a non-double move using both
# dice on one checker) is further split into single-die hops here -- a step
# compute_move_notation's own merged text notation does NOT need, since
# written notation is allowed to show a combined span like "13/7".
# ---------------------------------------------------------------------------

def _hit_points(before: list[int], after: list[int]) -> set[int]:
    """Points (mover numbering) where an opponent checker was captured
    during this ply -- including a point the mover only passed *through*
    (hit-and-continue), which a purely net before/after point-count diff
    cannot otherwise distinguish from "nothing happened here". Mirrors
    ``compute_move_notation``'s own ``hit_points`` detection exactly: a
    point that held an opponent checker (before[i] < 0) and, after the
    ply, either isn't a made opposing point anymore or gained a mover
    checker, was hit. This is the same signal that function uses to know
    which of two dice was played first through a blot -- needed here for
    the same reason: to correctly order-split a combined-dice span into
    single-die OGXM steps that match the actual path played, not just any
    numerically-equivalent path.
    """
    pts = set()
    for i in range(1, 25):
        if before[i] < 0 and (after[i] >= 0 or after[i] > before[i]):
            pts.add(i)
    return pts


def _matched_spans(before: list[int], after: list[int], d1: int, d2: int) -> list[tuple[int, int]]:
    """Diff two mover-perspective boards into (from, to) spans, mover numbering
    (1-24 board points, 25 = mover's bar, 0 = borne off)."""
    from_pts: list[int] = []
    to_pts: list[int] = []

    bar_diff = after[25] - before[25]
    if bar_diff < 0:
        from_pts.extend([25] * (-bar_diff))

    for i in range(1, 25):
        wb = before[i] if before[i] > 0 else 0
        wa = after[i] if after[i] > 0 else 0
        if before[i] < 0 and after[i] > 0:
            wa, wb = after[i], 0
        elif before[i] > 0 and after[i] < 0:
            wb, wa = before[i], 0
        diff = wa - wb
        if diff > 0:
            to_pts.extend([i] * diff)
        elif diff < 0:
            from_pts.extend([i] * (-diff))

    on_before = before[25] + sum(v for v in before[1:25] if v > 0)
    on_after = after[25] + sum(v for v in after[1:25] if v > 0)
    borne_off = on_before - on_after
    to_pts.extend([0] * borne_off)

    from_pts.sort(reverse=True)
    to_pts.sort(reverse=True)

    dice = [d1, d1, d1, d1] if d1 == d2 else [d1, d2]
    spans: list[tuple[int, int]] = []
    used_from = [False] * len(from_pts)
    used_to = [False] * len(to_pts)
    used_die = [False] * len(dice)

    for di, d in enumerate(dice):
        if used_die[di]:
            continue
        for fi, f in enumerate(from_pts):
            if used_from[fi]:
                continue
            expected = (25 - d) if f == 25 else (f - d)
            matched = False
            for ti, t in enumerate(to_pts):
                if used_to[ti]:
                    continue
                if t == expected or (expected <= 0 and t == 0):
                    spans.append((f, t))
                    used_from[fi] = used_to[ti] = used_die[di] = True
                    matched = True
                    break
            if matched:
                break

    for fi, f in enumerate(from_pts):
        if used_from[fi]:
            continue
        for ti, t in enumerate(to_pts):
            if used_to[ti]:
                continue
            spans.append((f, t))
            used_from[fi] = used_to[ti] = True
            break

    return spans


def _span_distance(f: int, t: int) -> int:
    return (25 - t) if f == 25 else (f - t)


def _split_span_double(f: int, t: int, die: int) -> list[tuple[int, int]]:
    dist = _span_distance(f, t)
    if dist <= 0:
        return [(f, die)]
    if dist % die != 0:
        return [(f, dist)]  # best-effort fallback (shouldn't occur for legal doubles)
    n = dist // die
    hops = []
    cur = f
    for _ in range(n):
        hops.append((cur, die))
        cur = (25 - die) if cur == 25 else (cur - die)
    return hops


def _split_span_nondouble(
    f: int, t: int, d1: int, d2: int,
    board: list[int] | None = None, hit_points: set[int] | None = None,
) -> list[tuple[int, int]]:
    """Split a (possibly two-die) span into single-die hops.

    When a single checker uses both (different) dice, the board diff alone
    can't tell us which die was played first -- only the final landing
    point is observable. Two signals (when available -- both require the
    mover-perspective board *before* this move, absent when deriving an
    unplayed alternative's steps from its notation string only) disambiguate,
    in priority order:

      1. ``hit_points``: if exactly one of the two candidate intermediate
         points is a point where a capture happened this ply, that's the
         true intermediate stop (mirrors ``compute_move_notation``'s own
         hit-based move splitting -- the same ambiguity, the same fix).
      2. ``board``: otherwise prefer whichever intermediate point isn't
         blocked by an opponent's made point (>=2 checkers), since landing
         there would have been illegal.

    With neither signal (or both orders tied on it), the tie is broken by a
    canonical rule (larger die first) -- a real but rare residual ambiguity,
    since a board diff alone cannot always recover which specific die order
    a human/engine actually played when it doesn't affect the final board.
    The rule is deliberately independent of whether the caller passed
    ``(d1, d2)`` or ``(d2, d1)``: different importers report a ply's dice in
    different orders (e.g. XG preserves original roll order; some sources
    always report the larger die first), and if the fallback just used
    "d1 then d2" verbatim, two sources describing the identical physical
    move could disagree on which intermediate point this function reports
    -- a spurious cross-source mismatch with no board-level difference
    behind it.
    """
    dist = _span_distance(f, t)
    if dist == d1:
        return [(f, d1)]
    if dist == d2:
        return [(f, d2)]
    if dist == d1 + d2:
        mid1 = (25 - d1) if f == 25 else (f - d1)
        mid2 = (25 - d2) if f == 25 else (f - d2)
        order = (
            [(d1, d2, mid1), (d2, d1, mid2)] if d1 >= d2
            else [(d2, d1, mid2), (d1, d2, mid1)]
        )
        if hit_points:
            hit_order = [o for o in order if 1 <= o[2] <= 24 and o[2] in hit_points]
            if hit_order:
                order = hit_order
            elif board is not None:
                legal = [o for o in order if 1 <= o[2] <= 24 and board[o[2]] > -2]
                if legal:
                    order = legal
        elif board is not None:
            legal = [o for o in order if 1 <= o[2] <= 24 and board[o[2]] > -2]
            if legal:
                order = legal
        first_d, second_d, mid = order[0]
        return [(f, first_d), (mid, second_d)]
    return [(f, dist)] if dist > 0 else []  # best-effort fallback


def _mover_to_abs(point_mover: int, mover_is_white: bool) -> int:
    """Mover-numbering point (0-25, 25=own bar) -> OGXM absolute point
    (0=white bar, 25=black bar, 1-24 white's numbering) -- same formula
    ``ogid.py``'s ``_absolute_positions`` uses and validates."""
    return (25 - point_mover) if mover_is_white else point_mover


def _compute_move_steps(
    board_before_p1: list[int], board_after_p1: list[int],
    mover_is_white: bool, d1: int, d2: int,
) -> list[dict]:
    """Derive OGXM ``moves: [{from, pips}]`` from a P1/White-perspective
    board-before/after pair (authoritative: this is the played move)."""
    mb_before = board_before_p1 if mover_is_white else _flip_board(board_before_p1)
    mb_after = board_after_p1 if mover_is_white else _flip_board(board_after_p1)
    spans = _matched_spans(mb_before, mb_after, d1, d2)
    hit_points = _hit_points(mb_before, mb_after)  # mutated: each hit claimed once

    steps: list[dict] = []
    for f, t in spans:
        if d1 == d2:
            hops = _split_span_double(f, t, d1)
        else:
            hops = _split_span_nondouble(f, t, d1, d2, board=mb_before, hit_points=hit_points)
            if len(hops) == 2:
                hit_points.discard(hops[1][0])  # hops[1][0] is the claimed intermediate point
        for hf, pips in hops:
            steps.append({"from": _mover_to_abs(hf, mover_is_white), "pips": pips})
    return steps


def _parse_notation(notation: str) -> list[tuple[int, int]]:
    """Parse a display notation string ("13/10 8/5", "bar/20*", "6/off(2)")
    into (from, to) spans, mover numbering. Used only for *alternative*
    moves, which have no board array to diff against."""
    spans: list[tuple[int, int]] = []
    if not notation:
        return spans
    for raw in notation.split():
        token = raw.strip()
        if not token:
            continue
        count = 1
        if "(" in token and token.endswith(")"):
            base, cnt = token.rsplit("(", 1)
            token = base
            cnt = cnt[:-1]
            if cnt.isdigit():
                count = max(1, int(cnt))
        token = token.rstrip("*")
        if "/" not in token:
            continue
        f_s, t_s = token.split("/", 1)
        f_s, t_s = f_s.strip().lower(), t_s.strip().lower()
        try:
            f = 25 if f_s == "bar" else int(f_s)
            t = 0 if t_s == "off" else int(t_s)
        except ValueError:
            continue
        spans.extend([(f, t)] * count)
    return spans


def _notation_to_steps(
    notation: str, mover_is_white: bool, d1: int, d2: int,
    board: list[int] | None = None,
) -> list[dict]:
    """Steps for a move given only its notation string.

    ``board`` is the mover-perspective board *before* the move (own checkers
    positive, opponent's negative), and is what keeps a one-checker two-die
    span from being split through a point the opponent has made. Every caller
    that has one must pass it; only a caller with no board in hand may leave it
    None and accept the canonical larger-die-first tie-break.

    For the move that was actually *played* the split is not a cosmetic choice:
    an intermediate point holding two enemy checkers reads back as a hit, which
    invents checkers and corrupts every board replayed from that ply onward.
    For an unplayed *alternative* nothing is replayed, so the board survives --
    but the steps are what the display's move arrows are drawn from, and a
    route through a made point shows a checker landing on a stack of enemy
    checkers and moving on.
    """
    steps: list[dict] = []
    for f, t in _parse_notation(notation):
        hops = (_split_span_double(f, t, d1) if d1 == d2
                else _split_span_nondouble(f, t, d1, d2, board=board))
        for hf, pips in hops:
            steps.append({"from": _mover_to_abs(hf, mover_is_white), "pips": pips})
    return steps


def _steps_per_roll(d1: int, d2: int) -> int:
    """How many steps a ply record has room for: a ``.gvab`` ply carries
    exactly the hops the roll allows (see ``binary.DICE_TABLE``), and anything
    past that has nowhere to go."""
    return 4 if d1 == d2 else 2


def _notation_to_steps_unsplit(
    notation: str, mover_is_white: bool, d1: int, d2: int,
) -> list[dict]:
    """Steps for a move the dice cannot explain: one step per span.

    ``_notation_to_steps`` splits a span across the dice, which is right for a
    legal play and wrong for an *illegal* one -- a play that used more die-moves
    than the roll has (13/9 with a 3-1, then 12/11: three hops for a two-hop
    roll) expands past what a ply record can hold, and the overflow is dropped
    on write, leaving every board replayed after that ply one checker off.
    Recording each span at its own pip distance keeps the play to one step per
    checker moved, which replays to exactly the board the source recorded.

    A span longer than 6 pips still has to be split (``pips`` is 3 bits), so
    this is best-effort: callers must re-check the step count against
    ``_steps_per_roll`` and fall back to a set-position ply if it still
    overflows.
    """
    steps: list[dict] = []
    for f, t in _parse_notation(notation):
        dist = _span_distance(f, t)
        if 1 <= dist <= 6:
            steps.append({"from": _mover_to_abs(f, mover_is_white), "pips": dist})
            continue
        hops = (_split_span_double(f, t, d1) if d1 == d2
                else _split_span_nondouble(f, t, d1, d2))
        for hf, pips in hops:
            steps.append({"from": _mover_to_abs(hf, mover_is_white), "pips": pips})
    return steps


def _p1_to_absolute(board_p1: list[int]) -> list[int]:
    """P1/White mover-perspective frame -> the OGXM absolute frame a
    ``set_position`` ply carries (0=white bar, 25=black bar stored negative,
    point i mirrored). Inverse of ``reader._absolute_to_p1``."""
    board_abs = [0] * 26
    board_abs[0] = board_p1[25]       # white bar
    board_abs[25] = -board_p1[0]      # black bar, stored negative
    for i in range(1, 25):
        board_abs[25 - i] = board_p1[i]
    return board_abs


def fit_move_steps(
    steps: list[dict], notation: str | None,
    mover_is_white: bool, d1: int, d2: int,
) -> list[dict] | None:
    """Steps that fit a ply record, or ``None`` if no checker ply can hold them.

    One rule for all three converters. A ``.gvab`` checker ply carries exactly
    the hops the roll allows (``binary.DICE_TABLE``), and an *illegal* play can
    use more -- ``14/10`` off a 1-3 and then ``13/12``: three die-moves for a
    two-hop roll. Dropping the overflow writes a ply that replays to the wrong
    board, and the damage surfaces only plies later as an impossible position,
    so ``binary._encode_ply`` refuses it outright. This is what a converter has
    to call before handing steps over.

    Three rungs:

    1. the steps as built -- split across the dice, right for a legal play;
    2. one step per span (``_notation_to_steps_unsplit``), which is what an
       illegal play usually needs and usually fits;
    3. ``None`` -- too tangled for even one step per checker, so the caller
       must restate the ply with ``set_position_ply``.

    Rung 2 needs the notation. A converter working from a board *diff* has
    none, and must not collapse the steps itself: a step from 11 cannot be told
    apart from a checker already sitting on 11, so merging picks the wrong
    checker and silently changes the position.
    """
    room = _steps_per_roll(d1, d2)
    if len(steps) <= room:
        return steps
    if notation:
        unsplit = _notation_to_steps_unsplit(notation, mover_is_white, d1, d2)
        if len(unsplit) <= room:
            return unsplit
    return None


def set_position_ply(
    mover_is_white: bool, d1: int, d2: int, board_after_p1: list[int],
    ogid_before: str, ogid_after: str,
) -> dict:
    """An illegal play restated as the position it produced (action 31).

    The third rung of ``fit_move_steps``. No checker ply can carry the play and
    truncating it would corrupt every board after this one, so state the
    resulting position outright -- what action 31 is for (the spec notes its
    optional dice are exactly this case). The play itself is lost; it broke the
    rules, so there is no move to score.
    """
    return {
        "color": 1 if mover_is_white else 0,
        "action_id": 31,
        "d1": d1,
        "d2": d2,
        "set_position": _p1_to_absolute(board_after_p1),
        "ogid_before": ogid_before,
        "ogid_after": ogid_after,
    }


# ---------------------------------------------------------------------------
# OGID turn-phase state machine
#
# Port of ogxm_replay.cpp's replay_game(): the per-game state carried across
# a game's ply sequence, driving each ply's ogid_before/ogid_after
# game_state + cube fields. ``ogid.py``'s board_to_ogid already accepts
# absolute cube-owner characters ("N"/"W"/"B") straight through (see its
# ``_ABSOLUTE_CUBE_OWNERS``), so this state machine tracks cube ownership in
# that same absolute frame the reference uses (OGID_CUBE_WHITE/BLACK/
# CENTERED) rather than mover-relative keywords -- no perspective flip is
# needed when threading it into ``_ogid`` below.
# ---------------------------------------------------------------------------

class _TurnState:
    """Mutable per-game state mirroring ogxm_replay.cpp's replay_game()
    locals: cur_state, cube_action, cube_owner, cube_log2, move_id,
    awaiting_response, is_first_ply. One instance per game, reset at the
    start of every game (cube re-centers, dice haven't rolled yet)."""

    __slots__ = (
        "cur_state", "cube_owner", "cube_action", "cube_log2",
        "move_id", "awaiting_response", "is_first_ply",
    )

    def __init__(self) -> None:
        self.cur_state = _OGID_STATE_INITIAL_BOTH
        self.cube_owner = _OGID_CUBE_CENTERED
        self.cube_action = _OGID_ACTION_NONE
        self.cube_log2 = 0
        self.move_id = 0
        self.awaiting_response = False
        self.is_first_ply = True

    @property
    def cube_value(self) -> int:
        return 1 << self.cube_log2


# ---------------------------------------------------------------------------
# OGID helpers
# ---------------------------------------------------------------------------

def _ogid(
    board_p1: list[int], *, cube_value: int, cube_owner: str, cube_action: str,
    dice: tuple[int, int] | None, on_roll: str, game_state: str,
    score_white: int, score_black: int, match_length: int, crawford: bool,
    move_id: int = 0,
) -> str:
    return board_to_ogid(
        board_p1,
        mover_is_white=True,
        cube_value=cube_value,
        cube_owner=cube_owner,
        cube_action=cube_action,
        dice=dice,
        on_roll=on_roll,
        game_state=game_state,
        score_white=score_white,
        score_black=score_black,
        match_length=match_length,
        crawford=crawford,
        move_id=move_id,
    )


# ---------------------------------------------------------------------------
# Analysis-object builders
# ---------------------------------------------------------------------------

def _build_alternatives(move_options: list[dict], mover_is_white: bool, d1: int, d2: int,
                        board_before_mover: list[int] | None = None) -> list[dict]:
    """``board_before_mover`` is the pre-move board in the mover's own
    numbering, and every candidate shares it -- they all start from this ply's
    position. It is what stops a one-checker two-die alternative from being
    drawn through a point the opponent has made: the board arrows are built
    from these steps, so without it a rejected alternative is displayed landing
    on a stack of enemy checkers and moving on. Passing the *pre-move* board is
    sound even for a multi-checker candidate, because a play can only hit -- it
    never adds opponent checkers -- so a point open before the play cannot be
    blocked by it.
    """
    alts = []
    best_equity = move_options[0]["equity"] if move_options else None
    for opt in move_options:
        alt = {
            "move": _notation_to_steps(opt.get("move", ""), mover_is_white, d1, d2,
                                       board_before_mover),
            "notation": opt.get("move", ""),
            "equity": opt["equity"],
            "is_played": bool(opt.get("played", False)),
            "diff": round(opt["equity"] - best_equity, 4) if best_equity is not None else 0.0,
        }
        if "probs" in opt:
            alt["eval"] = _probs_to_eval(opt["probs"])
        lvl = _normalize_eval_level(opt.get("eval_level"))
        if lvl is not None:
            alt["eval_level"] = lvl
        alts.append(alt)
    return alts


def _cube_sub_analysis(cube: dict) -> tuple[str, dict]:
    """Build the (key, dict) pair for a checker ply's embedded cube info:
    ("missed_double", {...}) -- the should-have-doubled error -- or
    ("cube_decision", {...}) -- a correct/non-error live cube (no-double).

    Both mirror the maintainer's base OGXM shape. ``missed_double`` carries
    ``correct_action="double"``; ``cube_decision`` carries the boolean
    ``should_double`` + derived ``action`` label, plus this repo's ``decision``
    and ``eval_level`` extensions. Neither stores ``classification`` -- that is
    a reader-owned threshold policy over ``equity_loss`` (compute-on-read).
    """
    # An equity loss is floored at zero but has no ceiling here. The only bound
    # is the encoder's (binary.MAX_EQUITY_LOSS, 6.5535), which sits above the
    # worst loss backgammon can produce -- equity runs [-3, +3], so no decision
    # can cost more than 6.0. Capping at 1.0, as this did, silently rewrote
    # every blunder past a point of equity into a one-point one.
    equity_loss = max(0.0, cube.get("lost_equity", 0.0))
    no_double_equity = cube["equity_no_double"]
    double_take_equity = cube["equity_double_take"]
    double_pass_equity = cube["equity_double_pass"]
    if cube.get("optimal_action") == "double":
        # Player should have doubled but didn't: missed_double.
        md: dict = {
            "no_double_equity": no_double_equity,
            "double_take_equity": double_take_equity,
            "double_pass_equity": double_pass_equity,
            "equity_loss": round(equity_loss, 4),
            "correct_action": "double",
        }
        # The cube's own pre-roll probabilities, the same ones a cube_decision
        # carries. The analyzer computes them for every cube decision it looks
        # at; which way the decision came out is no reason to drop them.
        if "probs" in cube:
            md["eval"] = _probs_to_eval(cube["probs"])
        md_lvl = _normalize_eval_level(cube.get("eval_level"))
        if md_lvl is not None:
            md["eval_level"] = md_lvl
        return "missed_double", md
    should_double = cube.get("optimal_action") == "double"  # False in this branch
    sub: dict = {
        "should_double": should_double,
        "no_double_equity": no_double_equity,
        "double_take_equity": double_take_equity,
        "double_pass_equity": double_pass_equity,
        "action": _cube_action_label(should_double, double_take_equity, double_pass_equity),
        "equity_loss": round(equity_loss, 4),
        "decision": bool(cube.get("counted", False)),
    }
    if "probs" in cube:
        sub["eval"] = _probs_to_eval(cube["probs"])
    lvl = _normalize_eval_level(cube.get("eval_level"))
    if lvl is not None:
        sub["eval_level"] = lvl
    return "cube_decision", sub


def _checker_analysis(entry: dict, mover_is_white: bool, d1: int, d2: int,
                      base_ply: int = 0,
                      board_before_mover: list[int] | None = None) -> dict | None:
    move_options = entry.get("move_options") or []
    if not move_options:
        # No legal move (a dance / forced no-play): nothing to analyze about the
        # checker play, but two things can still ride on this ply. (1) The dice
        # were rolled, so a luck value was computed by the independent luck
        # analyzer -- keep it, or these (negatively-skewed) plies would bias luck
        # totals upward. (2) The player was on roll with a live cube and chose
        # not to double before rolling; that no-double cube decision (embedded
        # as `cube` by _collate_game) is a real decision the engine counts, so
        # it must appear in the OGXM too -- otherwise the stored analysis and the
        # engine's own tally disagree (they did for a no-double immediately
        # before a dance). Emit a minimal block with whichever is present.
        analysis: dict = {}
        luck = entry.get("luck")
        if luck is not None:
            analysis["luck"] = luck
        if "cube" in entry:
            key, sub = _cube_sub_analysis(entry["cube"])
            analysis[key] = sub
        return analysis or None

    alternatives = _build_alternatives(move_options, mover_is_white, d1, d2,
                                       board_before_mover)
    best_equity = move_options[0]["equity"]
    played_opt = next((o for o in move_options if o.get("played")), move_options[-1])
    played_equity = played_opt["equity"]

    has_analysis = "lost_equity" in entry
    if has_analysis:
        equity_loss = max(0.0, entry["lost_equity"])
        decision = bool(entry.get("counted", False))
    else:
        equity_loss = max(0.0, round(best_equity - played_equity, 4))
        decision = False  # forced move, or already-decided position: excluded from PR

    analysis: dict = {
        "eval": _probs_to_eval(move_options[0]["probs"]),
        "best_equity": best_equity,
        "played_equity": played_equity,
        "equity_loss": round(equity_loss, 4),
        "decision": decision,
        "alternatives": alternatives,
    }
    # Per-decision depth (base OGXM `ply`): the deepest level any alternative
    # was ultimately evaluated at (in two-pass analysis the top candidates run
    # deeper than the rest). Emit only when it exceeds the analysis-block base
    # ply; omission means "same as base". The per-alt `eval_level` strings stay
    # (they carry per-alternative depth *and* mode, which a plain int cannot).
    alt_depths = [_eval_depth(a["eval_level"]) for a in alternatives if a.get("eval_level")]
    decision_ply = max(alt_depths) if alt_depths else 0
    if decision_ply > base_ply:
        analysis["ply"] = decision_ply

    if "luck" in entry:
        # At the luck eval level (1-ply today), not the full eval level.
        analysis["luck"] = entry["luck"]

    if entry.get("illegal_move"):
        analysis["illegal_move"] = True

    if "cube" in entry:
        key, sub = _cube_sub_analysis(entry["cube"])
        analysis[key] = sub

    return analysis


def _cube_analysis_ply(analysis: dict, base_ply: int) -> None:
    """Set base OGXM's per-decision ``ply`` on a cube analysis, in place.

    ``CUBE.ply`` is the base format's own field for "analysis ply for this
    decision; 0 = use ANAL header ply", so a two-pass preset that upgrades a
    close cube from the screening depth belongs there — otherwise a reader with
    no GVAN concludes the decision was judged at the header depth. Emitted only
    when it exceeds the block's base ply, matching the checker path.

    Only the *depth* travels: a plain int cannot say "truncated" or "rollout",
    so GVAN's ``eval_level`` byte stays authoritative for the mode.
    """
    depth = _eval_depth(analysis.get("eval_level"))
    if depth > base_ply:
        analysis["ply"] = depth


def _cube_decision_analysis(entry: dict, base_ply: int = 0) -> dict | None:
    # An unanalyzed cube-decision entry (e.g. from the engine-free mat->OGXM
    # path) carries no analysis payload; there is nothing to emit.
    if "optimal_action" not in entry:
        return None
    equity_loss = max(0.0, entry.get("lost_equity", 0.0))
    analysis = {
        "correct_action": entry["optimal_action"],
        "played_action": entry["player_action"],
        "no_double_equity": entry["equity_no_double"],
        "double_take_equity": entry["equity_double_take"],
        "double_pass_equity": entry["equity_double_pass"],
        "equity_loss": round(equity_loss, 4),
        "decision": bool(entry.get("counted", False)),
    }
    if "probs" in entry:
        analysis["eval"] = _probs_to_eval(entry["probs"])
    lvl = _normalize_eval_level(entry.get("eval_level"))
    if lvl is not None:
        analysis["eval_level"] = lvl
    _cube_analysis_ply(analysis, base_ply)
    return analysis


def _cube_response_analysis(entry: dict, no_double_equity: float | None,
                            base_ply: int = 0) -> dict | None:
    # Unanalyzed take/pass entry (engine-free mat->OGXM path): no analysis.
    if "optimal_response" not in entry:
        return None
    equity_loss = max(0.0, entry.get("lost_equity", 0.0))
    analysis = {
        "correct_action": entry["optimal_response"],
        "played_action": entry["player_response"],
        "double_take_equity": entry["equity_take"],
        "double_pass_equity": entry["equity_pass"],
        "equity_loss": round(equity_loss, 4),
        "decision": bool(entry.get("counted", False)),
    }
    if no_double_equity is not None:
        analysis["no_double_equity"] = no_double_equity
    if "probs" in entry:
        analysis["eval"] = _probs_to_eval(entry["probs"])
    lvl = _normalize_eval_level(entry.get("eval_level"))
    if lvl is not None:
        analysis["eval_level"] = lvl
    _cube_analysis_ply(analysis, base_ply)
    return analysis


# ---------------------------------------------------------------------------
# Game-end ply
# ---------------------------------------------------------------------------

def _game_end_action_id(result_type: str, match_complete_here: bool) -> int:
    """Choose an OGXM game/match-end action_id for a GVA game result `type`.

    Documented choice (the task's action-id mapping is under-specified for
    the resign/forfeit/pass combination, so this is the resolution used):
      - "forfeit"                -> 29 (Force-forfeit)
      - "resign" / "time"        -> 28 (Resign match) if this game finishes
                                     the match, else 27 (Resign game)
      - "pass" (double + drop)   -> 26 (Final) -- a cube-drop ending is
                                     always treated as a terminal "Final"
                                     ply regardless of match completion,
                                     since (per the task brief) it is
                                     explicitly called out as its own case
      - "normal"/gammon/backgammon -> 26 (Final) if this game finishes the
                                     match, else 24 (Game over)
    """
    if result_type == "forfeit":
        return 29
    if result_type in ("resign", "time"):
        return 28 if match_complete_here else 27
    if result_type == "pass":
        return 26
    return 26 if match_complete_here else 24


# ---------------------------------------------------------------------------
# Top-level conversion
# ---------------------------------------------------------------------------

def _parse_timestamp(date: str | None, event_time: str | None) -> int:
    """Best-effort mat-header (`EventDate`/`EventTime`) -> unix seconds.
    Returns 0 if unparseable. Header format is normally "YYYY.MM.DD" /
    "HH:MM"; treated as UTC (the mat format carries no timezone)."""
    if not date:
        return 0
    try:
        d = date.replace(".", "-")
        t = (event_time or "00:00").replace(".", ":")
        dt = datetime.fromisoformat(f"{d}T{t}")
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


def _convert_checker_ply(entry: dict, player_white: str, score_white: int, score_black: int,
                          match_length: int, crawford: bool, board_before: list[int],
                          turn: _TurnState, base_ply: int = 0) -> dict:
    d1, d2 = entry["dice"]
    is_white = entry["player"] == player_white
    board_after = entry["board_after"]
    on_roll = "W" if is_white else "B"

    # Opening ply: before_state = INITIAL_BOTH. Any later checker ply:
    # before_state = ROLLED -- always, regardless of what cur_state carried
    # in (e.g. "A" fresh off a take), matching ogxm_replay.cpp's dice branch
    # (it ignores cur_state entirely and picks between just these two).
    before_state = _OGID_STATE_INITIAL_BOTH if turn.is_first_ply else _OGID_STATE_ROLLED

    ogid_before = _ogid(
        board_before, cube_value=turn.cube_value, cube_owner=turn.cube_owner,
        cube_action=turn.cube_action, dice=(d1, d2), on_roll=on_roll,
        game_state=before_state, score_white=score_white, score_black=score_black,
        match_length=match_length, crawford=crawford, move_id=turn.move_id,
    )

    turn.move_id += 1
    turn.is_first_ply = False
    turn.cur_state = _OGID_STATE_CHECKER_DONE
    turn.cube_action = _OGID_ACTION_NONE

    ogid_after = _ogid(
        board_after, cube_value=turn.cube_value, cube_owner=turn.cube_owner,
        cube_action=turn.cube_action, dice=None, on_roll=("B" if is_white else "W"),
        game_state=turn.cur_state, score_white=score_white, score_black=score_black,
        match_length=match_length, crawford=crawford, move_id=turn.move_id,
    )

    # This path derives steps from a board diff, so it has no notation of its
    # own -- the .mat converter threads the source's through on the entry (see
    # mat._decisions_to_moves) precisely so the middle rung is reachable here.
    steps = fit_move_steps(
        _compute_move_steps(board_before, board_after, is_white, d1, d2),
        entry.get("notation") or entry.get("player_move"), is_white, d1, d2,
    )
    if steps is None:
        return set_position_ply(is_white, d1, d2, board_after, ogid_before, ogid_after)

    ply = {
        "color": 1 if is_white else 0,
        "action_id": _dice_action_id(d1, d2),
        "d1": d1, "d2": d2,
        "moves": steps,
        "ogid_before": ogid_before,
        "ogid_after": ogid_after,
    }
    analysis = _checker_analysis(
        entry, is_white, d1, d2, base_ply,
        board_before if is_white else _flip_board(board_before),
    )
    if analysis is not None:
        ply["analysis"] = analysis
    return ply


def _convert_cube_decision_ply(entry: dict, player_white: str, score_white: int, score_black: int,
                                match_length: int, crawford: bool, turn: _TurnState,
                                base_ply: int = 0) -> dict:
    is_white = entry["player"] == player_white  # doubler
    board = entry["board"]
    on_roll = "W" if is_white else "B"

    # before_state carries whatever cur_state was left at by the prior ply
    # (typically "C" or "A"); cube_action likewise carries forward.
    ogid_before = _ogid(
        board, cube_value=turn.cube_value, cube_owner=turn.cube_owner,
        cube_action=turn.cube_action, dice=None, on_roll=on_roll,
        game_state=turn.cur_state, score_white=score_white, score_black=score_black,
        match_length=match_length, crawford=crawford, move_id=turn.move_id,
    )

    turn.awaiting_response = True
    turn.cur_state = _OGID_STATE_DOUBLE_OFFERED
    turn.cube_action = _OGID_ACTION_DOUBLE

    ogid_after = _ogid(
        board, cube_value=turn.cube_value, cube_owner=turn.cube_owner,
        cube_action=turn.cube_action, dice=None, on_roll=("B" if is_white else "W"),
        game_state=turn.cur_state, score_white=score_white, score_black=score_black,
        match_length=match_length, crawford=crawford, move_id=turn.move_id,
    )

    ply = {
        "color": 1 if is_white else 0,
        "action_id": 21,
        "ogid_before": ogid_before,
        "ogid_after": ogid_after,
    }
    analysis = _cube_decision_analysis(entry, base_ply)
    if analysis is not None:
        ply["analysis"] = analysis
    return ply


def _convert_cube_response_ply(entry: dict, player_white: str, score_white: int, score_black: int,
                                match_length: int, crawford: bool,
                                no_double_equity: float | None, turn: _TurnState,
                                base_ply: int = 0) -> dict:
    is_white = entry["player"] == player_white  # entry["player"] = responder
    board = entry["board"]
    on_roll = "W" if is_white else "B"

    # A take/drop is only ever legal in response to a standing double offer,
    # so before_state/cube_action are hard-coded DOUBLE_OFFERED/OFFER here
    # (matching ogxm_replay.cpp's take/drop branch), not read off turn.cur_state.
    ogid_before = _ogid(
        board, cube_value=turn.cube_value, cube_owner=turn.cube_owner,
        cube_action=_OGID_ACTION_DOUBLE, dice=None, on_roll=on_roll,
        game_state=_OGID_STATE_DOUBLE_OFFERED, score_white=score_white,
        score_black=score_black, match_length=match_length, crawford=crawford,
        move_id=turn.move_id,
    )

    took = entry["player_response"] == "take"
    turn.awaiting_response = False
    if took:
        turn.cube_log2 += 1
        turn.cube_owner = _OGID_CUBE_WHITE if is_white else _OGID_CUBE_BLACK
        turn.cur_state = _OGID_STATE_AFTER_TAKE
        turn.cube_action = _OGID_ACTION_TAKE
    else:
        turn.cur_state = _OGID_STATE_GAME_OVER
        turn.cube_action = _OGID_ACTION_PASS

    ogid_after = _ogid(
        board, cube_value=turn.cube_value, cube_owner=turn.cube_owner,
        cube_action=turn.cube_action, dice=None, on_roll=("B" if is_white else "W"),
        game_state=turn.cur_state, score_white=score_white, score_black=score_black,
        match_length=match_length, crawford=crawford, move_id=turn.move_id,
    )

    action_id = 22 if took else 23
    ply = {
        "color": 1 if is_white else 0,
        "action_id": action_id,
        "ogid_before": ogid_before,
        "ogid_after": ogid_after,
    }
    analysis = _cube_response_analysis(entry, no_double_equity, base_ply)
    if analysis is not None:
        ply["analysis"] = analysis
    return ply


def _convert_game(game: dict, player_white: str, player_black: str, match_length: int,
                  base_ply: int = 0) -> dict:
    score_white = game["score_start"]["player1"]
    score_black = game["score_start"]["player2"]
    is_crawford = bool(game.get("is_crawford", False))

    plies: list[dict] = []
    board = list(_STARTING_BOARD_P1)
    pending_nd_equity: float | None = None
    turn = _TurnState()

    for entry in game["moves"]:
        kind = entry["kind"]
        if kind == "checker":
            board_before = entry["board_before"]
            ply = _convert_checker_ply(
                entry, player_white, score_white, score_black, match_length, is_crawford,
                board_before, turn, base_ply,
            )
            plies.append(ply)
            board = entry["board_after"]
        elif kind == "cube_decision":
            ply = _convert_cube_decision_ply(
                entry, player_white, score_white, score_black, match_length, is_crawford, turn,
                base_ply,
            )
            plies.append(ply)
            board = entry["board"]
            pending_nd_equity = entry.get("equity_no_double")
        elif kind == "cube_response":
            ply = _convert_cube_response_ply(
                entry, player_white, score_white, score_black, match_length, is_crawford,
                pending_nd_equity, turn, base_ply,
            )
            plies.append(ply)
            board = entry["board"]
            pending_nd_equity = None

    result = game.get("result") or {}
    winner_name = result.get("winner")
    result_type = result.get("type", "normal")
    points = result.get("points")
    if points is None:
        points = 0

    winner_is_white = winner_name == player_white
    # NOTE: `points` stays the game's full value (a 4-point gammon is 4), which
    # is what the source file recorded and what the win type is recovered from
    # downstream (reconstruct_mat reads points/cube to name a gammon). The match
    # length caps it only where a running score is *accumulated* -- see
    # cap_points_won's callers -- which is also what the reference codec does:
    # its writer echoes the value and match_score.hpp caps on the way in.
    match_complete_here = bool(
        match_length and (
            (winner_is_white and score_white + points >= match_length)
            or (not winner_is_white and score_black + points >= match_length)
        )
    )

    if winner_name is not None:
        action_id = _game_end_action_id(result_type, match_complete_here)
        # A terminal ply names the winner: nobody *does* a game-over, and
        # ogxm_replay.cpp just carries the winner through. Resignation is the
        # one exception -- it is an act, and the player who resigns is the one
        # who lost -- so 27/28 gets the resigner, both as the ply's color
        # (`color` is documented as the player a ply belongs to) and as the
        # player on roll (the resigner is the one facing the roll they chose
        # not to take).
        actor_is_white = (
            not winner_is_white if action_id in RESIGN_ACTIONS else winner_is_white
        )
        on_roll = "W" if actor_is_white else "B"
        # ogxm_replay.cpp's replay_game() never specializes game_state or
        # cube_action for a terminal action_id (24/25/26/27/28/29): they all
        # fall through its catch-all `else` branch, which just carries
        # `cur_state`/`cube_action` forward unchanged from whatever the last
        # real ply left them at. Mirror that here instead of inventing a
        # dedicated end-of-game state.
        end_state = turn.cur_state
        end_cube_action = turn.cube_action
        # Reflects the game's actual final cube state (tracked by `turn`
        # across the ply loop above), not a hardcoded centered-cube-at-1 --
        # a game that ends after one or more doubles should show the cube
        # where it actually stood, e.g. "W1N" after a single take.
        end_kwargs = dict(
            cube_value=turn.cube_value, cube_owner=turn.cube_owner,
            cube_action=end_cube_action, dice=None,
            game_state=end_state, score_white=score_white, score_black=score_black,
            match_length=match_length, crawford=is_crawford, move_id=turn.move_id,
        )
        # Like every real ply, ogid_after flips the color field to the opponent
        # (the reference computes ogid_after with next_color = 1 - ply.color and
        # never emits identical before/after, even for a terminal marker ply).
        end_ply = {
            "color": 1 if actor_is_white else 0,
            "action_id": action_id,
            "ogid_before": _ogid(board, on_roll=on_roll, **end_kwargs),
            "ogid_after": _ogid(
                board, on_roll=("B" if actor_is_white else "W"), **end_kwargs),
        }
        plies.append(end_ply)
        winner_code = 0 if winner_is_white else 1
    else:
        winner_code = 255

    game_obj = {
        "game_index": game["game_number"] - 1,
        "winner": winner_code,
        "points_won": points,
        "is_crawford": is_crawford,
        "plies": plies,
    }
    return game_obj


def to_ogxm_json(result: dict) -> dict:
    """Convert an ``analyze_mat(...)``-shaped result dict into OGXM-JSON.

    Pure function: performs no bgsage/engine calls. ``result`` must have the
    GVA shape produced by ``gvan_match.analyze_mat`` (top-level ``summary``
    + ``games`` with each game's ``moves`` list; see ``game_eval.py`` for
    the exact per-entry shapes this function reads).
    """
    summary = result["summary"]
    player_white, player_black, p1_is_white = _canonical_orientation(
        summary["player1"], summary["player2"],
    )
    match_length = summary.get("match_length") or 0

    eval_level = _normalize_eval_level(summary.get("eval_level")) or "2ply"
    # analysis_info.ply is the *base* (first-pass / screen) depth. In a two-pass
    # scheme individual decisions may be evaluated deeper than this base (they
    # then carry their own analysis.ply); the authoritative deepest level is
    # carried by the mode-aware eval_level string, not this integer.
    first_pass_level = _normalize_eval_level(summary.get("first_pass_level"))
    base_ply = _eval_depth(first_pass_level or eval_level)

    games_in = result["games"]
    if not p1_is_white:
        # Canonical white is the source's player-2: flip every board (and
        # score_start) up front so the converter body below -- which builds
        # everything from "player-1/summary's original player1 == white" --
        # produces output in canonical white's frame unchanged.
        games_in = [_flip_game_orientation(g) for g in games_in]
    games_out = [
        _convert_game(g, player_white, player_black, match_length, base_ply)
        for g in games_in
    ]

    score_white = games_in[-1]["score_start"]["player1"] if games_in else 0
    score_black = games_in[-1]["score_start"]["player2"] if games_in else 0
    if games_in:
        last_result = games_in[-1].get("result") or {}
        winner_name = last_result.get("winner")
        points = last_result.get("points") or 0
        if winner_name == player_white:
            score_white += cap_points_won(points, score_white, match_length)
        elif winner_name == player_black:
            score_black += cap_points_won(points, score_black, match_length)

    if match_length and score_white >= match_length:
        match_result = 1
    elif match_length and score_black >= match_length:
        match_result = 2
    else:
        match_result = 0

    luck_eval_level = _normalize_eval_level(summary.get("luck_eval_level"))

    # Prefer a precomputed unix ``timestamp`` when the summary carries one (the
    # OGXM-sourced path already has unix seconds, not the mat header's date/time
    # strings); otherwise parse the mat header. Back-compatible: mat callers
    # pass ``date``/``event_time`` and no ``timestamp`` key, so behavior is
    # unchanged for them.
    ts = summary.get("timestamp")
    if ts is None:
        ts = _parse_timestamp(summary.get("date"), summary.get("event_time"))

    ogxm: dict = {
        "match_length": match_length,
        "player_white": player_white,
        "player_black": player_black,
        "white_score": score_white,
        "black_score": score_black,
        "result": match_result,
        "source": 1,  # mat_import
        "timestamp": ts,
        "crawford": bool(summary.get("crawford_rule")),
        "jacoby": bool(summary.get("jacoby_rule")),
        "beaver": bool(summary.get("beaver_rule")),
        "cube_limit": summary.get("cube_limit") or 0,
        "event": clean_place(summary.get("event")),
        "site": clean_place(summary.get("site")),
    }
    # Emit analysis_info only when the match actually carries analysis. The
    # engine-free mat->OGXM path produces plies with no `analysis` object, so a
    # base OGXM stays analysis-free (no dangling metadata for a run that never
    # happened); the analyzed path is unaffected -- its plies all carry
    # analysis, so this is present exactly as before.
    has_analysis = any(
        ply.get("analysis") is not None
        for g in games_out for ply in g["plies"]
    )
    if has_analysis:
        ogxm["analysis_info"] = {
            "ply": base_ply,
            "eval_level": eval_level,
            **({"luck_eval_level": luck_eval_level} if luck_eval_level is not None else {}),
            "preset": summary.get("preset"),
            # The producer, supplied by whoever ran the analysis --
            # ``gvanalysis.match.model_id()`` yields "gv-bgsage/<engine
            # version>". This package is engine-free and cannot ask
            # bgsage its version, so the label arrives in the summary;
            # the bare fallback keeps an older caller's output valid.
            "model_id": summary.get("engine") or "bgsage",
            "timestamp": ts,
        }
    ogxm["games"] = games_out
    return ogxm
