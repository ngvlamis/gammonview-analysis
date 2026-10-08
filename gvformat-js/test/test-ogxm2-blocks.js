// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// The OGXM v2 analysis fields as document keys: the block's own fields, levels
// at all three tiers, and the keys v2 adds to decisions and alternatives.
// Mirrors tests/test_ogxm2_blocks.py -- keep the two in step.
//
//   (a) Each synthetic document of tests/ogxm2_blocks_cases.py
//       (fixtures/ogxm2/blocks-docs.json) is written with the JavaScript codec;
//       the bytes must be the Python writer's (blocks-sha256.json), the document
//       read back must be the one Python reads, and rewriting is byte-stable.
//   (b) blocks.ogxm is a file the reference codec wrote using every field. It
//       reads into blocks.expected.json and rewrites byte for byte, and edits to
//       it keep what they do not touch.
//   (c) Where each field lands: in DECS, in the annotation that keeps a decision
//       exactly, or in the block's own annotation; and the read-back rules.
// The reference library itself is Python's to run; here the same structure is
// checked without it.

import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deepStrictEqual } from 'node:assert';
import { write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { appendAnalysis } from '../src/merge.js';
import { KEY } from '../src/ogxm2_passthrough.js';
import {
  _walkSections, _decodeAnno, _decodeAnal, _decodeDecs, GV_KEY_ANALYSIS, GV_KEY_DECISIONS,
  GV_FORMAT, GV_PREFIX, KIND_CHECKER, KIND_CUBE,
} from '../src/ogxm2.js';

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

/** A document as text both languages spell the same (see gen_ogxm2_blocks_fixtures.py). */
function canon(v) {
  if (Array.isArray(v)) return `[${v.map(canon).join(',')}]`;
  if (v !== null && typeof v === 'object') {
    return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(',')}}`;
  }
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(8);
  return JSON.stringify(v);
}
const sha = (s) => createHash('sha256').update(s).digest('hex');

const here = dirname(fileURLToPath(import.meta.url));
const dir = join(here, 'fixtures', 'ogxm2');
const toU8 = (buf) => new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
const docs = JSON.parse(readFileSync(join(dir, 'blocks-docs.json'), 'utf8'));
const pinned = JSON.parse(readFileSync(join(dir, 'blocks-sha256.json'), 'utf8'));
const FOREIGN = toU8(readFileSync(join(dir, 'blocks.ogxm')));
const expectedForeign = JSON.parse(readFileSync(join(dir, 'blocks.expected.json'), 'utf8'));

const sections = (data) => {
  const out = {};
  for (const s of _walkSections(data, data.length)) (out[s.type] = out[s.type] || []).push(s.payload);
  return out;
};
const annotations = (data) => (sections(data).ANNO || []).flatMap((p) => _decodeAnno(p));
const decisions = (data) => _decodeDecs(sections(data).DECS[0]);
const blockRecord = (data) => _decodeAnal(sections(data).ANAL[0]);
const exactRefs = (data) => new Set(annotations(data)
  .filter((r) => (r.key || '').startsWith(GV_KEY_DECISIONS)).map((r) => r.ref));
const analysisAt = (doc, g, p) => doc.games[g].plies[p].analysis;
const blockItems = (data) => annotations(data)
  .filter((r) => (r.key || '').startsWith(GV_KEY_ANALYSIS))
  .sort((x, y) => (x.key < y.key ? -1 : 1))
  .map((r, i) => (i ? r.value : r.value.slice(GV_FORMAT.length))).join('');

// (a) every document
const written = {};
const settled = {};
for (const [name, doc] of Object.entries(docs)) {
  const data = write_gvab(doc);
  written[name] = data;
  const want = pinned[name];
  assert(want !== undefined && data.length === want.length && sha(Buffer.from(data)) === want.sha256,
    `${name}: bytes equal the Python writer's (length ${data.length})`);
  const back = readGvab(data);
  settled[name] = back;
  assert(sha(canon(back)) === want.settled_sha256, `${name}: reads into the document Python reads`);
  assert(bytesEqual(write_gvab(readGvab(data)), data), `${name}: rewriting is byte-stable`);
  assert(bytesEqual(write_gvab(plain(readGvab(data))), data), `${name}: and so is the .gva route`);
  assert(same(readGvab(write_gvab(back)), back), `${name}: read(write(D)) == D`);
}

// the block
const rich = settled['rich-money'];
const info = rich.analysis_info;
assert(info.producer === 0 && info.complete === true && info.model_name === 'Xerxes'
  && info.model_id === '16fcd41c-9f64-4fcc-bca2-c89e6de07721'
  && info.model_digest === '0123456789abcdef'.repeat(4) && info.engine_build === '7a1be2d941e2-dirty'
  && info.cube_efficiency === 0.6 && info.tables === 'egtb-2026.1' && info.currency === 0
  && info.completed_at === 1790380900123 && info.sources.length === 2
  && info.dials.top_deep_threshold === 0.04 && info.dials.cube_rule === 2
  && same(info.level, { preset: '2ply', checker_ply: 2, cube_ply: 2 }),
'the block states every optional field v2 gives it');
const rec = blockRecord(written['rich-money']);
assert(rec.producer === 0 && rec.complete && rec.currency === 0 && rec.model_name === 'Xerxes'
  && rec.dials.exact_bearoff === true && rec.sources.length === 2 && rec.level.cube_ply === 2,
'and each is v2\'s own field in the ANAL record');
assert(!blockItems(written['rich-money']).includes('engine_build='),
  'nothing about the block needed an annotation of ours');

const part = settled.partial.analysis_info;
assert(part.coverage.length === docs.partial.games[0].plies.length + 2 && part.producer === 2
  && part.complete === undefined && !part.coverage.some((c) => c[0] === 9),
'coverage lists the plies attempted (one that names no ply is gone), and not complete is no key');

// levels and decisions
const a0 = analysisAt(rich, 0, 0);
assert(a0.alternatives_total === 40 && a0.rollouts_done === 3 && a0.deep_searched === 6
  && a0.position_tags === 5 && a0.producer_ref === 1 && a0.source_band === 1800
  && a0.level.rollout.seed === '18446744073709551615' && a0.level.rollout.match_policy === 1
  && a0.luck_producer_ref === 0 && a0.luck_level.preset === 'x1',
'checker: counts, sources, a rollout level of every field and a roll level');
assert(a0.alternatives.slice(0, 3).every((a) => a.level === undefined && a.rollout_se > 0
  && a.cubeless_equity < a.equity)
  && a0.alternatives[3].level.rollout === undefined && a0.alternatives[3].level.preset === '1ply',
'alternatives: the first three inherit the rollout, the rest state a level without one');
const d0 = decisions(written['rich-money']).find((d) => d.ply_ref === 0 && d.kind === KIND_CHECKER);
assert(d0 !== undefined && d0.rollouts_done === 3 && d0.level.rollout.seed === '18446744073709551615'
  && !exactRefs(written['rich-money']).has(0),
'that decision is in DECS');
const exact = exactRefs(written['rich-money']);
assert(exact.size >= 4 && analysisAt(rich, 0, 1).alternatives_total === 2
  && analysisAt(rich, 0, 1).rollouts_done === 4 && analysisAt(rich, 0, 3).producer_ref === 7,
'counts DECS has no place for are kept exactly, and come back as stated');
const cubeDec = decisions(written['rich-money']).find((d) => d.kind === KIND_CUBE && d.is_free_cube);
assert(cubeDec !== undefined && cubeDec.take_point === 0.3125 && cubeDec.cubeful_take_value === 0.4321,
  'cube: take_point, the flags and cubeful_take_value are in the DECS record');
const resigned = Object.values(settled.resigned.games).flatMap((g) => g.plies)
  .map((p) => p.analysis).find((a) => a);
assert(resigned.correct_value === 1 && resigned.producer_ref === 0
  && resigned.level.rollout.trials === 72, 'resign: correct_value, producer_ref and a level');

// currencies
const mc = settled['match-currencies'];
const subs = mc.games.flatMap((g) => g.plies).map((p) => (p.analysis || {}).cube_decision)
  .filter((s) => s);
assert(mc.analysis_info.currency === undefined && subs[0].currency === 1 && subs[1].currency === undefined,
  'a cube decision keeps a currency of its own; one equal to the block\'s is not written');
assert(settled['money-block-in-a-match'].analysis_info.currency === 1
  && settled['money-in-a-match-currency'].analysis_info.currency === undefined
  && blockRecord(written['money-in-a-match-currency']).currency === 1,
'a money block in a match keeps its currency; match chances in a money game are written as money');

// what v2 cannot hold
const un = settled.unholdable.analysis_info;
const raw = docs.unholdable.analysis_info;
assert(['producer', 'model_name', 'model_digest', 'engine_build', 'tables', 'completed_at', 'model_id']
  .every((k) => un[k] === raw[k]) && same(un.sources, raw.sources) && same(un.dials, raw.dials),
'values v2 cannot hold travel in the block\'s annotation and come back whole');
const items = blockItems(written.unholdable);
assert(['producer=', 'model_name=', 'model_digest=', 'engine_build=', 'tables=', 'completed_at=',
  'sources=', 'dials.jacoby_mode=', 'dials.top_deep_threshold='].every((k) => items.includes(k)),
'each as an item of the annotation that marks the block as ours');
assert(settled.unholdable.annotations === undefined, 'the reader consumes them: no annotations key');

const two = settled['two-blocks'];
assert(two.analyses_info.length === 2 && two.analyses_info[1].engine_build === 'b2'
  && two.analyses_info[1].producer === 4 && two.analyses_info[1].complete === true,
'two blocks each keep their own fields');

// (b) the reference's file
const f = readGvab(FOREIGN);
const held = f[KEY];
delete f[KEY];
assert(same(f, expectedForeign), 'blocks.ogxm reads into the document Python reads');
assert(held !== undefined && !(held.anno || []).length && !(held.unknown || []).length,
  'nothing is kept beside the document but the bytes of what it still says');
assert(f.analysis_info.model_id === '16fcd41c-9f64-4fcc-bca2-c89e6de07721'
  && f.analysis_info.model_id === '16fcd41c-9f64-4fcc-bca2-c89e6de07721'
  && f.analysis_info.coverage.length === 30 && f.analysis_info.currency === 0,
'the block: the name and the identifier as stated');
assert(bytesEqual(write_gvab(readGvab(FOREIGN)), FOREIGN), 'rewriting it unedited is byte for byte the same');
assert(bytesEqual(write_gvab(plain(readGvab(FOREIGN))), FOREIGN), 'and so is the .gva route');

function edited(change) {
  const d = readGvab(FOREIGN);
  change(d);
  const out = write_gvab(d);
  return [out, readGvab(out)];
}
let [out, back] = edited((d) => { d.player_white = 'Alicia'; });
assert(bytesEqual(sections(out).ANAL[0], sections(FOREIGN).ANAL[0]),
  'a rename leaves the block the source\'s own bytes');
[out, back] = edited((d) => { d.analysis_info.engine_build = 'next-build'; });
let r = blockRecord(out);
assert(r.engine_build === 'next-build' && r.model_name === 'Xerxes' && r.sources.length === 2
  && r.complete && r.producer === 0 && r.currency === 0 && r.coverage.length === 30
  && same(r.dials, blockRecord(FOREIGN).dials),
'editing one field of the block re-encodes it with every other field');
assert(r.started_at % 1000n === 123n && r.started_at === blockRecord(FOREIGN).started_at,
  'and the milliseconds the source stated for its start (P4)');
const dec = decisions(out).find((d) => d.ply_ref === 0 && d.kind === KIND_CHECKER);
assert(dec.level.rollout.seed === '18446744073709551615' && dec.rollouts_done === 3,
  'and its decisions, rollouts included');
[out, back] = edited((d) => {
  const a = analysisAt(d, 0, 0);
  a.alternatives[0].rollout_se = 0.05;
  a.alternatives[1].cubeless_equity = 0.1234;
});
assert(analysisAt(back, 0, 0).alternatives[0].rollout_se === 0.05
  && analysisAt(back, 0, 0).alternatives[1].cubeless_equity === 0.1234
  && analysisAt(back, 0, 0).rollouts_done === 3, 'a decision edit keeps its other fields');
[out, back] = edited((d) => { delete d.analysis_info.sources; });
assert(analysisAt(back, 0, 0).producer_ref === 1 && back.analysis_info.sources === undefined
  && exactRefs(out).size > 0,
'a block that loses its sources keeps the producer_refs, in the annotation that holds them exactly');
[out, back] = edited((d) => { d.games[0].plies.unshift({ color: 1, action_id: 37 }); });
assert(analysisAt(back, 0, 1).alternatives_total === 40
  && back.analysis_info.coverage.some((c) => c[0] === 0 && c[1] === 22),
'an inserted ply moves the decisions with their plies; coverage names every ply a decision is on');

// the read-back rules (profile section 4)
{
  const money = structuredClone(docs['rich-money']);
  money.analysis_info.currency = 1;
  assert(readGvab(write_gvab(money)).analysis_info.currency === undefined,
    'rule currency-default: a block\'s currency equal to the match\'s default is not a key');
  const seeded = structuredClone(docs['rich-money']);
  seeded.games[0].plies[0].analysis.level.rollout.seed = 42;
  assert(readGvab(write_gvab(seeded)).games[0].plies[0].analysis.level.rollout.seed === '42',
    'rule seed-string: a seed given as a number reads back as a decimal string');
  const dials = structuredClone(docs['rich-money']);
  dials.analysis_info.dials.nonsense = 3;
  assert(readGvab(write_gvab(dials)).analysis_info.dials.nonsense === undefined,
    'rule dials-known: dials v2 does not define are not kept');
}

// (d) a beaver answers a double: the engine's take lands on the beaver ply, and
// the raccoon after it has no decision
{
  const mk = (rows) => ({ game_index: 0, plies: rows.map(([a, an]) => ({
    color: 0, action_id: a, ...(an ? { analysis: an } : {}) })) });
  const base = { player_white: 'A', games: [mk([[0], [21], [32], [33], [1], [24]])] };
  const our = { player_white: 'A', analysis_info: { ply: 2 },
    games: [mk([[0, { n: 0 }], [21, { n: 1 }], [22, { n: 2 }], [1, { n: 3 }]])] };
  const merged = appendAnalysis(base, our);
  assert(merged.games[0].plies.map((p) => (p.analysis ? p.analysis.n : '-')).join('') === '012-3-',
    'a take is paired with the beaver that answers the double, and the raccoon has no analysis');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
