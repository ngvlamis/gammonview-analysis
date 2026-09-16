// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Completing an analysis block that was written without the [GV] extensions.
//
// OGXM is HedgeHog's format and ours extends it. Our GVAN chunk carries the
// fields the base spec has no room for: whether a ply counted as a *decision*,
// per-ply luck, and the engine's own name for the level it searched at. A file
// from any other producer -- a `.ogxm` off hedgehog-bg.com, say -- has no GVAN,
// and a reader that takes its absence literally reports "not a decision" for
// every ply in the match. That is not what the file says. The file says
// nothing, and a performance rating over an empty denominator is worse than no
// rating at all.
//
// So a block with no GVAN is completed here, from what the base format *does*
// carry. Two separate things are wrong until it is:
//
//   * **Units.** In match play the base spec stores the three cube values as
//     raw MWC -- "because that is the unit the analyser decides in" -- and
//     offers normalized equity as a derived view. We store the normalized view,
//     where dropping the cube is -1 and cashing it is +1. The map between them
//     is affine and both anchors are the pre-decision stake at this score,
//     which the ply's own replayed OGID already carries.
//
//   * **Decisions.** Which plies counted has to be derived rather than read: a
//     checker play counts when its candidates actually disagree, a cube when it
//     is not trivial. These are the rules `xg.py`, `bgf.py` and `og2gva.js`
//     already apply to sources that record no flag of their own, and they are
//     restated rather than imported so this module stands alone -- the same
//     bargain those three struck with each other.
//
// **Luck is not here, and cannot be.** It is the pre-roll position's equity
// averaged over all 21 rolls against what the roll actually gave, and no amount
// of reading tells you the first number -- it takes an engine. A block filled
// here therefore still has no luck, deliberately: see `gvanalysis`'s luck pass,
// which is what supplies it.

import { mwcAnchors } from './met.js';

// Checker plays sit at action ids 0-20; 21-23 are the cube.
const _MAX_CHECKER_ACTION_ID = 20;
const _TAKE_PASS_ACTIONS = new Set([22, 23]);

// Mirrors gvformat.xg._CHECKER_SPREAD_EPS / xg2gva's CHECKER_SPREAD_EPS.
const CHECKER_SPREAD_EPS = 1e-4;

// Mirrors gvanalysis.game_eval._trivial_cube / xg2gva's trivialCube.
function trivialCube(nd, dt, dp) {
  return (
    Math.abs(nd - Math.min(dt, dp)) < 0.001
    || (nd - dt) > 0.200
    || (nd - dp) > 0.200
    || (nd < -0.900 && dt < -0.900)
  );
}

// Mirrors gvanalysis.game_eval._trivial_take_pass / xg2gva's trivialTakePass.
function trivialTakePass(dt, dp) {
  return Math.abs(dt - dp) < 0.001;
}

// ---------------------------------------------------------------------------
// The score frame a ply was played at
// ---------------------------------------------------------------------------

// OGID's match_length field: digits, then an optional single-letter suffix
// ("C" = Crawford, "G<n>" = fixed-games money session, "L" = post-Crawford).
const _OGID_MATCH_LENGTH_RE = /^(\d+)([A-Za-z].*)?$/;

/** The cube/score context of `ogid`, or null when it is absent or malformed.
 *
 *  Deliberately the same parse `stats.js` does, off the same field layout, for
 *  the same reason: the frame a ply was decided in is in the ply, and nothing
 *  needs to store it a second time. */
function _parseOgidContext(ogid) {
  if (!ogid) return null;
  const parts = String(ogid).split(':');
  if (parts.length < 9) return null;
  if (parts[2] == null || parts[2].length < 2 || !/^\d$/.test(parts[2][1])) return null;
  const cubeValue = 1 << parseInt(parts[2][1], 10);
  const scoreW = parseInt(parts[6], 10);
  const scoreB = parseInt(parts[7], 10);
  if (Number.isNaN(scoreW) || Number.isNaN(scoreB)) return null;
  const m = _OGID_MATCH_LENGTH_RE.exec(parts[8]);
  if (!m) return null;
  return {
    cubeValue,
    scoreW,
    scoreB,
    matchLength: parseInt(m[1], 10),
    isCrawford: (m[2] || '').startsWith('C'),
  };
}

/**
 * The affine map from this ply's MWC onto its normalized-equity frame, or null
 * where there is no frame to map onto (money play, an unreplayable ply).
 *
 * The three cube values are the **doubler's** throughout, so on a take or a
 * pass the frame belongs to the other player -- the ply's own colour is the one
 * answering the cube, not the one who offered it.
 */
function _normalizer(ply) {
  const ctx = _parseOgidContext(ply.ogid_before);
  if (ctx == null) return null;
  if (ctx.matchLength <= 0) return null; // money play: already equity
  const awayW = ctx.matchLength - ctx.scoreW;
  const awayB = ctx.matchLength - ctx.scoreB;
  const answering = _TAKE_PASS_ACTIONS.has(ply.action_id);
  const doublerIsWhite = answering ? ply.color !== 1 : ply.color === 1;
  const away1 = doublerIsWhite ? awayW : awayB;
  const away2 = doublerIsWhite ? awayB : awayW;
  if (away1 <= 0 || away2 <= 0) return null;
  const [mwcWin, mwcLose] = mwcAnchors(away1, away2, ctx.cubeValue, ctx.isCrawford);
  const span = mwcWin - mwcLose;
  if (span === 0) return null;
  const mid = (mwcWin + mwcLose) / 2;
  return (mwc) => Math.round((2 * (mwc - mid) / span) * 10000) / 10000;
}

/** The three cube values a payload carries, if it carries them. */
function _cubeValues(sub) {
  if (sub == null || sub.no_double_equity == null) return null;
  return [sub.no_double_equity, sub.double_take_equity, sub.double_pass_equity];
}

/**
 * Are this block's cube values raw MWC, or are they already equity?
 *
 * The GVAN test above says the block is not ours, and nothing but ours writes
 * the normalized view into those slots -- so in practice the answer is always
 * "MWC". This is the belt to that braces, and it is decidable rather than a
 * guess: an MWC is a probability and cannot leave [0, 1], while a normalized
 * equity leaves it constantly (a doubler who is behind is negative, and a take
 * that loses ground runs past -1). One value outside the unit interval is
 * therefore proof the block is already converted, and no MWC block can produce
 * one. The reverse is not proof -- a match whose every cube decision favoured
 * the doubler would sit inside [0, 1] in either unit -- which is why this is
 * the second test and not the first.
 */
function _valuesAreMwc(blockObj) {
  let seen = false;
  for (const analysis of blockObj.values()) {
    for (const sub of [analysis, analysis.cube_decision, analysis.missed_double]) {
      const values = _cubeValues(sub);
      if (values === null) continue;
      seen = true;
      for (const v of values) {
        if (!(v >= 0 && v <= 1)) return false;
      }
    }
  }
  return seen;
}

/** Rewrite one cube payload's three values through `toEquity`, in place. */
function _normalizeCube(sub, toEquity) {
  if (sub == null || sub.no_double_equity == null) return;
  sub.no_double_equity = toEquity(sub.no_double_equity);
  sub.double_take_equity = toEquity(sub.double_take_equity);
  sub.double_pass_equity = toEquity(sub.double_pass_equity);
}

// ---------------------------------------------------------------------------
// Deriving the decision flags
// ---------------------------------------------------------------------------

/** Did this checker play pose a decision at all?
 *
 *  A forced move and a position where every candidate scores the same are not
 *  decisions, and an illegal play is excluded outright -- the player did not
 *  choose among these moves, they broke the rules. */
function _checkerIsDecision(analysis) {
  if (analysis.illegal_move) return false;
  const alts = analysis.alternatives;
  if (!Array.isArray(alts) || alts.length < 2) return false;
  let lo = Infinity;
  let hi = -Infinity;
  for (const alt of alts) {
    const eq = Number(alt.equity);
    if (!Number.isFinite(eq)) continue;
    if (eq < lo) lo = eq;
    if (eq > hi) hi = eq;
  }
  return hi - lo >= CHECKER_SPREAD_EPS;
}

/** The flag for a cube ply of its own (action 21-23), on the normalized scale. */
function _cubePlyIsDecision(ply, analysis) {
  const nd = analysis.no_double_equity;
  const dt = analysis.double_take_equity;
  const dp = analysis.double_pass_equity;
  if (nd == null || dt == null || dp == null) return false;
  if (_TAKE_PASS_ACTIONS.has(ply.action_id)) return !trivialTakePass(dt, dp);
  return !(trivialCube(nd, dt, dp) && (analysis.equity_loss || 0) < 0.001);
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/**
 * Complete one analysis block in place, given the plies it describes.
 *
 * @param {Map<string, object>} blockObj  "gameIndex,plyIndex" -> analysis
 * @param {Map<string, object>} plyByKey  the same keys -> the ply itself
 * @param {object} analysisInfo           the block's own header, also completed
 */
export function completeBaseBlock(blockObj, plyByKey, analysisInfo) {
  const plyDepths = new Set();
  const mwc = _valuesAreMwc(blockObj);

  for (const [key, analysis] of blockObj) {
    const ply = plyByKey.get(key);
    if (ply === undefined) continue;

    // Units first: every threshold below is stated in normalized equity, and
    // reading a raw MWC as one would call almost every cube trivial.
    const toEquity = mwc ? _normalizer(ply) : null;
    if (toEquity !== null) {
      _normalizeCube(analysis, toEquity);
      _normalizeCube(analysis.cube_decision, toEquity);
      _normalizeCube(analysis.missed_double, toEquity);
    }

    if (ply.action_id != null && ply.action_id <= _MAX_CHECKER_ACTION_ID) {
      analysis.decision = _checkerIsDecision(analysis);
    } else if (analysis.no_double_equity != null) {
      analysis.decision = _cubePlyIsDecision(ply, analysis);
    }

    // The live cube above a checker play: not itself an error, so triviality is
    // the whole test (og2gva applies the same one to the same shape).
    const live = analysis.cube_decision;
    if (live != null && live.no_double_equity != null) {
      live.decision = !trivialCube(
        live.no_double_equity, live.double_take_equity, live.double_pass_equity);
    }
    // A `missed_double` deliberately keeps no flag of its own: `stats.js`
    // re-derives one from its three equities whenever the source recorded none,
    // and that rule already accounts for the doubler's own error.

    if (analysis.ply) plyDepths.add(analysis.ply);
  }

  // The base spec lets a decision be searched deeper than its block, and the
  // two fields carrying that are alternatives, not a pair: the header's `ply`
  // is a default, and a per-decision `ply` of 0 means "use it". So a producer
  // can put the depth in either place, and real files use both -- HedgeHog
  // stamps every entry and leaves the header at 0 for a plain run, then does
  // the exact reverse for a block of re-run decisions. Read whichever one
  // speaks, so the block can say how deep it looked either way.
  if (analysisInfo != null) {
    let depth = plyDepths.size === 1 ? plyDepths.values().next().value : null;
    if (depth == null && plyDepths.size === 0) depth = analysisInfo.ply || null;
    if (depth != null) {
      if (!analysisInfo.ply) analysisInfo.ply = depth;
      if (analysisInfo.eval_level == null) analysisInfo.eval_level = `${depth}ply`;
    }
  }
}

export { CHECKER_SPREAD_EPS, trivialCube, trivialTakePass };
