// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

/**
 * Pure-stdlib "compute-on-read" aggregates for an OGXM-JSON match dict.
 *
 * Computes PR / error / decision / luck / illegal-move aggregates from the
 * per-ply ``analysis`` records OGXM stores. No engine calls; the only
 * in-repo import is the sibling pure-stdlib ``./met.js`` (the shipped MET,
 * for engine-free equity->MWC conversion).
 *
 * White = Player 1, Black = Player 2 (mat-file convention).
 * A ply belongs to white if ``color == 1``, black if ``color == 0``.
 *
 * Luck / luck_mwc: ``luck`` is a single field stored directly on a checker
 * ply's ``analysis`` (postroll - preroll at the luck-analyzer's level).
 * ``luck_mwc`` (aggregated as ``total_luck_mwc``) is computed engine-free via
 * the one shipped MET (``./met.js``, Kazaross-XG2): for a fixed
 * (score, cube), eq2mwc is affine in equity, so a *change* in equity
 * converts to a change in MWC via half its win/loss slope --
 * ``luck_mwc = luck * (mwc_on_win - mwc_on_loss) / 2``, where the anchors
 * come from ``mwcAnchors(away1, away2, cube_value, is_crawford)``. The
 * format carries no per-decision anchors any more (MWC is compute-on-read
 * everywhere), so this module derives (away1, away2, cube_value,
 * is_crawford) itself by parsing the ply's own ``ogid_before`` string.
 */

import { mwcAnchors } from './met.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const _MAX_CHECKER_ACTION_ID = 20;

const _CUBE_ACTION_IDS = new Set([21, 22, 23]);

const _COLOR_NAME = { 1: "white", 0: "black" };

// ---------------------------------------------------------------------------
// OGID field parsing: derive (away1, away2, cube_value, is_crawford) from a
// ply's own `ogid_before` string, so luck_mwc needs no stored per-decision
// anchor. Self-contained (doesn't import an OGID encoder; just splits the
// string this module's own OGID producer emits -- see ogid.js).
// ---------------------------------------------------------------------------

// OGID's match_length field: digits, then an optional single-letter suffix
// ("C" = Crawford, "G<n>" = fixed-games money session, "L" = post-Crawford)
// -- see ogid.js's encoder. Only "C" means Crawford.
const _OGID_MATCH_LENGTH_RE = /^(\d+)([A-Za-z].*)?$/;

/**
 * Parse an OGID string's cube/score/match_length fields.
 *
 * OGID field layout (colon-separated, see gvformat-js/src/ogid.js):
 * white_positions:black_positions:cube:dice:color:game_state:score_w:
 * score_b:match_length[:move_id[:nrof_checkers]]. The cube field is
 * <owner><exponent><action> (e.g. "N0N"); cube_value = 1 << exponent.
 * Returns {cube_value, score_w, score_b, match_length, is_crawford}, or
 * null if ogid is absent/malformed.
 */
function _parseOgidContext(ogid) {
  if (!ogid) return null;
  const parts = ogid.split(":");
  if (parts.length < 9) return null;
  if (parts[2] == null || parts[2].length < 2 || !/^\d$/.test(parts[2][1])) return null;
  const cubeExp = parseInt(parts[2][1], 10);
  const score_w = parseInt(parts[6], 10);
  const score_b = parseInt(parts[7], 10);
  if (Number.isNaN(score_w) || Number.isNaN(score_b)) return null;
  const cube_value = 1 << cubeExp;
  const m = _OGID_MATCH_LENGTH_RE.exec(parts[8]);
  if (!m) return null;
  const match_length = parseInt(m[1], 10);
  const is_crawford = (m[2] || "").startsWith("C");
  return { cube_value, score_w, score_b, match_length, is_crawford };
}

/**
 * Convert a *change in equity* on a ply into a change in MWC, engine-free
 * via the one shipped MET. Used for both per-ply luck (postroll - preroll)
 * and per-decision error (best - played): eq2mwc is affine in equity for a
 * fixed score/cube, so any delta maps via half the win/loss slope:
 * ``delta_mwc = delta * (mwc_on_win - mwc_on_loss) / 2``.
 *
 * Derives (away1, away2, cube_value, is_crawford) from the ply's own
 * ``ogid_before`` (the mover is ``ply.color`` -- NOT the OGID string's own
 * "color" field, which encodes the complement of on-roll). Returns null for
 * money games / an unparseable ogid_before.
 */
function _eqDeltaToMwc(ply, delta) {
  const ctx = _parseOgidContext(ply.ogid_before);
  if (ctx == null) return null;
  const { cube_value, score_w, score_b, match_length, is_crawford } = ctx;
  if (match_length <= 0) return null; // money game: no MET frame
  const away_w = match_length - score_w;
  const away_b = match_length - score_b;
  const mover_is_white = ply.color === 1;
  const away1 = mover_is_white ? away_w : away_b;
  const away2 = mover_is_white ? away_b : away_w;
  if (away1 <= 0 || away2 <= 0) return null;
  const [mwc_win, mwc_loss] = mwcAnchors(away1, away2, cube_value, is_crawford);
  return delta * (mwc_win - mwc_loss) / 2.0;
}

// ---------------------------------------------------------------------------
// Per-player accumulator
// ---------------------------------------------------------------------------

class Totals {
  constructor() {
    this.error = 0.0;
    this.error_mwc = 0.0;
    this.decisions = 0;
    this.cube_decisions = 0;
    this.luck = 0.0;
    this.luck_rolls = 0;
    this.luck_mwc = 0.0;
    this.has_mwc = false;
  }

  add(other) {
    this.error += other.error;
    this.error_mwc += other.error_mwc;
    this.decisions += other.decisions;
    this.cube_decisions += other.cube_decisions;
    this.luck += other.luck;
    this.luck_rolls += other.luck_rolls;
    this.luck_mwc += other.luck_mwc;
    this.has_mwc = this.has_mwc || other.has_mwc;
  }
}

function _new_pair() {
  return { white: new Totals(), black: new Totals() };
}

// ---------------------------------------------------------------------------
// Cube-decision triviality (ported from game_eval.py's _trivial_cube)
// ---------------------------------------------------------------------------

function _trivial_cube(nd, dt, dp) {
  return (
    Math.abs(nd - Math.min(dt, dp)) < 0.001 ||
    (nd - dt) > 0.200 ||
    (nd - dp) > 0.200 ||
    (nd < -0.900 && dt < -0.900)
  );
}

// ---------------------------------------------------------------------------
// Missed-double decision recompute
// ---------------------------------------------------------------------------

function _missed_double_counts(missed_double) {
  // The source recorded its own counted-ness (BGF: pr.cubeError); prefer it
  // over re-deriving, which cannot reproduce another engine's rule.
  if (missed_double.decision != null) return Boolean(missed_double.decision);
  const nd = missed_double.no_double_equity;
  const dt = missed_double.double_take_equity;
  const dp = missed_double.double_pass_equity;
  if (nd == null || dt == null || dp == null) {
    return true;
  }
  const doubler_err = Math.max(0.0, Math.min(dt, dp) - nd);
  const trivial = _trivial_cube(nd, dt, dp);
  return !(trivial && doubler_err < 0.001);
}

// ---------------------------------------------------------------------------
// Finalize a Totals into an output dict
// ---------------------------------------------------------------------------

function _finalize(t) {
  const pr = t.decisions > 0
    ? Math.round(t.error / t.decisions * 500.0 * 1000) / 1000
    : null;
  const out = {
    pr,
    total_error: Math.round(t.error * 10000) / 10000,
    total_decisions: t.decisions,
    cube_decisions: t.cube_decisions,
    total_luck: Math.round(t.luck * 10000) / 10000,
    luck_rolls: t.luck_rolls,
  };
  // total_luck_mwc is engine-free (met.js's mwcAnchors, derived from
  // ogid_before); absent for money games (no score/cube frame to anchor
  // MWC to), mirroring the old GVA summary (which omitted all MWC fields
  // for money play).
  if (t.has_mwc) {
    out.total_luck_mwc = Math.round(t.luck_mwc * 1000000) / 1000000;
    // MWC analog of total_error: per counted decision, equity_loss converts to
    // MWC lost via the same MET slope as luck (see _eqDeltaToMwc).
    out.total_error_mwc = Math.round(t.error_mwc * 1000000) / 1000000;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Cube sub-analysis accumulation
// ---------------------------------------------------------------------------

function _accumulate_cube_sub_analysis(ply, sub, t, counts) {
  if (!counts) return;
  const eqLoss = sub.equity_loss || 0.0;
  t.error += eqLoss;
  t.decisions += 1;
  t.cube_decisions += 1;
  // Embedded cube error in MWC terms uses the parent checker ply's anchors
  // (the no-double decision is at that ply's score/cube frame).
  const emwc = _eqDeltaToMwc(ply, eqLoss);
  if (emwc != null) {
    t.error_mwc += emwc;
    t.has_mwc = true;
  }
}

// ---------------------------------------------------------------------------
// Per-ply accumulation
// ---------------------------------------------------------------------------

function _accumulate_ply(ply, totals, illegal_counter) {
  const analysis = ply.analysis;
  if (analysis == null) return;

  const color = _COLOR_NAME[ply.color];
  if (color == null) return;
  const t = totals[color];
  const action_id = ply.action_id;

  if (_CUBE_ACTION_IDS.has(action_id)) {
    if (analysis.decision) {
      const eqLoss = analysis.equity_loss || 0.0;
      t.error += eqLoss;
      t.decisions += 1;
      t.cube_decisions += 1;
      const emwc = _eqDeltaToMwc(ply, eqLoss);
      if (emwc != null) {
        t.error_mwc += emwc;
        t.has_mwc = true;
      }
    }
    return;
  }

  if (action_id == null || action_id > _MAX_CHECKER_ACTION_ID) return;

  if (analysis.decision) {
    const eqLoss = analysis.equity_loss || 0.0;
    t.error += eqLoss;
    t.decisions += 1;
    const emwc = _eqDeltaToMwc(ply, eqLoss);
    if (emwc != null) {
      t.error_mwc += emwc;
      t.has_mwc = true;
    }
  }

  // Luck: a single stored field (postroll - preroll at the luck eval
  // level). Summed for every rolled ply, independent of `decision`.
  const luck_val = analysis.luck;
  if (luck_val != null) {
    t.luck += luck_val;
    t.luck_rolls += 1;
    const lmwc = _eqDeltaToMwc(ply, luck_val);
    if (lmwc != null) {
      t.luck_mwc += lmwc;
      t.has_mwc = true;
    }
  }

  const cube_decision = analysis.cube_decision;
  if (cube_decision != null) {
    _accumulate_cube_sub_analysis(ply, cube_decision, t, Boolean(cube_decision.decision));
  }

  const missed_double = analysis.missed_double;
  if (missed_double != null) {
    _accumulate_cube_sub_analysis(ply, missed_double, t, _missed_double_counts(missed_double));
  }

  // There is deliberately no third source of cube decisions here. A cube
  // decision with no stored evaluation cannot be represented in a `.gvab` --
  // a CUBE record *is* its three equities -- so counting one would make these
  // totals depend on something the format cannot carry, and a saved match
  // would disagree with the same match on screen. See `bgf2gva.js`, which
  // drops BGBlitz's marker for exactly that reason.

  // Illegal-move flag. `analysis.illegal_move` is the location this repo
  // writes and the only one a .gvab can carry (one flag bit per ply, decoded
  // back onto `analysis` by reader.js); the other two are read for foreign
  // files, and the `else` keeps a file that sets both from counting twice.
  // It does fire on real matches -- see test/test-illegal-move.js, which
  // checks the corpus match that has one against the same count Python gets.
  if (analysis.illegal_move || ply.illegal_move) {
    illegal_counter[0] += 1;
  } else {
    const alternatives = analysis.alternatives;
    if (alternatives) {
      for (const alt of alternatives) {
        if (alt.illegal_move) {
          illegal_counter[0] += 1;
          break;
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Compute PR / error / decision / luck / illegal-move aggregates.
 *
 * @param {object} ogxm - The dict produced by ogxm_export.to_ogxm_json(...)
 * @returns {{ match: { white: object, black: object, illegal_moves: number },
 *             games: Array<{ game_index: number, white: object, black: object,
 *                            illegal_moves: number }> }}
 */
export function compute_aggregates(ogxm) {
  const match_totals = _new_pair();
  const match_illegal = [0];
  const games_out = [];

  const games = ogxm.games || [];
  for (let game_index = 0; game_index < games.length; game_index++) {
    const game = games[game_index];
    const game_totals = _new_pair();
    const game_illegal = [0];

    const plies = game.plies || [];
    for (const ply of plies) {
      _accumulate_ply(ply, game_totals, game_illegal);
    }

    for (const color of ["white", "black"]) {
      match_totals[color].add(game_totals[color]);
    }
    match_illegal[0] += game_illegal[0];

    games_out.push({
      game_index: game.game_index != null ? game.game_index : game_index,
      white: _finalize(game_totals.white),
      black: _finalize(game_totals.black),
      illegal_moves: game_illegal[0],
    });
  }

  return {
    match: {
      white: _finalize(match_totals.white),
      black: _finalize(match_totals.black),
      illegal_moves: match_illegal[0],
    },
    games: games_out,
  };
}
