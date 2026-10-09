// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Where the document and an OGXM file must agree, and where v2 sets a limit:
// an abandoned match stays abandoned; a cube verdict comes back as the label it
// was written from; v2 holds 64 analysis blocks and a v1 file 16; a checker
// decision with more than 1024 alternatives is written truncated with
// `alternatives_total`, the played move kept; too many games or plies are
// refused. Mirrors tests/test_ogxm2_limits.py -- keep the two in step.

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { write_gvab, write_gvab_v1 } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { appendAnalysis, MAX_ANALYSES } from '../src/merge.js';

let passed = 0, failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}
function throwsWith(fn, text) {
  try { fn(); } catch (e) { return String(e.message).includes(text); }
  return false;
}
const bytesEqual = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
const clone = (x) => structuredClone(x);

const dir = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'ogxm2');
const blocks = JSON.parse(readFileSync(join(dir, 'blocks-docs.json'), 'utf8'));
const fields = JSON.parse(readFileSync(join(dir, 'fields-docs.json'), 'utf8'));
const rich = () => clone(blocks['rich-money']);
const plies = (d) => d.games.flatMap((g, gi) => g.plies.map((p, pi) => ({ gi, pi, p })));
const cubePly = (d, action) => plies(d).find(({ p }) => p.action_id === action && p.analysis).p;

console.log('--- 1. an abandoned match stays abandoned ---');
{
  const doc = clone(fields.midmatch);
  assert(readGvab(write_gvab(doc)).result === 1, '1. a result the score decides is still derived from it');
  for (const result of [3, 0]) {
    const d = clone(doc);
    d.result = result;
    const want = result === 3 ? 3 : 1;
    assert(readGvab(write_gvab(d)).result === want, `1. result ${result} reads back as ${want}`);
  }
  const d = clone(doc);
  d.result = 3;
  const blob = write_gvab(d);
  assert(bytesEqual(write_gvab(readGvab(blob)), blob), '1. and rewriting it is byte-stable');
}

console.log('\n--- 2. a cube verdict comes back as the label it was written from ---');
{
  let d;
  for (const [label, doubleLabel] of [['beaver', 'too_good'], ['raccoon', 'no_double']]) {
    d = rich();
    cubePly(d, 21).analysis.correct_action = doubleLabel;
    cubePly(d, 23).analysis.correct_action = label;
    const blob = write_gvab(d);
    const back = readGvab(blob);
    assert(cubePly(back, 23).analysis.correct_action === label,
      `2. a ${label} verdict is read as '${label}', not as a take`);
    assert(cubePly(back, 21).analysis.correct_action === doubleLabel,
      `2. a ${doubleLabel} verdict is read as '${doubleLabel}'`);
    assert(bytesEqual(write_gvab(back), blob), `2. and the ${label} file rewrites byte for byte`);
  }
  assert(readGvab(write_gvab_v1(d), { deriveOgids: false }) !== null,
    '2. v1 holds only four labels and writes the refinements as the action they refine');
}

console.log('\n--- 3. blocks: v2 holds 64, a v1 file 16 ---');
{
  assert(MAX_ANALYSES === 64, "3. the cap appendAnalysis enforces is v2's 64");
  const d = rich();
  const info = d.analysis_info;
  delete d.analysis_info;
  d.analyses_info = Array.from({ length: 17 }, (_, i) => (
    { ...info, analysis_id: `1f2e3d4c-0000-4000-8000-${String(i).padStart(12, '0')}` }));
  assert(throwsWith(() => write_gvab_v1(d), 'too many analysis blocks for an OGXM v1 file'),
    '3. write_gvab_v1 refuses 17 blocks with a clear error');
  d.analyses_info = [...d.analyses_info, ...d.analyses_info, ...d.analyses_info, ...d.analyses_info,
    d.analyses_info[0]];
  assert(throwsWith(() => write_gvab(d), 'too many analysis blocks for an OGXM v2 file'),
    '3. write_gvab refuses more than 64');

  let big = rich();
  const one = (i) => {
    const m = rich();
    m.analysis_info = { ...m.analysis_info, model_id: `engine-${i}`,
      analysis_id: `1f2e3d4c-0000-4000-8000-${String(i).padStart(12, '0')}` };
    return m;
  };
  big.analysis_info.analysis_id = '1f2e3d4c-0000-4000-8000-000000000000';
  for (let i = 1; i < MAX_ANALYSES; i++) big = appendAnalysis(big, one(i));
  assert(big.analyses_info.length === MAX_ANALYSES, '3. 64 blocks can be accumulated');
  assert(throwsWith(() => appendAnalysis(big, one(999)), 'cannot append'),
    '3. appending a 65th is refused');
  assert(readGvab(write_gvab(big)).analyses_info.length === MAX_ANALYSES,
    '3. and all 64 are written and read back');
}

console.log('\n--- 4. alternatives past 1024 are truncated and say so ---');
{
  const withAlts = (n, playedAt, total = null) => {
    const d = rich();
    const hit = plies(d).find(({ p }) => {
      const alts = (p.analysis || {}).alternatives || [];
      const idx = alts.findIndex((x) => x.is_played);
      return alts.length >= 6 && idx > 0;
    });
    const a = hit.p.analysis;
    const best = a.alternatives[0].equity;
    const played = clone(a.alternatives.find((x) => x.is_played));
    const alts = [];
    for (let i = 0; i < n; i++) {
      const x = clone(a.alternatives[0]);
      x.is_played = false;
      x.equity = best - 0.0001 * i - 0.0001;
      alts.push(x);
    }
    alts[0] = clone(a.alternatives[0]);
    alts[0].is_played = false;
    played.equity = alts[playedAt].equity;
    alts[playedAt] = played;
    a.alternatives = alts;
    a.equity_loss = Math.round((best - played.equity) * 10000) / 10000;
    if (total !== null) a.alternatives_total = total;
    return [d, hit];
  };
  const decision = (d, hit) => readGvab(write_gvab(d)).games[hit.gi].plies[hit.pi].analysis;
  const flagged = (a) => a.alternatives.filter((x) => x.is_played).length;

  let [d, hit] = withAlts(1100, 3);
  let a = decision(d, hit);
  assert(a.alternatives.length === 1024 && a.alternatives_total === 1100,
    `4. 1100 alternatives are written as 1024 with alternatives_total 1100 (got ${a.alternatives.length}, ${a.alternatives_total})`);
  assert(flagged(a) === 1, '4. the played move, within the cut, is still the flagged one');
  [d, hit] = withAlts(1100, 1050);
  a = decision(d, hit);
  assert(a.alternatives.length === 1024 && a.alternatives_total === 1100 && flagged(a) === 1,
    '4. a played move past the cut is kept in the list');
  [d, hit] = withAlts(1100, 3, 5000);
  assert(decision(d, hit).alternatives_total === 5000,
    '4. a larger total the decision already states is kept');
  [d, hit] = withAlts(1024, 3);
  a = decision(d, hit);
  assert(a.alternatives.length === 1024 && a.alternatives_total === undefined,
    '4. exactly 1024 is not truncated and states no total');
}

console.log('\n--- 5. games and plies ---');
{
  let d = clone(fields.midmatch);
  d.games = Array.from({ length: 1001 }, () => clone(d.games[0]));
  assert(throwsWith(() => write_gvab(d), 'too many games'), '5. 1001 games are refused');
  d = clone(fields.midmatch);
  d.games[0].plies = Array.from({ length: 1501 }, () => d.games[0].plies[0]);
  assert(throwsWith(() => write_gvab(d), 'too many plies'), '5. 1501 plies in a game are refused');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
