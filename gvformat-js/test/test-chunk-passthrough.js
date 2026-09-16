// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Chunk passthrough and the points_won / match-length cap.
// Mirrors tests/test_chunk_passthrough.py -- keep the two in step.

import {
  write_gvab, capPointsWon, clampMatchScore, _b64decode,
} from '../src/binary.js';
import { readGvab, GvabError } from '../src/reader.js';
import { CHUNK_CSUM, CHUNK_SIGN, CHUNK_VIDO } from '../src/constants.js';

let passed = 0, failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

function crc32(bytes) {
  let table = crc32._t;
  if (!table) {
    table = crc32._t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      table[i] = c >>> 0;
    }
  }
  let c = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) c = table[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

function mkChunk(type, body, critical) {
  const out = new Uint8Array(12 + body.length);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, type, true);
  dv.setUint32(4, body.length, true);
  dv.setUint16(8, critical ? 1 : 0, true);
  out.set(body, 12);
  return out;
}

function findChunk(data, type) {
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  for (let p = 20; p < data.length - 8;) {
    const t = dv.getUint32(p, true);
    if (t === type) return p;
    p += 12 + dv.getUint32(p + 4, true);
  }
  return -1;
}

/** Insert whole chunks just before CSUM, fixing file_size + CRC. */
function spliceBeforeCsum(src, extra) {
  const i = findChunk(src, CHUNK_CSUM);
  const out = new Uint8Array(src.length + extra.length);
  out.set(src.subarray(0, i), 0);
  out.set(extra, i);
  out.set(src.subarray(i), i + extra.length);
  const dv = new DataView(out.buffer);
  dv.setUint32(12, out.length, true);
  const cs = findChunk(out, CHUNK_CSUM);
  dv.setUint32(cs + 16, crc32(out.subarray(0, cs)), true);
  return out;
}

function chunkNames(data) {
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const names = [];
  for (let p = 20; p < data.length - 8;) {
    const t = dv.getUint32(p, true);
    names.push(String.fromCharCode(t & 0xFF, (t >>> 8) & 0xFF, (t >>> 16) & 0xFF, (t >>> 24) & 0xFF));
    p += 12 + dv.getUint32(p + 4, true);
  }
  return names;
}

const enc = (s) => new TextEncoder().encode(s);

// Built here rather than read from samples/: a hand-made document pins the
// exact chunk layout this test is about. One analysis
// block is what matters: SIGN binds to it, so the passthrough needs a block to
// bind against.
const src = write_gvab({
  match_length: 5, player_white: 'W', player_black: 'B',
  white_score: 0, black_score: 5,
  analysis_info: { ply: 2, model_id: 'test', timestamp: 0 },
  games: [{
    game_index: 0, winner: 1, points_won: 5, plies: [
      { color: 0, action_id: 6, d1: 3, d2: 1, analysis: { ply: 2, equity_loss: 0.0 } },
      { color: 1, action_id: 24 },
    ],
  }],
});

console.log('--- 1. unknown ancillary chunks survive a round trip ---');
const signBody = enc('signature-payload');
const vidoBody = enc('video-marks');
const withChunks = spliceBeforeCsum(src, new Uint8Array([
  ...mkChunk(CHUNK_SIGN, signBody, false),
  ...mkChunk(CHUNK_VIDO, vidoBody, false),
]));

const d = readGvab(withChunks);
const carried = d._unknown_chunks || [];
assert(JSON.stringify(carried.map((c) => c.name)) === '["SIGN","VIDO"]',
  '1. SIGN and VIDO are captured, in stream order');
assert(new TextDecoder().decode(_b64decode(carried[0].data)) === 'signature-payload'
    && new TextDecoder().decode(_b64decode(carried[1].data)) === 'video-marks',
  '1. their bodies are captured verbatim');
assert(carried.every((c) => c.anal_index === 0),
  '1. each records the analysis block it followed');

const out = write_gvab(d);
assert(JSON.stringify(chunkNames(out)) === JSON.stringify(chunkNames(withChunks)),
  '1. the rewritten file has the same chunks in the same order');
assert(JSON.stringify(readGvab(out)._unknown_chunks) === JSON.stringify(carried),
  '1. a second round trip is stable');
assert(JSON.stringify(JSON.parse(JSON.stringify(carried))) === JSON.stringify(carried),
  '1. the captured chunks survive the .gva JSON form (base64, not bytes)');

console.log('\n--- 2. an unknown CRITICAL chunk is rejected, not carried ---');
const crit = spliceBeforeCsum(src, mkChunk(0x5A5A5A5A, enc('hi'), true));
try {
  readGvab(crit);
  assert(false, '2. unknown critical chunk throws GvabError');
} catch (e) {
  assert(e instanceof GvabError && e.message.includes('ZZZZ'),
    `2. unknown critical chunk throws GvabError naming it (${e.message})`);
}
const anc = spliceBeforeCsum(src, mkChunk(0x5A5A5A5A, enc('hi'), false));
assert(JSON.stringify(readGvab(anc)._unknown_chunks.map((c) => c.name)) === '["ZZZZ"]',
  '2. the same chunk marked ancillary is carried instead');

console.log('\n--- 3. points_won vs the match length ---');
for (const [args, want] of [[[4, 6, 7], 1], [[4, 0, 7], 4], [[4, 3, 7], 4],
                            [[4, 6, 0], 4], [[6, 0, 5], 5], [[-2, 0, 5], 0]]) {
  const got = capPointsWon(...args);
  assert(got === want, `3. capPointsWon(${args}) === ${want} (got ${got})`);
}
assert(clampMatchScore(16, 13) === 13, '3. clampMatchScore(16, 13) === 13');
assert(clampMatchScore(16, 0) === 16, '3. money play is never clamped');

const ogxm = {
  match_length: 13, player_white: 'W', player_black: 'B',
  white_score: 7, black_score: 16, // deliberately the uncapped sum
  games: [
    { game_index: 0, winner: 0, points_won: 7, plies: [] },
    { game_index: 1, winner: 1, points_won: 12, plies: [] },
    { game_index: 2, winner: 1, points_won: 4, plies: [] },
  ],
};
const back = readGvab(write_gvab(ogxm));
assert(JSON.stringify(back.games.map((g) => g.points_won)) === '[7,12,4]',
  '3. points_won is stored uncapped (the win type is recoverable)');
assert(back.white_score === 7 && back.black_score === 13,
  `3. MHDR final score is clamped to the match length (${back.white_score}-${back.black_score})`);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
