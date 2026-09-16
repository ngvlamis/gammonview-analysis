// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis
//
// Portions of this file are ported from HedgeHog's C++ codec
// (MIT, Copyright (c) 2026 Eran Lambooij). See THIRD-PARTY-NOTICES.md,
// whose notices must be preserved in copies of this file.

// ESM port of gvformat/export.py — GVA analysis result dicts -> OGXM-JSON.
// Pure conversion layer: no bgsage / engine calls.

import { RESIGN_ACTIONS } from "./constants.js";
import { boardToOgid } from "./ogid.js";
import { cleanPlace } from "./place.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Standard starting position, Player-1/White perspective. */
export const _STARTING_BOARD_P1 = [
  0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5,
  5, 0, 0, 0, -3, 0, -5, 0, 0, 0, 0, 2, 0,
];

const _DICE_PAIRS = [];
for (let d1 = 1; d1 <= 6; d1++) {
  for (let d2 = d1; d2 <= 6; d2++) {
    _DICE_PAIRS.push([d1, d2]);
  }
}
const _DICE_ACTION_ID = new Map(_DICE_PAIRS.map((p, i) => [`${p[0]},${p[1]}`, i]));

// OGID turn-phase state/action string constants
export const _OGID_STATE_INITIAL_BOTH = "IB";
export const _OGID_STATE_ROLLED = "R";
export const _OGID_STATE_CHECKER_DONE = "C";
export const _OGID_STATE_DOUBLE_OFFERED = "D";
export const _OGID_STATE_AFTER_TAKE = "A";
export const _OGID_STATE_GAME_OVER = "G";

export const _OGID_ACTION_NONE = "N";
export const _OGID_ACTION_DOUBLE = "O";
export const _OGID_ACTION_TAKE = "T";
export const _OGID_ACTION_PASS = "P";

const _OGID_CUBE_CENTERED = "N";
export const _OGID_CUBE_WHITE = "W";
export const _OGID_CUBE_BLACK = "B";

const _TRUNCATED_DISPLAY_TO_CANON = { "1T": "truncated1", "2T": "truncated2", "3T": "truncated3" };
const _HYPHEN_PLY_RE = /^(\d)-ply$/i;
const _CANONICAL_EVAL_LEVELS = new Set([
  "1ply", "2ply", "3ply", "4ply",
  "truncated1", "truncated2", "truncated3", "rollout",
]);

// ---------------------------------------------------------------------------
// Eval-level normalisation
// ---------------------------------------------------------------------------

function _normalizeEvalLevel(raw) {
  if (raw == null) return null;
  const s = String(raw);
  if (_CANONICAL_EVAL_LEVELS.has(s)) return s;
  if (s in _TRUNCATED_DISPLAY_TO_CANON) return _TRUNCATED_DISPLAY_TO_CANON[s];
  if (s.trim().toLowerCase() === "rollout") return "rollout";
  const m = s.trim().match(_HYPHEN_PLY_RE);
  if (m) return `${m[1]}ply`;
  return s;
}

function _evalDepth(evalLevel) {
  if (!evalLevel) return 0;
  const m = String(evalLevel).match(/(\d)/);
  return m ? parseInt(m[1], 10) : 0;
}

// ---------------------------------------------------------------------------
// Small pure helpers
// ---------------------------------------------------------------------------

export function _flipBoard(board) {
  const flipped = new Array(26).fill(0);
  flipped[0] = board[25];
  flipped[25] = board[0];
  for (let i = 1; i <= 24; i++) {
    flipped[25 - i] = -board[i];
  }
  return flipped;
}

/** Sort key for the canonical white/black rule: case-insensitive,
 * Unicode-aware primary order, raw-string tiebreak for names differing
 * only by case. */
export function _canonicalKey(name) {
  return [name.toLowerCase(), name];
}

function _keyLess(a, b) {
  if (a[0] !== b[0]) return a[0] < b[0];
  return a[1] < b[1];
}

/** Canonical (white, black, name1IsWhite) for two source player names.
 * White = the alphabetically-first name by _canonicalKey. A tie
 * (byte-identical names) keeps name1 as white (source-order fallback). */
export function _canonicalOrientation(name1, name2) {
  if (_keyLess(_canonicalKey(name2), _canonicalKey(name1))) {
    return [name2, name1, false];
  }
  return [name1, name2, true];
}

/** Return a copy of `game` with every board mirrored via _flipBoard and
 * score_start's player1/player2 swapped. Used when canonical white is the
 * source's player-2: every board in the input is in a fixed player-1/white
 * frame, so flipping every board here is what makes the rest of the
 * (unmodified) converter -- which assumes player-1 == white -- produce
 * canonical output. Leaves entry.player (a name) and move-notation strings
 * untouched (name-keyed / mover-relative, not frame-dependent). */
export function _flipGameOrientation(game) {
  const newGame = { ...game };
  if (game.score_start) {
    newGame.score_start = {
      player1: game.score_start.player2,
      player2: game.score_start.player1,
    };
  }
  newGame.moves = (game.moves || []).map(entry => {
    const newEntry = { ...entry };
    if ("board_before" in entry) newEntry.board_before = _flipBoard(entry.board_before);
    if ("board_after" in entry) newEntry.board_after = _flipBoard(entry.board_after);
    if ("board" in entry) newEntry.board = _flipBoard(entry.board);
    return newEntry;
  });
  return newGame;
}

function _cubeActionLabel(shouldDouble, doubleTakeEquity, doublePassEquity) {
  if (!shouldDouble) return "no_double";
  return doublePassEquity >= doubleTakeEquity ? "double_take" : "double_pass";
}

function _probsToEval(probs) {
  const [win, gwin, bgwin, gloss, bgloss] = probs;
  const equity = win + gwin + bgwin - gloss - bgloss;
  return {
    win, gammon_win: gwin, bg_win: bgwin,
    gammon_loss: gloss, bg_loss: bgloss, equity: Math.round(equity * 10000) / 10000,
  };
}

export function _diceActionId(d1, d2) {
  const lo = Math.min(d1, d2);
  const hi = Math.max(d1, d2);
  return _DICE_ACTION_ID.get(`${lo},${hi}`);
}

// ---------------------------------------------------------------------------
// Structured move steps: board-diff -> spans -> single-die hops
// ---------------------------------------------------------------------------

function _hitPoints(before, after) {
  const pts = new Set();
  for (let i = 1; i <= 24; i++) {
    if (before[i] < 0 && (after[i] >= 0 || after[i] > before[i])) {
      pts.add(i);
    }
  }
  return pts;
}

function _matchedSpans(before, after, d1, d2) {
  const fromPts = [];
  const toPts = [];

  const barDiff = after[25] - before[25];
  if (barDiff < 0) {
    for (let k = 0; k < -barDiff; k++) fromPts.push(25);
  }

  for (let i = 1; i <= 24; i++) {
    let wb = before[i] > 0 ? before[i] : 0;
    let wa = after[i] > 0 ? after[i] : 0;
    if (before[i] < 0 && after[i] > 0) {
      wa = after[i]; wb = 0;
    } else if (before[i] > 0 && after[i] < 0) {
      wb = before[i]; wa = 0;
    }
    const diff = wa - wb;
    if (diff > 0) {
      for (let k = 0; k < diff; k++) toPts.push(i);
    } else if (diff < 0) {
      for (let k = 0; k < -diff; k++) fromPts.push(i);
    }
  }

  const onBefore = before[25] + before.slice(1, 25).reduce((s, v) => s + (v > 0 ? v : 0), 0);
  const onAfter = after[25] + after.slice(1, 25).reduce((s, v) => s + (v > 0 ? v : 0), 0);
  const borneOff = onBefore - onAfter;
  for (let k = 0; k < borneOff; k++) toPts.push(0);

  fromPts.sort((a, b) => b - a);
  toPts.sort((a, b) => b - a);

  const dice = d1 === d2 ? [d1, d1, d1, d1] : [d1, d2];
  const spans = [];
  const usedFrom = new Array(fromPts.length).fill(false);
  const usedTo = new Array(toPts.length).fill(false);
  const usedDie = new Array(dice.length).fill(false);

  for (let di = 0; di < dice.length; di++) {
    const d = dice[di];
    if (usedDie[di]) continue;
    let matched = false;
    for (let fi = 0; fi < fromPts.length; fi++) {
      if (usedFrom[fi]) continue;
      const f = fromPts[fi];
      const expected = f === 25 ? (25 - d) : (f - d);
      for (let ti = 0; ti < toPts.length; ti++) {
        if (usedTo[ti]) continue;
        const t = toPts[ti];
        if (t === expected || (expected <= 0 && t === 0)) {
          spans.push([f, t]);
          usedFrom[fi] = usedTo[ti] = usedDie[di] = true;
          matched = true;
          break;
        }
      }
      if (matched) break;
    }
  }

  for (let fi = 0; fi < fromPts.length; fi++) {
    if (usedFrom[fi]) continue;
    for (let ti = 0; ti < toPts.length; ti++) {
      if (usedTo[ti]) continue;
      spans.push([fromPts[fi], toPts[ti]]);
      usedFrom[fi] = usedTo[ti] = true;
      break;
    }
  }

  return spans;
}

function _spanDistance(f, t) {
  return f === 25 ? (25 - t) : (f - t);
}

function _splitSpanDouble(f, t, die) {
  const dist = _spanDistance(f, t);
  if (dist <= 0) return [[f, die]];
  if (dist % die !== 0) return [[f, dist]];
  const n = dist / die;
  const hops = [];
  let cur = f;
  for (let k = 0; k < n; k++) {
    hops.push([cur, die]);
    cur = cur === 25 ? (25 - die) : (cur - die);
  }
  return hops;
}

function _splitSpanNondouble(f, t, d1, d2, board, hitPoints) {
  const dist = _spanDistance(f, t);
  if (dist === d1) return [[f, d1]];
  if (dist === d2) return [[f, d2]];
  if (dist === d1 + d2) {
    const mid1 = f === 25 ? (25 - d1) : (f - d1);
    const mid2 = f === 25 ? (25 - d2) : (f - d2);
    // Canonical default order (larger die first) -- independent of whether
    // the caller passed (d1, d2) or (d2, d1), so callers that report dice
    // in different orders agree on this arbitrary tie-break instead of
    // silently picking different intermediate stops for the identical
    // physical move (see gvformat/export.py's _split_span_nondouble).
    let order = d1 >= d2 ? [[d1, d2, mid1], [d2, d1, mid2]] : [[d2, d1, mid2], [d1, d2, mid1]];
    if (hitPoints) {
      const hitOrder = order.filter(o => o[2] >= 1 && o[2] <= 24 && hitPoints.has(o[2]));
      if (hitOrder.length) {
        order = hitOrder;
      } else if (board) {
        const legal = order.filter(o => o[2] >= 1 && o[2] <= 24 && board[o[2]] > -2);
        if (legal.length) order = legal;
      }
    } else if (board) {
      const legal = order.filter(o => o[2] >= 1 && o[2] <= 24 && board[o[2]] > -2);
      if (legal.length) order = legal;
    }
    const [firstD, secondD, mid] = order[0];
    return [[f, firstD], [mid, secondD]];
  }
  return dist > 0 ? [[f, dist]] : [];
}

function _moverToAbs(pointMover, moverIsWhite) {
  return moverIsWhite ? (25 - pointMover) : pointMover;
}

export function _computeMoveSteps(boardBeforeP1, boardAfterP1, moverIsWhite, d1, d2) {
  const mbBefore = moverIsWhite ? boardBeforeP1 : _flipBoard(boardBeforeP1);
  const mbAfter = moverIsWhite ? boardAfterP1 : _flipBoard(boardAfterP1);
  const spans = _matchedSpans(mbBefore, mbAfter, d1, d2);
  const hitPoints = _hitPoints(mbBefore, mbAfter);

  const steps = [];
  for (const [f, t] of spans) {
    const hops = d1 === d2
      ? _splitSpanDouble(f, t, d1)
      : _splitSpanNondouble(f, t, d1, d2, mbBefore, hitPoints);
    if (d1 !== d2 && hops.length === 2) {
      hitPoints.delete(hops[1][0]);
    }
    for (const [hf, pips] of hops) {
      steps.push({ from: _moverToAbs(hf, moverIsWhite), pips });
    }
  }
  return steps;
}

function _parseNotation(notation) {
  const spans = [];
  if (!notation) return spans;
  for (const raw of notation.split(/\s+/)) {
    let token = raw.trim();
    if (!token) continue;
    let count = 1;
    if (token.includes("(") && token.endsWith(")")) {
      const parenIdx = token.lastIndexOf("(");
      const base = token.slice(0, parenIdx);
      const cnt = token.slice(parenIdx + 1, -1);
      token = base;
      if (/^\d+$/.test(cnt)) count = Math.max(1, parseInt(cnt, 10));
    }
    token = token.replace(/\*$/, "");
    if (!token.includes("/")) continue;
    const [fStr, tStr] = token.split("/", 2);
    const fLow = fStr.trim().toLowerCase();
    const tLow = tStr.trim().toLowerCase();
    let f, t;
    try {
      f = fLow === "bar" ? 25 : parseInt(fLow, 10);
      t = tLow === "off" ? 0 : parseInt(tLow, 10);
      if (isNaN(f) || isNaN(t)) continue;
    } catch {
      continue;
    }
    for (let k = 0; k < count; k++) spans.push([f, t]);
  }
  return spans;
}

// `board` is the mover-perspective board *before* the move (own checkers
// positive, opponent's negative), and is what keeps a one-checker two-die span
// from being split through a point the opponent has made. Every caller that has
// one must pass it; only a caller with no board in hand may leave it undefined
// and accept the canonical larger-die-first tie-break.
//
// For the move that was actually *played* the split is not a cosmetic choice:
// an intermediate point holding two enemy checkers reads back as a hit, which
// invents checkers and corrupts every board replayed from that ply onward. For
// an unplayed *alternative* nothing is replayed, so the board survives -- but
// the steps are what the display's move arrows are drawn from, and a route
// through a made point shows a checker landing on a stack of enemy checkers and
// moving on.
export function _notationToSteps(notation, moverIsWhite, d1, d2, board) {
  const steps = [];
  for (const [f, t] of _parseNotation(notation)) {
    const hops = d1 === d2
      ? _splitSpanDouble(f, t, d1)
      : _splitSpanNondouble(f, t, d1, d2, board);
    for (const [hf, pips] of hops) {
      steps.push({ from: _moverToAbs(hf, moverIsWhite), pips });
    }
  }
  return steps;
}

// How many steps a ply record has room for: a `.gvab` ply carries exactly the
// hops the roll allows (see binary.js DICE_TABLE), and anything past that has
// nowhere to go.
export function _stepsPerRoll(d1, d2) {
  return d1 === d2 ? 4 : 2;
}

// Steps for a move the dice cannot explain: one step per span.
//
// `_notationToSteps` splits a span across the dice, which is right for a legal
// play and wrong for an *illegal* one -- a play that used more die-moves than
// the roll has (13/9 with a 3-1, then 12/11: three hops for a two-hop roll)
// expands past what a ply record can hold, and the overflow is dropped on
// write, leaving every board replayed after that ply one checker off.
// Recording each span at its own pip distance keeps the play to one step per
// checker moved, which replays to exactly the board the source recorded.
//
// A span longer than 6 pips still has to be split (`pips` is 3 bits), so this
// is best-effort: callers must re-check the step count against `_stepsPerRoll`
// and fall back to a set-position ply if it still overflows.
export function _notationToStepsUnsplit(notation, moverIsWhite, d1, d2) {
  const steps = [];
  for (const [f, t] of _parseNotation(notation)) {
    const dist = _spanDistance(f, t);
    if (dist >= 1 && dist <= 6) {
      steps.push({ from: _moverToAbs(f, moverIsWhite), pips: dist });
      continue;
    }
    const hops = d1 === d2
      ? _splitSpanDouble(f, t, d1)
      : _splitSpanNondouble(f, t, d1, d2);
    for (const [hf, pips] of hops) {
      steps.push({ from: _moverToAbs(hf, moverIsWhite), pips });
    }
  }
  return steps;
}

// P1/White mover-perspective frame -> the OGXM absolute frame a `set_position`
// ply carries (0=white bar, 25=black bar stored negative, point i mirrored).
// Inverse of reader.js's `_absoluteToP1`.
export function _p1ToAbsolute(boardP1) {
  const boardAbs = new Array(26).fill(0);
  boardAbs[0] = boardP1[25];        // white bar
  boardAbs[25] = -boardP1[0];       // black bar, stored negative
  for (let i = 1; i < 25; i++) boardAbs[25 - i] = boardP1[i];
  return boardAbs;
}

// Steps that fit a ply record, or `null` if no checker ply can hold them.
//
// One rule for all three converters. A `.gvab` checker ply carries exactly the
// hops the roll allows (binary.js DICE_TABLE), and an *illegal* play can use
// more -- `14/10` off a 1-3 and then `13/12`: three die-moves for a two-hop
// roll. Dropping the overflow writes a ply that replays to the wrong board,
// and the damage surfaces only plies later as an impossible position, so
// write_gvab refuses it outright. This is what a converter has to call before
// handing steps over.
//
// Three rungs:
//   1. the steps as built -- split across the dice, right for a legal play;
//   2. one step per span (`_notationToStepsUnsplit`), which is what an illegal
//      play usually needs and usually fits;
//   3. `null` -- too tangled for even one step per checker, so the caller must
//      restate the ply with `setPositionPly`.
//
// Rung 2 needs the notation. A converter working from a board *diff* has none,
// and must not collapse the steps itself: a step from 11 cannot be told apart
// from a checker already sitting on 11, so merging picks the wrong checker and
// silently changes the position.
export function fitMoveSteps(steps, notation, moverIsWhite, d1, d2) {
  const room = _stepsPerRoll(d1, d2);
  if (steps.length <= room) return steps;
  if (notation) {
    const unsplit = _notationToStepsUnsplit(notation, moverIsWhite, d1, d2);
    if (unsplit.length <= room) return unsplit;
  }
  return null;
}

// An illegal play restated as the position it produced (action 31).
//
// The third rung of `fitMoveSteps`. No checker ply can carry the play and
// truncating it would corrupt every board after this one, so state the
// resulting position outright -- what action 31 is for (the spec notes its
// optional dice are exactly this case). The play itself is lost; it broke the
// rules, so there is no move to score.
export function setPositionPly(moverIsWhite, d1, d2, boardAfterP1, ogidBefore, ogidAfter) {
  return {
    color: moverIsWhite ? 1 : 0,
    action_id: 31,
    d1,
    d2,
    set_position: _p1ToAbsolute(boardAfterP1),
    ogid_before: ogidBefore,
    ogid_after: ogidAfter,
  };
}

// ---------------------------------------------------------------------------
// OGID turn-phase state machine
// ---------------------------------------------------------------------------

export class _TurnState {
  constructor() {
    this.curState = _OGID_STATE_INITIAL_BOTH;
    this.cubeOwner = _OGID_CUBE_CENTERED;
    this.cubeAction = _OGID_ACTION_NONE;
    this.cubeLog2 = 0;
    this.moveId = 0;
    this.awaitingResponse = false;
    this.isFirstPly = true;
  }

  get cubeValue() {
    return 1 << this.cubeLog2;
  }
}

// ---------------------------------------------------------------------------
// OGID wrapper
// ---------------------------------------------------------------------------

export function _ogid(boardP1, opts) {
  return boardToOgid(boardP1, {
    moverIsWhite: true,
    cubeValue: opts.cubeValue,
    cubeOwner: opts.cubeOwner,
    cubeAction: opts.cubeAction,
    dice: opts.dice ?? null,
    onRoll: opts.onRoll ?? null,
    gameState: opts.gameState,
    scoreWhite: opts.scoreWhite,
    scoreBlack: opts.scoreBlack,
    matchLength: opts.matchLength,
    crawford: opts.crawford,
    moveId: opts.moveId,
  });
}

// ---------------------------------------------------------------------------
// Analysis-object builders
// ---------------------------------------------------------------------------

// `boardBeforeMover` is the pre-move board in the mover's own numbering, and
// every candidate shares it -- they all start from this ply's position. It is
// what stops a one-checker two-die alternative from being drawn through a
// point the opponent has made: the steps are what the board arrows are built
// from, so without it a rejected alternative is displayed landing on a stack
// of enemy checkers and moving on. Passing the *pre-move* board is sound even
// for a multi-checker candidate, because a play can only hit -- it never adds
// opponent checkers -- so a point open before the play cannot be blocked by it.
function _buildAlternatives(moveOptions, moverIsWhite, d1, d2, boardBeforeMover) {
  const alts = [];
  const bestEquity = moveOptions.length ? moveOptions[0].equity : null;
  for (const opt of moveOptions) {
    const alt = {
      move: _notationToSteps(opt.move || "", moverIsWhite, d1, d2, boardBeforeMover),
      notation: opt.move || "",
      equity: opt.equity,
      is_played: Boolean(opt.played),
      diff: bestEquity != null ? Math.round((opt.equity - bestEquity) * 10000) / 10000 : 0.0,
    };
    if ("probs" in opt) alt.eval = _probsToEval(opt.probs);
    const lvl = _normalizeEvalLevel(opt.eval_level);
    if (lvl != null) alt.eval_level = lvl;
    alts.push(alt);
  }
  return alts;
}

// An equity loss is floored at zero but has no ceiling here. The only bound is
// the encoder's (binary.js's MAX_EQUITY_LOSS, 6.5535), which sits above the
// worst loss backgammon can produce -- equity runs [-3, +3], so no decision can
// cost more than 6.0. Capping at 1.0, as this did, silently rewrote every
// blunder past a point of equity into a one-point one.
export function _cubeSubAnalysis(cube) {
  const equityLoss = Math.max(0.0, cube.lost_equity || 0.0);
  const noDoubleEquity = cube.equity_no_double;
  const doubleTakeEquity = cube.equity_double_take;
  const doublePassEquity = cube.equity_double_pass;

  if (cube.optimal_action === "double") {
    const md = {
      no_double_equity: noDoubleEquity,
      double_take_equity: doubleTakeEquity,
      double_pass_equity: doublePassEquity,
      equity_loss: Math.round(equityLoss * 10000) / 10000,
      correct_action: "double",
    };
    // The cube's own pre-roll probabilities, the same ones a cube_decision
    // carries. The analyzer computes them for every cube decision it looks at;
    // which way the decision came out is no reason to drop them.
    if ("probs" in cube) md.eval = _probsToEval(cube.probs);
    const mdLvl = _normalizeEvalLevel(cube.eval_level);
    if (mdLvl != null) md.eval_level = mdLvl;
    return ["missed_double", md];
  }

  const shouldDouble = cube.optimal_action === "double";
  const sub = {
    should_double: shouldDouble,
    no_double_equity: noDoubleEquity,
    double_take_equity: doubleTakeEquity,
    double_pass_equity: doublePassEquity,
    action: _cubeActionLabel(shouldDouble, doubleTakeEquity, doublePassEquity),
    equity_loss: Math.round(equityLoss * 10000) / 10000,
    decision: Boolean(cube.counted),
  };
  if ("probs" in cube) sub.eval = _probsToEval(cube.probs);
  const lvl = _normalizeEvalLevel(cube.eval_level);
  if (lvl != null) sub.eval_level = lvl;
  return ["cube_decision", sub];
}

export function _checkerAnalysis(entry, moverIsWhite, d1, d2, basePly, boardBeforeMover) {
  const moveOptions = entry.move_options || [];
  if (!moveOptions.length) {
    // No legal move (a dance / forced no-play): nothing to analyze about the
    // checker play, but two things can still ride on this ply. (1) The dice
    // were rolled, so a luck value was computed by the independent luck
    // analyzer -- keep it, or these (negatively-skewed) plies would bias luck
    // totals upward. (2) The player was on roll with a live cube and chose not
    // to double before rolling; that no-double cube decision is a real decision
    // the engine counts, so it must appear in the OGXM too -- otherwise the
    // stored analysis and the engine's own tally disagree (they did for a
    // no-double immediately before a dance). Mirrors gvformat.export's
    // _checker_analysis exactly.
    const analysis = {};
    if (entry.luck != null) analysis.luck = entry.luck;
    if ("cube" in entry) {
      const [key, sub] = _cubeSubAnalysis(entry.cube);
      analysis[key] = sub;
    }
    return Object.keys(analysis).length ? analysis : null;
  }

  const alternatives = _buildAlternatives(moveOptions, moverIsWhite, d1, d2, boardBeforeMover);
  const bestEquity = moveOptions[0].equity;
  const playedOpt = moveOptions.find(o => o.played) || moveOptions[moveOptions.length - 1];
  const playedEquity = playedOpt.equity;

  const hasAnalysis = "lost_equity" in entry;
  let equityLoss, decision;
  if (hasAnalysis) {
    equityLoss = Math.max(0.0, entry.lost_equity);
    decision = Boolean(entry.counted);
  } else {
    equityLoss = Math.max(0.0, Math.round((bestEquity - playedEquity) * 10000) / 10000);
    decision = false;
  }

  const analysis = {
    eval: _probsToEval(moveOptions[0].probs),
    best_equity: bestEquity,
    played_equity: playedEquity,
    equity_loss: Math.round(equityLoss * 10000) / 10000,
    decision,
    alternatives,
  };

  const altDepths = alternatives.filter(a => a.eval_level).map(a => _evalDepth(a.eval_level));
  const decisionPly = altDepths.length ? Math.max(...altDepths) : 0;
  if (decisionPly > basePly) analysis.ply = decisionPly;

  if ("luck" in entry) {
    analysis.luck = entry.luck;
  }

  if (entry.illegal_move) analysis.illegal_move = true;

  if ("cube" in entry) {
    const [key, sub] = _cubeSubAnalysis(entry.cube);
    analysis[key] = sub;
  }

  return analysis;
}

function _cubeDecisionAnalysis(entry) {
  // An unanalyzed cube-decision entry (e.g. from the engine-free mat->OGXM
  // path) carries no analysis payload; there is nothing to emit.
  if (!("optimal_action" in entry)) return null;
  const equityLoss = Math.max(0.0, entry.lost_equity || 0.0);
  const analysis = {
    correct_action: entry.optimal_action,
    played_action: entry.player_action,
    no_double_equity: entry.equity_no_double,
    double_take_equity: entry.equity_double_take,
    double_pass_equity: entry.equity_double_pass,
    equity_loss: Math.round(equityLoss * 10000) / 10000,
    decision: Boolean(entry.counted),
  };
  if ("probs" in entry) analysis.eval = _probsToEval(entry.probs);
  const lvl = _normalizeEvalLevel(entry.eval_level);
  if (lvl != null) analysis.eval_level = lvl;
  return analysis;
}

function _cubeResponseAnalysis(entry, noDoubleEquity) {
  // Unanalyzed take/pass entry (engine-free mat->OGXM path): no analysis.
  if (!("optimal_response" in entry)) return null;
  const equityLoss = Math.max(0.0, entry.lost_equity || 0.0);
  const analysis = {
    correct_action: entry.optimal_response,
    played_action: entry.player_response,
    double_take_equity: entry.equity_take,
    double_pass_equity: entry.equity_pass,
    equity_loss: Math.round(equityLoss * 10000) / 10000,
    decision: Boolean(entry.counted),
  };
  if (noDoubleEquity != null) analysis.no_double_equity = noDoubleEquity;
  if ("probs" in entry) analysis.eval = _probsToEval(entry.probs);
  return analysis;
}

// ---------------------------------------------------------------------------
// Game-end ply
// ---------------------------------------------------------------------------

function _gameEndActionId(resultType, matchCompleteHere) {
  if (resultType === "forfeit") return 29;
  if (resultType === "resign" || resultType === "time") return matchCompleteHere ? 28 : 27;
  if (resultType === "pass") return 26;
  return matchCompleteHere ? 26 : 24;
}

// ---------------------------------------------------------------------------
// Top-level conversion
// ---------------------------------------------------------------------------

function _parseTimestamp(date, eventTime) {
  if (!date) return 0;
  try {
    const d = date.replace(/\./g, "-");
    const t = (eventTime || "00:00").replace(/\./g, ":");
    return Math.floor(new Date(`${d}T${t}Z`).getTime() / 1000);
  } catch {
    return 0;
  }
}

function _convertCheckerPly(entry, playerWhite, scoreWhite, scoreBlack,
  matchLength, crawford, boardBefore, turn, basePly) {
  const d1 = entry.dice[0], d2 = entry.dice[1];
  const isWhite = entry.player === playerWhite;
  const boardAfter = entry.board_after;
  const onRoll = isWhite ? "W" : "B";

  const beforeState = turn.isFirstPly ? _OGID_STATE_INITIAL_BOTH : _OGID_STATE_ROLLED;

  const ogidBefore = _ogid(boardBefore, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: turn.cubeAction, dice: [d1, d2], onRoll,
    gameState: beforeState, scoreWhite, scoreBlack,
    matchLength, crawford, moveId: turn.moveId,
  });

  turn.moveId++;
  turn.isFirstPly = false;
  turn.curState = _OGID_STATE_CHECKER_DONE;
  turn.cubeAction = _OGID_ACTION_NONE;

  const ogidAfter = _ogid(boardAfter, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: turn.cubeAction, dice: null,
    onRoll: isWhite ? "B" : "W",
    gameState: turn.curState, scoreWhite, scoreBlack,
    matchLength, crawford, moveId: turn.moveId,
  });

  // This path derives steps from a board diff, so it has no notation of its
  // own -- the .mat converter threads the source's through on the entry (see
  // mat2gva.js) precisely so the middle rung is reachable here.
  const steps = fitMoveSteps(
    _computeMoveSteps(boardBefore, boardAfter, isWhite, d1, d2),
    entry.notation || entry.player_move, isWhite, d1, d2,
  );
  if (steps === null) {
    return setPositionPly(isWhite, d1, d2, boardAfter, ogidBefore, ogidAfter);
  }

  const ply = {
    color: isWhite ? 1 : 0,
    action_id: _diceActionId(d1, d2),
    d1, d2,
    moves: steps,
    ogid_before: ogidBefore,
    ogid_after: ogidAfter,
  };
  const analysis = _checkerAnalysis(entry, isWhite, d1, d2, basePly,
    isWhite ? boardBefore : _flipBoard(boardBefore));
  if (analysis != null) ply.analysis = analysis;
  return ply;
}

function _convertCubeDecisionPly(entry, playerWhite, scoreWhite, scoreBlack,
  matchLength, crawford, turn) {
  const isWhite = entry.player === playerWhite;
  const board = entry.board;
  const onRoll = isWhite ? "W" : "B";

  const ogidBefore = _ogid(board, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: turn.cubeAction, dice: null, onRoll,
    gameState: turn.curState, scoreWhite, scoreBlack,
    matchLength, crawford, moveId: turn.moveId,
  });

  turn.awaitingResponse = true;
  turn.curState = _OGID_STATE_DOUBLE_OFFERED;
  turn.cubeAction = _OGID_ACTION_DOUBLE;

  const ogidAfter = _ogid(board, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: turn.cubeAction, dice: null,
    onRoll: isWhite ? "B" : "W",
    gameState: turn.curState, scoreWhite, scoreBlack,
    matchLength, crawford, moveId: turn.moveId,
  });

  const ply = {
    color: isWhite ? 1 : 0,
    action_id: 21,
    ogid_before: ogidBefore,
    ogid_after: ogidAfter,
  };
  const analysis = _cubeDecisionAnalysis(entry);
  if (analysis !== null) ply.analysis = analysis;
  return ply;
}

function _convertCubeResponsePly(entry, playerWhite, scoreWhite, scoreBlack,
  matchLength, crawford, noDoubleEquity, turn) {
  const isWhite = entry.player === playerWhite;
  const board = entry.board;
  const onRoll = isWhite ? "W" : "B";

  const ogidBefore = _ogid(board, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: _OGID_ACTION_DOUBLE, dice: null, onRoll,
    gameState: _OGID_STATE_DOUBLE_OFFERED, scoreWhite,
    scoreBlack, matchLength, crawford, moveId: turn.moveId,
  });

  const took = entry.player_response === "take";
  turn.awaitingResponse = false;
  if (took) {
    turn.cubeLog2++;
    turn.cubeOwner = isWhite ? _OGID_CUBE_WHITE : _OGID_CUBE_BLACK;
    turn.curState = _OGID_STATE_AFTER_TAKE;
    turn.cubeAction = _OGID_ACTION_TAKE;
  } else {
    turn.curState = _OGID_STATE_GAME_OVER;
    turn.cubeAction = _OGID_ACTION_PASS;
  }

  const ogidAfter = _ogid(board, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: turn.cubeAction, dice: null,
    onRoll: isWhite ? "B" : "W",
    gameState: turn.curState, scoreWhite, scoreBlack,
    matchLength, crawford, moveId: turn.moveId,
  });

  const ply = {
    color: isWhite ? 1 : 0,
    action_id: took ? 22 : 23,
    ogid_before: ogidBefore,
    ogid_after: ogidAfter,
  };
  const analysis = _cubeResponseAnalysis(entry, noDoubleEquity);
  if (analysis !== null) ply.analysis = analysis;
  return ply;
}

function _convertGame(game, playerWhite, playerBlack, matchLength, basePly) {
  const scoreWhite = game.score_start.player1;
  const scoreBlack = game.score_start.player2;
  const isCrawford = Boolean(game.is_crawford);

  const plies = [];
  let board = [..._STARTING_BOARD_P1];
  let pendingNdEquity = null;
  const turn = new _TurnState();

  for (const entry of game.moves) {
    const kind = entry.kind;
    if (kind === "checker") {
      const boardBefore = entry.board_before;
      plies.push(_convertCheckerPly(
        entry, playerWhite, scoreWhite, scoreBlack,
        matchLength, isCrawford, boardBefore, turn, basePly,
      ));
      board = entry.board_after;
    } else if (kind === "cube_decision") {
      plies.push(_convertCubeDecisionPly(
        entry, playerWhite, scoreWhite, scoreBlack,
        matchLength, isCrawford, turn,
      ));
      board = entry.board;
      pendingNdEquity = entry.equity_no_double;
    } else if (kind === "cube_response") {
      plies.push(_convertCubeResponsePly(
        entry, playerWhite, scoreWhite, scoreBlack,
        matchLength, isCrawford, pendingNdEquity, turn,
      ));
      board = entry.board;
      pendingNdEquity = null;
    }
  }

  const result = game.result || {};
  const winnerName = result.winner;
  const resultType = result.type || "normal";
  let points = result.points;
  if (points == null) points = 0;

  const winnerIsWhite = winnerName === playerWhite;
  const matchCompleteHere = Boolean(
    matchLength && (
      (winnerIsWhite && scoreWhite + points >= matchLength) ||
      (!winnerIsWhite && scoreBlack + points >= matchLength)
    )
  );

  let winnerCode;
  if (winnerName != null) {
    const actionId = _gameEndActionId(resultType, matchCompleteHere);
    // A terminal ply names the winner: nobody *does* a game-over, and
    // ogxm_replay.cpp just carries the winner through. Resignation is the one
    // exception -- it is an act, and the player who resigns is the one who
    // lost -- so 27/28 gets the resigner, both as the ply's colour (`color`
    // is documented as the player a ply belongs to) and as the player on roll
    // (the resigner is the one facing the roll they chose not to take).
    const actorIsWhite = RESIGN_ACTIONS.has(actionId) ? !winnerIsWhite : winnerIsWhite;
    const onRoll = actorIsWhite ? "W" : "B";
    const endState = turn.curState;
    const endCubeAction = turn.cubeAction;
    const endKwargs = {
      cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
      cubeAction: endCubeAction, dice: null,
      gameState: endState, scoreWhite, scoreBlack,
      matchLength, crawford: isCrawford, moveId: turn.moveId,
    };
    plies.push({
      color: actorIsWhite ? 1 : 0,
      action_id: actionId,
      ogid_before: _ogid(board, { onRoll, ...endKwargs }),
      ogid_after: _ogid(board, { onRoll: actorIsWhite ? "B" : "W", ...endKwargs }),
    });
    winnerCode = winnerIsWhite ? 0 : 1;
  } else {
    winnerCode = 255;
  }

  return {
    game_index: game.game_number - 1,
    winner: winnerCode,
    points_won: points,
    is_crawford: isCrawford,
    plies,
  };
}

/**
 * Convert an analyze_mat()-shaped result dict into OGXM-JSON.
 *
 * @param {object} result
 * @returns {object}
 */
export function toOgxmJson(result) {
  const summary = result.summary;
  const [playerWhite, playerBlack, p1IsWhite] = _canonicalOrientation(summary.player1, summary.player2);
  const matchLength = summary.match_length || 0;

  const evalLevel = _normalizeEvalLevel(summary.eval_level) || "2ply";
  const firstPassLevel = _normalizeEvalLevel(summary.first_pass_level);
  const basePly = _evalDepth(firstPassLevel || evalLevel);

  // Canonical white is the source's player-2: flip every board (and
  // score_start) up front so the converter body below -- which builds
  // everything from "player-1/summary's original player1 == white" --
  // produces output in canonical white's frame unchanged.
  const gamesIn = p1IsWhite ? result.games : result.games.map(_flipGameOrientation);
  const gamesOut = gamesIn.map(g => _convertGame(g, playerWhite, playerBlack, matchLength, basePly));

  let scoreWhite = gamesIn.length ? gamesIn[gamesIn.length - 1].score_start.player1 : 0;
  let scoreBlack = gamesIn.length ? gamesIn[gamesIn.length - 1].score_start.player2 : 0;
  if (gamesIn.length) {
    const lastResult = gamesIn[gamesIn.length - 1].result || {};
    const winnerName = lastResult.winner;
    const pts = lastResult.points || 0;
    if (winnerName === playerWhite) scoreWhite += pts;
    else if (winnerName === playerBlack) scoreBlack += pts;
  }

  let matchResult;
  if (matchLength && scoreWhite >= matchLength) matchResult = 1;
  else if (matchLength && scoreBlack >= matchLength) matchResult = 2;
  else matchResult = 0;

  const luckEvalLevel = _normalizeEvalLevel(summary.luck_eval_level);
  // Prefer a precomputed unix `timestamp` when the summary carries one (the
  // OGXM-sourced analysis path already has unix seconds, not the mat header's
  // date/time strings); otherwise parse the mat header. Back-compatible: mat
  // callers pass date/event_time and no timestamp. Mirrors gvformat.export.
  const timestamp = summary.timestamp != null
    ? summary.timestamp
    : _parseTimestamp(summary.date, summary.event_time);

  const ogxm = {
    match_length: matchLength,
    player_white: playerWhite,
    player_black: playerBlack,
    white_score: scoreWhite,
    black_score: scoreBlack,
    result: matchResult,
    source: 1,
    timestamp,
    crawford: Boolean(summary.crawford_rule),
    jacoby: Boolean(summary.jacoby_rule),
    beaver: Boolean(summary.beaver_rule),
    cube_limit: summary.cube_limit || 0,
    event: cleanPlace(summary.event),
    site: cleanPlace(summary.site),
  };

  // Emit analysis_info only when the match actually carries analysis. The
  // engine-free mat->OGXM path (mat2gva.js) produces plies with no `analysis`
  // object, so a base OGXM stays analysis-free (no dangling metadata for a run
  // that never happened) -- mirrors gvformat.export.to_ogxm_json's has_analysis
  // gate exactly.
  const hasAnalysis = gamesOut.some(g => g.plies.some(ply => ply.analysis != null));
  if (hasAnalysis) {
    const analysisInfo = { ply: basePly, eval_level: evalLevel };
    if (luckEvalLevel != null) analysisInfo.luck_eval_level = luckEvalLevel;
    analysisInfo.preset = summary.preset;
    // The producer, supplied by whoever ran the analysis --
    // gvanalysis.match.model_id() yields "gv-bgsage/<engine version>".
    // Mirrors gvformat.export.to_ogxm_json exactly: this package is
    // engine-free, so the label arrives in the summary.
    analysisInfo.model_id = summary.engine || "bgsage";
    analysisInfo.timestamp = timestamp;
    ogxm.analysis_info = analysisInfo;
  }

  ogxm.games = gamesOut;
  return ogxm;
}
