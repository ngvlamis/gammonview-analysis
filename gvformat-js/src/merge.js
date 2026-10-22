// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// JavaScript ESM port of gvformat/merge.py — append analysis onto an OGXM
// document, preserving any it already carries.
//
// The OGXM format allows up to MAX_ANALYSES analysis blocks per match (see the
// binary spec's ANAL groups / the JSON spec's `analyses_info` + per-ply
// `analyses[]`). `appendAnalysis` merges one document's analysis into another,
// so a match can gather evaluations from several engines without dropping one.
//
// The base document's match body (games, plies, OGIDs, orientation, clock) is
// kept **verbatim**; only per-ply `analysis`/`analyses` and the top-level
// analysis metadata change. `our`'s analysis objects are transplanted onto the
// base's plies by decision-ply order (a decision ply is action_id 0-23, or a beaver
// answering a double; terminal and set-position plies never carry analysis).
//
// Both documents must share orientation (same `player_white`) — analysis move
// steps live in the absolute frame, so a mismatch would mirror them. This is
// asserted rather than repaired: mirroring an analysis means flipping point
// numbers, probability vectors, equity signs and cube fields, and a caller that
// hits this is better served storing the two documents side by side.
//
// ## Differences from the Python original
//
// Python's `append_analysis` appends exactly one block (it reads `our`'s
// singular `analysis_info`). This port appends **every** block `our` carries,
// which is what merging a user-supplied multi-analysis file requires. It also
// treats an `our` with no analysis at all as a no-op rather than appending an
// empty block, and rejects a game-count mismatch instead of silently zipping to
// the shorter document.

import { MAX_ANALYSES } from './constants.js';

/** The plies of one game that can carry an analysis, in order: a checker or cube
 *  action (id 0-23), a *restated play* -- a set-position ply (31) carrying the
 *  roll's dice, which is an illegal play no dice ply could encode (see
 *  `setPositionPly` in export.js) -- and the ply that answers a double with a
 *  beaver (32) or a raccoon (33), which is where the engine's response lands.
 *  Both documents reach such a ply by the same route, so both list it here and
 *  the pairing stays 1:1: `our` has a take where the base has a beaver.
 *
 *  A beaver is answered at most once: the raccoon that follows it answers the
 *  beaver, not a double, and the engine has no decision for it. A resignation
 *  (27, 28) is a decision -- whether to concede, and whether to accept -- but
 *  not every source records one (see `_aligned`). The other terminal plies
 *  (24-26, 29, 30) hold no decision at all, and a set-position ply with *no*
 *  dice states where a game starts rather than a turn taken. */
function _decisionPlies(game) {
  const out = [];
  let pending = false;                   // a double that has not been answered
  for (const ply of game.plies || []) {
    const aid = ply.action_id;
    if (aid === null || aid === undefined) continue;
    if (aid === 21) {
      pending = true;
      out.push(ply);
    } else if (aid === 22 || aid === 23) {
      pending = false;
      out.push(ply);
    } else if (aid === 32 || aid === 33) {
      if (pending) out.push(ply);
      pending = false;
    } else if ((aid >= 0 && aid <= 20) || aid === 27 || aid === 28   // 27/28: a resignation
               || (aid === 31 && ply.d1 !== null && ply.d1 !== undefined)) {
      out.push(ply);
    }
  }
  return out;
}

function _isResign(ply) {
  return ply.action_id === 27 || ply.action_id === 28;
}

/** Pair each base decision ply with the corresponding `our` analysis (via
 *  `select`, null when that ply has nothing to report).
 *
 *  Checker plays and cube actions pair 1:1, and a different count of them is
 *  an error: the two documents are not the same match. Resignations pair only
 *  when both record the same number of them. Sources differ there -- one
 *  converter records a game's resignation as plies and another only as the
 *  game's result -- so a resignation one document has and the other lacks is
 *  left without the other's analysis, as every resignation was before they
 *  counted as decisions, rather than failing the whole merge. */
function _aligned(baseGame, ourGame, select) {
  const base = _decisionPlies(baseGame);
  const our = _decisionPlies(ourGame);
  const basePlays = base.filter((p) => !_isResign(p));
  const ourPlays = our.filter((p) => !_isResign(p));
  if (basePlays.length !== ourPlays.length) {
    throw new Error(
      `analysis/ply count mismatch in game ${baseGame.game_index}: `
      + `${basePlays.length} base decision plies vs ${ourPlays.length} analyses`);
  }
  const pairs = basePlays.map((ply, i) => [ply, select(ourPlays[i])]);
  const baseResigns = base.filter(_isResign);
  const ourResigns = our.filter(_isResign);
  if (baseResigns.length === ourResigns.length) {
    baseResigns.forEach((ply, i) => pairs.push([ply, select(ourResigns[i])]));
  }
  return pairs;
}

/** Rewrite a single-analysis base in place to explicit multi form: move its
 *  `analysis_info` into `analyses_info[0]` and each ply's `analysis` into a
 *  one-element `analyses` (tagged analysis_index=0). The single-form mirrors
 *  (`analysis_info` + per-ply `analysis`) are kept as the primary. */
function _promoteToMulti(base) {
  base.analyses_info = [base.analysis_info];
  for (const game of base.games || []) {
    for (const ply of game.plies || []) {
      const a = ply.analysis;
      if (a !== null && a !== undefined) {
        ply.analyses = [{ ...a, analysis_index: 0 }];
      }
    }
  }
}

/** An analysis object as it appears in single-analysis form: no analysis_index,
 *  which only exists to tag entries inside a multi-analysis `analyses[]`. */
function _stripIndex(analysis) {
  const { analysis_index, ...rest } = analysis;
  return rest;
}

/** The analysis blocks `our` carries, as [[info, select(ply)], ...] in order.
 *  Empty when `our` has no analysis. */
function _ourBlocks(our) {
  const analysesInfo = our.analyses_info;
  if (Array.isArray(analysesInfo) && analysesInfo.length) {
    return analysesInfo.map((info, k) => [
      info || {},
      (ply) => {
        for (const a of ply.analyses || []) {
          if (a.analysis_index === k) return a;
        }
        return null;
      },
    ]);
  }
  const info = our.analysis_info;
  const hasPlyAnalysis = (our.games || []).some(
    g => (g.plies || []).some(p => p.analysis !== null && p.analysis !== undefined));
  if ((info !== null && typeof info === 'object') || hasPlyAnalysis) {
    return [[
      (info !== null && typeof info === 'object') ? info : {},
      (ply) => (ply.analysis !== undefined ? ply.analysis : null),
    ]];
  }
  return [];
}

/**
 * Return a copy of `base` with every analysis `our` carries appended.
 *
 * - `base` carries **no** analysis and `our` carries one: the result is
 *   single-analysis (legacy shape) — `our`'s analysis objects on the plies,
 *   `our`'s `analysis_info` at the top. Byte-for-byte the same as analyzing the
 *   match directly.
 * - Otherwise the result is multi-analysis — `analyses_info` lists all blocks
 *   (existing first, `our`'s last) and each decision ply gains an entry in its
 *   `analyses` array. The existing primary (index 0) stays mirrored in
 *   `analysis_info` / per-ply `analysis` for naive single-analysis readers.
 *
 * Throws on an orientation mismatch, a game-count or decision-ply-count
 * mismatch, or overflowing MAX_ANALYSES.
 */
export function appendAnalysis(base, our) {
  if (base.player_white !== our.player_white) {
    throw new Error(
      `orientation mismatch: base player_white=${JSON.stringify(base.player_white)} `
      + `vs our=${JSON.stringify(our.player_white)}`);
  }

  const out = structuredClone(base);
  const ourBlocks = _ourBlocks(our);
  if (!ourBlocks.length) return out;

  const baseGames = out.games || [];
  const ourGames = our.games || [];
  if (baseGames.length !== ourGames.length) {
    throw new Error(
      `game count mismatch: base has ${baseGames.length}, ours has ${ourGames.length}`);
  }
  const pairs = baseGames.map((bg, i) => [bg, ourGames[i]]);

  let hasExisting = out.analyses_info !== undefined || out.analysis_info !== undefined;

  for (const [info, select] of ourBlocks) {
    if (!hasExisting) {
      // Single-analysis output: attach directly.
      for (const [bg, og] of pairs) {
        for (const [basePly, analysis] of _aligned(bg, og, select)) {
          if (analysis !== null && analysis !== undefined) {
            basePly.analysis = _stripIndex(analysis);
          }
        }
      }
      out.analysis_info = { ...info };
      hasExisting = true;
      continue;
    }

    // Multi-analysis output. Normalize to explicit multi form first.
    if (out.analyses_info === undefined) _promoteToMulti(out);

    if (out.analyses_info.length >= MAX_ANALYSES) {
      throw new Error(
        `cannot append: base already has ${out.analyses_info.length} analyses `
        + `(format cap is ${MAX_ANALYSES})`);
    }

    const k = out.analyses_info.length;
    out.analyses_info.push({ ...info });
    for (const [bg, og] of pairs) {
      for (const [basePly, analysis] of _aligned(bg, og, select)) {
        if (analysis !== null && analysis !== undefined) {
          if (basePly.analyses === undefined) basePly.analyses = [];
          basePly.analyses.push({ ...analysis, analysis_index: k });
        }
      }
    }
  }

  return out;
}

/** How many analysis blocks a parsed document carries. */
export function analysisCount(ogxm) {
  const analysesInfo = ogxm.analyses_info;
  if (Array.isArray(analysesInfo)) return analysesInfo.length;
  return ogxm.analysis_info !== undefined ? 1 : 0;
}

export { MAX_ANALYSES };
