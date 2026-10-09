// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// The OGXM v2 clock, video and annotations as document keys. Mirrors
// tests/test_ogxm2_annos.py -- keep the two in step.
//
//   (a) Each synthetic document of tests/ogxm2_annos_cases.py
//       (fixtures/ogxm2/annos-docs.json) is written with the JavaScript codec;
//       the bytes must be the Python writer's (annos-sha256.json), read back to
//       the document, and rewrite byte for byte.
//   (b) annos.ogxm is a file the reference codec wrote using every field of the
//       clock, the video and the annotations. It reads into annos.expected.json
//       and rewrites byte for byte; each edit of ogxm2_annos_cases.EDITS, made
//       here, writes the bytes Python wrote (annos-edits-sha256.json), and keeps
//       the clock, the video and the annotations where the profile says.
//   (c) A v1 file's clock and video are decoded.
// The reference library itself is Python's to run; here the same structure is
// checked without it.

import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deepStrictEqual } from 'node:assert';
import { write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { KEY, decodeClock } from '../src/ogxm2_passthrough.js';
import {
  _walkSections, _decodeAnno, annoIsOurs, GV_KEY_ANNOTATIONS, GV_FORMAT, GV_KEY_VIDEO_URL,
  V2_FIELD_NAMES,
} from '../src/ogxm2.js';
import { _b64decode } from '../src/binary.js';

let passed = 0, failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}
const bytesEqual = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
const plain = (x) => JSON.parse(JSON.stringify(x));
function same(a, b) {
  try { deepStrictEqual(plain(a), plain(b)); return true; } catch (e) {
    console.error(e.message.slice(0, 1500));
    return false;
  }
}

const here = dirname(fileURLToPath(import.meta.url));
const dir = join(here, 'fixtures', 'ogxm2');
const toU8 = (buf) => new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
const docs = JSON.parse(readFileSync(join(dir, 'annos-docs.json'), 'utf8'));
const pinned = JSON.parse(readFileSync(join(dir, 'annos-sha256.json'), 'utf8'));
const pinnedEdits = JSON.parse(readFileSync(join(dir, 'annos-edits-sha256.json'), 'utf8'));
const FOREIGN = toU8(readFileSync(join(dir, 'annos.ogxm')));
const V1 = toU8(readFileSync(join(dir, 'v1-chunks.gvab')));
const expectedForeign = JSON.parse(readFileSync(join(dir, 'annos.expected.json'), 'utf8'));
const FOREIGN_READINGS = 15;

const secs = (d) => _walkSections(d, d.length).map((s) => ({ type: s.type, payload: s.payload }));
const payloads = (d, t) => secs(d).filter((s) => s.type === t).map((s) => s.payload);
const kinds = (d) => secs(d).map((s) => s.type);
const annos = (d) => { const p = payloads(d, 'ANNO'); return p.length ? _decodeAnno(p[0]) : []; };
const sameList = (a, b) => a.length === b.length && a.every((p, i) => bytesEqual(p, b[i]));
const sha = (d) => createHash('sha256').update(d).digest('hex');
const carried = (d) => {
  const parts = annos(d).filter((a) => (a.key || '').split('~')[0] === GV_KEY_ANNOTATIONS)
    .map((a) => [a.key, a.value]);
  if (!parts.length) return [];
  parts.sort((x, y) => Number(x[0].split('~')[1] || 0) - Number(y[0].split('~')[1] || 0));
  const text = parts.map((p) => p[1]).join('').slice(GV_FORMAT.length);
  return JSON.parse(new TextDecoder().decode(_b64decode(text)));
};
const sameSections = (kinds_, a, b) => kinds_.every((k) => sameList(payloads(a, k), payloads(b, k)));

// -- (a) synthetic documents ------------------------------------------------------
const readBack = {};
{
  const clone = (x) => structuredClone(x);
  // the two cases whose read-back differs from what is written (profile section 4)
  const dropped = clone(docs['clock-video']);
  readBack['video-dropped-mark'] = dropped;
  const gap = clone(docs['clock-video']);
  delete gap.clock_info;
  for (const g of gap.games) for (const p of g.plies) delete p.timestamp_ms;
  delete gap.games[0].plies[6].timestamp_ms;
  readBack['clock-gap'] = gap;
}
for (const [name, doc] of Object.entries(docs)) {
  const data = write_gvab(doc);
  const want = pinned[name];
  assert(want !== undefined && data.length === want.length && sha(data) === want.sha256,
    `${name}: bytes equal the Python writer's (length ${data.length}, sha256 ${sha(data).slice(0, 12)})`);
  const back = readGvab(data);
  delete back[KEY];
  assert(same(back, readBack[name] || doc), `${name}: read(write(D)) is D`);
  assert(bytesEqual(write_gvab(readGvab(data)), data), `${name}: rewriting is byte-stable`);
  assert(readGvab(data)[KEY] === undefined, `${name}: a file we wrote carries no passthrough`);
}

let data = write_gvab(docs.annotated);
assert(!carried(data).length, 'a fully addressable document carries nothing in x-gammonview-annotations');
let own = annos(data).filter((a) => !annoIsOurs(a));
assert(own.length === 20 && new Set(own.map((a) => a.scope)).size === 5,
  'all twenty annotations are ANNO records, at all five scopes');
assert(own.filter((a) => a.scope === 3 && a.kind === 3).length === 1
  && own.filter((a) => a.scope === 3 && a.kind === 1).length === 3,
'a roll, and the cube decisions of a dice ply and of a double, name their kind and block');

data = write_gvab(docs['annotated-fallback']);
assert(same(carried(data).map((e) => [e.s, e.g ?? null, e.p ?? null]),
  [[0, null, null], [2, 0, 1], [2, 0, 2], [2, 0, 3], [1, 1, null], [2, 1, 0]]),
'a drawing at the match, a value past the cap, two drawings v2 forbids, a language past the cap and a note on a set-up position are carried');
assert(same(annos(data).filter((a) => !annoIsOurs(a)).map((a) => a.value).sort(), ['native', 'ok']),
  'the ones ANNO can hold are written as ANNO');
data = write_gvab(docs['annotated-decisions-fallback']);
assert(same(carried(data).map((e) => [e.s, e.p, e.k, e.i ?? null]), [[3, 7, 0, null], [4, 7, 0, 1]]),
  'a decision only the annotation holds takes its annotations with it; the cube beside it does not');
assert(annos(write_gvab(docs['video-url-unholdable'])).some((a) => a.key === GV_KEY_VIDEO_URL),
  'a video URL v2 would drop is carried');

const refuses = (doc) => { try { write_gvab(doc); return false; } catch (e) { return !(e instanceof RangeError); } };
{
  const base = structuredClone(docs['clock-empty']);
  const mk = (f) => { const d = structuredClone(base); f(d); return d; };
  assert(refuses(mk((d) => { d.annotations = [{ value: 'x', key: 'value' }]; })),
    'a key that is a v2 field name is refused');
  assert(refuses(mk((d) => { d.annotations = [{ value: 'x', key: 'x-gammonview-note' }]; })),
    'a key in our namespace is refused');
  assert(refuses(mk((d) => { d.annotations = [{ value: 'a', key: 'x-k' }, { value: 'b', key: 'x-k' }]; })),
    'a (key, language) pair used twice on one target is refused');
  assert(refuses(mk((d) => { d.clock_info = { reserve_ms: -1 }; })), 'a negative clock is refused');
  const many = mk((d) => { d.annotations = Array.from({ length: 4100 }, (_, i) => ({ value: 'n', key: `x-k${i}` })); });
  const big = mk((d) => { d.annotations = Array.from({ length: 70 }, (_, i) => ({ value: 'b'.repeat(4000), key: `x-k${i}` })); });
  for (const [label, d] of [['more than 4096 annotations', many], ['more than 256 KiB of annotations', big]]) {
    let msg = '';
    try { write_gvab(d); } catch (e) { msg = e.message; }
    assert(msg.includes('annotations'), `${label}: the writer raises, and drops nothing`);
  }
  assert(V2_FIELD_NAMES.size === 140, 'the field-name list has the reference schema\'s 140 names');
}

// -- (b) a foreign file -------------------------------------------------------------
const doc = readGvab(FOREIGN);
const pt = doc[KEY];
const held = { ...doc };
delete held[KEY];
assert(same(held, expectedForeign), 'readGvab gives the expected document');
assert(pt.anno.length === 2 && pt.anno_raw.length === 17 && pt.clck && pt.vido && !pt.clock_info && !pt.video_info,
  'only the two annotations that address nothing are kept whole; the rest by fingerprint');
assert(bytesEqual(write_gvab(doc), FOREIGN), 'write(read(F)) is F, byte for byte');
assert(bytesEqual(write_gvab(JSON.parse(JSON.stringify(doc))), FOREIGN), 'the .gva route writes the same bytes');
assert(!JSON.stringify(doc).includes('x-gammonview'), 'none of ours surfaces in the document');

// the edits of tests/ogxm2_annos_cases.py
const cubeSet = (c) => ({ color: c, action_id: 36, cube_value: 1, cube_owner: 2 });
const EDITS = {
  unchanged: (d) => d,
  comment_edited: (d) => { d.games[0].plies[3].annotations[0].value = 'hello, edited'; return d; },
  annotation_added: (d) => {
    d.games[0].plies[6].annotations = [
      { value: 'new', key: 'x-new', drawings: [{ shape: 0, at: 12, color: 1 }] }];
    d.annotations.push({ value: 'one more at the match' });
    d.games[0].plies[2].analyses[0].alternatives[1].annotations = [
      { value: 'a fresh note on alternative one' }];
    return d;
  },
  annotation_removed: (d) => { delete d.games[0].plies[3].annotations; return d; },
  clock_edited: (d) => { d.games[0].plies[6].timestamp_ms += 40; return d; },
  ply_removed: (d) => { d.games[0].plies.splice(7, 2); return d; },
  ply_inserted_after_clock: (d) => {
    const p = d.games[0].plies;
    p.splice(FOREIGN_READINGS + 1, 0, cubeSet(p[FOREIGN_READINGS + 1].color));
    return d;
  },
  ply_inserted_in_clock: (d) => {
    const p = d.games[0].plies;
    p.splice(2, 0, cubeSet(p[2].color));
    return d;
  },
  block_removed: (d) => {
    d.analyses_info.splice(1, 1);
    for (const g of d.games) for (const p of g.plies) {
      if (p.analyses) p.analyses = p.analyses.filter((a) => a.analysis_index === 0);
    }
    return d;
  },
};
const edited = (name) => write_gvab(EDITS[name](readGvab(FOREIGN)));
for (const name of Object.keys(EDITS)) {
  const out = edited(name);
  const want = pinnedEdits[name];
  assert(want !== undefined && out.length === want.length && sha(out) === want.sha256,
    `${name}: bytes equal the Python writer's (length ${out.length}, sha256 ${sha(out).slice(0, 12)})`);
  assert(bytesEqual(write_gvab(readGvab(out)), out), `${name}: the result is stable`);
}

let out = edited('comment_edited');
assert(sameSections(['MTCH', 'GAME', 'ANAL', 'DECS', 'SIGN', 'MSIG', 'CLCK', 'VIDO'], out, FOREIGN),
  'an edited comment changes ANNO only: signatures, clock and video are the source\'s bytes');
{
  const a = annos(FOREIGN), b = annos(out);
  assert(a.length === b.length && a.filter((x, i) => !bytesEqual(x.raw, b[i].raw)).length === 1,
    'one annotation record differs; every other is the source\'s bytes');
}
out = edited('annotation_added');
assert(sameSections(['MTCH', 'GAME', 'ANAL', 'DECS', 'SIGN', 'MSIG', 'CLCK', 'VIDO'], out, FOREIGN),
  'adding annotations keeps every signature valid');
assert(annos(out).length === annos(FOREIGN).length + 3 && !carried(out).length,
  'three records are added, all addressed natively');
out = edited('annotation_removed');
assert(sameSections(['GAME', 'SIGN', 'MSIG', 'CLCK', 'VIDO'], out, FOREIGN)
  && annos(out).length === annos(FOREIGN).length - 1, 'removing an annotation touches ANNO only');
out = edited('clock_edited');
assert(!sameList(payloads(out, 'CLCK'), payloads(FOREIGN, 'CLCK'))
  && sameList(payloads(out, 'VIDO'), payloads(FOREIGN, 'VIDO'))
  && sameList(payloads(out, 'SIGN'), payloads(FOREIGN, 'SIGN')),
'an edited reading re-encodes the clock; the video and the block signatures stand');
assert(sameList(payloads(out, 'MSIG'), payloads(FOREIGN, 'MSIG').slice(0, 1)),
  'the match signature that covers the clock is gone; the one that does not stays');
{
  const was = decodeClock(payloads(FOREIGN, 'CLCK')[0], 68).ts;
  assert(decodeClock(payloads(out, 'CLCK')[0], 68).ts[6] === was[6] + 40, 'the reading is the edited one');
}
out = edited('ply_removed');
assert(!kinds(out).includes('SIGN') && !kinds(out).includes('MSIG')
  && payloads(out, 'ANAL').length && payloads(out, 'CLCK').length && payloads(out, 'VIDO').length,
'a move edit drops the signatures, not the clock, the video or the analysis');
assert(decodeClock(payloads(out, 'CLCK')[0], 66).ts.length === FOREIGN_READINGS - 2, 'the clock has two readings fewer');
out = edited('ply_inserted_after_clock');
assert(sameList(payloads(out, 'CLCK'), payloads(FOREIGN, 'CLCK'))
  && sameList(payloads(out, 'VIDO'), payloads(FOREIGN, 'VIDO')),
'a ply after the last reading leaves the clock and the video the source\'s bytes');
out = edited('ply_inserted_in_clock');
assert(!kinds(out).includes('CLCK') && payloads(out, 'VIDO').length,
  'a ply among the readings with none of its own: the clock is dropped, the video stands');
{
  const back = readGvab(out);
  const marks = back.games.flatMap((g, gi) => g.plies.map((p, pi) => [gi, pi, p]))
    .filter(([, , p]) => p.video_ms !== undefined).map(([gi, pi]) => [gi, pi]);
  assert(same(marks, [[0, 1], [0, 6], [1, 10]]), 'the video marks moved with their plies');
  assert(back.clock_info === undefined && back.games[0].plies[4].annotations !== undefined
    && back.games[0].plies[5].annotations !== undefined, 'no clock; the annotations moved too');
}
out = edited('block_removed');
assert(payloads(out, 'SIGN').length === 1 && sameList(payloads(out, 'MSIG'), payloads(FOREIGN, 'MSIG')),
  'the removed block\'s signature goes; the match signatures stand');

// -- (c) a v1 file ------------------------------------------------------------------------
const v1 = readGvab(V1);
assert(same(v1.clock_info, { reserve_ms: 120000, delay_ms: 12000, increment_ms: 0, start_timestamp: 1790442000 })
  && v1.video_info.kind === 1 && v1.video_info.offset_ms === -500,
'a v1 file\'s chunks decode to clock and video');
assert(v1.games.flatMap((g) => g.plies).filter((p) => p.timestamp_ms !== undefined).length === 11
  && v1._unknown_chunks.map((c) => c.name).join() === 'SIGN,CLCK,VIDO',
'eleven readings, and the chunks still stand for a v1 rewrite');
{
  const o = write_gvab(v1);
  assert(bytesEqual(write_gvab(readGvab(o)), o) && same(readGvab(o).clock_info, v1.clock_info),
    'the v2 file reads back to the same clock');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
