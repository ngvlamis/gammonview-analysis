// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Tests that XG's eval levels survive a .gvab round trip.
//
// XG names its own analysis levels -- "XG Roller++", the opening book -- and the
// converter used to carry those names through verbatim ("xgroller++", "ob_v2").
// Nothing could store them: GVAN encodes a level as one byte, a 4-bit depth plus
// the truncated/rollout/database flags, so an unknown name encoded as 0, and 0
// means "same as the header level" on read. A match dragged into the viewer
// showed "xgroller++" on the alternatives XG had judged that way; the same match
// saved to an account and reloaded showed the header's plain ply depth on them
// instead -- silently, with no way to tell the two apart.
//
// The converter now emits the canonical names for those codes, which describe
// what XG is actually doing: the three XG Roller settings are short truncated
// rollouts, and the opening book is a lookup rather than a search.
//
// Section 3 covers the *cube* rows, which had a second version of the same
// complaint: the level was there in the file and not in the document. Only the
// doubler ply reported one. A take, a pass, a held cube and a missed double
// each showed nothing -- and since a match holds far more held cubes than
// doubles, most of the cube decisions in an XG match had no level on them. The
// doubler's own level was wrong besides: it was read from the *following move
// record* rather than the cube record, and XG searches a cube deeper than the
// roll after it often enough that 12 of this corpus's 30 in-move doubles
// reported the shallower of the two.
//
// Mirrors tests/test_xg_eval_levels.py.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { _encode_eval_level, write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { convertXg, _evalLevelName } from '../src/xg2gva.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SAMPLES_DIR = path.join(__dirname, '..', '..', 'samples', 'xg');

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

/** Every alternative's eval level, in document order. */
function altLevels(doc) {
  const out = [];
  for (const game of doc.games) {
    for (const ply of game.plies) {
      for (const alt of ply.analysis?.alternatives ?? []) out.push(alt.eval_level ?? null);
    }
  }
  return out;
}

/**
 * Every cube decision's eval level, in document order, as `[what, level]`.
 *
 * "Cube decision" is all five shapes one takes in a document: the three
 * standalone cube plies (double, take, pass) and the two sub-objects a checker
 * ply carries when the cube was live (a held cube, or a double that should have
 * been offered). All five are the same XG cube record underneath, so all five
 * have a level to report.
 */
function cubeLevels(doc) {
  const out = [];
  for (const game of doc.games) {
    for (const ply of game.plies) {
      const a = ply.analysis;
      if (!a) continue;
      if (ply.action_id >= 21 && ply.action_id <= 23) {
        out.push([`action${ply.action_id}`, a.eval_level ?? null]);
      }
      for (const key of ['missed_double', 'cube_decision']) {
        if (a[key]) out.push([key, a[key].eval_level ?? null]);
      }
    }
  }
  return out;
}

// --- 1. The codes XG uses for its non-ply levels ----------------------------
assert(_evalLevelName(1000) === 'truncated1', 'XG Roller -> truncated1');
assert(_evalLevelName(1001) === 'truncated2', 'XG Roller+ -> truncated2');
assert(_evalLevelName(1002) === 'truncated3', 'XG Roller++ -> truncated3');
assert(_evalLevelName(998) === 'database', 'opening book v2 -> database');
assert(_evalLevelName(999) === 'database', 'opening book v1 -> database');

// Every name the converter can produce has to encode to a non-zero byte, since
// zero is not "unknown" but "same as the header level".
{
  const codes = [0, 1, 2, 3, 4, 5, 6, 12, 100, 998, 999, 1000, 1001, 1002];
  const unstorable = codes.filter(c => !_encode_eval_level(_evalLevelName(c)));
  assert(unstorable.length === 0,
         `every known XG level encodes to a real byte (unstorable: ${unstorable})`);
}

// An unrecognised code says nothing rather than inventing a name no `.gvab` can
// hold -- the same trap under a different label.
assert(_evalLevelName(7777) === null, "an unknown level code -> null, not 'level_7777'");

// --- 2. The round trip, over the corpus ------------------------------------
const xgFiles = fs.existsSync(SAMPLES_DIR)
  ? fs.readdirSync(SAMPLES_DIR).filter(f => f.endsWith('.xg')).sort()
  : [];

if (xgFiles.length === 0) {
  console.log(`SKIP: no sample .xg files at ${SAMPLES_DIR}`);
} else {
  const seen = new Set();
  const lossy = [];
  for (const file of xgFiles) {
    const doc = await convertXg(new Uint8Array(fs.readFileSync(path.join(SAMPLES_DIR, file))));
    const before = altLevels(doc);
    for (const lvl of before) if (lvl) seen.add(lvl);
    const after = altLevels(readGvab(write_gvab(doc)));
    if (JSON.stringify(before) !== JSON.stringify(after)) {
      const differing = before
        .map((b, i) => [b, after[i]])
        .filter(([b, a]) => b !== a)
        .slice(0, 3);
      lossy.push(`${file}: ${JSON.stringify(differing)}`);
    }
  }

  assert(lossy.length === 0,
         `alternative levels survive write+read (${lossy.slice(0, 3).join('; ')})`);

  // The corpus has to actually contain the levels this is about, or the check
  // above passes on files that never exercised it.
  for (const level of ['truncated1', 'truncated2', 'truncated3', 'database']) {
    assert(seen.has(level), `the corpus exercises ${level}`);
  }

  // --- 3. Cube rows report a level, and it is the cube's own ---------------
  const blank = [];
  const cubeLossy = [];
  const cubeSeen = new Set();
  let triples = 0;
  let deeperCube = 0;

  for (const file of xgFiles) {
    const doc = await convertXg(new Uint8Array(fs.readFileSync(path.join(SAMPLES_DIR, file))));

    const before = cubeLevels(doc);
    for (const [what, lvl] of before) {
      if (lvl) cubeSeen.add(lvl);
      else blank.push(`${file}: ${what}`);
    }

    const after = cubeLevels(readGvab(write_gvab(doc)));
    // Reading back derives the companion `cube_decision` beside each
    // `missed_double` the spec says belongs there, so the two lists are not
    // the same length; compare as counted multisets of what survived.
    const countBy = (rows) => {
      const m = new Map();
      for (const [what, lvl] of rows) {
        const k = `${what}=${lvl}`;
        m.set(k, (m.get(k) ?? 0) + 1);
      }
      return m;
    };
    const b = countBy(before);
    const a = countBy(after);
    for (const [k, n] of b) {
      if ((a.get(k) ?? 0) < n) cubeLossy.push(`${file}: ${k} (${n} -> ${a.get(k) ?? 0})`);
    }

    // A double, its take, and the checker play that followed: necessarily an
    // in-move double, the case that used to take its level from that play's
    // move record. If it still did, these two could never disagree.
    for (const game of doc.games) {
      const ps = game.plies;
      for (let i = 0; i + 2 < ps.length; i++) {
        if (ps[i].action_id !== 21 || ps[i + 1].action_id !== 22) continue;
        if (!(ps[i + 2].action_id >= 0 && ps[i + 2].action_id <= 20)) continue;
        triples++;
        const dbl = ps[i].analysis?.eval_level;
        const play = ps[i + 2].analysis?.eval_level;
        if (dbl && play && dbl !== play) deeperCube++;
      }
    }
  }

  assert(blank.length === 0,
         `every analyzed cube row reports a level (${blank.length} blank: ${blank.slice(0, 3).join('; ')})`);
  assert(cubeLossy.length === 0,
         `cube levels survive write+read (${cubeLossy.slice(0, 3).join('; ')})`);
  // Not decoration: a corpus of nothing but plain ply depths would pass the
  // two checks above without ever running a cube level through the mapping.
  assert([...cubeSeen].some(l => !/^\d+ply$/.test(l)),
         `the corpus exercises a non-ply level on a cube row (saw: ${[...cubeSeen].sort().join(', ')})`);
  assert(triples > 0, `the corpus has in-move doubles to check (${triples})`);
  assert(deeperCube > 0,
         'an in-move double reports the cube record\'s level, not the following play\'s '
         + `(${deeperCube} of ${triples} disagree)`);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
