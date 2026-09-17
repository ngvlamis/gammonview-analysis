# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Core per-decision and per-game evaluation logic for match PR analysis."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
for _p in (_PROJECT_ROOT / "python", _PROJECT_ROOT / "build"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

if sys.platform == "win32":
    import os as _os
    _cuda_x64 = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.1\bin\x64"
    if _os.path.isdir(_cuda_x64):
        _os.add_dll_directory(_cuda_x64)
    if (_PROJECT_ROOT / "build").is_dir():
        _os.add_dll_directory(str(_PROJECT_ROOT / "build"))

from bgsage import BgBotAnalyzer, STARTING_BOARD
from bgsage.board import flip_board, possible_moves
from bgsage.text_export import compute_move_notation

from gvformat.legality import board_problems
from gvformat.met import eq2mwc, mwc2eq, score_mwc

from .checker_eval import checker_play as _checker_play


def _probs_list(p) -> list[float]:
    return [
        round(float(p.win), 3),
        round(float(p.gammon_win), 3),
        round(float(p.backgammon_win), 3),
        round(float(p.gammon_loss), 3),
        round(float(p.backgammon_loss), 3),
    ]


def _eq2mwc(equity: float, away1: int, away2: int, cube_value: int, is_crawford: bool) -> float | None:
    """Convert cubeful equity to match winning probability. Returns None for money games."""
    if away1 <= 0 or away2 <= 0:
        return None
    return round(eq2mwc(float(equity), int(away1), int(away2), int(cube_value), bool(is_crawford)), 4)


# ---------------------------------------------------------------------------
# Decision filters (XG-compatible)
# ---------------------------------------------------------------------------

# Minimum equity spread across a ply's top ten moves for the checker play to
# count as a PR decision (and to be worth analyzing at the authoritative level).
# Mirrors gvformat.xg._CHECKER_SPREAD_EPS: XG has no triviality threshold for
# checker plays, it only drops plies whose candidates are indistinguishable at
# its equity display precision -- in practice already-decided positions, where
# every legal move scores the same. XG stores at most ten candidates, so its
# spread is measured over the top ten; `best - tenth` below is the same window.
#
# Identified against XG's displayed PR on six player-sides (see xg.py for the
# derivation). The former 0.001 was an order of magnitude too aggressive and ran
# PR ~2% high by under-counting the denominator.
_CHECKER_SPREAD_EPS = 1e-4

# EXPERIMENT (GVAN_MID_ESCALATE=1): let a borderline checker play that turns out
# to be a real error escalate from the middle tier to the rollout, the way the
# cube path already does with `real_error`. Off by default so the goldens and
# the shipping presets are unchanged while this is measured.
_MID_ESCALATE = os.environ.get("GVAN_MID_ESCALATE") == "1"


def _trivial_cube(nd: float, dt: float, dp: float) -> bool:
    return (
        abs(nd - min(dt, dp)) < 0.001
        or (nd - dt) > 0.200
        or (nd - dp) > 0.200
        or (nd < -0.900 and dt < -0.900)
    )


def _trivial_take_pass(dt: float, dp: float) -> bool:
    return abs(dt - dp) < 0.001


# ---------------------------------------------------------------------------
# Sage evaluation
# ---------------------------------------------------------------------------

_DICE_PAIRS: list[tuple[int, int, int]] = [
    (d1, d2, 1 if d1 == d2 else 2)
    for d1 in range(1, 7)
    for d2 in range(d1, 7)
]  # (die1, die2, weight_out_of_36); all 21 distinct rolls


def _opening_baseline(
    away1: int, away2: int, cube_value: int, is_crawford: bool
) -> float:
    """Equity the opening roll is measured against: the pre-game MWC, in this
    ply's equity frame.

    Every other ply averages over the 21 rolls, because the state before the
    dice is "I am on roll" -- a position the average summarizes. A game's first
    ply has no such state: before the opening roll nobody is on roll yet, and the
    single dice event bundles *who* plays with *what* they play. Averaging over
    the rolls would condition on having already won the roll-off and throw that
    half away. So the reference is the score itself, straight off the MET.

    The two mirror roll-offs (I show the 5, you show the 5) are equally likely
    and equal and opposite, so this is zero-mean per player *across* games -- but
    only in a two-sided ledger. Luck is credited to the roller alone, here as
    everywhere, so the negative half is never written down and the roller banks
    the roll-off advantage (~+0.05, score-dependent in equity units). That is the
    field convention, not an artifact: BGBlitz's own per-ply luck over 54 sample
    games averages +0.066 on the first roll against -0.0006 across all plies.

    EMG does not absorb the score here, which is the trap. Equity 0 is the
    midpoint of *this game's* win/loss anchors, not the current MWC; the two
    coincide only when the score is symmetric. Hence `mwc2eq` rather than 0.0 --
    though when away1 == away2 the anchors are complementary and this returns
    exactly 0.0, which is what the code did before for that case. BGBlitz avoids
    the whole question by computing luck as an MWC delta and converting once
    (gvformat/bgf.py `luckPlain` / `luckWeighted`); doing the same throughout
    would also fix the residual asymmetry that the mover's and opponent's equity
    scales differ at a lopsided score.

    Costs no engine calls, where the 21-roll sweep it replaces cost 21.
    """
    return mwc2eq(
        score_mwc(away1, away2, is_crawford),
        away1, away2, cube_value, is_crawford,
    )


def _compute_luck(
    board: list[int], die1: int, die2: int,
    cube_value: int, cube_owner: str,
    away1: int, away2: int, is_crawford: bool,
    analyzer: "BgBotAnalyzer",
    verbose: bool = False,
    postroll_eq: float | None = None,
    hint_eq: float | None = None,
) -> tuple[float, float] | tuple[None, None]:
    """Return (luck, preroll_equity).

    Pre-roll equity: 1-ply probability-weighted average over all 21 dice pairs.
    Post-roll equity: `postroll_eq` when supplied (full-analysis result already
    computed elsewhere); otherwise the 1-ply equity for the actual dice.

    The 21-call sweep is deliberate. bgsage exposes the same metric as
    `roll_luck()` over `cube_action(incl_2ply_details=True).details["nd"]`, which
    is free *if you already hold a cube analysis of this board* -- but our cube
    decisions are separate units of work (dispatched to separate processes when
    jobs > 1), so luck cannot borrow one. Measured standalone, a dedicated 2-ply
    cube_action costs ~75% more than these 21 1-ply calls. `batch_checker_play`
    would be the natural batched replacement but rejects the 19-NN production
    model ("requires exactly 17 weight paths").

    `hint_eq` is the best available equity for the actual roll (best_eq for
    analyzed moves, forced-move equity for forced moves). It is used in two ways:
    (1) As a fallback equity for dice pairs that return an empty moves list in the
    21-roll loop — this fixes near-terminal bearoff positions where bgsage 1-ply
    cannot evaluate the resulting sparse board (e.g. 1 checker on point 1) and
    returns empty instead of the correct ~1.0 equity.
    (2) To detect 1-ply sign errors on game-ending boards: when |final_postroll
    − hint_eq| > 1.5 we skip the move entirely (return None).

    The opening roll takes a different baseline entirely -- see
    `_opening_baseline`. Rolling the 21 dice pairs answers "how good was this
    roll, given I am the one on roll", which silently discards the roll-off that
    decided *who* is on roll. Positive luck means the actual dice were above
    average for the mover.
    """
    if list(board) == list(STARTING_BOARD):
        preroll = _opening_baseline(away1, away2, cube_value, is_crawford)
        if postroll_eq is None:
            try:
                result = analyzer.checker_play(
                    board, die1, die2,
                    cube_value=cube_value, cube_owner=cube_owner,
                    away1=away1, away2=away2, is_crawford=is_crawford,
                )
                moves = list(result.moves)
                postroll_eq = float(moves[0].equity) if moves else 0.0
            except Exception as e:
                if verbose:
                    print(f"  _compute_luck error (starting board, dice {die1}{die2}): {e}")
                return None, None
        return round(postroll_eq - preroll, 4), round(preroll, 4)

    actual_key = (min(die1, die2), max(die1, die2))
    total = 0.0
    one_ply_postroll: float | None = None
    for d1, d2, w in _DICE_PAIRS:
        try:
            result = analyzer.checker_play(
                board, d1, d2,
                cube_value=cube_value, cube_owner=cube_owner,
                away1=away1, away2=away2, is_crawford=is_crawford,
            )
            moves = list(result.moves)
        except Exception as e:
            if verbose:
                print(f"  _compute_luck error (dice {d1}{d2}): {e}")
            return None, None
        eq = float(moves[0].equity) if moves else (hint_eq if hint_eq is not None else 0.0)
        # bgsage 1-ply sign-flips a *terminal win* (mover fully borne off) to -1.0.
        # Correct such a roll to the real win value (hint_eq) — but ONLY when the
        # resulting board is actually a terminal win. A genuine loss for the other
        # dice also evaluates to -1.0 with the same degenerate all-zero probs, and
        # must be left alone: promoting those to a win too would collapse the whole
        # 21-roll average toward the postroll value and zero out the luck.
        if (eq < -0.9 and hint_eq is not None and hint_eq > 0.5
                and moves and max(moves[0].board) <= 0):
            eq = hint_eq
        total += w * eq
        if (d1, d2) == actual_key:
            one_ply_postroll = eq
    preroll_eq = total / 36.0
    final_postroll = postroll_eq if postroll_eq is not None else one_ply_postroll
    if final_postroll is None:
        return None, None
    # Detect 1-ply sign error on game-ending boards: bgsage 1-ply returns -1.0 for
    # a terminal win (opponent's-perspective equity leaking through). The discrepancy
    # vs hint_eq is > 1.5, which is not reachable in normal evaluation disagreements.
    if hint_eq is not None and abs(final_postroll - hint_eq) > 1.5:
        return None, None
    return round(final_postroll - preroll_eq, 4), round(preroll_eq, 4)


_LEVEL_DISPLAY = {
    "1ply": "1ply", "2ply": "2ply", "3ply": "3ply", "4ply": "4ply",
    "truncated1": "1T", "truncated2": "2T", "truncated3": "3T",
}


@dataclass
class _EvalCtx:
    """Read-only inputs shared by the per-decision evaluators."""
    analyzer: "BgBotAnalyzer"
    base_analyzer: "BgBotAnalyzer | None"
    # Middle tier, one analyzer per decision kind (a preset may give a cube a
    # mid tier and leave checker plays 2-tier, or vice versa); None => that
    # kind has no middle tier. Both may be the SAME object when the preset
    # names one level for both -- built once by the caller.
    mid_analyzer_checker: "BgBotAnalyzer | None"
    mid_analyzer_cube: "BgBotAnalyzer | None"
    luck_analyzer: "BgBotAnalyzer | None"
    close_threshold: float | None
    verbose: bool
    all_moves: bool
    count_illegal: bool
    level_display: str
    game: dict

    def eval_label(self, raw: str) -> str:
        return self.level_display if raw == "Rollout" else raw


@dataclass
class _GameTally:
    """Mutable accumulators built up across one game's decisions."""
    p1_err: float = 0.0
    p1_dec: int = 0
    p1_cube_dec: int = 0
    p1_luck: float = 0.0
    p1_luck_mwc: float = 0.0
    p1_luck_count: int = 0
    p2_err: float = 0.0
    p2_dec: int = 0
    p2_cube_dec: int = 0
    p2_luck: float = 0.0
    p2_luck_mwc: float = 0.0
    p2_luck_count: int = 0
    illegal_moves: int = 0
    moves_log: list = field(default_factory=list)
    move_num: int = 0
    pending_cube: "dict | None" = None


@dataclass
class _DecResult:
    """Self-contained output of evaluating ONE decision (cube or checker).

    Entries carry no "move_number" (assigned during collation, which is the
    only place that knows cross-decision order) and a checker entry carries no
    pre-attached "cube" key (collation attaches a carried-forward no-double
    cube payload, since that too is cross-decision state).
    """
    entries: list = field(default_factory=list)
    p1_err: float = 0.0
    p1_dec: int = 0
    p1_cube_dec: int = 0
    p1_luck: float = 0.0
    p1_luck_mwc: float = 0.0
    p1_luck_count: int = 0
    p2_err: float = 0.0
    p2_dec: int = 0
    p2_cube_dec: int = 0
    p2_luck: float = 0.0
    p2_luck_mwc: float = 0.0
    p2_luck_count: int = 0
    illegal_moves: int = 0
    pending_cube: "dict | None" = None


def _board_p1(board: list[int], mover_is_p1: bool) -> list[int]:
    """Return `board` (stored in the mover's perspective) in a fixed Player-1
    (White) perspective, so downstream OGXM export never has to reason about
    per-ply perspective. Flips when the mover is Player 2."""
    return list(board) if mover_is_p1 else flip_board(board)


def _require_playable(dec: dict, ctx: _EvalCtx) -> None:
    """Raise if this decision's board could not be a real position.

    bgsage is C++ and trusts its input: an impossible board indexes off the end
    of a bearoff table and takes the whole worker process down with it -- a
    segfault the caller sees only as "a process in the process pool was
    terminated abruptly", with no clue which ply caused it. So check first and
    say what is wrong.

    Failing the whole run rather than skipping the one decision is deliberate.
    A board like this means the replay that produced it already went wrong --
    most often a play whose steps could not all be stored, after which every
    ply lifted checkers off the wrong points -- so the rest of that game sits
    on a fiction too, and the boards that are merely *wrong* rather than
    impossible are the ones this cannot detect. A match that analyzes with a
    few plies quietly missing would hide that; an error names the ply and
    points at the file.

    Both boards are checked, because either reaches the engine: the position
    faced goes to `checker_play`/`cube_action`, and the position played goes to
    `post_move_analytics` (and rides along as a `force_boards` entry). A
    truncated play shows up in the *played* board first -- the one the mover
    faced still replays clean.
    """
    problems = board_problems(dec["board"])
    played = dec.get("board_played")
    if played is not None:
        problems += board_problems(played)
    if not problems:
        return
    dice = dec.get("dice")
    where = f"game {ctx.game['game_number']}"
    if dec.get("player"):
        where += f", {dec['player']}"
    if dice and dice[0]:
        where += f"'s {dice[0]}-{dice[1]}"
    raise ValueError(
        f"{where}: this ply replays to a position that cannot exist "
        f"({'; '.join(problems)}). The match file's moves are corrupt -- "
        f"re-import it from the original file."
    )


def _eval_cube_decision(dec: dict, ctx: _EvalCtx) -> _DecResult:
    _require_playable(dec, ctx)
    res = _DecResult()
    # Single cube_action() call covers both doubler and responder.
    try:
        def cube_at(analyzer):
            return analyzer.cube_action(
                dec["board"],
                cube_value=dec["cube_value"],
                cube_owner=dec["cube_owner"],
                away1=dec["away1"],
                away2=dec["away2"],
                is_crawford=dec["is_crawford"],
            )

        cheap = ctx.base_analyzer if ctx.base_analyzer is not None else ctx.analyzer
        cheap_result = cube_at(cheap)
        nd_b = float(cheap_result.equity_nd)
        dt_b = float(cheap_result.equity_dt)
        dp_b = float(cheap_result.equity_dp)
        cube_upgraded = False
        cube_probs = None
        if ctx.base_analyzer is not None:
            # Which way each side went at the screening ply, and by how much:
            # the doubler's margin over its double point, the responder's over
            # its take point. A side that disagrees with what was actually done
            # is an error costing that side's margin.
            opt_b = "double" if min(dt_b, dp_b) > nd_b else "no_double"
            played_cube = "double" if dec["doubled"] else "no_double"
            doubler_gap = abs(nd_b - min(dt_b, dp_b))
            doubler_wrong = played_cube != opt_b

            has_resp = dec["doubled"] and dec["response"] is not None
            resp_opt_b = "take" if dt_b <= dp_b else "pass"
            resp_gap = abs(dt_b - dp_b)
            resp_wrong = has_resp and dec["response"] != resp_opt_b

            # 3-tier presets only: `thr` is the margin under which a side counts
            # as borderline. None => plain 2-tier, where nothing is marginal and
            # every disagreement is a real error.
            thr = None
            if ctx.mid_analyzer_cube is not None and ctx.close_threshold is not None:
                thr = ctx.close_threshold

            def marginal(gap: float) -> bool:
                return thr is not None and gap <= thr

            # A real error -- a side played wrong by MORE than the borderline
            # margin -- is what the authoritative pass exists to size. Escalate
            # on that alone, never on every non-trivial cube: this matches XG
            # World Class, which judges cube decisions at the screening ply and
            # only rolls out (XGRoller+) on a cube error.
            real_error = ((doubler_wrong and not marginal(doubler_gap))
                          or (resp_wrong and not marginal(resp_gap)))

            # Failing that, deepen a BORDERLINE cube to the middle tier. Within
            # a hair of the double or take point the screen's verdict is least
            # trustworthy, but the equity riding on it is bounded by that same
            # hair -- so depth is what's wanted and a rollout would be waste.
            # Same shape as the near-tied checker play below, and as there, a
            # side too trivial to score (the _trivial_cube / _trivial_take_pass
            # rules the PR count uses) cannot earn the upgrade.
            close = (
                (marginal(doubler_gap) and not _trivial_cube(nd_b, dt_b, dp_b))
                or (has_resp and marginal(resp_gap)
                    and not _trivial_take_pass(dt_b, dp_b))
            )

            if real_error:
                full_result = cube_at(ctx.analyzer)
                cube_upgraded = True
            elif close:
                full_result = cube_at(ctx.mid_analyzer_cube)
                cube_upgraded = True
            else:
                full_result = cheap_result
            nd = float(full_result.equity_nd)
            dt = float(full_result.equity_dt)
            dp = float(full_result.equity_dp)
        else:
            nd, dt, dp = nd_b, dt_b, dp_b
            full_result = cheap_result
        cube_probs = full_result.probs
    except Exception as e:
        if ctx.verbose:
            print(f"  cube_action error: {e}")
        return res

    optimal = max(nd, min(dt, dp))
    doubled = dec["doubled"]

    # Doubler error
    actual_d = min(dt, dp) if doubled else nd
    doubler_err = max(0.0, optimal - actual_d)
    trivial = _trivial_cube(nd, dt, dp)
    doubler_counts = not (trivial and doubler_err < 0.001)

    if doubler_counts:
        if dec["is_doubler_p1"]:
            res.p1_err += doubler_err
            res.p1_dec += 1
            res.p1_cube_dec += 1
        else:
            res.p2_err += doubler_err
            res.p2_dec += 1
            res.p2_cube_dec += 1

    optimal_action = "double" if min(dt, dp) > nd else "no_double"
    cube_dec_entry: dict = {
        "cube_value": dec["cube_value"],
        "equity_no_double": round(nd, 4),
        "equity_double_take": round(dt, 4),
        "equity_double_pass": round(dp, 4),
        "optimal_action": optimal_action,
        "player_action": "double" if doubled else "no_double",
        "lost_equity": round(doubler_err, 4),
        "upgraded": cube_upgraded,
        "counted": doubler_counts,
        "eval_level": ctx.eval_label(full_result.eval_level),
        # Board at the cube decision (P1/White perspective). Cube actions do not
        # move checkers, so before == after for the OGXM ply.
        "board": _board_p1(dec["board"], dec["is_doubler_p1"]),
    }
    if cube_probs is not None:
        cube_dec_entry["probs"] = _probs_list(cube_probs)

    mwc_nd = _eq2mwc(nd, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
    mwc_dt = _eq2mwc(dt, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
    mwc_dp = _eq2mwc(dp, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
    if mwc_nd is not None:
        cube_dec_entry["mwc_no_double"] = mwc_nd
    if mwc_dt is not None:
        cube_dec_entry["mwc_double_take"] = mwc_dt
    if mwc_dp is not None:
        cube_dec_entry["mwc_double_pass"] = mwc_dp
    opt_mwc = _eq2mwc(optimal, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
    act_mwc = _eq2mwc(actual_d, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
    if opt_mwc is not None and act_mwc is not None:
        cube_dec_entry["lost_mwc"] = round(opt_mwc - act_mwc, 4)

    if doubled:
        # Actual double: emit as its own move entry
        res.entries.append({
            "player": dec["doubler"],
            "kind": "cube_decision",
            **cube_dec_entry,
        })
    else:
        # No-double: carry cube data forward to embed in next checker entry
        res.pending_cube = {"player": dec["doubler"], **cube_dec_entry}

    # Responder error (only on double turns with a response)
    if doubled and dec["response"] is not None:
        resp_counts = not _trivial_take_pass(dt, dp)
        resp_err_raw = 0.0
        if resp_counts:
            optimal_r = min(dt, dp)
            actual_r = dt if dec["response"] == "take" else dp
            resp_err_raw = max(0.0, actual_r - optimal_r)
            # Responder is !is_doubler_p1
            if dec["is_doubler_p1"]:
                res.p2_err += resp_err_raw
                res.p2_dec += 1
                res.p2_cube_dec += 1
            else:
                res.p1_err += resp_err_raw
                res.p1_dec += 1
                res.p1_cube_dec += 1

        cube_resp_entry: dict = {
            "player": dec["responder"],
            "kind": "cube_response",
            "cube_value": dec["cube_value"] * 2,
            "equity_take": round(dt, 4),
            "equity_pass": round(dp, 4),
            "optimal_response": "take" if dt <= dp else "pass",
            "player_response": dec["response"],
            "lost_equity": round(resp_err_raw, 4),
            "upgraded": cube_upgraded,
            "counted": resp_counts,
            # Same evaluation as the double decision beside it (both read
            # `full_result`), so it carries the same level. Recorded because
            # base OGXM's CUBE.ply wants the depth this decision was judged at;
            # without it an upgraded take/pass reads as the header depth.
            "eval_level": ctx.eval_label(full_result.eval_level),
            # Board being decided on (P1/White perspective); take/pass move no
            # checkers, so before == after for the OGXM ply.
            "board": _board_p1(dec["board"], dec["is_doubler_p1"]),
        }
        if cube_probs is not None:
            cube_resp_entry["probs"] = _probs_list(cube_probs)

        mwc_take = _eq2mwc(dt, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
        mwc_pass = _eq2mwc(dp, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
        if mwc_take is not None:
            cube_resp_entry["mwc_take"] = mwc_take
        if mwc_pass is not None:
            cube_resp_entry["mwc_pass"] = mwc_pass
        optimal_r = min(dt, dp)
        actual_r = dt if dec["response"] == "take" else dp
        opt_mwc_r = _eq2mwc(optimal_r, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
        act_mwc_r = _eq2mwc(actual_r, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
        if opt_mwc_r is not None and act_mwc_r is not None:
            # Mirror lost_equity: force 0 for a trivial (uncounted) response.
            cube_resp_entry["lost_mwc"] = round(act_mwc_r - opt_mwc_r, 4) if resp_counts else 0.0

        res.entries.append(cube_resp_entry)

    return res


def _eval_checker_decision(dec: dict, ctx: _EvalCtx) -> _DecResult:
    _require_playable(dec, ctx)
    res = _DecResult()
    die1, die2 = dec["dice"]
    analyzed = False
    illegal_move = False
    checker_err: float | None = None
    played_eq: float | None = None
    best_eq: float | None = None
    forced_eq: float | None = None
    alternatives: list[dict] = []

    # A ply with no legal move used to be skipped entirely -- nothing to judge,
    # so nothing to compute. But there is still a *position*: the one the
    # opponent now faces, and a viewer showing probabilities for a ply has
    # nothing else to show them from. Without it the only numbers on the ply are
    # the cube decision's, which are pre-roll and describe the position before
    # the dice -- so the panel reads as if it never updated.
    #
    # bgsage returns the unchanged board as the single legal "move" for a dance,
    # so it evaluates exactly like a forced move and needs no branch of its own;
    # it lands below as one played option with an empty notation, the same shape
    # XG writes. It stays out of PR, because the forced branch never sets
    # `analyzed`. The `no_legal` flag now only holds back the multi-move branch:
    # if a recorded no-play somehow has real moves available, that is a broken
    # record, not a decision to score, and it is left alone as before.
    legal = possible_moves(dec["board"], die1, die2)
    if len(legal) == 1:
        try:
            cheap = ctx.base_analyzer if ctx.base_analyzer is not None else ctx.analyzer
            forced_result = cheap.checker_play(
                dec["board"], die1, die2,
                cube_value=dec["cube_value"],
                cube_owner=dec["cube_owner"],
                away1=dec["away1"],
                away2=dec["away2"],
                is_crawford=dec["is_crawford"],
            )
            forced_moves = list(forced_result.moves)
            if forced_moves:
                m = forced_moves[0]
                eq = float(m.equity)
                forced_eq = eq
                opt: dict = {
                    "move": compute_move_notation(dec["board"], list(m.board), die1, die2),
                    "equity": round(eq, 4),
                    "eval_level": ctx.eval_label(m.eval_level),
                    "probs": _probs_list(m.probs),
                    "played": True,
                }
                mwc = _eq2mwc(eq, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
                if mwc is not None:
                    opt["mwc"] = mwc
                alternatives = [opt]
        except Exception as e:
            if ctx.verbose:
                print(f"  checker_play error (forced): {e}")
    elif len(legal) >= 2 and not dec.get("no_legal"):
        try:
            cheap = ctx.base_analyzer if ctx.base_analyzer is not None else ctx.analyzer
            # force_boards pins the played move into the deep-eval set at
            # every tier, so it is scored at the same level as the best move
            # even when the TINY filter would drop it (a blunder usually
            # lands outside the filter). Without it the played move carries
            # only its cheap 1-ply filter equity and the error has to be
            # re-derived from post_move_analytics below.
            cheap_result = _checker_play(
                cheap,
                dec["board"], die1, die2,
                cube_value=dec["cube_value"],
                cube_owner=dec["cube_owner"],
                away1=dec["away1"],
                away2=dec["away2"],
                is_crawford=dec["is_crawford"],
                force_boards=[dec["board_played"]],
            )
            cheap_moves = list(cheap_result.moves)
            if len(cheap_moves) >= 2:
                best_eq_b = float(cheap_moves[0].equity)
                tenth_eq_b = float(cheap_moves[min(9, len(cheap_moves) - 1)].equity)
                if abs(best_eq_b - tenth_eq_b) >= _CHECKER_SPREAD_EPS:
                    played_tuple = tuple(dec["board_played"])
                    checker_upgraded = False
                    played_m_eval_level: str | None = None
                    # tier_analyzer is the analyzer that produced `moves`
                    # (used below to re-evaluate the played move at the
                    # same depth). Defaults to the authoritative level.
                    tier_analyzer = ctx.analyzer
                    top2_gap = abs(best_eq_b - float(cheap_moves[1].equity))
                    if (ctx.mid_analyzer_checker is not None
                            and ctx.close_threshold is not None
                            and top2_gap <= ctx.close_threshold):
                        # 3-tier: near-tied decision -> middle tier, no
                        # rollout (the top moves are ~equal).
                        full_result = _checker_play(
                            ctx.mid_analyzer_checker,
                            dec["board"], die1, die2,
                            cube_value=dec["cube_value"],
                            cube_owner=dec["cube_owner"],
                            away1=dec["away1"],
                            away2=dec["away2"],
                            is_crawford=dec["is_crawford"],
                            force_boards=[dec["board_played"]],
                        )
                        moves = list(full_result.moves)
                        checker_upgraded = True
                        tier_analyzer = ctx.mid_analyzer_checker
                        # A near-tied TOP TWO says nothing about how far down
                        # the list the player went -- top2_gap is best-vs-second
                        # and the played move may be the eighth. Size a genuine
                        # error at the rollout tier even though the decision
                        # arrived as borderline: the rule the cube path already
                        # applies via `real_error`, and the one presets.py
                        # documents ("errors bigger than close_threshold still
                        # go to second_pass").
                        if _MID_ESCALATE and ctx.base_analyzer is not None:
                            mid_err = next(
                                (abs(float(m.equity_diff)) for m in moves
                                 if tuple(m.board) == played_tuple), None)
                            if mid_err is not None and mid_err > ctx.close_threshold:
                                full_result = _checker_play(
                                    ctx.analyzer,
                                    dec["board"], die1, die2,
                                    cube_value=dec["cube_value"],
                                    cube_owner=dec["cube_owner"],
                                    away1=dec["away1"],
                                    away2=dec["away2"],
                                    is_crawford=dec["is_crawford"],
                                    force_boards=[dec["board_played"]],
                                )
                                moves = list(full_result.moves)
                                tier_analyzer = ctx.analyzer
                    elif (ctx.base_analyzer is not None
                            and tuple(cheap_moves[0].board) != played_tuple):
                        # Error (played != screen best) -> rollout tier.
                        full_result = _checker_play(
                            ctx.analyzer,
                            dec["board"], die1, die2,
                            cube_value=dec["cube_value"],
                            cube_owner=dec["cube_owner"],
                            away1=dec["away1"],
                            away2=dec["away2"],
                            is_crawford=dec["is_crawford"],
                            force_boards=[dec["board_played"]],
                        )
                        moves = list(full_result.moves)
                        checker_upgraded = True
                    else:
                        moves = cheap_moves
                    best_eq = float(moves[0].equity)
                    for i, m in enumerate(moves):
                        is_played = tuple(m.board) == played_tuple
                        if is_played:
                            checker_err = abs(float(m.equity_diff))
                            played_eq = float(m.equity)
                            played_m_eval_level = m.eval_level
                        if ctx.all_moves or i < 10 or is_played:
                            m_eq = float(m.equity)
                            opt: dict = {
                                "move": compute_move_notation(dec["board"], list(m.board), die1, die2),
                                "equity": round(m_eq, 4),
                                "eval_level": ctx.eval_label(m.eval_level),
                                "probs": [
                                    round(float(m.probs.win), 3),
                                    round(float(m.probs.gammon_win), 3),
                                    round(float(m.probs.backgammon_win), 3),
                                    round(float(m.probs.gammon_loss), 3),
                                    round(float(m.probs.backgammon_loss), 3),
                                ],
                            }
                            mwc = _eq2mwc(m_eq, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
                            if mwc is not None:
                                opt["mwc"] = mwc
                            if is_played:
                                opt["played"] = True
                            alternatives.append(opt)
                    # Fallback for a played move still scored below the top
                    # move's level. force_boards above covers the normal
                    # filtered-blunder case; this remains for the levels
                    # where forcing cannot apply (post_move_analytics
                    # evaluates the resulting position rather than the move,
                    # so it is the weaker of the two paths).
                    if (checker_err is not None
                            and played_m_eval_level != moves[0].eval_level):
                        try:
                            post = tier_analyzer.post_move_analytics(
                                dec["board_played"],
                                cube_owner=dec["cube_owner"],
                                cube_value=dec["cube_value"],
                                away1=dec["away1"],
                                away2=dec["away2"],
                                is_crawford=dec["is_crawford"],
                            )
                            played_eq = post.cubeful_equity
                            checker_err = max(0.0, best_eq - played_eq)
                            for opt in alternatives:
                                if opt.get("played"):
                                    opt["equity"] = round(played_eq, 4)
                                    opt["eval_level"] = ctx.eval_label(moves[0].eval_level)
                                    break
                        except Exception as e:
                            if ctx.verbose:
                                print(f"  post_move_analytics error: {e}")
                    if checker_err is None:
                        illegal_move = True
                        res.illegal_moves += 1
                        if ctx.verbose:
                            print(
                                f"  Warning: played board not found in Sage's move list "
                                f"(game {ctx.game['game_number']}, "
                                f"turn {dec.get('notation', '?')!r}, "
                                f"dice {die1}{die2})"
                            )
                        try:
                            post = tier_analyzer.post_move_analytics(
                                dec["board_played"],
                                cube_owner=dec["cube_owner"],
                                cube_value=dec["cube_value"],
                                away1=dec["away1"],
                                away2=dec["away2"],
                                is_crawford=dec["is_crawford"],
                            )
                            played_eq = float(post.cubeful_equity)
                            checker_err = max(0.0, best_eq - played_eq)
                            analyzed = True
                            illegal_opt: dict = {
                                "move": dec.get("notation", ""),
                                "equity": round(played_eq, 4),
                                "eval_level": ctx.eval_label(moves[0].eval_level),
                                "probs": _probs_list(post.probs),
                                "played": True,
                                "illegal_move": True,
                            }
                            ill_mwc = _eq2mwc(played_eq, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
                            if ill_mwc is not None:
                                illegal_opt["mwc"] = ill_mwc
                            alternatives.append(illegal_opt)
                            if ctx.count_illegal:
                                if dec["is_p1"]:
                                    res.p1_err += checker_err
                                    res.p1_dec += 1
                                else:
                                    res.p2_err += checker_err
                                    res.p2_dec += 1
                        except Exception as e:
                            if ctx.verbose:
                                print(f"  post_move_analytics error (illegal move): {e}")
                    else:
                        analyzed = True
                        if dec["is_p1"]:
                            res.p1_err += checker_err
                            res.p1_dec += 1
                        else:
                            res.p2_err += checker_err
                            res.p2_dec += 1
                else:
                    # Already-decided position (top-ten equity spread below
                    # _CHECKER_SPREAD_EPS -- every option scores the same):
                    # show alternatives for the viewer but don't count the
                    # move in PR.
                    played_tuple = tuple(dec["board_played"])
                    checker_upgraded = False
                    for i, m in enumerate(cheap_moves):
                        is_played = tuple(m.board) == played_tuple
                        if ctx.all_moves or i < 10 or is_played:
                            m_eq = float(m.equity)
                            opt: dict = {
                                "move": compute_move_notation(dec["board"], list(m.board), die1, die2),
                                "equity": round(m_eq, 4),
                                "eval_level": ctx.eval_label(m.eval_level),
                                "probs": [
                                    round(float(m.probs.win), 3),
                                    round(float(m.probs.gammon_win), 3),
                                    round(float(m.probs.backgammon_win), 3),
                                    round(float(m.probs.gammon_loss), 3),
                                    round(float(m.probs.backgammon_loss), 3),
                                ],
                            }
                            mwc = _eq2mwc(m_eq, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
                            if mwc is not None:
                                opt["mwc"] = mwc
                            if is_played:
                                opt["played"] = True
                            alternatives.append(opt)
        except Exception as e:
            if ctx.verbose:
                print(f"  checker_play error: {e}")

    log_entry: dict = {
        "player": dec["player"],
        "kind": "checker",
        "dice": [die1, die2],
        "cube_value": dec["cube_value"],
        "cube_owner": dec["cube_owner"],
        "player_move": compute_move_notation(dec["board"], dec["board_played"], die1, die2),
        # The source's own notation, for export's illegal-play ladder. It beats
        # player_move there because player_move is re-derived from the two
        # boards, which is the thing that cannot be collapsed safely when an
        # illegal play overflows its ply record (export.fit_move_steps).
        "notation": dec.get("notation"),
        "move_options": alternatives,
        # Boards in P1/White perspective for OGXM export (ogid + structured move steps).
        "board_before": _board_p1(dec["board"], dec["is_p1"]),
        "board_after": _board_p1(dec["board_played"], dec["is_p1"]),
    }
    if illegal_move:
        log_entry["illegal_move"] = True
    if analyzed:
        update: dict = {
            "lost_equity": round(checker_err, 4),
            "counted": ctx.count_illegal if illegal_move else True,
            "upgraded": checker_upgraded,
        }
        best_mwc = _eq2mwc(best_eq, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
        played_mwc = _eq2mwc(played_eq, dec["away1"], dec["away2"], dec["cube_value"], dec["is_crawford"])
        if best_mwc is not None and played_mwc is not None:
            update["lost_mwc"] = round(best_mwc - played_mwc, 4)
        log_entry.update(update)

    _luck_az = ctx.luck_analyzer if ctx.luck_analyzer is not None else ctx.analyzer
    _hint = best_eq if best_eq is not None else forced_eq
    luck_val, preroll_eq_val = _compute_luck(
        dec["board"], die1, die2,
        dec["cube_value"], dec["cube_owner"],
        dec["away1"], dec["away2"], dec["is_crawford"],
        _luck_az, verbose=ctx.verbose,
        postroll_eq=None,  # use the 1-ply (luck-level) post-roll, not the preset best_eq,
        hint_eq=_hint,     # so luck is preset-independent (hint_eq still guards sign errors)
    )
    if luck_val is not None:
        log_entry["luck"] = luck_val
        # luck_mwc is a summary-only aggregate (not stored per-ply in OGXM --
        # MWC is compute-on-read there, see gvformat.stats). Computed here via
        # the shipped MET (gvformat.met.eq2mwc) purely to feed match.py's
        # engine-free summary total_luck_mwc.
        if dec["away1"] > 0 and dec["away2"] > 0:
            postroll_eq_raw = float(preroll_eq_val) + float(luck_val)
            luck_mwc_val = round(
                eq2mwc(postroll_eq_raw, int(dec["away1"]), int(dec["away2"]),
                       int(dec["cube_value"]), bool(dec["is_crawford"]))
                - eq2mwc(float(preroll_eq_val), int(dec["away1"]), int(dec["away2"]),
                         int(dec["cube_value"]), bool(dec["is_crawford"])),
                6,
            )
            if dec["is_p1"]:
                res.p1_luck_mwc += luck_mwc_val
            else:
                res.p2_luck_mwc += luck_mwc_val
        if dec["is_p1"]:
            res.p1_luck += luck_val
            res.p1_luck_count += 1
        else:
            res.p2_luck += luck_val
            res.p2_luck_count += 1
    res.entries.append(log_entry)
    return res


def _collate_game(results: "list[_DecResult]", game: dict, game_result, is_crawford: bool) -> dict:
    """Merge one game's per-decision results (in decision order) into the same
    dict shape `evaluate_game` has always returned.

    This is the single source of truth for cross-decision bookkeeping -- the
    sequential move_number and the no-double cube's carry-forward attachment
    onto the next checker entry -- so the serial and parallel drivers can't
    drift: both build a list of `_DecResult` (in decision order) and hand it
    to this same function.
    """
    tally = _GameTally()
    move_num = 0
    pending: "dict | None" = None

    for r in results:
        tally.p1_err += r.p1_err
        tally.p1_dec += r.p1_dec
        tally.p1_cube_dec += r.p1_cube_dec
        tally.p1_luck += r.p1_luck
        tally.p1_luck_mwc += r.p1_luck_mwc
        tally.p1_luck_count += r.p1_luck_count
        tally.p2_err += r.p2_err
        tally.p2_dec += r.p2_dec
        tally.p2_cube_dec += r.p2_cube_dec
        tally.p2_luck += r.p2_luck
        tally.p2_luck_mwc += r.p2_luck_mwc
        tally.p2_luck_count += r.p2_luck_count
        tally.illegal_moves += r.illegal_moves

        for entry in r.entries:
            if entry["kind"] == "checker" and pending is not None:
                entry["cube"] = pending
                pending = None
            move_num += 1
            entry["move_number"] = move_num
            tally.moves_log.append(entry)

        if r.pending_cube is not None:
            pending = r.pending_cube

    p1_pr = (tally.p1_err / tally.p1_dec * 500.0) if tally.p1_dec > 0 else float("nan")
    p2_pr = (tally.p2_err / tally.p2_dec * 500.0) if tally.p2_dec > 0 else float("nan")

    return {
        "game_number": game["game_number"],
        "score1_start": game["score1_start"],
        "score2_start": game["score2_start"],
        "is_crawford": is_crawford,
        "game_result": game_result,
        "p1_err": tally.p1_err,
        "p1_dec": tally.p1_dec,
        "p1_cube_dec": tally.p1_cube_dec,
        "p1_pr": p1_pr,
        "p1_luck": tally.p1_luck,
        "p1_luck_mwc": tally.p1_luck_mwc,
        "p1_luck_count": tally.p1_luck_count,
        "p2_err": tally.p2_err,
        "p2_dec": tally.p2_dec,
        "p2_cube_dec": tally.p2_cube_dec,
        "p2_pr": p2_pr,
        "p2_luck": tally.p2_luck,
        "p2_luck_mwc": tally.p2_luck_mwc,
        "p2_luck_count": tally.p2_luck_count,
        "illegal_moves": tally.illegal_moves,
        "moves": tally.moves_log,
    }


def evaluate_game(game: dict, analyzer: BgBotAnalyzer, recon: dict,
                  is_crawford: bool = False,
                  verbose: bool = False,
                  base_analyzer: "BgBotAnalyzer | None" = None,
                  luck_analyzer: "BgBotAnalyzer | None" = None,
                  mid_analyzer_checker: "BgBotAnalyzer | None" = None,
                  mid_analyzer_cube: "BgBotAnalyzer | None" = None,
                  close_threshold: float | None = None,
                  level: str = "3ply", all_moves: bool = False,
                  count_illegal: bool = False,
                  progress: "Callable[[], None] | None" = None) -> dict:
    """Evaluate all decisions in one game. Returns per-player error/decision counts and move log.

    `recon` is a pre-built reconstruction (``ogxm_reconstructor``'s per-game
    entry) supplying the decisions and game result; `game` carries only the
    per-game metadata (number, starting scores) used for reporting.
    `progress`, if given, is called once per decision processed (for a bar).
    """
    level_display = _LEVEL_DISPLAY.get(level, level)
    decisions = recon["decisions"]
    game_result = recon["game_result"]

    ctx = _EvalCtx(
        analyzer=analyzer, base_analyzer=base_analyzer,
        mid_analyzer_checker=mid_analyzer_checker, mid_analyzer_cube=mid_analyzer_cube,
        luck_analyzer=luck_analyzer, close_threshold=close_threshold,
        verbose=verbose, all_moves=all_moves, count_illegal=count_illegal,
        level_display=level_display, game=game,
    )

    results: "list[_DecResult]" = []
    for dec in decisions:
        if progress is not None:
            progress()
        kind = dec["kind"]
        if kind == "cube":
            results.append(_eval_cube_decision(dec, ctx))
        elif kind == "checker":
            results.append(_eval_checker_decision(dec, ctx))

    return _collate_game(results, game, game_result, is_crawford)
