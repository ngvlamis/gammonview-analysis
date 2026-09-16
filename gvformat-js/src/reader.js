// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// JavaScript ESM port of gvformat/reader.py — pure-stdlib OGXM binary reader.

import { write_gvab, _crc32, capPointsWon, _b64encode } from './binary.js';
import {
  OGXM_MAGIC, END_MAGIC, VERSION_MAJOR, VERSION_MINOR,
  CHUNK_MHDR, CHUNK_GAME, CHUNK_ANAL, CHUNK_EVAL, CHUNK_ALTS, CHUNK_CUBE,
  CHUNK_GVAN, CHUNK_CSUM, CHUNK_FLAG_CRITICAL,
  ACTION_SET_POSITION, SET_POSITION_DICE_FLAG,
  CUBE_TYPE_MISSED_DOUBLE, CUBE_TYPE_LIVE_CHECKER,
  CUBE_TYPE_RESIGN, CUBE_TYPE_DOUBLE_DECISION, CUBE_TYPE_TAKE_PASS,
  CUBE_ACTION_DOUBLE, CUBE_ACTION_NO_DOUBLE,
  DICE_TABLE, DEFAULT_BOARD_SENTINEL,
  ACTION_NAMES,
} from './constants.js';
import { board_to_ogid } from './ogid.js';
import { completeBaseBlock } from './basefill.js';
import { splitPlace } from './place.js';
import {
  _STARTING_BOARD_P1, _TurnState, _ogid, _flipBoard as _flip_board,
  _OGID_STATE_INITIAL_BOTH, _OGID_STATE_ROLLED, _OGID_STATE_CHECKER_DONE,
  _OGID_STATE_DOUBLE_OFFERED, _OGID_STATE_AFTER_TAKE, _OGID_STATE_GAME_OVER,
  _OGID_ACTION_NONE, _OGID_ACTION_DOUBLE, _OGID_ACTION_TAKE, _OGID_ACTION_PASS,
  _OGID_CUBE_WHITE, _OGID_CUBE_BLACK,
} from './export.js';

// ---------------------------------------------------------------------------
// Entry sizes (bytes per flat record, matching Python struct.calcsize)
// ---------------------------------------------------------------------------

const _EVAL_ENTRY_SIZE = 20;
const _ALT_ENTRY_SIZE = 17;
const _CUBE_ENTRY_SIZE = 28;

// Terminal action ids (game/match end, resign, forfeit, null) — plies that
// carry no move and leave the turn state untouched.
const _TERMINAL_ACTIONS = new Set([24, 25, 26, 27, 28, 29, 30]);

// ---------------------------------------------------------------------------
// Custom error
// ---------------------------------------------------------------------------

class GvabError extends Error {
  constructor(message) {
    super(message);
    this.name = 'GvabError';
  }
}

// ---------------------------------------------------------------------------
// Small decoders (inverses of binary.js's fixed-point / bit-flag encoders)
// ---------------------------------------------------------------------------

function _q(raw) {
  return raw / 10000.0;
}

function _decodeEvalLevel(b) {
  if (b === 0) return null;
  if (b & 0x20) return 'rollout';
  if (b & 0x40) return 'database';
  if (b & 0x10) return `truncated${b & 0x0F}`;
  return `${b & 0x0F}ply`;
}

function _evalFromProbs(win, gwin, bgwin, gloss, bgloss) {
  const equity = win + gwin + bgwin - gloss - bgloss;
  return {
    win, gammon_win: gwin, bg_win: bgwin,
    gammon_loss: gloss, bg_loss: bgloss, equity: Math.round(equity * 10000) / 10000,
  };
}

// Take when the take equity is *strictly* below the pass equity; an exact tie
// is a pass, which is the engine's own rule (`cube_opponent_takes`) and the base
// spec's. The tie is not hypothetical: these equities are quantised to 1e-4 on
// the way into the binary, so any pair within 5e-5 arrives as one value. The
// spelling stays ours -- `double_take`, not the reference's `double/take` --
// because it is a display label nothing computes on, and it is what our own
// `.gva` specification documents.
function _cubeActionLabel(shouldDouble, doubleTakeEquity, doublePassEquity) {
  if (!shouldDouble) return 'no_double';
  return doubleTakeEquity < doublePassEquity ? 'double_take' : 'double_pass';
}

// ---------------------------------------------------------------------------
// Pascal string reader
// ---------------------------------------------------------------------------

const _textDecoder = new TextDecoder('utf-8', { fatal: false });

function _readPascal(data, pos) {
  const n = data[pos];
  pos += 1;
  const s = _textDecoder.decode(data.subarray(pos, pos + n));
  return [s, pos + n];
}

// ---------------------------------------------------------------------------
// DataView helper: create a view over a Uint8Array (handles byteOffset)
// ---------------------------------------------------------------------------

function _dv(data) {
  return new DataView(data.buffer, data.byteOffset, data.byteLength);
}

// Chunks this reader decodes into the OGXM object. Everything else is carried
// through verbatim so a read/write cycle does not drop it (see _unknown_chunks).
const _DECODED_CHUNKS = new Set([
  CHUNK_MHDR, CHUNK_GAME, CHUNK_ANAL, CHUNK_EVAL, CHUNK_ALTS, CHUNK_CUBE,
  CHUNK_GVAN, CHUNK_CSUM,
]);

/** The chunk's four ASCII characters, for messages; hex if not printable. */
function _chunkName(typeCode) {
  const raw = [0, 8, 16, 24].map((sh) => (typeCode >>> sh) & 0xFF);
  if (raw.every((b) => b >= 0x20 && b < 0x7F)) {
    return String.fromCharCode(...raw);
  }
  return `0x${(typeCode >>> 0).toString(16).toUpperCase().padStart(8, '0')}`;
}

// ---------------------------------------------------------------------------
// Chunk-stream walker
// ---------------------------------------------------------------------------

function _walkChunks(data) {
  const n = data.length;
  if (n < 28) {
    throw new GvabError(`file too small (${n} bytes) to be a valid .gvab stream`);
  }
  const view = _dv(data);
  let pos = 20; // skip the 20-byte file header
  const chunks = [];
  let csumStart = -1;
  while (true) {
    const remaining = n - pos;
    if (remaining === 8) break; // End Marker
    if (remaining < 8) {
      throw new GvabError(`truncated stream at offset ${pos} (${remaining} bytes left)`);
    }
    if (pos + 12 > n) {
      throw new GvabError(`truncated chunk header at offset ${pos}`);
    }
    const ctype = view.getUint32(pos, true);
    const clen = view.getUint32(pos + 4, true);
    const cflags = view.getUint16(pos + 8, true);
    if (ctype === CHUNK_CSUM) csumStart = pos;
    const bodyStart = pos + 12;
    if (bodyStart + clen > n) {
      throw new GvabError(`chunk 0x${ctype.toString(16).toUpperCase().padStart(8, '0')} at ${pos} claims ${clen} bytes, past EOF`);
    }
    chunks.push([ctype, data.subarray(bodyStart, bodyStart + clen), cflags]);
    pos = bodyStart + clen;
  }
  return [chunks, csumStart];
}

// ---------------------------------------------------------------------------
// MHDR
// ---------------------------------------------------------------------------

function _decodeMhdr(mhdr) {
  const view = _dv(mhdr);
  const matchLength = view.getUint16(0, true);
  const _numGames = view.getUint8(2);
  const flags = view.getUint8(3);
  const whiteScore = view.getUint16(4, true);
  const blackScore = view.getUint16(6, true);
  const result = view.getUint8(8);
  const source = view.getUint8(9);
  const timestamp = view.getUint32(10, true);
  const _totalPlies = view.getUint16(14, true);
  const cubeLimit = view.getUint16(16, true);

  let pos = 24;
  let playerWhite;
  [playerWhite, pos] = _readPascal(mhdr, pos);
  let playerBlack;
  [playerBlack, pos] = _readPascal(mhdr, pos);
  let event = null;
  let site = null;
  if (pos < mhdr.length) {
    let ev;
    [ev, pos] = _readPascal(mhdr, pos);
    event = ev || null;
  }
  if (pos < mhdr.length) {
    let st;
    [st, pos] = _readPascal(mhdr, pos);
    site = st || null;
  }
  if (event !== null && site === null) {
    // Written before event/site were separate fields: one combined string in
    // the `event` slot. Recover the pair so old files read the same as new
    // ones (a string with no separator is all event -- see place.js).
    ({ event, site } = splitPlace(event));
  }

  return {
    match_length: matchLength,
    player_white: playerWhite,
    player_black: playerBlack,
    white_score: whiteScore,
    black_score: blackScore,
    result,
    source,
    timestamp,
    crawford: Boolean(flags & 0x01),
    jacoby: Boolean(flags & 0x02),
    beaver: Boolean(flags & 0x04),
    raccoon: Boolean(flags & 0x08),
    cube_limit: cubeLimit,
    event,
    site,
  };
}

// ---------------------------------------------------------------------------
// GAME (header + initial board + ply records)
// ---------------------------------------------------------------------------

function _decodePly(data, pos) {
  // Bounds-check before reading. A typed array yields `undefined` past its end
  // rather than throwing, and `undefined & 0x1F` is 0 -- so without this a
  // truncated chunk would silently decode as a valid action-0 ply forever.
  if (pos >= data.length) {
    throw new GvabError(`ply record truncated: offset ${pos} past end of GAME chunk (${data.length})`);
  }
  const first = data[pos];
  const actionId = first & 0x1F;
  const color = (first >> 5) & 0x01;

  if (actionId === ACTION_SET_POSITION) {
    const hasDice = Boolean(first & SET_POSITION_DICE_FLAG);
    pos += 1;
    const ply = { color, action_id: actionId };
    if (hasDice) {
      const diceByte = data[pos];
      pos += 1;
      ply.d1 = diceByte & 0x0F;
      ply.d2 = (diceByte >> 4) & 0x0F;
    }
    const boardView = _dv(data);
    const board = [];
    for (let i = 0; i < 26; i++) {
      board.push(boardView.getInt8(pos + i));
    }
    pos += 26;
    ply.set_position = board;
    return [ply, pos];
  }

  if (actionId >= 0 && actionId <= 20) {
    const [d1, d2, nMoves] = DICE_TABLE[actionId];
    pos += 1;
    const steps = data.subarray(pos, pos + nMoves);
    pos += nMoves;
    const moves = [];
    for (let i = 0; i < steps.length; i++) {
      const b = steps[i];
      if (b !== 0) {
        moves.push({ from: b & 0x1F, pips: (b >> 5) & 0x07 });
      }
    }
    return [{ color, action_id: actionId, d1, d2, moves }, pos];
  }

  // Actions 21-30 (double/take/drop/game-end/resign/null): first byte only.
  return [{ color, action_id: actionId }, pos + 1];
}

function _decodeGame(game) {
  const view = _dv(game);
  const gameIndex = view.getUint8(0);
  const numPlies = view.getUint16(1, true);
  const winner = view.getUint8(3);
  const pointsWon = view.getUint8(4);
  const gflags = view.getUint8(5);
  const firstToMove = view.getUint8(6);
  // _reserved at offset 7

  let pos = 8;
  let initialBoard = null;
  if (game[pos] === DEFAULT_BOARD_SENTINEL) {
    pos += 2; // [0xFF][0xFF] default sentinel
  } else {
    const boardView = _dv(game);
    initialBoard = [];
    for (let i = 0; i < 26; i++) {
      initialBoard.push(boardView.getInt8(pos + i));
    }
    pos += 27; // 26 signed points + 1 pad byte
  }

  const plies = [];
  for (let i = 0; i < numPlies; i++) {
    const [ply, newPos] = _decodePly(game, pos);
    plies.push(ply);
    pos = newPos;
  }

  const gameObj = {
    game_index: gameIndex,
    winner,
    points_won: pointsWon,
    is_crawford: Boolean(gflags & 0x01),
    is_lastgame: Boolean(gflags & 0x02),
    first_to_move: firstToMove,
    plies,
  };
  if (initialBoard !== null) {
    gameObj.initial_board = initialBoard;
  }
  return [gameObj, gameIndex];
}

// ---------------------------------------------------------------------------
// Analysis chunks (ANAL / EVAL / ALTS / CUBE) + GVAN
// ---------------------------------------------------------------------------

function _decodeAnal(anal) {
  const view = _dv(anal);
  const ply = view.getUint8(0);
  // _res1 at 1
  const numChecker = view.getUint16(2, true);
  const numCube = view.getUint16(4, true);
  const timestamp = view.getUint32(6, true);
  const durationMs = view.getUint32(10, true);
  // _res2 at 14 (2 bytes), _res8 at 16 (8 bytes) = 24 total
  const [modelId] = _readPascal(anal, 24);
  return {
    ply,
    num_checker: numChecker,
    num_cube: numCube,
    timestamp,
    duration_ms: durationMs,
    model_id: modelId,
  };
}

function _decodeEvalEntries(evalData) {
  const out = [];
  const view = _dv(evalData);
  for (let off = 0; off <= evalData.length - _EVAL_ENTRY_SIZE; off += _EVAL_ENTRY_SIZE) {
    const gi = view.getUint8(off);
    const pi = view.getUint16(off + 1, true);
    const win = view.getUint16(off + 3, true);
    const gwin = view.getUint16(off + 5, true);
    const bgwin = view.getUint16(off + 7, true);
    const gloss = view.getUint16(off + 9, true);
    const bgloss = view.getUint16(off + 11, true);
    const bestEq = view.getInt16(off + 13, true);
    const eqLoss = view.getUint16(off + 15, true);
    const numAlts = view.getUint8(off + 17);
    const entryPly = view.getUint8(off + 18);
    const flags = view.getUint8(off + 19);
    out.push({
      game_index: gi,
      ply_index: pi,
      probs: [_q(win), _q(gwin), _q(bgwin), _q(gloss), _q(bgloss)],
      best_equity: _q(bestEq),
      equity_loss: _q(eqLoss),
      num_alts: numAlts,
      ply: entryPly,
      has_missed_double: Boolean(flags & 0x01),
    });
  }
  return out;
}

function _decodeAltEntries(altsData) {
  const out = [];
  const view = _dv(altsData);
  for (let off = 0; off <= altsData.length - _ALT_ENTRY_SIZE; off += _ALT_ENTRY_SIZE) {
    // move_bytes: 4 bytes at offset 0
    const moveBytes = altsData.subarray(off, off + 4);
    const eq = view.getInt16(off + 4, true);
    const win = view.getUint16(off + 6, true);
    const gwin = view.getUint16(off + 8, true);
    const bgwin = view.getUint16(off + 10, true);
    const gloss = view.getUint16(off + 12, true);
    const bgloss = view.getUint16(off + 14, true);
    const flags = view.getUint8(off + 16);
    const move = [];
    for (let i = 0; i < moveBytes.length; i++) {
      const b = moveBytes[i];
      if (b !== 0) {
        move.push({ from: b & 0x1F, pips: (b >> 5) & 0x07 });
      }
    }
    out.push({
      move,
      equity: _q(eq),
      probs: [_q(win), _q(gwin), _q(bgwin), _q(gloss), _q(bgloss)],
      is_played: Boolean(flags & 0x01),
    });
  }
  return out;
}

function _decodeCubeEntries(cubeData) {
  const out = [];
  const view = _dv(cubeData);
  for (let off = 0; off <= cubeData.length - _CUBE_ENTRY_SIZE; off += _CUBE_ENTRY_SIZE) {
    const gi = view.getUint8(off);
    const pi = view.getUint16(off + 1, true);
    const ctype = view.getUint8(off + 3);
    const nd = view.getInt16(off + 4, true);
    const dt = view.getInt16(off + 6, true);
    const dp = view.getInt16(off + 8, true);
    const win = view.getUint16(off + 10, true);
    const gwin = view.getUint16(off + 12, true);
    const bgwin = view.getUint16(off + 14, true);
    const gloss = view.getUint16(off + 16, true);
    const bgloss = view.getUint16(off + 18, true);
    const eqLoss = view.getUint16(off + 20, true);
    const corr = view.getUint8(off + 22);
    const played = view.getUint8(off + 23);
    const entryPly = view.getUint8(off + 24);
    // 3 reserved bytes at 25-27
    out.push({
      game_index: gi,
      ply_index: pi,
      type: ctype,
      no_double_equity: _q(nd),
      double_take_equity: _q(dt),
      double_pass_equity: _q(dp),
      probs: [_q(win), _q(gwin), _q(bgwin), _q(gloss), _q(bgloss)],
      equity_loss: _q(eqLoss),
      correct_action: corr,
      played_action: played,
      ply: entryPly,
    });
  }
  return out;
}

/**
 * Parse a GVAN chunk (v3, or v2 for files written before the shrink).
 *
 * v3 dropped the two blanked uint16 anchor slots from each checker and cube
 * record (7->3 and 6->2 bytes). They held per-decision mwc_on_win/mwc_on_loss
 * in v1; v2 zeroed them when MWC became compute-on-read but kept the width.
 * Record widths are the only difference, so both decode to the same shape.
 *
 * v1 is not supported (its slots hold live anchors, not zeros). An unknown
 * version is rejected rather than guessed at: the sections are positional and
 * fixed-width, so misreading the width silently yields plausible garbage.
 */
function _decodeGvan(gvan, numChecker, numAlts, numCube) {
  const view = _dv(gvan);
  const version = view.getUint8(0);
  const baseLevel = view.getUint8(1);
  const sectionFlags = view.getUint8(2);
  const luckLevel = view.getUint8(3);
  let chkW, cubeW;
  if (version === 3) { chkW = 3; cubeW = 2; }
  else if (version === 2) { chkW = 7; cubeW = 6; }
  else {
    throw new GvabError(
      `unsupported GVAN version ${version} (this reader handles 2 and 3)`);
  }
  let pos = 4;

  const checker = [];
  if (sectionFlags & 0x01) {
    for (let i = 0; i < numChecker; i++) {
      const flags = view.getUint8(pos);
      const luck = view.getInt16(pos + 1, true);
      pos += chkW;
      checker.push({
        decision: Boolean(flags & 0x01),
        has_luck: Boolean(flags & 0x02),
        illegal_move: Boolean(flags & 0x04),
        luck: _q(luck),
      });
    }
  }

  let altLevels = [];
  if (sectionFlags & 0x02) {
    altLevels = Array.from(gvan.subarray(pos, pos + numAlts));
    pos += numAlts;
  }

  const cube = [];
  if (sectionFlags & 0x04) {
    for (let i = 0; i < numCube; i++) {
      const flags = view.getUint8(pos);
      const evlvl = view.getUint8(pos + 1);
      pos += cubeW;
      cube.push({
        decision: Boolean(flags & 0x01),
        eval_level: evlvl,
      });
    }
  }

  return {
    version,
    base_eval_level: baseLevel,
    luck_eval_level: luckLevel,
    checker,
    alt_levels: altLevels,
    cube,
  };
}

// ---------------------------------------------------------------------------
// Analysis reassembly
// ---------------------------------------------------------------------------

function _buildAlt(alt, levelByte) {
  const out = { move: alt.move, equity: alt.equity, is_played: alt.is_played };
  if (alt.probs.some(v => v !== 0)) {
    out.eval = _evalFromProbs(...alt.probs);
  }
  const lvl = _decodeEvalLevel(levelByte);
  if (lvl !== null) out.eval_level = lvl;
  return out;
}

function _buildCheckerAnalysis(ev, gv, alts, altLevels) {
  const bestEquity = ev.best_equity;
  const equityLoss = ev.equity_loss;
  const analysis = {
    // EVAL's five probabilities are the *best move's* resulting position, which
    // is also the first alternative's -- so a producer that fills one and not
    // the other has still said it. All-zero means "not recorded" rather than
    // "0% to win" here exactly as it does on a cube record, and the fallback
    // below costs nothing when the field is populated.
    eval: _evalFromProbs(...ev.probs),
    best_equity: bestEquity,
    played_equity: Math.round((bestEquity - equityLoss) * 10000) / 10000,
    equity_loss: equityLoss,
    decision: gv ? Boolean(gv.decision) : false,
    alternatives: alts.map((a, i) => _buildAlt(a, altLevels[i] !== undefined ? altLevels[i] : 0)),
  };
  if (!ev.probs.some(v => v !== 0)) {
    const best = analysis.alternatives[0];
    if (best && best.eval) analysis.eval = { ...best.eval };
  }
  if (ev.ply) analysis.ply = ev.ply;
  if (gv && gv.has_luck) {
    analysis.luck = gv.luck;
  }
  if (gv && gv.illegal_move) analysis.illegal_move = true;
  return analysis;
}

function _buildMissedDouble(c, gv) {
  const sub = {
    no_double_equity: c.no_double_equity,
    double_take_equity: c.double_take_equity,
    double_pass_equity: c.double_pass_equity,
    equity_loss: c.equity_loss,
    correct_action: ACTION_NAMES[c.correct_action] || 'double',
  };
  // Pre-roll probabilities for the cube decision, as on any other cube record.
  // Absent in files written before writers stored them, where all-zero probs
  // mean "not recorded" rather than "0% to win".
  if (c.probs.some(v => v !== 0)) {
    sub.eval = _evalFromProbs(...c.probs);
  }
  const lvl = _decodeEvalLevel(gv.eval_level);
  if (lvl !== null) sub.eval_level = lvl;
  return sub;
}

/**
 * The live double/take/pass a checker ply posed.
 *
 * `derived` builds the same block from a *missed double's* record rather than
 * from a `live_checker` one. The base spec has a reader emit both from a type=2
 * entry -- one cube entry per checker ply, so the error record has to stand in
 * for the decision as well -- because a consumer rendering a cube panel reads
 * only `cube_decision`.
 *
 * A derived block carries **no `decision`**, and that is the whole of what
 * keeps this safe. `CubeDecision` is a display projection: the base spec gives
 * it no `equity_loss` and no `classification` precisely because "the decision is
 * not itself an error, and any error made against it is scored on
 * `missed_double`". Our `decision` flag is accounting, so it belongs with the
 * error too -- put it on both and `stats.js` counts one cube twice, since it
 * accumulates a `cube_decision` on its own flag *and* the `missed_double`
 * beside it.
 */
function _buildCubeDecision(c, gv, derived = false) {
  const shouldDouble = c.correct_action === CUBE_ACTION_DOUBLE;
  const sub = {
    should_double: shouldDouble,
    no_double_equity: c.no_double_equity,
    double_take_equity: c.double_take_equity,
    double_pass_equity: c.double_pass_equity,
    action: _cubeActionLabel(shouldDouble, c.double_take_equity, c.double_pass_equity),
  };
  if (!derived) sub.decision = Boolean(gv.decision);
  if (c.probs.some(v => v !== 0)) {
    sub.eval = _evalFromProbs(...c.probs);
  }
  const lvl = _decodeEvalLevel(gv.eval_level);
  if (lvl !== null) sub.eval_level = lvl;
  return sub;
}

function _buildStandaloneCube(c, gv) {
  let analysis;
  if (c.type === CUBE_TYPE_RESIGN) {
    analysis = {
      resign_error: c.no_double_equity,
      take_resign_error: c.double_take_equity,
      equity_loss: c.equity_loss,
      decision: Boolean(gv.decision),
    };
  } else {
    analysis = {
      correct_action: ACTION_NAMES[c.correct_action] || 'no_double',
      played_action: ACTION_NAMES[c.played_action] || 'no_double',
      no_double_equity: c.no_double_equity,
      double_take_equity: c.double_take_equity,
      double_pass_equity: c.double_pass_equity,
      equity_loss: c.equity_loss,
      decision: Boolean(gv.decision),
    };
  }
  if (c.probs.some(v => v !== 0)) {
    analysis.eval = _evalFromProbs(...c.probs);
  }
  const lvl = _decodeEvalLevel(gv.eval_level);
  if (lvl !== null) analysis.eval_level = lvl;
  if (c.ply) analysis.ply = c.ply;
  return analysis;
}

// ---------------------------------------------------------------------------
// OGID derivation
// ---------------------------------------------------------------------------

function _absoluteToP1(boardAbs) {
  const p1 = new Array(26).fill(0);
  p1[0] = -boardAbs[25];
  p1[25] = boardAbs[0];
  for (let i = 1; i < 25; i++) {
    p1[i] = boardAbs[25 - i];
  }
  return p1;
}

function _applyMovesP1(boardP1, moves, moverIsWhite) {
  const mb = moverIsWhite
    ? boardP1.slice()
    : _flip_board(boardP1);
  for (const m of moves) {
    const fromAbs = Number(m.from);
    const pips = Number(m.pips);
    const src = moverIsWhite ? (25 - fromAbs) : fromAbs;
    // Bounds-check before indexing. An out-of-range `from` (only reachable from
    // a corrupt or hand-edited stream) would otherwise write a stray property
    // onto the array and leave the board quietly wrong, with no error at all.
    if (!(src >= 0 && src <= 25)) {
      throw new GvabError(`move from point ${fromAbs} is outside the board`);
    }
    mb[src] -= 1;
    const dest = src - pips;
    if (dest >= 1) {
      if (mb[dest] < 0) {
        mb[dest] = 1;
        mb[0] += 1;
      } else {
        mb[dest] += 1;
      }
    }
  }
  return moverIsWhite ? mb : _flip_board(mb);
}

/**
 * Per-game [whiteStart, blackStart] match scores, accumulating each game's
 * points_won to its winner.
 *
 * Each game's contribution is capped at what its winner still needed: the
 * stored points_won is the game's full value (a 4-point gammon is 4 even when
 * it only had to bank 1), so summing it raw runs the score past matchLength.
 */
function _gameStartScores(games, matchLength = 0) {
  const out = [];
  let w = 0, b = 0;
  for (const g of games) {
    out.push([w, b]);
    const winner = g.winner;
    if (winner === 0) w += capPointsWon(g.points_won, w, matchLength);
    else if (winner === 1) b += capPointsWon(g.points_won, b, matchLength);
  }
  return out;
}

function _deriveOgids(ogxm) {
  const matchLength = Number(ogxm.match_length || 0);
  const games = ogxm.games || [];
  const startScores = _gameStartScores(games, matchLength);

  for (let gi = 0; gi < games.length; gi++) {
    const g = games[gi];
    const [sw, sb] = startScores[gi];
    const crawford = Boolean(g.is_crawford);
    const turn = new _TurnState();
    let board = _STARTING_BOARD_P1.slice();

    function ogid(brd, { on_roll, game_state, cube_action, dice }) {
      return board_to_ogid(brd, {
        moverIsWhite: true,
        cubeValue: turn.cubeValue,
        cubeOwner: turn.cubeOwner,
        cubeAction: cube_action,
        dice,
        onRoll: on_roll,
        gameState: game_state,
        scoreWhite: sw,
        scoreBlack: sb,
        matchLength,
        crawford,
        moveId: turn.moveId,
      });
    }

    for (const ply of (g.plies || [])) {
      const aid = ply.action_id;
      const colorPly = ply.color;
      const color = colorPly === 1 || colorPly === true ? 1 : 0;
      const onRoll = color === 1 ? 'W' : 'B';
      const opp = color === 1 ? 'B' : 'W';

      if (aid !== null && aid !== undefined && aid >= 0 && aid <= 20) {
        const d1 = ply.d1;
        const d2 = ply.d2;
        const beforeState = turn.isFirstPly ? _OGID_STATE_INITIAL_BOTH : _OGID_STATE_ROLLED;
        ply.ogid_before = ogid(board, {
          on_roll: onRoll,
          game_state: beforeState,
          cube_action: turn.cubeAction,
          dice: [d1, d2],
        });
        turn.moveId += 1;
        turn.isFirstPly = false;
        turn.curState = _OGID_STATE_CHECKER_DONE;
        turn.cubeAction = _OGID_ACTION_NONE;
        board = _applyMovesP1(board, ply.moves || [], color === 1);
        ply.ogid_after = ogid(board, {
          on_roll: opp,
          game_state: _OGID_STATE_CHECKER_DONE,
          cube_action: _OGID_ACTION_NONE,
        });
      } else if (aid === 21) {
        ply.ogid_before = ogid(board, {
          on_roll: onRoll,
          game_state: turn.curState,
          cube_action: turn.cubeAction,
        });
        turn.awaitingResponse = true;
        turn.curState = _OGID_STATE_DOUBLE_OFFERED;
        turn.cubeAction = _OGID_ACTION_DOUBLE;
        ply.ogid_after = ogid(board, {
          on_roll: opp,
          game_state: _OGID_STATE_DOUBLE_OFFERED,
          cube_action: _OGID_ACTION_DOUBLE,
        });
      } else if (aid === 22 || aid === 23) {
        ply.ogid_before = ogid(board, {
          on_roll: onRoll,
          game_state: _OGID_STATE_DOUBLE_OFFERED,
          cube_action: _OGID_ACTION_DOUBLE,
        });
        turn.awaitingResponse = false;
        if (aid === 22) {
          turn.cubeLog2 += 1;
          turn.cubeOwner = color === 1 ? _OGID_CUBE_WHITE : _OGID_CUBE_BLACK;
          turn.curState = _OGID_STATE_AFTER_TAKE;
          turn.cubeAction = _OGID_ACTION_TAKE;
        } else {
          turn.curState = _OGID_STATE_GAME_OVER;
          turn.cubeAction = _OGID_ACTION_PASS;
        }
        ply.ogid_after = ogid(board, {
          on_roll: opp,
          game_state: turn.curState,
          cube_action: turn.cubeAction,
        });
      } else if (aid === ACTION_SET_POSITION) {
        board = _absoluteToP1(ply.set_position || new Array(26).fill(0));
      } else if (_TERMINAL_ACTIONS.has(aid)) {
        ply.ogid_before = ogid(board, {
          on_roll: onRoll,
          game_state: turn.curState,
          cube_action: turn.cubeAction,
        });
        ply.ogid_after = ogid(board, {
          on_roll: opp,
          game_state: turn.curState,
          cube_action: turn.cubeAction,
        });
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Parse `.gvab` binary bytes into an OGXM-JSON object.
 *
 * Every failure on malformed input arrives as a `GvabError`. That is a
 * guarantee, not a best effort: callers hand this untrusted files, so a stray
 * `RangeError`/`TypeError` from a decoder's internals must never escape.
 */
function readGvab(data, options) {
  try {
    return _readGvab(data, options);
  } catch (e) {
    if (e instanceof GvabError) throw e;
    throw new GvabError(`malformed .gvab (${e.name}: ${e.message})`);
  }
}

/** `readGvab`'s body; split out so the entry point enforces the GvabError
 *  contract in one place. */
function _readGvab(data, options) {
  const { verifyCrc = true, deriveOgids = true } = options || {};

  if (data.length < 20) {
    throw new GvabError('input shorter than the 20-byte file header');
  }
  const view = _dv(data);
  const magic = view.getUint32(0, true);
  const _vmaj = view.getUint16(4, true);
  const _vmin = view.getUint16(6, true);
  const rmaj = view.getUint16(8, true);
  const rmin = view.getUint16(10, true);
  const fileSize = view.getUint32(12, true);
  const _hflags = view.getUint32(16, true);

  if (magic !== OGXM_MAGIC) {
    throw new GvabError(`bad magic 0x${magic.toString(16).toUpperCase().padStart(8, '0')} (expected 0x${OGXM_MAGIC.toString(16).toUpperCase().padStart(8, '0')})`);
  }
  // min_reader_* is the file's own statement of the spec version it needs.
  // Honouring it is the point of the field: a file using a later layout must
  // fail as a clean version mismatch, not be parsed optimistically into
  // silent garbage. Checked before file_size, as the reference codec does.
  if (rmaj > VERSION_MAJOR || (rmaj === VERSION_MAJOR && rmin > VERSION_MINOR)) {
    throw new GvabError(`file requires an OGXM ${rmaj}.${rmin} reader; this one implements ${VERSION_MAJOR}.${VERSION_MINOR}`);
  }
  if (fileSize && fileSize !== data.length) {
    throw new GvabError(`file_size header (${fileSize}) != actual length (${data.length})`);
  }

  const endMagic = view.getUint32(data.length - 8, true);
  if (endMagic !== END_MAGIC) {
    throw new GvabError(`bad end marker 0x${endMagic.toString(16).toUpperCase().padStart(8, '0')} (expected 0x${END_MAGIC.toString(16).toUpperCase().padStart(8, '0')})`);
  }

  const [chunks, csumStart] = _walkChunks(data);

  if (verifyCrc && csumStart >= 0) {
    const csumView = _dv(data);
    const want = csumView.getUint32(csumStart + 16, true);
    const got = _crc32(data.subarray(0, csumStart));
    if (want !== got) {
      throw new GvabError(`CSUM mismatch: stored 0x${want.toString(16).toUpperCase().padStart(8, '0')}, computed 0x${got.toString(16).toUpperCase().padStart(8, '0')}`);
    }
  }

  function first(ctype) {
    for (const [t, body] of chunks) {
      if (t === ctype) return body;
    }
    return null;
  }

  const mhdr = first(CHUNK_MHDR);
  if (mhdr === null) throw new GvabError('no MHDR chunk');
  const ogxm = _decodeMhdr(mhdr);

  // Games in stream order; also index each ply by (game_index, ply_index)
  const games = [];
  const plyByKey = new Map();
  for (const [ctype, body] of chunks) {
    if (ctype !== CHUNK_GAME) continue;
    const [gameObj, gi] = _decodeGame(body);
    for (let pi = 0; pi < gameObj.plies.length; pi++) {
      plyByKey.set(`${gi},${pi}`, gameObj.plies[pi]);
    }
    games.push(gameObj);
  }

  // Games, and the positions they replay to, are settled before any analysis is
  // decoded. `basefill.js` needs a ply's own score and cube to read a foreign
  // block's cube values, and derivation depends on nothing an analysis holds --
  // so the order costs nothing and is what lets that completion happen while
  // each block is still a map of its own, before the primary is aliased onto
  // `ply.analysis`.
  ogxm.games = games;
  if (deriveOgids) {
    _deriveOgids(ogxm);
  }

  // Group the analysis chunks by ANAL: each EVAL/ALTS/CUBE/GVAN binds to the
  // most recent ANAL (base spec 1.3 -- zero or more analysis blocks, primary
  // first). One block is the legacy single-analysis case; more than one is
  // multi-analysis (analyses_info + per-ply analyses[]).
  //
  // The same pass captures every chunk we do not decode (SIGN, CLCK, VIDO,
  // anything a later spec version adds) so write_gvab can re-emit it. Each is
  // tagged with the analysis block it followed, which is what SIGN binds to.
  const analGroups = [];
  const unknown = [];
  {
    const EMPTY = new Uint8Array(0);
    let cur = null;
    for (const [t, body, cflags] of chunks) {
      if (t === CHUNK_ANAL) {
        cur = { anal: body, eval: EMPTY, alts: EMPTY, cube: EMPTY, gvan: null };
        analGroups.push(cur);
      } else if (cur !== null) {
        if (t === CHUNK_EVAL) cur.eval = body;
        else if (t === CHUNK_ALTS) cur.alts = body;
        else if (t === CHUNK_CUBE) cur.cube = body;
        else if (t === CHUNK_GVAN) cur.gvan = body;
      }
      if (_DECODED_CHUNKS.has(t)) continue;
      if (cflags & CHUNK_FLAG_CRITICAL) {
        // A critical chunk we cannot interpret means the file says more than
        // we can read. Carrying it through would be a lie; the base spec's
        // rule is to reject.
        throw new GvabError(
          `unknown critical chunk ${_chunkName(t)} -- this reader cannot `
          + 'safely read or rewrite the file');
      }
      unknown.push({
        type: t,
        name: _chunkName(t),
        flags: cflags,
        anal_index: analGroups.length - 1,
        data: _b64encode(body),
      });
    }
  }

  // Decode each block to [analysisInfo, Map<"gameIndex,plyIndex", analysis>].
  // Keying by (game_index, ply_index) rather than writing straight onto the ply
  // is what lets several blocks describe the same ply without colliding.
  const blocks = [];
  //: Which of them arrived without our extensions, for a caller that wants to
  //: say so -- a foreign block is missing its luck until an engine supplies it.
  //: Not written back: once a block has been completed and re-encoded it has a
  //: GVAN of its own and is no longer foreign to the next read.
  const baseBlocks = [];
  for (const grp of analGroups) {
    const info = _decodeAnal(grp.anal);

    const evalEntries = _decodeEvalEntries(grp.eval);
    const altEntries = _decodeAltEntries(grp.alts);
    const cubeEntries = _decodeCubeEntries(grp.cube);
    let totalAlts = 0;
    for (const e of evalEntries) totalAlts += e.num_alts;

    const gvan = grp.gvan !== null
      ? _decodeGvan(grp.gvan, evalEntries.length, totalAlts, cubeEntries.length)
      : null;
    const gvChecker = gvan ? gvan.checker : [];
    const gvAltLevels = gvan ? gvan.alt_levels : [];
    const gvCube = gvan ? gvan.cube : [];

    const blockObj = new Map();

    // EVAL + ALTS + GVAN-checker -> per-checker-ply analysis
    let altCursor = 0;
    for (let i = 0; i < evalEntries.length; i++) {
      const ev = evalEntries[i];
      const n = ev.num_alts;
      const alts = altEntries.slice(altCursor, altCursor + n);
      const levels = gvAltLevels.length
        ? gvAltLevels.slice(altCursor, altCursor + n)
        : new Array(n).fill(0);
      altCursor += n;
      const gv = i < gvChecker.length ? gvChecker[i] : null;
      blockObj.set(`${ev.game_index},${ev.ply_index}`,
        _buildCheckerAnalysis(ev, gv, alts, levels));
    }

    // CUBE + GVAN-cube -> standalone cube-ply analyses, plus missed_double
    // / cube_decision sub-objects on their checker ply
    for (let i = 0; i < cubeEntries.length; i++) {
      const c = cubeEntries[i];
      const gv = i < gvCube.length ? gvCube[i] : { decision: false, eval_level: 0 };
      const key = `${c.game_index},${c.ply_index}`;
      if (c.type === CUBE_TYPE_MISSED_DOUBLE) {
        if (!blockObj.has(key)) blockObj.set(key, {});
        const obj = blockObj.get(key);
        obj.missed_double = _buildMissedDouble(c, gv);
        // And the decision the error was made against, as the spec has every
        // cube-live checker ply carry -- a missed-double ply included. Derived,
        // so it holds no `decision` of its own; the accounting stays above.
        obj.cube_decision = _buildCubeDecision(c, gv, true);
      } else if (c.type === CUBE_TYPE_LIVE_CHECKER) {
        if (!blockObj.has(key)) blockObj.set(key, {});
        blockObj.get(key).cube_decision = _buildCubeDecision(c, gv);
      } else {
        blockObj.set(key, _buildStandaloneCube(c, gv));
      }
    }

    const analysisInfo = { ply: info.ply };
    if (gvan !== null) {
      const baseLvl = _decodeEvalLevel(gvan.base_eval_level);
      if (baseLvl !== null) analysisInfo.eval_level = baseLvl;
      const luckLvl = _decodeEvalLevel(gvan.luck_eval_level);
      if (luckLvl !== null) analysisInfo.luck_eval_level = luckLvl;
    }
    analysisInfo.model_id = info.model_id;
    analysisInfo.timestamp = info.timestamp;
    if (info.duration_ms) analysisInfo.duration_ms = info.duration_ms;

    // No GVAN means no writer of ours: the block is the base format alone, and
    // the fields only our extension carries have to be derived from what is
    // there rather than read as absent. See `basefill.js` -- and note it needs
    // the OGIDs, which is why they are derived above and not at the end.
    if (gvan === null && blockObj.size) {
      if (deriveOgids) completeBaseBlock(blockObj, plyByKey, analysisInfo);
      baseBlocks.push(blocks.length);
    }
    blocks.push([analysisInfo, blockObj]);
  }

  if (blocks.length === 1) {
    const [info, blockObj] = blocks[0];
    for (const [key, obj] of blockObj) {
      const ply = plyByKey.get(key);
      if (ply !== undefined) ply.analysis = obj;
    }
    ogxm.analysis_info = info;
  } else if (blocks.length > 1) {
    // Multi-analysis: analyses_info lists every block; each analyzed ply gets
    // one analyses[] entry per block (tagged analysis_index). The primary
    // (index 0) is also mirrored as analysis_info + per-ply analysis so naive
    // single-analysis readers keep working.
    ogxm.analyses_info = blocks.map(([info]) => info);
    ogxm.analysis_info = blocks[0][0];
    for (let k = 0; k < blocks.length; k++) {
      const blockObj = blocks[k][1];
      for (const [key, obj] of blockObj) {
        const ply = plyByKey.get(key);
        if (ply === undefined) continue;
        if (ply.analyses === undefined) ply.analyses = [];
        ply.analyses.push({ ...obj, analysis_index: k });
        if (k === 0) ply.analysis = obj;
      }
    }
  }

  if (unknown.length) ogxm._unknown_chunks = unknown;
  if (baseBlocks.length) ogxm._base_analyses = baseBlocks;

  return ogxm;
}

function canonicalize(ogxm) {
  return readGvab(write_gvab(ogxm));
}

export {
  readGvab,
  canonicalize,
  GvabError,
  // Internal, exported for tests (same convention as binary.js's _encode_*).
  // Python's twin is likewise reached directly by gvanalysis.ogxm_reconstructor.
  _applyMovesP1,
  _absoluteToP1,
};
