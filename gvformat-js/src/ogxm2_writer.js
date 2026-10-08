// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Writing OGXM v2 -- HedgeHog's current match format -- from our document.
//
// The inverse of `ogxm2.js`'s reader for the documents this package produces:
// `_readOgxm2(write_ogxm2(D))` is `D` again, up to the normalizations listed in
// `docs/OGXM_V2_PROFILE.md`. JavaScript ESM port of gvformat/ogxm2_writer.py --
// keep the two in sync, byte for byte.
//
// Everything is written as plain v2 that any conforming reader loads. What v2
// has no field for travels in `ANNO` records whose keys start `x-` -- the
// namespace the spec reserves for producers outside it (N6), which a conforming
// reader keeps and never interprets:
//
// `x-gammonview-analysis/<analysis_id>` (match scope, one per block)
//     Marks the block as ours and lists the decisions whose PR-counting flag
//     differs from the rule the reader derives it by (`defaultFlags`).
//     It also carries `frame=` when a source normalized by a match equity table
//     that is not ours (BGBlitz's): `<ply_ref>:<mid_white>:<half>` entries, each
//     in force from its ply until the next, stating the MWC frame (`mwc_frame`
//     on the document's analyses) so the file holds the source's own MWCs.
// `x-gammonview-decisions/<analysis_id>` (ply scope)
//     Decision records the block holds for a ply that the `DECS` stream cannot
//     take as they are: everything on an illegal play (v2 allows no decision on
//     the `set position` ply that records one), and a checker decision that
//     breaks a v2 invariant (A1/A3/A5, or an equity loss the played move's
//     equity does not give). Our reader prefers these to `DECS`.
// `x-gammonview-illegal-ply` (ply scope)
//     The steps of an illegal play that our document holds as a dice ply.
// `x-gammonview-site`, `x-gammonview-event` (match scope)
//     A `site` (v2's is a host name) and an over-long `event`.
//
// Every value begins with a format version, `1:`.
//
// Units. A match block is written in v2's `cubeful match` currency -- MWC --
// converted through each ply's own score frame (`mwcFrameInverse`), which is
// how the reader converts it back. A money block is cubeful money, written as
// is.

import { frameKey, framePerspectiveIsWhite, mwcFrameInverse } from './basefill.js';
import { DICE_TABLE } from './constants.js';
import { _round_c, _crc32, _b64encode } from './binary.js';
import { _STARTING_BOARD_P1, _flipBoard, _p1ToAbsolute } from './export.js';
import { isPlayLegal } from './legality.js';
import { _absoluteToP1, _applyMovesP1, _deriveOgids } from './reader.js';
import {
  KIND_CHECKER, KIND_CUBE, KIND_RESIGN, KIND_ROLL,
  CURRENCY_CUBEFUL_MONEY, CURRENCY_CUBEFUL_MATCH,
  GV_FORMAT, GV_KEY_ANALYSIS, GV_KEY_DECISIONS, GV_KEY_ILLEGAL_PLY,
  GV_KEY_SITE, GV_KEY_EVENT, GV_KEY_SCORE,
  _decodeAnal, _decodeDecs, _scoreWalk, _v1Block, frameFromWire, _parseFrames,
} from './ogxm2.js';
import {
  Plan, annoSortKey, cmpSortKeys, b64d,
} from './ogxm2_passthrough.js';

const MAX_STRING = 4096;
const MAX_ALTS = 1024;
const MAX_EVENT = 120;

const SCOPE_MATCH = 0;
const SCOPE_PLY = 2;

const ACTION_DOUBLE = 21;
const ACTION_TAKE = 22;
const ACTION_DROP = 23;
const ACTION_RESIGN_GAME = 27;
const ACTION_RESIGN_MATCH = 28;
const ACTION_SET_POSITION = 31;

const _MARKERS = [24, 25, 26, 30];
const _RESIGNS = [ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH];
const _FORFEIT = 29;

// Characters a label keeps unescaped in an annotation value; `;` and `=`
// delimit its items.
const _SAFE = ' +-_./()';

const _VERDICT = {
  no_double: 0, double: 1, take: 2, pass: 3, too_good: 4, beaver: 5, raccoon: 6,
};
const _OFFER_VERDICTS = [0, 1, 4];
const _RESPONSE_VERDICTS = [2, 3, 5, 6];

// An equity loss that the played move's own equity does not give, past this, is
// a different estimate of the play (not rounding), so the decision is kept
// exactly in the annotation. Two 1e-4 roundings apart is still the same number.
const _LOSS_TOLERANCE = 1.5e-4;

const _enc = new TextEncoder();

// ---------------------------------------------------------------------------
// Python-shaped helpers
// ---------------------------------------------------------------------------

/** `int(v)` for the numbers a document holds. */
function _int(v) {
  return Math.trunc(Number(v));
}

/** `x is None` -> `x ?? null`. */
function _nn(v) {
  return v === undefined ? null : v;
}

function _has(o, k) {
  return Object.prototype.hasOwnProperty.call(o, k);
}

/** Concatenate byte arrays and plain arrays of byte values. */
function _cat(...parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

function _pushAll(out, bytes) {
  for (let i = 0; i < bytes.length; i++) out.push(bytes[i]);
}

function _u16(n) {
  return new Uint8Array([n & 0xFF, (n >>> 8) & 0xFF]);
}

function _u32(n) {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setUint32(0, n, true);
  return b;
}

function _i32(n) {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setInt32(0, n, true);
  return b;
}

/** `urllib.parse.quote(value, safe=_SAFE)`: letters, digits and `_.-~` plus the
 *  safe set pass; every other UTF-8 byte becomes `%XX`, uppercase. */
function _quote(value, safe) {
  let out = '';
  for (const b of _enc.encode(value)) {
    const ch = String.fromCharCode(b);
    if (b < 0x80 && (/[A-Za-z0-9_.\-~]/.test(ch) || safe.includes(ch))) {
      out += ch;
    } else {
      out += `%${b.toString(16).toUpperCase().padStart(2, '0')}`;
    }
  }
  return out;
}

/** `str(uuid.UUID(bytes=b))`. */
function _uuidStr(b) {
  const h = Array.from(b, (v) => v.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/** `uuid.UUID(str(given)).bytes`. */
function _uuidBytes(given) {
  const h = String(given).replace(/^urn:uuid:/i, '').replace(/[{}-]/g, '');
  if (!/^[0-9a-fA-F]{32}$/.test(h)) throw new Error(`badly formed hexadecimal UUID string: ${given}`);
  const out = new Uint8Array(16);
  for (let i = 0; i < 16; i++) out[i] = parseInt(h.slice(2 * i, 2 * i + 2), 16);
  return out;
}

/** `itertools.permutations`, in its order: lexicographic over index positions. */
function* _permutations(items) {
  const n = items.length;
  const used = new Array(n).fill(false);
  const cur = [];
  function* rec() {
    if (cur.length === n) {
      yield cur.map((i) => items[i]);
      return;
    }
    for (let i = 0; i < n; i++) {
      if (used[i]) continue;
      used[i] = true;
      cur.push(i);
      yield* rec();
      cur.pop();
      used[i] = false;
    }
  }
  yield* rec();
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

function _varint(n) {
  if (n < 0) throw new Error(`OGXM v2 varint cannot hold ${n}`);
  const out = [];
  for (;;) {
    const b = n % 128;
    n = Math.floor(n / 128);
    if (n) {
      out.push(b | 0x80);
    } else {
      out.push(b);
      return Uint8Array.from(out);
    }
  }
}

/** A length-prefixed record (3.2): mask, mandatory fields, then the present
 *  optional fields in ascending bit order. */
function _record(mandatory, fields) {
  let mask = 0;
  const body = [mandatory];
  const bits = Object.keys(fields).map(Number).sort((a, b) => a - b);
  for (const bit of bits) {
    mask += 2 ** bit;
    body.push(fields[bit]);
  }
  const inner = _cat(_varint(mask), ...body);
  return _cat(_varint(inner.length), inner);
}

function _str(s) {
  const b = _enc.encode(s);
  if (b.length > MAX_STRING) {
    throw new Error(`string of ${b.length} bytes exceeds OGXM v2's ${MAX_STRING}`);
  }
  return _cat(_varint(b.length), b);
}

function _equity(v) {
  const n = _round_c(Number(v) * 1e6);
  return _i32(Math.max(-0x80000000, Math.min(0x7FFFFFFF, n)));
}

function _loss(v) {
  const n = _round_c(Math.max(0.0, Number(v)) * 1e6);
  return _u32(Math.min(0xFFFFFFFF, n));
}

function _prob(p) {
  p = p < 0.0 ? 0.0 : (p > 1.0 ? 1.0 : p);
  return _u16(_round_c(p * 10000.0));
}

function _probs(ev) {
  return _cat(...['win', 'gammon_win', 'bg_win', 'gammon_loss', 'bg_loss']
    .map((k) => _prob(Number(ev[k] || 0.0))));
}

function _board(b) {
  const out = new Uint8Array(26);
  for (let i = 0; i < 26; i++) {
    const v = _int(b[i]);
    if (!(v >= -128 && v <= 127)) throw new Error(`board value ${v} does not fit a signed byte`);
    out[i] = v & 0xFF;
  }
  return out;
}

function _stepByte(m) {
  const frm = _int(m.from === undefined ? -1 : m.from);
  const pips = _int(m.pips === undefined ? 0 : m.pips);
  if (!(frm >= 0 && frm <= 25 && pips >= 1 && pips <= 6)) return null;
  return (pips << 5) | frm;
}

function _section(stype, payload, critical) {
  return _cat(_enc.encode(stype), _u32(payload.length), [critical ? 1 : 0], payload);
}

/** v1's colour polarity is the Seat enum's reverse: colour 1 is seat 0. */
function _seat(color) {
  return color ? 0 : 1;
}

// ---------------------------------------------------------------------------
// Levels (6.4)
// ---------------------------------------------------------------------------

function _levelBytes(lv) {
  const fields = {};
  if (_has(lv, 'preset')) fields[0] = _str(lv.preset);
  if (_has(lv, 'checker_ply')) fields[1] = _varint(lv.checker_ply);
  if (_has(lv, 'cube_ply')) fields[2] = _varint(lv.cube_ply);
  return _record(new Uint8Array(0), fields);
}

/** The fields of `want` that differ from the inherited level (L1). */
function _override(parent, want) {
  const out = {};
  for (const [k, v] of Object.entries(want)) {
    if (v !== null && v !== undefined && parent[k] !== v) out[k] = v;
  }
  return out;
}

// ---------------------------------------------------------------------------
// The match
// ---------------------------------------------------------------------------

/** `[mine, opp]` in the mover's own numbering, as `legality` wants. */
function _moverFrame(boardP1, moverIsWhite) {
  const mb = moverIsWhite ? boardP1.slice() : _flipBoard(boardP1);
  const mine = new Array(26).fill(0);
  const opp = new Array(26).fill(0);
  for (let p = 1; p < 25; p++) {
    if (mb[p] > 0) mine[p] = mb[p];
    else if (mb[p] < 0) opp[p] = -mb[p];
  }
  mine[25] = Math.max(0, mb[25]);
  return [mine, opp];
}

function _playIsLegal(boardP1, moverIsWhite, action, moves) {
  const n = DICE_TABLE[action][2];
  if (moves.length > n || moves.some((m) => _stepByte(m) === null)) return false;
  const [d1, d2] = DICE_TABLE[action];
  const [mine, opp] = _moverFrame(boardP1, moverIsWhite);
  const pairs = [];
  for (const m of moves) {
    const frm = _int(m.from);
    const src = moverIsWhite ? 25 - frm : frm;
    const dest = src - _int(m.pips);
    pairs.push([src, dest > 0 ? dest : 0]);
  }
  return isPlayLegal(mine, opp, d1, d2, pairs);
}

/** Can the mover (own checkers positive, own bar 25) move one checker from
 *  `src` by `pips` here? */
function _stepOk(mb, src, pips) {
  if (!(src >= 1 && src <= 25) || mb[src] <= 0) return false;
  if (mb[25] > 0 && src !== 25) return false;       // the bar enters first
  const dest = src - pips;
  if (dest >= 1) return mb[dest] >= -1;             // not onto a made point
  for (let p = 7; p < 26; p++) if (mb[p] > 0) return false;   // bear off only from home
  if (dest === 0) return true;
  for (let p = src + 1; p < 7; p++) if (mb[p] > 0) return false;
  return true;
}

/** `moves` in an order whose every intermediate position is legal (M3), keeping
 *  the stored order when it already is. The play itself is unchanged (M5
 *  compares positions), so only the spelling moves. */
function _legalOrder(boardP1, moverIsWhite, moves) {
  const mb0 = moverIsWhite ? boardP1.slice() : _flipBoard(boardP1);

  function replays(order) {
    const mb = mb0.slice();
    for (const m of order) {
      const src = moverIsWhite ? 25 - _int(m.from) : _int(m.from);
      const pips = _int(m.pips);
      if (!_stepOk(mb, src, pips)) return false;
      mb[src] -= 1;
      const dest = src - pips;
      if (dest >= 1) {
        if (mb[dest] === -1) {
          mb[dest] = 0;
          mb[0] += 1;
        }
        mb[dest] += 1;
      }
    }
    return true;
  }

  if (replays(moves)) return moves;
  for (const order of _permutations(moves)) {
    if (replays(order)) return order;
  }
  return moves;
}

function _resignValue(pointsWon, cube) {
  if (cube > 0 && pointsWon % cube === 0
      && Math.floor(pointsWon / cube) >= 1 && Math.floor(pointsWon / cube) <= 3) {
    return Math.floor(pointsWon / cube);
  }
  return 1;
}

/** The match half of the file, and what the analysis half needs from it. */
class _Match {
  constructor(doc, flaggedIllegal) {
    this.doc = doc;
    this.match_length = _int(doc.match_length || 0);
    this.games = [];
    this.ply_at = [];                  // ply_ref order: {key, gi, pi, ply}
    this.ref_of = new Map();           // "gi,pi" -> ply_ref
    this.illegal = new Set();          // ply_refs written as set position + illegal
    this.position_before = new Map();  // ref -> [board_p1, mover is White]
    this.annos = [];                   // [scope, ref, key, value]
    this.pending_double_end = false;
    this.game_start = [];              // leading document plies v2 states as the initial board (0 or 1)
    this.mtch_mandatory = new Uint8Array(0);
    this.mtch_fields = {};
    this._encodeGames(flaggedIllegal);
  }

  _encodeGames(flaggedIllegal) {
    const openingAbs = _p1ToAbsolute(_STARTING_BOARD_P1);
    (this.doc.games || []).forEach((g, gi) => {
      const plies = g.plies || [];
      let board = _STARTING_BOARD_P1.slice();
      const fields = {};
      let start = 0;
      const first = plies.length ? plies[0] : null;
      if (first !== null && first.action_id === ACTION_SET_POSITION && !first.d1) {
        const sp = (first.set_position || new Array(26).fill(0)).map(_int);
        if (!(sp.length === openingAbs.length && sp.every((v, i) => v === openingAbs[i]))) {
          // A leading dice-less set position is a game that starts from a
          // set-up board: v2 states that as the game's initial board. One
          // stating the opening position itself stays a ply, since an initial
          // board equal to the default is never written (3.2).
          start = 1;
          const boardAbs = first.set_position.map(_int);
          board = _absoluteToP1(boardAbs);
          fields[3] = _board(boardAbs);
        }
      }

      this.game_start.push(start);
      const winner = g.winner;
      if (winner === 0 || winner === 1) {
        fields[0] = _varint(winner);
        fields[1] = _varint(Math.max(0, _int(g.points_won || 0)));
      }
      if (g.is_lastgame) fields[2] = new Uint8Array(0);

      const out = [];
      _pushAll(out, _record(new Uint8Array(0), fields));
      let cube = 1;
      let doublePending = false;
      for (let pi = start; pi < plies.length; pi++) {
        const p = plies[pi];
        const key = `${gi},${pi}`;
        const ref = this.ply_at.length;
        this.ref_of.set(key, ref);
        this.ply_at.push({ key, gi, pi, ply: p });
        const raw = p.action_id;
        const action = raw !== null && raw !== undefined ? _int(raw) : 30;
        const color = p.color ? 1 : 0;

        if ((action >= 0 && action <= 20) || (action === ACTION_SET_POSITION && p.d1)) {
          this.position_before.set(ref, [board.slice(), color === 1]);
        }
        if (action >= 0 && action <= 20) {
          const moves = p.moves || [];
          const nxt = pi + 1 < plies.length ? plies[pi + 1].action_id : null;
          const unplayed = !moves.length && (_RESIGNS.includes(nxt) || nxt === _FORFEIT);
          if (!unplayed && (flaggedIllegal.has(key)
                            || !_playIsLegal(board, color === 1, action, moves))) {
            const after = _applyMovesP1(board, moves, color === 1);
            const [d1, d2] = DICE_TABLE[action];
            _pushAll(out, _Match._setPosition(1 - color, [d1, d2], _p1ToAbsolute(after)));
            this.illegal.add(ref);
            this.annos.push([SCOPE_PLY, ref, GV_KEY_ILLEGAL_PLY, GV_FORMAT + moves.map(
              (m) => `${_int(m.from)}/${_int(m.pips)}`).join(',')]);
            board = after;
          } else {
            out.push(action | (_seat(color) << 6));
            const steps = _legalOrder(board, color === 1, moves).map(_stepByte);
            _pushAll(out, steps);
            if (steps.length < DICE_TABLE[action][2]) out.push(0);
            board = _applyMovesP1(board, moves, color === 1);
          }
        } else if (action === ACTION_SET_POSITION) {
          const boardAbs = (p.set_position || new Array(26).fill(0)).map(_int);
          if (p.d1 && p.d2) {
            // An illegal play restated as its board: the side on roll is the
            // mover's opponent (M2).
            _pushAll(out, _Match._setPosition(1 - color, [p.d1, p.d2], boardAbs));
            this.illegal.add(ref);
          } else {
            _pushAll(out, _Match._setPosition(color, null, boardAbs));
          }
          board = _absoluteToP1(boardAbs);
        } else if (_RESIGNS.includes(action)) {
          out.push(action | (_seat(color) << 6) | 0x80);
          _pushAll(out, _record(new Uint8Array(0), {
            1: _varint(_resignValue(_int(g.points_won || 0), cube)),
          }));
          this.pending_double_end = this.pending_double_end || doublePending;
        } else if (_MARKERS.includes(action)) {
          out.push(action);
        } else if (action === ACTION_DOUBLE || action === ACTION_TAKE
                   || action === ACTION_DROP || action === _FORFEIT) {
          out.push(action | (_seat(color) << 6));
          if (action === ACTION_DOUBLE) {
            doublePending = true;
          } else if (action === ACTION_TAKE) {
            doublePending = false;
            cube *= 2;
          } else if (action === ACTION_DROP) {
            doublePending = false;
          } else {
            this.pending_double_end = this.pending_double_end || doublePending;
          }
        } else {
          throw new Error(`game ${gi} ply ${pi}: action ${action} has no OGXM v2 form here`);
        }
      }
      this.games.push(Uint8Array.from(out));
    });
  }

  static _setPosition(color, dice, boardAbs) {
    const fields = { 6: _board(boardAbs) };
    if (dice !== null) {
      fields[0] = Uint8Array.from([_int(dice[0]), _int(dice[1])]);
      fields[3] = new Uint8Array(0);
    }
    return _cat([ACTION_SET_POSITION | (_seat(color) << 6) | 0x80],
      _record(new Uint8Array(0), fields));
  }

  /** The `MTCH` payload, and whether it uses a 2.1 field (4 bits 12-25). */
  mtch() {
    const doc = this.doc;
    const length = this.match_length;
    const fields = {};
    if (doc.player_white) fields[0] = _str(doc.player_white);
    if (doc.player_black) fields[1] = _str(doc.player_black);
    const rules = (doc.crawford ? 1 : 0) | (doc.jacoby ? 2 : 0)
      | (doc.beaver ? 4 : 0) | (doc.raccoon ? 8 : 0);
    if (rules) fields[2] = _varint(rules);
    const cubeLimit = _int(doc.cube_limit || 0);
    if (cubeLimit > 0 && (cubeLimit & (cubeLimit - 1)) === 0) {
      fields[3] = _varint(cubeLimit);        // anything else is a source's "no limit"
    }
    // The score is derived (M8) and the result follows from it; a result is
    // stored only where the score leaves it open.
    const { final: [w, b] } = _scoreWalk({ match_length: length }, (doc.games || []).map((g) => {
      const o = {};
      if (g.winner !== null && g.winner !== undefined && (g.winner === 0 || g.winner === 1)) {
        o.winner = g.winner;
      }
      if (g.points_won !== null && g.points_won !== undefined) o.points_won = g.points_won;
      return o;
    }));
    const derived = length > 0 && w >= length ? 1 : length > 0 && b >= length ? 2 : 0;
    // A score the games do not add up to (a source's header that disagrees with
    // its own games) cannot be stored: score_final is verified (11).
    let stated = [_int(doc.white_score || 0), _int(doc.black_score || 0)];
    if (length > 0) stated = stated.map((s) => Math.min(s, length));
    if (stated[0] !== w || stated[1] !== b) {
      this.annos.push([SCOPE_MATCH, 0, GV_KEY_SCORE, `${GV_FORMAT}${stated[0]},${stated[1]}`]);
    }
    const result = _int(doc.result || 0);
    if (result && result !== derived) fields[6] = _varint(result);
    if (doc.source) fields[7] = _varint(_int(doc.source));
    if (doc.timestamp) fields[8] = _varint(_int(doc.timestamp) * 1000);
    const event = doc.event || '';
    if (event) {
      if (_enc.encode(event).length <= MAX_EVENT) {
        fields[12] = _str(event);
      } else {
        this.annos.push([SCOPE_MATCH, 0, GV_KEY_EVENT, GV_FORMAT + event]);
      }
    }
    if (doc.site) this.annos.push([SCOPE_MATCH, 0, GV_KEY_SITE, GV_FORMAT + doc.site]);
    this.mtch_mandatory = _cat(_varint(length), _varint(0));
    this.mtch_fields = fields;
    const payload = _record(this.mtch_mandatory, fields);
    return [payload, _has(fields, 12)];
  }
}

// ---------------------------------------------------------------------------
// Analysis
// ---------------------------------------------------------------------------

/** `[[analysis_info, select], ...]`, primary first, as `binary` resolves them. */
function _blocks(doc) {
  const infos = doc.analyses_info;
  if (Array.isArray(infos) && infos.length) {
    return infos.map((info, k) => [info || {}, (ply) => {
      for (const a of ply.analyses || []) {
        if (a.analysis_index === k) return a;
      }
      return null;
    }]);
  }
  const hasAny = (doc.games || []).some((g) => (g.plies || []).some(
    (p) => p.analysis !== null && typeof p.analysis === 'object' && !Array.isArray(p.analysis)));
  const info = doc.analysis_info;
  const isDict = info !== null && typeof info === 'object' && !Array.isArray(info);
  if (isDict || hasAny) {
    return [[isDict ? info : {}, (ply) => (ply.analysis === undefined ? null : ply.analysis)]];
  }
  return [];
}

function _analysisId(index, info, matchBytes) {
  const given = info.analysis_id;
  if (given) return _uuidBytes(given);
  // Deterministic, so the same document always writes the same bytes: four
  // CRC32s over the match and the block's identity, stamped as a UUIDv8.
  function part(v) {
    // Spelled out rather than str(), so the JavaScript mirror derives the same
    // id: strings as they are, numbers as integers, nothing as empty.
    if (v === null || v === undefined) return '';
    return typeof v === 'string' ? v : String(_int(v));
  }
  const seed = _cat(
    _enc.encode('gammonview-analysis\0'), _u32(index),
    _enc.encode(['model_id', 'timestamp', 'duration_ms', 'eval_level', 'ply']
      .map((k) => part(info[k])).join('\0')),
    [0], matchBytes);
  const raw = _cat(...[0, 1, 2, 3].map((i) => _u32(_crc32(_cat(seed, [i])))));
  raw[6] = (raw[6] & 0x0F) | 0x80;
  raw[8] = (raw[8] & 0x3F) | 0x80;
  return raw;
}

/** Eight places, trailing zeros dropped (`0.5`, `1`): the form a `frame=`
 *  number takes, spelled so the Python original matches. */
function _fmt8(x) {
  let s = x.toFixed(8);
  s = s.replace(/0+$/, '').replace(/\.$/, '');
  return s === '-0' ? '0' : s;
}

/** `Number(x.toFixed(8))`: Python's `round(x, 8)`. */
function _round8(x) {
  return Number(x.toFixed(8));
}

/** `[mid_white, half]` as `frame=` spells them for this ply's analysis, or
 *  null when it states no usable source frame. */
function _sourceFrame(ply, analysis) {
  const f = analysis.mwc_frame;
  if (!Array.isArray(f) || f.length !== 2 || frameKey(ply) === null) return null;
  let mid = Number(f[0]);
  let half = Number(f[1]);
  if (typeof f[0] === 'boolean' || typeof f[1] === 'boolean') return null;
  if (!(Number.isFinite(mid) && Number.isFinite(half) && half > 0)) return null;
  mid = _round8(mid);
  half = _round8(half);
  return [_fmt8(framePerspectiveIsWhite(ply) ? mid : 1 - mid), _fmt8(half)];
}

/** Normalized equity -> the block's currency, in one ply's frame. */
class _Converter {
  constructor(ply, mwc, source = null) {
    if (mwc) {
      const inv = mwcFrameInverse(
        ply, source === null ? null : frameFromWire(source[0], source[1], framePerspectiveIsWhite(ply)));
      if (inv === null) {
        throw new Error('a match-play decision with no score frame cannot be written as MWC');
      }
      this.eq = inv.fromEquity;
      this.delta = inv.fromDelta;
    } else {
      this.eq = Number;
      this.delta = Number;
    }
  }
}

function _altRecord(alt, lvOverride, played, conv) {
  let steps = (alt.move || []).map(_stepByte);
  if (steps.some((s) => s === null)) {
    steps = [];          // a hop no step byte holds: draw nothing rather than something wrong
  }
  const fields = {};
  if ('eval' in alt) fields[0] = _probs(alt.eval);
  if (Object.keys(lvOverride).length) fields[1] = _levelBytes(lvOverride);
  if (played) fields[2] = new Uint8Array(0);
  return _record(
    _cat(_varint(steps.length), steps, _equity(conv.eq(_altEquity(alt)))), fields);
}

/** `float(alt.get("equity", 0.0))`. */
function _altEquity(alt) {
  return Number(alt.equity === undefined ? 0.0 : alt.equity);
}

/** The position `moves` produce, for comparing two plays (M5). */
function _positionKey(position, moves) {
  if (position === null || position === undefined) return null;
  const [board, moverIsWhite] = position;
  try {
    return _applyMovesP1(board, moves || [], moverIsWhite).join(',');
  } catch (e) {
    return null;
  }
}

/**
 * `[main, exact]`: the record `DECS` can carry (or null), and the exact one for
 * the annotation when that differs (or null). `position` is the board before a
 * legal dice ply and `playedMoves` its play, for A4.
 */
function _checkerRecords(a, ref, blockLevel, conv, unplayed, position = null, playedMoves = null) {
  const alts = (a.alternatives || []).slice(0, MAX_ALTS);
  const best = Number(a.best_equity || 0.0);
  const loss = Number(a.equity_loss || 0.0);
  if (!alts.length && !best && !loss) return [null, null];

  const want = {
    preset: alts.length ? _nn(alts[0].eval_level) : null,
    checker_ply: a.ply || null,
  };
  const decOverride = _override(blockLevel, want);
  const decLevel = { ...blockLevel, ...decOverride };
  const labels = alts.map((alt) => _nn(alt.eval_level || decLevel.preset));
  const played = [];
  alts.forEach((alt, i) => { if (alt.is_played) played.push(i); });

  function build(order, explicit) {
    const fields = {};
    if (order.length) {
      fields[0] = _cat(_varint(order.length), ...order.map((i) => _altRecord(
        alts[i], _override(decLevel, { preset: _nn(alts[i].eval_level) }),
        played.includes(i), conv)));
    }
    if (explicit || !order.length) fields[2] = _equity(conv.eq(best));
    if (explicit || !played.length) fields[3] = _loss(conv.delta(loss));
    if (Object.keys(decOverride).length) fields[4] = _levelBytes(decOverride);
    return _record(_cat(_varint(ref), _varint(KIND_CHECKER)), fields);
  }

  const exact = build(alts.map((_a, i) => i), true);
  if (unplayed || played.length > 1) return [null, exact];
  if (alts.length && Math.abs(best - _altEquity(alts[0])) > 1e-9) return [null, exact];
  if (played.length) {
    const p = played[0];
    if (labels[p] !== labels[0]) return [null, exact];                                   // A5
    if (Math.abs((best - _altEquity(alts[p])) - loss) > _LOSS_TOLERANCE) return [null, exact];
  }

  // A4: the played move, wherever it appears in the list, is the flagged
  // alternative. Another copy of it (a notation split two ways) is dropped from
  // the record v2 holds; the exact list stays in the annotation.
  let keep = alts.map((_a, i) => i);
  const playedKey = position !== null ? _positionKey(position, playedMoves) : null;
  if (playedKey !== null) {
    const same = new Set(keep.filter((i) => _positionKey(position, alts[i].move) === playedKey));
    if (played.length && !same.has(played[0])) return [null, exact];
    keep = keep.filter((i) => !same.has(i) || (played.length && i === played[0]));
  }

  // A1: alternatives of one level adjacent, equity non-increasing within each.
  const order = [];
  const groups = new Map();
  for (const i of keep) {
    if (!groups.has(labels[i])) groups.set(labels[i], []);
    groups.get(labels[i]).push(i);
  }
  for (const idxs of groups.values()) order.push(...idxs);
  const enc = alts.map((alt) => _round_c(conv.eq(_altEquity(alt)) * 1e6));
  for (const idxs of groups.values()) {
    for (let j = 0; j < idxs.length - 1; j++) {
      if (enc[idxs[j + 1]] > enc[idxs[j]]) return [null, exact];
    }
  }
  const identity = order.length === alts.length && order.every((v, i) => v === i);
  if (!identity) {
    if (!order.length || order[0] !== 0) return [null, exact];
    return [build(order, false), exact];
  }
  return [build(order, false), null];
}

function _cubeRecord(sub, ref, verdict, loss, blockLevel, want, conv) {
  const g = (k) => (sub[k] === undefined ? 0.0 : sub[k]);
  const fields = {
    0: _equity(conv.eq(g('no_double_equity'))),
    1: _equity(conv.eq(g('double_take_equity'))),
    2: _equity(conv.eq(g('double_pass_equity'))),
  };
  if ('eval' in sub) fields[3] = _probs(sub.eval);
  if (loss !== null) fields[4] = _loss(conv.delta(loss));
  const lv = _override(blockLevel, want);
  if (Object.keys(lv).length) fields[7] = _levelBytes(lv);
  return _record(_cat(_varint(ref), _varint(KIND_CUBE), _varint(verdict)), fields);
}

const _PLY_LABEL = /^(\d+)ply$/;

/** The level a luck record states. The label itself travels once per block
 *  (`x-gammonview-analysis`); each record states the depth, which is what a
 *  reader outside GammonView can use, and costs a byte where a label costs
 *  five. */
function _luckOverride(blockLevel, label) {
  const m = _PLY_LABEL.exec(label || '');
  if (m) return _override(blockLevel, { checker_ply: parseInt(m[1], 10) });
  return _override(blockLevel, { preset: label });
}

/** The level most of a block's decisions were judged at -- what v2 means by a
 *  block's level (6.4), and what lets most of them state none of their own. */
function _commonLabel(match, select) {
  const counts = new Map();
  for (const { ply: p } of match.ply_at) {
    const a = select(p);
    if (a === null || a === undefined || typeof a !== 'object') continue;
    const alts = a.alternatives || [];
    const labels = [alts.length ? alts[0].eval_level : null, a.eval_level];
    for (const sub of [a.cube_decision, a.missed_double]) {
      if (sub !== null && typeof sub === 'object') labels.push(sub.eval_level);
    }
    for (const lbl of labels) {
      if (lbl) counts.set(lbl, (counts.get(lbl) || 0) + 1);
    }
  }
  // Python's max(counts, key=counts.get): the first key with the largest count.
  let bestLabel = null;
  let bestCount = -1;
  for (const [lbl, n] of counts) {
    if (n > bestCount) {
      bestLabel = lbl;
      bestCount = n;
    }
  }
  return counts.size ? bestLabel : null;
}

/** `[decs, extra, frames]`: the records `DECS` holds, sorted, the per-ply
 *  records only the annotation can hold, and the `frame=` entries for plies
 *  whose source frame is not ours. */
function _blockRecords(match, select, blockLevel, luckLabel) {
  const mwc = match.match_length > 0;
  const decs = [];                      // [ref, kind, bytes]
  const extra = new Map();              // ref -> [[kind, bytes]]
  const frames = [];
  let inForce = null;
  const plies = match.ply_at;
  const addExtra = (ref, kind, rec) => {
    if (!extra.has(ref)) extra.set(ref, []);
    extra.get(ref).push([kind, rec]);
  };
  plies.forEach(({ key, gi, ply: p }, idx) => {
    const a = select(p);
    if (a === null || a === undefined || typeof a !== 'object') return;
    const ref = match.ref_of.get(key);
    const action = _int(p.action_id !== null && p.action_id !== undefined ? p.action_id : 30);
    const source = mwc ? _sourceFrame(p, a) : null;
    if ((source === null ? '' : source.join(':')) !== (inForce === null ? '' : inForce.join(':'))) {
      frames.push(source === null ? `${ref}:` : `${ref}:${source[0]}:${source[1]}`);
      inForce = source;
    }
    const conv = new _Converter(p, mwc, source);
    const illegal = match.illegal.has(ref);
    const recs = [];                    // [kind, bytes, main-capable]

    if ((action >= 0 && action <= 20) || illegal) {
      const nxt = idx + 1 < plies.length ? plies[idx + 1].ply.action_id : null;
      const unplayed = action >= 0 && action <= 20 && !(p.moves || []).length
        && (_RESIGNS.includes(nxt) || nxt === _FORFEIT) && plies[idx + 1].gi === gi;
      const legalDice = action >= 0 && action <= 20 && !illegal;
      const [main, exact] = _checkerRecords(
        a, ref, blockLevel, conv, unplayed,
        legalDice ? (match.position_before.get(ref) || null) : null,
        p.moves || []);
      if (main !== null) recs.push([KIND_CHECKER, main, true]);
      if (exact !== null) addExtra(ref, KIND_CHECKER, exact);
      const md = a.missed_double;
      const cd = a.cube_decision;
      if (md !== null && typeof md === 'object') {
        const verdict = _nn(_VERDICT[md.correct_action || 'double']) ?? 1;
        recs.push([KIND_CUBE, _cubeRecord(
          md, ref, verdict, Number(md.equity_loss || 0.0), blockLevel,
          { preset: _nn(md.eval_level) }, conv), _OFFER_VERDICTS.includes(verdict)]);
      } else if (cd !== null && typeof cd === 'object') {
        recs.push([KIND_CUBE, _cubeRecord(
          cd, ref, cd.should_double ? 1 : 0, null, blockLevel,
          { preset: _nn(cd.eval_level) }, conv), true]);
      }
      if ('luck' in a) {
        const lv = _luckOverride(blockLevel, luckLabel);
        recs.push([KIND_ROLL, _record(
          _cat(_varint(ref), _varint(KIND_ROLL), _equity(conv.delta(a.luck))),
          Object.keys(lv).length ? { 0: _levelBytes(lv) } : {}), true]);
      }
    } else if (action === ACTION_DOUBLE || action === ACTION_TAKE || action === ACTION_DROP) {
      const verdict = _nn(_VERDICT[a.correct_action || 'no_double']) ?? 0;
      const allowed = action === ACTION_DOUBLE ? _OFFER_VERDICTS : _RESPONSE_VERDICTS;
      const loss = Number(a.equity_loss || 0.0);
      recs.push([KIND_CUBE, _cubeRecord(
        a, ref, verdict, loss || null, blockLevel,
        { preset: _nn(a.eval_level), cube_ply: a.ply || null }, conv),
      allowed.includes(verdict)]);
    } else if (_RESIGNS.includes(action)) {
      const fields = {
        1: _equity(conv.delta(a.resign_error || 0.0)),
        2: _equity(conv.delta(a.take_resign_error || 0.0)),
        4: _loss(conv.delta(a.equity_loss || 0.0)),
      };
      if ('eval' in a) fields[3] = _probs(a.eval);
      recs.push([KIND_RESIGN, _record(_cat(_varint(ref), _varint(KIND_RESIGN)), fields), true]);
    }

    for (const [kind, rec, ok] of recs) {
      if (illegal || !ok) addExtra(ref, kind, rec);
      else decs.push([ref, kind, rec]);
    }
  });
  decs.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
  for (const recs of extra.values()) recs.sort((x, y) => x[0] - y[0]);
  return [decs, extra, frames];
}

/** Tokens for every flag where the document disagrees with the reader's
 *  derivation (`defaultFlags` already applied to `decoded`). */
function _flagExceptions(docObjs, decoded) {
  const tokens = [];
  for (const { ref, key, want } of [...docObjs].sort((x, y) => x.ref - y.ref)) {
    const got = decoded.get(key) || {};
    if (Boolean(want.decision) !== Boolean(got.decision)) tokens.push(`${ref}c`);
    const wl = want.cube_decision;
    const gl = got.cube_decision;
    if (wl !== null && typeof wl === 'object' && 'decision' in wl
        && gl !== null && typeof gl === 'object') {
      if (Boolean(wl.decision) !== Boolean(gl.decision)) tokens.push(`${ref}l`);
    }
    if (Boolean(want.illegal_move) !== Boolean(got.illegal_move)) tokens.push(`${ref}i`);
  }
  return tokens;
}

/** Split an ASCII value over `key`, `key~1`, ... to stay under `MAX_STRING`;
 *  the reader concatenates them in suffix order. */
function _chunked(key, value) {
  const room = MAX_STRING - 16;
  if (value.length <= room) return [[key, value]];
  const out = [];
  for (let i = 0, n = 0; i < value.length; i += room, n++) {
    out.push([n === 0 ? key : `${key}~${n}`, value.slice(i, i + room)]);
  }
  return out;
}

function _cmpBytes(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

function _annoRecord(scope, ref, key, value) {
  return _record(_cat(_varint(scope), _varint(ref), _str(value)), { 0: _str(key) });
}

/** One analysis block as the document canonically encodes it. */
class _Block {
  constructor(aid, anal, decs, annos) {
    this.aid = aid;
    this.aid_str = _uuidStr(aid);
    this.anal = anal;
    this.decs = decs;
    this.annos = annos;                // [scope, ref, key, value] this block adds to ANNO
  }

  anno_bytes() {
    const sorted = this.annos.map((t) => ({ t, kb: _enc.encode(t[2]) }));
    sorted.sort((x, y) => x.t[0] - y.t[0] || x.t[1] - y.t[1] || _cmpBytes(x.kb, y.kb));
    return _cat(...sorted.map(({ t }) => _annoRecord(...t)));
  }
}

/** The document, encoded part by part: what `ogxm2_passthrough` fingerprints
 *  and what `_assemble` puts in a file. */
class _Parts {
  constructor(doc, match, mtch, uses21, blocks) {
    this.doc = doc;
    this.match = match;
    this.mtch = mtch;
    this.uses_21 = uses21;
    this.blocks = blocks;
  }
}

export function _encode(ogxm) {
  let doc = ogxm;
  if ((doc.games || []).some((g) => (g.plies || []).some(
    (p) => p.action_id !== null && p.action_id !== undefined
      && !_MARKERS.includes(p.action_id) && !p.ogid_before))) {
    doc = structuredClone(ogxm);
    _deriveOgids(doc);
  }

  const blocks = _blocks(doc);
  const flagged = new Set();
  (doc.games || []).forEach((g, gi) => (g.plies || []).forEach((p, pi) => {
    for (const [, select] of blocks) {
      const a = select(p);
      if (a !== null && a !== undefined && typeof a === 'object' && a.illegal_move) {
        flagged.add(`${gi},${pi}`);
        break;
      }
    }
  }));
  const match = new _Match(doc, flagged);
  const [mtch, uses21] = match.mtch();
  const matchBytes = _cat(mtch, ...match.games);

  const out = [];
  const usedIds = new Set();
  blocks.forEach(([info, select], k) => {
    const aid = _analysisId(k, info, matchBytes);
    const aidStr = _uuidStr(aid);
    if (usedIds.has(aidStr)) throw new Error(`analysis block ${k} repeats an analysis_id`);
    usedIds.add(aidStr);
    const blockLevel = {};
    const common = _commonLabel(match, select) || info.eval_level;
    if (common) blockLevel.preset = common;
    if (info.ply) blockLevel.checker_ply = _int(info.ply);
    const luckLabel = info.luck_eval_level || '1ply';
    const [decs, extra, frames] = _blockRecords(match, select, blockLevel, luckLabel);

    const analFields = {};
    if (Object.keys(blockLevel).length) analFields[2] = _levelBytes(blockLevel);
    if (info.model_id) analFields[5] = _str(info.model_id);
    analFields[9] = _varint(match.match_length > 0 ? CURRENCY_CUBEFUL_MATCH : CURRENCY_CUBEFUL_MONEY);
    const metId = info.met_id || (match.match_length > 0 ? 'kazaross-xg2' : null);
    if (metId) analFields[11] = _str(metId);
    if (info.timestamp) analFields[14] = _varint(_int(info.timestamp) * 1000);
    if (info.duration_ms) analFields[16] = _varint(_int(info.duration_ms));
    const anal = _record(aid, analFields);
    const decsPayload = _cat(...decs.map(([, , rec]) => rec));

    // The flags v2 has no field for: derive them the way the reader will, from
    // the records just written, and keep only where the document disagrees.
    const analDec = _decodeAnal(anal);
    const extraDec = new Map();
    for (const [ref, recs] of extra) extraDec.set(ref, _decodeDecs(_cat(...recs.map((r) => r[1]))));
    const [, decoded] = _v1Block(
      analDec, _decodeDecs(decsPayload), match.ply_at, match.match_length,
      { extra: extraDec, exceptions: new Set(), illegal: match.illegal,
        frames: _parseFrames(frames.join(',')) });
    const want = [];
    for (const { key, ply: p } of match.ply_at) {
      const s = select(p);
      if (s !== null && s !== undefined && typeof s === 'object') {
        want.push({ ref: match.ref_of.get(key), key, want: s });
      }
    }
    const tokens = _flagExceptions(want, decoded);

    const items = [];
    if (_nn(info.eval_level) !== _nn(blockLevel.preset)) {
      items.push(`level=${_quote(info.eval_level || '', _SAFE)}`);
    }
    if (decs.some(([, kind]) => kind === KIND_ROLL)
        || [...extra.values()].some((recs) => recs.some(([kind]) => kind === KIND_ROLL))) {
      items.push(`luck=${_quote(luckLabel, _SAFE)}`);
    }
    items.push(`pr=${tokens.join(',')}`);
    if (frames.length) items.push(`frame=${frames.join(',')}`);
    const annos = [];
    for (const [key, value] of _chunked(GV_KEY_ANALYSIS + aidStr, GV_FORMAT + items.join(';'))) {
      annos.push([SCOPE_MATCH, 0, key, value]);
    }
    for (const [ref, recs] of extra) {
      const value = GV_FORMAT + _b64encode(_cat(...recs.map((r) => r[1])));
      for (const [key, part] of _chunked(GV_KEY_DECISIONS + aidStr, value)) {
        annos.push([SCOPE_PLY, ref, key, part]);
      }
    }
    out.push(new _Block(aid, anal, decsPayload, annos));
  });
  return new _Parts(doc, match, mtch, uses21, out);
}

/** The file: the plan's parts in the order 2.4 requires, and `CSUM`. */
export function _assemble(parts, plan) {
  const match = parts.match;
  const secs = [];                              // [anchor key, section]
  secs.push(['MTCH', _section('MTCH', plan.mtch, true)]);
  plan.games.forEach((g, i) => secs.push([`GAME:${i}`, _section('GAME', g, true)]));
  parts.blocks.forEach((b, k) => {
    const [anal, decs, sign] = plan.blocks[k];
    const key = `BLOCK:${b.aid_str}`;
    secs.push([key, _section('ANAL', anal, false)]);
    secs.push([key, _section('DECS', decs, false)]);
    if (sign !== null) secs.push([key, _section('SIGN', sign, false)]);
  });
  if (plan.clck !== null) secs.push(['CLCK', _section('CLCK', plan.clck, false)]);
  if (plan.vido !== null) secs.push(['VIDO', _section('VIDO', plan.vido, false)]);

  const entries = [];
  for (const r of plan.foreign_annos) {
    entries.push([annoSortKey(r.scope, r.ref, r.analysis, r.kind, r.alt, r.key, r.lang, entries.length),
      b64d(r.raw)]);
  }
  const ours = [...match.annos];
  for (const blk of plan.blocks) ours.push(...blk[3]);
  for (const [scope, ref, key, value] of ours) {
    if (plan.skip_site && key === GV_KEY_SITE && scope === SCOPE_MATCH) continue;
    entries.push([annoSortKey(scope, ref, null, null, null, key, null, entries.length),
      _annoRecord(scope, ref, key, value)]);
  }
  if (entries.length) {
    entries.sort((x, y) => cmpSortKeys(x[0], y[0]));
    secs.push(['ANNO', _section('ANNO', _cat(...entries.map((e) => e[1])), false)]);
  }
  const nOther = secs.length;
  plan.msig.forEach((m, i) => secs.push([`MSIG:${i}`, _section('MSIG', m, false)]));

  // Unknown sections go back after the section they followed; where that one is
  // gone, at the end of the body (before the signatures).
  let bodySections;
  if (plan.unknown.length) {
    const keys = secs.map((s) => s[0]);
    const nGames = plan.games.length;
    const inserts = new Map();
    for (const [stype, payload, after] of plan.unknown) {
      const k = after.k;
      let want = k === 'head' ? 'head' : (k === 'GAME' || k === 'MSIG') ? `${k}:${after.i}`
        : k === 'BLOCK' ? `BLOCK:${after.id}` : k;
      if (k === 'GAME' && after.i >= nGames) want = nGames ? `GAME:${nGames - 1}` : 'MTCH';
      let at;
      if (want === 'head') at = 0;
      else if (keys.includes(want)) at = keys.lastIndexOf(want) + 1;
      else at = nOther;
      if (!inserts.has(at)) inserts.set(at, []);
      inserts.get(at).push(_section(stype, payload, false));
    }
    bodySections = [];
    for (let i = 0; i <= secs.length; i++) {
      if (inserts.has(i)) bodySections.push(...inserts.get(i));
      if (i < secs.length) bodySections.push(secs[i][1]);
    }
  } else {
    bodySections = secs.map((s) => s[1]);
  }

  const minor = Math.max(match.pending_double_end ? 2 : (parts.uses_21 ? 1 : 0), plan.minor_floor);
  const minMinor = Math.max(match.pending_double_end ? 2 : 0, plan.min_minor_floor);
  const head = _cat(_enc.encode('OGXM'), _u16(2), _u16(minor), _u16(2), _u16(minMinor), _u32(0));
  const body = _cat(head, ...bodySections);
  const csumAt = body.length;
  const csumPayloadLen = _record(_cat(_varint(0), _varint(4), [0, 0, 0, 0]), {}).length;
  const total = csumAt + 9 + csumPayloadLen + 4;
  new DataView(body.buffer, body.byteOffset).setUint32(12, total, true);
  const crc = _crc32(body);
  const out = _cat(body,
    _section('CSUM', _record(_cat(_varint(0), _varint(4), _u32(crc)), {}), true),
    _enc.encode('END!'));
  if (out.length !== total) throw new Error('OGXM v2 writer: file size mismatch');
  return out;
}

/** Serialize our document to OGXM v2 bytes. Pure JS, no engine.
 *
 *  A document read from another producer's v2 file carries
 *  `_ogxm2_passthrough`, and what it did not edit is written back as it came
 *  (`ogxm2_passthrough`). */
export function write_ogxm2(ogxm) {
  const parts = _encode(ogxm);
  return _assemble(parts, new Plan(parts, parts.doc));
}

export { _varint, _record, _str, _uuidBytes, _uuidStr, _cmpBytes };
