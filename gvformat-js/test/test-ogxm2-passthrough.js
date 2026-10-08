// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Spec I7 -- unknown content is preserved -- for a foreign OGXM v2 file. Mirrors
// tests/test_ogxm2_passthrough.py -- keep the two in step.
//
// fixtures/ogxm2/foreign.ogxm is HedgeHog's two-blocks.ogxm with a clock, a
// video, signatures, annotations, unknown fields and an unknown section added
// through the reference codec (tests/gen_ogxm2_passthrough_fixtures.py). Each
// edit of tests/passthrough_cases.py is made here with the JavaScript codec and
// must write the bytes Python wrote (fixtures/ogxm2/passthrough-sha256.json).
// The Python test holds the semantic checks against the reference library; here
// the same structure is checked without it.

import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { appendAnalysis } from '../src/merge.js';
import { sha256Hex } from '../src/sha256.js';
import { _walkSections } from '../src/ogxm2.js';
import { KEY, splitMtch, matchDigest } from '../src/ogxm2_passthrough.js';

let passed = 0, failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}
const eq = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

const here = dirname(fileURLToPath(import.meta.url));
const dir = join(here, 'fixtures', 'ogxm2');
const toU8 = (buf) => new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
const FOREIGN = toU8(readFileSync(join(dir, 'foreign.ogxm')));
const V1 = toU8(readFileSync(join(dir, 'v1-chunks.gvab')));
const pinned = JSON.parse(readFileSync(join(dir, 'passthrough-sha256.json'), 'utf8'));

const secs = (d) => _walkSections(d, d.length).map((s) => ({ type: s.type, payload: s.payload }));
const payloads = (d, t) => secs(d).filter((s) => s.type === t).map((s) => s.payload);
const kinds = (d) => secs(d).map((s) => s.type);
const sameList = (a, b) => a.length === b.length && a.every((p, i) => eq(p, b[i]));

// sha256, against vectors
assert(sha256Hex(new TextEncoder().encode('abc'))
  === 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad', 'sha256: "abc"');
assert(sha256Hex(new Uint8Array(0))
  === 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'sha256: empty');
assert(sha256Hex(new TextEncoder().encode('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq'))
  === '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1', 'sha256: two-block vector');
for (const n of [55, 56, 63, 64, 65, 1000]) {
  const b = new Uint8Array(n).map((_, i) => (i * 31 + 7) & 255);
  assert(sha256Hex(b) === createHash('sha256').update(b).digest('hex'), `sha256: ${n} bytes agrees with node:crypto`);
}

// The edits of tests/passthrough_cases.py
function secondAnalysis(doc) {
  const our = structuredClone(doc);
  for (const g of our.games) for (const p of g.plies) delete p.analyses;
  delete our.analyses_info;
  const info = { ...doc.analysis_info };
  delete info.analysis_id;
  Object.assign(info, { model_id: 'test/appended', eval_level: '3ply', ply: 3, timestamp: 1791300000 });
  our.analysis_info = info;
  return our;
}
const appended = (doc) => appendAnalysis(doc, secondAnalysis(doc));
const renamed = (doc) => { const d = structuredClone(doc); d.player_white = 'Alicia'; return d; };
const lastPlyDropped = (doc) => { const d = structuredClone(doc); d.games[1].plies.pop(); return d; };
const secondBlockRemoved = (doc) => {
  const d = structuredClone(doc);
  d.analyses_info.splice(1, 1);
  for (const g of d.games) {
    for (const p of g.plies) {
      if (p.analyses) p.analyses = p.analyses.filter((a) => a.analysis_index === 0);
    }
  }
  return d;
};
const firstBlockEdited = (doc) => {
  const d = structuredClone(doc);
  for (const g of d.games) {
    for (const p of g.plies) {
      for (const a of p.analyses || []) {
        if (a.analysis_index === 0 && a.alternatives && a.alternatives.length) {
          a.alternatives[0].equity = Math.round((a.alternatives[0].equity + 0.01) * 1e6) / 1e6;
          return d;
        }
      }
    }
  }
  throw new Error('no decision to edit');
};
const viaGva = (doc) => JSON.parse(JSON.stringify(doc));
const run = (data, ...edits) => {
  let doc = readGvab(data);
  for (const e of edits) doc = e(doc);
  return write_gvab(doc);
};
const CASES = {
  unchanged: () => run(FOREIGN),
  append: () => run(FOREIGN, appended),
  metadata_edit: () => run(FOREIGN, renamed),
  move_edit: () => run(FOREIGN, lastPlyDropped),
  block_removed: () => run(FOREIGN, secondBlockRemoved),
  analysis_edit: () => run(FOREIGN, firstBlockEdited),
  gva_unchanged: () => run(FOREIGN, viaGva),
  gva_append: () => run(FOREIGN, viaGva, appended),
  append_then_rename: () => run(FOREIGN, appended, renamed),
  v1_chunks: () => run(V1),
};
const out = {};
for (const [name, fn] of Object.entries(CASES)) {
  out[name] = fn();
  const want = pinned[name];
  const sha = createHash('sha256').update(out[name]).digest('hex');
  assert(want && sha === want.sha256 && out[name].length === want.length,
    `${name}: bytes equal Python's (length ${out[name].length}, sha256 ${sha.slice(0, 12)})`);
}

// Structure
const doc = readGvab(FOREIGN);
assert(doc[KEY] && JSON.stringify(JSON.parse(JSON.stringify(doc[KEY]))) === JSON.stringify(doc[KEY]),
  'the document carries a JSON-safe _ogxm2_passthrough');
assert(JSON.stringify(Object.keys(doc[KEY].blocks)) === JSON.stringify(doc.analyses_info.map((i) => i.analysis_id)),
  'a foreign block keeps its own analysis_id');
assert(eq(out.unchanged, FOREIGN), 'write(read(F)) is F, byte for byte');
assert(eq(out.gva_unchanged, FOREIGN), 'the .gva route writes the same bytes');
assert(!(KEY in readGvab(write_gvab(readGvab(toU8(readFileSync(join(here, '..', '..', 'samples', 'gv', 'B4_SrGcsKAQmoTyHlgJCbM.gvab'))))))),
  'a file we wrote carries no passthrough');

for (const kind of ['MTCH', 'GAME', 'ZZZZ', 'SIGN', 'CLCK', 'VIDO', 'MSIG', 'ANAL', 'DECS']) {
  const theirs = payloads(FOREIGN, kind);
  assert(sameList(payloads(out.append, kind).slice(0, theirs.length), theirs),
    `append: every original ${kind} section is present, byte for byte`);
}
assert(kinds(out.append).filter((t) => t === 'ANAL').length === 3, 'append: a third block follows their two');

const meta = out.metadata_edit;
assert(!kinds(meta).includes('SIGN') && !kinds(meta).includes('MSIG'), 'metadata edit: SIGN and MSIG are gone');
assert(sameList(payloads(meta, 'GAME'), payloads(FOREIGN, 'GAME'))
  && sameList(payloads(meta, 'CLCK'), payloads(FOREIGN, 'CLCK'))
  && sameList(payloads(meta, 'VIDO'), payloads(FOREIGN, 'VIDO'))
  && sameList(payloads(meta, 'ZZZZ'), payloads(FOREIGN, 'ZZZZ'))
  && sameList(payloads(meta, 'DECS'), payloads(FOREIGN, 'DECS'))
  && sameList(payloads(meta, 'ANNO'), payloads(FOREIGN, 'ANNO')),
  'metadata edit: games, clock, video, unknown section, decisions and annotations are verbatim');
const digest = matchDigest(payloads(meta, 'MTCH')[0], payloads(meta, 'GAME'));
const a0 = payloads(meta, 'ANAL')[0];
const hexIn = (hay, needle) => hay.some((_, i) => i + needle.length <= hay.length && needle.every((v, j) => hay[i + j] === v));
assert(hexIn(a0, digest) && !eq(a0, payloads(FOREIGN, 'ANAL')[0])
  && eq(payloads(meta, 'ANAL')[1], payloads(FOREIGN, 'ANAL')[1]),
  'metadata edit: match_digest in ANAL is recomputed');
const o = splitMtch(payloads(FOREIGN, 'MTCH')[0]);
const n = splitMtch(payloads(meta, 'MTCH')[0]);
assert(n.unknownMask === o.unknownMask && eq(n.tail, o.tail) && n.tail.length === 3,
  'metadata edit: the unknown MTCH tail is spliced back');
assert(Object.keys(o.fields).every((b) => b === '0' || eq(n.fields[b], o.fields[b]))
  && !eq(n.fields[0], o.fields[0]) && Object.keys(n.fields).length === Object.keys(o.fields).length,
  'metadata edit: every other MTCH field is as it was');

const mv = out.move_edit;
assert(['SIGN', 'MSIG'].every((t) => !kinds(mv).includes(t)) && ['CLCK', 'VIDO', 'ZZZZ'].every((t) => kinds(mv).includes(t)),
  'move edit: SIGN and MSIG gone; the clock, the video and the unknown section stay');
assert(eq(payloads(mv, 'GAME')[0], payloads(FOREIGN, 'GAME')[0])
  && !eq(payloads(mv, 'GAME')[1], payloads(FOREIGN, 'GAME')[1]), 'move edit: only the edited game is re-encoded');

const rm = out.block_removed;
assert(kinds(rm).filter((t) => t === 'ANAL').length === 1 && kinds(rm).filter((t) => t === 'SIGN').length === 1
  && sameList(payloads(rm, 'SIGN'), payloads(FOREIGN, 'SIGN').slice(0, 1))
  && sameList(payloads(rm, 'MSIG'), payloads(FOREIGN, 'MSIG')), 'block removed: not emitted, the rest stands');

const ed = out.analysis_edit;
assert(!eq(payloads(ed, 'DECS')[0], payloads(FOREIGN, 'DECS')[0])
  && eq(payloads(ed, 'DECS')[1], payloads(FOREIGN, 'DECS')[1])
  && sameList(payloads(ed, 'SIGN'), payloads(FOREIGN, 'SIGN').slice(1)),
  'analysis edit: the edited block is re-encoded and unsigned, the other verbatim');

const v1 = out.v1_chunks;
assert(['SIGN', 'CLCK', 'VIDO'].every((t) => kinds(v1).includes(t)), 'v1: SIGN, CLCK and VIDO are written as v2');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
