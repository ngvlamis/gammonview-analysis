// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Pure-JS converter: eXtreme Gammon .xg match files -> OGXM JSON.
// Binary parsing via DataView; zlib decompression via pako.

import {
  _TurnState, _ogid, _diceActionId, _flipBoard, _notationToSteps,
  _notationToStepsUnsplit, _p1ToAbsolute, _stepsPerRoll,
  fitMoveSteps, setPositionPly,
  _canonicalOrientation, _STARTING_BOARD_P1,
  _OGID_STATE_INITIAL_BOTH, _OGID_STATE_ROLLED, _OGID_STATE_CHECKER_DONE,
  _OGID_STATE_DOUBLE_OFFERED, _OGID_STATE_AFTER_TAKE, _OGID_STATE_GAME_OVER,
  _OGID_ACTION_NONE, _OGID_ACTION_DOUBLE, _OGID_ACTION_TAKE, _OGID_ACTION_PASS,
  _OGID_CUBE_WHITE, _OGID_CUBE_BLACK,
} from "./export.js";
import { canonicalNotation, fromXgP1Frame } from "./notation.js";
import { ACTION_SET_POSITION, RESIGN_ACTIONS } from "./constants.js";
import { cleanPlace } from "./place.js";

// ---------------------------------------------------------------------------
// pako lazy-loader
// ---------------------------------------------------------------------------
//
// A bare specifier, resolved by the consumer: pako is a declared dependency, so
// Node and every bundler find it locally. This used to fall back to a jsdelivr
// URL when the bare import failed, for a browser with no bundler and no import
// map -- but share.js imports pako as a bare specifier with no fallback, so
// that environment was broken either way. The fallback bought no portability
// and cost the package an undeclared runtime reach-out to a third-party CDN, on
// an unpinned major. If a no-bundler browser target is ever wanted, an import
// map is the fix, and it fixes every module at once rather than this one.
//
// Lazy on purpose: importing this module must not drag zlib in for a caller
// that only reads .gvab.

let _Inflate = null;
async function getInflate() {
  if (_Inflate) return _Inflate;
  const mod = await import("pako");
  _Inflate = mod.Inflate;
  return _Inflate;
}

async function decompressZlib(data) {
  // XG files contain two concatenated zlib streams; we want only the first.
  // Using false (Z_NO_FLUSH) instead of true (Z_FINISH) prevents pako from attempting
  // to decompress the second stream after the first ends — Z_FINISH causes pako to
  // continue past the Z_STREAM_END boundary and pick up extra bytes.
  // DecompressionStream can't be used reliably here because browsers differ in whether
  // they throw or silently truncate output when extra bytes follow the first stream.
  const Inflate = await getInflate();
  const inflator = new Inflate();
  const chunks = [];
  inflator.onData = (chunk) => chunks.push(new Uint8Array(chunk));
  try {
    inflator.push(data, false);
  } catch (e) {
    if (chunks.length === 0) throw new Error(`Decompression failed: ${e.message}`);
  }
  const total = chunks.reduce((s, c) => s + c.length, 0);
  if (total === 0) throw new Error("Decompression failed: no output");
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

// ---------------------------------------------------------------------------
// Local helpers (not exported from export.js)
// ---------------------------------------------------------------------------

// XG leaves a record's probability block all-zero when it has nothing to say
// about the position. A zero *win* probability is not that: a play that is a
// certain loss reads win = 0 with a real gammon_loss beside it, and the last
// few plies of a lost game are full of them. Guarding on `probs[0] > 0` threw
// those evaluations away, so the top play on such a ply showed equities with
// no probabilities under them.
//
// An unwritten block is five zeros *and* a zero equity. No genuine evaluation
// is: equity 0 means a roughly even game, which cannot sit beside a zero win
// probability. So the pair separates the two cleanly, and the blank-record
// protection the old guard was really there for survives.
export function _hasEval(probs, equity) {
  return probs.some((p) => p !== 0) || equity !== 0;
}

function _probsToEval(probs) {
  const [win, gwin, bgwin, gloss, bgloss] = probs;
  const equity = win + gwin + bgwin - gloss - bgloss;
  return {
    win, gammon_win: gwin, bg_win: bgwin,
    gammon_loss: gloss, bg_loss: bgloss, equity: Math.round(equity * 10000) / 10000,
  };
}

// ---------------------------------------------------------------------------
// XG binary format constants
// ---------------------------------------------------------------------------

const MAGIC = 0x484D4752;
const RICH_GAME_HEADER_SIZE = 8232;
const SAVE_REC_SIZE = 2560;
const NOT_ANALYZED = -1000.0;

const TS_HEADER_MATCH = 0;
const TS_HEADER_GAME = 1;
const TS_CUBE = 2;
const TS_MOVE = 3;
const TS_FOOTER_GAME = 4;
const TS_FOOTER_MATCH = 5;

// Delphi TDateTime epoch (1899-12-30 UTC)
const DELPHI_EPOCH_MS = Date.UTC(1899, 11, 30);

// ---------------------------------------------------------------------------
// Delphi TDateTime -> unix timestamp
// ---------------------------------------------------------------------------

function delphiToUnix(dt) {
  if (dt <= 0) return 0;
  try {
    const days = Math.floor(dt);
    const secs = Math.floor((dt - days) * 86400);
    return Math.floor((DELPHI_EPOCH_MS + (days * 86400 + secs) * 1000) / 1000);
  } catch {
    return 0;
  }
}

// ---------------------------------------------------------------------------
// XG eval level name
// ---------------------------------------------------------------------------

// XG's own level codes, mapped onto the canonical OGXM eval levels rather than
// kept under XG's names. The names are not decoration: an eval level has to
// survive `.gvab`, and GVAN encodes a level as one byte -- a 4-bit depth plus
// the truncated/rollout/database flags -- so anything outside that vocabulary
// encodes as 0, which means "same as the header level" on read. An "xgroller+"
// alternative therefore came back from a saved match claiming the match's plain
// ply depth, silently, while the same file dragged into the viewer showed the
// real level.
//
// The canonical names fit because they describe what XG actually does. The
// three XG Roller settings *are* short truncated rollouts (1000/1001/1002 ->
// truncated1/2/3), and the two opening-book codes (998/999) are a lookup, not a
// search, which is what `database` names. Readers that want XG's own wording
// back can key it off `analysis_info.model_id === "xg"`; nothing is lost that
// the file does not already say.
const LEVEL_NAMES = {
  0: "1ply", 1: "2ply", 2: "3ply", 3: "4ply", 4: "5ply", 5: "6ply", 6: "7ply",
  12: "3ply", 100: "rollout", 998: "database", 999: "database",
  1000: "truncated1", 1001: "truncated2", 1002: "truncated3",
};

// null, not a made-up name, for a code we do not recognise. The vocabulary
// above is the whole of what a `.gvab` can store, so a "level_7" would go the
// way "xgroller+" used to: encoded as 0, read back as the header level, with
// the file claiming a depth XG never reported. Saying nothing is the honest
// answer and the one every caller here already handles -- each assigns
// `eval_level` only if this returns something.
function evalLevelName(code) {
  return LEVEL_NAMES[code] ?? null;
}

export { evalLevelName as _evalLevelName };

// Minimum spread (max - min) across a ply's candidate equities for the checker
// play to count as a PR decision. Not a triviality threshold: XG counts
// essentially every play with a real choice, and only drops the ones where the
// candidates are indistinguishable at its own equity display precision -- in
// practice already-decided positions, where every legal move scores the same.
// Empirically identified against XG's displayed PR on six player-sides, whose
// feasible intervals intersect in (5.0e-5, 1.33e-4]. The candidate list is NOT
// sorted by equity, so max-min over the whole list is the well-defined form.
// Mirrors gvformat.xg._CHECKER_SPREAD_EPS.
export const CHECKER_SPREAD_EPS = 1e-4;

// Cube decision so clear it should not count toward PR. Mirrors
// gvformat.xg._trivial_cube / gvanalysis.game_eval._trivial_cube.
export function trivialCube(nd, dt, dp) {
  return (
    Math.abs(nd - Math.min(dt, dp)) < 0.001
    || (nd - dt) > 0.200
    || (nd - dp) > 0.200
    || (nd < -0.900 && dt < -0.900)
  );
}

// Take/pass response so clear it should not count toward PR. Mirrors
// gvanalysis.game_eval._trivial_take_pass.
export function trivialTakePass(dt, dp) {
  return Math.abs(dt - dp) < 0.001;
}

// Classify a no-double cube decision from its (rounded) equities. Returns
// { key, sub } with key "missed_double" or "cube_decision", or null for an
// unanalyzed row. Optimal action derived from equities (double iff
// min(dt,dp) > nd), matching gvformat.xg._xg_embedded_cube -- not doubleChoice.
export function embeddedCube(nd, dt, dp) {
  if (dp === 0.0) return null;
  const best = Math.min(dt, dp);
  if (best > nd) {
    return { key: "missed_double", sub: {
      no_double_equity: nd,
      double_take_equity: dt,
      double_pass_equity: dp,
      equity_loss: r4(best - nd),
      correct_action: "double",
    } };
  }
  return { key: "cube_decision", sub: {
    should_double: false,
    no_double_equity: nd,
    double_take_equity: dt,
    double_pass_equity: dp,
    action: "no_double",
    equity_loss: 0.0,
    decision: !trivialCube(nd, dt, dp),
  } };
}

// ---------------------------------------------------------------------------
// Binary reader helpers
// ---------------------------------------------------------------------------

function readPascalAnsi(buf, offset) {
  const n = buf[offset];
  const bytes = new Uint8Array(buf.buffer, buf.byteOffset + offset + 1, n);
  return new TextDecoder("latin1").decode(bytes);
}

function readTShortUnicode(buf, offset, length = 129) {
  const chars = [];
  for (let i = 0; i < length; i++) {
    const code = buf.getUint16(offset + i * 2, true);
    if (code === 0) break;
    chars.push(String.fromCharCode(code));
  }
  return chars.join("");
}

function readInt32LE(buf, offset) {
  return buf.getInt32(offset, true);
}

function readFloatLE(buf, offset) {
  return buf.getFloat32(offset, true);
}

function readDoubleLE(buf, offset) {
  return buf.getFloat64(offset, true);
}

function readUint32LE(buf, offset) {
  return buf.getUint32(offset, true);
}

function readInt16LE(buf, offset) {
  return buf.getInt16(offset, true);
}

// ---------------------------------------------------------------------------
// Record parsers
// ---------------------------------------------------------------------------

function parseHeaderMatch(chunk) {
  const buf = new DataView(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  const player1 = readTShortUnicode(buf, 880) || readPascalAnsi(chunk, 9);
  const player2 = readTShortUnicode(buf, 1138) || readPascalAnsi(chunk, 50);
  const matchLength = readInt32LE(buf, 92);
  const crawford = chunk[100] !== 0;
  const jacoby = chunk[101] !== 0;
  const beaver = chunk[102] !== 0;
  const elo1 = readDoubleLE(buf, 104);
  const elo2 = readDoubleLE(buf, 112);
  const date = readDoubleLE(buf, 128);
  const event = readTShortUnicode(buf, 622) || readPascalAnsi(chunk, 136);
  const location = readTShortUnicode(buf, 1396);
  const cubeLimitCode = readInt32LE(buf, 612);
  return {
    player1, player2, matchLength, crawford, jacoby, beaver,
    elo1, elo2, date, event, location, cubeLimitCode,
  };
}

function parseHeaderGame(chunk) {
  const buf = new DataView(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  return {
    score1: readInt32LE(buf, 12),
    score2: readInt32LE(buf, 16),
    isCrawford: chunk[20] !== 0,
    gameNumber: readInt32LE(buf, 48),
  };
}

function parseCubeRecord(chunk) {
  const buf = new DataView(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  const dd = 64;
  return {
    actif: readInt32LE(buf, 12),
    doubled: readInt32LE(buf, 16),
    take: readInt32LE(buf, 20),
    cubeB: readInt32LE(buf, 32),
    level: readInt32LE(buf, dd + 28),
    equB: readFloatLE(buf, dd + 88),
    equDouble: readFloatLE(buf, dd + 92),
    equDrop: readFloatLE(buf, dd + 96),
    // (doubleChoice at dd+102 intentionally not read: cube actions are derived
    // from equities, never from XG's unreliable double_choice flag.)
    evalNd: Array.from({ length: 7 }, (_, i) => readFloatLE(buf, dd + 60 + i * 4)),
    evalDt: Array.from({ length: 7 }, (_, i) => readFloatLE(buf, dd + 104 + i * 4)),
    errCube: readDoubleLE(buf, 200),
    errTake: readDoubleLE(buf, 216),
  };
}

function parseMoveRecord(chunk) {
  const buf = new DataView(chunk.buffer, chunk.byteOffset, chunk.byteLength);

  // Positions (26 signed bytes each)
  const posBefore = Array.from(new Int8Array(chunk.buffer, chunk.byteOffset + 9, 26));
  const posAfter = Array.from(new Int8Array(chunk.buffer, chunk.byteOffset + 35, 26));

  const actifp = readInt32LE(buf, 64);
  const movesRaw = Array.from(new Int8Array(chunk.buffer, chunk.byteOffset + 68, 8));
  const dice = [readInt32LE(buf, 100), readInt32LE(buf, 104)];
  const cubeA = readInt32LE(buf, 108);
  const nmoves = readInt32LE(buf, 120);
  const errMove = readDoubleLE(buf, 2312);
  const errLuck = readDoubleLE(buf, 2320);
  const compChoice = readInt32LE(buf, 2328);
  const initEq = readDoubleLE(buf, 2336);
  const analyzeM = readInt32LE(buf, 2472);
  const invalidM = readInt32LE(buf, 2480);

  // Candidate moves
  const candidates = [];
  for (let i = 0; i < Math.min(nmoves, 32); i++) {
    const posI = Array.from(new Int8Array(chunk.buffer, chunk.byteOffset + 192 + i * 26, 26));
    const movI = Array.from(new Int8Array(chunk.buffer, chunk.byteOffset + 1024 + i * 8, 8));
    const levelI = readInt16LE(buf, 1280 + i * 4);
    const evalI = Array.from({ length: 7 }, (_, j) => readFloatLE(buf, 1408 + i * 28 + j * 4));

    // XG order [loseBG, loseG, loseS, winS, winG, winBG, equity] -> [winS, winG, winBG, loseG, loseBG]
    const probs = [evalI[3], evalI[4], evalI[5], evalI[1], evalI[0]];
    const equity = evalI[6];
    const isPlayed = posI.every((v, j) => v === posAfter[j]);

    candidates.push({ pos: posI, moves: movI, level: levelI, probs, equity, isPlayed });
  }

  // Parse the played move into from/to pairs
  const playedFrom = [];
  const playedTo = [];
  for (let i = 0; i < movesRaw.length; i += 2) {
    if (movesRaw[i] === -1) break;
    playedFrom.push(movesRaw[i]);
    if (i + 1 < movesRaw.length) playedTo.push(movesRaw[i + 1]);
  }

  return {
    actifp, posBefore, posAfter, movesRaw, playedFrom, playedTo,
    dice, cubeA, nmoves,
    candidates, errMove, errLuck, compChoice, initEq,
    level: analyzeM, invalidM,
  };
}

function parseFooterGame(chunk) {
  const buf = new DataView(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  return {
    score1: readInt32LE(buf, 12),
    score2: readInt32LE(buf, 16),
    winner: readInt32LE(buf, 24),
    points: readInt32LE(buf, 28),
    termination: readInt32LE(buf, 32),
  };
}

function parseFooterMatch(chunk) {
  const buf = new DataView(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  return {
    score1: readInt32LE(buf, 12),
    score2: readInt32LE(buf, 16),
    matchResult: readInt32LE(buf, 20),
  };
}

// ---------------------------------------------------------------------------
// Board: XG P1 frame -> absolute
// ---------------------------------------------------------------------------

function xgToAbsolute(pos) {
  const board = new Array(26).fill(0);
  board[25] = pos[0];       // White's bar
  for (let i = 1; i < 25; i++) board[i] = pos[i];
  board[0] = -pos[25];      // Black's bar (negative)
  return board;
}

// ---------------------------------------------------------------------------
// Move formatting (for candidate alternatives notation)
// ---------------------------------------------------------------------------

/**
 * Format an XG move as a notation string, annotating hits with `*`.
 *
 * `fromPts`/`toPts` are 0-indexed points in the *mover's* own numbering
 * (point n = index n-1, bar = 24, off = -1). `board`, when given, is the raw
 * XG P1-frame position (index 1-24 signed, +P1/-P2) as it stood before this
 * move; hops that land on a lone opponent checker get a trailing `*`. A
 * working copy is advanced hop-by-hop so multi-leg plays annotate each hit
 * correctly.
 *
 * Returns `[notation, work]`: `work` is the resulting raw P1-frame board
 * after every hop (same 26-slot convention as `board`), or `null` when
 * `board` was `null`. Callers that need an authoritative post-move board
 * (XG's own `posAfter` field is documented as unreliable -- it "can roll
 * forward across a turn") should use this instead of re-deriving one from
 * `posAfter`.
 */
function fmtXgMove(fromPts, toPts, board = null, moverIsP1 = true) {
  const work = board !== null ? [...board] : null;
  // A mover-point n sits at P1-frame index n for P1, else 25-n for P2.
  const p1Index = (moverPoint) => (moverIsP1 ? moverPoint : 25 - moverPoint);
  const moverSign = moverIsP1 ? 1 : -1;

  const parts = [];
  for (let i = 0; i < fromPts.length; i++) {
    const f = fromPts[i];
    const t = toPts[i];
    // -1 is XG's explicit terminator; an all-zero (f===t===0) pair is unused
    // padding or a "no move" (dance) sub-move — a zero-pip 1/1 is never a real
    // play, so stop rather than emit a bogus "1/1".
    if (f === -1 || (f === 0 && t === 0)) break;
    const fStr = f === 24 ? "bar" : String(f + 1);
    // A bear-off destination is any point off the board. XG encodes the exact
    // bear-off as -1, but an overage roll (e.g. bearing a checker off the
    // 1-point with a 5) lands at a more-negative index; clamp all of them to
    // "off" rather than emitting a negative point (1/-4).
    const tStr = t < 0 ? "off" : String(t + 1);

    let hit = "";
    if (work !== null) {
      // Advance the working board: pull the mover's checker off its source
      // -- the bar (f===24) lives at raw index 0 (P1's own bar, positive
      // count) or 25 (P2's own bar, stored negative); a normal point uses
      // p1Index. The same "-= moverSign" works for both bar slots too (P1:
      // decrements a positive count; P2: increments -- i.e. moves toward
      // zero -- a negative one).
      const srcIdx = f === 24 ? (moverIsP1 ? 0 : 25) : p1Index(f + 1);
      work[srcIdx] -= moverSign;
      // ...and, for a non-bear-off, land it on the destination, hitting a lone
      // opponent blot (sign === -moverSign, count 1) if present.
      if (t >= 0) {
        const di = p1Index(t + 1);
        if (work[di] === -moverSign) {
          hit = "*";
          work[di] = 0;
          // Send the hit opponent checker to *its own* bar: P1's bar is raw
          // index 0 (positive count), P2's is 25 (stored negative) -- the
          // opposite slot from the mover's own bar above.
          work[moverIsP1 ? 25 : 0] -= moverSign;
        }
        work[di] += moverSign;
      }
    }
    parts.push(`${fStr}/${tStr}${hit}`);
  }
  return [parts.join(" "), work];
}

// ---------------------------------------------------------------------------
// Round helper
// ---------------------------------------------------------------------------

function r4(v) { return Math.round(v * 1e4) / 1e4; }

// ---------------------------------------------------------------------------
// Cube-decision emission (shared by the in-move and game-ending double paths)
// ---------------------------------------------------------------------------

/**
 * Build the analyzed doubler ply (action_id=21) and its response ply
 * (action_id=22 take / 23 pass) for a real double (cd.doubled === 1).
 *
 * `board` is the board as it stood when the double was offered (pre-roll).
 * For an in-move double this is the following move record's posBefore; for
 * a game-ending double/pass (no following move) it is the last-seen
 * `lastBoard` (the board doesn't change between the last checker move and
 * the double offer). Mutates `turn`'s OGID phase-state fields exactly as
 * the reference replay does (offer -> _OGID_STATE_DOUBLE_OFFERED, then
 * _OGID_STATE_AFTER_TAKE on a take or _OGID_STATE_GAME_OVER on a pass,
 * advancing cubeLog2/cubeOwner on a take).
 *
 * `flip` is whether canonical white is XG's player-2 (cd.actif is XG's raw
 * +1=P1/-1=P2 mover flag, so `dblIsWhite = (cd.actif === 1) !== flip`).
 *
 * Returns { doublerPly, responsePly, hasTake }; the caller pushes both
 * plies and, on a take, updates its own cubeValue/cubeOwnerBgf bookkeeping.
 */
function emitDoubleResponse(cd, board, turn, scoreWhite, scoreBlack, matchLength, isCrawford, evalLevel, flip) {
  const dblIsP1 = cd.actif === 1;
  const dblIsWhite = dblIsP1 !== flip;
  const onRoll = dblIsWhite ? "W" : "B";

  // Doubler ply
  const ogidBefore = _ogid(board, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: turn.cubeAction, dice: null, onRoll,
    gameState: turn.curState, scoreWhite, scoreBlack,
    matchLength, crawford: isCrawford, moveId: turn.moveId,
  });
  turn.awaitingResponse = true;
  turn.curState = _OGID_STATE_DOUBLE_OFFERED;
  turn.cubeAction = _OGID_ACTION_DOUBLE;
  const ogidAfter = _ogid(board, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: turn.cubeAction, dice: null,
    onRoll: dblIsWhite ? "B" : "W",
    gameState: turn.curState, scoreWhite, scoreBlack,
    matchLength, crawford: isCrawford, moveId: turn.moveId,
  });

  const probsNd = [cd.evalNd[3], cd.evalNd[4], cd.evalNd[5], cd.evalNd[1], cd.evalNd[0]];
  const probsDt = [cd.evalDt[3], cd.evalDt[4], cd.evalDt[5], cd.evalDt[1], cd.evalDt[0]];
  // Optimal action from equities (double iff realized double value beats
  // no-double), matching game_eval.
  const optimalAction = Math.min(cd.equDouble, cd.equDrop) > cd.equB ? "double" : "no_double";
  const playerAction = "double";
  const lostEquity = optimalAction === playerAction ? 0.0 : r4(Math.abs(cd.equB - Math.min(cd.equDouble, cd.equDrop)));

  const dblAnalysis = {
    correct_action: optimalAction,
    played_action: playerAction,
    no_double_equity: r4(cd.equB),
    double_take_equity: r4(cd.equDouble),
    double_pass_equity: r4(cd.equDrop),
    equity_loss: lostEquity,
    // A double counts toward PR unless the cube is trivial AND the doubler
    // made no error -- mirrors game_eval._eval_cube_decision (doubler_counts)
    // and stats._missed_double_counts.
    decision: !(trivialCube(cd.equB, cd.equDouble, cd.equDrop) && lostEquity < 0.001),
  };
  if (_hasEval(probsNd, cd.evalNd[6])) dblAnalysis.eval = _probsToEval(probsNd);
  if (evalLevel) dblAnalysis.eval_level = evalLevel;

  const doublerPly = {
    color: dblIsWhite ? 1 : 0,
    action_id: 21,
    ogid_before: ogidBefore,
    ogid_after: ogidAfter,
    analysis: dblAnalysis,
  };

  const pendingNdEquity = dblAnalysis.no_double_equity;

  // Response ply
  const respIsWhite = !dblIsWhite;
  const hasTake = cd.take === 1;
  const respAction = hasTake ? "take" : "pass";

  const onRollR = respIsWhite ? "W" : "B";
  const ogidBeforeR = _ogid(board, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: _OGID_ACTION_DOUBLE, dice: null, onRoll: onRollR,
    gameState: _OGID_STATE_DOUBLE_OFFERED, scoreWhite, scoreBlack,
    matchLength, crawford: isCrawford, moveId: turn.moveId,
  });

  turn.awaitingResponse = false;
  if (hasTake) {
    turn.cubeLog2++;
    turn.cubeOwner = respIsWhite ? _OGID_CUBE_WHITE : _OGID_CUBE_BLACK;
    turn.curState = _OGID_STATE_AFTER_TAKE;
    turn.cubeAction = _OGID_ACTION_TAKE;
  } else {
    turn.curState = _OGID_STATE_GAME_OVER;
    turn.cubeAction = _OGID_ACTION_PASS;
  }

  const ogidAfterR = _ogid(board, {
    cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
    cubeAction: turn.cubeAction, dice: null,
    onRoll: respIsWhite ? "B" : "W",
    gameState: turn.curState, scoreWhite, scoreBlack,
    matchLength, crawford: isCrawford, moveId: turn.moveId,
  });

  // Optimal response from equities: take iff the taken equity is no worse
  // for the responder than passing.
  const optimalResp = cd.equDouble <= cd.equDrop ? "take" : "pass";
  const respLost = optimalResp === respAction ? 0.0 : r4(Math.abs(cd.equDouble - cd.equDrop));

  const respAnalysis = {
    correct_action: optimalResp,
    played_action: respAction,
    double_take_equity: r4(cd.equDouble),
    double_pass_equity: r4(cd.equDrop),
    equity_loss: respLost,
    // A take/pass counts toward PR unless it is trivial (take and pass
    // equities within 0.001) -- mirrors game_eval (resp_counts).
    decision: !trivialTakePass(cd.equDouble, cd.equDrop),
  };
  if (pendingNdEquity != null) respAnalysis.no_double_equity = pendingNdEquity;
  if (_hasEval(probsDt, cd.evalDt[6])) respAnalysis.eval = _probsToEval(probsDt);

  const responsePly = {
    color: respIsWhite ? 1 : 0,
    action_id: hasTake ? 22 : 23,
    ogid_before: ogidBeforeR,
    ogid_after: ogidAfterR,
    analysis: respAnalysis,
  };

  return { doublerPly, responsePly, hasTake };
}

// ---------------------------------------------------------------------------
// Main converter
// ---------------------------------------------------------------------------

/**
 * Convert an eXtreme Gammon .xg file (as ArrayBuffer) to OGXM JSON.
 * @param {Uint8Array} fileBytes - Raw file bytes
 * @returns {Promise<object>}
 */
export async function convertXg(fileBytes) {
  const raw = fileBytes instanceof Uint8Array ? fileBytes : new Uint8Array(fileBytes);

  // We need a File-like object for arrayBuffer(); wrap in a Blob approach:
  // Actually we already have bytes, let's re-implement readXg inline for the bytes.
  const headerView = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  const magic = readUint32LE(headerView, 0);
  if (magic !== MAGIC) throw new Error(`Not an XG file (magic: 0x${magic.toString(16).padStart(8, "0")})`);

  const thumbSize = readUint32LE(headerView, 20);
  const recordStart = RICH_GAME_HEADER_SIZE + thumbSize;
  const compressed = raw.slice(recordStart);

  const decompressed = await decompressZlib(compressed);

  const nrecs = Math.floor(decompressed.length / SAVE_REC_SIZE);
  const records = [];
  for (let i = 0; i < nrecs; i++) {
    const chunk = decompressed.slice(i * SAVE_REC_SIZE, (i + 1) * SAVE_REC_SIZE);
    const recType = chunk[8];
    switch (recType) {
      case TS_HEADER_MATCH: records.push(["header_match", parseHeaderMatch(chunk)]); break;
      case TS_HEADER_GAME: records.push(["header_game", parseHeaderGame(chunk)]); break;
      case TS_CUBE: records.push(["cube", parseCubeRecord(chunk)]); break;
      case TS_MOVE: records.push(["move", parseMoveRecord(chunk)]); break;
      case TS_FOOTER_GAME: records.push(["footer_game", parseFooterGame(chunk)]); break;
      case TS_FOOTER_MATCH: records.push(["footer_match", parseFooterMatch(chunk)]); break;
    }
  }

  // Build output
  const hm = records.find(r => r[0] === "header_match")?.[1];
  if (!hm) throw new Error("No header_match record found");

  const nameP1 = hm.player1;
  const nameP2 = hm.player2;

  // Canonical orientation: XG stores the account player as P1, so hardcoding
  // white=P1 disagrees with other sources' P1. `flip` is whether canonical
  // white is XG's P2 -- every P1-frame board must then be mirrored
  // (_flipBoard) before it reaches _ogid, every P1-relative mover flag must
  // be corrected to `isWhite = isP1 !== flip` for white/black-facing fields
  // (color, onRoll, cubeOwner), and the two scores are swapped. Candidate/
  // played moves in mover-own numbering are untouched; only the
  // `_notationToSteps` call (mover-own -> absolute white numbering) needs
  // `isWhite`, NOT the `fmtXgMove` hit-detection call (which stays `isP1`, a
  // frame-internal fact about the raw P1-frame board it's diffing).
  const [playerWhite, playerBlack, p1IsWhite] = _canonicalOrientation(nameP1, nameP2);
  const flip = !p1IsWhite;

  let matchLength = hm.matchLength;
  const isMoneyGame = matchLength === 0 || matchLength === 99999;
  if (isMoneyGame) matchLength = 0;
  const crawford = hm.crawford;
  const jacoby = hm.jacoby;
  const beaver = hm.beaver;
  const cubeLimit = hm.cubeLimitCode >= 0 ? (1 << hm.cubeLimitCode) : 64;
  const timestamp = delphiToUnix(hm.date);

  // XG's `location` is OGXM's `site`; the two stay separate fields.
  const eventStr = cleanPlace(hm.event);
  const siteStr = cleanPlace(hm.location);

  // Iterate records to build games
  const gamesOut = [];
  let gi = 0;
  let scoreP1 = 0, scoreP2 = 0;
  let idx = 0;
  let lastBoard = new Array(26).fill(0);

  while (idx < records.length) {
    const [rtype, rdata] = records[idx];

    if (rtype === "header_game") {
      scoreP1 = rdata.score1;
      scoreP2 = rdata.score2;
      const scoreWhite = flip ? scoreP2 : scoreP1;
      const scoreBlack = flip ? scoreP1 : scoreP2;
      const isCrawford = rdata.isCrawford;

      const turn = new _TurnState();
      const plies = [];
      let cubeValue = 1;
      let cubeOwnerBgf = 0;

      // Persistent RAW P1-frame board tracker (XG's own pos-array
      // convention: index 0 = P1's bar (positive), 1-24 signed (+P1/-P2),
      // 25 = P2's bar (negative)), reset every game to the standard
      // starting position. XG's per-record posBefore/posAfter are both
      // individually unreliable (see the comment at the played-move
      // reconstruction below), so every checker ply reads its "before"
      // board from this tracker (carried forward from the previous ply's
      // authoritative fmtXgMove outcome) instead of trusting either raw
      // field. _STARTING_BOARD_P1's 1-24 entries are numerically the same
      // array XG's raw pos-frame uses at the start of a game (both are
      // "positive = P1"); bar slots are 0 either way at game start.
      let rawBoard = [..._STARTING_BOARD_P1];

      idx++;
      let pendingCube = null;
      let m = null;

      while (idx < records.length) {
        const [rtype2, rdata2] = records[idx];

        if (rtype2 === "footer_game") {
          // A game-ending double/pass has no following move record -- the
          // game is over, so the loop reaches footer_game directly with the
          // analyzed double still pending. A taken double never reaches
          // here (the game continues, so a move record follows and the
          // in-move path above already consumed pendingCube, clearing it to
          // null).
          if (pendingCube !== null && pendingCube.doubled === 1) {
            const cd = pendingCube;
            pendingCube = null;
            const evalLevel = evalLevelName(cd.level);
            const { doublerPly, responsePly, hasTake } = emitDoubleResponse(
              cd, lastBoard, turn, scoreWhite, scoreBlack, matchLength, isCrawford, evalLevel, flip,
            );
            plies.push(doublerPly);
            plies.push(responsePly);
            if (hasTake) {
              cubeValue = turn.cubeValue;
              cubeOwnerBgf = cd.actif !== 1 ? 1 : -1;
            }
          }
          idx++;
          break;
        }

        if (rtype2 === "cube") {
          pendingCube = rdata2;
          idx++;
          continue;
        }

        if (rtype2 === "move") {
          m = rdata2;
          const [d1, d2] = m.dice;
          const actifp = m.actifp;
          const isP1 = actifp === 1;
          const isWhite = isP1 !== flip;

          // Board in absolute coordinates, from the persistent RAW P1-frame
          // tracker (NOT m.posBefore -- see rawBoard's comment above),
          // mirrored into canonical white's frame when white is XG's P2.
          // preMoveRaw keeps the pre-move RAW frame around for the
          // alternatives loop below, which runs *after* rawBoard has
          // already been advanced to the played move's outcome.
          const preMoveRaw = rawBoard;
          let boardBefore = xgToAbsolute(rawBoard);
          if (flip) boardBefore = _flipBoard(boardBefore);

          const evalLevel = evalLevelName(m.level);

          // Handle cube pair
          if (pendingCube !== null) {
            const cd = pendingCube;

            if (cd.doubled === 1) {
              pendingCube = null;
              const { doublerPly, responsePly, hasTake } = emitDoubleResponse(
                cd, boardBefore, turn, scoreWhite, scoreBlack, matchLength, isCrawford, evalLevel, flip,
              );
              plies.push(doublerPly);
              plies.push(responsePly);

              if (hasTake) {
                cubeValue = turn.cubeValue;
                cubeOwnerBgf = cd.actif !== 1 ? 1 : -1;
              } else {
                idx++;
                break;
              }
            } else if (cd.doubled !== 0) {
              // Passive/other cube record — discard.
              pendingCube = null;
            }
            // doubled === 0: leave pendingCube set; embedded block below consumes it.
          }

          // Checker move
          const onRoll = isWhite ? "W" : "B";
          const beforeState = turn.isFirstPly ? _OGID_STATE_INITIAL_BOTH : _OGID_STATE_ROLLED;

          const ogidBefore = _ogid(boardBefore, {
            cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
            cubeAction: turn.cubeAction,
            dice: d1 ? [Math.max(d1, d2), Math.min(d1, d2)] : null,
            onRoll, gameState: beforeState, scoreWhite, scoreBlack,
            matchLength, crawford: isCrawford, moveId: turn.moveId,
          });

          turn.moveId++;
          turn.isFirstPly = false;
          turn.curState = _OGID_STATE_CHECKER_DONE;
          turn.cubeAction = _OGID_ACTION_NONE;

          // Reconstruct the played move from the *played candidate's* from/to
          // pairs, NOT a board diff and NOT the +68 `movesRaw` field -- both
          // misencode the play here. XG's `posBefore`/`posAfter` pair is not a
          // clean single-ply transition (its stored position can roll forward
          // across a turn) and its `actifp` mover flag is unreliable (`-1`
          // occurs), so a board diff fabricates impossible plays (>4 hops,
          // phantom bear-offs); `movesRaw` at +68 is in a different encoding
          // again. The candidate DataMoves entry (`c.moves`) is authoritative
          // -- render the played one through the same notation path the
          // alternatives use (`fmtXgMove` -> `_notationToSteps`) so the
          // top-level `moves` matches the played alternative exactly. A dance
          // has no distinct played candidate -> emit nothing.
          // `moverIsP1=isP1` in the fmtXgMove call is deliberate (NOT
          // flipped): it's used only for hit-detection against the raw
          // P1-frame rawBoard, a frame-internal fact. `_notationToSteps`
          // maps mover-own numbering to ABSOLUTE white numbering, so it
          // needs the flip-corrected `isWhite`.
          const playedCand = m.candidates.find(c => c.isPlayed);
          let moveSteps, boardAfter;
          if (playedCand) {
            // `fmtXgMove` renders a no-move (dance) candidate as an empty
            // string, so a dance yields no steps here.
            const [playedNotation, playedWork] = fmtXgMove(
              Array.from({ length: 4 }, (_, i) => playedCand.moves[i * 2]),
              Array.from({ length: 4 }, (_, i) => playedCand.moves[1 + i * 2]),
              rawBoard, isP1,
            );
            // Hand the splitter the pre-move board in the mover's own
            // numbering. XG stores a one-checker two-die play as a single span
            // ("24/18" off a 5-1), leaving the intermediate point to be
            // inferred, and only one of the two routes may be open. Without the
            // board the tie-break takes the larger die first and can route the
            // checker through a point the opponent has *made*, which replays as
            // a hit: their five checkers become one of yours plus a bar
            // checker, and every board after this ply is wrong.
            moveSteps = _notationToSteps(playedNotation, isWhite, d1, d2,
              isWhite ? boardBefore : _flipBoard(boardBefore));
            // An illegal play (`invalidM === 2`: a rules violation the site let
            // through) can use more die-moves than the roll has -- 13/9 with a
            // 3-1, then 12/11, is three hops for a two-hop roll. A ply record
            // holds exactly the roll's hops, so the extra step would be dropped
            // on write and every board replayed after this ply would be one
            // checker off; a later ply then lifts a checker off an empty point,
            // which mints checkers until the position is impossible. Record
            // each span at its own pip distance instead: the dice cannot
            // explain these hops anyway, and it replays to the board XG
            // recorded.
            const fitted = fitMoveSteps(moveSteps, playedNotation, isWhite, d1, d2);
            if (fitted !== null) {
              moveSteps = fitted;
            } else {
              // Still overflowing at one step per span. Keep the collapsed
              // form so the check below sees it is too long and demotes the
              // ply to a set position.
              moveSteps = _notationToStepsUnsplit(playedNotation, isWhite, d1, d2);
            }
            // XG's own posAfter "can roll forward across a turn" (see
            // comment above) -- rederive boardAfter from the authoritative
            // played move instead, and carry it forward as the tracker for
            // the next ply.
            rawBoard = playedWork;
            boardAfter = xgToAbsolute(rawBoard);
            if (flip) boardAfter = _flipBoard(boardAfter);
          } else {
            // A true dance: no checkers moved, so the board is unchanged
            // (rawBoard stays as-is for the next ply).
            moveSteps = [];
            boardAfter = boardBefore;
          }
          lastBoard = boardAfter;

          const ogidAfter = _ogid(boardAfter, {
            cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
            cubeAction: turn.cubeAction, dice: null,
            onRoll: isWhite ? "B" : "W",
            gameState: turn.curState, scoreWhite, scoreBlack,
            matchLength, crawford: isCrawford, moveId: turn.moveId,
          });

          const actionId = (d1 && d2) ? _diceActionId(d1, d2) : 30;

          // Checker analysis
          let analysis = null;
          if (m.candidates.length) {
            // XG flags an illegal play (a rules violation the player actually
            // made) with invalidM === 2. The played candidate is the illegal
            // move; size its error against the best *legal* alternative
            // (excluding the played one), and flag the ply so PR / decision
            // counting excludes it (see stats.js).
            const isIllegal = m.invalidM === 2;
            const playedIdx = m.candidates.findIndex(c => c.isPlayed);
            let bestEq;
            if (isIllegal && playedIdx !== -1 && m.candidates.length > 1) {
              const legalEqs = m.candidates
                .filter((_, i) => i !== playedIdx)
                .map(c => c.equity);
              bestEq = legalEqs.length ? Math.max(...legalEqs) : m.candidates[0].equity;
            } else {
              bestEq = m.candidates[0].equity;
            }

            let playedEq = bestEq;
            let playedProbs = m.candidates[0].probs;
            for (const c of m.candidates) {
              if (c.isPlayed) { playedEq = c.equity; playedProbs = c.probs; break; }
            }

            const equityLoss = r4(Math.max(0.0, bestEq - playedEq));
            const isAnalyzed = m.errMove > NOT_ANALYZED + 1;
            // A checker play counts toward PR only if XG analyzed it AND there
            // was a genuine choice: candidate equities spanning at least
            // CHECKER_SPREAD_EPS. Forced moves (a single candidate) and
            // already-decided positions (every option scores the same) are
            // excluded, as are illegal plies.
            const candEqs = m.candidates.map((c) => c.equity);
            const spread = candEqs.length >= 2
              ? Math.max(...candEqs) - Math.min(...candEqs)
              : 0.0;
            const isDecision = isAnalyzed && !isIllegal
              && spread >= CHECKER_SPREAD_EPS;

            analysis = {
              best_equity: r4(bestEq),
              played_equity: r4(playedEq),
              equity_loss: equityLoss,
              decision: isDecision,
            };
            if (isIllegal) analysis.illegal_move = true;

            // XG's ErrLuck is this roll's `postroll - preroll` equity, the same
            // quantity the spec's `luck` field carries. Unanalyzed rolls carry
            // the NOT_ANALYZED sentinel rather than 0, so guard on it -- a
            // genuine luck of exactly 0.0 is meaningful and must survive.
            if (m.errLuck > NOT_ANALYZED + 1) analysis.luck = r4(m.errLuck);

            if (_hasEval(m.candidates[0].probs, m.candidates[0].equity)) {
              analysis.eval = _probsToEval(m.candidates[0].probs);
            }
            if (evalLevel) analysis.eval_level = evalLevel;

            // Alternatives
            const alts = [];
            for (const c of m.candidates) {
              const alt = {
                equity: r4(c.equity),
                is_played: c.isPlayed,
                diff: r4(c.equity - bestEq),
              };
              if (_hasEval(c.probs, c.equity)) alt.eval = _probsToEval(c.probs);
              const lvl = evalLevelName(c.level);
              if (lvl) alt.eval_level = lvl;
              // Per the OGXM spec, `move` is the structured source of truth
              // (Step[] of {from, pips} in absolute coords) and `notation` is
              // its derived display string. Render the per-hop mover-frame
              // string first (with hit `*` markers, and the candidate's own
              // post-move board `candWork`), then parse it into absolute steps
              // with the same helper the analyze path uses, so XG-import and
              // analyze output are byte-identical (and .gvab-able). The
              // *display* `notation` is rendered separately through the shared
              // board-diff canonicalizer (collapse hops, combine identical
              // legs) so it matches the analyzer and BGF paths exactly.
              const [notation, candWork] = fmtXgMove(
                Array.from({ length: 4 }, (_, i) => c.moves[i * 2]),
                Array.from({ length: 4 }, (_, i) => c.moves[1 + i * 2]),
                preMoveRaw, isP1,
              );
              // Same pre-move board the played move is split against (see the
              // `_notationToSteps` call above). Every candidate starts from
              // this ply's position, and without the board a one-checker
              // two-die alternative is split larger-die-first and can be drawn
              // routing through a point the opponent has made -- the board
              // arrows come straight from these steps, so it shows a checker
              // landing on a stack of enemy checkers and moving on.
              alt.move = _notationToSteps(notation, isWhite, d1, d2,
                isWhite ? boardBefore : _flipBoard(boardBefore));
              // Same collapse the played move gets above, so the played
              // alternative still describes the same play as the ply's own
              // `moves`. Only an illegal play can overflow, and only the played
              // candidate is ever illegal.
              if (alt.move.length > _stepsPerRoll(d1, d2)) {
                alt.move = _notationToStepsUnsplit(notation, isWhite, d1, d2);
              }
              alt.notation = canonicalNotation(
                fromXgP1Frame(preMoveRaw, isP1),
                fromXgP1Frame(candWork, isP1),
                d1, d2,
              );
              alts.push(alt);
            }
            analysis.alternatives = alts;
          }

          // Embedded cube analysis (no-double decision). Classify by equity
          // (min(dt,dp) > nd), matching xg.py / game_eval / export -- NOT XG's
          // doubleChoice flag, which is an unreliable sentinel here.
          if (pendingCube !== null && pendingCube.doubled === 0) {
            const cd = pendingCube;
            pendingCube = null;
            const result = embeddedCube(r4(cd.equB), r4(cd.equDouble), r4(cd.equDrop));
            if (result !== null && analysis !== null) {
              // XG's no-double evaluation is the pre-roll read of the cube
              // decision, so it belongs on either classification -- a missed
              // double is the same position, judged the other way.
              const probsNd = [cd.evalNd[3], cd.evalNd[4], cd.evalNd[5], cd.evalNd[1], cd.evalNd[0]];
              if (_hasEval(probsNd, cd.evalNd[6])) result.sub.eval = _probsToEval(probsNd);
              analysis[result.key] = result.sub;
            }
          }

          let ply;
          if (actionId >= 0 && actionId <= 20
              && moveSteps.length > _stepsPerRoll(d1, d2)) {
            // An illegal play too tangled for even one step per checker (three
            // checkers moved on a two-hop roll, say). No checker ply can carry
            // it, and truncating it would corrupt every board after this one,
            // so state the resulting position outright -- what action 31 is for
            // (the spec notes its optional dice are exactly this case). The
            // play itself is lost; it broke the rules, so there is no move to
            // score.
            ply = setPositionPly(isWhite, d1, d2, boardAfter, ogidBefore, ogidAfter);
          } else {
            ply = {
              color: isWhite ? 1 : 0,
              action_id: actionId,
              d1, d2,
              moves: moveSteps,
              ogid_before: ogidBefore,
              ogid_after: ogidAfter,
            };
            if (analysis) ply.analysis = analysis;
          }
          plies.push(ply);

          idx++;
          continue;
        }
        idx++;
      }

      // Game result
      let footer = null;
      for (let fi = idx - 1; fi < Math.min(idx + 5, records.length); fi++) {
        if (records[fi][0] === "footer_game") { footer = records[fi][1]; break; }
      }

      let wonPts = 0, winnerP1 = false, winnerIsWhite = false;
      if (footer) {
        wonPts = footer.points;
        winnerP1 = footer.winner === 1;
        winnerIsWhite = winnerP1 !== flip;
        const term = footer.termination;
        const base = term % 100;
        const modifier = term - base;
        const resultType = modifier === 100 ? "resign" : base === 2 ? "gammon" : base === 3 ? "backgammon" : "normal";

        const matchComplete = Boolean(matchLength && (
          (winnerP1 && scoreP1 + wonPts >= matchLength) ||
          (!winnerP1 && scoreP2 + wonPts >= matchLength)
        ));

        const endActionId = resultType === "resign" ? (matchComplete ? 28 : 27) : matchComplete ? 26 : 24;
        // A terminal ply names the winner: nobody *does* a game-over, and
        // ogxm_replay.cpp just carries the winner through. Resignation is the
        // one exception -- it is an act, and the player who resigns is the one
        // who lost -- so 27/28 gets the resigner, both as the ply's colour
        // (`color` is documented as the player a ply belongs to) and as the
        // player on roll (the resigner is the one facing the roll they chose
        // not to take).
        const actorIsWhite = RESIGN_ACTIONS.has(endActionId) ? !winnerIsWhite : winnerIsWhite;
        const onRollE = actorIsWhite ? "W" : "B";

        const endState = turn.curState;
        const endCubeAction = turn.cubeAction;
        const ogidBeforeEnd = _ogid(lastBoard, {
          cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
          cubeAction: endCubeAction, dice: null, onRoll: onRollE,
          gameState: endState, scoreWhite, scoreBlack,
          matchLength, crawford: isCrawford, moveId: turn.moveId,
        });
        const ogidAfterEnd = _ogid(lastBoard, {
          cubeValue: turn.cubeValue, cubeOwner: turn.cubeOwner,
          cubeAction: endCubeAction, dice: null,
          onRoll: actorIsWhite ? "B" : "W",
          gameState: endState, scoreWhite, scoreBlack,
          matchLength, crawford: isCrawford, moveId: turn.moveId,
        });
        plies.push({
          color: actorIsWhite ? 1 : 0,
          action_id: endActionId,
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
      gi++;
      continue;
    }
    idx++;
  }

  // Match-level output
  const fm = records.find(r => r[0] === "footer_match")?.[1];
  const finalP1 = fm ? fm.score1 : scoreP1;
  const finalP2 = fm ? fm.score2 : scoreP2;
  const finalWhite = flip ? finalP2 : finalP1;
  const finalBlack = flip ? finalP1 : finalP2;

  let matchResult;
  if (matchLength && finalWhite >= matchLength) matchResult = 1;
  else if (matchLength && finalBlack >= matchLength) matchResult = 2;
  else matchResult = 0;

  let maxPly = 0;
  for (const [rtype, rdata] of records) {
    if (rtype === "move" && rdata.level > maxPly && rdata.level < 100) maxPly = rdata.level;
  }
  const plyDepth = maxPly > 0 ? maxPly + 1 : 3;

  return {
    match_length: matchLength,
    player_white: playerWhite,
    player_black: playerBlack,
    white_score: finalWhite,
    black_score: finalBlack,
    result: matchResult,
    source: 2,
    timestamp,
    crawford,
    jacoby,
    beaver,
    cube_limit: cubeLimit,
    event: eventStr,
    site: siteStr,
    analysis_info: {
      ply: plyDepth,
      eval_level: `${plyDepth}ply`,
      model_id: "xg",
      timestamp,
    },
    games: gamesOut,
  };
}
