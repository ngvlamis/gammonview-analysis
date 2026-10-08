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
//     A `site` that is neither the city nor the platform (v2's own `site` is a
//     host name), and an over-long `event`.
// `x-gammonview-<field>` (match scope), `x-gammonview-<side>_profile.<field>`,
// and `x-gammonview-<field>` at game scope
//     A value v2 cannot hold (a string past its cap or outside its byte set, a
//     year without an event, a rating that is not a hundredth ...): the reader
//     puts it back, so nothing a document says is lost.
//
// Every value begins with a format version, `1:`.
//
// Units. A match block is written in v2's `cubeful match` currency -- MWC --
// converted through each ply's own score frame (`mwcFrameInverse`), which is
// how the reader converts it back. A money block is cubeful money, written as
// is.

import { frameKey, framePerspectiveIsWhite, mwcFrameInverse } from './basefill.js';
import { DICE_TABLE } from './constants.js';
import { _round_c, _crc32, _b64encode, _b64decode } from './binary.js';
import {
  _STARTING_BOARD_P1, _flipBoard, _p1ToAbsolute, variantOpeningAbs, variantOpeningP1,
} from './export.js';
import { isPlayLegal } from './legality.js';
import { _absoluteToP1, _applyMovesP1, _deriveOgids } from './reader.js';
import {
  KIND_CHECKER, KIND_CUBE, KIND_RESIGN, KIND_ROLL,
  CURRENCY_CUBEFUL_MONEY, CURRENCY_CUBEFUL_MATCH,
  GV_FORMAT, GV_KEY_ANALYSIS, GV_KEY_DECISIONS, GV_KEY_ILLEGAL_PLY,
  GV_KEY_SITE, GV_KEY_EVENT, GV_KEY_SCORE, GV_KEY_ANNOTATIONS, GV_KEY_VIDEO_URL, V2_FIELD_NAMES,
  GV_PREFIX, SCOPE_GAME, SCOPE_DECISION, SCOPE_ALTERNATIVE, naturalKind, decisionHolder, _gvText,
  ACTION_BEAVER, ACTION_RACCOON, ACTION_SETTLE, ACTION_CUBE_SET, ACTION_PASS, ACTION_ESCAPE,
  LAST_KNOWN_ACTION, derivedResignValue,
  _decodeAnal, _decodeDecs, _scoreWalk, _v1Block, frameFromWire, _parseFrames,
  _resolve, _stable, tierLevel, blockLevelOf, luckLabels, defaultCurrency,
  numberText, CUBE_ACTIONS, CHECKER_KEYS, DIAL_FIELDS, GV_DIAL_FIELDS,
} from './ogxm2.js';
import {
  Plan, annoSortKey, cmpSortKeys, b64d, encodeClock, encodeVideo, urlStorable, CLOCK_PRECISION,
  MAX_VIDEO_URL,
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

const _ROLLOUT_UINTS = ['trials', 'truncation_depth', 'move_ply', 'variance_reduction', 'budget_ms',
  'match_policy'];

/** A `level` record from an override (`_levelDiff`'s shape). */
function _levelBytes(lv) {
  const fields = {};
  if (_has(lv, 'preset')) fields[0] = _str(lv.preset);
  if (_has(lv, 'checker_ply')) fields[1] = _varint(lv.checker_ply);
  if (_has(lv, 'cube_ply')) fields[2] = _varint(lv.cube_ply);
  if (_has(lv, 'rollout')) {
    const r = lv.rollout;
    const rf = {};
    ['trials', 'truncation_depth', 'move_ply', 'variance_reduction', 'seed', 'budget_ms',
      'match_policy'].forEach((key, bit) => {
      if (!_has(r, key)) return;
      if (key === 'seed') {
        const b = new Uint8Array(8);
        new DataView(b.buffer).setBigUint64(0, BigInt(r[key]), true);
        rf[bit] = b;
      } else {
        rf[bit] = _varint(r[key]);
      }
    });
    fields[3] = _record(new Uint8Array(0), rf);
  }
  if (lv.no_rollout) fields[4] = new Uint8Array(0);
  return _record(new Uint8Array(0), fields);
}

/** A level the document states, checked against what v2 can hold and put in the
 *  form the rest of this module compares: no empty strings, no absent fields, a
 *  seed as a decimal string. A level v2 cannot hold is an error, not a quiet
 *  change (P6). */
function _checkLevel(lv) {
  if (lv === null || typeof lv !== 'object' || Array.isArray(lv)) {
    throw new Error(`a level must be an object, not ${JSON.stringify(lv)}`);
  }
  const out = {};
  const preset = lv.preset;
  if (preset !== undefined && preset !== null && preset !== '') {
    if (typeof preset !== 'string' || _nbytes(preset) > MAX_STRING) {
      throw new Error(`a level's preset ${JSON.stringify(preset)} is not a string v2 can hold`);
    }
    out.preset = preset;
  }
  for (const key of ['checker_ply', 'cube_ply']) {
    const v = lv[key];
    if (v !== undefined && v !== null) {
      if (!_isUint(v)) throw new Error(`a level's ${key} ${JSON.stringify(v)} is not a count`);
      out[key] = v;
    }
  }
  const r = lv.rollout;
  if (r !== undefined && r !== null && !(typeof r === 'object' && !Object.keys(r).length)) {
    if (typeof r !== 'object' || Array.isArray(r)) {
      throw new Error(`a level's rollout must be an object, not ${JSON.stringify(r)}`);
    }
    const ro = {};
    for (const key of _ROLLOUT_UINTS) {
      const v = r[key];
      if (v !== undefined && v !== null) {
        if (!_isUint(v) || (key === 'trials' && v < 1)) {
          throw new Error(`a rollout's ${key} ${JSON.stringify(v)} is not a count v2 can hold`);
        }
        ro[key] = v;
      }
    }
    const seed = r.seed;
    if (seed !== undefined && seed !== null) {
      const ok = (typeof seed === 'number' && Number.isInteger(seed) && seed >= 0)
        || (typeof seed === 'string' && /^[0-9]+$/.test(seed));
      if (!ok || BigInt(seed) >= (1n << 64n)) {
        throw new Error(`a rollout's seed ${JSON.stringify(seed)} is not a 64-bit count`);
      }
      ro.seed = String(BigInt(seed));
    }
    if (Object.keys(ro).length) out.rollout = ro;
  }
  return out;
}

/** What a tier must state to have the level `want` under `parent` (L1, L2): the
 *  fields that differ, a rollout's only as far as it differs, and `no_rollout`
 *  where the tier above has one and this has none. */
function _levelDiff(parent, want) {
  const out = {};
  for (const key of ['preset', 'checker_ply', 'cube_ply']) {
    if (_has(want, key) && want[key] !== parent[key]) out[key] = want[key];
  }
  const rollout = want.rollout;
  const above = parent.rollout;
  if (rollout === undefined) {
    if (above !== undefined) out.no_rollout = true;
  } else {
    const diff = {};
    for (const [k, v] of Object.entries(rollout)) {
      if ((above || {})[k] !== v) diff[k] = v;
    }
    if (Object.keys(diff).length) out.rollout = diff;
  }
  return out;
}

/** `[override, effective]` for one tier: the level the document states, if it
 *  does, else the one its labels give. A level the document states is the tier's
 *  whole level -- except that a tier cannot clear a field the tier above has,
 *  only its rollout (J9), so a field it leaves out is inherited. */
function _tier(parent, explicit, labels = {}, follow = true) {
  let want;
  if (explicit !== null && explicit !== undefined) {
    const stated = _checkLevel(explicit);
    want = {};
    for (const [k, v] of Object.entries(parent)) if (k !== 'rollout') want[k] = v;
    for (const [k, v] of Object.entries(stated)) if (k !== 'rollout') want[k] = v;
    if (_has(stated, 'rollout')) want.rollout = stated.rollout;
  } else {
    want = tierLevel(parent, { ...labels, follow });
    // A label that only restates the depth, under a tier that names no preset,
    // says nothing: reading labels a foreign tier by its depth, and writing that
    // back must not turn the depth into an override.
    const { preset } = want;
    if (preset && !_has(parent, 'preset') && /^\d+ply$/.test(preset)
        && want.checker_ply === parseInt(preset, 10)) {
      delete want.preset;
    }
  }
  const override = _levelDiff(parent, want);
  return [override, _resolve(parent, override)];
}

/** A level as something comparable (A1 groups by it). */
function _levelKey(level) {
  return _stable(level);
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

const _resignValue = derivedResignValue;

// What v2 holds in a match-context string (4): its cap in bytes and the byte set.
const _HOST = /^[a-z0-9.-]+$/;
const _MATCH_REF = /^[A-Za-z0-9._-]+$/;
const _RATING_SYSTEM = /^[a-z0-9-]+$/;
const _COUNTRY = /^[A-Z]{2}$/;
const _DAY_MS = 86_400_000;

function _isUint(v, bits = 32) {
  return typeof v === 'number' && Number.isInteger(v) && v >= 0
    && (bits >= 53 ? v <= Number.MAX_SAFE_INTEGER : v < 2 ** bits);
}

function _isPow2(v) {
  return _isUint(v) && v >= 1 && (v & (v - 1)) === 0;
}

function _nbytes(v) {
  return typeof v === 'string' ? _enc.encode(v).length : -1;
}

/** A non-empty string within `cap` bytes (and, if given, of the byte set
 *  `pattern` -- ASCII, so characters are bytes). */
function _strOk(v, cap, pattern = null) {
  const n = _nbytes(v);
  if (!(n >= 1 && n <= cap)) return false;
  return pattern === null || pattern.test(v);
}

function _urlOk(v) {
  if (typeof v !== 'string' || !v.startsWith('https://') || v.length > 512) return false;
  for (let i = 0; i < v.length; i++) {
    const c = v.charCodeAt(i);
    if (c < 0x20 || c > 0x7E) return false;
  }
  return true;
}

/** Whether `ms` is the first instant (UTC) of the day, month or year
 *  `precision` names (9.27); an unknown precision is not checked (I7). */
function _onBoundary(ms, precision) {
  if (ms % _DAY_MS) return false;
  if (precision !== 1 && precision !== 2) return true;
  const d = new Date(ms);
  return d.getUTCDate() === 1 && (precision === 1 || d.getUTCMonth() === 0);
}

/** A rating as the hundredths v2 stores, or null where it is not exactly that
 *  (not a 0.01 step, or out of range). */
function _ratingHundredths(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return null;
  const n = Math.round(v * 100);
  return n >= 0 && n <= 0xFFFFFFFF && n / 100 === v ? n : null;
}

/** How many leading plies of `g` v2 states as the game's initial board (0 or
 *  1). A leading dice-less set position is a game that starts from a set-up
 *  board; one stating the opening position itself stays a ply, since an initial
 *  board equal to the default is never written (3.2). */
function _gameStart(g, openingAbs) {
  const plies = g.plies || [];
  const first = plies.length ? plies[0] : null;
  if (first === null || first.action_id !== ACTION_SET_POSITION || first.d1) return 0;
  const sp = (first.set_position || new Array(26).fill(0)).map(_int);
  return sp.length === openingAbs.length && sp.every((v, i) => v === openingAbs[i]) ? 0 : 1;
}

/** `{keys, starts}`: the `[game, ply]` of every ply v2 holds, in `ply_ref`
 *  order, and each game's leading set-up position (0 or 1) that it does not. */
export function plyLayout(doc) {
  const openingAbs = variantOpeningAbs(_int(doc.variant || 0)) || _p1ToAbsolute(_STARTING_BOARD_P1);
  const keys = [];
  const starts = [];
  (doc.games || []).forEach((g, gi) => {
    const st = _gameStart(g, openingAbs);
    starts.push(st);
    for (let pi = st; pi < (g.plies || []).length; pi++) keys.push([gi, pi]);
  });
  return { keys, starts };
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
    this.cube_limit = _isPow2(doc.cube_limit) ? doc.cube_limit : 0;
    this._encodeGames(flaggedIllegal);
  }

  /** Carry a value v2 cannot hold in an annotation of ours (P6). */
  _gv(scope, ref, name, kind, value) {
    this.annos.push([scope, ref, GV_PREFIX + name, GV_FORMAT + _gvText(kind, value)]);
  }

  /** Bits 4-7 of a `GAME` record (the game's cube and how it ended); returns
   *  the cube the game opens with (5.3). */
  _gameFields(gi, g, fields) {
    let value = 1;
    let doubles = 0;
    const v = g.initial_cube_value;
    if (v !== undefined && v !== null && v !== 1) {
      if (_isPow2(v) && (!this.cube_limit || v <= this.cube_limit)) {
        fields[4] = _varint(v);
        value = v;
      } else {
        this._gv(SCOPE_GAME, gi, 'initial_cube_value', 'i', v);
      }
    }
    const owner = g.initial_cube_owner;
    if (owner !== undefined && owner !== null && owner !== 2) {
      if (owner === 0 || owner === 1) fields[5] = _varint(owner);
      else this._gv(SCOPE_GAME, gi, 'initial_cube_owner', 'i', owner);
    }
    const a = g.auto_doubles;
    if (a !== undefined && a !== null && a !== 0) {
      if (_isUint(a)) {
        fields[6] = _varint(a);
        doubles = a;
      } else {
        this._gv(SCOPE_GAME, gi, 'auto_doubles', 'i', a);
      }
    }
    const t = g.termination;
    if (t !== undefined && t !== null) {
      if (_isUint(t)) fields[7] = _varint(t);
      else this._gv(SCOPE_GAME, gi, 'termination', 'i', t);
    }
    return value * 2 ** doubles;
  }

  _encodeGames(flaggedIllegal) {
    const variant = _int(this.doc.variant || 0);
    const openingAbs = variantOpeningAbs(variant) || _p1ToAbsolute(_STARTING_BOARD_P1);
    const openingP1 = variantOpeningP1(variant) || _STARTING_BOARD_P1.slice();
    (this.doc.games || []).forEach((g, gi) => {
      const plies = g.plies || [];
      let board = openingP1.slice();
      const fields = {};
      const start = _gameStart(g, openingAbs);
      if (start) {
        const boardAbs = plies[0].set_position.map(_int);
        board = _absoluteToP1(boardAbs);
        fields[3] = _board(boardAbs);
      }

      this.game_start.push(start);
      const winner = g.winner;
      if (winner === 0 || winner === 1) {
        fields[0] = _varint(winner);
        fields[1] = _varint(Math.max(0, _int(g.points_won || 0)));
      }
      if (g.is_lastgame) fields[2] = new Uint8Array(0);
      let cube = this._gameFields(gi, g, fields);

      const out = [];
      _pushAll(out, _record(new Uint8Array(0), fields));
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
          const rv = _isUint(p.resign_value) ? p.resign_value
            : _resignValue(_int(g.points_won || 0), cube);
          _pushAll(out, _record(new Uint8Array(0), { 1: _varint(rv) }));
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
        } else if (action === ACTION_BEAVER || action === ACTION_RACCOON || action === ACTION_PASS) {
          out.push(action | (_seat(color) << 6));
          if (action !== ACTION_PASS) {
            doublePending = false;
            cube *= action === ACTION_BEAVER ? 4 : 2;
          }
        } else if (action === ACTION_SETTLE) {
          out.push(action | (_seat(color) << 6) | 0x80);
          _pushAll(out, _record(new Uint8Array(0), { 4: _equity(p.settle_value || 0.0) }));
        } else if (action === ACTION_CUBE_SET) {
          const value = p.cube_value;
          if (!_isPow2(value)) throw new Error(`game ${gi} ply ${pi}: a cube set to ${value}`);
          const f = { 2: _varint(value) };
          if (p.cube_owner === 0 || p.cube_owner === 1) f[8] = _varint(p.cube_owner);
          out.push(action | (_seat(color) << 6) | 0x80);
          _pushAll(out, _record(new Uint8Array(0), f));
          cube = value;
        } else if (action > LAST_KNOWN_ACTION && action !== ACTION_ESCAPE) {
          // An action id nothing here assigns a meaning to: its extras are the
          // producer's, kept as they came.
          let extras = p.extras_raw ? _b64decode(p.extras_raw) : new Uint8Array(0);
          if (action >= 64 && !extras.length) {
            extras = _record(new Uint8Array(0), { 7: _varint(action) });
          }
          out.push((action < 64 ? action : ACTION_ESCAPE)
            | (_seat(color) << 6) | (extras.length ? 0x80 : 0));
          _pushAll(out, extras);
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

  /** Store the string `key` at `bit` when v2 can hold it, else in an
   *  annotation; returns whether it is in the record. */
  _putStr(fields, bit, key, ok) {
    const v = this.doc[key];
    if (v === undefined || v === null || v === '') return false;
    if (ok) {
      fields[bit] = _str(v);
      return true;
    }
    this._gv(SCOPE_MATCH, 0, key, 's', v);
    return false;
  }

  /** A `player` record (4.1) from `<side>_profile`; what v2 cannot hold goes in
   *  `x-gammonview-<side>_profile.<field>`. Returns the written `kind` (for
   *  `player_seat`'s check). */
  _profile(side, bit, fields, hasPlatform) {
    const name = `${side}_profile`;
    const pr = this.doc[name];
    if (pr === null || typeof pr !== 'object' || Array.isArray(pr) || !Object.keys(pr).length) {
      return null;
    }
    const out = {};
    const carry = (field, kind) => {
      this.annos.push([SCOPE_MATCH, 0, `${GV_PREFIX}${name}.${field}`,
        GV_FORMAT + _gvText(kind, pr[field])]);
    };
    const uid = pr.user_id;
    if (uid !== undefined && uid !== null && uid !== '') {
      if (hasPlatform && _strOk(uid, 64)) out[0] = _str(uid);
      else carry('user_id', 's');
    }
    const rating = pr.rating === undefined ? null : pr.rating;
    const system = pr.rating_system === undefined ? null : pr.rating_system;
    if (rating !== null || system !== null) {
      const n = rating !== null ? _ratingHundredths(rating) : null;
      if (n !== null && system !== null && _strOk(system, 32, _RATING_SYSTEM)) {
        out[1] = _varint(n);
        out[2] = _str(system);
      } else {
        // The two stand or fall together: a rating without its system (or the
        // reverse) is not a v2 record.
        if (rating !== null) carry('rating', 'f');
        if (system !== null) carry('rating_system', 's');
      }
    }
    const country = pr.country;
    if (country !== undefined && country !== null && country !== '') {
      if (typeof country === 'string' && _COUNTRY.test(country)) out[3] = _str(country);
      else carry('country', 's');
    }
    const kind = pr.kind;
    let writtenKind = null;
    if (kind !== undefined && kind !== null) {
      if (_isUint(kind)) {
        out[4] = _varint(kind);
        writtenKind = kind;
      } else {
        carry('kind', 'i');
      }
    }
    if (Object.keys(out).length) fields[bit] = _record(new Uint8Array(0), out);
    return writtenKind;
  }

  /** The `MTCH` payload, and whether it uses a 2.1 field (4 bits 12-25). */
  mtch() {
    const doc = this.doc;
    const length = this.match_length;
    const fields = {};
    if (doc.player_white) fields[0] = _str(doc.player_white);
    if (doc.player_black) fields[1] = _str(doc.player_black);
    let rules = (doc.crawford ? 1 : 0) | (doc.jacoby ? 2 : 0)
      | (doc.beaver ? 4 : 0) | (doc.raccoon ? 8 : 0) | (doc.auto_doubles ? 16 : 0);
    const other = doc.rules_other;
    if (other) {
      if (_isUint(other) && !(other & 0x1F)) rules = (rules | other) >>> 0;
      else this._gv(SCOPE_MATCH, 0, 'rules_other', 'i', other);
    }
    if (rules) fields[2] = _varint(rules);
    const cubeLimit = _int(doc.cube_limit || 0);
    if (cubeLimit > 0 && (cubeLimit & (cubeLimit - 1)) === 0) {
      fields[3] = _varint(cubeLimit);        // anything else is a source's "no limit"
    }
    const ss = doc.score_start || [0, 0];
    const start = [_int(ss[0] || 0), _int(ss[1] || 0)];
    if (start[0] !== 0 || start[1] !== 0) fields[4] = _cat(_varint(start[0]), _varint(start[1]));
    // The score is derived (M8) and the result follows from it; a result is
    // stored only where the score leaves it open.
    const { final: [w, b] } = _scoreWalk({ match_length: length, score_start: start },
      (doc.games || []).map((g) => {
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
    const started = doc.timestamp ? _int(doc.timestamp) * 1000 : 0;
    if (started) fields[8] = _varint(started);
    const completed = doc.completed_at;
    if (completed !== undefined && completed !== null) {
      if (_isUint(completed, 64)) fields[9] = _varint(completed);
      else this._gv(SCOPE_MATCH, 0, 'completed_at', 'i', completed);
    }
    const event = doc.event || '';
    let hasEvent = false;
    if (event) {
      if (_enc.encode(event).length <= MAX_EVENT) {
        fields[12] = _str(event);
        hasEvent = true;
      } else {
        this.annos.push([SCOPE_MATCH, 0, GV_KEY_EVENT, GV_FORMAT + event]);
      }
    }
    const year = doc.event_year;
    if (year !== undefined && year !== null) {
      if (_isUint(year) && hasEvent) fields[13] = _varint(year);
      else this._gv(SCOPE_MATCH, 0, 'event_year', 'i', year);
    }
    const precision = doc.date_precision;
    if (precision !== undefined && precision !== null) {
      if (_isUint(precision) && started && _onBoundary(started, precision)) {
        fields[14] = _varint(precision);
      } else {
        this._gv(SCOPE_MATCH, 0, 'date_precision', 'i', precision);
      }
    }
    this._putStr(fields, 15, 'stage', _strOk(doc.stage, 60));
    const rnd = doc.round;
    if (rnd !== undefined && rnd !== null) {
      if (_isUint(rnd) && rnd >= 1 && rnd <= 99) fields[16] = _varint(rnd);
      else this._gv(SCOPE_MATCH, 0, 'round', 'i', rnd);
    }
    this._putStr(fields, 17, 'table', _strOk(doc.table, 24));
    this._putStr(fields, 18, 'city', _strOk(doc.city, 80));
    this._putStr(fields, 19, 'country', typeof doc.country === 'string' && _COUNTRY.test(doc.country));
    this._putStr(fields, 20, 'event_url', _urlOk(doc.event_url));
    const hasPlatform = this._putStr(fields, 21, 'platform', _strOk(doc.platform, 253, _HOST));
    const ref = doc.match_ref;
    this._putStr(fields, 22, 'match_ref',
      hasPlatform && _strOk(ref, 64, _MATCH_REF) && ref !== '.' && ref !== '..');
    const kinds = {
      white: this._profile('white', 23, fields, hasPlatform),
      black: this._profile('black', 24, fields, hasPlatform),
    };
    const seat = doc.player_seat;
    if (seat !== undefined && seat !== null) {
      const human = seat !== 0 && seat !== 1
        || kinds[seat === 0 ? 'white' : 'black'] === null || kinds[seat === 0 ? 'white' : 'black'] === 0;
      if (_isUint(seat) && human) fields[10] = _varint(seat);
      else this._gv(SCOPE_MATCH, 0, 'player_seat', 'i', seat);
    }
    if (doc.crawford_before_start) fields[11] = new Uint8Array(0);
    if (doc.rated) fields[25] = new Uint8Array(0);
    // Our `site` is where the match was played, free text; v2 states the same
    // in `city` (or names the platform). Only a `site` that is neither needs
    // its own record.
    const site = doc.site;
    if (site && site !== (doc.city || doc.platform)) {
      this.annos.push([SCOPE_MATCH, 0, GV_KEY_SITE, GV_FORMAT + site]);
    }
    const variant = doc.variant || 0;
    if (!_isUint(variant)) throw new Error(`variant ${variant} is not an OGXM v2 variant number`);
    this.mtch_mandatory = _cat(_varint(length), _varint(variant));
    this.mtch_fields = fields;
    const payload = _record(this.mtch_mandatory, fields);
    return [payload, Object.keys(fields).some((bit) => Number(bit) >= 12)];
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

function _number(v, what) {
  if (typeof v !== 'number' || !Number.isFinite(v)) {
    throw new Error(`${what} ${JSON.stringify(v)} is not a number`);
  }
  return v;
}

function _uintField(v, what, bits = 32) {
  if (!_isUint(v, bits)) throw new Error(`${what} ${JSON.stringify(v)} is not a count v2 can hold`);
  return v;
}

function _present(v) {
  return v !== undefined && v !== null;
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
  if (_present(alt.rollout_se)) fields[3] = _equity(conv.delta(_number(alt.rollout_se, 'rollout_se')));
  if (_present(alt.cubeless_equity)) {
    fields[4] = _equity(conv.eq(_number(alt.cubeless_equity, 'cubeless_equity')));
  }
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
 * `[fields, conformant]`: the checker record's fields beyond the list and the
 * level, from the document, and whether `DECS` can hold them as they are. The
 * list's size decides two of them (7.1): the total only means something where
 * the list was cut, and so do the counts of what was searched. A value `DECS`
 * would reject is still written, for the annotation that keeps the decision
 * exactly (P6).
 */
function _checkerExtras(a, count, nsources) {
  const f = {};
  let ok = true;
  let truncated = false;
  if (_present(a.alternatives_total)) {
    _uintField(a.alternatives_total, 'alternatives_total');
    f[1] = _varint(a.alternatives_total);
    truncated = a.alternatives_total > count;
    ok = ok && truncated;
  }
  for (const [bit, key] of [[5, 'rollouts_done'], [6, 'deep_searched']]) {
    if (_present(a[key])) {
      f[bit] = _varint(_uintField(a[key], key));
      ok = ok && truncated;
    }
  }
  for (const [bit, key] of [[7, 'position_tags'], [9, 'source_band']]) {
    if (_present(a[key])) f[bit] = _varint(_uintField(a[key], key));
  }
  if (_present(a.producer_ref)) {
    f[8] = _varint(_uintField(a.producer_ref, 'producer_ref'));
    ok = ok && a.producer_ref < nsources;
  }
  return [f, ok];
}

/**
 * `[main, exact, places]`: the record `DECS` can carry (or null), the exact one
 * for the annotation when that differs (or null), and where each document
 * alternative sits in `main` (null for one it leaves out; the list is null
 * without a `main`). `position` is the board before a legal dice ply and
 * `playedMoves` its play, for A4.
 */
function _checkerRecords(a, ref, blockEff, conv, unplayed, position = null, playedMoves = null,
  nsources = 0) {
  const alts = (a.alternatives || []).slice(0, MAX_ALTS);
  const best = Number(a.best_equity || 0.0);
  const loss = Number(a.equity_loss || 0.0);
  const hasExtras = CHECKER_KEYS.some((k) => _present(a[k]));
  if (!alts.length && !best && !loss && !hasExtras && !_present(a.level)) return [null, null, null];

  const [decOverride, decEff] = _tier(blockEff, _nn(a.level), {
    preset: alts.length ? _nn(alts[0].eval_level) : null, checker_ply: a.ply || null });
  const tiers = alts.map((alt) => _tier(decEff, _nn(alt.level), { preset: _nn(alt.eval_level) }));
  const keys = tiers.map(([, eff]) => _levelKey(eff));
  const played = [];
  alts.forEach((alt, i) => { if (alt.is_played) played.push(i); });

  function build(order, explicit) {
    const fields = {};
    if (order.length) {
      fields[0] = _cat(_varint(order.length), ...order.map((i) => _altRecord(
        alts[i], tiers[i][0], played.includes(i), conv)));
    }
    const [extra, conformant] = _checkerExtras(a, order.length, nsources);
    Object.assign(fields, extra);
    if (explicit || !order.length) fields[2] = _equity(conv.eq(best));
    if (explicit || !played.length) fields[3] = _loss(conv.delta(loss));
    if (Object.keys(decOverride).length) fields[4] = _levelBytes(decOverride);
    return [_record(_cat(_varint(ref), _varint(KIND_CHECKER)), fields), conformant];
  }

  const [exact] = build(alts.map((_a, i) => i), true);
  if (unplayed || played.length > 1) return [null, exact, null];
  if (alts.length && Math.abs(best - _altEquity(alts[0])) > 1e-9) return [null, exact, null];
  if (played.length) {
    const p = played[0];
    if (keys[p] !== keys[0]) return [null, exact, null];                                 // A5
    if (Math.abs((best - _altEquity(alts[p])) - loss) > _LOSS_TOLERANCE) return [null, exact, null];
  }

  // A4: the played move, wherever it appears in the list, is the flagged
  // alternative. Another copy of it (a notation split two ways) is dropped from
  // the record v2 holds; the exact list stays in the annotation.
  let keep = alts.map((_a, i) => i);
  const playedKey = position !== null ? _positionKey(position, playedMoves) : null;
  if (playedKey !== null) {
    const same = new Set(keep.filter((i) => _positionKey(position, alts[i].move) === playedKey));
    if (played.length && !same.has(played[0])) return [null, exact, null];
    keep = keep.filter((i) => !same.has(i) || (played.length && i === played[0]));
  }

  // A1: alternatives of one level adjacent, equity non-increasing within each.
  const order = [];
  const groups = new Map();
  for (const i of keep) {
    if (!groups.has(keys[i])) groups.set(keys[i], []);
    groups.get(keys[i]).push(i);
  }
  for (const idxs of groups.values()) order.push(...idxs);
  const enc = alts.map((alt) => _round_c(conv.eq(_altEquity(alt)) * 1e6));
  for (const idxs of groups.values()) {
    for (let j = 0; j < idxs.length - 1; j++) {
      if (enc[idxs[j + 1]] > enc[idxs[j]]) return [null, exact, null];
    }
  }
  const identity = order.length === alts.length && order.every((v, i) => v === i);
  if (!identity) {
    if (!order.length || order[0] !== 0) return [null, exact, null];
    const [main, conformant] = build(order, false);
    const places = alts.map((_a, i) => (order.includes(i) ? order.indexOf(i) : null));
    return [conformant ? main : null, exact, conformant ? places : null];
  }
  const [main, conformant] = build(order, false);
  return conformant ? [main, null, alts.map((_a, i) => i)] : [null, exact, null];
}

/** What the records of one block need to know about the block. */
class _Ctx {
  constructor(match, blockEff, writtenCurrency, nsources, luckLabel) {
    this.match = match;
    this.blockEff = blockEff;
    this.currency = writtenCurrency;
    this.nsources = nsources;
    this.luckLabel = luckLabel;
  }

  get mwc() {
    return this.currency === CURRENCY_CUBEFUL_MATCH;
  }

  /** A cube decision's own currency, where it states one that is not the block's
   *  and that can be written; else null. A match currency in a money game has no
   *  frame to convert through. */
  currencyOverride(sub) {
    const c = sub.currency;
    if (!_present(c)) return null;
    _uintField(c, 'currency');
    if (c === this.currency || (c === CURRENCY_CUBEFUL_MATCH && this.match.match_length <= 0)) {
      return null;
    }
    return c;
  }
}

/** `[record, conformant]`. `convFor(currency)` makes the converter for a currency
 *  the decision states itself. */
function _cubeRecord(sub, ref, verdict, loss, ctx, explicitLevel, conv, convFor, labels) {
  const own = ctx.currencyOverride(sub);
  if (own !== null) conv = convFor(own);
  const g = (k) => (sub[k] === undefined ? 0.0 : sub[k]);
  const fields = {
    0: _equity(conv.eq(g('no_double_equity'))),
    1: _equity(conv.eq(g('double_take_equity'))),
    2: _equity(conv.eq(g('double_pass_equity'))),
  };
  if ('eval' in sub) fields[3] = _probs(sub.eval);
  if (loss !== null) fields[4] = _loss(conv.delta(loss));
  if (_present(sub.take_point)) fields[5] = _prob(_number(sub.take_point, 'take_point'));
  if (sub.window_searched) fields[6] = new Uint8Array(0);
  const [override] = _tier(ctx.blockEff, explicitLevel, labels);
  if (Object.keys(override).length) fields[7] = _levelBytes(override);
  if (sub.is_optional) fields[8] = new Uint8Array(0);
  if (sub.is_free_cube) fields[9] = new Uint8Array(0);
  if (_present(sub.cubeful_take_value)) {
    fields[10] = _equity(conv.eq(_number(sub.cubeful_take_value, 'cubeful_take_value')));
  }
  if (own !== null) fields[11] = _varint(own);
  let ok = true;
  if (_present(sub.producer_ref)) {
    fields[12] = _varint(_uintField(sub.producer_ref, 'producer_ref'));
    ok = sub.producer_ref < ctx.nsources;
  }
  return [_record(_cat(_varint(ref), _varint(KIND_CUBE), _varint(verdict)), fields), ok];
}

/** `[record, conformant]`. */
function _luckRecord(a, ref, ctx, conv) {
  const [override] = _tier(ctx.blockEff, _nn(a.luck_level), luckLabels(ctx.luckLabel), false);
  const fields = {};
  if (Object.keys(override).length) fields[0] = _levelBytes(override);
  let ok = true;
  if (_present(a.luck_producer_ref)) {
    fields[1] = _varint(_uintField(a.luck_producer_ref, 'luck_producer_ref'));
    ok = a.luck_producer_ref < ctx.nsources;
  }
  return [_record(_cat(_varint(ref), _varint(KIND_ROLL), _equity(conv.delta(a.luck))), fields), ok];
}

/** `[decs, extra, frames]`: the records `DECS` holds, sorted, the per-ply
 *  records only the annotation can hold, and the `frame=` entries for plies
 *  whose source frame is not ours. */
function _blockRecords(match, select, ctx) {
  const mwc = ctx.mwc;
  const decs = [];                      // [ref, kind, bytes]
  const extra = new Map();              // ref -> [[kind, bytes]]
  const places = new Map();             // ref -> where a checker decision's document alternatives sit
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
    const convFor = (currency) => new _Converter(p, currency === CURRENCY_CUBEFUL_MATCH, null);
    const cube = (sub, verdict, loss, plyLabel = false) => {
      const labels = { preset: _nn(sub.eval_level) };
      if (plyLabel) labels.cube_ply = sub.ply || null;
      return _cubeRecord(sub, ref, verdict, loss, ctx, _nn(sub.level), conv, convFor, labels);
    };

    if ((action >= 0 && action <= 20) || illegal) {
      const nxt = idx + 1 < plies.length ? plies[idx + 1].ply.action_id : null;
      const unplayed = action >= 0 && action <= 20 && !(p.moves || []).length
        && (_RESIGNS.includes(nxt) || nxt === _FORFEIT) && plies[idx + 1].gi === gi;
      const legalDice = action >= 0 && action <= 20 && !illegal;
      const [main, exact, placed] = _checkerRecords(
        a, ref, ctx.blockEff, conv, unplayed,
        legalDice ? (match.position_before.get(ref) || null) : null,
        p.moves || [], ctx.nsources);
      if (main !== null) {
        recs.push([KIND_CHECKER, main, true]);
        places.set(ref, placed);
      }
      if (exact !== null) addExtra(ref, KIND_CHECKER, exact);
      const md = a.missed_double;
      const cd = a.cube_decision;
      if (md !== null && typeof md === 'object') {
        const verdict = _nn(_VERDICT[md.correct_action || 'double']) ?? 1;
        const [rec, ok] = cube(md, verdict, Number(md.equity_loss || 0.0));
        recs.push([KIND_CUBE, rec, ok && _OFFER_VERDICTS.includes(verdict)]);
      } else if (cd !== null && typeof cd === 'object') {
        const [rec, ok] = cube(cd, cd.should_double ? 1 : 0, null);
        recs.push([KIND_CUBE, rec, ok]);
      }
      if ('luck' in a) {
        const [rec, ok] = _luckRecord(a, ref, ctx, conv);
        recs.push([KIND_ROLL, rec, ok]);
      }
    } else if (CUBE_ACTIONS.includes(action)) {
      const verdict = _nn(_VERDICT[a.correct_action || 'no_double']) ?? 0;
      const allowed = action === ACTION_DOUBLE ? _OFFER_VERDICTS : _RESPONSE_VERDICTS;
      const loss = Number(a.equity_loss || 0.0);
      const [rec, ok] = cube(a, verdict, loss || null, true);
      recs.push([KIND_CUBE, rec, ok && allowed.includes(verdict)]);
    } else if (_RESIGNS.includes(action)) {
      const fields = {
        1: _equity(conv.delta(a.resign_error || 0.0)),
        2: _equity(conv.delta(a.take_resign_error || 0.0)),
        4: _loss(conv.delta(a.equity_loss || 0.0)),
      };
      if (_present(a.correct_value)) fields[0] = _varint(_uintField(a.correct_value, 'correct_value'));
      if ('eval' in a) fields[3] = _probs(a.eval);
      const [override] = _tier(ctx.blockEff, _nn(a.level));
      if (Object.keys(override).length) fields[5] = _levelBytes(override);
      let ok = true;
      if (_present(a.producer_ref)) {
        fields[6] = _varint(_uintField(a.producer_ref, 'producer_ref'));
        ok = a.producer_ref < ctx.nsources;
      }
      recs.push([KIND_RESIGN, _record(_cat(_varint(ref), _varint(KIND_RESIGN)), fields), ok]);
    }

    for (const [kind, rec, ok] of recs) {
      if (illegal || !ok) addExtra(ref, kind, rec);
      else decs.push([ref, kind, rec]);
    }
  });
  decs.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
  for (const recs of extra.values()) recs.sort((x, y) => x[0] - y[0]);
  return [decs, extra, frames, places];
}

const _HEX64 = /^[0-9a-f]{64}$/;
const _UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Whether `v` is the hyphenated lower-case form this module reads back. */
function _uuidOk(v) {
  return typeof v === 'string' && _UUID.test(v);
}

function _hexBytes(h) {
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.slice(2 * i, 2 * i + 2), 16);
  return out;
}

/**
 * `{fields, items, nsources, currency}` for a block's `ANAL` record: the
 * optional fields by bit (not the level or coverage), the `x-gammonview-analysis`
 * items for values v2 cannot hold (P6), how many sources the record states, and
 * the currency it is written in.
 *
 * A block is written in its stated currency when that can be converted to: match
 * winning chances need a match to be a chance of winning, every other currency is
 * the document's numbers as they are. Otherwise it is written in the default for
 * the match, and reads back so (profile section 4).
 */
function _blockFields(info, match) {
  const fields = {};
  const items = [];
  const item = (name, text) => items.push(`${name}=${_quote(text, _SAFE)}`);
  const string = (bit, key, cap = MAX_STRING) => {
    const v = info[key];
    if (v === undefined || v === null || v === '') return false;
    if (_strOk(v, cap)) {
      fields[bit] = _str(v);
      return true;
    }
    item(key, v);
    return false;
  };

  const producer = _nn(info.producer);
  if (producer !== null) {
    if (_isUint(producer)) fields[1] = _varint(producer);
    else item('producer', _gvText('i', producer));
  }
  if (info.complete) fields[3] = new Uint8Array(0);

  string(5, 'model_id');
  string(6, 'model_name');
  const digest = info.model_digest;
  if (digest) {
    if (typeof digest === 'string' && _HEX64.test(digest)) fields[7] = _hexBytes(digest);
    else item('model_digest', String(digest));
  }
  string(8, 'engine_build');

  const dflt = defaultCurrency(match.match_length);
  const stated = _nn(info.currency);
  if (stated !== null) _uintField(stated, 'currency');
  const currency = stated === null
    || (stated === CURRENCY_CUBEFUL_MATCH && match.match_length <= 0) ? dflt : stated;
  fields[9] = _varint(currency);

  if (_present(info.cube_efficiency)) {
    fields[10] = _prob(_number(info.cube_efficiency, 'cube_efficiency'));
  }
  const metId = info.met_id || (match.match_length > 0 ? 'kazaross-xg2' : null);
  if (metId) fields[11] = _str(metId);
  string(12, 'tables');

  const dials = info.dials;
  if (dials !== null && typeof dials === 'object' && !Array.isArray(dials)
      && Object.keys(dials).length) {
    const df = {};
    for (const [bit, key] of DIAL_FIELDS) {
      const v = dials[key];
      if (v === undefined || v === null) continue;
      const kind = GV_DIAL_FIELDS[key];
      if (kind === 'b') {
        if (v) df[bit] = new Uint8Array(0);
      } else if (kind === 'f') {
        const num = _number(v, `dials.${key}`);
        if (num >= 0) df[bit] = _loss(num);
        else item(`dials.${key}`, numberText(num));
      } else if (_isUint(v)) {
        df[bit] = _varint(v);
      } else if (typeof v === 'number' && Number.isInteger(v)) {
        item(`dials.${key}`, String(v));
      } else {
        throw new Error(`dials.${key} ${JSON.stringify(v)} is not a count`);
      }
    }
    if (Object.keys(df).length) fields[13] = _record(new Uint8Array(0), df);
  }

  if (info.timestamp) fields[14] = _varint(_int(info.timestamp) * 1000);
  const completed = _nn(info.completed_at);
  if (completed !== null) {
    if (_isUint(completed, 64)) fields[15] = _varint(completed);
    else item('completed_at', _gvText('i', completed));
  }
  if (info.duration_ms) fields[16] = _varint(_int(info.duration_ms));

  const sources = info.sources;
  let nsources = 0;
  if (Array.isArray(sources) && sources.length) {
    if (sources.every(_uuidOk)) {
      fields[17] = _cat(_varint(sources.length), ...sources.map(_uuidBytes));
      nsources = sources.length;
    } else {
      items.push(`sources=${sources.map((x) => _quote(String(x), _SAFE)).join(',')}`);
    }
  }
  return { fields, items, nsources, currency };
}

/** The `ply_ref`s a block's `coverage` names, ascending: the plies the document
 *  lists that exist, and every ply the block holds a decision for (6.5 makes a
 *  decision outside its coverage an error). */
function _coverage(info, match, decs) {
  const listed = info.coverage;
  if (!Array.isArray(listed) || !listed.length) return [];
  const refs = new Set();
  for (const entry of listed) {
    let ref;
    try {
      ref = Array.isArray(entry) && entry.length >= 2
        ? match.ref_of.get(`${_int(entry[0])},${_int(entry[1])}`) : undefined;
    } catch (e) {
      ref = undefined;
    }
    if (ref !== undefined) refs.add(ref);
  }
  if (!refs.size) return [];
  for (const [ref] of decs) refs.add(ref);
  return [...refs].sort((x, y) => x - y);
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
// Annotations, the clock and the video (8.2 - 8.4)
// ---------------------------------------------------------------------------

const MAX_DRAWINGS = 256;
const MAX_ANNOTATIONS = 4096;
const MAX_ANNO_TOTAL = 256 * 1024;

function _optUint(d, key, what, bits = 32) {
  const v = d[key];
  if (v === undefined || v === null) return 0;
  if (!_isUint(v, bits)) throw new Error(`${what} ${key} ${v} is not a count v2 can hold`);
  return v;
}

/** Why v2's ANNO cannot hold the annotation `a` (`annoCanon`'s shape), or null.
 *  Such a record travels in `x-gammonview-annotations`. */
export function annoProblem(a) {
  if (!_isUint(a.ref)) return 'its target is out of range';
  for (const name of ['value', 'key', 'lang', 'author']) {
    const v = a[name];
    if (v !== undefined && v !== null) {
      const n = _nbytes(v);
      if (!(n >= 0 && n <= MAX_STRING)) return `its ${name} is not a string v2 can hold`;
    }
  }
  if (a.at !== undefined && a.at !== null && !_isUint(a.at, 64)) return 'its time is not an instant';
  const { scope } = a;
  if (scope >= SCOPE_DECISION && !_isUint(a.kind)) return 'its decision kind is not a count';
  if (scope === SCOPE_ALTERNATIVE && !_isUint(a.alt_index)) return 'its alternative is not a count';
  const drawings = a.drawings || [];
  if (!Array.isArray(drawings) || drawings.length > MAX_DRAWINGS) {
    return 'it has more drawings than v2 allows';
  }
  if (drawings.length && scope === SCOPE_MATCH) return 'a match has no board to draw on (D3)';
  for (const d of drawings) {
    if (d === null || typeof d !== 'object' || !_isUint(d.shape)) return 'a drawing has no shape';
    const { at, to } = d;
    if (!_isUint(at, 8) || (to !== undefined && to !== null && !_isUint(to, 8))) {
      return "a drawing's point is not a point";
    }
    const hasTo = to !== undefined && to !== null;
    if (at > 27 || (hasTo && to > 27)) {
      return "a drawing's point is off the board (D2)";
    }
    if (d.shape === 1 && (!hasTo || to === at)) return 'an arrow needs a head that is not its tail (D1)';
    if (d.shape === 0 && hasTo) return 'a highlight has no head (D1)';
    if (d.color !== undefined && d.color !== null && !_isUint(d.color)) return "a drawing's colour is not a colour";
  }
  return null;
}

/** The canonical ANNO record (8.4) of `a`: `scope`, `ref`, `value` and whichever
 *  of `key`, `kind`, `alt_index`, `lang`, `author`, `at`, `drawings` and
 *  `analysis` (a hyphenated identifier) it states. An empty string is no
 *  string. */
export function annoCanon(a) {
  const fields = {};
  if (a.key) fields[0] = _str(a.key);
  if (a.kind !== undefined && a.kind !== null) fields[1] = _varint(a.kind);
  if (a.alt_index !== undefined && a.alt_index !== null) fields[2] = _varint(a.alt_index);
  if (a.lang) fields[3] = _str(a.lang);
  if (a.author) fields[4] = _str(a.author);
  if (a.at !== undefined && a.at !== null) fields[5] = _varint(a.at);
  if (a.drawings && a.drawings.length) {
    const out = [_varint(a.drawings.length)];
    for (const d of a.drawings) {
      const df = {};
      if (d.to !== undefined && d.to !== null) df[0] = Uint8Array.of(d.to);
      if (d.color !== undefined && d.color !== null) df[1] = _varint(d.color);
      out.push(_record(_cat(_varint(d.shape), Uint8Array.of(d.at)), df));
    }
    fields[6] = _cat(...out);
  }
  if (a.analysis) fields[7] = _uuidBytes(a.analysis);
  return _record(_cat(_varint(a.scope), _varint(a.ref), _str(a.value || '')), fields);
}

/** `json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=False)`. */
function _canonJson(v) {
  if (Array.isArray(v)) return `[${v.map(_canonJson).join(',')}]`;
  if (v !== null && typeof v === 'object') {
    return `{${Object.keys(v).filter((k) => v[k] !== undefined).sort()
      .map((k) => `${JSON.stringify(k)}:${_canonJson(v[k])}`).join(',')}}`;
  }
  return JSON.stringify(v);
}

/** One annotation of the document, placed. `native` says whether the file being
 *  written holds what it addresses, so ANNO can state it; if not (or if v2
 *  cannot hold the record) it travels in `x-gammonview-annotations`, which names
 *  the target in the document's own terms (`coords`). */
class _Anno {
  constructor(scope, ref, rec, coords, analysis = null, kind = null, alt = null, native = true) {
    this.coords = coords;
    this.rec = rec;
    this.analysis = analysis;
    this.group = _canonJson(coords);
    this.a = {
      scope, ref: ref !== null ? ref : 0, value: rec.value || '',
      key: _nn(rec.key), lang: _nn(rec.lang), author: _nn(rec.author), at: _nn(rec.at),
      drawings: _nn(rec.drawings), analysis, kind, alt_index: alt,
    };
    this.problem = ref !== null ? null : 'its target is not in the file';
    if (this.problem === null) this.problem = annoProblem(this.a);
    this.native = native && this.problem === null;
    this.bytes = this.problem === null ? annoCanon(this.a) : null;
  }

  /** The record with an alternative addressed by its place in the document's
   *  list, which is where another producer's DECS keeps it. */
  asDocumentIndex() {
    if (this.problem !== null || this.a.scope !== SCOPE_ALTERNATIVE) return this.bytes;
    return annoCanon({ ...this.a, alt_index: this.coords.i });
  }

  sort_key(seq, documentIndex = false) {
    const { a } = this;
    const alt = documentIndex && a.scope === SCOPE_ALTERNATIVE ? this.coords.i : a.alt_index;
    return annoSortKey(a.scope, a.ref, a.analysis, a.kind, alt,
      a.key || null, a.lang || null, seq);
  }

  fallback_item() {
    return { ...this.coords, v: this.rec };
  }
}

function _records(holder, what) {
  const got = holder.annotations;
  if (got === undefined || got === null) return [];
  if (!Array.isArray(got) || !got.every((r) => r !== null && typeof r === 'object' && !Array.isArray(r))) {
    throw new Error(`${what}: annotations must be a list of objects`);
  }
  return got;
}

/** `rec` without the keys that name its target (`kind`), checked: a key may not
 *  be a v2 field name (8.4.2), and one in our namespace would be read as a value
 *  of ours. */
function _own(rec, scope) {
  const { key } = rec;
  if (key) {
    if (V2_FIELD_NAMES.has(key)) throw new Error(`annotation key ${JSON.stringify(key)} is the name of a v2 field (8.4.2)`);
    if (key.startsWith(GV_PREFIX) && scope <= SCOPE_PLY) {
      throw new Error(`annotation key ${JSON.stringify(key)} is in the x-gammonview namespace, which is ours`);
    }
  }
  const out = {};
  for (const [k, v] of Object.entries(rec)) if (k !== 'kind') out[k] = v;
  if (out.value === undefined || out.value === null) out.value = '';
  return out;
}

/** The annotations at match, game and ply scope, in document order. */
function documentAnnos(doc, match) {
  const out = [];
  for (const rec of _records(doc, 'the match')) {
    out.push(new _Anno(SCOPE_MATCH, 0, _own(rec, 0), { s: 0 }));
  }
  (doc.games || []).forEach((g, gi) => {
    for (const rec of _records(g, `game ${gi}`)) {
      out.push(new _Anno(SCOPE_GAME, gi, _own(rec, 1), { s: 1, g: gi }));
    }
    (g.plies || []).forEach((p, pi) => {
      for (const rec of _records(p, `game ${gi} ply ${pi}`)) {
        const ref = match.ref_of.has(`${gi},${pi}`) ? match.ref_of.get(`${gi},${pi}`) : null;
        out.push(new _Anno(SCOPE_PLY, ref, _own(rec, 2), { s: 2, g: gi, p: pi }));
      }
    });
  });
  return out;
}

/** The annotations at decision and alternative scope of one block, in `ply_ref`
 *  order. A decision DECS does not carry (an illegal play's, one kept exactly in
 *  an annotation of ours) cannot be addressed by ANNO. */
function blockAnnos(match, select, aid, decs, places) {
  const held = new Set(decs.map(([ref, kind]) => `${ref},${kind}`));
  const out = [];
  const holderOk = (p, a, kind, what) => {
    if (!_isUint(kind) || decisionHolder(p, a, kind) === null) {
      throw new Error(`${what}: annotation names a decision (kind ${kind}) the analysis lacks`);
    }
  };
  for (const { key, gi, pi, ply: p } of match.ply_at) {
    const a = select(p);
    if (a === null || a === undefined || typeof a !== 'object') continue;
    const ref = match.ref_of.get(key);
    const what = `game ${gi} ply ${pi}`;
    const nat = naturalKind(p);
    const decision = (rec, kind) => {
      out.push(new _Anno(SCOPE_DECISION, ref, _own(rec, 3),
        { s: 3, g: gi, p: pi, a: aid, k: kind }, aid, kind, null, held.has(`${ref},${kind}`)));
    };
    for (const rec of _records(a, what)) {
      const kind = rec.kind !== undefined ? rec.kind : nat;
      holderOk(p, a, kind, what);
      decision(rec, kind);
    }
    if (nat === KIND_CHECKER) {
      for (const name of ['missed_double', 'cube_decision']) {
        const sub = a[name];
        if (sub !== null && sub !== undefined && typeof sub === 'object') {
          for (const rec of _records(sub, what)) {
            holderOk(p, a, KIND_CUBE, what);
            decision(rec, KIND_CUBE);
          }
        }
      }
      (a.alternatives || []).forEach((alt, i) => {
        const recs = _records(alt, what);
        if (recs.length) holderOk(p, a, KIND_CHECKER, what);
        const where = held.has(`${ref},${KIND_CHECKER}`) ? places.get(ref) : undefined;
        const place = where !== undefined && where !== null && i < where.length ? where[i] : null;
        for (const rec of recs) {
          out.push(new _Anno(SCOPE_ALTERNATIVE, ref, _own(rec, 4),
            { s: 4, g: gi, p: pi, a: aid, k: KIND_CHECKER, i }, aid, KIND_CHECKER,
            place !== null ? place : 0, place !== null));
        }
      });
    }
  }
  return out;
}

/** Spec N2: within one target, a `(key, lang)` pair is used once. */
function checkAnnos(annos) {
  const seen = new Set();
  for (const a of annos) {
    if (a.a.key) {
      const ident = `${a.group}\0${a.a.key}\0${a.a.lang || ''}\0${a.a.lang ? 1 : 0}`;
      if (seen.has(ident)) throw new Error(`annotation key ${JSON.stringify(a.a.key)} is used twice on one target (N2)`);
      seen.add(ident);
    }
  }
}

/** CLCK (8.2) from `clock` and each ply's `clock_ms`, or null where the document
 *  has no clock or the series cannot be written: a reading after a ply without
 *  one, a first reading that is not 0, one that runs backwards. */
function _clockSection(doc, match) {
  const { clock } = doc;
  if (clock === null || typeof clock !== 'object' || clock === undefined) return null;
  const flags = (clock.white_berserk ? 1 : 0) | (clock.black_berserk ? 2 : 0);
  const other = _optUint(clock, 'flags_other', 'clock', 8);
  if (other & 3) throw new Error('clock flags_other overlaps the berserk flags');
  const header = ['reserve_ms', 'delay_ms', 'increment_ms', 'start_timestamp']
    .map((k) => _optUint(clock, k, 'clock')).concat([flags | other]);
  let precision = clock.precision;
  if (precision === undefined || precision === null) precision = CLOCK_PRECISION;
  if (!_isUint(precision) || precision < 1) throw new Error(`clock precision ${precision} is not a step`);
  const ts = [];
  let gap = false;
  for (const { ply: p } of match.ply_at) {
    const v = p.clock_ms;
    if (v === undefined || v === null) { gap = true; continue; }
    if (gap) return null;
    if (!_isUint(v)) throw new Error(`clock_ms ${v} is not a time v2 can hold`);
    ts.push(v);
  }
  return encodeClock(header, ts, match.ply_at.length, precision);
}

/** VIDO (8.3) from `video` and the marks on the plies. A mark on a ply v2 has no
 *  record of (a game's set-up position, a game past the 256th) is dropped by
 *  itself. */
function _videoSection(doc, match) {
  const { video } = doc;
  if (video === null || typeof video !== 'object' || video === undefined) return null;
  const kind = _optUint(video, 'kind', 'video', 8);
  const offset = video.offset_ms || 0;
  if (!Number.isInteger(offset) || !(offset >= -(2 ** 31) && offset < 2 ** 31)) {
    throw new Error(`video offset_ms ${offset} is not an offset v2 can hold`);
  }
  const url = video.url || '';
  if (typeof url !== 'string') throw new Error('video url must be a string');
  let raw = url ? _enc.encode(url) : new Uint8Array(0);
  if (raw.length > MAX_VIDEO_URL || !urlStorable(raw, kind)) {
    if (url) match.annos.push([SCOPE_MATCH, 0, GV_KEY_VIDEO_URL, GV_FORMAT + url]);
    raw = new Uint8Array(0);
  }
  const marks = [];
  for (const { gi, pi, ply: p } of match.ply_at) {
    if (p.video_ms === undefined || p.video_ms === null) continue;
    const v2pi = pi - match.game_start[gi];
    if (gi > 255 || v2pi > 0xFFFF) continue;
    const wall = _nn(p.wall_ms);
    const behind = _nn(p.behind_live_ms);
    for (const [name, v, bits] of [['video_ms', p.video_ms, 32], ['wall_ms', wall, 64],
      ['behind_live_ms', behind, 32]]) {
      if (v !== null && !_isUint(v, bits)) throw new Error(`${name} ${v} is not a time v2 can hold`);
    }
    marks.push([gi, v2pi, Boolean(p.video_hand_anchored), p.video_ms, wall, behind]);
  }
  return encodeVideo([kind, Boolean(video.is_live), offset, raw], marks);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

function _annoRecord(scope, ref, key, value) {
  return _record(_cat(_varint(scope), _varint(ref), _str(value)), { 0: _str(key) });
}

/** One analysis block as the document canonically encodes it. */
class _Block {
  constructor(aid, anal, decs, annos, fields) {
    this.aid = aid;
    this.fields = fields;              // the ANAL record's optional fields, by bit
    this.aid_str = _uuidStr(aid);
    this.anal = anal;
    this.decs = decs;
    this.annos = annos;                // [scope, ref, key, value] this block adds to ANNO
  }

  /** The `ANAL` record with `started_at` the exact instant `ms`: a source that
   *  stated milliseconds keeps them while the document's second is the same (P4). */
  anal_started(ms) {
    return _record(this.aid, { ...this.fields, 14: _varint(ms) });
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
  constructor(doc, match, mtch, uses21, blocks, annos, clck, vido) {
    this.doc = doc;
    this.match = match;
    this.mtch = mtch;
    this.uses_21 = uses21;
    this.blocks = blocks;
    this.annos = annos;                // the document's annotations (_Anno), in document order
    this.clck = clck;                  // the canonical CLCK / VIDO payloads, or null
    this.vido = vido;
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
  const docAnnos = documentAnnos(doc, match);
  const usedIds = new Set();
  blocks.forEach(([info, select], k) => {
    const aid = _analysisId(k, info, matchBytes);
    const aidStr = _uuidStr(aid);
    if (usedIds.has(aidStr)) throw new Error(`analysis block ${k} repeats an analysis_id`);
    usedIds.add(aidStr);
    const objs = match.ply_at.map(({ ply }) => select(ply));
    const stated = _nn(info.level);
    const blockEff = stated !== null ? _checkLevel(stated) : blockLevelOf(info, objs);
    const luckLabel = info.luck_eval_level || '1ply';
    const { fields, items, nsources, currency } = _blockFields(info, match);
    const ctx = new _Ctx(match, blockEff, currency, nsources, luckLabel);
    const [decs, extra, frames, places] = _blockRecords(match, select, ctx);

    const analFields = fields;
    if (Object.keys(blockEff).length) analFields[2] = _levelBytes(blockEff);
    const coverage = _coverage(info, match, decs);
    if (coverage.length) {
      analFields[4] = _cat(_varint(coverage.length),
        ...coverage.map((r, i) => _varint(r - (i ? coverage[i - 1] + 1 : 0))));
    }
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

    if (_nn(info.eval_level) !== _nn(blockEff.preset)) {
      items.push(`level=${_quote(info.eval_level || '', _SAFE)}`);
    }
    if (decs.some(([, kind]) => kind === KIND_ROLL)
        || [...extra.values()].some((recs) => recs.some(([kind]) => kind === KIND_ROLL))) {
      items.push(`luck=${_quote(luckLabel, _SAFE)}`);
    }
    items.push(`pr=${tokens.join(',')}`);
    if (frames.length) items.push(`frame=${frames.join(',')}`);
    const gvAnnos = [];
    for (const [key, value] of _chunked(GV_KEY_ANALYSIS + aidStr, GV_FORMAT + items.join(';'))) {
      gvAnnos.push([SCOPE_MATCH, 0, key, value]);
    }
    for (const [ref, recs] of extra) {
      const value = GV_FORMAT + _b64encode(_cat(...recs.map((r) => r[1])));
      for (const [key, part] of _chunked(GV_KEY_DECISIONS + aidStr, value)) {
        gvAnnos.push([SCOPE_PLY, ref, key, part]);
      }
    }
    out.push(new _Block(aid, anal, decsPayload, gvAnnos, analFields));
    docAnnos.push(...blockAnnos(match, select, aidStr, decs, places));
  });
  checkAnnos(docAnnos);
  const clck = _clockSection(doc, match);
  const vido = _videoSection(doc, match);
  return new _Parts(doc, match, mtch, uses21, out, docAnnos, clck, vido);
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
  // The document's own annotations: the source's bytes while it still encodes
  // to what the source held, else v2's record where the file holds what it
  // addresses, else a record of ours.
  const ours = [...match.annos];
  for (const blk of plan.blocks) ours.push(...blk[3]);
  const carried = [];
  for (const a of parts.annos) {
    const foreign = plan.foreign_verbatim.has(a.analysis);
    const canon = foreign ? a.asDocumentIndex() : a.bytes;
    const raw = canon !== null && (a.analysis === null || plan.verbatim_ids.has(a.analysis))
      ? plan.takeRaw(canon) : null;
    if (raw !== null) {
      entries.push([a.sort_key(entries.length), raw]);
    } else if (foreign) {
      // Another producer's DECS is as it came, so the annotation can address what
      // it holds: a decision of that kind, an alternative in its list.
      const held = (plan.foreign_decs.get(a.analysis) || new Map()).get(`${a.a.ref},${a.a.kind}`);
      if (canon !== null && held !== undefined
          && (a.a.scope === SCOPE_DECISION || a.coords.i < held)) {
        entries.push([a.sort_key(entries.length, true), canon]);
      } else {
        carried.push(a.fallback_item());
      }
    } else if (a.native) {
      entries.push([a.sort_key(entries.length), a.bytes]);
    } else {
      carried.push(a.fallback_item());
    }
  }
  if (carried.length) {
    const text = _b64encode(_enc.encode(_canonJson(carried)));
    for (const [key, part] of _chunked(GV_KEY_ANNOTATIONS, GV_FORMAT + text)) {
      ours.push([SCOPE_MATCH, 0, key, part]);
    }
  }
  for (const [scope, ref, key, value] of ours) {
    entries.push([annoSortKey(scope, ref, null, null, null, key, null, entries.length),
      _annoRecord(scope, ref, key, value)]);
  }
  if (entries.length) {
    entries.sort((x, y) => cmpSortKeys(x[0], y[0]));
    const payload = _cat(...entries.map((e) => e[1]));
    // Spec section 10: never write a file past the limits, nor drop a record.
    if (entries.length > MAX_ANNOTATIONS) {
      throw new Error(`${entries.length} annotations (ours included) exceed OGXM v2's ${MAX_ANNOTATIONS}`);
    }
    if (payload.length > MAX_ANNO_TOTAL) {
      throw new Error(`the annotations (ours included) are ${payload.length} bytes, past OGXM v2's ${MAX_ANNO_TOTAL}`);
    }
    secs.push(['ANNO', _section('ANNO', payload, false)]);
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
