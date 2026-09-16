# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Append one analysis onto an OGXM document, preserving any it already carries.

The OGXM format allows up to ``MAX_ANALYSES`` analysis blocks per match (see the
binary spec's ``ANAL`` groups / the JSON spec's ``analyses_info`` +
per-ply ``analyses[]``). ``append_analysis`` merges a freshly-produced analysis
(``our`` — an OGXM built by ``to_ogxm_json`` over the *same* match) into a base
document, so the analysis service can add its evaluation without dropping a
model the caller already ran.

The base document's match body (games, plies, OGIDs, orientation, clock) is
kept **verbatim**; only per-ply ``analysis``/``analyses`` and the top-level
analysis metadata change. ``our``'s analysis objects are transplanted onto the
base's plies by decision-ply order (``our`` carries only decision plies; the
base may also have terminal/set-position plies, which never take analysis).

Both documents must share orientation (same ``player_white``) — analysis move
steps live in the absolute frame, so a mismatch would mirror them. Callers
build ``our`` from ``base``'s own ``player_white``/``player_black``, and every
codec-produced OGXM is canonically oriented, so this holds by construction; it
is asserted defensively.
"""

from __future__ import annotations

import copy

#: Format cap on analysis blocks (binary spec: MAX_ANALYSES).
MAX_ANALYSES = 16


def _is_decision(ply: dict) -> bool:
    """True for a checker/cube action ply (id 0-23), which can carry analysis.
    Terminal (24-30) and set-position (31) plies cannot."""
    aid = ply.get("action_id")
    return aid is not None and 0 <= aid <= 23


def _decision_analyses(game: dict) -> list[dict | None]:
    """Per-decision analysis objects for one ``our`` game, in ply order — each
    decision ply's ``analysis`` (``None`` when it has nothing to report).
    Terminal/set-position plies are skipped so this aligns 1:1 with the base's
    decision plies."""
    return [ply.get("analysis") for ply in game.get("plies") or [] if _is_decision(ply)]


def _base_decision_plies(game: dict) -> list[dict]:
    """The base plies that can carry analysis (checker/cube actions, id 0-23),
    in order. Terminal (24-30) and set-position (31) plies are excluded."""
    return [ply for ply in game.get("plies") or [] if _is_decision(ply)]


def _aligned(base_game: dict, our_game: dict) -> list[tuple[dict, dict | None]]:
    """Pair each base decision ply with the corresponding ``our`` analysis."""
    base_plies = _base_decision_plies(base_game)
    our_analyses = _decision_analyses(our_game)
    if len(base_plies) != len(our_analyses):
        raise ValueError(
            f"analysis/ply count mismatch in game "
            f"{base_game.get('game_index')}: {len(base_plies)} base decision "
            f"plies vs {len(our_analyses)} analyses"
        )
    return list(zip(base_plies, our_analyses))


def _promote_to_multi(base: dict) -> None:
    """Rewrite a single-analysis base in place to explicit multi form: move its
    ``analysis_info`` into ``analyses_info[0]`` and each ply's ``analysis`` into
    a one-element ``analyses`` (tagged ``analysis_index=0``). The single-form
    mirrors (``analysis_info`` + per-ply ``analysis``) are kept as the primary."""
    base["analyses_info"] = [base["analysis_info"]]
    for game in base.get("games") or []:
        for ply in game.get("plies") or []:
            a = ply.get("analysis")
            if a is not None:
                ply["analyses"] = [{**a, "analysis_index": 0}]


def append_analysis(base: dict, our: dict) -> dict:
    """Return a copy of ``base`` with ``our``'s analysis appended.

    - ``base`` carries **no** analysis: the result is single-analysis (legacy
      shape) — ``our``'s analysis objects on the plies, ``our``'s
      ``analysis_info`` at the top. Byte-for-byte the same as analyzing the
      match directly.
    - ``base`` already carries **one or more**: the result is multi-analysis —
      ``analyses_info`` lists all blocks (existing first, ``our`` last) and each
      decision ply gains ``our``'s entry in its ``analyses`` array. The existing
      primary (index 0) stays mirrored in ``analysis_info`` / per-ply
      ``analysis`` for naive single-analysis readers.
    """
    if base.get("player_white") != our.get("player_white"):
        raise ValueError(
            "orientation mismatch: base player_white="
            f"{base.get('player_white')!r} vs our={our.get('player_white')!r}"
        )

    base = copy.deepcopy(base)
    our_info = dict(our.get("analysis_info") or {})
    games = list(zip(base.get("games") or [], our.get("games") or []))

    has_existing = "analyses_info" in base or "analysis_info" in base

    if not has_existing:
        # Single-analysis output: attach directly.
        for bg, og in games:
            for base_ply, analysis in _aligned(bg, og):
                if analysis is not None:
                    base_ply["analysis"] = analysis
        base["analysis_info"] = our_info
        return base

    # Multi-analysis output. Normalize to explicit multi form first.
    if "analyses_info" not in base:
        _promote_to_multi(base)

    if len(base["analyses_info"]) >= MAX_ANALYSES:
        raise ValueError(
            f"cannot append: base already has {len(base['analyses_info'])} "
            f"analyses (format cap is {MAX_ANALYSES})"
        )

    our_index = len(base["analyses_info"])
    base["analyses_info"].append(our_info)
    for bg, og in games:
        for base_ply, analysis in _aligned(bg, og):
            if analysis is not None:
                base_ply.setdefault("analyses", []).append(
                    {**analysis, "analysis_index": our_index}
                )
    return base
