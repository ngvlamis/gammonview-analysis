// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Keeping what someone else wrote in an OGXM v2 file (spec I7).
//
// JavaScript ESM port of gvformat/ogxm2_passthrough.py -- keep the two in sync,
// byte for byte. `docs/OGXM_V2_PROFILE.md` (section 5) is the account for
// readers.
//
// Our document models a match, its analyses, the clock, the video and every
// annotation a player or another tool wrote (`clock_info`, `video_info`, `annotations`).
// What it cannot model -- the signatures, unknown sections, fields and annotation
// scopes this version does not know -- would be dropped by reading a foreign v2
// file and writing it back. `_readOgxm2` therefore attaches `_ogxm2_passthrough`
// to the document, a JSON-safe record of the source's own bytes, and
// `ogxm2_writer` consults it.
//
// Fingerprints, not trust. A signature digests the bytes as stored, so the only
// way to keep one valid is to write back the stored bytes -- and the only safe
// time to do that is when the document still says what they say. The reader
// stamps each part (MTCH, each GAME, each analysis block) with the SHA-256 of our
// writer's canonical encoding of that part, computed from the document it has
// just built. The clock, the video and each annotation are parts too: they are
// encoded from the document, and the source's bytes go out instead only while
// the document still encodes to what the source decoded to. The writer encodes the document again; a part whose canonical
// bytes hash to the stored fingerprint has not been edited, and its original
// bytes are emitted instead. An edit changes the canonical bytes, so the original
// is not used and nothing stale is ever written.
//
// The edit cases, in order of damage:
//  * nothing changed, or blocks added or removed: every original part verbatim,
//    signatures and all;
//  * MTCH changed (a metadata edit): MTCH is re-encoded keeping every field the
//    document does not model; MSIG and SIGN go, since they cover it;
//    `match_digest` inside a kept ANAL is recomputed;
//  * a GAME changed (a move edit): the foreign analysis blocks go, since they
//    were made over the old moves, and with them SIGN and MSIG. The clock, the
//    video and the annotations travel with their plies in the document, so they
//    are written from it and survive.
//
// A part is dropped only when keeping it would write a file that lies. A clock
// that cannot be written for lack of a reading is dropped (profile section 5).

import { _b64encode, _b64decode } from './binary.js';
import { sha256 } from './sha256.js';
import { CHUNK_SIGN, CHUNK_CLCK, CHUNK_VIDO } from './constants.js';
import {
  Cursor, KNOWN_SECTIONS, annoIsOurs, uuidOf, _decodeAnal, _decodeDecs,
} from './ogxm2.js';
import {
  _encode, _assemble, _varint, _record, _uuidBytes, _cmpBytes, annoCanon, plyLayout,
} from './ogxm2_writer.js';

export const KEY = '_ogxm2_passthrough';
const VERSION = 1;
export const CLOCK_PRECISION = 10;

// Document keys each group of MTCH fields is read into (see _v1Match); a group
// whose keys still equal what the source stated is the source's. Fields that
// constrain one another (a year needs its event, a seat its profile's kind)
// share a group, so an edit to one can never leave the other invalid.
const MTCH_DOC_KEYS = ['player_white', 'player_black', 'crawford', 'jacoby', 'beaver', 'raccoon',
  'auto_doubles', 'rules_other', 'cube_limit', 'score_start', 'result', 'white_score',
  'black_score', 'source', 'timestamp', 'date_precision', 'completed_at',
  'crawford_before_start', 'event', 'event_year', 'stage', 'round', 'table', 'city', 'country',
  'event_url', 'player_seat', 'platform', 'match_ref', 'white_profile', 'black_profile', 'rated'];
const OWNED = [
  [[0], ['player_white']], [[1], ['player_black']],
  [[2], ['crawford', 'jacoby', 'beaver', 'raccoon', 'auto_doubles', 'rules_other']],
  [[3], ['cube_limit']],
  [[4, 5, 6], ['score_start', 'result', 'white_score', 'black_score']],
  [[7], ['source']], [[8, 14], ['timestamp', 'date_precision']],
  [[9], ['completed_at']], [[11], ['crawford_before_start']],
  [[12, 13], ['event', 'event_year']], [[15], ['stage']], [[16], ['round']],
  [[17], ['table']], [[18], ['city']], [[19], ['country']], [[20], ['event_url']],
  [[10, 21, 22, 23, 24], ['player_seat', 'platform', 'match_ref', 'white_profile',
    'black_profile']],
  [[25], ['rated']],
];

// How each MTCH field (spec 4) is laid out: s string, v varint, w varint64,
// p a pair of varints, f flag, r nested record.
const MTCH_KINDS = 'ssvvppvvwwvfsvvsvssssssrrf';          // bits 0-25

const _enc = new TextEncoder();
const _dec = new TextDecoder('utf-8', { fatal: true });

export const b64e = _b64encode;
export const b64d = _b64decode;

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

function _u32(n) {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setUint32(0, n, true);
  return b;
}

function _hex(b) {
  return Array.from(b, (v) => v.toString(16).padStart(2, '0')).join('');
}

function _eq(a, b) {
  return a.length === b.length && _cmpBytes(a, b) === 0;
}

/** SHA-256 over length-prefixed chunks, as hex: two parts that happen to join
 *  into the same bytes still differ. */
export function fingerprint(...chunks) {
  return _hex(sha256(_cat(...chunks.map((c) => _cat(_u32(c.length), c)))));
}

/** Spec 6.1: SHA-256 over MTCH and every GAME payload, each preceded by its
 *  length as a uint32. */
export function matchDigest(mtch, games) {
  return sha256(_cat(...[mtch, ...games].map((c) => _cat(_u32(c.length), c))));
}

function _varintBig(n) {
  const out = [];
  for (;;) {
    const b = Number(n & 0x7Fn);
    n >>= 7n;
    if (n) {
      out.push(b | 0x80);
    } else {
      out.push(b);
      return Uint8Array.from(out);
    }
  }
}

// ---------------------------------------------------------------------------
// MTCH, field by field
// ---------------------------------------------------------------------------

/** `{mandatory, fields: {bit: bytes}, unknownMask (BigInt), tail}` of an MTCH
 *  record: the two mandatory fields' bytes, each known optional field's bytes,
 *  and the unknown run at the record's end (3.2) with its mask bits. */
export function splitMtch(payload) {
  const cur = new Cursor(payload, 0, payload.length);
  const length = cur.varint();
  const start = cur.pos;
  const body = new Cursor(payload, start, start + length);
  const mask = body.varint64();
  const m0 = body.pos;
  body.varint();
  body.varint();
  const mandatory = payload.slice(m0, body.pos);
  const fields = {};
  for (let bit = 0; bit < MTCH_KINDS.length; bit++) {
    if (!((mask >> BigInt(bit)) & 1n)) continue;
    const s = body.pos;
    const kind = MTCH_KINDS[bit];
    if (kind === 's') body.str();
    else if (kind === 'v') body.varint();
    else if (kind === 'w') body.varint64();
    else if (kind === 'p') { body.varint(); body.varint(); } else if (kind === 'r') body.skip(body.varint());
    fields[bit] = payload.slice(s, body.pos);
  }
  const known = (1n << BigInt(MTCH_KINDS.length)) - 1n;
  return {
    mandatory, fields, unknownMask: mask & ~known, tail: payload.slice(body.pos, start + length),
  };
}

function _mtchRecord(mandatory, fields, unknownMask, tail) {
  let mask = unknownMask;
  const body = [mandatory];
  for (const bit of Object.keys(fields).map(Number).sort((a, b) => a - b)) {
    mask |= 1n << BigInt(bit);
    body.push(fields[bit]);
  }
  const inner = _cat(_varintBig(mask), ...body, tail);
  return _cat(_varint(inner.length), inner);
}

const _nul = (v) => (v === undefined ? null : v);

/** Whether two document values are the same (arrays and objects by content). */
function _same(a, b) {
  return JSON.stringify(_nul(a)) === JSON.stringify(_nul(b));
}

/** MTCH after an edit: ours for what the document says differently from when it
 *  was read, the source's for the rest -- including the unknown tail. A stated
 *  final score (bit 5) is the source's only while its games are. */
function mergeMtch(match, doc, ptMtch, gamesSame = true) {
  const { fields: orig, unknownMask, tail } = splitMtch(b64d(ptMtch.payload));
  const snap = ptMtch.doc || {};
  const ours = match.mtch_fields;
  const owned = new Set(OWNED.flatMap(([bits]) => bits));
  const fields = {};
  for (const [b, v] of Object.entries(orig)) if (!owned.has(Number(b))) fields[b] = v;
  for (const [bits, keys] of OWNED) {
    if (keys.every((k) => _same(doc[k], snap[k]))) {
      for (const b of bits) if (b in orig && (gamesSame || b !== 5)) fields[b] = orig[b];
    } else {
      for (const b of bits) if (b in ours) fields[b] = ours[b];
    }
  }
  // P4: the document counts seconds, so a source that stated milliseconds keeps
  // them while the second is the same -- unless a precision now claims the
  // instant is a period's first, which only the whole second can promise.
  if (8 in orig && !(14 in fields) && _same(doc.timestamp, snap.timestamp)) fields[8] = orig[8];
  return _mtchRecord(match.mtch_mandatory, fields, unknownMask, tail);
}

// ---------------------------------------------------------------------------
// The clock and the video (8.2, 8.3)
// ---------------------------------------------------------------------------

const MAX_TS = 0xFFFFFFFF;
export const MAX_VIDEO_URL = 512;
const WALL_UNKNOWN = 0xFFFFFFFF;
const LAG_ABSENT = 0xFFFF;
const LAG_SATURATED = 0xFFFE;

function _getBits(blob, pos, n) {
  let v = 0;
  for (let i = 0; i < n; i++) {
    const p = pos + i;
    v += ((blob[p >> 3] >> (p & 7)) & 1) * 2 ** i;
  }
  return v;
}

/** `{header, ts, precision}` of a valid CLCK payload (8.2), else null.
 *  `precision` is the canonical 10 when the series is empty. */
export function decodeClock(payload, plyCount) {
  if (payload.length < 29) return null;
  const dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const header = [dv.getUint32(0, true), dv.getUint32(4, true), dv.getUint32(8, true),
    dv.getUint32(12, true), payload[16]];
  const length = dv.getUint32(20, true);
  const smallBits = payload[24];
  const precision = dv.getUint32(25, true);
  if (length === 0) return payload.length === 29 ? { header, ts: [0], precision: CLOCK_PRECISION } : null;
  if (smallBits < 1 || smallBits > 31 || precision === 0 || length + 1 > plyCount) return null;
  const perWord = Math.floor(64 / smallBits);
  const words = Math.ceil(length / perWord);
  if (payload.length - 29 < words * 8) return null;
  const lsb = payload.subarray(29, 29 + words * 8);
  const low = [];
  for (let w = 0; w < words; w++) {
    const used = Math.min(perWord, length - w * perWord);
    if (used * smallBits < 64 && _getBits(lsb, w * 64 + used * smallBits, 64 - used * smallBits)) return null;
    for (let k = 0; k < used; k++) low.push(_getBits(lsb, w * 64 + k * smallBits, smallBits));
  }
  const msb = payload.subarray(29 + words * 8);
  let bit = 0;
  let t = 0;
  const ts = [0];
  for (let i = 0; i < length; i++) {
    let m = 0;
    for (;;) {
      if (bit >= msb.length * 8) return null;
      const one = (msb[bit >> 3] >> (bit & 7)) & 1;
      bit++;
      if (!one) break;
      m++;
      if (m > MAX_TS) return null;
    }
    const q = m * 2 ** smallBits + low[i];
    if (q > Math.floor(MAX_TS / precision) + 1) return null;
    t += q * precision;
    if (t > MAX_TS) return null;
    ts.push(t);
  }
  if (Math.ceil(bit / 8) !== msb.length) return null;
  if (bit % 8 && (msb[bit >> 3] >> (bit & 7)) !== 0) return null;
  return { header, ts, precision };
}

/** The canonical CLCK payload for these timestamps (8.2, Writing), or null. */
export function encodeClock(header, ts, plyCount, precision = CLOCK_PRECISION) {
  if ((ts.length && ts[0] !== 0) || ts.length > plyCount) return null;
  const rounded = [];
  for (let i = 0; i < ts.length; i++) {
    if (i && ts[i] < ts[i - 1]) return null;
    rounded.push(Math.floor((ts[i] + Math.floor(precision / 2)) / precision));
    if (rounded[i] * precision > MAX_TS) return null;
  }
  const length = Math.max(0, ts.length - 1);
  const q = [];
  for (let i = 0; i < length; i++) q.push(rounded[i + 1] - rounded[i]);
  const size = (b) => 8 * Math.ceil(length / Math.floor(64 / b))
    + Math.ceil(q.reduce((a, x) => a + Math.floor(x / 2 ** b) + 1, 0) / 8);
  let best = 1;
  if (length) {
    let bestSize = size(1);
    for (let b = 2; b < 32; b++) {
      const s = size(b);
      if (s < bestSize) { bestSize = s; best = b; }
    }
  }
  const out = [];
  const u32 = (n) => out.push(...Array.from(_u32(n)));
  header.slice(0, 4).forEach(u32);
  out.push(header[4], 0, 0, 0);
  u32(length);
  out.push(best);
  u32(precision);
  const perWord = Math.floor(64 / best);
  for (let i = 0; i < length; i += perWord) {
    const word = new Uint8Array(8);
    for (let k = 0; k < Math.min(perWord, length - i); k++) {
      const v = q[i + k] % 2 ** best;
      for (let j = 0; j < best; j++) {
        if (Math.floor(v / 2 ** j) % 2) {
          const p = k * best + j;
          word[p >> 3] |= 1 << (p & 7);
        }
      }
    }
    out.push(...word);
  }
  let acc = 0;
  let nbits = 0;
  const put = (bitv) => {
    acc |= bitv << nbits;
    if (++nbits === 8) { out.push(acc); acc = 0; nbits = 0; }
  };
  for (const x of q) {
    for (let m = Math.floor(x / 2 ** best); m > 0; m--) put(1);
    put(0);
  }
  if (nbits) out.push(acc);
  return Uint8Array.from(out);
}

export function urlStorable(url, kind) {
  try {
    _dec.decode(url);
  } catch {
    return false;
  }
  if (url.some((c) => c < 0x20 || c === 0x7F)) return false;
  const https = [0x68, 0x74, 0x74, 0x70, 0x73, 0x3A, 0x2F, 0x2F];
  return kind === 3 || url.length === 0 || https.every((c, i) => url[i] === c);
}

/** `{header, marks}` of a valid VIDO payload (8.3), else null. A mark
 *  addressing no ply is dropped on its own. */
export function decodeVideo(payload, pliesPerGame) {
  if (payload.length < 22 || payload[0] !== 1) return null;
  const dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const kind = payload[1];
  const flags = dv.getUint16(2, true);
  const offset = dv.getInt32(4, true);
  const baseWall = Number(dv.getBigUint64(8, true));
  const urlLen = dv.getUint16(16, true);
  const count = dv.getUint32(18, true);
  if (urlLen > MAX_VIDEO_URL || count > 1 << 20 || payload.length !== 22 + urlLen + 14 * count) return null;
  let url = payload.slice(22, 22 + urlLen);
  if (!urlStorable(url, kind)) url = new Uint8Array(0);
  const marks = [];
  let prev = [-1, -1];
  for (let i = 0; i < count; i++) {
    const o = 22 + urlLen + 14 * i;
    const gi = payload[o];
    const pi = dv.getUint16(o + 1, true);
    const mflags = payload[o + 3];
    const videoMs = dv.getUint32(o + 4, true);
    const wallDelta = dv.getUint32(o + 8, true);
    const lag = dv.getUint16(o + 12, true);
    if (gi < prev[0] || (gi === prev[0] && pi <= prev[1])) return null;
    prev = [gi, pi];
    const wall = baseWall && wallDelta !== WALL_UNKNOWN ? baseWall + wallDelta : null;
    const behind = lag !== LAG_ABSENT ? lag * 1000 : null;
    if (gi >= pliesPerGame.length || pi >= pliesPerGame[gi]) continue;
    marks.push([gi, pi, mflags & 1, videoMs, wall, behind]);
  }
  return { header: [kind, flags & 1, offset, url], marks };
}

export function encodeVideo(header, marksIn) {
  const [kind, live, offset, urlIn] = header;
  const marks = marksIn.map((m, i) => [m, i]).sort((a, b) => a[0][0] - b[0][0]
    || a[0][1] - b[0][1] || a[1] - b[1]).map((e) => e[0]);
  const unique = [];
  for (const m of marks) {
    const last = unique[unique.length - 1];
    if (last && last[0] === m[0] && last[1] === m[1]) unique[unique.length - 1] = m;
    else unique.push(m);
  }
  let base = 0;
  for (const m of unique) if (m[4] && (base === 0 || m[4] < base)) base = m[4];
  const url = urlStorable(urlIn, kind) ? urlIn : new Uint8Array(0);
  const out = new Uint8Array(22 + url.length + 14 * unique.length);
  const dv = new DataView(out.buffer);
  out[0] = 1;
  out[1] = kind;
  dv.setUint16(2, live ? 1 : 0, true);
  dv.setInt32(4, offset, true);
  dv.setBigUint64(8, BigInt(base), true);
  dv.setUint16(16, url.length, true);
  dv.setUint32(18, unique.length, true);
  out.set(url, 22);
  unique.forEach(([gi, pi, hand, videoMs, wall, behind], i) => {
    const o = 22 + url.length + 14 * i;
    let delta = WALL_UNKNOWN;
    if (base && wall && wall - base < WALL_UNKNOWN) delta = wall - base;
    const lag = behind === null ? LAG_ABSENT : Math.min(Math.floor(behind / 1000), LAG_SATURATED);
    out[o] = gi;
    dv.setUint16(o + 1, pi, true);
    out[o + 3] = hand ? 1 : 0;
    dv.setUint32(o + 4, videoMs, true);
    dv.setUint32(o + 8, delta, true);
    dv.setUint16(o + 12, lag, true);
  });
  return out;
}

/** The document's `clock_info` for a decoded header: its four numbers, the berserk
 *  flags when set, the unassigned flag bits when any, and `precision` only when
 *  it is not the canonical 10. */
export function clockDoc(header, precision) {
  const [reserve, delay, incr, start, flags] = header;
  const out = { reserve_ms: reserve, delay_ms: delay, increment_ms: incr, start_timestamp: start };
  if (flags) out.flags = flags;
  if (precision !== CLOCK_PRECISION) out.precision = precision;
  return out;
}

/** The document's `video_info` for a decoded header: its `kind` and the other fields
 *  where they are not the default. */
export function videoDoc(header) {
  const [kind, live, offset, url] = header;
  const out = { kind };
  if (live) out.is_live = true;
  if (offset) out.offset_ms = offset;
  if (url.length) out.url = _dec.decode(url);
  return out;
}

/** What a mark puts on its ply: `video_ms`, and the wall-clock time, the lag
 *  behind live and the hand-anchored flag where it states them. */
export function videoMarkDoc(hand, videoMs, wall, behind) {
  const out = { video_ms: videoMs };
  if (wall !== null) out.wall_ms = wall;
  if (behind !== null) out.behind_live_ms = behind;
  if (hand) out.video_hand_anchored = true;
  return out;
}

/** Whether an MSIG (8.6) signs the CLCK too (`covers` bit 0). */
function msigCoversClock(payload) {
  try {
    const { body, has } = new Cursor(payload, 0, payload.length).record();
    body.varint();
    body.skip(body.varint());
    if (has(0)) body.str();
    if (has(1)) body.skip(body.varint());
    if (has(2)) body.varint64();
    return Boolean(has(3) && (body.varint() & 1));
  } catch {
    return true;        // a signature that does not parse covers nothing we can tell
  }
}

/** A v1 SIGN chunk as a v2 SIGN payload, as the reference's `v1_to_v2` does. It
 *  cannot verify there (the signed payload differs, 8.1.1 against v1), and is
 *  carried because dropping it would erase who vouched for the analysis; a
 *  verifier reports it invalid, which is true. */
function v1SignToV2(body) {
  if (body.length < 4) return null;
  const [algo, klen, plen, slen] = body;
  if (algo !== 1 || body.length < 4 + klen + plen + slen) return null;
  const keyId = body.slice(4, 4 + klen);
  const pub = body.slice(4 + klen, 4 + klen + plen);
  const sig = body.slice(4 + klen + plen, 4 + klen + plen + slen);
  const fields = {};
  if (keyId.length) fields[0] = _cat(_varint(keyId.length), keyId);
  if (pub.length) fields[1] = _cat(_varint(pub.length), pub);
  return _record(_cat(_varint(algo), _varint(sig.length), sig), fields);
}

// ---------------------------------------------------------------------------
// Reading: attach the passthrough record to a document
// ---------------------------------------------------------------------------

/** Attach `_ogxm2_passthrough` to `ogxm` when the file holds anything the
 *  document cannot. `sections` is `[{type, start, payload}]` in file order,
 *  `annos` the decoded ANNO records (each with its `raw` bytes) and `oursIds`
 *  the analysis_id of every block our writer made.
 *
 *  A file the writer reproduces byte for byte gets nothing, since there is
 *  nothing to keep: that is our own files, the common case, and it is decided
 *  without encoding when every block is marked ours and the file holds no clock,
 *  video, signature, foreign annotation or unknown section. Anything that stops
 *  the document being encoded (it could not be written either) also gets
 *  nothing.
 *
 *  `annos` are the decoded ANNO records; `readOgxm2` marks the ones it hung on
 *  the document `placed`. A placed record is kept by the fingerprint of its
 *  canonical encoding, so it goes out verbatim while the document still says
 *  what it said; one that addressed nothing (an unknown scope or decision kind)
 *  is kept whole. */
export function attach(ogxm, data, header, sections, annos, oursIds) {
  const foreignAnnos = annos.filter((r) => !annoIsOurs(r));
  const types = sections.map((s) => s.type);
  const blocksInFile = sections.filter((s) => s.type === 'ANAL').map((s) => uuidOf(s.payload));
  const unknown = sections.filter((s) => !KNOWN_SECTIONS.has(s.type));
  if (blocksInFile.length && blocksInFile.every((b) => oursIds.has(b)) && !foreignAnnos.length
      && !unknown.length && !['CLCK', 'VIDO', 'MSIG', 'SIGN'].some((t) => types.includes(t))) {
    return;
  }
  let parts;
  try {
    parts = _encode(ogxm);
    if (_eq(_assemble(parts, new Plan(parts, ogxm)), data)) return;
  } catch {
    return;
  }

  const games = sections.filter((s) => s.type === 'GAME').map((s) => s.payload);
  if (games.length !== parts.match.games.length) return;
  const pt = {
    version: VERSION, version_minor: header[0], min_reader_minor: header[1],
    match_length: parts.match.match_length,
  };
  const mtch = sections.find((s) => s.type === 'MTCH').payload;
  const snap = {};
  for (const k of MTCH_DOC_KEYS) snap[k] = structuredClone(_nul(ogxm[k]));
  pt.mtch = { payload: b64e(mtch), fp: fingerprint(parts.mtch), doc: snap };
  pt.games = games.map((p, i) => ({ payload: b64e(p), fp: fingerprint(parts.match.games[i]) }));

  const byId = new Map(parts.blocks.map((b) => [b.aid_str, b]));
  const ancillary = {};
  const ptBlocks = {};
  const anchors = [];
  let cur = null;
  let gi = 0;
  let mi = 0;
  let anchor = { k: 'head' };
  for (const s of sections) {
    const t = s.type;
    if (t === 'MTCH') anchor = { k: 'MTCH' };
    else if (t === 'GAME') { anchor = { k: 'GAME', i: gi }; gi++; } else if (t === 'ANAL') {
      cur = uuidOf(s.payload);
      ptBlocks[cur] = { anal: b64e(s.payload), ours: oursIds.has(cur) };
      anchor = { k: 'BLOCK', id: cur };
    } else if ((t === 'DECS' || t === 'SIGN') && cur !== null) {
      ptBlocks[cur][t === 'DECS' ? 'decs' : 'sign'] = b64e(s.payload);
      anchor = { k: 'BLOCK', id: cur };
    } else if (t === 'CLCK' || t === 'VIDO') {
      ancillary[t.toLowerCase()] = s.payload;
      anchor = { k: t };
    } else if (t === 'ANNO') anchor = { k: 'ANNO' };
    else if (t === 'MSIG') {
      (pt.msig = pt.msig || []).push(b64e(s.payload));
      anchor = { k: 'MSIG', i: mi };
      mi++;
    } else if (!KNOWN_SECTIONS.has(t)) anchors.push([t, s.payload, anchor]);
  }
  for (const [aid, blk] of Object.entries(ptBlocks)) {
    const mine = byId.get(aid);
    if (mine === undefined || !('decs' in blk)) return;
    blk.fp = fingerprint(mine.anal, mine.decs, mine.anno_bytes());
  }
  pt.blocks = ptBlocks;
  // The clock and the video are in the document; the source's bytes stand for
  // them while the document still encodes to what they decoded to.
  for (const [name, canon] of [['clck', parts.clck], ['vido', parts.vido]]) {
    if (name in ancillary) {
      pt[name] = { payload: b64e(ancillary[name]), fp: fingerprint(canon || new Uint8Array(0)) };
    }
  }
  pt.anno = foreignAnnos.filter((r) => !r.placed).map((r) => ({
    scope: r.scope, ref: r.ref, analysis: _nul(r.analysis), kind: _nul(r.kind),
    alt: _nul(r.alt_index), key: _nul(r.key), lang: _nul(r.lang), raw: b64e(r.raw),
  }));
  pt.anno_raw = [];
  for (const r of foreignAnnos) {
    if (!r.placed) continue;
    try {
      pt.anno_raw.push({ fp: fingerprint(annoCanon(r)), raw: b64e(r.raw) });
    } catch {
      // a record the writer could not encode is not kept by fingerprint
    }
  }
  pt.unknown = anchors.map(([t, p, a]) => ({ type: t, payload: b64e(p), after: a }));
  ogxm[KEY] = pt;
}

// ---------------------------------------------------------------------------
// Writing: decide, part by part, what to emit
// ---------------------------------------------------------------------------

function patchDigest(anal, digest) {
  const cur = new Cursor(anal, 0, anal.length);
  const length = cur.varint();
  const body = new Cursor(anal, cur.pos, cur.pos + length);
  const mask = body.varint64();
  if (!(mask & 1n)) return anal;
  const at = body.pos + 16;
  return _cat(anal.subarray(0, at), digest, anal.subarray(at + 32));
}

/** What `write_ogxm2` emits: each part chosen between the document's canonical
 *  encoding and the source's original bytes. */
export class Plan {
  constructor(parts, doc) {
    this._init(parts, doc, true);
  }

  _init(parts, doc, usePt) {
    const pt = usePt && doc[KEY] && typeof doc[KEY] === 'object' && doc[KEY].version === VERSION
      ? doc[KEY] : null;
    const match = parts.match;
    this.mtch = parts.mtch;
    this.games = [...match.games];
    this.blocks = parts.blocks.map((b) => [b.anal, b.decs, null, b.annos]);
    this.clck = parts.clck;
    this.vido = parts.vido;
    this.msig = [];
    this.anno_raw = new Map();
    this.foreign_annos = [];
    this.unknown = [];
    this.minor_floor = 0;
    this.min_minor_floor = 0;
    this.verbatim_ids = new Set();
    // The verbatim blocks that are another producer's: their DECS is not ours,
    // so what it carries is only what the source's annotations say.
    this.foreign_verbatim = new Set();
    // What those blocks' own DECS holds: analysis_id -> Map "ply_ref,kind" ->
    // alternatives, which says what an annotation can address there.
    this.foreign_decs = new Map();
    this.games_same = false;
    this.mtch_same = false;
    if (pt !== null) {
      try {
        this._passthrough(parts, doc, pt);
      } catch {
        this._init(parts, doc, false);       // a record that does not parse keeps nothing
      }
    } else {
      this._v1Chunks(parts, doc);
    }
  }

  _passthrough(parts, doc, pt) {
    const match = parts.match;
    const pg = pt.games || [];
    this.games_same = pg.length === match.games.length
      && match.games.every((g, i) => fingerprint(g) === pg[i].fp)
      && pt.match_length === match.match_length;
    match.games.forEach((g, i) => {
      if (i < pg.length && fingerprint(g) === pg[i].fp) this.games[i] = b64d(pg[i].payload);
    });
    this.mtch_same = fingerprint(parts.mtch) === pt.mtch.fp;
    this.mtch = this.mtch_same ? b64d(pt.mtch.payload) : mergeMtch(match, doc, pt.mtch, this.games_same);
    this.minor_floor = pt.version_minor || 0;
    this.min_minor_floor = pt.min_reader_minor || 0;

    const digest = this.mtch_same ? null : matchDigest(this.mtch, this.games);
    parts.blocks.forEach((b, k) => {
      const pb = (pt.blocks || {})[b.aid_str];
      if (pb === undefined || !this.games_same
          || fingerprint(b.anal, b.decs, b.anno_bytes()) !== pb.fp) {
        if (pb !== undefined) this._keepStarted(k, b, pb);
        return;
      }
      let anal = b64d(pb.anal);
      if (digest !== null) anal = patchDigest(anal, digest);
      const sign = pb.sign && this.mtch_same ? b64d(pb.sign) : null;
      this.blocks[k] = [anal, b64d(pb.decs), sign, pb.ours ? b.annos : []];
      this.verbatim_ids.add(b.aid_str);
      if (!pb.ours) {
        this.foreign_verbatim.add(b.aid_str);
        this.foreign_decs.set(b.aid_str, new Map(_decodeDecs(b64d(pb.decs)).map(
          (d) => [`${d.ply_ref},${d.kind}`, (d.alternatives || []).length])));
      }
    });

    const original = {};
    for (const [name, mine] of [['clck', parts.clck], ['vido', parts.vido]]) {
      const pc = pt[name];
      original[name] = null;
      if (pc !== undefined && pc !== null) {
        original[name] = b64d(pc.payload);
        if (fingerprint(mine || new Uint8Array(0)) === pc.fp) this[name] = original[name];
      }
    }
    for (const r of pt.anno_raw || []) {
      if (!this.anno_raw.has(r.fp)) this.anno_raw.set(r.fp, []);
      this.anno_raw.get(r.fp).push(b64d(r.raw));
    }
    // A match signature digests the clock when it says so, so it stands only
    // while the clock written is the one it signed.
    if (this.games_same && this.mtch_same) {
      this.msig = (pt.msig || []).map(b64d).filter((m) => !msigCoversClock(m)
        || (this.clck === null ? original.clck === null
          : original.clck !== null && _eq(this.clck, original.clck)));
    }
    const emitted = new Set(this.blocks.map(([a]) => uuidOf(a)));
    const nGames = match.games.length;
    for (const r of pt.anno || []) {
      const scope = r.scope;
      if (!this.games_same && (![0, 1].includes(scope) || (scope === 1 && r.ref >= nGames))) continue;
      if ((scope === 3 || scope === 4)
          && (r.analysis === null || !this.verbatim_ids.has(r.analysis))) continue;
      if (r.analysis !== null && !emitted.has(r.analysis)) continue;
      this.foreign_annos.push(r);
    }
    this.unknown = (pt.unknown || []).map((t) => [t.type, b64d(t.payload), t.after]);
  }

  /** The source's bytes for an annotation the document still encodes to
   *  `canon` -- each used once, so duplicates stay duplicates. */
  takeRaw(canon) {
    const pool = this.anno_raw.get(fingerprint(canon));
    return pool && pool.length ? pool.shift() : null;
  }

  /** P4 for a block re-encoded from the document: a source that stated
   *  milliseconds for `started_at` keeps them while the document's second
   *  (`timestamp`) is the same. */
  _keepStarted(k, b, pb) {
    let src;
    let mine;
    try {
      src = _decodeAnal(b64d(pb.anal)).started_at;
      mine = _decodeAnal(b.anal).started_at;
    } catch {
      return;
    }
    if (src !== undefined && mine !== undefined && src !== mine && src / 1000n === mine / 1000n) {
      this.blocks[k] = [b.anal_started(Number(src)), b.decs, null, b.annos];
    }
  }

  /** A v1 file's SIGN chunks. Its clock and video are in the document
   *  (`decodeV1Chunks`), and are written from there. */
  _v1Chunks(parts, doc) {
    for (const c of doc._unknown_chunks || []) {
      if (Number(c.type || 0) !== CHUNK_SIGN) continue;
      const body = typeof c.data === 'string' ? b64d(c.data) : (c.data || new Uint8Array(0));
      const k = c.anal_index;
      const sign = v1SignToV2(body);
      if (Number.isInteger(k) && k >= 0 && k < this.blocks.length && sign !== null) {
        const [a, d, sg, n] = this.blocks[k];
        if (sg === null) this.blocks[k] = [a, d, sign, n];
      }
    }
  }
}

/** Read a v1 file's CLCK and VIDO chunks into `clock_info`, `video_info` and the plies'
 *  `timestamp_ms` / `video_ms`, as the reference's `v1_to_v2` reads them. The chunks
 *  stay in `_unknown_chunks`, so a v1 rewrite is unchanged. A chunk that is not
 *  valid (8.2, 8.3) is dropped, as it is there. */
export function decodeV1Chunks(doc) {
  const chunks = (doc._unknown_chunks || []).filter((c) => {
    const t = Number(c.type || 0);
    return t === CHUNK_CLCK || t === CHUNK_VIDO;
  });
  if (!chunks.length) return;
  const { keys } = plyLayout(doc);
  const { games } = doc;
  const done = new Set();
  for (const c of chunks) {
    const t = Number(c.type);
    if (done.has(t)) continue;
    const body = typeof c.data === 'string' ? b64d(c.data) : (c.data || new Uint8Array(0));
    if (t === CHUNK_CLCK) {
      const got = decodeClock(body, keys.length);
      if (got !== null) {
        // The reference rewrites a v1 clock at the canonical step, so does this:
        // the stated precision is not kept.
        doc.clock_info = clockDoc(got.header, CLOCK_PRECISION);
        got.ts.forEach((v, i) => { games[keys[i][0]].plies[keys[i][1]].timestamp_ms = v; });
        done.add(t);
      }
    } else {
      // v1 addresses a mark by the ply's place in the game as the document
      // lists it, a game's set-up position included.
      const got = decodeVideo(body, games.map((g) => g.plies.length));
      if (got !== null) {
        doc.video_info = videoDoc(got.header);
        for (const [gi, pi, hand, videoMs, wall, behind] of got.marks) {
          Object.assign(games[gi].plies[pi], videoMarkDoc(hand, videoMs, wall, behind));
        }
        done.add(t);
      }
    }
  }
}

/** Spec 8.4.1 (N1-N3): groups by target, keyed records first by (key, lang),
 *  prose in the order given. */
export function annoSortKey(scope, ref, analysis, kind, alt, key, lang, seq) {
  return [
    scope, ref, analysis !== null ? 1 : 0, analysis !== null ? _uuidBytes(analysis) : new Uint8Array(0),
    kind !== null ? 1 : 0, kind || 0, alt !== null ? 1 : 0, alt || 0,
    key === null ? 1 : 0, key !== null ? _enc.encode(key) : new Uint8Array(0),
    lang !== null ? 1 : 0, lang !== null ? _enc.encode(lang) : new Uint8Array(0), seq,
  ];
}

export function cmpSortKeys(a, b) {
  for (let i = 0; i < a.length; i++) {
    const d = a[i] instanceof Uint8Array ? _cmpBytes(a[i], b[i]) : a[i] - b[i];
    if (d) return d;
  }
  return 0;
}
