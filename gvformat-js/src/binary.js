// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis
//
// Portions of this file are ported from HedgeHog's C++ codec
// (MIT, Copyright (c) 2026 Eran Lambooij). See THIRD-PARTY-NOTICES.md,
// whose notices must be preserved in copies of this file.

// JavaScript ESM port of gvformat/binary.py — pure-stdlib OGXM binary writer.

import {
  OGXM_MAGIC, VERSION_MAJOR, VERSION_MINOR,
  CHUNK_MHDR, CHUNK_GAME, CHUNK_ANAL, CHUNK_EVAL, CHUNK_ALTS,
  CHUNK_CUBE, CHUNK_GVAN, CHUNK_CSUM, CHUNK_SIGN, END_MAGIC,
  CHUNK_FLAG_CRITICAL, HEADER_FLAG_HAS_ANALYSIS,
  MAX_ALTS_PER_DECISION, MAX_PLAYER_NAME, DEFAULT_BOARD_SENTINEL, GVAN_VERSION,
  WINNER_INCOMPLETE,
  ACTION_DOUBLE, ACTION_TAKE, ACTION_DROP,
  ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH, ACTION_SET_POSITION,
  SET_POSITION_DICE_FLAG,
  CUBE_TYPE_DOUBLE_DECISION, CUBE_TYPE_TAKE_PASS,
  CUBE_TYPE_MISSED_DOUBLE, CUBE_TYPE_RESIGN, CUBE_TYPE_LIVE_CHECKER,
  CUBE_ACTION_NO_DOUBLE, CUBE_ACTION_DOUBLE, CUBE_ACTION_TAKE, CUBE_ACTION_PASS,
  CUBE_ACTION_CODES,
  DICE_TABLE, MAX_ANALYSES,
} from './constants.js';

// ---------------------------------------------------------------------------
// CRC-32 (ISO-HDLC / "zip" CRC-32: reflected, poly 0xEDB88320,
// init/final XOR 0xFFFFFFFF) — mirrors zlib.crc32()
// ---------------------------------------------------------------------------

const _CRC32_TABLE = new Uint32Array(256);
for (let i = 0; i < 256; i++) {
  let crc = i;
  for (let j = 0; j < 8; j++) {
    crc = (crc & 1) ? ((crc >>> 1) ^ 0xEDB88320) : (crc >>> 1);
  }
  _CRC32_TABLE[i] = crc >>> 0;
}

function _crc32(buf) {
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) {
    crc = _CRC32_TABLE[(crc ^ buf[i]) & 0xFF] ^ (crc >>> 8);
  }
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

// ---------------------------------------------------------------------------
// Byte-builder helpers
// ---------------------------------------------------------------------------

function _concat(...parts) {
  let totalLen = 0;
  for (const p of parts) totalLen += p.length;
  const out = new Uint8Array(totalLen);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

function _u8(v) {
  return new Uint8Array([v & 0xFF]);
}

function _u16(v) {
  const buf = new Uint8Array(2);
  new DataView(buf.buffer).setUint16(0, v & 0xFFFF, true);
  return buf;
}

function _i16(v) {
  const buf = new Uint8Array(2);
  new DataView(buf.buffer).setInt16(0, v, true);
  return buf;
}

function _u32(v) {
  const buf = new Uint8Array(4);
  new DataView(buf.buffer).setUint32(0, v >>> 0, true);
  return buf;
}

function _packSignedBytes(values) {
  const buf = new Uint8Array(values.length);
  const dv = new DataView(buf.buffer);
  for (let i = 0; i < values.length; i++) {
    dv.setInt8(i, values[i]);
  }
  return buf;
}

// ---------------------------------------------------------------------------
// Fixed-point encoding (mirrors ogxm_format.hpp's encode_probability /
// encode_equity / encode_equity_loss — including round-half-away-from-zero,
// matching C's roundf() rather than JS's Math.round() which is banker's
// rounding).
// ---------------------------------------------------------------------------

function _round_c(x) {
  return x >= 0 ? Math.floor(x + 0.5) : Math.ceil(x - 0.5);
}

function _enc_prob(p) {
  p = p < 0.0 ? 0.0 : (p > 1.0 ? 1.0 : p);
  return _round_c(p * 10000.0);
}

function _enc_equity(eq) {
  eq = eq < -3.0 ? -3.0 : (eq > 3.0 ? 3.0 : eq);
  return _round_c(eq * 10000.0);
}

// uint16 at 1e-4, so 6.5535 is the largest representable loss. Was 1.0 until
// 2026-08, which silently truncated big blunders -- and since played_equity is
// derived (best_equity - equity_loss), it moved that too. Widening the writer
// needs no version bump; readers decode with a plain divide and never range-check.
const MAX_EQUITY_LOSS = 6.5535;

function _enc_equity_loss(loss) {
  loss = loss < 0.0 ? 0.0 : (loss > MAX_EQUITY_LOSS ? MAX_EQUITY_LOSS : loss);
  return _round_c(loss * 10000.0);
}

// ---------------------------------------------------------------------------
// Pack helpers
// ---------------------------------------------------------------------------

function _pack_move_step(start, pips) {
  return (start & 0x1F) | ((pips & 0x07) << 5);
}

function _pack_string(s) {
  const encoded = new TextEncoder().encode(s || '');
  const b = encoded.slice(0, MAX_PLAYER_NAME);
  const out = new Uint8Array(1 + b.length);
  out[0] = b.length;
  out.set(b, 1);
  return out;
}

/**
 * Clamp a game's points_won to what the winner could actually bank.
 *
 * A match ends the instant somebody reaches the target, so a 4-point gammon
 * won at 6-1 of a 7-point match banks 1, not 4, and the match ends 7-1. The
 * stored field keeps the game's full value (the win type is read back out of
 * it); every consumer that sums it into a running score caps it here first.
 *
 * Money play (matchLength 0/absent) has no target and is never capped.
 */
export function capPointsWon(points, winnerScoreBefore, matchLength) {
  points = Math.max(0, Number(points || 0));
  matchLength = Number(matchLength || 0);
  if (matchLength <= 0) return points;
  return Math.min(points, Math.max(0, matchLength - Number(winnerScoreBefore || 0)));
}

/** Clamp one side's recorded total to the match length. Companion to capPointsWon. */
export function clampMatchScore(score, matchLength) {
  score = Number(score || 0);
  matchLength = Number(matchLength || 0);
  return (matchLength > 0 && score > matchLength) ? matchLength : score;
}

// Plain base64 over raw bytes (the JSON-safe carrier for a passthrough chunk's
// body), without Buffer -- btoa/atob exist in browsers and in Node >= 18.
// Chunked so a large body doesn't blow String.fromCharCode's argument limit.
export function _b64encode(bytes) {
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

export function _b64decode(str) {
  const binary = atob(str);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Re-emit chunks carried verbatim from the input (see _unknown_chunks). */
function _encode_passthrough(chunks) {
  const parts = [];
  for (const c of chunks || []) {
    const body = typeof c.data === 'string' ? _b64decode(c.data) : (c.data || new Uint8Array(0));
    parts.push(_concat(
      _u32(Number(c.type) >>> 0),
      _u32(body.length),
      _u16(Number(c.flags || 0) & 0xFFFF),
      _u16(0),
      body,
    ));
  }
  return parts.length ? _concat(...parts) : new Uint8Array(0);
}

/**
 * Partition _unknown_chunks into [perBlock, trailing].
 *
 * A chunk records the index of the analysis block it followed in the source
 * file. SIGN binds to its ANAL and must stay inside that block; everything
 * else (CLCK, VIDO, any future ancillary chunk) trails all blocks, which is
 * also where a chunk from a block we no longer emit is parked.
 */
function _split_passthrough(ogxm, numBlocks) {
  const perBlock = Array.from({ length: numBlocks }, () => []);
  const trailing = [];
  for (const c of ogxm._unknown_chunks || []) {
    const idx = c.anal_index;
    if ((Number(c.type) >>> 0) === CHUNK_SIGN
        && Number.isInteger(idx) && idx >= 0 && idx < numBlocks) {
      perBlock[idx].push(c);
    } else {
      trailing.push(c);
    }
  }
  return [perBlock, trailing];
}

function _chunk(type_code, data, critical) {
  const flags = critical ? CHUNK_FLAG_CRITICAL : 0;
  return _concat(
    _u32(type_code),
    _u32(data.length),
    _u16(flags),
    _u16(0),
    data,
  );
}

// ---------------------------------------------------------------------------
// Eval helpers
// ---------------------------------------------------------------------------

function _probs_from_eval(ev) {
  if (!ev || typeof ev !== 'object') {
    return [0.0, 0.0, 0.0, 0.0, 0.0];
  }
  return [
    Number(ev.win || 0.0) || 0.0,
    Number(ev.gammon_win || 0.0) || 0.0,
    Number(ev.bg_win || 0.0) || 0.0,
    Number(ev.gammon_loss || 0.0) || 0.0,
    Number(ev.bg_loss || 0.0) || 0.0,
  ];
}

function _cube_action_code(s) {
  return CUBE_ACTION_CODES[s] !== undefined ? CUBE_ACTION_CODES[s] : CUBE_ACTION_NO_DOUBLE;
}

// ---------------------------------------------------------------------------
// Ply encoding (mirrors ogxm_io.cpp's encode_ply)
// ---------------------------------------------------------------------------

function _encode_ply(ply, action_id) {
  const color = ply.color ? 1 : 0;
  let first_byte = (action_id & 0x1F) | (color << 5);

  if (action_id === ACTION_SET_POSITION) {
    let board = ply.set_position || new Array(26).fill(0);
    board = board.map(v => Number(v));
    if (board.length !== 26) {
      board = board.concat(new Array(26).fill(0)).slice(0, 26);
    }
    const d1 = ply.d1;
    const d2 = ply.d2;
    const has_dice = (
      Number.isInteger(d1) && Number.isInteger(d2)
      && d1 >= 1 && d1 <= 6 && d2 >= 1 && d2 <= 6
    );
    const board_bytes = _packSignedBytes(board);
    if (has_dice) {
      return _concat(
        _u8(first_byte | SET_POSITION_DICE_FLAG),
        _u8((d1 & 0x0F) | ((d2 & 0x0F) << 4)),
        board_bytes,
      );
    }
    return _concat(_u8(first_byte), board_bytes);
  }

  if (action_id < 0 || action_id > 20) {
    return _u8(first_byte);
  }

  const n_moves = DICE_TABLE[action_id][2];
  const moves = ply.moves || [];
  if (moves.length > n_moves) {
    // The record holds exactly the roll's hops. Silently keeping the first
    // `n_moves` would write a ply that replays to the wrong board, and the
    // error only surfaces plies later as an impossible position -- so say it
    // here. A play with extra hops is an illegal one (see xg2gva.js); a
    // converter must reshape it before writing, not hand it over as-is.
    throw new Error(
      `ply has ${moves.length} move steps but action_id ${action_id} has room `
      + `for ${n_moves}; an illegal play must be written as a set-position ply`,
    );
  }
  const step_bytes = [];
  for (let i = 0; i < n_moves; i++) {
    if (i < moves.length) {
      const m = moves[i];
      step_bytes.push(_pack_move_step(Number(m.from || 0), Number(m.pips || 0)));
    } else {
      step_bytes.push(0);
    }
  }
  const move0 = step_bytes.length ? step_bytes[0] : 0;
  first_byte |= (move0 & 0x03) << 6;
  return _concat(_u8(first_byte), new Uint8Array(step_bytes));
}

// ---------------------------------------------------------------------------
// Analysis-record builders: reshape a ply's inline `analysis` dict into the
// flat records the base EVAL/ALTS/CUBE writers and the GVAN writer consume.
// ---------------------------------------------------------------------------

function _build_checker_eval(game_index, ply_index, analysis) {
  const alts_raw = analysis.alternatives || [];
  const alts = alts_raw.slice(0, MAX_ALTS_PER_DECISION).map(a => ({
    move: a.move || [],
    equity: Number(a.equity || 0.0),
    probs: a.eval !== undefined ? _probs_from_eval(a.eval) : null,
    is_played: Boolean(a.is_played),
    eval_level: a.eval_level,
  }));
  const has_luck = 'luck' in analysis;
  return {
    game_index,
    ply_index,
    probs: analysis.eval !== undefined ? _probs_from_eval(analysis.eval) : null,
    best_equity: Number(analysis.best_equity || 0.0),
    equity_loss: Number(analysis.equity_loss || 0.0),
    ply: Number(analysis.ply || 0),
    has_missed_double: analysis.missed_double !== null && typeof analysis.missed_double === 'object',
    alternatives: alts,
    decision: Boolean(analysis.decision),
    has_luck,
    illegal_move: Boolean(analysis.illegal_move),
    luck: has_luck ? Number(analysis.luck || 0.0) : 0.0,
  };
}

function _build_cube_eval_decision(game_index, ply_index, analysis, ctype) {
  return {
    game_index,
    ply_index,
    type: ctype,
    correct_action: _cube_action_code(analysis.correct_action),
    played_action: _cube_action_code(analysis.played_action),
    no_double_eq: Number(analysis.no_double_equity || 0.0),
    double_take_eq: Number(analysis.double_take_equity || 0.0),
    double_pass_eq: Number(analysis.double_pass_equity || 0.0),
    probs: analysis.eval !== undefined ? _probs_from_eval(analysis.eval) : null,
    equity_loss: Number(analysis.equity_loss || 0.0),
    ply: Number(analysis.ply || 0),
    decision: Boolean(analysis.decision),
    eval_level: analysis.eval_level,
  };
}

// Cube decision so clear it should not count toward PR. Mirrors
// gvanalysis.game_eval._trivial_cube / xg2gva's trivialCube / stats.js's
// _trivial_cube.
function _trivial_cube(nd, dt, dp) {
  return (
    Math.abs(nd - Math.min(dt, dp)) < 0.001
    || (nd - dt) > 0.200
    || (nd - dp) > 0.200
    || (nd < -0.900 && dt < -0.900)
  );
}

/** Whether a missed double counts toward PR, from its own three equities.
 *
 *  Derived here rather than taken from the document, and derived rather than
 *  assumed. A CUBE type=2 entry has one decision bit and no way to spell "the
 *  source said nothing", so nothing sets `missed_double.decision` -- see
 *  `bgf2gva.js`, which drops BGBlitz's own answer for exactly that reason, and
 *  `stats.js`'s `_missed_double_counts`, which every reader falls back to. This
 *  writes the bit those readers will compute, so the file agrees with them.
 *
 *  It used to be hardcoded `true`. Nothing read it back, so nothing broke --
 *  but a missed double by 0.0002 on a cube nobody had to think about does not
 *  count, and a stored `true` is a claim about that ply which every reader of
 *  the file disagrees with. One is in the sample corpus.
 */
function _missed_double_counts(nd, dt, dp) {
  const doubler_err = Math.max(0.0, Math.min(dt, dp) - nd);
  return !(_trivial_cube(nd, dt, dp) && doubler_err < 0.001);
}

function _build_cube_eval_missed_double(game_index, ply_index, md, ev_ply) {
  return {
    game_index,
    ply_index,
    type: CUBE_TYPE_MISSED_DOUBLE,
    correct_action: _cube_action_code(md.correct_action || 'double'),
    played_action: CUBE_ACTION_NO_DOUBLE,
    no_double_eq: Number(md.no_double_equity || 0.0),
    double_take_eq: Number(md.double_take_equity || 0.0),
    double_pass_eq: Number(md.double_pass_equity || 0.0),
    // The CUBE entry carries its five probability fields whatever its type, so
    // a missed double stores the pre-roll probabilities like any other cube
    // record. They are the only probabilities that describe the cube decision
    // -- the checker ply's own eval is post-roll -- and a reader with nowhere
    // else to turn will otherwise show the checker play's. All-zero still means
    // absent, so files written before this are read exactly as they were.
    probs: md.eval !== undefined ? _probs_from_eval(md.eval) : null,
    equity_loss: Number(md.equity_loss || 0.0),
    ply: Number(ev_ply || 0),
    decision: _missed_double_counts(
      Number(md.no_double_equity || 0.0),
      Number(md.double_take_equity || 0.0),
      Number(md.double_pass_equity || 0.0)),
    eval_level: md.eval_level,
  };
}

function _build_cube_eval_resign(game_index, ply_index, analysis) {
  return {
    game_index,
    ply_index,
    type: CUBE_TYPE_RESIGN,
    correct_action: CUBE_ACTION_NO_DOUBLE,
    played_action: CUBE_ACTION_NO_DOUBLE,
    no_double_eq: Number(analysis.resign_error || 0.0),
    double_take_eq: Number(analysis.take_resign_error || 0.0),
    double_pass_eq: 0.0,
    probs: analysis.eval !== undefined ? _probs_from_eval(analysis.eval) : null,
    equity_loss: Number(analysis.equity_loss || 0.0),
    ply: Number(analysis.ply || 0),
    decision: Boolean(analysis.decision !== undefined ? analysis.decision : true),
    eval_level: null,
  };
}

function _build_cube_eval_live_checker(game_index, ply_index, cd, ev_ply) {
  return {
    game_index,
    ply_index,
    type: CUBE_TYPE_LIVE_CHECKER,
    correct_action: cd.should_double ? CUBE_ACTION_DOUBLE : CUBE_ACTION_NO_DOUBLE,
    played_action: CUBE_ACTION_NO_DOUBLE,
    no_double_eq: Number(cd.no_double_equity || 0.0),
    double_take_eq: Number(cd.double_take_equity || 0.0),
    double_pass_eq: Number(cd.double_pass_equity || 0.0),
    probs: cd.eval !== undefined ? _probs_from_eval(cd.eval) : null,
    equity_loss: 0.0,
    ply: Number(ev_ply || 0),
    decision: Boolean(cd.decision),
    eval_level: cd.eval_level,
  };
}

// ---------------------------------------------------------------------------
// Base chunk encoders (mirrors ogxm_io.cpp's write_mhdr/write_game/write_anal/
// write_eval/write_alts/write_cube)
// ---------------------------------------------------------------------------

function _encode_mhdr(ogxm, games) {
  let total_plies = 0;
  for (const g of games) {
    total_plies += (g.plies || []).length;
  }
  total_plies = Math.min(total_plies, 0xFFFF);

  const flags =
    (ogxm.crawford ? 0x01 : 0)
    | (ogxm.jacoby ? 0x02 : 0)
    | (ogxm.beaver ? 0x04 : 0)
    | (ogxm.raccoon ? 0x08 : 0);

  const cube_limit = Number(ogxm.cube_limit || 0) & 0xFFFF;
  const reserved8 = _concat(_u16(cube_limit), new Uint8Array(6));

  const match_length = Number(ogxm.match_length || 0) & 0xFFFF;
  const num_games = games.length & 0xFF;
  // The two final scores stop at the match length, for the same reason
  // points_won is capped on the way into a running score.
  const white_score = clampMatchScore(ogxm.white_score, ogxm.match_length) & 0xFFFF;
  const black_score = clampMatchScore(ogxm.black_score, ogxm.match_length) & 0xFFFF;
  const result = Number(ogxm.result || 0) & 0xFF;
  const source = Number(ogxm.source || 0) & 0xFF;
  const timestamp = Number(ogxm.timestamp || 0) & 0xFFFFFFFF;

  // struct.pack("<HBBHHBBIH8s", ...)
  const hdr = _concat(
    _u16(match_length),
    _u8(num_games),
    _u8(flags),
    _u16(white_score),
    _u16(black_score),
    _u8(result),
    _u8(source),
    _u32(timestamp),
    _u16(total_plies),
    reserved8,
  );

  // GammonView extension: `event` then `site` appended after player_black,
  // each its own length-prefixed string. Old readers read the two player-name
  // strings by their own length prefixes and stop, so these trailing fields
  // are never reached; a reader that knows `event` but not `site` stops one
  // field earlier, by the same argument.
  return _concat(
    hdr,
    _pack_string(ogxm.player_white || ''),
    _pack_string(ogxm.player_black || ''),
    _pack_string(ogxm.event),
    _pack_string(ogxm.site),
  );
}

function _encode_anal(ply, num_checker, num_cube, timestamp, duration_ms, model_id) {
  // struct.pack("<BBHHIIH8s", ...)
  return _concat(
    _u8(ply & 0xFF),
    _u8(0),
    _u16(num_checker & 0xFFFF),
    _u16(num_cube & 0xFFFF),
    _u32(timestamp & 0xFFFFFFFF),
    _u32(duration_ms & 0xFFFFFFFF),
    _u16(0),
    new Uint8Array(8),
    _pack_string(model_id),
  );
}

function _encode_eval_entry(ce) {
  const probs = ce.probs !== null && ce.probs !== undefined
    ? ce.probs : [0.0, 0.0, 0.0, 0.0, 0.0];
  // struct.pack("<BHHHHHHhHBBB", ...)
  return _concat(
    _u8(ce.game_index & 0xFF),
    _u16(ce.ply_index & 0xFFFF),
    _u16(_enc_prob(probs[0])),
    _u16(_enc_prob(probs[1])),
    _u16(_enc_prob(probs[2])),
    _u16(_enc_prob(probs[3])),
    _u16(_enc_prob(probs[4])),
    _i16(_enc_equity(ce.best_equity)),
    _u16(_enc_equity_loss(ce.equity_loss)),
    _u8(Math.min(ce.alternatives.length, MAX_ALTS_PER_DECISION) & 0xFF),
    _u8(ce.ply & 0xFF),
    _u8((ce.has_missed_double ? 0x01 : 0) & 0xFF),
  );
}

function _encode_alt_entry(alt) {
  const move_bytes = new Uint8Array(4);
  const steps = alt.move || [];
  for (let i = 0; i < Math.min(4, steps.length); i++) {
    const s = steps[i];
    move_bytes[i] = _pack_move_step(Number(s.from || 0), Number(s.pips || 0));
  }
  const probs = alt.probs !== null && alt.probs !== undefined
    ? alt.probs : [0.0, 0.0, 0.0, 0.0, 0.0];
  // struct.pack("<4sh5HB", ...)
  return _concat(
    move_bytes,
    _i16(_enc_equity(alt.equity)),
    _u16(_enc_prob(probs[0])),
    _u16(_enc_prob(probs[1])),
    _u16(_enc_prob(probs[2])),
    _u16(_enc_prob(probs[3])),
    _u16(_enc_prob(probs[4])),
    _u8(alt.is_played ? 0x01 : 0),
  );
}

function _encode_cube_entry(c) {
  const probs = c.probs !== null && c.probs !== undefined
    ? c.probs : [0.0, 0.0, 0.0, 0.0, 0.0];
  // struct.pack("<BHBhhhHHHHHHBBB3s", ...)
  return _concat(
    _u8(c.game_index & 0xFF),
    _u16(c.ply_index & 0xFFFF),
    _u8(c.type & 0xFF),
    _i16(_enc_equity(c.no_double_eq)),
    _i16(_enc_equity(c.double_take_eq)),
    _i16(_enc_equity(c.double_pass_eq)),
    _u16(_enc_prob(probs[0])),
    _u16(_enc_prob(probs[1])),
    _u16(_enc_prob(probs[2])),
    _u16(_enc_prob(probs[3])),
    _u16(_enc_prob(probs[4])),
    _u16(_enc_equity_loss(c.equity_loss)),
    _u8(c.correct_action & 0xFF),
    _u8(c.played_action & 0xFF),
    _u8(c.ply & 0xFF),
    new Uint8Array(3),
  );
}

// ---------------------------------------------------------------------------
// GVAN chunk encoder
// ---------------------------------------------------------------------------

const _EVAL_LEVEL_PLY_RE = /^(\d+)ply$/;
const _EVAL_LEVEL_TRUNC_RE = /^truncated(\d+)$/;

function _encode_eval_level(level) {
  if (!level) return 0;
  let m = _EVAL_LEVEL_PLY_RE.exec(level);
  if (m) return parseInt(m[1], 10) & 0x0F;
  m = _EVAL_LEVEL_TRUNC_RE.exec(level);
  if (m) return (parseInt(m[1], 10) & 0x0F) | 0x10;
  if (level === 'rollout') return 0x20;
  if (level === 'database') return 0x40;
  return 0;
}

function _encode_gvan(analysis_info, checker_evals, cube_evals) {
  const ai = analysis_info || {};
  const base_level = _encode_eval_level(ai.eval_level);
  const luck_level_raw = ai.luck_eval_level;
  const luck_level = luck_level_raw ? _encode_eval_level(luck_level_raw) : 0x01;

  let total_alts = 0;
  for (const ce of checker_evals) {
    total_alts += ce.alternatives.length;
  }

  let section_flags = 0;
  if (checker_evals.length) section_flags |= 0x01;
  if (total_alts) section_flags |= 0x02;
  if (cube_evals.length) section_flags |= 0x04;

  const parts = [];
  // struct.pack("<BBBB", 2, base_level, section_flags, luck_level)
  parts.push(_concat(
    _u8(GVAN_VERSION),
    _u8(base_level & 0xFF),
    _u8(section_flags & 0xFF),
    _u8(luck_level & 0xFF),
  ));

  if (checker_evals.length) {
    for (const ce of checker_evals) {
      let flags = 0;
      if (ce.decision) flags |= 0x01;
      if (ce.has_luck) flags |= 0x02;
      if (ce.illegal_move) flags |= 0x04;
      const luck = ce.has_luck ? _enc_equity(ce.luck) : 0;
      // struct.pack("<Bh", flags, luck)
      parts.push(_concat(_u8(flags & 0xFF), _i16(luck)));
    }
  }

  if (total_alts) {
    for (const ce of checker_evals) {
      for (const alt of ce.alternatives) {
        parts.push(_u8(_encode_eval_level(alt.eval_level) & 0xFF));
      }
    }
  }

  if (cube_evals.length) {
    for (const c of cube_evals) {
      const flags = c.decision ? 0x01 : 0;
      // struct.pack("<BB", flags, eval_level)
      parts.push(_concat(
        _u8(flags & 0xFF),
        _u8(_encode_eval_level(c.eval_level) & 0xFF),
      ));
    }
  }

  return _concat(...parts);
}

// ---------------------------------------------------------------------------
// Analysis blocks (one ANAL group per analysis; the format allows up to
// MAX_ANALYSES). `select(ply)` picks which analysis object to serialize for a
// ply -- the single `analysis` for one block, or the matching `analyses[]` entry
// for a given block in the multi-analysis case.
// ---------------------------------------------------------------------------

// Serialize one analysis block as its ANAL -> EVAL -> ALTS -> CUBE -> GVAN
// chunk group. EVAL/ALTS/CUBE are omitted when they would be empty; GVAN (the
// non-critical GammonView extension) is always written.
function _encode_analysis_block(info, checker_evals, cube_evals) {
  const ai = (info !== null && typeof info === 'object') ? info : {};
  const parts = [];

  parts.push(_chunk(
    CHUNK_ANAL,
    _encode_anal(Number(ai.ply || 0), checker_evals.length, cube_evals.length,
      Number(ai.timestamp || 0), Number(ai.duration_ms || 0), ai.model_id || ''),
    false,
  ));

  if (checker_evals.length) {
    const eval_data = _concat(...checker_evals.map(ce => _encode_eval_entry(ce)));
    parts.push(_chunk(CHUNK_EVAL, eval_data, false));
  }

  if (checker_evals.some(ce => ce.alternatives.length > 0)) {
    const alts_parts = [];
    for (const ce of checker_evals) {
      for (const alt of ce.alternatives) {
        alts_parts.push(_encode_alt_entry(alt));
      }
    }
    parts.push(_chunk(CHUNK_ALTS, _concat(...alts_parts), false));
  }

  if (cube_evals.length) {
    const cube_data = _concat(...cube_evals.map(c => _encode_cube_entry(c)));
    parts.push(_chunk(CHUNK_CUBE, cube_data, false));
  }

  parts.push(_chunk(CHUNK_GVAN, _encode_gvan(ai, checker_evals, cube_evals), false));

  return _concat(...parts);
}

// Build [checker_evals, cube_evals] for one analysis block by walking every ply
// in game/ply order and reading its analysis object via `select`.
function _gather_evals(games, select) {
  const checker_evals = [];
  const cube_evals = [];
  for (const g of games) {
    const game_index = Number(g.game_index || 0);
    const plies = g.plies || [];
    for (let pi = 0; pi < plies.length; pi++) {
      const ply = plies[pi];
      const _raw = ply.action_id;
      const action_id = _raw !== null && _raw !== undefined ? Number(_raw) : 30;
      const analysis = select(ply);
      if (analysis === null || typeof analysis !== 'object') continue;
      if (action_id >= 0 && action_id <= 20) {
        checker_evals.push(_build_checker_eval(game_index, pi, analysis));
        // missed_double and cube_decision are mutually exclusive per ply
        // (mirrors ogxm_json.cpp's if/else): both map to base CUBE entries
        // (type=2 / type=4 respectively).
        const md = analysis.missed_double;
        const cd = analysis.cube_decision;
        if (md !== null && typeof md === 'object') {
          cube_evals.push(_build_cube_eval_missed_double(
            game_index, pi, md, analysis.ply || 0));
        } else if (cd !== null && typeof cd === 'object') {
          cube_evals.push(_build_cube_eval_live_checker(
            game_index, pi, cd, analysis.ply || 0));
        }
      } else if (
        action_id === ACTION_DOUBLE
        || action_id === ACTION_TAKE
        || action_id === ACTION_DROP
      ) {
        const ctype = action_id === ACTION_DOUBLE
          ? CUBE_TYPE_DOUBLE_DECISION : CUBE_TYPE_TAKE_PASS;
        cube_evals.push(_build_cube_eval_decision(game_index, pi, analysis, ctype));
      } else if (
        action_id === ACTION_RESIGN_GAME
        || action_id === ACTION_RESIGN_MATCH
      ) {
        cube_evals.push(_build_cube_eval_resign(game_index, pi, analysis));
      }
    }
  }
  return [checker_evals, cube_evals];
}

// Resolve the analysis blocks to serialize as [[info, checker_evals,
// cube_evals], ...], primary first.
//
// Multi-analysis (`analyses_info` present): one block per entry, each reading
// its ply `analyses[]` entry by `analysis_index`. Otherwise the legacy single
// block from `analysis_info` + per-ply `analysis`. Empty when the match carries
// no analysis at all.
function _analysis_blocks(ogxm, games) {
  const analyses_info = ogxm.analyses_info;
  if (Array.isArray(analyses_info) && analyses_info.length) {
    const blocks = [];
    for (let k = 0; k < analyses_info.length; k++) {
      const select = (ply) => {
        for (const a of ply.analyses || []) {
          if (a.analysis_index === k) return a;
        }
        return null;
      };
      const [ce, cu] = _gather_evals(games, select);
      blocks.push([analyses_info[k] || {}, ce, cu]);
    }
    return blocks;
  }

  const [ce, cu] = _gather_evals(games, ply => ply.analysis);
  const analysis_info = ogxm.analysis_info;
  if ((analysis_info !== null && typeof analysis_info === 'object')
      || ce.length || cu.length) {
    return [[
      (analysis_info !== null && typeof analysis_info === 'object')
        ? analysis_info : {},
      ce,
      cu,
    ]];
  }
  return [];
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

function write_gvab(ogxm) {
  const games = ogxm.games || [];

  const game_chunks = [];

  let has_set_position = false;
  let has_set_position_dice = false;

  for (const g of games) {
    const plies = g.plies || [];
    const game_index = Number(g.game_index || 0);

    let first_to_move = g.first_to_move;
    if (first_to_move === null || first_to_move === undefined) {
      first_to_move = plies.length ? Number(plies[0].color || 0) : 0xFF;
    }

    const gflags =
      (g.is_crawford ? 0x01 : 0)
      | (g.is_lastgame ? 0x02 : 0);
    const winner = g.winner !== null && g.winner !== undefined
      ? Number(g.winner) : WINNER_INCOMPLETE;
    // Stored verbatim, like the reference writer: this is the game's full
    // value, and capping it here would destroy the win type (a match-ending
    // 4-point gammon would become indistinguishable from a single). Consumers
    // that sum it into a running score cap it there -- see capPointsWon.
    const points_won = Math.max(0, Number(g.points_won || 0)) & 0xFF;

    // struct.pack("<BHBBBBB", ...)
    const hdr = _concat(
      _u8(game_index & 0xFF),
      _u16(plies.length & 0xFFFF),
      _u8(winner & 0xFF),
      _u8(points_won & 0xFF),
      _u8(gflags & 0xFF),
      _u8(Number(first_to_move) & 0xFF),
      _u8(0),
    );

    const ib = g.initial_board;
    let board_bytes;
    if (Array.isArray(ib) && ib.length === 26) {
      board_bytes = _concat(
        _packSignedBytes(ib.map(v => Number(v))),
        new Uint8Array(1),
      );
    } else {
      board_bytes = new Uint8Array([DEFAULT_BOARD_SENTINEL, DEFAULT_BOARD_SENTINEL]);
    }

    const ply_parts = [];
    for (let pi = 0; pi < plies.length; pi++) {
      const ply = plies[pi];
      const _raw_action_id = ply.action_id;
      const action_id = _raw_action_id !== null && _raw_action_id !== undefined
        ? Number(_raw_action_id) : 30;
      ply_parts.push(_encode_ply(ply, action_id));

      if (action_id === ACTION_SET_POSITION) {
        has_set_position = true;
        const d1 = ply.d1;
        const d2 = ply.d2;
        if (Number.isInteger(d1) && Number.isInteger(d2) && d1 >= 1 && d1 <= 6 && d2 >= 1 && d2 <= 6) {
          has_set_position_dice = true;
        }
      }

    }

    const game_data = _concat(hdr, board_bytes, _concat(...ply_parts));
    game_chunks.push(_chunk(CHUNK_GAME, game_data, true));
  }

  const blocks = _analysis_blocks(ogxm, games);
  if (blocks.length > MAX_ANALYSES) {
    throw new Error(
      `too many analysis blocks: ${blocks.length} (format cap is ${MAX_ANALYSES})`);
  }
  const has_analysis = blocks.length > 0;

  let min_reader_minor = 0;
  if (has_set_position_dice) {
    min_reader_minor = 2;
  } else if (has_set_position) {
    min_reader_minor = 1;
  }
  // Pre-1.3 readers stop at the first ANAL, so a second block has to be gated.
  if (blocks.length > 1) {
    min_reader_minor = Math.max(min_reader_minor, 3);
  }

  const header_flags = has_analysis ? HEADER_FLAG_HAS_ANALYSIS : 0;

  const parts = [];
  // File header: struct.pack("<IHHHHII", ...)
  parts.push(_concat(
    _u32(OGXM_MAGIC),
    _u16(VERSION_MAJOR),
    _u16(VERSION_MINOR),
    _u16(VERSION_MAJOR),
    _u16(min_reader_minor & 0xFFFF),
    _u32(0),  // file_size placeholder, patched below
    _u32(header_flags & 0xFFFFFFFF),
  ));

  parts.push(_chunk(CHUNK_MHDR, _encode_mhdr(ogxm, games), true));
  for (const gc of game_chunks) {
    parts.push(gc);
  }

  const [per_block_extra, trailing_extra] = _split_passthrough(ogxm, blocks.length);
  for (let i = 0; i < blocks.length; i++) {
    const [info, checker_evals, cube_evals] = blocks[i];
    parts.push(_encode_analysis_block(info, checker_evals, cube_evals));
    parts.push(_encode_passthrough(per_block_extra[i]));
  }
  // CLCK/VIDO and any other chunk we do not decode: after every analysis
  // block, before CSUM, which is the order the base spec fixes for them.
  parts.push(_encode_passthrough(trailing_extra));

  // CSUM chunk (CRC32 algorithm=0, value patched later)
  const csum_chunk_start = _concat(...parts).length;
  const csum_data = _concat(_u32(0), new Uint8Array(4));
  parts.push(_chunk(CHUNK_CSUM, csum_data, true));

  // End marker
  const total_size = _concat(...parts).length + 8;
  parts.push(_concat(_u32(END_MAGIC), _u32(total_size)));

  // Assemble, then patch file_size and CRC in place
  const out = _concat(...parts);

  // Patch file_size at offset 12 (after magic + 4 uint16s)
  new DataView(out.buffer, out.byteOffset).setUint32(12, total_size, true);

  // CRC over everything before the CSUM chunk
  const crc = _crc32(out.slice(0, csum_chunk_start));
  // CSUM chunk layout: 12-byte chunk header + 4-byte algorithm + 4-byte CRC value
  const crc_value_offset = csum_chunk_start + 12 + 4;
  new DataView(out.buffer, out.byteOffset).setUint32(crc_value_offset, crc, true);

  return out;
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

export {
  // CRC
  _crc32,
  // Fixed-point encoding
  _round_c,
  _enc_prob,
  _enc_equity,
  _enc_equity_loss,
  MAX_EQUITY_LOSS,
  // Pack helpers
  _pack_move_step,
  _pack_string,
  _chunk,
  // Eval helpers
  _probs_from_eval,
  _cube_action_code,
  // Ply encoding
  _encode_ply,
  // Analysis-record builders
  _build_checker_eval,
  _build_cube_eval_decision,
  _build_cube_eval_missed_double,
  _build_cube_eval_resign,
  _build_cube_eval_live_checker,
  // Base chunk encoders
  _encode_mhdr,
  _encode_anal,
  _encode_eval_entry,
  _encode_alt_entry,
  _encode_cube_entry,
  // GVAN
  _encode_eval_level,
  _encode_gvan,
  // Public API
  write_gvab,
};
