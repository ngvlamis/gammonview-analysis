// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Reading OGXM v2 -- HedgeHog's current match format -- into the document
// `readGvab` returns for a v1 file.
//
// v2 (HedgeHog's `docs/OGXM_FORMAT_SPEC.md`, 2.2 at the time of writing) keeps
// v1's magic and its ideas and changes nearly every byte: a 16-byte header,
// sections with a 9-byte header, varint-framed records with a presence mask,
// plies addressed by one match-wide ordinal (`ply_ref`), and a single `DECS`
// section holding every decision of an analysis block. HedgeHog writes nothing
// else since 2.0; v1 is frozen.
//
// **This is a reader into the v1 shape, not a v2 codec.** Nothing here writes
// v2, and nothing downstream learns there was a second version: the document
// that comes out is the one a v1 file holding the same match would give,
// `basefill.js` completes its analysis exactly as it completes any foreign v1
// block, and saving it writes ordinary v1 `.gvab`. HedgeHog's own `to_v1`
// (`src/match/ogxm2_v1.cpp`) is the model for the mapping, with two
// departures, both towards reading more files rather than fewer:
//
//   * `to_v1` refuses a game with a `termination` field, a file with `ANNO`,
//     `MSIG`, a `completed_at`, and so on -- content its v1 *writer* cannot
//     store. We are not writing a v1 file of record (the original upload is
//     what is kept), so content that does not change what is on the board is
//     simply not carried: signatures, annotations, the clock, the video. What
//     *would* change the board or the score -- beavers, a cube set by edit,
//     a settlement, a starting score -- is refused, with a message for the
//     player, because the v1 shape has no way to say it and a wrong board is
//     worse than none.
//
//   * Units. A v2 block names its currency, and HedgeHog's match analyses are
//     `cubeful_match`: every equity in them -- each candidate move's, the three
//     cube values, the losses -- is a match-winning chance. We store
//     normalized equity throughout, so each value is mapped through its own
//     ply's frame (`mwcFrame` in basefill.js). Because the currency is stated,
//     this is a conversion and not an inference; basefill's own unit test is
//     switched off for these blocks so nothing is converted twice.
//
// **Luck is read**, from v2's `ROLL` decisions, as it is from an `.xg` or a
// `.bgf`: the producer measured it, so the file says it. It lands as `luck` on
// the dice ply's analysis, in the same normalized units as everything else,
// with the block's `luck_eval_level` taken from the rolls' own level. A block
// that has no rolls is the one listed in `_base_analyses` -- the record of a
// foreign block still missing its luck, which is what sends a match to
// `POST /luck`.
//
// Validation is the framing's, not the spec's every rule: every record must
// end where its length says, every varint must fit, every ply_ref must name a
// ply. A file that frames cleanly but breaks a v2 invariant (a redundant level
// override, say) is read rather than refused -- the same leniency the v1
// reader extends.

import {
  GvabError, _deriveOgids, _attachBlocks, _evalFromProbs, _cubeActionLabel,
} from './reader.js';
import { _crc32 } from './binary.js';
import { completeBaseBlock, mwcFrame } from './basefill.js';
import { DICE_TABLE } from './constants.js';

// ---------------------------------------------------------------------------
// Constants (spec section numbers in brackets)
// ---------------------------------------------------------------------------

const HEADER_SIZE = 16;                          // [2.1]
const SECTION_HEADER_SIZE = 9;                   // [2.2]
const END_MARKER = 'END!';                       // [2.3]
const READER_MAJOR = 2;
const READER_MINOR = 2;
const MAX_FILE_SIZE = 64 * 1024 * 1024;          // [10]

const KNOWN_SECTIONS = new Set([
  'MTCH', 'GAME', 'ANAL', 'DECS', 'SIGN', 'CLCK', 'VIDO', 'ANNO', 'MSIG', 'CSUM',
]);

// [9.14] action ids beyond v1's 0-31 that change the board, the cube or the
// score, so a document without them would replay wrongly.
const ACTION_DOUBLE = 21;
const ACTION_TAKE = 22;
const ACTION_DROP = 23;
const ACTION_RESIGN_GAME = 27;
const ACTION_RESIGN_MATCH = 28;
const ACTION_SET_POSITION = 31;
const ACTION_ESCAPE = 63;
const UNSUPPORTED_ACTIONS = {
  32: 'a beaver',
  33: 'a raccoon',
  34: 'a settlement',
  35: 'a reserved move code',
  36: 'a cube value set by hand',
  37: 'a turn with no recorded roll',
};

const KIND_CHECKER = 0;
const KIND_CUBE = 1;
const KIND_RESIGN = 2;
const KIND_ROLL = 3;

const CURRENCY_CUBEFUL_MATCH = 2;                // [9.10]

// [9.13] verdict -> the v1 label set (no_double / double / take / pass).
// "Too good" is a no-double; a beaver or raccoon verdict is a take that does
// better than a take, and the take is what the v1 shape can say.
const VERDICT_NAMES = {
  0: 'no_double', 1: 'double', 2: 'take', 3: 'pass', 4: 'no_double',
  5: 'take', 6: 'take',
};

const PRODUCER_OGX = 0;                          // [9.9] HedgeHog's own engine

// ---------------------------------------------------------------------------
// Byte cursor
// ---------------------------------------------------------------------------

const _utf8 = new TextDecoder('utf-8', { fatal: true });

class Cursor {
  constructor(data, pos, end) {
    this.data = data;
    this.view = new DataView(data.buffer, data.byteOffset, data.byteLength);
    this.pos = pos;
    this.end = end;
  }

  need(n) {
    if (this.pos + n > this.end) {
      throw new GvabError(`OGXM v2 record truncated at offset ${this.pos}`);
    }
  }

  u8() { this.need(1); return this.data[this.pos++]; }
  u16() { this.need(2); const v = this.view.getUint16(this.pos, true); this.pos += 2; return v; }
  i32() { this.need(4); const v = this.view.getInt32(this.pos, true); this.pos += 4; return v; }
  u32() { this.need(4); const v = this.view.getUint32(this.pos, true); this.pos += 4; return v; }
  skip(n) { this.need(n); this.pos += n; }

  bytes(n) {
    this.need(n);
    const out = this.data.subarray(this.pos, this.pos + n);
    this.pos += n;
    return out;
  }

  /** LEB128 as a BigInt, at most `bits` wide [3.1 V1]. */
  _leb(bits) {
    let v = 0n;
    let shift = 0n;
    const maxBytes = Math.ceil(bits / 7);
    for (let i = 0; i < maxBytes; i++) {
      const b = this.u8();
      v |= BigInt(b & 0x7F) << shift;
      shift += 7n;
      if (!(b & 0x80)) {
        if (b === 0 && i > 0) throw new GvabError('OGXM v2 overlong varint');
        if (v >> BigInt(bits)) throw new GvabError('OGXM v2 varint exceeds its width');
        return v;
      }
    }
    throw new GvabError('OGXM v2 varint too long');
  }

  varint() { return Number(this._leb(32)); }
  varint64() { return this._leb(64); }

  str() {
    const n = this.varint();
    try {
      return _utf8.decode(this.bytes(n));
    } catch (e) {
      if (e instanceof GvabError) throw e;
      throw new GvabError('OGXM v2 string is not valid UTF-8');
    }
  }

  equity() { return this.i32() / 1e6; }        // [3.3]
  loss() { return this.u32() / 1e6; }
  prob() { return this.u16() / 10000; }
  probs() { return [this.prob(), this.prob(), this.prob(), this.prob(), this.prob()]; }

  board() {
    this.need(26);
    const out = [];
    for (let i = 0; i < 26; i++) out.push(this.view.getInt8(this.pos + i));
    this.pos += 26;
    return out;
  }

  /** A length-prefixed record [3.2]: its mask and a cursor over its body. */
  record() {
    const len = this.varint();
    const start = this.pos;
    if (start + len > this.end) {
      throw new GvabError(`OGXM v2 record at ${start} claims ${len} bytes past its parent`);
    }
    this.pos = start + len;
    const body = new Cursor(this.data, start, start + len);
    const mask = body.varint64();
    return { body, has: (bit) => ((mask >> BigInt(bit)) & 1n) === 1n };
  }
}

/** A step byte [5.1]: `pips << 5 | from`. */
function _step(b) {
  const from = b & 0x1F;
  const pips = (b >> 5) & 0x07;
  if (from > 25 || pips === 0 || pips === 7) {
    throw new GvabError(`OGXM v2 step byte 0x${b.toString(16)} is not a move`);
  }
  return { from, pips };
}

function _countedSteps(cur) {
  const n = cur.varint();
  const out = [];
  for (let i = 0; i < n; i++) out.push(_step(cur.u8()));
  return out;
}

// ---------------------------------------------------------------------------
// Nested records
// ---------------------------------------------------------------------------

/** [6.2] */
function _rollout(cur) {
  const { body, has } = cur.record();
  const r = {};
  if (has(0)) r.trials = body.varint();
  if (has(1)) r.truncation_depth = body.varint();
  if (has(2)) r.move_ply = body.varint();
  if (has(3)) r.variance_reduction = body.varint();
  if (has(4)) body.skip(8);                      // seed: provenance only
  if (has(5)) r.budget_ms = body.varint();
  if (has(6)) r.match_policy = body.varint();
  return r;
}

/** [6.4] One tier's override, unresolved. */
function _level(cur) {
  const { body, has } = cur.record();
  const l = {};
  if (has(0)) l.preset = body.str();
  if (has(1)) l.checker_ply = body.varint();
  if (has(2)) l.cube_ply = body.varint();
  if (has(3)) l.rollout = _rollout(body);
  if (has(4)) l.no_rollout = true;
  return l;
}

/** [6.4] The effective level: each field from the innermost tier carrying it. */
function _resolve(parent, override) {
  if (!override) return parent;
  const out = { ...parent };
  if (override.preset !== undefined) out.preset = override.preset;
  if (override.checker_ply !== undefined) out.checker_ply = override.checker_ply;
  if (override.cube_ply !== undefined) out.cube_ply = override.cube_ply;
  if (override.no_rollout) delete out.rollout;
  if (override.rollout) {
    out.rollout = { ...(override.no_rollout ? {} : parent.rollout), ...override.rollout };
  }
  return out;
}

function _levelLabel(level) {
  if (level.rollout) return 'rollout';
  return level.checker_ply !== undefined ? `${level.checker_ply}ply` : null;
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

/** [4] */
function _decodeMtch(payload) {
  const cur = new Cursor(payload, 0, payload.length);
  const { body, has } = cur.record();
  const m = { match_length: body.varint(), variant: body.varint() };
  if (has(0)) m.white_name = body.str();
  if (has(1)) m.black_name = body.str();
  if (has(2)) m.rules = body.varint();
  if (has(3)) m.cube_limit = body.varint();
  if (has(4)) m.score_start = [body.varint(), body.varint()];
  if (has(5)) m.score_final = [body.varint(), body.varint()];
  if (has(6)) m.result = body.varint();
  if (has(7)) m.source = body.varint();
  if (has(8)) m.started_at = body.varint64();
  if (has(9)) body.varint64();                   // completed_at
  if (has(10)) body.varint();                    // player_seat
  if (has(11)) m.crawford_before_start = true;
  if (has(12)) m.event = body.str();
  if (has(13)) m.event_year = body.varint();
  if (has(14)) body.varint();                    // date_precision
  if (has(15)) body.str();                       // stage
  if (has(16)) body.varint();                    // round
  if (has(17)) body.str();                       // table
  if (has(18)) m.city = body.str();
  if (has(19)) m.country = body.str();
  if (has(20)) body.str();                       // event_url
  if (has(21)) m.site = body.str();
  // Bits 22-25 (match_ref, the two player profiles, rated) are the last fields
  // and nothing here reads them, so the record's own length steps over them.
  return m;
}

/** [5.1] One ply, from `cur`; advances it. */
function _decodePly(cur) {
  const b0 = cur.u8();
  const ply = { action: b0 & 0x3F, seat: (b0 >> 6) & 1, steps: [] };
  const hasExtras = Boolean(b0 & 0x80);
  if (ply.action <= 20) {
    const moveBytes = DICE_TABLE[ply.action][2];
    for (let i = 0; i < moveBytes; i++) {
      const b = cur.u8();
      if (b === 0) break;                        // zero-terminated within move_bytes
      ply.steps.push(_step(b));
    }
  }
  if (hasExtras) {
    const { body, has } = cur.record();
    const x = {};
    if (has(0)) x.dice = [body.u8(), body.u8()];
    if (has(1)) x.resign_value = body.varint();
    if (has(2)) x.cube_value = body.varint();
    if (has(3)) x.illegal = true;
    if (has(4)) x.settle_value = body.equity();
    if (has(5)) x.steps = _countedSteps(body);
    if (has(6)) x.board = body.board();
    if (has(7)) x.action_ext = body.varint();
    if (has(8)) x.cube_owner = body.varint();
    ply.extras = x;
    if (ply.action === ACTION_ESCAPE) {
      if (x.action_ext === undefined) throw new GvabError('OGXM v2 escaped action without its id');
      ply.action = x.action_ext;
    }
  }
  return ply;
}

/** [5] */
function _decodeGame(payload) {
  const cur = new Cursor(payload, 0, payload.length);
  const { body, has } = cur.record();
  const g = {};
  if (has(0)) g.winner = body.varint();
  if (has(1)) g.points_won = body.varint();
  if (has(2)) g.is_last_game = true;
  if (has(3)) g.initial_board = body.board();
  if (has(4)) g.initial_cube_value = body.varint();
  if (has(5)) g.initial_cube_owner = body.varint();
  if (has(6)) g.auto_doubles = body.varint();
  if (has(7)) g.termination = body.varint();
  g.plies = [];
  while (cur.pos < cur.end) g.plies.push(_decodePly(cur));
  return g;
}

/** [6] */
function _decodeAnal(payload) {
  const cur = new Cursor(payload, 0, payload.length);
  const { body, has } = cur.record();
  body.skip(16);                                 // analysis_id
  const a = {};
  if (has(0)) body.skip(32);                     // match_digest
  if (has(1)) a.producer = body.varint();
  if (has(2)) a.level = _level(body);
  if (has(3)) a.complete = true;
  if (has(4)) {
    const n = body.varint();
    for (let i = 0; i < n; i++) body.varint();   // coverage: attempted, not stored
  }
  if (has(5)) a.model_id = body.str();
  if (has(6)) a.model_name = body.str();
  if (has(7)) body.skip(32);                     // model_digest
  if (has(8)) body.str();                        // engine_build
  if (has(9)) a.currency = body.varint();
  // The rest (cube efficiency, MET, tables, dials, start/end times, duration,
  // sources) is provenance, except the times, read below.
  if (has(10)) body.u16();
  if (has(11)) body.str();
  if (has(12)) body.str();
  if (has(13)) body.record();                    // dials
  if (has(14)) a.started_at = body.varint64();
  if (has(15)) body.varint64();
  if (has(16)) a.duration_ms = body.varint();
  return a;
}

/** [7.2] */
function _decodeAlternative(cur) {
  const { body, has } = cur.record();
  const n = body.varint();
  const steps = [];
  for (let i = 0; i < n; i++) steps.push(_step(body.u8()));
  const alt = { steps, equity: body.equity() };
  if (has(0)) alt.probs = body.probs();
  if (has(1)) alt.level = _level(body);
  if (has(2)) alt.is_played = true;
  return alt;
}

/** [7] Every record of one DECS payload, unknown kinds skipped. */
function _decodeDecs(payload) {
  const cur = new Cursor(payload, 0, payload.length);
  const out = [];
  while (cur.pos < cur.end) {
    const { body, has } = cur.record();
    const d = { ply_ref: body.varint(), kind: body.varint() };
    if (d.kind === KIND_CHECKER) {
      if (has(0)) {
        const n = body.varint();
        d.alternatives = [];
        for (let i = 0; i < n; i++) d.alternatives.push(_decodeAlternative(body));
      }
      if (has(1)) body.varint();                 // alternatives_total
      if (has(2)) d.best_equity = body.equity();
      if (has(3)) d.equity_loss = body.loss();
      if (has(4)) d.level = _level(body);
    } else if (d.kind === KIND_CUBE) {
      d.verdict = body.varint();
      if (has(0)) d.no_double_equity = body.equity();
      if (has(1)) d.double_take_equity = body.equity();
      if (has(2)) d.double_pass_equity = body.equity();
      if (has(3)) d.probs = body.probs();
      if (has(4)) d.equity_loss = body.loss();
      if (has(5)) body.u16();                    // take_point
      // bit 6, window_searched, is a flag: no bytes
      if (has(7)) d.level = _level(body);
      // bits 8-9 are flags; 10 is the one field before currency
      if (has(10)) body.equity();
      if (has(11)) d.currency = body.varint();
    } else if (d.kind === KIND_RESIGN) {
      if (has(0)) body.varint();                 // correct_value
      if (has(1)) d.resign_error = body.equity();
      if (has(2)) d.take_resign_error = body.equity();
      if (has(3)) d.probs = body.probs();
      if (has(4)) d.equity_loss = body.loss();
      if (has(5)) d.level = _level(body);
    } else if (d.kind === KIND_ROLL) {
      d.luck = body.equity();
      if (has(0)) d.level = _level(body);
    } else {
      continue;                                  // a kind this reader does not know
    }
    out.push(d);
  }
  return out;
}

/** [8.5] Verify a CRC32 CSUM; a SHA-256 one is skipped, as 8.5 allows. */
function _verifyCsum(data, payload, sectionStart) {
  const cur = new Cursor(payload, 0, payload.length);
  const { body } = cur.record();
  const algorithm = body.varint();
  const digest = body.bytes(body.varint());
  if (algorithm !== 0) return;
  if (digest.length !== 4) throw new GvabError('OGXM v2 CRC32 checksum is not 4 bytes');
  const want = new DataView(digest.buffer, digest.byteOffset, 4).getUint32(0, true);
  const got = _crc32(data.subarray(0, sectionStart));
  if (want !== got) {
    throw new GvabError(`CSUM mismatch: stored 0x${want.toString(16)}, computed 0x${got.toString(16)}`);
  }
}

// ---------------------------------------------------------------------------
// The match, in v1's shape
// ---------------------------------------------------------------------------

/** Refuse what the v1 shape cannot replay, in words a player can act on. */
function _unsupported(what) {
  return new GvabError(
    `This match uses ${what}, which GammonView cannot show yet.`);
}

/** v1's colour polarity is the Seat enum's reverse: 1 is White [JSON 3.3]. */
function _color(seat) {
  return seat === 0 ? 1 : 0;
}

function _v1Ply(p) {
  const x = p.extras || {};
  if (UNSUPPORTED_ACTIONS[p.action]) throw _unsupported(UNSUPPORTED_ACTIONS[p.action]);
  if (p.action > ACTION_SET_POSITION) throw _unsupported(`a move type (${p.action}) this reader does not know`);
  if (x.cube_value !== undefined || x.settle_value !== undefined
      || x.cube_owner !== undefined || (x.steps && x.steps.length)) {
    throw _unsupported('a move type this reader does not know');
  }
  const ply = { color: _color(p.seat), action_id: p.action };
  if (p.action <= 20) {
    const [d1, d2] = DICE_TABLE[p.action];
    ply.d1 = d1;
    ply.d2 = d2;
    ply.moves = p.steps;
  } else if (p.action === ACTION_SET_POSITION) {
    if (!x.board) throw new GvabError('OGXM v2 set-position ply without its board');
    if (x.dice) {
      ply.d1 = x.dice[0];
      ply.d2 = x.dice[1];
    }
    ply.set_position = x.board;
  }
  return ply;
}

/** Each game's [white, black] score at its start, and the final score [M8]. */
function _scoreWalk(mtch, games) {
  const length = mtch.match_length;
  let [w, b] = mtch.score_start || [0, 0];
  const starts = [];
  for (const g of games) {
    starts.push([w, b]);
    if (g.winner === undefined || g.points_won === undefined) continue;
    if (g.winner === 0) w += length > 0 ? Math.min(g.points_won, length - w) : g.points_won;
    else if (g.winner === 1) b += length > 0 ? Math.min(g.points_won, length - b) : g.points_won;
  }
  return { starts, final: [w, b] };
}

/** [5] The Crawford game is derived: the first at whose start one side is 1-away. */
function _crawfordGames(mtch, starts) {
  const out = starts.map(() => false);
  const length = mtch.match_length;
  if (!(length > 0) || !((mtch.rules || 0) & 1) || mtch.crawford_before_start) return out;
  const i = starts.findIndex(([w, b]) => w === length - 1 || b === length - 1);
  if (i >= 0 && Math.max(...starts[i]) < length) out[i] = true;
  return out;
}

function _v1Match(mtch, v2games) {
  if (mtch.variant !== 0) throw _unsupported('a backgammon variant');
  if (mtch.score_start && (mtch.score_start[0] || mtch.score_start[1])) {
    throw _unsupported('a match that starts part-way through');
  }
  const rules = mtch.rules || 0;
  if (rules & 0x10) throw _unsupported('automatic doubles');

  const { starts, final } = _scoreWalk(mtch, v2games);
  const crawford = _crawfordGames(mtch, starts);
  const [whiteScore, blackScore] = mtch.score_final || final;
  let result = mtch.result;
  if (result === undefined || result === 3) {
    const length = mtch.match_length;
    result = length > 0 && whiteScore >= length ? 1
      : length > 0 && blackScore >= length ? 2 : 0;
  }

  const games = v2games.map((g, gi) => {
    if ((g.initial_cube_value !== undefined && g.initial_cube_value !== 1)
        || (g.initial_cube_owner !== undefined && g.initial_cube_owner !== 2)) {
      throw _unsupported('a game that starts with the cube already turned');
    }
    if (g.auto_doubles) throw _unsupported('automatic doubles');
    const plies = g.plies.map(_v1Ply);
    const first = plies.find((p) => p.action_id < 24 || p.action_id === ACTION_RESIGN_GAME
                                  || p.action_id === ACTION_RESIGN_MATCH);
    // A game from a set-up position. `initial_board` does not move the board in
    // our documents -- `_deriveOgids` replays from the opening -- so the start
    // is stated the way our own exports state it: a leading set-position ply
    // with no dice, by the side on roll. Every ply after it moves down one,
    // which `plyOffset` hands back so decisions still find their plies.
    if (g.initial_board) {
      plies.unshift({
        color: first ? first.color : 1,
        action_id: ACTION_SET_POSITION,
        set_position: g.initial_board,
      });
    }
    const game = {
      game_index: gi,
      winner: g.winner !== undefined ? g.winner : 255,
      points_won: g.points_won || 0,
      is_crawford: crawford[gi],
      is_lastgame: Boolean(g.is_last_game),
      first_to_move: first ? first.color : 0,
      plies,
    };
    if (g.initial_board) game.initial_board = g.initial_board;
    return game;
  });
  const plyOffset = v2games.map((g) => (g.initial_board ? 1 : 0));

  let event = mtch.event || null;
  if (event && mtch.event_year) event = `${event} ${mtch.event_year}`;
  const rulesFlags = {
    crawford: Boolean(rules & 0x01),
    jacoby: Boolean(rules & 0x02),
    beaver: Boolean(rules & 0x04),
    raccoon: Boolean(rules & 0x08),
  };
  return [{
    match_length: mtch.match_length,
    player_white: mtch.white_name || '',
    player_black: mtch.black_name || '',
    white_score: whiteScore,
    black_score: blackScore,
    result,
    source: mtch.source || 0,
    timestamp: mtch.started_at !== undefined ? Number(mtch.started_at / 1000n) : 0,
    ...rulesFlags,
    cube_limit: mtch.cube_limit || 0,
    event,
    // Our `site` is where the match was played (XG's "location"); v2 has that
    // as `city`, and its own `site` is the host name of the platform, which is
    // the nearest thing when no city was recorded.
    site: mtch.city || mtch.site || null,
    games,
  }, plyOffset];
}

// ---------------------------------------------------------------------------
// Analysis, in v1's shape
// ---------------------------------------------------------------------------

/** The engine's name as `model_id`, which the viewer shows. v2 HedgeHog files
 *  carry a UUID there and the model's name beside it; the name is the one a
 *  person can read, and `hedgehog/` keeps it apart from an engine of ours. */
function _modelId(anal) {
  if (anal.model_name) {
    return anal.producer === PRODUCER_OGX ? `hedgehog/${anal.model_name}` : anal.model_name;
  }
  return anal.model_id || '';
}

const _r4 = (v) => Math.round(v * 10000) / 10000;
const _identity = { toEquity: _r4, toDelta: _r4 };

/** The checker play at a dice ply, as `_buildCheckerAnalysis` would give it. */
function _checker(d, blockLevel, frame) {
  const level = _resolve(blockLevel, d.level);
  const alts = d.alternatives || [];
  const levels = alts.map((a) => _resolve(level, a.level));
  const split = levels.some((l) => _levelLabel(l) !== _levelLabel(levels[0]));

  const bestMwc = d.best_equity !== undefined ? d.best_equity
    : (alts.length ? alts[0].equity : 0);
  const played = alts.find((a) => a.is_played);
  const lossMwc = d.equity_loss !== undefined ? d.equity_loss
    : (played ? bestMwc - played.equity : 0);

  const best = frame.toEquity(bestMwc);
  const loss = played && d.equity_loss === undefined
    ? Math.max(0, _r4(best - frame.toEquity(played.equity)))
    : Math.max(0, frame.toDelta(lossMwc));
  const analysis = {
    best_equity: best,
    played_equity: _r4(best - loss),
    equity_loss: loss,
    decision: false,
    alternatives: alts.map((a, i) => {
      const out = { move: a.steps, equity: frame.toEquity(a.equity), is_played: Boolean(a.is_played) };
      if (a.probs) out.eval = _evalFromProbs(...a.probs);
      if (split) {
        const label = _levelLabel(levels[i]);
        if (label !== null) out.eval_level = label;
      }
      return out;
    }),
  };
  const top = analysis.alternatives[0];
  if (top && top.eval) analysis.eval = { ...top.eval };
  if (level.checker_ply !== undefined && level.checker_ply !== blockLevel.checker_ply) {
    analysis.ply = level.checker_ply;
  }
  return analysis;
}

function _cubeTriple(d, frame) {
  return {
    no_double_equity: frame.toEquity(d.no_double_equity || 0),
    double_take_equity: frame.toEquity(d.double_take_equity || 0),
    double_pass_equity: frame.toEquity(d.double_pass_equity || 0),
  };
}

/** The cube decision at a dice ply: `missed_double` + `cube_decision`, or the
 *  live `cube_decision` alone -- the two shapes of `_buildMissedDouble` /
 *  `_buildCubeDecision` in reader.js. */
function _liveCube(d, frame, analysis) {
  const triple = _cubeTriple(d, frame);
  const evalObj = d.probs ? _evalFromProbs(...d.probs) : null;
  const shouldDouble = d.verdict === 1;
  const live = {
    should_double: shouldDouble,
    ...triple,
    action: _cubeActionLabel(shouldDouble, triple.double_take_equity, triple.double_pass_equity),
  };
  if (evalObj) live.eval = evalObj;
  if (d.equity_loss !== undefined) {
    const missed = {
      ...triple,
      equity_loss: Math.max(0, frame.toDelta(d.equity_loss)),
      correct_action: VERDICT_NAMES[d.verdict] || 'double',
    };
    if (evalObj) missed.eval = { ...evalObj };
    analysis.missed_double = missed;
  } else {
    live.decision = false;
  }
  analysis.cube_decision = live;
}

/** A cube ply's own decision (double, take, drop). */
function _cubePly(d, action, blockLevel, frame) {
  const level = _resolve(blockLevel, d.level);
  const analysis = {
    correct_action: VERDICT_NAMES[d.verdict] || 'no_double',
    played_action: action === ACTION_DOUBLE ? 'double' : action === ACTION_TAKE ? 'take' : 'pass',
    ..._cubeTriple(d, frame),
    equity_loss: Math.max(0, frame.toDelta(d.equity_loss || 0)),
    decision: false,
  };
  if (d.probs) analysis.eval = _evalFromProbs(...d.probs);
  if (level.cube_ply !== undefined && level.cube_ply !== blockLevel.cube_ply) {
    analysis.ply = level.cube_ply;
  }
  return analysis;
}

function _resign(d, frame) {
  const analysis = {
    resign_error: frame.toDelta(d.resign_error || 0),
    take_resign_error: frame.toDelta(d.take_resign_error || 0),
    equity_loss: Math.max(0, frame.toDelta(d.equity_loss || 0)),
    decision: false,
  };
  if (d.probs) analysis.eval = _evalFromProbs(...d.probs);
  return analysis;
}

/**
 * One v2 analysis block -> `[analysisInfo, Map<"gi,pi", analysis>]`.
 *
 * @param {object[]} plyAt  ply_ref -> {key, ply} (ply already carries its OGIDs)
 */
function _v1Block(anal, decisions, plyAt, matchLength) {
  const blockLevel = _resolve({}, anal.level);
  const blockMwc = matchLength > 0 && anal.currency === CURRENCY_CUBEFUL_MATCH;
  const blockObj = new Map();
  const luckLevels = new Set();

  for (const d of decisions) {
    const at = plyAt[d.ply_ref];
    if (at === undefined) {
      throw new GvabError(`OGXM v2 decision names ply ${d.ply_ref}, past the end of the match`);
    }
    const { key, ply } = at;
    const currency = d.currency !== undefined ? d.currency : anal.currency;
    const mwc = d.kind === KIND_CUBE
      ? matchLength > 0 && currency === CURRENCY_CUBEFUL_MATCH : blockMwc;
    // A ply with no frame (one past a replay failure) cannot be converted; its
    // decision is dropped rather than shown in the wrong unit.
    const frame = mwc ? mwcFrame(ply) : _identity;
    if (frame === null) continue;

    const action = ply.action_id;
    if (d.kind === KIND_CHECKER && (action <= 20 || action === ACTION_SET_POSITION)) {
      const prior = blockObj.get(key) || {};
      blockObj.set(key, { ..._checker(d, blockLevel, frame), ...prior });
    } else if (d.kind === KIND_CUBE && action <= 20) {
      if (!blockObj.has(key)) blockObj.set(key, {});
      _liveCube(d, frame, blockObj.get(key));
    } else if (d.kind === KIND_CUBE
               && (action === ACTION_DOUBLE || action === ACTION_TAKE || action === ACTION_DROP)) {
      blockObj.set(key, _cubePly(d, action, blockLevel, frame));
    } else if (d.kind === KIND_ROLL && action <= 20) {
      // From the roller's side, in the block's currency [7.6] -- a difference
      // of two MWCs, so it converts by the frame's slope alone.
      if (!blockObj.has(key)) blockObj.set(key, { decision: false });
      blockObj.get(key).luck = frame.toDelta(d.luck);
      luckLevels.add(_levelLabel(_resolve(blockLevel, d.level)));
    } else if (d.kind === KIND_RESIGN
               && (action === ACTION_RESIGN_GAME || action === ACTION_RESIGN_MATCH)) {
      blockObj.set(key, _resign(d, frame));
    }
  }

  const info = { ply: blockLevel.checker_ply || 0 };
  if (blockLevel.rollout) info.eval_level = 'rollout';
  info.model_id = _modelId(anal);
  info.timestamp = anal.started_at !== undefined ? Number(anal.started_at / 1000n) : 0;
  if (anal.duration_ms) info.duration_ms = anal.duration_ms;
  if (luckLevels.size === 1) {
    const label = luckLevels.values().next().value;
    if (label !== null) info.luck_eval_level = label;
  }
  return [info, blockObj, luckLevels.size > 0];
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/** Walk the sections [2.2-2.4]. */
function _walkSections(data, fileSize) {
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const sections = [];
  let pos = HEADER_SIZE;
  const end = fileSize - END_MARKER.length;
  while (pos < end) {
    if (pos + SECTION_HEADER_SIZE > end) throw new GvabError(`OGXM v2 section header truncated at ${pos}`);
    const type = String.fromCharCode(...data.subarray(pos, pos + 4));
    const len = view.getUint32(pos + 4, true);
    const flags = data[pos + 8];
    const start = pos + SECTION_HEADER_SIZE;
    if (start + len > end) throw new GvabError(`OGXM v2 section ${type} at ${pos} runs past the end`);
    if (!KNOWN_SECTIONS.has(type) && (flags & 1)) {
      throw new GvabError(`unknown critical OGXM v2 section ${JSON.stringify(type)} -- this reader cannot safely read the file`);
    }
    sections.push({ type, start: pos, payload: data.subarray(start, start + len) });
    pos = start + len;
  }
  return sections;
}

/**
 * Parse an OGXM v2 file into the document `readGvab` returns for v1.
 * Called by `readGvab`, which owns the GvabError contract and the options.
 */
export function _readOgxm2(data, options) {
  const { verifyCrc = true, deriveOgids = true } = options || {};
  if (data.length < HEADER_SIZE + END_MARKER.length) {
    throw new GvabError('input shorter than an OGXM v2 header');
  }
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const rmaj = view.getUint16(8, true);
  const rmin = view.getUint16(10, true);
  const fileSize = view.getUint32(12, true);
  if (rmaj > READER_MAJOR || (rmaj === READER_MAJOR && rmin > READER_MINOR)) {
    throw new GvabError(`file requires an OGXM ${rmaj}.${rmin} reader; this one implements ${READER_MAJOR}.${READER_MINOR}`);
  }
  if (fileSize > MAX_FILE_SIZE || fileSize !== data.length) {
    throw new GvabError(`file_size header (${fileSize}) != actual length (${data.length})`);
  }
  if (String.fromCharCode(...data.subarray(fileSize - 4, fileSize)) !== END_MARKER) {
    throw new GvabError('bad OGXM v2 end marker');
  }

  const sections = _walkSections(data, fileSize);
  // The checksum first, so a damaged file says so rather than failing
  // wherever the damage happens to trip the decoder.
  const csum = sections.find((s) => s.type === 'CSUM');
  if (csum && verifyCrc) _verifyCsum(data, csum.payload, csum.start);

  let mtch = null;
  const games = [];
  const blocks = [];                             // {anal, decisions}
  for (const s of sections) {
    if (s.type === 'MTCH') mtch = _decodeMtch(s.payload);
    else if (s.type === 'GAME') games.push(_decodeGame(s.payload));
    else if (s.type === 'ANAL') blocks.push({ anal: _decodeAnal(s.payload), decisions: [] });
    else if (s.type === 'DECS') {
      if (!blocks.length) throw new GvabError('OGXM v2 DECS with no ANAL before it');
      blocks[blocks.length - 1].decisions = _decodeDecs(s.payload);
    }
  }
  if (mtch === null) {
    // An Analysis-shape file: evaluations of a match it does not contain.
    throw new GvabError('This file holds an analysis without its match, so there is nothing to show.');
  }

  const [ogxm, plyOffset] = _v1Match(mtch, games);
  if (deriveOgids) _deriveOgids(ogxm);

  // ply_ref [I2] is the ply's ordinal across the whole match, in v2's plies --
  // which a synthetic set-position ply is not one of.
  const plyAt = [];
  const plyByKey = new Map();
  ogxm.games.forEach((g, gi) => g.plies.forEach((ply, pi) => {
    const key = `${gi},${pi}`;
    if (pi >= plyOffset[gi]) plyAt.push({ key, ply });
    plyByKey.set(key, ply);
  }));

  const decoded = [];
  const baseBlocks = [];
  for (const { anal, decisions } of blocks) {
    const [info, blockObj, hasLuck] = _v1Block(anal, decisions, plyAt, ogxm.match_length);
    if (blockObj.size) {
      if (deriveOgids) completeBaseBlock(blockObj, plyByKey, info, { convertUnits: false });
      if (!hasLuck) baseBlocks.push(decoded.length);
    }
    decoded.push([info, blockObj]);
  }
  _attachBlocks(ogxm, decoded, plyByKey);
  if (baseBlocks.length) ogxm._base_analyses = baseBlocks;
  return ogxm;
}
