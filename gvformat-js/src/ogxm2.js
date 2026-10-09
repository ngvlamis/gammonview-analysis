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
// **A reader into our document's shape.** The document that comes out is the
// one a v1 file holding the same match would give, so nothing downstream learns
// there are two versions. `ogxm2_writer.js` is the other half, and what
// `write_gvab` writes; `docs/OGXM_V2_PROFILE.md` is the profile the two share.
//
// **Two kinds of block.** A block our writer made is marked by its
// `x-gammonview-analysis` annotation and read exactly: the `x-` annotations put
// back what v2 has no field for (an illegal play's analysis and steps, the
// PR-counting flags, level labels, `site`), and the reader derives nothing it
// was told. Any other producer's block is read as below, and `basefill.js`
// completes it as it completes any foreign v1 block. HedgeHog's own `to_v1`
// (`src/match/ogxm2_v1.cpp`) is the model for that, with two departures, both
// towards reading more files rather than fewer:
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
import { _crc32, _b64decode, _b64encode } from './binary.js';
import {
  completeBaseBlock, framePerspectiveIsWhite, mwcFrame, trivialCube, _checkerIsDecision,
  _cubePlyIsDecision,
} from './basefill.js';
import { DICE_TABLE } from './constants.js';
import {
  attach, decodeClock, clockDoc, decodeVideo, videoDoc, videoMarkDoc,
} from './ogxm2_passthrough.js';

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

// [9.14] action ids beyond v1's 0-31.
const ACTION_DOUBLE = 21;
const ACTION_TAKE = 22;
const ACTION_DROP = 23;
const ACTION_RESIGN_GAME = 27;
const ACTION_RESIGN_MATCH = 28;
const ACTION_SET_POSITION = 31;
const ACTION_BEAVER = 32;
const ACTION_RACCOON = 33;
const ACTION_SETTLE = 34;
const ACTION_RESERVED = 35;
const ACTION_CUBE_SET = 36;
const ACTION_PASS = 37;
const ACTION_ESCAPE = 63;
// The last action id this reader assigns a meaning to; above it a ply is kept
// whole (`extras_raw`), since only the producer knows what it says (P1, P2).
const LAST_KNOWN_ACTION = ACTION_PASS;

const KIND_CHECKER = 0;
const KIND_CUBE = 1;
const KIND_RESIGN = 2;
const KIND_ROLL = 3;

const CURRENCY_CUBEFUL_MONEY = 1;
const CURRENCY_CUBEFUL_MATCH = 2;                // [9.10]

// The plies a cube record can sit on besides a dice action [7.4], and what each
// is called in a document's `played_action`.
const CUBE_ACTIONS = [ACTION_DOUBLE, ACTION_TAKE, ACTION_DROP, ACTION_BEAVER, ACTION_RACCOON];
const PLAYED_ACTION = {
  [ACTION_DOUBLE]: 'double', [ACTION_TAKE]: 'take', [ACTION_DROP]: 'pass',
  [ACTION_BEAVER]: 'beaver', [ACTION_RACCOON]: 'raccoon',
};

// A level marker the pass in `finishLevels` reads and removes.
const _LV = '\0level';
const _LV_LUCK = '\0luck';
// Decision keys that are v2 fields copied as they are, by kind.
const CHECKER_KEYS = ['alternatives_total', 'rollouts_done', 'deep_searched', 'position_tags',
  'producer_ref', 'source_band'];
const CUBE_KEYS = ['take_point', 'window_searched', 'is_optional', 'is_free_cube', 'producer_ref'];
const RESIGN_KEYS = ['correct_value', 'producer_ref'];

// What v2 has no field for, carried in ANNO under the `x-` namespace the spec
// reserves for producers outside it (N6). See ogxm2_writer.js for what each
// holds; every value starts with GV_FORMAT.
const GV_FORMAT = '1:';
const GV_KEY_ANALYSIS = 'x-gammonview-analysis/';
const GV_KEY_DECISIONS = 'x-gammonview-decisions/';
const GV_KEY_ILLEGAL_PLY = 'x-gammonview-illegal-ply';
const GV_KEY_SITE = 'x-gammonview-site';
const GV_KEY_EVENT = 'x-gammonview-event';
const GV_KEY_SCORE = 'x-gammonview-score';
// Annotations of the document that v2's ANNO cannot address or hold (a decision
// the DECS stream does not carry, a value past a cap): a JSON list, base64,
// chunked (profile section 4).
const GV_KEY_ANNOTATIONS = 'x-gammonview-annotations';
// A video URL v2 cannot store (8.3: it is dropped there).
const GV_KEY_VIDEO_URL = 'x-gammonview-video.url';
const GV_PREFIX = 'x-gammonview-';

// Every name v2 gives a field (spec 8.4.2): an ANNO key may not be one. It is
// the reference codec's list, taken from its schema.
const V2_FIELD_NAMES = new Set((
  'action action_ext algorithm alt_index alternatives alternatives_total analysis analysis_id '
  + 'at author auto_doubles best_equity black_name black_profile board budget_ms checker_ply city '
  + 'color complete completed_at correct_value country coverage covers crawford_before_start '
  + 'cube_efficiency cube_limit cube_limit_mode cube_limit_resolved cube_owner cube_ply cube_rule '
  + 'cube_value cubeful_take_value cubeless_equity currency date_precision deep_searched dials dice '
  + 'digest double_pass_equity double_take_equity drawings duration_ms engine_build equity '
  + 'equity_loss event event_url event_year exact_bearoff illegal initial_board initial_cube_owner '
  + 'initial_cube_value is_free_cube is_last_game is_optional is_played jacoby_mode jacoby_resolved '
  + 'key key_id kind lang level luck match_digest match_length match_policy match_ref met_id '
  + 'model_digest model_id model_name move_ply no_double_equity no_rollout player_seat ply_ref '
  + 'points_won position_tags preset probs producer producer_ref public_key race_order rated '
  + 'rating rating_system ref resign_error resign_value result rollout rollout_budget_on '
  + 'rollout_se rollouts_done round rules scope score_final score_start seat seed settle_value '
  + 'shape signature signed_at site source source_band sources stage started_at steps table '
  + 'tables take_point take_resign_error termination to top_deep top_deep_accept top_deep_keep '
  + 'top_deep_threshold trials truncation_depth user_id value variance_reduction variant verdict '
  + 'white_name white_profile window_searched winner').split(' '));

// Match fields a value v2 cannot hold travels under `x-gammonview-<field>` at
// match scope (`x-gammonview-<side>_profile.<field>` for a player). Each maps to
// how its text spells it: `s` as is, `i` an integer, `f` a number.
const GV_MATCH_FIELDS = {
  stage: 's', round: 'i', table: 's', city: 's', country: 's', event_url: 's',
  platform: 's', match_ref: 's', event_year: 'i', date_precision: 'i',
  player_seat: 'i', rules_other: 'i',
};
const GV_PROFILE_FIELDS = { user_id: 's', rating: 'f', rating_system: 's', country: 's', kind: 'i' };
// The same for a game, at game scope.
const GV_GAME_FIELDS = {
  initial_cube_value: 'i', initial_cube_owner: 'i', auto_doubles: 'i', termination: 'i',
};

const SCOPE_MATCH = 0;
const SCOPE_GAME = 1;
const SCOPE_PLY = 2;
const SCOPE_DECISION = 3;
const SCOPE_ALTERNATIVE = 4;
const MARKER_ACTIONS = [24, 25, 26, 30];

// [9.13] verdict -> the document's label, one for one (writer: _VERDICT). The
// three beyond the v1 set keep their names: a reader that wants the plain
// no-double or take they refine can fold them itself.
const VERDICT_NAMES = {
  0: 'no_double', 1: 'double', 2: 'take', 3: 'pass', 4: 'too_good',
  5: 'beaver', 6: 'raccoon',
};

const PRODUCER_OGX = 0;                          // [9.9] HedgeHog's own engine

// [6.3] `dials`: bit -> name, in bit order. `DIAL_FLAGS` hold no payload,
// `top_deep_threshold` is an equity loss, the rest are varints.
const DIAL_FIELDS = [
  [0, 'jacoby_resolved'], [1, 'jacoby_mode'], [2, 'cube_limit_resolved'], [3, 'cube_limit_mode'],
  [4, 'exact_bearoff'], [5, 'race_order'], [6, 'top_deep'], [7, 'top_deep_threshold'],
  [8, 'rollout_budget_on'], [9, 'cube_rule'], [10, 'top_deep_keep'], [11, 'top_deep_accept'],
];
const DIAL_FLAGS = ['jacoby_resolved', 'exact_bearoff', 'race_order', 'top_deep', 'rollout_budget_on'];

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
  u64() { this.need(8); const v = this.view.getBigUint64(this.pos, true); this.pos += 8; return v; }
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

/** 16 bytes as the lowercase hyphenated string `str(uuid.UUID(bytes=...))` gives. */
function _uuidStr(b) {
  const h = Array.from(b, (v) => v.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
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
  if (has(4)) r.seed = String(body.u64());       // a decimal string, as the reference's JSON has it [J6]
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

const _DEPTH_PRESET = /^\d+ply$/;

/**
 * The level a tier has when its labels (`eval_level`, `ply`) are all that says
 * so: the tier above, with each label that is set laid over it. Shared by the
 * reader, which keeps a `level` object only where the true level is something
 * else, and the writer, which derives the level from the labels when the
 * document keeps none.
 *
 * A preset that is only a depth (`2ply`) follows a depth the labels change: a
 * tier at `ply` 3 under a `2ply` block is `3ply`, not a 3-ply search called
 * `2ply`. `follow = false` leaves it, which is how a luck record states its
 * depth (`luckLabels`).
 */
export function tierLevel(parent, { preset = null, checker_ply: checkerPly = null,
  cube_ply: cubePly = null, follow = true } = {}) {
  const out = { ...parent };
  if (preset) out.preset = preset;
  if (checkerPly) out.checker_ply = checkerPly;
  if (cubePly) out.cube_ply = cubePly;
  if (follow && !preset && _DEPTH_PRESET.test(out.preset || '')) {
    const depth = checkerPly || cubePly;
    if (depth) out.preset = `${depth}ply`;
  }
  return out;
}

/** The level most of a block's decisions were judged at -- what v2 means by a
 *  block's level [6.4], and what lets most of them state none of their own. */
export function commonLabel(objs) {
  const counts = new Map();
  for (const a of objs) {
    if (a === null || typeof a !== 'object') continue;
    const alts = a.alternatives || [];
    const labels = [alts.length ? alts[0].eval_level : null, a.eval_level];
    for (const sub of [a.cube_decision, a.missed_double]) {
      if (sub !== null && typeof sub === 'object') labels.push(sub.eval_level);
    }
    for (const lbl of labels) if (lbl) counts.set(lbl, (counts.get(lbl) || 0) + 1);
  }
  let best = null;
  let bestN = 0;
  for (const [lbl, n] of counts) {
    if (n > bestN) { best = lbl; bestN = n; }
  }
  return best;
}

/** The block level a block's labels give: the label most decisions share, and
 *  the base depth. A block whose level is more than that keeps it as `level`. */
export function blockLevelOf(info, objs) {
  return tierLevel({}, { preset: commonLabel(objs) || info.eval_level,
    checker_ply: info.ply ? Math.trunc(Number(info.ply)) : null });
}

/** What `luck_eval_level` says of a luck record's level. */
export function luckLabels(label) {
  const m = /^(\d+)ply$/.exec(label || '');
  return m ? { checker_ply: parseInt(m[1], 10) } : { preset: label };
}

function _luckLevelOf(blockLevel, label) {
  return tierLevel(blockLevel, { ...luckLabels(label), follow: false });
}

/** A JSON-safe value copied. */
function _clone(v) {
  return JSON.parse(JSON.stringify(v));
}

/** A value as comparable text, object keys in order. */
function _stable(v) {
  if (Array.isArray(v)) return `[${v.map(_stable).join(',')}]`;
  if (v !== null && typeof v === 'object') {
    return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${_stable(v[k])}`).join(',')}}`;
  }
  return JSON.stringify(v);
}

/** A level without a preset that is only a depth: `2ply` says nothing the depth
 *  next to it does not. */
function _normLevel(level) {
  const out = { ...level };
  if (out.preset && _DEPTH_PRESET.test(out.preset)) delete out.preset;
  return out;
}

export function sameLevel(a, b) {
  return _stable(_normLevel(a)) === _stable(_normLevel(b));
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

/** [4.1] A `player` record. */
function _decodeProfile(cur) {
  const { body, has } = cur.record();
  const pr = {};
  if (has(0)) pr.user_id = body.str();
  if (has(1)) pr.rating = body.varint() / 100;
  if (has(2)) pr.rating_system = body.str();
  if (has(3)) pr.country = body.str();
  if (has(4)) pr.kind = body.varint();
  return pr;
}

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
  if (has(9)) m.completed_at = Number(body.varint64());
  if (has(10)) m.player_seat = body.varint();
  if (has(11)) m.crawford_before_start = true;
  if (has(12)) m.event = body.str();
  if (has(13)) m.event_year = body.varint();
  if (has(14)) m.date_precision = body.varint();
  if (has(15)) m.stage = body.str();
  if (has(16)) m.round = body.varint();
  if (has(17)) m.table = body.str();
  if (has(18)) m.city = body.str();
  if (has(19)) m.country = body.str();
  if (has(20)) m.event_url = body.str();
  if (has(21)) m.site = body.str();
  if (has(22)) m.match_ref = body.str();
  if (has(23)) m.white_profile = _decodeProfile(body);
  if (has(24)) m.black_profile = _decodeProfile(body);
  if (has(25)) m.rated = true;
  // Any later bits are the unknown run; the record length steps over them.
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
    const first = cur.pos;
    const { body, has } = cur.record();
    const x = {};
    // An action id nothing here assigns (38-62) carries a payload only its
    // producer can read, so none of the fields below is its; an escape (63)
    // has to be read as far as its real id.
    const known = ply.action <= LAST_KNOWN_ACTION || ply.action === ACTION_ESCAPE;
    if (known && has(0)) x.dice = [body.u8(), body.u8()];
    if (known && has(1)) x.resign_value = body.varint();
    if (known && has(2)) x.cube_value = body.varint();
    if (known && has(3)) x.illegal = true;
    if (known && has(4)) x.settle_value = body.equity();
    if (known && has(5)) x.steps = _countedSteps(body);
    if (known && has(6)) x.board = body.board();
    if (known && has(7)) x.action_ext = body.varint();
    if (known && has(8)) x.cube_owner = body.varint();
    ply.extras = x;
    ply.extras_raw = cur.data.slice(first, cur.pos);     // the record, length prefix and all
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

/** [6.3] A `dials` record: flags as `true`, the threshold a number. */
function _decodeDials(cur) {
  const { body, has } = cur.record();
  const d = {};
  for (const [bit, name] of DIAL_FIELDS) {
    if (!has(bit)) continue;
    if (DIAL_FLAGS.includes(name)) d[name] = true;
    else if (name === 'top_deep_threshold') d[name] = body.loss();
    else d[name] = body.varint();
  }
  return d;
}

/** [6] */
function _decodeAnal(payload) {
  const cur = new Cursor(payload, 0, payload.length);
  const { body, has } = cur.record();
  const a = { analysis_id: _uuidStr(body.bytes(16)) };
  if (has(0)) body.skip(32);                     // match_digest
  if (has(1)) a.producer = body.varint();
  if (has(2)) a.level = _level(body);
  if (has(3)) a.complete = true;
  if (has(4)) {
    const n = body.varint();
    const refs = [];
    for (let i = 0; i < n; i++) refs.push(body.varint() + (refs.length ? refs[refs.length - 1] + 1 : 0));
    a.coverage = refs;
  }
  if (has(5)) a.model_id = body.str();
  if (has(6)) a.model_name = body.str();
  if (has(7)) a.model_digest = Array.from(body.bytes(32), (v) => v.toString(16).padStart(2, '0')).join('');
  if (has(8)) a.engine_build = body.str();
  if (has(9)) a.currency = body.varint();
  if (has(10)) a.cube_efficiency = body.prob();
  if (has(11)) a.met_id = body.str();
  if (has(12)) a.tables = body.str();
  if (has(13)) a.dials = _decodeDials(body);
  if (has(14)) a.started_at = body.varint64();
  if (has(15)) a.completed_at = Number(body.varint64());
  if (has(16)) a.duration_ms = body.varint();
  if (has(17)) {
    const n = body.varint();
    a.sources = [];
    for (let i = 0; i < n; i++) a.sources.push(_uuidStr(body.bytes(16)));
  }
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
  if (has(3)) alt.rollout_se = body.equity();
  if (has(4)) alt.cubeless_equity = body.equity();
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
      if (has(1)) d.alternatives_total = body.varint();
      if (has(2)) d.best_equity = body.equity();
      if (has(3)) d.equity_loss = body.loss();
      if (has(4)) d.level = _level(body);
      if (has(5)) d.rollouts_done = body.varint();
      if (has(6)) d.deep_searched = body.varint();
      if (has(7)) d.position_tags = body.varint();
      if (has(8)) d.producer_ref = body.varint();
      if (has(9)) d.source_band = body.varint();
    } else if (d.kind === KIND_CUBE) {
      d.verdict = body.varint();
      if (has(0)) d.no_double_equity = body.equity();
      if (has(1)) d.double_take_equity = body.equity();
      if (has(2)) d.double_pass_equity = body.equity();
      if (has(3)) d.probs = body.probs();
      if (has(4)) d.equity_loss = body.loss();
      if (has(5)) d.take_point = body.prob();
      if (has(6)) d.window_searched = true;
      if (has(7)) d.level = _level(body);
      if (has(8)) d.is_optional = true;
      if (has(9)) d.is_free_cube = true;
      if (has(10)) d.cubeful_take_value = body.equity();
      if (has(11)) d.currency = body.varint();
      if (has(12)) d.producer_ref = body.varint();
    } else if (d.kind === KIND_RESIGN) {
      if (has(0)) d.correct_value = body.varint();
      if (has(1)) d.resign_error = body.equity();
      if (has(2)) d.take_resign_error = body.equity();
      if (has(3)) d.probs = body.probs();
      if (has(4)) d.equity_loss = body.loss();
      if (has(5)) d.level = _level(body);
      if (has(6)) d.producer_ref = body.varint();
    } else if (d.kind === KIND_ROLL) {
      d.luck = body.equity();
      if (has(0)) d.level = _level(body);
      if (has(1)) d.producer_ref = body.varint();
    } else {
      continue;                                  // a kind this reader does not know
    }
    out.push(d);
  }
  return out;
}

/** The `analysis_id` of an `ANAL` payload, hyphenated. */
export function uuidOf(anal) {
  const cur = new Cursor(anal, 0, anal.length);
  const len = cur.varint();
  const body = new Cursor(anal, cur.pos, cur.pos + len);
  body.varint64();
  return _uuidStr(body.bytes(16));
}

/** [8.4.3] A drawing. A shape or colour nothing here knows is kept as is. */
function _decodeDrawing(cur) {
  const { body, has } = cur.record();
  const d = { shape: body.varint(), at: body.u8() };
  if (has(0)) d.to = body.u8();
  if (has(1)) d.color = body.varint();
  return d;
}

/** [8.4] ANNO records, in file order. Each carries its own bytes as `raw`, for
 *  `ogxm2_passthrough`. */
function _decodeAnno(payload) {
  const cur = new Cursor(payload, 0, payload.length);
  const out = [];
  while (cur.pos < cur.end) {
    const first = cur.pos;
    const { body, has } = cur.record();
    const r = { scope: body.varint(), ref: body.varint(), value: body.str(),
      raw: payload.slice(first, cur.pos) };
    if (has(0)) r.key = body.str();
    if (has(1)) r.kind = body.varint();
    if (has(2)) r.alt_index = body.varint();
    if (has(3)) r.lang = body.str();
    if (has(4)) r.author = body.str();
    if (has(5)) r.at = Number(body.varint64());
    if (has(6)) {
      r.drawings = [];
      for (let n = body.varint(); n > 0; n--) r.drawings.push(_decodeDrawing(body));
    }
    if (has(7)) r.analysis = _uuidStr(body.bytes(16));
    out.push(r);
  }
  return out;
}

/** An annotation as the document holds it: `value`, then whichever of `key`,
 *  `lang`, `author`, `at` and `drawings` it states. An empty string is no
 *  string, and no drawings are none. */
function annoDoc(r) {
  const out = { value: r.value };
  for (const k of ['key', 'lang', 'author']) if (r[k]) out[k] = r[k];
  if (r.at !== undefined && r.at !== null) out.at = r.at;
  if (r.drawings && r.drawings.length) out.drawings = r.drawings.map((d) => ({ ...d }));
  return out;
}

/** Whether `readOgxm2` consumes this annotation into document keys it has (and
 *  so regenerates it on write), rather than into `annotations`. */
function annoIsOurs(r) {
  const base = (r.key || '').split('~')[0];
  const name = base.startsWith(GV_PREFIX) ? base.slice(GV_PREFIX.length) : null;
  if (r.scope === SCOPE_MATCH) {
    const dot = (name || '').indexOf('.');
    const side = dot < 0 ? name : name.slice(0, dot);
    const field = dot < 0 ? '' : name.slice(dot + 1);
    return base === GV_KEY_SITE || base === GV_KEY_EVENT || base === GV_KEY_SCORE
      || base === GV_KEY_ANNOTATIONS || base === GV_KEY_VIDEO_URL
      || base.startsWith(GV_KEY_ANALYSIS) || (name !== null && name in GV_MATCH_FIELDS)
      || ((side === 'white_profile' || side === 'black_profile') && field in GV_PROFILE_FIELDS);
  }
  if (r.scope === SCOPE_GAME) return name !== null && name in GV_GAME_FIELDS;
  if (r.scope === SCOPE_PLY) return base === GV_KEY_ILLEGAL_PLY || base.startsWith(GV_KEY_DECISIONS);
  return false;
}

/** The decision kind an analysis object of this ply is, by its action: a dice
 *  play is a checker decision, a cube action a cube decision, a resignation a
 *  resign decision; nothing else is analysed. */
function naturalKind(ply) {
  const action = ply.action_id;
  if (_playsDice(ply)) return KIND_CHECKER;
  if (CUBE_ACTIONS.includes(action)) return KIND_CUBE;
  if (action === ACTION_RESIGN_GAME || action === ACTION_RESIGN_MATCH) return KIND_RESIGN;
  return null;
}

/** `[dict, explicit]`: the part of an analysis object that stands for the
 *  decision `kind` of `ply`, or null where the object holds no such decision. The
 *  object itself is the ply's natural decision; a cube decision on a dice play
 *  is its `missed_double` (else `cube_decision`); a roll is the object too, and
 *  then `explicit` says its records must name `kind`. */
function decisionHolder(ply, obj, kind) {
  const nat = naturalKind(ply);
  if (kind === KIND_ROLL) return nat === KIND_CHECKER && 'luck' in obj ? [obj, true] : null;
  if (kind === nat) {
    if (kind === KIND_CHECKER && !('alternatives' in obj)) return null;
    return [obj, false];
  }
  if (kind === KIND_CUBE && nat === KIND_CHECKER) {
    for (const name of ['missed_double', 'cube_decision']) {
      if (obj[name] !== null && typeof obj[name] === 'object') return [obj[name], false];
    }
  }
  return null;
}

/** Our `x-gammonview` values at `scope`, as a Map of `"ref\0key"` ->
 *  `{ref, key, value}`, with a value split over `key`, `key~1`, ... joined back
 *  and the format prefix checked off. */
function _gvValues(annos, scope) {
  const parts = new Map();
  for (const r of annos) {
    const key = r.key || '';
    if (r.scope !== scope || !key.startsWith('x-gammonview-')) continue;
    const sep = key.indexOf('~');
    const base = sep < 0 ? key : key.slice(0, sep);
    const idx = sep < 0 ? '' : key.slice(sep + 1);
    const k = `${r.ref}\0${base}`;
    if (!parts.has(k)) parts.set(k, { ref: r.ref, key: base, chunks: [] });
    parts.get(k).chunks.push([/^[0-9]+$/.test(idx) ? parseInt(idx, 10) : 0, r.value]);
  }
  const out = new Map();
  for (const [k, { ref, key, chunks }] of parts) {
    chunks.sort((a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
    const value = chunks.map((c) => c[1]).join('');
    if (!value.startsWith(GV_FORMAT)) {
      throw new GvabError(`annotation ${JSON.stringify(key)} is in a format this reader does not know`);
    }
    out.set(k, { ref, key, value: value.slice(GV_FORMAT.length) });
  }
  return out;
}

function _gvGet(map, ref, key) {
  const e = map.get(`${ref}\0${key}`);
  return e === undefined ? undefined : e.value;
}

/** `urllib.parse.unquote`: `%XX` runs decoded as UTF-8, anything else as is. */
function _unquote(s) {
  const bytes = [];
  const enc = new TextEncoder();
  for (let i = 0; i < s.length;) {
    if (s[i] === '%' && /^[0-9a-fA-F]{2}$/.test(s.slice(i + 1, i + 3))) {
      bytes.push(parseInt(s.slice(i + 1, i + 3), 16));
      i += 3;
    } else {
      const cp = s.codePointAt(i);
      const ch = String.fromCodePoint(cp);
      for (const b of enc.encode(ch)) bytes.push(b);
      i += ch.length;
    }
  }
  return new TextDecoder('utf-8').decode(new Uint8Array(bytes));
}

/** Where each alternative of the DECS record sits in the exact list an
 *  annotation of ours stands in for it: the same alternative, by content (two
 *  that are identical are interchangeable). */
function _altMap(mainAlts, exactAlts) {
  const used = new Set();
  const exact = exactAlts.map((a) => JSON.stringify(a));
  return mainAlts.map((m) => {
    const want = JSON.stringify(m);
    for (let i = 0; i < exact.length; i++) {
      if (!used.has(i) && exact[i] === want) {
        used.add(i);
        return i;
      }
    }
    return null;
  });
}

/** `[dict, extra]` an annotation addressed to `kind` (and, where given,
 *  alternative `alt` of the record) belongs on, or null: `extra` is what the
 *  document's record must state besides the annotation itself. */
function _holderOf(ply, obj, kind, alt, amap) {
  if (obj === undefined || obj === null || typeof obj !== 'object') return null;
  const got = decisionHolder(ply, obj, kind);
  if (got === null) return null;
  const [holder, explicit] = got;
  const extra = explicit ? { kind } : {};
  if (alt === null) return [holder, extra];
  if (kind !== KIND_CHECKER) return null;
  if (amap !== null) alt = alt < amap.length ? amap[alt] : null;
  const alts = holder.alternatives;
  if (alt === null || !Array.isArray(alts) || alt >= alts.length) return null;
  return [alts[alt], {}];
}

/** Hang every annotation that is not one of ours on what it addresses -- the
 *  match, a game, a ply, a decision's analysis object or one of its
 *  alternatives -- and mark it `placed`. One that addresses nothing the document
 *  holds (an unknown scope or decision kind, a decision whose block could not be
 *  read) is left for the passthrough record. `fallback` is the list of
 *  annotations ours carried for what ANNO could not address
 *  (`x-gammonview-annotations`); they follow the native ones. */
function _placeAnnotations(ogxm, plyAt, decoded, annos, altMaps, fallback) {
  const objs = new Map(decoded.map(([info, obj]) => [info.analysis_id, obj]));
  const hang = (holder, rec) => {
    if (holder.annotations === undefined) holder.annotations = [];
    holder.annotations.push(rec);
  };
  const games = ogxm.games;
  for (const r of annos) {
    if (annoIsOurs(r)) continue;
    const { scope, ref } = r;
    const rec = annoDoc(r);
    let target = null;
    if (scope === SCOPE_MATCH) target = ogxm;
    else if (scope === SCOPE_GAME && ref < games.length) target = games[ref];
    else if (scope === SCOPE_PLY && ref < plyAt.length) target = plyAt[ref].ply;
    else if ((scope === SCOPE_DECISION || scope === SCOPE_ALTERNATIVE) && ref < plyAt.length
             && objs.has(r.analysis) && r.kind !== undefined
             && (scope === SCOPE_DECISION || r.alt_index !== undefined)) {
      const { key, ply } = plyAt[ref];
      const got = _holderOf(ply, objs.get(r.analysis).get(key), r.kind,
        scope === SCOPE_ALTERNATIVE ? r.alt_index : null,
        altMaps.has(`${r.analysis}\0${ref}`) ? altMaps.get(`${r.analysis}\0${ref}`) : null);
      if (got !== null) {
        [target] = got;
        Object.assign(rec, got[1]);
      }
    }
    if (target !== null) {
      hang(target, rec);
      r.placed = true;
    }
  }

  for (const e of fallback) {
    let target;
    const rec = { ...e.v };
    const bad = () => new GvabError('an annotation of ours addresses nothing in the match');
    if (e.s === SCOPE_MATCH) target = ogxm;
    else if (e.s === SCOPE_GAME) target = games[e.g];
    else if (e.s === SCOPE_PLY) target = (games[e.g] || {}).plies && games[e.g].plies[e.p];
    else {
      const ply = (games[e.g] || {}).plies && games[e.g].plies[e.p];
      const objMap = objs.get(e.a);
      if (!ply || !objMap) throw bad();
      const got = _holderOf(ply, objMap.get(`${e.g},${e.p}`), e.k,
        e.s === SCOPE_ALTERNATIVE ? e.i : null, null);
      if (got === null) throw bad();
      [target] = got;
      Object.assign(rec, got[1]);
    }
    if (!target) throw bad();
    hang(target, rec);
  }
}

/** The CLCK section as `clock_info` and each ply's `timestamp_ms`; an invalid section is
 *  dropped (8.2) and kept only by the passthrough. */
function _docClock(payload, plyAt, ogxm) {
  const got = decodeClock(payload, plyAt.length);
  if (got === null) return;
  ogxm.clock_info = clockDoc(got.header, got.precision);
  got.ts.forEach((t, i) => { plyAt[i].ply.timestamp_ms = t; });
}

/** The VIDO section as `video_info` and its marks on the plies they mark. */
function _docVideo(payload, v2games, ogxm, plyOffset) {
  const got = decodeVideo(payload, v2games.map((g) => g.plies.length));
  if (got === null) return;
  ogxm.video_info = videoDoc(got.header);
  for (const [gi, pi, hand, videoMs, wall, behind] of got.marks) {
    Object.assign(ogxm.games[gi].plies[pi + plyOffset[gi]], videoMarkDoc(hand, videoMs, wall, behind));
  }
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

function _diceActionId(d1, d2) {
  const a = Math.min(d1, d2);
  const b = Math.max(d1, d2);
  return [0, 6, 11, 15, 18, 20][a - 1] + (b - a);
}

/** v1's colour polarity is the Seat enum's reverse: 1 is White [JSON 3.3]. */
function _color(seat) {
  return seat === 0 ? 1 : 0;
}

function _v1Ply(p) {
  const x = p.extras || {};
  const action = p.action;
  if (action === ACTION_RESERVED) throw new GvabError('OGXM v2 uses the reserved action 35');
  const assigned = action <= LAST_KNOWN_ACTION;
  // Each extras field belongs to the actions that name it [5.1].
  const allowed = {
    [ACTION_SET_POSITION]: ['dice', 'illegal', 'board'],
    [ACTION_SETTLE]: ['settle_value'],
    [ACTION_CUBE_SET]: ['cube_value', 'cube_owner'],
    [ACTION_RESIGN_GAME]: ['resign_value'],
    [ACTION_RESIGN_MATCH]: ['resign_value'],
  }[action] || [];
  if (assigned && Object.keys(x).some((k) => !allowed.includes(k) && k !== 'action_ext')) {
    throw new GvabError(`OGXM v2 action ${action} carries extras that do not belong to it`);
  }
  const ply = { color: _color(p.seat), action_id: action };
  if (action <= 20) {
    const [d1, d2] = DICE_TABLE[action];
    ply.d1 = d1;
    ply.d2 = d2;
    ply.moves = p.steps;
  } else if (action === ACTION_SET_POSITION) {
    if (!x.board) throw new GvabError('OGXM v2 set-position ply without its board');
    if (x.dice) {
      ply.d1 = x.dice[0];
      ply.d2 = x.dice[1];
      // A set position with dice restates a play [5.4.7]. Its seat is the side
      // on roll afterwards (M2); our colour is the player who made it.
      ply.color = 1 - ply.color;
    }
    ply.set_position = x.board;
  } else if (action === ACTION_SETTLE) {
    if (x.settle_value === undefined) throw new GvabError('OGXM v2 settle ply without its value');
    ply.settle_value = x.settle_value;
  } else if (action === ACTION_CUBE_SET) {
    if (x.cube_value === undefined) throw new GvabError('OGXM v2 cube-set ply without its value');
    ply.cube_value = x.cube_value;
    if (x.cube_owner !== undefined) ply.cube_owner = x.cube_owner;
  } else if (action > LAST_KNOWN_ACTION && p.extras_raw) {
    ply.extras_raw = _b64encode(p.extras_raw);
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

/** The factor a resignation is worth, as the writer derives it when the
 *  document states none: `points_won` over the cube, or 1. */
export function derivedResignValue(pointsWon, cube) {
  if (cube > 0 && pointsWon % cube === 0 && pointsWon / cube >= 1 && pointsWon / cube <= 3) {
    return pointsWon / cube;
  }
  return 1;
}

/** The cube's value after a ply [M6]. A double changes nothing until it is
 *  answered; a drop ends the game. */
function _cubeAfter(cube, action, ply) {
  if (action === ACTION_TAKE) return cube * 2;
  if (action === ACTION_BEAVER) return cube * 4;
  if (action === ACTION_RACCOON) return cube * 2;
  if (action === ACTION_CUBE_SET) return Number(ply.cube_value || cube);
  return cube;
}

function _v1Match(mtch, v2games) {
  const { starts, final } = _scoreWalk(mtch, v2games);
  const crawford = _crawfordGames(mtch, starts);
  const [whiteScore, blackScore] = mtch.score_final || final;
  let result = mtch.result;
  if (result === undefined) {   // 3 (abandoned) is stored, and read back as stored
    const length = mtch.match_length;
    result = length > 0 && whiteScore >= length ? 1
      : length > 0 && blackScore >= length ? 2 : 0;
  }
  const rules = mtch.rules || 0;

  const games = v2games.map((g, gi) => {
    const plies = g.plies.map(_v1Ply);
    // A resignation's factor is derived from the points and the cube; one that
    // is not what the writer would derive is kept on its ply.
    let cube = (g.initial_cube_value || 1) * 2 ** (g.auto_doubles || 0);
    plies.forEach((ply, pi) => {
      if (ply.action_id === ACTION_RESIGN_GAME || ply.action_id === ACTION_RESIGN_MATCH) {
        const stated = (g.plies[pi].extras || {}).resign_value;
        if (stated !== undefined && stated !== derivedResignValue(g.points_won || 0, cube)) {
          ply.resign_value = stated;
        }
      }
      cube = _cubeAfter(cube, ply.action_id, ply);
    });
    // A marker has no actor (its seat is always 0); our documents stamp it with
    // the game's winner, as every converter here does.
    if (g.winner === 0 || g.winner === 1) {
      for (const p of plies) {
        if (MARKER_ACTIONS.includes(p.action_id)) p.color = _color(g.winner);
      }
    }
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
    for (const key of Object.keys(GV_GAME_FIELDS)) {
      if (g[key] !== undefined) game[key] = g[key];
    }
    return game;
  });
  const plyOffset = v2games.map((g) => (g.initial_board ? 1 : 0));

  const top = {
    match_length: mtch.match_length,
    player_white: mtch.white_name || '',
    player_black: mtch.black_name || '',
    white_score: whiteScore,
    black_score: blackScore,
    result,
    source: mtch.source || 0,
    timestamp: mtch.started_at !== undefined ? Number(mtch.started_at / 1000n) : 0,
    crawford: Boolean(rules & 0x01),
    jacoby: Boolean(rules & 0x02),
    beaver: Boolean(rules & 0x04),
    raccoon: Boolean(rules & 0x08),
    cube_limit: mtch.cube_limit || 0,
    event: mtch.event || null,
    // Our `site` is where the match was played: v2's `city`, else the platform
    // it was played on (`_readOgxm2` settles it, an annotation of ours winning
    // over both).
    site: null,
  };
  if (mtch.variant) top.variant = mtch.variant;
  if (rules & 0x10) top.auto_doubles = true;
  if (rules & ~0x1F) top.rules_other = rules & ~0x1F;
  if (mtch.score_start) top.score_start = mtch.score_start.slice();
  for (const key of ['completed_at', 'player_seat', 'crawford_before_start', 'event_year',
    'date_precision', 'stage', 'round', 'table', 'city', 'country', 'event_url',
    'match_ref', 'white_profile', 'black_profile', 'rated']) {
    if (mtch[key] !== undefined) top[key] = mtch[key];
  }
  if (mtch.site !== undefined) top.platform = mtch.site;   // v2's `site` is the platform's host name
  top.games = games;
  return [top, plyOffset];
}

/** How a value v2 cannot hold is spelled in an annotation of ours. */
export function numberText(x) {
  return String(Number(x));
}

function _gvText(kind, value) {
  if (kind === 's') return String(value);
  if (kind === 'i') return String(Math.trunc(Number(value)));
  return numberText(value);
}

function _gvParse(kind, text, what) {
  if (kind === 's') return text;
  const ok = kind === 'i' ? /^\s*[+-]?\d+\s*$/.test(text)
    : /^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$/.test(text);
  if (!ok) throw new GvabError(`annotation ${what} is malformed`);
  return kind === 'i' ? parseInt(text, 10) : parseFloat(text);
}

/** The block fields an `x-gammonview-analysis` annotation carries because v2
 *  could not hold them: `<field>=<value>`, `dials.<name>=<value>`, and a list as
 *  its elements, each quoted, joined by commas. */
function _blockItems(items) {
  const out = {};
  for (const [name, kind] of Object.entries(GV_BLOCK_FIELDS)) {
    if (!items.has(name)) continue;
    out[name] = kind === 'l' ? items.get(name).split(',').map(_unquote)
      : _gvParse(kind, _unquote(items.get(name)), name);
  }
  const dials = {};
  for (const [name, kind] of Object.entries(GV_DIAL_FIELDS)) {
    if (!items.has(`dials.${name}`)) continue;
    const text = _unquote(items.get(`dials.${name}`));
    dials[name] = kind === 'b' ? text === '1' : _gvParse(kind, text, name);
  }
  if (Object.keys(dials).length) out.dials = dials;
  return out;
}

/** Put back, from `x-gammonview-<prefix><field>` annotations at `ref`, the
 *  values the writer could not store in v2's own fields. */
function _restoreFields(target, gv, ref, fields, prefix = '') {
  for (const [name, kind] of Object.entries(fields)) {
    const key = GV_PREFIX + prefix + name;
    const text = _gvGet(gv, ref, key);
    if (text !== undefined) target[name] = _gvParse(kind, text, key);
  }
}

// ---------------------------------------------------------------------------
// Analysis, in v1's shape
// ---------------------------------------------------------------------------

const _r4 = (v) => Math.round(v * 10000) / 10000;
const _identity = { toEquity: _r4, toDelta: _r4 };

/** A level's display label. Our own blocks name it in `preset` (`3ply`,
 *  `truncated2`, `rollout`); a foreign block's preset is the producer's own
 *  vocabulary, so its label comes from the depth instead. */
function _label(level, ours) {
  if (ours && level.preset !== undefined) return level.preset;
  return _levelLabel(level);
}

/** The checker play at a dice ply, as `_buildCheckerAnalysis` would give it. */
function _checker(d, blockLevel, frame, ours = false) {
  const level = _resolve(blockLevel, d.level);
  const alts = d.alternatives || [];
  const levels = alts.map((a) => _resolve(level, a.level));
  const split = ours || levels.some((l) => _levelLabel(l) !== _levelLabel(levels[0]));

  const bestMwc = d.best_equity !== undefined ? d.best_equity
    : (alts.length ? alts[0].equity : 0);
  const played = alts.find((a) => a.is_played);
  // A derived loss [7.1] is the difference of the stored values, taken before
  // either is rounded to the four places a document holds.
  const lossMwc = d.equity_loss !== undefined ? d.equity_loss
    : (played ? bestMwc - played.equity : 0);

  const best = frame.toEquity(bestMwc);
  const loss = Math.max(0, frame.toDelta(lossMwc));
  const analysis = {
    best_equity: best,
    played_equity: _r4(best - loss),
    equity_loss: loss,
    decision: false,
    alternatives: alts.map((a, i) => {
      const out = { move: a.steps, equity: frame.toEquity(a.equity), is_played: Boolean(a.is_played) };
      if (a.probs) out.eval = _evalFromProbs(...a.probs);
      if (split) {
        const label = _label(levels[i], ours);
        if (label !== null) out.eval_level = label;
      }
      if (a.rollout_se !== undefined) out.rollout_se = frame.toDelta(a.rollout_se);
      if (a.cubeless_equity !== undefined) out.cubeless_equity = frame.toEquity(a.cubeless_equity);
      out[_LV] = levels[i];                      // the true level, for `finishLevels`
      return out;
    }),
  };
  const top = analysis.alternatives[0];
  if (top && top.eval) analysis.eval = { ...top.eval };
  if (level.checker_ply !== undefined && level.checker_ply !== blockLevel.checker_ply) {
    analysis.ply = level.checker_ply;
  }
  for (const k of CHECKER_KEYS) if (d[k] !== undefined) analysis[k] = d[k];
  analysis[_LV] = [KIND_CHECKER, level];
  return analysis;
}

function _cubeTriple(d, frame) {
  return {
    no_double_equity: frame.toEquity(d.no_double_equity || 0),
    double_take_equity: frame.toEquity(d.double_take_equity || 0),
    double_pass_equity: frame.toEquity(d.double_pass_equity || 0),
  };
}

/** The cube record's own fields beyond the triple, on `target`. */
function _cubeExtras(d, frame, target, level) {
  for (const k of CUBE_KEYS) if (d[k] !== undefined) target[k] = d[k];
  if (d.cubeful_take_value !== undefined) {
    target.cubeful_take_value = frame.toEquity(d.cubeful_take_value);
  }
  if (d.currency !== undefined) target.currency = d.currency;
  target[_LV] = [KIND_CUBE, level];
}

/** The cube decision at a dice ply: `missed_double` + `cube_decision`, or the
 *  live `cube_decision` alone -- the two shapes of `_buildMissedDouble` /
 *  `_buildCubeDecision` in reader.js. */
function _liveCube(d, frame, analysis, label = null, level = null) {
  const triple = _cubeTriple(d, frame);
  const evalObj = d.probs ? _evalFromProbs(...d.probs) : null;
  const shouldDouble = d.verdict === 1;
  const live = {
    should_double: shouldDouble,
    ...triple,
    action: _cubeActionLabel(shouldDouble, triple.double_take_equity, triple.double_pass_equity),
  };
  if (evalObj) live.eval = evalObj;
  if (label !== null) live.eval_level = label;
  if (d.equity_loss !== undefined) {
    const missed = {
      ...triple,
      equity_loss: Math.max(0, frame.toDelta(d.equity_loss)),
      correct_action: VERDICT_NAMES[d.verdict] || 'double',
    };
    if (evalObj) missed.eval = { ...evalObj };
    if (label !== null) missed.eval_level = label;
    _cubeExtras(d, frame, missed, level);
    analysis.missed_double = missed;
  } else {
    live.decision = false;
  }
  _cubeExtras(d, frame, live, level);
  analysis.cube_decision = live;
}

/** A cube ply's own decision (double, take, drop). */
function _cubePly(d, action, blockLevel, frame, ours = false) {
  const level = _resolve(blockLevel, d.level);
  const analysis = {
    correct_action: VERDICT_NAMES[d.verdict] || 'no_double',
    played_action: PLAYED_ACTION[action] || 'pass',
    ..._cubeTriple(d, frame),
    equity_loss: Math.max(0, frame.toDelta(d.equity_loss || 0)),
    decision: false,
  };
  if (d.probs) analysis.eval = _evalFromProbs(...d.probs);
  if (ours && level.preset !== undefined) analysis.eval_level = level.preset;
  if (level.cube_ply !== undefined && level.cube_ply !== blockLevel.cube_ply) {
    analysis.ply = level.cube_ply;
  }
  _cubeExtras(d, frame, analysis, level);
  return analysis;
}

function _resign(d, frame, level) {
  const analysis = {
    resign_error: frame.toDelta(d.resign_error || 0),
    take_resign_error: frame.toDelta(d.take_resign_error || 0),
    equity_loss: Math.max(0, frame.toDelta(d.equity_loss || 0)),
    decision: false,
  };
  if (d.probs) analysis.eval = _evalFromProbs(...d.probs);
  for (const k of RESIGN_KEYS) if (d[k] !== undefined) analysis[k] = d[k];
  analysis[_LV] = [KIND_RESIGN, level];
  return analysis;
}

/** A dice action, or an illegal play restated as a set position. */
function _playsDice(ply) {
  const action = ply.action_id;
  return action <= 20 || (action === ACTION_SET_POSITION && Boolean(ply.d1));
}

/**
 * Set the flags v2 has no field for to what a block of ours holds unless it
 * says otherwise (`x-gammonview-analysis`): `illegal_move` on a checker analysis
 * of an illegal play, the PR-counting `decision` by the rule `basefill.js`
 * applies to a foreign block, and a live cube's own `decision` by triviality.
 * Shared with the writer, which records only where its document disagrees.
 */
export function defaultFlags(ply, analysis, illegalPlay) {
  const action = ply.action_id;
  if (illegalPlay && 'alternatives' in analysis) analysis.illegal_move = true;
  if (_playsDice(ply)) {
    analysis.decision = 'alternatives' in analysis && _checkerIsDecision(analysis);
  } else if (CUBE_ACTIONS.includes(action)) {
    analysis.decision = _cubePlyIsDecision(ply, analysis);
  } else if (action === ACTION_RESIGN_GAME || action === ACTION_RESIGN_MATCH) {
    analysis.decision = true;
  }
  const live = analysis.cube_decision;
  if (live !== null && typeof live === 'object' && !('missed_double' in analysis)) {
    live.decision = !trivialCube(
      live.no_double_equity, live.double_take_equity, live.double_pass_equity);
  }
}

function _applyExceptions(analysis, letters) {
  if (letters.has('c')) analysis.decision = !analysis.decision;
  if (letters.has('l') && analysis.cube_decision !== null && typeof analysis.cube_decision === 'object') {
    const live = analysis.cube_decision;
    live.decision = !live.decision;
  }
  if (letters.has('i')) {
    if (analysis.illegal_move) delete analysis.illegal_move;
    else analysis.illegal_move = true;
  }
}

/**
 * A source `[mid, half]` from the strings `x-gammonview-analysis`'s `frame=`
 * item holds, in the perspective of a ply whose frame owner is White (or not).
 * Rounded to the eight places the item carries, so a document that states a
 * frame and one read back from it agree exactly.
 */
export function frameFromWire(midWhite, half, white) {
  const mid = Number(midWhite);
  return [Number((white ? mid : 1 - mid).toFixed(8)), Number(Number(half).toFixed(8))];
}

/**
 * `frame=`: `<ply_ref>:<mid_white>:<half>` entries, each in force from its ply
 * until the next; `<ply_ref>:` ends a run with no source frame. Returns
 * `[[ref, [mid_white, half] | null], ...]` sorted by ref.
 */
export function _parseFrames(value) {
  const out = [];
  for (const entry of value.split(',')) {
    if (!entry) continue;
    const [ref, ...rest] = entry.split(':');
    const mid = rest[0] || '';
    out.push([parseInt(ref, 10), mid ? [mid, rest[1] || ''] : null]);
  }
  return out.sort((x, y) => x[0] - y[0]);
}

/** The `[mid_white, half]` in force at `ref`, or null. */
function _frameInForce(frames, ref) {
  let state = null;
  for (const [at, st] of frames) {
    if (at > ref) break;
    state = st;
  }
  return state;
}

/**
 * One v2 analysis block -> `[analysisInfo, Map<"gi,pi", analysis>, hasLuck]`.
 * `ours` is set for a block our writer made: `{extra: Map<ply_ref, records>,
 * exceptions: Set<token>, illegal: Set<ply_ref>, frames}` -- the annotation's
 * decision records, which stand in for `DECS`'s at their ply and kind, the
 * flags to flip, and the source frames its values were converted through.
 *
 * @param {object[]} plyAt  ply_ref -> {key, ply} (ply already carries its OGIDs)
 */
export function _v1Block(anal, decisions, plyAt, matchLength, ours = null) {
  const blockLevel = _resolve({}, anal.level);
  const blockMwc = matchLength > 0 && anal.currency === CURRENCY_CUBEFUL_MATCH;
  const blockObj = new Map();
  let luckLevels = new Set();
  const mine = ours !== null;

  if (mine && ours.extra.size) {
    const replaced = new Set();
    for (const [ref, recs] of ours.extra) for (const d of recs) replaced.add(`${ref},${d.kind}`);
    const merged = decisions.filter((d) => !replaced.has(`${d.ply_ref},${d.kind}`));
    for (const recs of ours.extra.values()) merged.push(...recs);
    decisions = merged.sort((x, y) => x.ply_ref - y.ply_ref || x.kind - y.kind);
  }

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
    // decision is dropped rather than shown in the wrong unit. A block that
    // states the source's own frame for the ply converts through that, not
    // through our table.
    const wire = mine && mwc ? _frameInForce(ours.frames, d.ply_ref) : null;
    const source = wire !== null
      ? frameFromWire(wire[0], wire[1], framePerspectiveIsWhite(ply)) : null;
    const frame = mwc ? mwcFrame(ply, source) : _identity;
    if (frame === null) continue;

    const action = ply.action_id;
    const dice = _playsDice(ply);
    if (d.kind === KIND_CHECKER && dice) {
      const prior = blockObj.get(key) || {};
      blockObj.set(key, { ..._checker(d, blockLevel, frame, mine), ...prior });
    } else if (d.kind === KIND_CUBE && dice) {
      const lv = _resolve(blockLevel, d.level);
      const label = mine ? _label(lv, true) : null;
      if (!blockObj.has(key)) blockObj.set(key, {});
      _liveCube(d, frame, blockObj.get(key), label === undefined ? null : label, lv);
    } else if (d.kind === KIND_CUBE && CUBE_ACTIONS.includes(action)) {
      blockObj.set(key, _cubePly(d, action, blockLevel, frame, mine));
    } else if (d.kind === KIND_ROLL && dice) {
      // From the roller's side, in the block's currency [7.6] -- a difference
      // of two MWCs, so it converts by the frame's slope alone.
      if (!blockObj.has(key)) blockObj.set(key, { decision: false });
      const obj = blockObj.get(key);
      obj.luck = frame.toDelta(d.luck);
      if (d.producer_ref !== undefined) obj.luck_producer_ref = d.producer_ref;
      const lv = _resolve(blockLevel, d.level);
      obj[_LV_LUCK] = lv;
      luckLevels.add(_label(lv, mine));
    } else if (d.kind === KIND_RESIGN
               && (action === ACTION_RESIGN_GAME || action === ACTION_RESIGN_MATCH)) {
      blockObj.set(key, _resign(d, frame, _resolve(blockLevel, d.level)));
    }
  }

  if (mine) {
    const refOf = new Map(plyAt.map((at, i) => [at.key, i]));
    const flips = new Map();
    for (const token of ours.exceptions) {
      const ref = parseInt(token.slice(0, -1), 10);
      if (!flips.has(ref)) flips.set(ref, new Set());
      flips.get(ref).add(token[token.length - 1]);
    }
    for (const [key, obj] of blockObj) {
      const ref = refOf.get(key);
      defaultFlags(plyAt[ref].ply, obj, ours.illegal.has(ref));
      _applyExceptions(obj, flips.get(ref) || new Set());
      const wire = blockMwc ? _frameInForce(ours.frames, ref) : null;
      if (wire !== null) {
        obj.mwc_frame = frameFromWire(wire[0], wire[1], framePerspectiveIsWhite(plyAt[ref].ply));
      }
    }
  }

  const info = { ply: blockLevel.checker_ply || 0 };
  if (mine) {
    // The block's own label travels in its annotation when it is not the level
    // most decisions share, which is what v2's block level states.
    const label = 'level' in ours ? ours.level : (blockLevel.preset === undefined ? null : blockLevel.preset);
    if (label !== null) info.eval_level = label;
    if (luckLevels.size && ours.luck) luckLevels = new Set([ours.luck]);
  } else if (blockLevel.rollout) {
    info.eval_level = 'rollout';
  }
  if (anal.model_id !== undefined) info.model_id = anal.model_id;
  if (anal.met_id) info.met_id = anal.met_id;
  info.timestamp = anal.started_at !== undefined ? Number(anal.started_at / 1000n) : 0;
  if (anal.duration_ms) info.duration_ms = anal.duration_ms;
  if (luckLevels.size === 1) {
    const label = luckLevels.values().next().value;
    if (label !== null) info.luck_eval_level = label;
  }
  // Every block states its identifier, not only ours: a signature, and an
  // annotation addressed to a decision, name the block by it.
  info.analysis_id = anal.analysis_id;
  Object.assign(info, _blockFields(anal, ours, plyAt, matchLength));
  if (mine && ours.fields && ours.fields.model_id !== undefined) {
    info.model_id = ours.fields.model_id;        // a value v2 could not hold
  }
  info[_LV] = blockLevel;                        // for `finishLevels`, once the block is complete
  return [info, blockObj, luckLevels.size > 0];
}

// The block fields that are v2's own, in the order a document lists them.
const BLOCK_KEYS = ['producer', 'complete', 'coverage', 'model_name', 'model_digest',
  'engine_build', 'currency', 'cube_efficiency', 'tables', 'dials', 'completed_at', 'sources'];
// How each is spelled in an `x-gammonview-analysis` item when v2 cannot hold it:
// `s` as is, `i` an integer, `f` a number, `l` a list of strings.
const GV_BLOCK_FIELDS = {
  producer: 'i', model_id: 's', model_name: 's', model_digest: 's',
  engine_build: 's', tables: 's', completed_at: 'i', sources: 'l',
};
const GV_DIAL_FIELDS = Object.fromEntries(DIAL_FIELDS.map(([, name]) => [
  name, DIAL_FLAGS.includes(name) ? 'b' : name === 'top_deep_threshold' ? 'f' : 'i']));

/** The currency a block is written in unless its document says another: a
 *  match's equities are match winning chances, a money game's cubeful money. */
export function defaultCurrency(matchLength) {
  return matchLength > 0 ? CURRENCY_CUBEFUL_MATCH : CURRENCY_CUBEFUL_MONEY;
}

/** The keys v2's block record adds to `analysis_info`: the file's, with an
 *  annotation of ours standing in for a value v2 could not hold. */
function _blockFields(anal, ours, plyAt, matchLength) {
  const have = {};
  for (const k of BLOCK_KEYS) if (anal[k] !== undefined) have[k] = anal[k];
  if (have.coverage !== undefined) {
    for (const ref of have.coverage) {
      if (ref >= plyAt.length) {
        throw new GvabError(`OGXM v2 coverage names ply ${ref}, past the end of the match`);
      }
    }
    have.coverage = have.coverage.map((ref) => plyAt[ref].key.split(',').map(Number));
  }
  if (have.currency === defaultCurrency(matchLength)) delete have.currency;   // the default is not a key
  if (ours !== null && ours.fields) {
    for (const [k, v] of Object.entries(ours.fields)) {
      if (k === 'dials') have.dials = { ...(have.dials || {}), ...v };
      else if (BLOCK_KEYS.includes(k)) have[k] = v;
    }
  }
  const out = {};
  for (const k of BLOCK_KEYS) if (have[k] !== undefined) out[k] = have[k];
  return out;
}

/**
 * Settle which tiers keep a `level` object, and remove the markers.
 *
 * A document states a level by its labels (`eval_level`, `ply`), which is all
 * its own analyses need. A tier whose true level is something else -- a rollout,
 * a preset the producer named, a depth its labels do not reproduce -- keeps the
 * true level as `level`, resolved against the tier above, so the key appears
 * exactly where the labels would give a different level. A preset that only
 * restates the depth is no difference (`sameLevel`): producers name a plain
 * 3-ply search `3ply`, and a document would otherwise carry that on every
 * decision.
 *
 * Runs once the block is complete -- `basefill` settles `ply` and `eval_level`
 * on a foreign block, and the labels are what the writer will derive from.
 */
export function finishLevels(info, blockObj) {
  const bt = info[_LV];
  delete info[_LV];
  const objs = [...blockObj.values()];
  const bd = blockLevelOf(info, objs);
  let parent;
  if (sameLevel(bd, bt)) {
    parent = bd;
  } else {
    info.level = _clone(bt);
    parent = bt;
  }
  const luckLabel = info.luck_eval_level || '1ply';

  // The level the writer will have for this tier.
  const settle = (truth, derived, target, key = 'level') => {
    if (sameLevel(truth, derived)) return derived;
    target[key] = _clone(truth);
    return truth;
  };

  for (const obj of objs) {
    const marker = obj[_LV];
    const luck = obj[_LV_LUCK];
    delete obj[_LV];
    delete obj[_LV_LUCK];
    if (marker !== undefined) {
      const [kind, level] = marker;
      if (kind === KIND_CHECKER) {
        const alts = obj.alternatives || [];
        const derived = tierLevel(parent, {
          preset: alts.length ? alts[0].eval_level : null, checker_ply: obj.ply });
        const eff = settle(level, derived, obj);
        for (const alt of alts) {
          const lv = alt[_LV];
          delete alt[_LV];
          if (lv !== undefined) settle(lv, tierLevel(eff, { preset: alt.eval_level }), alt);
        }
      } else if (kind === KIND_CUBE) {
        settle(level, tierLevel(parent, { preset: obj.eval_level, cube_ply: obj.ply }), obj);
      } else {
        settle(level, parent, obj);
      }
    }
    for (const sub of [obj.cube_decision, obj.missed_double]) {
      if (sub !== null && typeof sub === 'object' && sub[_LV] !== undefined) {
        const [, level] = sub[_LV];
        delete sub[_LV];
        settle(level, tierLevel(parent, { preset: sub.eval_level }), sub);
      }
    }
    if (luck !== undefined) settle(luck, _luckLevelOf(parent, luckLabel), obj, 'luck_level');
  }
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
  let annos = [];
  let clck = null;
  let vido = null;
  for (const s of sections) {
    if (s.type === 'MTCH') mtch = _decodeMtch(s.payload);
    else if (s.type === 'GAME') games.push(_decodeGame(s.payload));
    else if (s.type === 'ANAL') blocks.push({ anal: _decodeAnal(s.payload), decisions: [] });
    else if (s.type === 'DECS') {
      if (!blocks.length) throw new GvabError('OGXM v2 DECS with no ANAL before it');
      blocks[blocks.length - 1].decisions = _decodeDecs(s.payload);
    } else if (s.type === 'ANNO') annos = _decodeAnno(s.payload);
    else if (s.type === 'CLCK') clck = s.payload;
    else if (s.type === 'VIDO') vido = s.payload;
  }
  if (mtch === null) {
    // An Analysis-shape file: evaluations of a match it does not contain.
    throw new GvabError('This file holds an analysis without its match, so there is nothing to show.');
  }

  const [ogxm, plyOffset] = _v1Match(mtch, games);
  const gvMatch = _gvValues(annos, SCOPE_MATCH);
  const gvGame = _gvValues(annos, SCOPE_GAME);
  const gvPly = _gvValues(annos, SCOPE_PLY);
  const event = _gvGet(gvMatch, 0, GV_KEY_EVENT);
  if (event !== undefined) ogxm.event = event;
  _restoreFields(ogxm, gvMatch, 0, GV_MATCH_FIELDS);
  for (const side of ['white', 'black']) {
    const name = `${side}_profile`;
    const restored = {};
    _restoreFields(restored, gvMatch, 0, GV_PROFILE_FIELDS, `${name}.`);
    if (Object.keys(restored).length) ogxm[name] = { ...(ogxm[name] || {}), ...restored };
  }
  ogxm.games.forEach((game, gi) => _restoreFields(game, gvGame, gi, GV_GAME_FIELDS));
  // `site` is where the match was played: our annotation, else v2's `city`,
  // else the platform's host name.
  const site = _gvGet(gvMatch, 0, GV_KEY_SITE);
  ogxm.site = site !== undefined ? site : (ogxm.city || ogxm.platform || null);
  const score = _gvGet(gvMatch, 0, GV_KEY_SCORE);
  if (score !== undefined) {
    const parts = score.split(',');
    if (parts.length !== 2 || !parts.every((v) => /^\s*[+-]?\d+\s*$/.test(v))) {
      throw new GvabError('the stated-score annotation is malformed');
    }
    ogxm.white_score = parseInt(parts[0], 10);
    ogxm.black_score = parseInt(parts[1], 10);
  }

  // ply_ref [I2] is the ply's ordinal across the whole match, in v2's plies --
  // which a synthetic set-position ply is not one of.
  const plyAt = [];
  const plyByKey = new Map();
  const illegalRefs = new Set();
  ogxm.games.forEach((g, gi) => g.plies.forEach((ply, pi) => {
    const key = `${gi},${pi}`;
    if (pi >= plyOffset[gi]) {
      const v2ply = games[gi].plies[pi - plyOffset[gi]];
      if ((v2ply.extras || {}).illegal) illegalRefs.add(plyAt.length);
      plyAt.push({ key, ply });
    }
    plyByKey.set(key, ply);
  }));

  // An illegal play our document held as a dice ply: v2 states it as the board
  // it produced, and the annotation keeps the steps that produced it.
  for (const { ref, key, value } of gvPly.values()) {
    if (key !== GV_KEY_ILLEGAL_PLY) continue;
    if (!illegalRefs.has(ref)) {
      throw new GvabError(`ply ${ref} carries an illegal play's steps but is not one`);
    }
    const ply = plyAt[ref].ply;
    const action = _diceActionId(ply.d1, ply.d2);
    const steps = value ? value.split(',').map((s) => s.split('/')) : [];
    const restored = {
      color: ply.color, action_id: action,
      d1: DICE_TABLE[action][0], d2: DICE_TABLE[action][1],
      moves: steps.map(([f, n]) => ({ from: parseInt(f, 10), pips: parseInt(n, 10) })),
    };
    for (const k of Object.keys(ply)) delete ply[k];
    Object.assign(ply, restored);
  }

  if (clck !== null) _docClock(clck, plyAt, ogxm);
  if (vido !== null) {
    _docVideo(vido, games, ogxm, plyOffset);
    const url = _gvGet(gvMatch, 0, GV_KEY_VIDEO_URL);
    if (ogxm.video_info !== undefined && url !== undefined) ogxm.video_info.url = url;
  }

  if (deriveOgids) _deriveOgids(ogxm);

  const decoded = [];
  const baseBlocks = [];
  const oursIds = new Set();
  const altMaps = new Map();
  for (const { anal, decisions } of blocks) {
    const aid = anal.analysis_id;
    let ours = null;
    const marked = _gvGet(gvMatch, 0, GV_KEY_ANALYSIS + aid);
    if (marked !== undefined) {
      oursIds.add(aid);
      const items = new Map();
      for (const item of marked.split(';')) {
        if (!item) continue;
        const eq = item.indexOf('=');
        items.set(eq < 0 ? item : item.slice(0, eq), eq < 0 ? '' : item.slice(eq + 1));
      }
      const extra = new Map();
      for (const { ref, key, value } of gvPly.values()) {
        if (key !== GV_KEY_DECISIONS + aid) continue;
        if (value.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) {
          throw new GvabError(`annotation on ply ${ref} is not base64`);
        }
        const recs = _decodeDecs(_b64decode(value));
        if (recs.some((d) => d.ply_ref !== ref)) {
          throw new GvabError(`annotation on ply ${ref} holds another ply's decision`);
        }
        extra.set(ref, recs);
        // An annotation addresses the record DECS holds, whose alternatives may
        // be fewer, or ordered otherwise, than the exact ones ours keeps.
        const exact = recs.find((d) => d.kind === KIND_CHECKER);
        const main = decisions.find((d) => d.ply_ref === ref && d.kind === KIND_CHECKER);
        if (exact !== undefined && main !== undefined) {
          altMaps.set(`${aid}\0${ref}`, _altMap(main.alternatives || [], exact.alternatives || []));
        }
      }
      ours = {
        extra, illegal: illegalRefs,
        exceptions: new Set((items.get('pr') || '').split(',').filter((t) => t)),
      };
      if (items.has('level')) ours.level = _unquote(items.get('level')) || null;
      if (items.has('luck')) ours.luck = _unquote(items.get('luck'));
      ours.frames = _parseFrames(items.get('frame') || '');
      ours.fields = _blockItems(items);
    }
    const [info, blockObj, hasLuck] = _v1Block(anal, decisions, plyAt, ogxm.match_length, ours);
    if (blockObj.size && ours === null) {
      if (deriveOgids) completeBaseBlock(blockObj, plyByKey, info, { convertUnits: false });
      if (!hasLuck) baseBlocks.push(decoded.length);
    }
    finishLevels(info, blockObj);
    decoded.push([info, blockObj]);
  }
  let fallback = [];
  const carried = _gvGet(gvMatch, 0, GV_KEY_ANNOTATIONS);
  if (carried !== undefined) {
    if (carried.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(carried)) {
      throw new GvabError('the annotations of ours are malformed');
    }
    try {
      fallback = JSON.parse(_utf8.decode(_b64decode(carried)));
    } catch {
      throw new GvabError('the annotations of ours are malformed');
    }
  }
  _placeAnnotations(ogxm, plyAt, decoded, annos, altMaps, fallback);
  _attachBlocks(ogxm, decoded, plyByKey);
  if (baseBlocks.length) ogxm._base_analyses = baseBlocks;
  // What the file holds that this document cannot: kept for the writer.
  attach(ogxm, data, [view.getUint16(6, true), rmin], sections, annos, oursIds);
  return ogxm;
}

export {
  CHECKER_KEYS, _resolve, CUBE_ACTIONS, GV_BLOCK_FIELDS, GV_DIAL_FIELDS, BLOCK_KEYS, DIAL_FIELDS, DIAL_FLAGS, _stable,
  KIND_CHECKER, KIND_CUBE, KIND_RESIGN, KIND_ROLL,
  CURRENCY_CUBEFUL_MONEY, CURRENCY_CUBEFUL_MATCH,
  GV_FORMAT, GV_KEY_ANALYSIS, GV_KEY_DECISIONS, GV_KEY_ILLEGAL_PLY,
  GV_KEY_SITE, GV_KEY_EVENT, GV_KEY_SCORE, GV_KEY_ANNOTATIONS, GV_KEY_VIDEO_URL, V2_FIELD_NAMES,
  SCOPE_DECISION, SCOPE_ALTERNATIVE, naturalKind, decisionHolder, annoDoc, annoIsOurs,
  GV_PREFIX, GV_MATCH_FIELDS, GV_PROFILE_FIELDS,
  GV_GAME_FIELDS, ACTION_DOUBLE, ACTION_TAKE, ACTION_DROP, ACTION_RESIGN_GAME,
  ACTION_RESIGN_MATCH, ACTION_SET_POSITION, ACTION_BEAVER, ACTION_RACCOON, ACTION_SETTLE,
  ACTION_RESERVED, ACTION_CUBE_SET, ACTION_PASS, ACTION_ESCAPE, LAST_KNOWN_ACTION,
  SCOPE_GAME, _gvText, _decodeAnno, _decodeMtch,
  _decodeAnal, _decodeDecs, _scoreWalk, _walkSections, Cursor, KNOWN_SECTIONS,
  SCOPE_MATCH, SCOPE_PLY,
};
