# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Completing an analysis block that was written without the [GV] extensions.

OGXM is HedgeHog's format and ours extends it. Our GVAN chunk carries the
fields the base spec has no room for: whether a ply counted as a *decision*,
per-ply luck, and the engine's own name for the level it searched at. A file
from any other producer -- a ``.ogxm`` off hedgehog-bg.com, say -- has no GVAN,
and a reader that takes its absence literally reports "not a decision" for
every ply in the match. That is not what the file says. The file says
nothing, and a performance rating over an empty denominator is worse than no
rating at all.

So a block with no GVAN is completed here, from what the base format *does*
carry. Two separate things are wrong until it is:

  * **Units.** In match play the base spec stores the three cube values as
    raw MWC -- "because that is the unit the analyser decides in" -- and
    offers normalized equity as a derived view. We store the normalized view,
    where dropping the cube is -1 and cashing it is +1. The map between them
    is affine and both anchors are the pre-decision stake at this score,
    which the ply's own replayed OGID already carries.

  * **Decisions.** Which plies counted has to be derived rather than read: a
    checker play counts when its candidates actually disagree, a cube when it
    is not trivial. These are the rules ``xg.py``, ``bgf.py`` and
    ``og2gva.js`` already apply to sources that record no flag of their own,
    and they are restated rather than imported so this module stands alone --
    the same bargain those three struck with each other.

**Luck is not here, and cannot be.** It is the pre-roll position's equity
averaged over all 21 rolls against what the roll actually gave, and no amount
of reading tells you the first number -- it takes an engine. A block filled
here therefore still has no luck, deliberately: see ``gvanalysis``'s luck
pass, which is what supplies it.

The JavaScript mirror is ``gvformat-js/src/basefill.js`` -- keep the two in
step.
"""

from __future__ import annotations

import math
import re

from .met import mwc_anchors

# Checker plays sit at action ids 0-20; 21-23 are the cube.
_MAX_CHECKER_ACTION_ID = 20
_TAKE_PASS_ACTIONS = frozenset({22, 23})

# Mirrors gvformat.xg._CHECKER_SPREAD_EPS / xg2gva's CHECKER_SPREAD_EPS.
_CHECKER_SPREAD_EPS = 1e-4


def _trivial_cube(nd: float, dt: float, dp: float) -> bool:
    """Mirrors gvanalysis.game_eval._trivial_cube / xg2gva's trivialCube."""
    return (
        abs(nd - min(dt, dp)) < 0.001
        or (nd - dt) > 0.200
        or (nd - dp) > 0.200
        or (nd < -0.900 and dt < -0.900)
    )


def _trivial_take_pass(dt: float, dp: float) -> bool:
    """Mirrors gvanalysis.game_eval._trivial_take_pass / xg2gva's
    trivialTakePass."""
    return abs(dt - dp) < 0.001


# ---------------------------------------------------------------------------
# The score frame a ply was played at
# ---------------------------------------------------------------------------

# OGID's match_length field: digits, then an optional single-letter suffix
# ("C" = Crawford, "G<n>" = fixed-games money session, "L" = post-Crawford).
_OGID_MATCH_LENGTH_RE = re.compile(r"^(\d+)([A-Za-z].*)?$")


def _parse_ogid_context(ogid: str | None) -> tuple[int, int, int, int, bool] | None:
    """The cube/score context of ``ogid``, or None when it is absent or
    malformed.

    Deliberately the same parse ``stats.py``'s ``_parse_ogid_context`` does,
    off the same field layout, for the same reason: the frame a ply was
    decided in is in the ply, and nothing needs to store it a second time.
    Returns ``(cube_value, score_w, score_b, match_length, is_crawford)``.
    """
    if not ogid:
        return None
    parts = ogid.split(":")
    if len(parts) < 9:
        return None
    if len(parts[2]) < 2 or not parts[2][1].isdigit():
        return None
    cube_value = 1 << int(parts[2][1])
    try:
        score_w = int(parts[6])
        score_b = int(parts[7])
    except ValueError:
        return None
    m = _OGID_MATCH_LENGTH_RE.match(parts[8])
    if not m:
        return None
    match_length = int(m.group(1))
    is_crawford = (m.group(2) or "").startswith("C")
    return cube_value, score_w, score_b, match_length, is_crawford


def _normalizer(ply: dict):
    """The affine map from this ply's MWC onto its normalized-equity frame,
    or None where there is no frame to map onto (money play, an unreplayable
    ply).

    The three cube values are the **doubler's** throughout, so on a take or a
    pass the frame belongs to the other player -- the ply's own colour is the
    one answering the cube, not the one who offered it.
    """
    ctx = _parse_ogid_context(ply.get("ogid_before"))
    if ctx is None:
        return None
    cube_value, score_w, score_b, match_length, is_crawford = ctx
    if match_length <= 0:
        return None  # money play: already equity
    away_w = match_length - score_w
    away_b = match_length - score_b
    answering = ply.get("action_id") in _TAKE_PASS_ACTIONS
    doubler_is_white = ply.get("color") != 1 if answering else ply.get("color") == 1
    away1 = away_w if doubler_is_white else away_b
    away2 = away_b if doubler_is_white else away_w
    if away1 <= 0 or away2 <= 0:
        return None
    mwc_win, mwc_lose = mwc_anchors(away1, away2, cube_value, is_crawford)
    span = mwc_win - mwc_lose
    if span == 0:
        return None
    mid = (mwc_win + mwc_lose) / 2

    def to_equity(mwc: float) -> float:
        return round((2 * (mwc - mid) / span) * 10000) / 10000

    return to_equity


def _cube_values(sub: dict | None) -> tuple[float, float, float] | None:
    """The three cube values a payload carries, if it carries them."""
    if sub is None or sub.get("no_double_equity") is None:
        return None
    return sub["no_double_equity"], sub["double_take_equity"], sub["double_pass_equity"]


def _values_are_mwc(block_obj: dict) -> bool:
    """Are this block's cube values raw MWC, or are they already equity?

    The GVAN test above says the block is not ours, and nothing but ours
    writes the normalized view into those slots -- so in practice the answer
    is always "MWC". This is the belt to that braces, and it is decidable
    rather than a guess: an MWC is a probability and cannot leave [0, 1],
    while a normalized equity leaves it constantly (a doubler who is behind
    is negative, and a take that loses ground runs past -1). One value
    outside the unit interval is therefore proof the block is already
    converted, and no MWC block can produce one. The reverse is not proof --
    a match whose every cube decision favoured the doubler would sit inside
    [0, 1] in either unit -- which is why this is the second test and not the
    first.
    """
    seen = False
    for analysis in block_obj.values():
        for sub in (analysis, analysis.get("cube_decision"), analysis.get("missed_double")):
            values = _cube_values(sub)
            if values is None:
                continue
            seen = True
            for v in values:
                if not (0 <= v <= 1):
                    return False
    return seen


def _normalize_cube(sub: dict | None, to_equity) -> None:
    """Rewrite one cube payload's three values through ``to_equity``, in
    place."""
    if sub is None or sub.get("no_double_equity") is None:
        return
    sub["no_double_equity"] = to_equity(sub["no_double_equity"])
    sub["double_take_equity"] = to_equity(sub["double_take_equity"])
    sub["double_pass_equity"] = to_equity(sub["double_pass_equity"])


# ---------------------------------------------------------------------------
# Deriving the decision flags
# ---------------------------------------------------------------------------

def _checker_is_decision(analysis: dict) -> bool:
    """Did this checker play pose a decision at all?

    A forced move and a position where every candidate scores the same are
    not decisions, and an illegal play is excluded outright -- the player did
    not choose among these moves, they broke the rules.
    """
    if analysis.get("illegal_move"):
        return False
    alts = analysis.get("alternatives")
    if not isinstance(alts, list) or len(alts) < 2:
        return False
    lo = float("inf")
    hi = float("-inf")
    for alt in alts:
        try:
            eq = float(alt.get("equity"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(eq):
            continue
        if eq < lo:
            lo = eq
        if eq > hi:
            hi = eq
    return hi - lo >= _CHECKER_SPREAD_EPS


def _cube_ply_is_decision(ply: dict, analysis: dict) -> bool:
    """The flag for a cube ply of its own (action 21-23), on the normalized
    scale."""
    nd = analysis.get("no_double_equity")
    dt = analysis.get("double_take_equity")
    dp = analysis.get("double_pass_equity")
    if nd is None or dt is None or dp is None:
        return False
    if ply.get("action_id") in _TAKE_PASS_ACTIONS:
        return not _trivial_take_pass(dt, dp)
    return not (_trivial_cube(nd, dt, dp) and (analysis.get("equity_loss") or 0) < 0.001)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def complete_base_block(block_obj: dict, ply_by_key: dict, analysis_info: dict | None) -> None:
    """Complete one analysis block in place, given the plies it describes.

    Args:
        block_obj: ``(game_index, ply_index) -> analysis``.
        ply_by_key: the same keys -> the ply itself.
        analysis_info: the block's own header, also completed.
    """
    ply_depths: set = set()
    mwc = _values_are_mwc(block_obj)

    for key, analysis in block_obj.items():
        ply = ply_by_key.get(key)
        if ply is None:
            continue

        # Units first: every threshold below is stated in normalized equity,
        # and reading a raw MWC as one would call almost every cube trivial.
        to_equity = _normalizer(ply) if mwc else None
        if to_equity is not None:
            _normalize_cube(analysis, to_equity)
            _normalize_cube(analysis.get("cube_decision"), to_equity)
            _normalize_cube(analysis.get("missed_double"), to_equity)

        action_id = ply.get("action_id")
        if action_id is not None and action_id <= _MAX_CHECKER_ACTION_ID:
            analysis["decision"] = _checker_is_decision(analysis)
        elif analysis.get("no_double_equity") is not None:
            analysis["decision"] = _cube_ply_is_decision(ply, analysis)

        # The live cube above a checker play: not itself an error, so
        # triviality is the whole test (og2gva applies the same one to the
        # same shape).
        live = analysis.get("cube_decision")
        if live is not None and live.get("no_double_equity") is not None:
            live["decision"] = not _trivial_cube(
                live["no_double_equity"], live["double_take_equity"], live["double_pass_equity"])
        # A `missed_double` deliberately keeps no flag of its own: `stats.py`
        # re-derives one from its three equities whenever the source recorded
        # none, and that rule already accounts for the doubler's own error.

        if analysis.get("ply"):
            ply_depths.add(analysis["ply"])

    # The base spec lets a decision be searched deeper than its block, and the
    # two fields carrying that are alternatives, not a pair: the header's `ply`
    # is a default, and a per-decision `ply` of 0 means "use it". So a producer
    # can put the depth in either place, and real files use both -- HedgeHog
    # stamps every entry and leaves the header at 0 for a plain run, then does
    # the exact reverse for a block of re-run decisions. Read whichever one
    # speaks, so the block can say how deep it looked either way.
    if analysis_info is not None:
        depth = next(iter(ply_depths)) if len(ply_depths) == 1 else None
        if depth is None and not ply_depths:
            depth = analysis_info.get("ply") or None
        if depth is not None:
            if not analysis_info.get("ply"):
                analysis_info["ply"] = depth
            if analysis_info.get("eval_level") is None:
                analysis_info["eval_level"] = f"{depth}ply"
