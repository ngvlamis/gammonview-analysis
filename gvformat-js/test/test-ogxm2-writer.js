// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// The OGXM v2 writer, against the Python writer's output. Mirrors
// tests/test_ogxm2_writer.py -- keep the two in step.
//
// The two writers must agree byte for byte: a file is the same file whichever
// side of GammonView wrote it.
//
//   (a) tests/golden/*.fast.gvab are v2 files written by Python. Reading one and
//       writing it back must give the same bytes, and reading it must give the
//       document Python's own reading of it (the matching .fast.gva) holds.
//   (b) samples/gv/*.gvab are v1. Writing each one's reading as v2 must hash to
//       what the Python writer made of it (fixtures/ogxm2/writer-sha256.json,
//       from tests/gen_js_v2_fixtures.py).
//   (c) A rewrite is byte-stable: write(read(b)) == b.
//   (d) samples/bgf/*.bgf, converted by the JS converter and written as v2, hash
//       to what Python's converter and writer made of them
//       (fixtures/ogxm2/bgf-writer-sha256.json) -- BGBlitz's own match equity
//       frame travels from the converter through the writer, and so do the two
//       points of a session's last game.

import { readFileSync, readdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deepStrictEqual } from 'node:assert';
import { write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { convertBgf } from '../src/bgf2gva.js';

let passed = 0, failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..');
const goldenDir = join(repo, 'tests', 'golden');
const samplesDir = join(repo, 'samples', 'gv');
const pinned = JSON.parse(readFileSync(join(here, 'fixtures', 'ogxm2', 'writer-sha256.json'), 'utf8'));

const toU8 = (buf) => new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);

// (a) the goldens
const goldens = readdirSync(goldenDir).filter((f) => f.endsWith('.fast.gvab')).sort();
assert(goldens.length > 0, 'found golden .fast.gvab files');
for (const name of goldens) {
  const data = toU8(readFileSync(join(goldenDir, name)));
  const doc = readGvab(data);
  assert(bytesEqual(write_gvab(doc), data), `${name}: write(read(golden)) is the golden, byte for byte`);
  const expected = JSON.parse(readFileSync(join(goldenDir, name.replace(/\.gvab$/, '.gva')), 'utf8'));
  let same = true;
  try { deepStrictEqual(JSON.parse(JSON.stringify(doc)), expected); } catch (e) { same = false; console.error(e.message.slice(0, 2000)); }
  assert(same, `${name}: reads to the document Python reads (the .gva)`);
}

// (b) + (c) the v1 samples
const samples = readdirSync(samplesDir).filter((f) => f.endsWith('.gvab')).sort();
assert(samples.length > 0, 'found samples/gv/*.gvab');
for (const name of samples) {
  const data = toU8(readFileSync(join(samplesDir, name)));
  const out = write_gvab(readGvab(data));
  const want = pinned[name];
  assert(want !== undefined, `${name}: has a pinned Python hash`);
  if (want === undefined) continue;
  const sha = createHash('sha256').update(out).digest('hex');
  assert(out.length === want.length && sha === want.sha256,
    `${name}: v2 bytes equal the Python writer's (length ${out.length}, sha256 ${sha.slice(0, 12)})`);
  assert(bytesEqual(write_gvab(readGvab(out)), out), `${name}: rewriting the v2 file is byte-stable`);
}

// (d) the BGBlitz samples, converted and written by JS
const pinnedBgf = JSON.parse(readFileSync(join(here, 'fixtures', 'ogxm2', 'bgf-writer-sha256.json'), 'utf8'));
const bgfDir = join(repo, 'samples', 'bgf');
const bgfs = readdirSync(bgfDir).filter((f) => f.endsWith('.bgf')).sort();
assert(bgfs.length > 0, 'found samples/bgf/*.bgf');
for (const name of bgfs) {
  const doc = await convertBgf(toU8(readFileSync(join(bgfDir, name))));
  const out = write_gvab(doc);
  const want = pinnedBgf[name];
  assert(want !== undefined, `${name}: has a pinned Python hash`);
  if (want === undefined) continue;
  const sha = createHash('sha256').update(out).digest('hex');
  assert(out.length === want.length && sha === want.sha256,
    `${name}: converted and written as v2, bytes equal Python's (length ${out.length}, sha256 ${sha.slice(0, 12)})`);
  assert(bytesEqual(write_gvab(readGvab(out)), out), `${name}: rewriting the v2 file is byte-stable`);
  if (doc.match_length > 0) {
    const framed = doc.games.flatMap((g) => g.plies).filter((p) => p.analysis);
    assert(framed.length > 0 && framed.every((p) => p.analysis.mwc_frame), `${name}: every analysed ply carries a frame`);
    assert(doc.analysis_info.met_id === 'bgblitz', `${name}: the block names BGBlitz's table`);
  }
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
