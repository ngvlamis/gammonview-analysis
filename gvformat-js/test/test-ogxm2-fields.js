// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// The OGXM v2 match, player and game fields as document keys. Mirrors
// tests/test_ogxm2_fields.py -- keep the two in step.
//
//   (a) Each synthetic document of tests/ogxm2_fields_cases.py
//       (fixtures/ogxm2/fields-docs.json) is written with the JavaScript codec;
//       the bytes must be the Python writer's (fields-sha256.json), read back to
//       the document, and rewrite byte for byte.
//   (b) fields.ogxm is a file the reference codec wrote using every field. It
//       reads into fields.expected.json and rewrites byte for byte, and edits to
//       it keep what they do not touch.
//   (c) The score, the cube and the opening position flow into the positions;
//       a value v2 cannot hold travels in an annotation and comes back whole.
// The reference library itself is Python's to run; here the same structure is
// checked without it.

import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deepStrictEqual } from 'node:assert';
import { write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { KEY } from '../src/ogxm2_passthrough.js';
import {
  _walkSections, _decodeAnno, _decodeMtch, GV_PREFIX, GV_KEY_ANALYSIS, GV_KEY_SITE, SCOPE_MATCH, SCOPE_GAME,
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

const here = dirname(fileURLToPath(import.meta.url));
const dir = join(here, 'fixtures', 'ogxm2');
const toU8 = (buf) => new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
const docs = JSON.parse(readFileSync(join(dir, 'fields-docs.json'), 'utf8'));
const pinned = JSON.parse(readFileSync(join(dir, 'fields-sha256.json'), 'utf8'));
const FOREIGN = toU8(readFileSync(join(dir, 'fields.ogxm')));
const expectedForeign = JSON.parse(readFileSync(join(dir, 'fields.expected.json'), 'utf8'));

const gvKeys = (data) => {
  const out = new Set();
  for (const s of _walkSections(data, data.length)) {
    if (s.type !== 'ANNO') continue;
    for (const r of _decodeAnno(s.payload)) {
      if ((r.key || '').startsWith(GV_PREFIX) && !r.key.startsWith(GV_KEY_ANALYSIS)) {
        out.add(`${r.scope}/${r.ref}/${r.key}`);
      }
    }
  }
  return out;
};

// (a) every document
for (const [name, doc] of Object.entries(docs)) {
  const data = write_gvab(doc);
  const want = pinned[name];
  const sha = createHash('sha256').update(data).digest('hex');
  assert(want !== undefined && data.length === want.length && sha === want.sha256,
    `${name}: bytes equal the Python writer's (length ${data.length}, sha256 ${sha.slice(0, 12)})`);
  const back = readGvab(data);
  delete back[KEY];                       // the reference's stated score_final, in `foreign`
  assert(same(back, doc), `${name}: read(write(D)) == D`);
  assert(bytesEqual(write_gvab(readGvab(data)), data), `${name}: rewriting is byte-stable`);
  assert(bytesEqual(write_gvab(plain(readGvab(data))), data), `${name}: and so is the .gva route`);
}

// (b) the reference's file
const f = readGvab(FOREIGN);
const held = f[KEY];
delete f[KEY];
assert(same(f, expectedForeign), 'fields.ogxm reads into the document Python reads');
assert(same(f, docs.foreign), 'and into the document the case states');
assert(held !== undefined && !(held.anno || []).length && !(held.unknown || []).length
  && !Object.keys(held.blocks || {}).length,
  'all that is kept beside it is the stated final score, which is derived');
assert(bytesEqual(write_gvab(readGvab(FOREIGN)), FOREIGN), 'rewriting it unedited is byte for byte the same');
assert(bytesEqual(write_gvab(plain(readGvab(FOREIGN))), FOREIGN), 'and so is the .gva route');

function edited(change) {
  const d = readGvab(FOREIGN);
  change(d);
  const out = write_gvab(d);
  return [out, readGvab(out)];
}
let [out, back] = edited((d) => { d.player_white = 'Alicia'; });
const renamed = readGvab(FOREIGN);
delete renamed[KEY];
renamed.player_white = 'Alicia';
delete back[KEY];
assert(same(back, renamed), 'a rename keeps every other field');
const startedAt = (data) => {
  const m = _walkSections(data, data.length).find((x) => x.type === 'MTCH');
  return _decodeMtch(m.payload).started_at;
};
[out, back] = edited((d) => { d.player_white = 'Alicia'; });
assert(startedAt(FOREIGN) % 1000n === 123n && startedAt(out) % 1000n === 123n,
  'a rename keeps the milliseconds the source stated for the start (P4)');
[out, back] = edited((d) => { d.timestamp += 1; });
assert(startedAt(out) === BigInt(back.timestamp) * 1000n, 'and a changed second is written as a whole one');
[out, back] = edited((d) => { d.event = null; });
assert(!back.event && back.event_year === 2026 && gvKeys(out).has(`${SCOPE_MATCH}/0/x-gammonview-event_year`),
  'an event removed keeps its year, in an annotation (a year needs an event)');
[out, back] = edited((d) => { d.city = 'X'.repeat(90); });
assert(back.city === 'X'.repeat(90) && back.site === 'Oslo',
  'a city too long for v2 comes back whole; the site has its own record');
[out, back] = edited((d) => { delete d.platform; });
assert(back.platform === undefined && back.match_ref === 'm-2026.10_7'
  && back.white_profile.user_id === 'u-1001',
  'a platform removed leaves the match reference and the user ids, in annotations');
[out, back] = edited((d) => { d.white_profile.rating = 1600.5; });
assert(back.white_profile.rating === 1600.5 && same(back.black_profile, f.black_profile),
  'a rating edit keeps the other profile');
[out, back] = edited((d) => { d.games[0].initial_cube_value = 8; });
assert(back.games[0].initial_cube_value === 8, "a game's cube edit");
[out, back] = edited((d) => { d.score_start = [1, 6]; });
assert(back.score_start[0] === 1 && back.score_start[1] === 6
  && back.games[0].plies[0].ogid_before.split(':').slice(6, 8).join(':') === '1:6',
  'a starting score edit, and every position follows it');

// (c) what each field does to the positions
const mid = readGvab(write_gvab(docs.midmatch));
assert(mid.score_start[0] === 2 && mid.white_score === 7 && mid.black_score === 2
  && !mid.games[0].is_crawford && mid.games[1].is_crawford,
  'a match joined at 2-2 ends 7-2, and its Crawford game is found from the start');
assert(mid.games[0].plies[0].ogid_before.split(':').slice(6, 9).join(':') === '2:2:7'
  && mid.games[1].plies[0].ogid_before.split(':').slice(6, 9).join(':') === '6:2:7C',
  'the first position says 2-2, the second 6-2 Crawford');
const cube = readGvab(write_gvab(docs['pre-turned-cube']));
assert(cube.games.map((g) => g.plies[0].ogid_before.split(':')[2]).join() === 'B1N,N2N,W3N',
  'games open with the cube as stated: owned 2, a centred 4, an 8 owned');
const hyper = readGvab(write_gvab(docs.hypergammon));
const nack = readGvab(write_gvab(docs.nackgammon));
assert(hyper.variant === 2 && hyper.games[0].plies[0].ogid_before.endsWith(':3')
  && nack.variant === 1 && !nack.games[0].plies[0].ogid_before.endsWith(':3')
  && nack.games[0].plies[0].ogid_before.startsWith('1122cccchhhjjjj:6666888ddddnnoo:'),
  "a variant opens from its own position; only hypergammon's OGID states its checker count");
const ctxData = write_gvab(docs.context);
assert(gvKeys(ctxData).size === 0 && readGvab(ctxData).site === 'Oslo',
  'a context that v2 holds needs no annotation, and the site is the city');

const un = gvKeys(write_gvab(docs.unholdable));
const expected = new Set([
  ...['event_year', 'date_precision', 'stage', 'round', 'table', 'city', 'country', 'event_url',
    'platform', 'match_ref', 'player_seat', 'rules_other', 'site', 'white_profile.user_id',
    'white_profile.rating', 'white_profile.rating_system', 'white_profile.country',
    'black_profile.user_id', 'black_profile.rating']
    .map((k) => `${SCOPE_MATCH}/0/${GV_PREFIX}${k}`),
  ...['initial_cube_value', 'initial_cube_owner', 'auto_doubles']
    .map((k) => `${SCOPE_GAME}/0/${GV_PREFIX}${k}`),
  `${SCOPE_GAME}/1/${GV_PREFIX}initial_cube_value`,
]);
assert(un.size === expected.size && [...expected].every((k) => un.has(k)),
  `each value v2 cannot hold is in an annotation, and nothing else is (${[...un].filter((k) => !expected.has(k))})`);

// The read-back rules (profile section 4)
{
  const ex = structuredClone(docs.extras);
  const resign = ex.games[1].plies.find((p) => p.action_id === 27);
  resign.resign_value = 1;                          // what the points and the cube derive
  ex.games[0].plies.find((p) => p.action_id === 34).settle_value = 4.0000004;
  const exBack = readGvab(write_gvab(ex));
  assert(exBack.games[1].plies.find((p) => p.action_id === 27).resign_value === undefined
    && exBack.games[2].plies.find((p) => p.action_id === 27).resign_value === 2,
  'rule resign-derived: a resign_value the points and cube give is not kept; another is');
  assert(exBack.games[0].plies.find((p) => p.action_id === 34).settle_value === 4,
    'rule settle-millionths: a settlement is held to a millionth of a point');
  const bare = structuredClone(docs.context);
  delete bare.site;
  assert(readGvab(write_gvab(bare)).site === 'Oslo',
    'rule site-follows-place: a document with a city and no site reads with the city as its site');
}

// defaults are not written
const dflt = structuredClone(docs.midmatch);
delete dflt.score_start;
const withDefaults = structuredClone(dflt);
Object.assign(withDefaults, {
  variant: 0, score_start: [0, 0], rated: false, crawford_before_start: false, auto_doubles: false,
  rules_other: 0, completed_at: null,
});
for (const g of withDefaults.games) {
  Object.assign(g, { initial_cube_value: 1, initial_cube_owner: 2, auto_doubles: 0 });
}
assert(bytesEqual(write_gvab(withDefaults), write_gvab(dflt)),
  "a value equal to v2's default is not written");

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
