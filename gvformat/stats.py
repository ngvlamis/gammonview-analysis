# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Pure-stdlib "compute-on-read" aggregates for an OGXM-JSON match dict.

``OGXM_JSON_SPEC_GAMMONVIEW.md`` deliberately does *not* store match/game/
player aggregates (PR, total error, decision counts, luck totals, illegal-
move counts) -- see its "Derived statistics are not stored" section. They
are meant to be computed from the per-ply ``analysis`` records on read, per
the formulas in ``OGXM_COMPUTED_FIELDS.md``. This module is that Python-side
helper. It performs no bgsage/engine calls -- its only in-repo import is the
sibling pure-stdlib ``gvformat.met`` (the shipped MET, for engine-free
equity->MWC conversion) -- and otherwise only reads the dict shape
``ogxm_export.to_ogxm_json(...)`` produces.

Conventions (mirrors ``OGXM_COMPUTED_FIELDS.md`` / the GammonView spec):

- White = Player 1, Black = Player 2 (mat-file convention).
- A ply belongs to white if ``color == 1``, black if ``color == 0``.
- Only a ply whose ``analysis.decision`` is true counts toward PR/error/
  decision counts. Cube decisions are ``action_id`` 21 (double), 22 (take),
  23 (pass) -- *plus* the cube sub-analyses this exporter embeds on checker
  plies (see "Embedded cube decisions" below).

Field-name note: ``OGXM_COMPUTED_FIELDS.md`` refers to a ``counted`` flag on
``analysis``; the exporter (``ogxm_export.py``) actually emits ``decision``
(bool) -- ``counted`` is this repo's internal/pre-export GVA-shape field
name, renamed to ``decision`` for the OGXM JSON. This module reads
``decision``, matching what real OGXM output actually contains.

Embedded cube decisions
------------------------
``OGXM_COMPUTED_FIELDS.md`` describes cube decisions as just the standalone
``action_id`` 21/22/23 plies. That undercounts: when a player *holds*
correctly (doesn't double, and holding was the optimal action), the
resulting cube analysis is embedded as ``cube_decision`` on the *next*
checker ply's ``analysis`` (mutually exclusive with ``missed_double`` --
see ``OGXM_JSON_SPEC_GAMMONVIEW.md``'s CubeDecision section), not emitted
as its own ply. ``cube_decision`` carries its own ``decision`` flag and
*does* count toward cube_decisions/total_decisions/total_error in the
authoritative in-memory tally (``game_eval.py``'s ``_eval_cube_decision``:
the ``doubler_counts`` bump happens before the ``if doubled:`` branch, so it
applies to both the doubled and not-doubled cases identically). This module
counts both standalone cube plies and embedded ``cube_decision`` blocks.

``missed_double`` (player should have doubled but didn't) is the other
half of that same not-doubled branch, but -- per
``OGXM_JSON_SPEC_GAMMONVIEW.md``'s MissedDouble object -- it carries no
``decision`` flag at all (unlike ``cube_decision``). Whether a missed
double counted toward PR is nonetheless fully recoverable: it is exactly
``game_eval.py``'s ``_trivial_cube(nd, dt, dp)`` combined with the
``< 0.001`` error-floor check, evaluated on the three equities the
MissedDouble object *does* store (``no_double_equity``,
``double_take_equity``, ``double_pass_equity``). This module reimplements
that check (``_missed_double_counts``) rather than assuming every
``missed_double`` counts -- assuming "always counts" was verified against
real data to overcount by exactly the trivial cases (equity gap < 0.001).

Illegal moves
--------------
``OGXM_COMPUTED_FIELDS.md`` says to count checker plies with
``analysis.illegal_move == true``. In the real pipeline, "illegal move"
means the played board couldn't be matched to any legal move bgsage
generated -- usually a transcription error rather than a rules violation --
and ``gvanalysis.game_eval`` sets an ``illegal_move`` flag on the *GVA-shape*
log entry, which ``export.py`` propagates onto the checker ply's
``analysis.illegal_move``. So this counter does fire on real matches: the
corpus match ``5nqfGw9bWG3deTaU`` carries one, and ``tests/test_illegal_move.py``
asserts it reaches these aggregates as ``illegal_moves == 1`` (the JavaScript
mirror checks the same file).

This module *also* looks at the ply's own ``illegal_move`` and at each
alternative's, purely for robustness against other producers. Neither is
written here: ``game_eval`` flags the played alternative too, but that flag
never reaches OGXM -- ``export.py`` does not copy it, and the binary has one
flag bit per ply (``reader.py``: ``flags & 0x04``), which decodes back onto
``analysis``. Count each ply once: the ``else`` below is what stops a file
carrying the flag in two places from being counted twice.

Luck / luck_mwc
-----------------
``luck`` is a single field stored directly on a checker ply's ``analysis``
(``postroll - preroll`` at the luck-analyzer's level, 1-ply today,
preset-independent -- see ``analysis_info.luck_eval_level``). Summed over
checker plies where the color matches and ``luck`` is present -- independent
of ``decision`` (luck is computed for every rolled ply, not just counted
ones). ``luck_rolls`` reproduces exactly against the authoritative tally
(verified below), and because ``luck`` is always at the fixed luck-analyzer
level, ``total_luck`` reproduces the authoritative in-memory tally *exactly*
(to floating-point rounding) for every checker ply, including forced-move
and trivial-spread plies. See ``tests/test_ogxm_stats.py`` for an exact-match
comparison against a real match's ``summary`` luck totals.

``luck_mwc`` (aggregated as ``total_luck_mwc``) is computed engine-free via
the one shipped MET (``gvformat.met``, Kazaross-XG2): for a fixed
``(score, cube)``, ``eq2mwc`` is affine in equity, so a *change* in equity
converts to a change in MWC via half its win/loss slope --
``luck_mwc = luck * (mwc_on_win - mwc_on_loss) / 2``, where the anchors come
from ``met.mwc_anchors(away1, away2, cube_value, is_crawford)``. The format
carries no per-decision anchors any more (removed from the binary/JSON --
MWC is compute-on-read everywhere), so this module derives
``(away1, away2, cube_value, is_crawford)`` itself by parsing the ply's own
``ogid_before`` string (see ``_parse_ogid_context``/``_luck_to_mwc`` below) --
no engine, no
stored anchor. ``total_luck_mwc`` reproduces the analyzer's engine-computed
totals to 1/10000 quantization (met.py is validated to match bgsage's own
``eq2mwc`` exactly); it is absent for money games (no score/cube frame to
anchor MWC to), matching the old GVA summary. See ``tests/test_ogxm_stats.py``.
"""

from __future__ import annotations

import re

from .met import mwc_anchors

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Checker-move ply action ids (dice combinations); anything above this is a
#: cube/game/match/resign ply, not a checker-move ply.
_MAX_CHECKER_ACTION_ID = 20

#: Standalone cube-decision ply action ids: 21 = double, 22 = take, 23 = pass.
_CUBE_ACTION_IDS = frozenset({21, 22, 23})

#: color -> player key, per the White = Player 1 / Black = Player 2 convention.
_COLOR_NAME = {1: "white", 0: "black"}


# ---------------------------------------------------------------------------
# OGID field parsing: derive (away1, away2, cube_value, is_crawford) from a
# ply's own `ogid_before` string, so luck_mwc needs no stored per-decision
# anchor -- see module docstring's "Luck / luck_mwc" section. Self-contained
# (doesn't import gvformat.ogid's encoder; just splits the string this
# module's own OGID producer emits -- see gvformat/ogid.py for the writer).
# ---------------------------------------------------------------------------

#: OGID's match_length field: digits, then an optional single-letter suffix
#: ("C" = Crawford, "G<n>" = fixed-games money session, "L" = post-Crawford)
#: -- see ogid.py's `_encode_match_length`. Only "C" means Crawford.
_OGID_MATCH_LENGTH_RE = re.compile(r"^(\d+)([A-Za-z].*)?$")


def _parse_ogid_context(ogid: str | None) -> tuple[int, int, int, int, bool] | None:
    """Parse an OGID string's cube/score/match_length fields.

    OGID field layout (colon-separated, see ``gvformat/ogid.py``):
    ``white_positions:black_positions:cube:dice:color:game_state:score_w:
    score_b:match_length[:move_id[:nrof_checkers]]``. The cube field is
    ``<owner><exponent><action>`` (e.g. ``"N0N"``); ``cube_value = 1 <<
    exponent``. Returns ``(cube_value, score_w, score_b, match_length,
    is_crawford)``, or None if ``ogid`` is absent/malformed.
    """
    if not ogid:
        return None
    parts = ogid.split(":")
    if len(parts) < 9:
        return None
    try:
        cube_value = 1 << int(parts[2][1])
        score_w = int(parts[6])
        score_b = int(parts[7])
    except (IndexError, ValueError):
        return None
    m = _OGID_MATCH_LENGTH_RE.match(parts[8])
    if not m:
        return None
    match_length = int(m.group(1))
    is_crawford = (m.group(2) or "").startswith("C")
    return cube_value, score_w, score_b, match_length, is_crawford


def _eq_delta_to_mwc(ply: dict, delta: float) -> float | None:
    """Convert a *change in equity* on ``ply`` into a change in MWC,
    engine-free via the one shipped MET.

    Used for both per-ply luck (postroll - preroll) and per-decision error
    (best - played): ``eq2mwc`` is affine in equity for a fixed score/cube,
    so any equity delta maps to an MWC delta via half the win/loss slope:
    ``delta_mwc = delta * (mwc_on_win - mwc_on_loss) / 2``.

    Derives ``(away1, away2, cube_value, is_crawford)`` from the ply's own
    ``ogid_before`` (the mover is ``ply["color"]`` -- NOT the OGID string's
    own "color" field, which encodes the complement of on-roll, i.e. the
    player who just moved to reach that position; see ``gvformat/ogid.py``'s
    module docstring). Returns None for money games / an unparseable
    ``ogid_before``.
    """
    ctx = _parse_ogid_context(ply.get("ogid_before"))
    if ctx is None:
        return None
    cube_value, score_w, score_b, match_length, is_crawford = ctx
    if match_length <= 0:
        return None  # money game: no MET frame
    away_w = match_length - score_w
    away_b = match_length - score_b
    mover_is_white = ply.get("color") == 1
    away1 = away_w if mover_is_white else away_b
    away2 = away_b if mover_is_white else away_w
    if away1 <= 0 or away2 <= 0:
        return None
    mwc_win, mwc_loss = mwc_anchors(away1, away2, cube_value, is_crawford)
    return delta * (mwc_win - mwc_loss) / 2.0


def _trivial_cube(nd: float, dt: float, dp: float) -> bool:
    return (
        abs(nd - min(dt, dp)) < 0.001
        or (nd - dt) > 0.200
        or (nd - dp) > 0.200
        or (nd < -0.900 and dt < -0.900)
    )


def _missed_double_counts(missed_double: dict) -> bool:
    """Whether a MissedDouble sub-object counted as a decision.

    Reimplements game_eval.py's doubler_counts check (`not (trivial and
    doubler_err < 0.001)`) purely from the three equities MissedDouble
    stores. `doubled` is always False for a missed double (that's what
    makes it "missed"), so `actual_d == no_double_equity` and
    `optimal == min(double_take_equity, double_pass_equity)` (always the
    larger of the two cube options, since correct_action == "double").
    """
    explicit = missed_double.get("decision")
    if explicit is not None:
        # The source recorded its own counted-ness (BGF: pr.cubeError); prefer
        # it over re-deriving, which cannot reproduce another engine's rule.
        return bool(explicit)
    nd = missed_double.get("no_double_equity")
    dt = missed_double.get("double_take_equity")
    dp = missed_double.get("double_pass_equity")
    if nd is None or dt is None or dp is None:
        return True  # can't recompute triviality; conservatively count it
    doubler_err = max(0.0, min(dt, dp) - nd)
    trivial = _trivial_cube(nd, dt, dp)
    return not (trivial and doubler_err < 0.001)


# ---------------------------------------------------------------------------
# Per-player accumulator
# ---------------------------------------------------------------------------

class _Totals:
    __slots__ = ("error", "error_mwc", "decisions", "cube_decisions", "luck",
                 "luck_rolls", "luck_mwc", "has_mwc")

    def __init__(self) -> None:
        self.error = 0.0
        self.error_mwc = 0.0
        self.decisions = 0
        self.cube_decisions = 0
        self.luck = 0.0
        self.luck_rolls = 0
        self.luck_mwc = 0.0
        self.has_mwc = False  # True once any anchored luck/error mwc was accumulated

    def add(self, other: "_Totals") -> None:
        self.error += other.error
        self.error_mwc += other.error_mwc
        self.decisions += other.decisions
        self.cube_decisions += other.cube_decisions
        self.luck += other.luck
        self.luck_rolls += other.luck_rolls
        self.luck_mwc += other.luck_mwc
        self.has_mwc = self.has_mwc or other.has_mwc


def _new_pair() -> dict:
    return {"white": _Totals(), "black": _Totals()}


def _finalize(t: _Totals) -> dict:
    pr = round(t.error / t.decisions * 500.0, 3) if t.decisions > 0 else None
    out = {
        "pr": pr,
        "total_error": round(t.error, 4),
        "total_decisions": t.decisions,
        "cube_decisions": t.cube_decisions,
        "total_luck": round(t.luck, 4),
        "luck_rolls": t.luck_rolls,
    }
    # total_luck_mwc / total_error_mwc are engine-free (met.mwc_anchors,
    # derived from ogid_before); absent for money games (no score/cube frame to
    # anchor MWC to), mirroring the old GVA summary (which omitted all MWC
    # fields for money play). total_error_mwc is the MWC analog of total_error:
    # per counted decision, equity_loss converts to MWC lost via the same MET
    # slope as luck (see _eq_delta_to_mwc).
    if t.has_mwc:
        out["total_luck_mwc"] = round(t.luck_mwc, 6)
        out["total_error_mwc"] = round(t.error_mwc, 6)
    return out


# ---------------------------------------------------------------------------
# Per-ply accumulation
# ---------------------------------------------------------------------------

def _accumulate_cube_sub_analysis(ply: dict, sub: dict, t: _Totals, counts: bool) -> None:
    if not counts:
        return
    eq_loss = sub.get("equity_loss") or 0.0
    t.error += eq_loss
    t.decisions += 1
    t.cube_decisions += 1
    # Embedded cube error in MWC terms uses the parent checker ply's anchors
    # (the no-double decision is at that ply's score/cube frame).
    emwc = _eq_delta_to_mwc(ply, eq_loss)
    if emwc is not None:
        t.error_mwc += emwc
        t.has_mwc = True


def _accumulate_ply(ply: dict, totals: dict, illegal_counter: list) -> None:
    analysis = ply.get("analysis")
    if analysis is None:
        return  # ply wasn't analyzed (e.g. unanalyzed match, or a game/end ply)
    color = _COLOR_NAME.get(ply.get("color"))
    if color is None:
        return
    t = totals[color]
    action_id = ply.get("action_id")

    if action_id in _CUBE_ACTION_IDS:
        # Standalone cube-decision ply (an actual double, or a take/pass response).
        if analysis.get("decision"):
            eq_loss = analysis.get("equity_loss") or 0.0
            t.error += eq_loss
            t.decisions += 1
            t.cube_decisions += 1
            emwc = _eq_delta_to_mwc(ply, eq_loss)
            if emwc is not None:
                t.error_mwc += emwc
                t.has_mwc = True
        return

    if action_id is None or action_id > _MAX_CHECKER_ACTION_ID:
        return  # game-over/match-over/final/resign/forfeit ply: not a PR/luck decision

    # Checker-move ply.
    if analysis.get("decision"):
        eq_loss = analysis.get("equity_loss") or 0.0
        t.error += eq_loss
        t.decisions += 1
        emwc = _eq_delta_to_mwc(ply, eq_loss)
        if emwc is not None:
            t.error_mwc += emwc
            t.has_mwc = True

    # Luck: a single stored field (postroll - preroll at the luck eval level).
    luck_val = analysis.get("luck")
    if luck_val is not None:
        t.luck += luck_val
        t.luck_rolls += 1
        lmwc = _eq_delta_to_mwc(ply, luck_val)
        if lmwc is not None:
            t.luck_mwc += lmwc
            t.has_mwc = True

    cube_decision = analysis.get("cube_decision")
    if cube_decision is not None:
        _accumulate_cube_sub_analysis(ply, cube_decision, t, bool(cube_decision.get("decision")))

    missed_double = analysis.get("missed_double")
    if missed_double is not None:
        _accumulate_cube_sub_analysis(ply, missed_double, t, _missed_double_counts(missed_double))

    # There is deliberately no third source of cube decisions here. A cube
    # decision with no stored evaluation cannot be represented in a ``.gvab``
    # -- a CUBE record *is* its three equities -- so counting one would make
    # these totals depend on something the format cannot carry, and a saved
    # match would disagree with the same match on screen. See ``gvformat.bgf``,
    # which drops BGBlitz's marker for exactly that reason.

    # Illegal-move flag: see the "Illegal moves" section of this module's
    # docstring. ``analysis.illegal_move`` is the location this repo writes and
    # the only one a .gvab can carry; the other two are read for foreign files,
    # and the ``else`` keeps a file that sets both from counting twice.
    if analysis.get("illegal_move") or ply.get("illegal_move"):
        illegal_counter[0] += 1
    else:
        for alt in analysis.get("alternatives") or ():
            if alt.get("illegal_move"):
                illegal_counter[0] += 1
                break


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_aggregates(ogxm: dict) -> dict:
    """Compute PR / error / decision / luck / illegal-move aggregates.

    ``ogxm`` is the dict produced by ``ogxm_export.to_ogxm_json(...)``
    (equivalently, anything conforming to ``OGXM_JSON_SPEC_GAMMONVIEW.md``).
    Pure function: no engine calls, no imports outside the stdlib.

    Returns::

        {
          "match": {
            "white": {"pr": float|None, "total_error": float,
                       "total_decisions": int, "cube_decisions": int,
                       "total_luck": float, "luck_rolls": int},
            "black": {...same shape...},
            "illegal_moves": int,
          },
          "games": [
            {"game_index": int, "white": {...}, "black": {...},
             "illegal_moves": int},
            ...
          ],
        }

    ``pr`` is ``None`` when a player has zero decisions (mirrors
    ``gvan_match.py``'s ``_pr_for_json``, which maps NaN -> None). See this
    module's docstring for what is *not* included (``luck_mwc``) and why.
    """
    match_totals = _new_pair()
    match_illegal = [0]
    games_out: list[dict] = []

    for game_index, game in enumerate(ogxm.get("games") or ()):
        game_totals = _new_pair()
        game_illegal = [0]
        for ply in game.get("plies") or ():
            _accumulate_ply(ply, game_totals, game_illegal)

        for color in ("white", "black"):
            match_totals[color].add(game_totals[color])
        match_illegal[0] += game_illegal[0]

        games_out.append({
            "game_index": game.get("game_index", game_index),
            "white": _finalize(game_totals["white"]),
            "black": _finalize(game_totals["black"]),
            "illegal_moves": game_illegal[0],
        })

    return {
        "match": {
            "white": _finalize(match_totals["white"]),
            "black": _finalize(match_totals["black"]),
            "illegal_moves": match_illegal[0],
        },
        "games": games_out,
    }
