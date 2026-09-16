// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Pure-JS converter: BGBlitz .bgf match files -> OGXM JSON.
// No third-party dependencies — Smile decoder covers BGBlitz's subset.

import {
  _TurnState, _ogid, _diceActionId, _notationToSteps,
  fitMoveSteps, setPositionPly,
  _canonicalOrientation, _flipBoard,
  _OGID_STATE_INITIAL_BOTH, _OGID_STATE_ROLLED, _OGID_STATE_CHECKER_DONE,
  _OGID_STATE_DOUBLE_OFFERED, _OGID_STATE_AFTER_TAKE, _OGID_STATE_GAME_OVER,
  _OGID_ACTION_NONE, _OGID_ACTION_DOUBLE, _OGID_ACTION_TAKE, _OGID_ACTION_PASS,
  _OGID_CUBE_WHITE, _OGID_CUBE_BLACK,
} from "./export.js";
import { RESIGN_ACTIONS } from "./constants.js";
import { isPlayLegal } from "./legality.js";
import { canonicalNotation, fromBgfAbsFrame } from "./notation.js";

// ---------------------------------------------------------------------------
// Local helper
// ---------------------------------------------------------------------------

function _probsToEval(probs) {
  const [win, gwin, bgwin, gloss, bgloss] = probs;
  const equity = win + gwin + bgwin - gloss - bgloss;
  return {
    win, gammon_win: gwin, bg_win: bgwin,
    gammon_loss: gloss, bg_loss: bgloss, equity: Math.round(equity * 10000) / 10000,
  };
}

// ---------------------------------------------------------------------------
// Smile decoder (subset used by BGBlitz)
// ---------------------------------------------------------------------------

const SMILE_MAGIC = 0x3A290A; // ":)\n" as bytes
const FEATURE_SHARED_NAMES = 0x01;
const SENTINEL = -999.0;

class SmileDecoder {
  constructor(data) {
    this.buf = data;
    this.pos = 4; // skip 3-byte magic + flags byte
    this.sharedNames = Boolean(data[3] & FEATURE_SHARED_NAMES);
    this.nameTable = [];
  }

  _rb() {
    if (this.pos >= this.buf.length) throw new Error("Unexpected end of Smile stream");
    return this.buf[this.pos++];
  }

  _read(n) {
    const end = this.pos + n;
    if (end > this.buf.length) throw new Error(`Need ${n} bytes at pos ${this.pos}`);
    const chunk = this.buf.slice(this.pos, end);
    this.pos = end;
    return chunk;
  }

  static _zigzag(n) {
    return (n >>> 1) ^ -(n & 1);
  }

  _vint() {
    let acc = 0;
    while (true) {
      const b = this._rb();
      acc = (acc << 6) | (b & 0x3F);
      if (b & 0x80) return SmileDecoder._zigzag(acc);
    }
  }

  _safeDouble() {
    const r = this._read(10);
    const bits = BigInt(r[0] & 0x7F) << 57n
      | BigInt(r[1] & 0x7F) << 50n
      | BigInt(r[2] & 0x7F) << 43n
      | BigInt(r[3] & 0x7F) << 36n
      | BigInt(r[4] & 0x7F) << 29n
      | BigInt(r[5] & 0x7F) << 22n
      | BigInt(r[6] & 0x7F) << 15n
      | BigInt(r[7] & 0x7F) << 8n
      | BigInt(r[8] & 0x7F) << 1n
      | BigInt((r[9] & 0x7F) >> 6);
    const view = new DataView(new ArrayBuffer(8));
    view.setBigUint64(0, bits);
    return view.getFloat64(0);
  }

  _readKey() {
    const b = this._rb();
    if (b === 0xFB || b === 0xFF) return null;
    if (b >= 0x40 && b <= 0x7F) return this.nameTable[b - 0x40];
    if (b >= 0x80 && b <= 0xBF) {
      const len = (b & 0x3F) + 1;
      const key = new TextDecoder("utf-8").decode(this._read(len));
      if (this.sharedNames) this.nameTable.push(key);
      return key;
    }
    if (b === 0x30) return this.nameTable[this._rb()];
    throw new Error(`Unknown Smile key token 0x${b.toString(16)} at pos ${this.pos - 1}`);
  }

  _readValue() {
    const b = this._rb();
    if (b === 0xFA) return this._parseObject();
    if (b === 0xF8) return this._parseArray();
    if (b === 0xF9 || b === 0xFF) return undefined; // END_ARRAY sentinel
    if (b === 0x20) return "";
    if (b === 0x21) return null;
    if (b === 0x22) return false;
    if (b === 0x23) return true;
    if (b === 0x24) return this._vint();
    if (b === 0x25) return this._vint();
    if (b === 0x28) {
      const raw = this._read(4);
      return new DataView(raw.buffer, raw.byteOffset, 4).getFloat32(0);
    }
    if (b === 0x29) return this._safeDouble();
    if (b >= 0x40 && b <= 0x5F) {
      const len = b - 0x3F;
      return new TextDecoder("ascii").decode(this._read(len));
    }
    if (b >= 0x60 && b <= 0x7F) {
      const len = b - 0x5E;
      return new TextDecoder("utf-8").decode(this._read(len));
    }
    if (b >= 0xC0 && b <= 0xDF) return SmileDecoder._zigzag(b - 0xC0);
    if (b >= 0xE0 && b <= 0xEF) {
      // Find 0xFC terminator
      let end = this.pos;
      while (end < this.buf.length && this.buf[end] !== 0xFC) end++;
      const chunk = this.buf.slice(this.pos, end);
      this.pos = end + 1;
      const enc = (b & 0x04) ? "utf-8" : "ascii";
      return new TextDecoder(enc, { fatal: false }).decode(chunk);
    }
    throw new Error(`Unknown Smile value token 0x${b.toString(16)} at pos ${this.pos - 1}`);
  }

  _parseObject() {
    const obj = {};
    while (true) {
      const key = this._readKey();
      if (key === null) return obj;
      obj[key] = this._readValue();
    }
  }

  _parseArray() {
    const arr = [];
    while (true) {
      const val = this._readValue();
      if (val === undefined) return arr; // END_ARRAY
      arr.push(val);
    }
  }

  decode() {
    const b = this._rb();
    if (b === 0xFA) return this._parseObject();
    if (b === 0xF8) return this._parseArray();
    this.pos--;
    return this._readValue();
  }
}

function decodeSmile(smileBytes) {
  return new SmileDecoder(smileBytes).decode();
}

// ---------------------------------------------------------------------------
// Decompression helper
// ---------------------------------------------------------------------------

async function decompressIfNeeded(bytes) {
  // Gzip magic: 0x1f 0x8b
  if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
    const ds = new DecompressionStream("gzip");
    const writer = ds.writable.getWriter();
    writer.write(bytes);
    writer.close();
    const chunks = [];
    const reader = ds.readable.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
    }
    const totalLen = chunks.reduce((s, c) => s + c.length, 0);
    const result = new Uint8Array(totalLen);
    let off = 0;
    for (const c of chunks) { result.set(c, off); off += c.length; }
    return result;
  }
  // Zlib magic: 0x78 0x01/0x9c/0xda
  if (bytes[0] === 0x78 && (bytes[1] === 0x01 || bytes[1] === 0x9c || bytes[1] === 0xda)) {
    const ds = new DecompressionStream("deflate");
    const writer = ds.writable.getWriter();
    writer.write(bytes);
    writer.close();
    const chunks = [];
    const reader = ds.readable.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
    }
    const totalLen = chunks.reduce((s, c) => s + c.length, 0);
    const result = new Uint8Array(totalLen);
    let off = 0;
    for (const c of chunks) { result.set(c, off); off += c.length; }
    return result;
  }
  return bytes;
}

// ---------------------------------------------------------------------------
// BGF file reader
// ---------------------------------------------------------------------------

async function readBgf(path) {
  const raw = await path.arrayBuffer();
  const bytes = new Uint8Array(raw);

  // Split on first newline — header is one line of JSON
  let splitIdx = -1;
  for (let i = 0; i < bytes.length; i++) {
    if (bytes[i] === 0x0A) { splitIdx = i; break; }
  }
  if (splitIdx < 0) throw new Error("BGF: no newline separator found");

  const headerBytes = bytes.slice(0, splitIdx);
  const header = JSON.parse(new TextDecoder("utf-8").decode(headerBytes));
  const tail = bytes.slice(splitIdx + 1);
  const payload = await decompressIfNeeded(tail);

  // Validate Smile magic ":)\n"
  if (payload[0] !== 0x3A || payload[1] !== 0x29 || payload[2] !== 0x0A) {
    throw new Error("Payload is not Smile (missing magic).");
  }

  return { header, payload };
}

// ---------------------------------------------------------------------------
// Low-level helpers
// ---------------------------------------------------------------------------

function flt(v) {
  if (v == null) return 0.0;
  return Number(v);
}

function nonempty(s) {
  return s ? String(s) : null;
}

function plyLevel(ply) {
  if (ply == null || ply === 0) return null;
  return `${Number(ply)}ply`;
}

function makeProbs(eq) {
  if (!eq || eq.myWins == null) return [];
  return [
    round4(flt(eq.myWins)),
    round4(flt(eq.myGammon)),
    round4(flt(eq.myBackGammon)),
    round4(flt(eq.oppGammon)),
    round4(flt(eq.oppBackGammon)),
  ];
}

function stateAction(state) {
  if (state === "DOUBLE" || state === "RE_DOUBLE") return "double";
  if (state === "NO_DOUBLE") return "no_double";
  return null;
}

function stateResponse(state) {
  if (state === "ACCEPT") return "take";
  if (state === "REJECT") return "pass";
  return null;
}

function round4(v) { return Math.round(v * 1e4) / 1e4; }

// Cube decision so clear it should not count toward PR. Mirrors
// gvanalysis.game_eval._trivial_cube / gvformat.xg.trivialCube.
function trivialCube(nd, dt, dp) {
  return (
    Math.abs(nd - Math.min(dt, dp)) < 0.001
    || (nd - dt) > 0.200
    || (nd - dp) > 0.200
    || (nd < -0.900 && dt < -0.900)
  );
}

// Take/pass response so clear it should not count toward PR. Mirrors
// gvanalysis.game_eval._trivial_take_pass.
function trivialTakePass(dt, dp) {
  return Math.abs(dt - dp) < 0.001;
}

// ---------------------------------------------------------------------------
// Board helpers (BGF -> absolute -> mover-perspective for OGID)
// ---------------------------------------------------------------------------

// BGF uses a 24-point board: positive = green (player1/white), negative = red
// (player2/black). Index i represents point (i+1) in BGF's coordinate system.
// Green's point N is absolute point N; Red's point N is absolute point
// (25 - N) (verified against gvformat.export's independently-derived,
// libogxm-matched absolute frame: the standard starting position's
// green/positive checkers land at the same absolute indices as XG's/export's
// White checkers only with this mapping, not the reverse one -- green's own
// numbering is the *direct* index, red's is reflected).
function bgfInitialBoardToAbsolute(initial) {
  const pts = initial.points || [];
  // `points` is a SINGLE shared frame -- index i is physical point i+1 in
  // RED's numbering -- and the sign alone says who owns the checkers
  // (positive = green, negative = red). Since green (assumed White, pre any
  // canonical-orientation flip applied by the caller) needs to land at its
  // own absolute white-numbering index directly, and red's numbering is the
  // mirror of green's (red's point N == green's point 25-N), points[i]
  // (physical point i+1 in red's numbering) goes to absolute index
  // 25 - (i + 1) -- i.e. board[24 - i] -- matching the same reflection
  // applyBgfMove applies to a red sub-move.
  const board = new Array(26).fill(0);
  for (let i = 0; i < 24; i++) {
    const val = i < pts.length ? Number(pts[i]) : 0;
    if (val) board[24 - i] = val;
  }
  const greenBar = pts.length > 24 ? Number(pts[24]) : 0;
  const redBar = pts.length > 25 ? Number(pts[25]) : 0;
  board[25] = greenBar;   // White's bar (green = white), stored positive
  // Opponent's (red's) bar: ogid's absolute-position encoder reads index 0
  // as a plain non-negative count (no abs()) -- same as xg2gva.js's
  // `board[0] = -pos[25]` (XG's raw pos[25] is itself negative, so the
  // double negation lands positive). Storing it negative here silently
  // drops every black-bar checker from the OGID position string
  // (`[x].fill(...)`-style repeats for a negative count are simply empty).
  board[0] = redBar;
  return board;
}

/**
 * Split bgf's absolute board into mover-relative [mine, opp] counts.
 *
 * `legality.js` works in the mover's own numbering (1 = ace point, 25 = own
 * bar). In bgf's absolute frame green's point N lives at N and red's at
 * 25 - N (see applyBgfMove), with green positive.
 */
function bgfRelativeBoard(board, pid) {
  const mine = new Array(26).fill(0);
  const opp = new Array(26).fill(0);
  if (pid === -1) { // green: positive, own point p at absolute p
    for (let p = 1; p <= 24; p++) {
      const v = board[p];
      if (v > 0) mine[p] = v;
      else if (v < 0) opp[p] = -v;
    }
    mine[25] = Math.max(0, board[25]);
  } else { // red: negative, own point p at absolute 25 - p
    for (let p = 1; p <= 24; p++) {
      const v = board[25 - p];
      if (v < 0) mine[p] = -v;
      else if (v > 0) opp[p] = v;
    }
    mine[25] = Math.max(0, board[0]);
  }
  return [mine, opp];
}

/** The actually-played [from, to] sub-move pairs, bear-off as 0. */
function bgfPlayedPairs(fromPts, toPts) {
  const out = [];
  const len = Math.min(fromPts.length, toPts.length);
  for (let i = 0; i < len; i++) {
    if (fromPts[i] === -1) continue;
    const t = toPts[i];
    out.push([Number(fromPts[i]), t && Number(t) > 0 ? Number(t) : 0]);
  }
  return out;
}

function applyBgfMove(board, fromPt, toPt, pid) {
  let absF, absT;
  if (fromPt === 25) {
    absF = pid === -1 ? 25 : 0;
  } else if (fromPt >= 1 && fromPt <= 24) {
    absF = pid === -1 ? fromPt : (25 - fromPt);
  } else {
    return false;
  }

  if (toPt <= 0) {
    absT = 0;
  } else if (toPt >= 1 && toPt <= 24) {
    absT = pid === -1 ? toPt : (25 - toPt);
  } else {
    return false;
  }

  // Remove from source. Green is stored positive and red negative on points
  // 1-24 (see the hit/place logic below), so removing one of the mover's
  // checkers moves the point count *toward* zero: -1 for green, +1 for red.
  // The bar slots (0 and 25) are both plain non-negative *counts* (not
  // signed like the points), so leaving one's own bar always decrements it
  // regardless of color; they must be decremented too, or entering from the
  // bar leaves a phantom checker there forever.
  if (absF >= 1 && absF <= 24) {
    board[absF] += pid === -1 ? -1 : 1;
  } else if (absF === 25) { // green's bar (own bar, non-negative count)
    board[25] -= 1;
  } else if (absF === 0) {  // red's bar (own bar, non-negative count)
    board[0] -= 1;
  }

  if (toPt <= 0) return false; // bearing off

  let hit = false;
  if (absT >= 1 && absT <= 24) {
    if (pid === -1) { // green moving; red blot = -1
      if (board[absT] === -1) {
        hit = true;
        board[absT] = 1;
        board[0] += 1; // opponent (red) checker to bar (count, +1)
      } else {
        board[absT] += 1;
      }
    } else { // red moving; green blot = +1
      if (board[absT] === 1) {
        hit = true;
        board[absT] = -1;
        board[25] += 1; // opponent (green) checker to bar
      } else {
        board[absT] -= 1;
      }
    }
  }
  return hit;
}

/**
 * Render BGF player-relative from/to as OGXM notation (`bar`/`off` + hit `*`),
 * without mutating the caller's board.
 *
 * Like the board-applying path but (a) runs on a *copy* of the absolute
 * pre-move board so it is safe to call per candidate, and (b) emits the spec's
 * `bar`/`off` tokens rather than `25`/`0` — so the string parses cleanly
 * through `_notationToSteps` into structured `move` steps and reconstructs
 * standard `.mat` notation.
 */
function bgfFmtNotation(fromPts, toPts, pid, boardAbs) {
  const work = [...boardAbs];
  const parts = [];
  const from = fromPts || [];
  const to = toPts || [];
  const len = Math.min(from.length, to.length);
  for (let i = 0; i < len; i++) {
    const f = from[i];
    if (f === -1) break;
    const tVal = to[i] || 0;
    const hit = applyBgfMove(work, f, tVal, pid);
    const fStr = f === 25 ? "bar" : String(f);
    const tStr = tVal <= 0 ? "off" : String(tVal);
    parts.push(`${fStr}/${tStr}${hit ? "*" : ""}`);
  }
  return parts.join(" ");
}

/**
 * Render the *display* `notation` string via the shared board-diff
 * canonicalizer -- collapsing hops and combining identical legs -- so BGF
 * import matches the analyzer and XG paths exactly. Applies the play to a copy
 * of the absolute pre-move board (same applyBgfMove the per-hop renderer uses),
 * then diffs before/after in the mover's perspective.
 */
function bgfCanonicalNotation(fromPts, toPts, pid, boardAbs, d1, d2) {
  const after = [...boardAbs];
  const from = fromPts || [];
  const to = toPts || [];
  const len = Math.min(from.length, to.length);
  for (let i = 0; i < len; i++) {
    if (from[i] === -1) break;
    applyBgfMove(after, from[i], to[i] || 0, pid);
  }
  return canonicalNotation(
    fromBgfAbsFrame(boardAbs, pid),
    fromBgfAbsFrame(after, pid),
    d1, d2,
  );
}

// ---------------------------------------------------------------------------
// OGID state constants
// ---------------------------------------------------------------------------

const OGID_STATE_INITIAL_BOTH = "IB";
const OGID_STATE_ROLLED = "R";
const OGID_STATE_CHECKER_DONE = "C";
const OGID_STATE_DOUBLE_OFFERED = "D";
const OGID_STATE_AFTER_TAKE = "A";
const OGID_STATE_GAME_OVER = "G";

const OGID_ACTION_NONE = "N";
const OGID_ACTION_DOUBLE = "O";
const OGID_ACTION_TAKE = "T";
const OGID_ACTION_PASS = "P";

const OGID_CUBE_CENTERED = "N";
const OGID_CUBE_WHITE = "W";
const OGID_CUBE_BLACK = "B";

// ---------------------------------------------------------------------------
// Alternatives builder
// ---------------------------------------------------------------------------

// `boardBeforeMover` is the pre-move board in the mover's own numbering (see
// `_buildAlternatives` in export.js for why the pre-move board is the right one
// for every candidate). BGBlitz records only a move's endpoints, so without it
// a one-checker two-die alternative ("18/7" off a 5-6) is split larger-die-
// first and can be drawn through a point the opponent has made -- the board
// arrows are built from these steps, so the display shows a checker landing on
// enemy checkers and moving on.
export function buildAlternatives(moveOptions, moverIsWhite, d1, d2, boardBeforeMover) {
  const alts = [];
  const bestEquity = moveOptions.length ? moveOptions[0].equity : null;
  for (const opt of moveOptions) {
    // Each move option already carries the 5-output probability vector computed
    // by checkerAnalysis (see makeProbs there); reuse it. The options never have
    // an `eq` field, so reading `opt.eq` here silently dropped every
    // alternative's win/gammon/backgammon eval.
    const probs = opt.probs || [];
    const alt = {
      equity: opt.equity,
      is_played: Boolean(opt.played),
      diff: bestEquity != null ? round4(opt.equity - bestEquity) : 0.0,
    };
    if (probs.length) alt.eval = _probsToEval(probs);
    const lvl = plyLevel(opt.ply);
    if (lvl) alt.eval_level = lvl;
    // Per the OGXM spec, `move` is the structured source of truth (Step[] of
    // {from, pips} in absolute coords) and `notation` its derived display
    // string. Parse the candidate's notation into steps with the same helper
    // the analyze/XG paths use.
    const notation = opt.move || "";
    alt.move = _notationToSteps(notation, moverIsWhite, d1, d2, boardBeforeMover);
    alt.notation = opt.notation || notation;
    alts.push(alt);
  }
  return alts;
}

// ---------------------------------------------------------------------------
// Checker analysis builder
// ---------------------------------------------------------------------------

export function checkerAnalysis(eqObj, moveAnalysis, prObj, pid, d1, d2, boardBefore, moverIsWhite,
                        dancingEq = null, plyRaw = null) {
  if (!moveAnalysis.length) {
    // A dance has no candidate list -- there was nothing to choose between.
    // BGBlitz still evaluates the position the non-play leaves behind and
    // stores it in `dancingEquity`, kept apart from the pre-roll `equity` that
    // carries this ply's cube decision (you can double even when you cannot
    // move). Without it the ply's only probabilities are the cube's, which
    // describe the board before the dice were thrown. Fed in here as a single
    // played option with no move, it takes the same path as every other ply and
    // comes out in the shape XG writes for a dance.
    if (!dancingEq) return null;
    moveAnalysis = [{ eq: dancingEq, ply: plyRaw, played: true, move: { from: [], to: [] } }];
  }

  // Build move options with equity from BGF's eq objects. Each candidate
  // carries its own player-relative from/to; render it to notation now (with
  // hit markers off the shared pre-move board) so buildAlternatives can parse
  // the spec-required structured `move` steps from it.
  const moveOptions = [];
  for (const ma of moveAnalysis) {
    const eq = ma.eq || {};
    // Per-candidate equity that PR/error is measured in. Match sessions
    // populate `emg` (match-equity-adjusted EMG). Money sessions leave emg (and
    // matchEquity) as the -999 sentinel (hasEMG=false); there the equivalent
    // quantity is the cubeful money equity in eq.cubeDecision.eqCubeFul. Using
    // the raw emg sentinel in money mode made every candidate equal (-999),
    // zeroing all checker error -- so PR reflected only cube error.
    const emg = (eq.hasEMG ?? true)
      ? flt(eq.emg)
      : flt((eq.cubeDecision || {}).eqCubeFul);
    const probs = makeProbs(eq);
    const mv = ma.move || {};
    const notation = bgfFmtNotation(mv.from || [], mv.to || [], pid, boardBefore);
    const display = bgfCanonicalNotation(mv.from || [], mv.to || [], pid, boardBefore, d1, d2);
    moveOptions.push({
      equity: round4(emg),
      played: Boolean(ma.played),
      emg,
      matchEquity: flt(eq.matchEquity),
      probs,
      ply: ma.ply,
      move: notation, // per-hop string; buildAlternatives -> steps
      notation: display, // canonical display string (bar/off, collapsed)
    });
  }

  if (!moveOptions.length) return null;

  const bestEmg = moveOptions[0].equity;
  const playedOpt = moveOptions.find(o => o.played) || moveOptions[moveOptions.length - 1];
  const playedEmg = playedOpt.equity;

  // Compute alternatives (structured move steps + derived notation)
  const alternatives = buildAlternatives(moveOptions, moverIsWhite, d1, d2,
    fromBgfAbsFrame(boardBefore, pid));

  const checkerErr = flt(prObj?.checkerError ?? -99);
  const checkerCnt = checkerErr > -98;

  const equityLoss = round4(Math.max(0.0, bestEmg - playedEmg));

  const analysis = {
    best_equity: bestEmg,
    played_equity: playedEmg,
    equity_loss: equityLoss,
    decision: checkerCnt,
    alternatives,
  };

  if (moveOptions[0].probs.length) analysis.eval = _probsToEval(moveOptions[0].probs);

  return analysis;
}

// ---------------------------------------------------------------------------
// Cube decision analysis
// ---------------------------------------------------------------------------

function cubeDecisionAnalysis(cd, eqFull, plyLevelStr, counted = null) {
  const eqNdRaw = flt(cd.eqNoDouble);
  if (eqNdRaw <= SENTINEL / 2) return null;

  const emg = flt(eqFull.emg);
  const meq = flt(eqFull.matchEquity);
  const eqDpRaw = flt(cd.eqDoublePass);

  const denom = 1.0 - emg;
  if (Math.abs(denom) < 1e-9) return null;
  const half = (eqDpRaw - meq) / denom;
  const center = eqDpRaw - half;
  if (Math.abs(half) < 1e-9) return null;

  const eqNd = round4((eqNdRaw - center) / half);
  const eqDt = round4((flt(cd.eqDoubleTake) - center) / half);
  const eqDp = 1.0;

  const optAction = stateAction(cd.stateOnMove);
  const actAction = cd.hasDoubled ? "double" : "no_double";
  const probs = makeProbs(eqFull);
  const doublerErr = optAction === actAction ? 0.0 : round4(Math.abs(eqNd - Math.min(eqDt, eqDp)));

  const analysis = {
    correct_action: optAction || "no_double",
    played_action: actAction,
    no_double_equity: eqNd,
    double_take_equity: eqDt,
    double_pass_equity: eqDp,
    equity_loss: doublerErr,
    // A double counts toward PR unless the cube is trivial AND the doubler
    // made no error -- mirrors game_eval._eval_cube_decision (doubler_counts).
    // BGBlitz's own counted-ness wins when the file records it.
    decision: counted != null
      ? counted
      : !(trivialCube(eqNd, eqDt, eqDp) && doublerErr < 0.001),
  };
  if (probs.length) analysis.eval = _probsToEval(probs);
  if (plyLevelStr) analysis.eval_level = plyLevelStr;
  return analysis;
}

// ---------------------------------------------------------------------------
// Cube response analysis
// ---------------------------------------------------------------------------

function cubeResponseAnalysis(cd, eqFull, noDoubleEquity, counted = null) {
  const eqNdRaw = flt(cd.eqNoDouble);
  if (eqNdRaw <= SENTINEL / 2) return null;

  const emg = flt(eqFull.emg);
  const meq = flt(eqFull.matchEquity);
  const eqDpRaw = flt(cd.eqDoublePass);

  const denom = 1.0 - emg;
  if (Math.abs(denom) < 1e-9) return null;
  const half = (eqDpRaw - meq) / denom;
  const center = eqDpRaw - half;
  if (Math.abs(half) < 1e-9) return null;

  const eqDt = round4((flt(cd.eqDoubleTake) - center) / half);
  const eqDp = 1.0;

  const optResp = stateResponse(cd.stateOther);
  const hasAccepted = cd.hasAccepted;
  const actResp = hasAccepted ? "take" : "pass";
  const probs = makeProbs(eqFull);

  const analysis = {
    correct_action: optResp || "take",
    played_action: actResp,
    double_take_equity: eqDt,
    double_pass_equity: eqDp,
    equity_loss: optResp === actResp ? 0.0 : round4(Math.abs(eqDt - eqDp)),
    // A take/pass counts toward PR unless it is trivial (take and pass
    // equities within 0.001) -- mirrors game_eval (resp_counts).
    decision: counted != null ? counted : !trivialTakePass(eqDt, eqDp),
  };
  if (noDoubleEquity != null) analysis.no_double_equity = noDoubleEquity;
  if (probs.length) analysis.eval = _probsToEval(probs);
  return analysis;
}

// ---------------------------------------------------------------------------
// Embedded cube analysis (no-double decision on a checker ply)
// ---------------------------------------------------------------------------

// Whether BGBlitz counted a cube decision at this ply. It stores a per-ply
// `pr.cubeError` with a -99 sentinel meaning "not counted" (same convention as
// checkerError); its match-level cubeCnt is exactly the number of non-sentinel
// entries and its reported PR divides by that count (verified against the
// BGBlitz app). Counted-ness cannot be re-derived from the cubeDecision fields
// -- identical-looking NO_DOUBLE/close=false plies appear both counted and not.
function prCubeCounted(pr) {
  return flt((pr || {}).cubeError ?? -99) > -98;
}

/**
 * The cube sub-analysis for a live cube on a checker ply, as `[key, sub]`.
 *
 * The key is returned rather than left for the caller to work out, because the
 * two are one decision: which shape gets built and which field it belongs in
 * are both `stateAction(state) === "double"`. Splitting them is what broke --
 * the caller used to re-derive the key and got `RE_DOUBLE` wrong, filing a
 * missed redouble under `cube_decision`, whose `equity_loss` is zero by
 * definition on disk. The error was real in memory and gone from the `.gvab`.
 *
 * Mirrors `export.js`'s `_cubeSubAnalysis`, which returns `[key, sub]` for the
 * same reason.
 */
export function embeddedCubeAnalysis(cdChk, eqObj, isMoneyGame, plyLevelStr, counted = false) {
  const state = cdChk.stateOnMove;
  if (state == null || cdChk.hasDoubled != null) return null;

  const emgChk = flt(eqObj.emg);
  const meqChk = flt(eqObj.matchEquity);
  const eqNdRaw = flt(cdChk.eqNoDouble);
  const eqDpRaw = flt(cdChk.eqDoublePass);

  if (eqNdRaw <= SENTINEL / 2) return null;
  const denom = 1.0 - emgChk;
  if (Math.abs(denom) < 1e-9) return null;
  const half = (eqDpRaw - meqChk) / denom;
  const center = eqDpRaw - half;
  if (Math.abs(half) < 1e-9) return null;

  const eqNd = round4((eqNdRaw - center) / half);
  const eqDt = round4((flt(cdChk.eqDoubleTake) - center) / half);
  const eqDp = 1.0;

  const optAction = stateAction(state);
  const probs = makeProbs(eqObj);

  if (optAction === "double") {
    const md = {
      no_double_equity: eqNd,
      double_take_equity: eqDt,
      double_pass_equity: eqDp,
      equity_loss: round4(Math.max(0.0, Math.min(eqDt, eqDp) - eqNd)),
      correct_action: "double",
      // No `decision` here, though BGBlitz records one. A CUBE type=2 entry has
      // a single decision bit and no way to spell "the source said nothing", so
      // storing BGBlitz's answer would need a new format flag -- and a value
      // the writer cannot keep is worse than none: it would read back one way
      // in the viewer and another from the saved file. Readers derive it from
      // the three equities (`stats.js`'s `_missed_double_counts`), the same
      // rule every other source already relies on.
    };
    // Same pre-roll probabilities the correct-no-double branch keeps.
    if (probs.length) md.eval = _probsToEval(probs);
    if (plyLevelStr) md.eval_level = plyLevelStr;
    return ["missed_double", md];
  }

  const sub = {
    should_double: false,
    no_double_equity: eqNd,
    double_take_equity: eqDt,
    double_pass_equity: eqDp,
    action: "no_double",
    // Zero, always: the player was right not to double, so the cube cost
    // nothing -- and `CUBE type=4`, where this lands, has no room for anything
    // else. A cube *error* belongs in `missed_double` above.
    equity_loss: 0.0,
    decision: counted,
  };
  if (probs.length) sub.eval = _probsToEval(probs);
  if (plyLevelStr) sub.eval_level = plyLevelStr;
  return ["cube_decision", sub];
}

// ---------------------------------------------------------------------------
// Main converter
// ---------------------------------------------------------------------------

/**
 * Convert a BGBlitz .bgf file (as File/Blob) to OGXM JSON.
 * @param {File|Blob|Uint8Array} fileInput
 * @returns {Promise<object>}
 */
export async function convertBgf(fileInput) {
  const rawBytes = fileInput instanceof Uint8Array ? fileInput : new Uint8Array(await fileInput.arrayBuffer());

  // Split header
  let splitIdx = -1;
  for (let i = 0; i < rawBytes.length; i++) {
    if (rawBytes[i] === 0x0A) { splitIdx = i; break; }
  }
  if (splitIdx < 0) throw new Error("BGF: no newline separator found");

  const headerLine = new TextDecoder("utf-8").decode(rawBytes.slice(0, splitIdx));
  const header = JSON.parse(headerLine);
  const payload = await decompressIfNeeded(rawBytes.slice(splitIdx + 1));

  if (payload[0] !== 0x3A || payload[1] !== 0x29 || payload[2] !== 0x0A) {
    throw new Error("Payload is not Smile (missing magic).");
  }

  const data = decodeSmile(payload);

  const nameGreen = String(data.nameGreen || "");
  const nameRed = String(data.nameRed || "");
  let matchlen = Number(data.matchlen || 0);
  const isMoneyGame = matchlen === 0;

  // Canonical orientation: BGF hardcodes green === white internally (every
  // helper above -- bgfInitialBoardToAbsolute, applyBgfMove, bgfFmtNotation
  // -- assumes it). When canonical white is actually red, every board fed to
  // _ogid must be mirrored at that boundary (canonBoard below), and every
  // green/red-derived "is white" flag must incorporate `flip`. The internal
  // `board` bookkeeping itself is left in its native green-positive frame
  // throughout -- only OGID-bound boards and the moverIsWhite passed to
  // _notationToSteps need the correction.
  const [playerWhite, playerBlack, greenIsWhite] = _canonicalOrientation(nameGreen, nameRed);
  const flip = !greenIsWhite;
  const canonBoard = (b) => (flip ? _flipBoard(b) : b);

  const allGames = data.games || [];
  const gamesOut = [];

  for (let gi = 0; gi < allGames.length; gi++) {
    const g = allGames[gi];
    const scoreGreen = Number(g.scoreGreen || 0);
    const scoreRed = Number(g.scoreRed || 0);
    const scoreWhite = flip ? scoreRed : scoreGreen;
    const scoreBlack = flip ? scoreGreen : scoreRed;
    const isCrawford = Boolean(g.isCrawford);
    const cubeValue = Number(g.initial?.cube || 1);
    const cubeOwnerBgf = Number(g.initial?.cubeOwner || 0);

    const board = bgfInitialBoardToAbsolute(g.initial || {});

    const turn = new _TurnState();
    if (cubeOwnerBgf === -1) turn.cubeOwner = flip ? OGID_CUBE_BLACK : OGID_CUBE_WHITE; // green owns cube
    else if (cubeOwnerBgf === 1) turn.cubeOwner = flip ? OGID_CUBE_WHITE : OGID_CUBE_BLACK; // red owns cube
    if (cubeValue > 1) turn.cubeLog2 = Math.floor(Math.log2(cubeValue));

    const awayGreen = isMoneyGame ? 0 : matchlen - scoreGreen;
    const awayRed = isMoneyGame ? 0 : matchlen - scoreRed;

    const plies = [];
    const raw = g.moves || [];
    let idx = 0;
    let pendingNdEquity = null;

    while (idx < raw.length) {
      const m = raw[idx];
      const fromPts = m.from || [-1, -1, -1, -1];
      const eqObj = m.equity || {};
      const cd = eqObj.cubeDecision || {};
      const hasDoubled = cd.hasDoubled;
      const isCubeRec = fromPts[0] === -1;

      // Phantom terminal record
      if (isCubeRec && hasDoubled == null) {
        idx++;
        continue;
      }

      // Cube action pair
      if (isCubeRec && hasDoubled != null) {
        const mD = m;
        const mR = (idx + 1 < raw.length) ? raw[idx + 1] : {};
        idx += 2;

        const cdD = ((mD.equity || {}).cubeDecision) || {};
        const prD = mD.pr || {};
        const prR = (mR.pr) || {};

        const pid = mD.player;
        const isWhite = (pid === -1) !== flip; // green = white, unless flipped

        const eqFull = mD.equity || {};
        const plyLevelStr = plyLevel(mD.ply);

        // Doubler ply
        const onRoll = isWhite ? "W" : "B";
        const ogidBefore = _ogid(canonBoard(board), {
          cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
          cubeAction: turn.cubeAction, dice: null, onRoll,
          gameState: turn.curState, scoreWhite, scoreBlack,
          matchLength: matchlen, crawford: isCrawford, moveId: turn.moveId,
        });

        turn.awaitingResponse = true;
        turn.curState = OGID_STATE_DOUBLE_OFFERED;
        turn.cubeAction = OGID_ACTION_DOUBLE;

        const ogidAfter = _ogid(canonBoard(board), {
          cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
          cubeAction: turn.cubeAction, dice: null,
          onRoll: isWhite ? "B" : "W",
          gameState: turn.curState, scoreWhite, scoreBlack,
          matchLength: matchlen, crawford: isCrawford, moveId: turn.moveId,
        });

        const analysis = cubeDecisionAnalysis(cdD, eqFull, plyLevelStr, prCubeCounted(prD));

        const ply = {
          color: isWhite ? 1 : 0,
          action_id: 21,
          ogid_before: ogidBefore,
          ogid_after: ogidAfter,
        };
        if (analysis) ply.analysis = analysis;
        plies.push(ply);

        pendingNdEquity = analysis ? analysis.no_double_equity : null;

        // Response ply
        const hasAccepted = cdD.hasAccepted;
        const respIsWhite = !isWhite;
        const onRollR = respIsWhite ? "W" : "B";

        const ogidBeforeR = _ogid(canonBoard(board), {
          cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
          cubeAction: OGID_ACTION_DOUBLE, dice: null, onRoll: onRollR,
          gameState: OGID_STATE_DOUBLE_OFFERED, scoreWhite,
          scoreBlack, matchLength: matchlen,
          crawford: isCrawford, moveId: turn.moveId,
        });

        turn.awaitingResponse = false;
        if (hasAccepted) {
          turn.cubeLog2++;
          turn.cubeOwner = respIsWhite ? OGID_CUBE_WHITE : OGID_CUBE_BLACK;
          turn.curState = OGID_STATE_AFTER_TAKE;
          turn.cubeAction = OGID_ACTION_TAKE;
        } else {
          turn.curState = OGID_STATE_GAME_OVER;
          turn.cubeAction = OGID_ACTION_PASS;
        }

        const ogidAfterR = _ogid(canonBoard(board), {
          cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
          cubeAction: turn.cubeAction, dice: null,
          onRoll: respIsWhite ? "B" : "W",
          gameState: turn.curState, scoreWhite,
          scoreBlack, matchLength: matchlen,
          crawford: isCrawford, moveId: turn.moveId,
        });

        const respAnalysis = cubeResponseAnalysis(cdD, eqFull, pendingNdEquity, prCubeCounted(prR));
        const respPly = {
          color: respIsWhite ? 1 : 0,
          action_id: hasAccepted ? 22 : 23,
          ogid_before: ogidBeforeR,
          ogid_after: ogidAfterR,
        };
        if (respAnalysis) respPly.analysis = respAnalysis;
        plies.push(respPly);

        pendingNdEquity = null;
        continue;
      }

      // Checker move
      const pid = m.player;
      const isWhite = (pid === -1) !== flip; // green = white, unless flipped
      const eqObjFull = m.equity || {};
      const moveAnalysis = m.moveAnalysis || [];
      const prObj = m.pr || {};
      const isDance = m.dancingEquity != null;

      let d1, d2;
      if (isDance) {
        d1 = fromPts[0] !== -1 ? Number(fromPts[0]) : 0;
        const toPtsArr = m.to || [-1, -1, -1, -1];
        d2 = toPtsArr[0] !== -1 ? Number(toPtsArr[0]) : 0;
      } else {
        d1 = Number(m.red || 0);
        d2 = Number(m.green || 0);
      }
      const dice = (d1 && d2) ? [Math.max(d1, d2), Math.min(d1, d2)] : [0, 0];

      // Board before for OGID
      const onRoll = isWhite ? "W" : "B";
      const beforeState = turn.isFirstPly ? OGID_STATE_INITIAL_BOTH : OGID_STATE_ROLLED;

      const ogidBefore = _ogid(canonBoard(board), {
        cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
        cubeAction: turn.cubeAction, dice: dice[0] ? dice : null,
        onRoll, gameState: beforeState, scoreWhite,
        scoreBlack, matchLength: matchlen,
        crawford: isCrawford, moveId: turn.moveId,
      });

      turn.moveId++;
      turn.isFirstPly = false;
      turn.curState = OGID_STATE_CHECKER_DONE;
      turn.cubeAction = OGID_ACTION_NONE;

      // Pre-move board (absolute) — captured before any advance so it can size
      // hit markers / structured steps for this ply's candidates.
      const preBoard = [...board];

      // Is the move the player actually made legal for this roll? BGBlitz has
      // no flag for this (unlike XG's invalidM), and its candidate list is
      // capped at 8 entries, so the only sound test is the rules themselves —
      // see legality.js. A dance record plays no checkers, which is legal only
      // if the position is a real dance.
      const [relMine, relOpp] = bgfRelativeBoard(preBoard, pid);
      const playedPairs = isDance
        ? []
        : bgfPlayedPairs(fromPts, m.to || [-1, -1, -1, -1]);
      const isIllegal = Boolean(d1 && d2)
        && !isPlayLegal(relMine, relOpp, d1, d2, playedPairs);

      // Apply move to board
      if (!isDance) {
        const toPtsList = m.to || [-1, -1, -1, -1];
        // Apply from/to pairs
        for (let fi = 0; fi < fromPts.length; fi++) {
          if (fromPts[fi] === -1) break;
          const tVal = toPtsList[fi] || 0;
          applyBgfMove(board, fromPts[fi], tVal, pid);
        }

        // For a legal play, re-derive the board from BGBlitz's own played
        // sub-moves (same destination, canonical ordering). For an illegal
        // play the top-level move is the only faithful record — every
        // moveAnalysis entry is a *legal* alternative — so keep the board we
        // just advanced with the real move.
        const playedMa = isIllegal ? null : moveAnalysis.find(o => o.played);
        if (playedMa?.move) {
          const postBoard = [...preBoard];
          const pm = playedMa.move;
          const pmFrom = pm.from || [];
          const pmTo = pm.to || [];
          for (let fi = 0; fi < pmFrom.length; fi++) {
            if (pmFrom[fi] === -1) break;
            applyBgfMove(postBoard, pmFrom[fi], pmTo[fi] || 0, pid);
          }
          for (let i = 0; i < 26; i++) board[i] = postBoard[i];
        }
      }

      const ogidAfter = _ogid(canonBoard(board), {
        cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
        cubeAction: turn.cubeAction, dice: null,
        onRoll: isWhite ? "B" : "W",
        gameState: turn.curState, scoreWhite,
        scoreBlack, matchLength: matchlen,
        crawford: isCrawford, moveId: turn.moveId,
      });

      const actionId = (d1 && d2) ? _diceActionId(d1, d2) : 30;

      // Build checker analysis
      let analysis = checkerAnalysis(
        eqObjFull, moveAnalysis, prObj, pid, d1, d2, preBoard, isWhite,
        m.dancingEquity, m.ply,
      );

      // Flag a rules violation so stats tallies it under illegal_moves and PR
      // excludes it (mirrors the XG path's analysis.illegal_move). BGBlitz's
      // candidates are all legal alternatives, so the "played equity" it
      // implies is fiction — never count this ply as a checker decision.
      if (isIllegal) {
        if (analysis == null) analysis = { decision: false, alternatives: [] };
        analysis.illegal_move = true;
        analysis.decision = false;
      }

      // BGBlitz stores this roll's luck two ways whose meaning flips with the
      // luck object's `mode`. The spec's `luck` is the normalized (cube-
      // independent) single-game EMG equity delta -- matching XG's luck to
      // within engine differences -- which is:
      //   * mode === "Match": `luckWeighted` (= luckPlain / MET slope, i.e. MWC
      //     converted back to equity); `luckPlain` is the raw MWC delta.
      //   * mode === "Money": `luckPlain` (already money EMG equity);
      //     `luckWeighted` is that scaled by the cube (weighted === plain*cube),
      //     which inflated money-session totals by up to the cube factor.
      // `luck_mwc` is derived from the stored value on read.
      const luckObj = m.luck || {};
      const luckVal = luckObj.mode === "Money"
        ? luckObj.luckPlain
        : luckObj.luckWeighted;
      if (luckVal != null) {
        // Luck is computed for every rolled ply, not just counted decisions
        // (see gvformat.stats). A ply with no candidate list (forced / not
        // analyzed) still carries luck, so attach a minimal analysis rather
        // than dropping it.
        if (analysis == null) analysis = { decision: false, alternatives: [] };
        analysis.luck = round4(flt(luckVal));
      }

      // Ply-level structured moves (spec's checker steps): parse the
      // actually-played from/to off the pre-move board. Empty on a dance.
      // BGBlitz records only a move's endpoints, so a single checker playing
      // both dice ("18/7" off 5-6) leaves the intermediate point to be
      // inferred. Hand the splitter the pre-move board in the mover's own
      // numbering so it rejects an intermediate the opponent has made --
      // without it the tie-break picks the larger die first and can route the
      // checker through a made point, which replays as a hit and corrupts the
      // board from there on.
      const playedNotation = isDance
        ? ""
        : bgfFmtNotation(fromPts, m.to || [-1, -1, -1, -1], pid, preBoard);
      const plyMoves = isDance
        ? []
        : _notationToSteps(
            playedNotation, isWhite, d1, d2,
            Array.from({ length: 26 }, (_, p) => relMine[p] - relOpp[p]),
          );

      // Add embedded cube analysis
      const cdChk = eqObjFull.cubeDecision || {};
      const chkState = cdChk.stateOnMove;
      const cubeCounted = prCubeCounted(prObj);
      if (chkState && cdChk.hasDoubled == null) {
        const emb = embeddedCubeAnalysis(cdChk, eqObjFull, isMoneyGame, plyLevel(m.ply), cubeCounted);
        if (emb != null && analysis != null) {
          const [key, sub] = emb;
          analysis[key] = sub;
        }
      }
      // A cube decision BGBlitz counted but stored no equities for is dropped,
      // deliberately. There is nothing to put in a CUBE record -- the record
      // *is* the three equities -- so keeping it would mean carrying a bare
      // "+1 decision" that the format cannot hold and the viewer cannot draw,
      // and the saved match would then disagree with the one on screen.
      //
      // It is not a loss worth chasing. In the one match where this appeared
      // it fired exactly once in 215 pre-roll cube decisions, on a dead cube
      // (post-Crawford, the player on roll 1-away, so no double is possible)
      // -- and BGBlitz wrote its own "not counted" sentinel on the four
      // identical dead-cube plies later in that same game. The lone marked
      // one is a BGBlitz bookkeeping slip, not a decision, and our own
      // triviality rules already give the right answer by ignoring it.

      // BGBlitz has no invalid-play flag, but a site can still hand it a play
      // that broke the rules, and the steps then overflow what a ply record
      // holds. Same ladder as the other two converters.
      const fitted = (actionId >= 0 && actionId <= 20)
        ? fitMoveSteps(plyMoves, playedNotation, isWhite, d1, d2)
        : plyMoves;
      if (fitted === null) {
        plies.push(setPositionPly(
          isWhite, d1, d2, canonBoard(board), ogidBefore, ogidAfter));
        idx++;
        continue;
      }

      const ply = {
        color: isWhite ? 1 : 0,
        action_id: actionId,
        d1, d2,
        moves: fitted, // structured steps, parsed from the played from/to
        ogid_before: ogidBefore,
        ogid_after: ogidAfter,
      };
      if (analysis) ply.analysis = analysis;
      plies.push(ply);

      idx++;
    }

    // Game result
    const wonPts = Number(g.wonPoints || 0);

    // Determine winner
    let winnerName;
    if (gi + 1 < allGames.length) {
      const nextG = allGames[gi + 1];
      winnerName = Number(nextG.scoreGreen || 0) > scoreGreen ? nameGreen : nameRed;
    } else {
      // `data.finalGreen` may legitimately be 0 (green scored nothing) --
      // `||` would wrongly treat that as "missing" and substitute matchlen,
      // matching Python's `data.get("finalGreen", matchlen)` only when the
      // key itself is absent requires `??`, not `||`.
      const finalGreen = Number(data.finalGreen ?? matchlen);
      winnerName = finalGreen > scoreGreen ? nameGreen : nameRed;
    }

    const winnerIsWhite = winnerName === playerWhite;

    // Result type
    let resultType;
    if (g.wasResignation) resultType = "resign";
    else if (cubeValue > 0) {
      const mult = Math.floor(wonPts / cubeValue);
      resultType = mult === 2 ? "gammon" : mult === 3 ? "backgammon" : "normal";
    } else resultType = "normal";

    // (scoreWhite/scoreBlack already set per-game above)
    const matchCompleteHere = Boolean(matchlen && (
      (winnerIsWhite && scoreWhite + wonPts >= matchlen) ||
      (!winnerIsWhite && scoreBlack + wonPts >= matchlen)
    ));

    if (winnerName) {
      let actionId;
      if (resultType === "resign") actionId = matchCompleteHere ? 28 : 27;
      else if (resultType === "pass") actionId = 26;
      else actionId = matchCompleteHere ? 26 : 24;
      // A terminal ply names the winner: nobody *does* a game-over, and
      // ogxm_replay.cpp just carries the winner through. Resignation is the one
      // exception -- it is an act, and the player who resigns is the one who
      // lost -- so 27/28 gets the resigner, both as the ply's colour (`color`
      // is documented as the player a ply belongs to) and as the player on roll
      // (the resigner is the one facing the roll they chose not to take).
      const actorIsWhite = RESIGN_ACTIONS.has(actionId) ? !winnerIsWhite : winnerIsWhite;
      const onRollE = actorIsWhite ? "W" : "B";

      const endState = turn.curState;
      const endCubeAction = turn.cubeAction;
      const ogidBeforeEnd = _ogid(canonBoard(board), {
        cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
        cubeAction: endCubeAction, dice: null, onRoll: onRollE,
        gameState: endState, scoreWhite, scoreBlack,
        matchLength: matchlen, crawford: isCrawford, moveId: turn.moveId,
      });
      const ogidAfterEnd = _ogid(canonBoard(board), {
        cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
        cubeAction: endCubeAction, dice: null,
        onRoll: actorIsWhite ? "B" : "W",
        gameState: endState, scoreWhite, scoreBlack,
        matchLength: matchlen, crawford: isCrawford, moveId: turn.moveId,
      });
      plies.push({
        color: actorIsWhite ? 1 : 0,
        action_id: actionId,
        ogid_before: ogidBeforeEnd,
        ogid_after: ogidAfterEnd,
      });
    }

    gamesOut.push({
      game_index: gi,
      winner: winnerIsWhite ? 0 : 1,
      points_won: wonPts,
      is_crawford: isCrawford,
      plies,
    });
  }

  // Match-level fields
  const finalGreenScore = Number(data.finalGreen || 0);
  const finalRedScore = Number(data.finalRed || 0);
  const scoreWhite = flip ? finalRedScore : finalGreenScore;
  const scoreBlack = flip ? finalGreenScore : finalRedScore;

  let matchResult;
  if (matchlen && scoreWhite >= matchlen) matchResult = 1;
  else if (matchlen && scoreBlack >= matchlen) matchResult = 2;
  else matchResult = 0;

  const eventStr = nonempty(data.event);
  const siteStr = nonempty(data.site);

  let maxPly = 0;
  for (const g of allGames) {
    for (const m of (g.moves || [])) {
      const p = m.ply;
      if (p && Number(p) > maxPly) maxPly = Number(p);
    }
  }

  let timestamp = 0;
  const dateStr = data.date || "";
  if (dateStr) {
    try {
      const d = dateStr.replace(/\./g, "-");
      const ms = new Date(`${d}T00:00:00Z`).getTime();
      // An unparseable date yields NaN, which JSON-serialises to null; Python
      // raises and leaves 0. Keep the two mirrors agreeing on 0.
      timestamp = Number.isFinite(ms) ? Math.floor(ms / 1000) : 0;
    } catch {
      // ignore
    }
  }

  return {
    match_length: matchlen,
    player_white: playerWhite,
    player_black: playerBlack,
    white_score: scoreWhite,
    black_score: scoreBlack,
    result: matchResult,
    source: 4,
    timestamp,
    crawford: Boolean(data.useCrawford),
    jacoby: Boolean(data.useJacoby),
    beaver: Boolean(data.useBeaver),
    cube_limit: Number(data.cubeLimit ?? 64),
    event: eventStr,
    site: siteStr,
    analysis_info: {
      ply: Math.max(1, maxPly),
      eval_level: `${Math.max(1, maxPly)}ply`,
      model_id: "bgblitz",
      timestamp,
    },
    games: gamesOut,
  };
}
