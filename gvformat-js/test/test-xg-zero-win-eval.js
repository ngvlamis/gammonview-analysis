// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Tests that an evaluation XG did record survives even when its win
// probability is exactly zero.
//
// The converter used to decide "did XG evaluate this?" by asking whether the
// win probability was above zero. That is the wrong question. A play in a
// hopelessly lost position reads win = 0.0 with a real gammon_loss beside it,
// and the last few plies of a lost game are full of them -- so the analysis
// panel showed those rows with an equity and a blank set of probabilities,
// including on the top-ranked play. Every match in the sample corpus but one
// contains such a ply.
//
// What the guard was really protecting against is a record XG never wrote
// into, which reads as five zero probabilities *and* a zero equity. A genuine
// evaluation cannot look like that: equity 0 means a roughly even game, which
// cannot sit beside a zero win probability. So the pair separates them.
//
// Mirrors tests/test_xg_zero_win_eval.py.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { convertXg, _hasEval } from '../src/xg2gva.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SAMPLES_DIR = path.join(__dirname, '..', '..', 'samples', 'xg');

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

// --- 1. The predicate itself ----------------------------------------------
// [win, gammon_win, bg_win, gammon_loss, bg_loss]
assert(_hasEval([0, 0, 0, 0, 0], 0) === false,
       'an unwritten record (five zeros, zero equity) is not an evaluation');
assert(_hasEval([0, 0, 0, 0.1157, 0], -1.1186) === true,
       'zero win beside a real gammon_loss is an evaluation');
assert(_hasEval([0, 0, 0, 1, 0], -2.0251) === true,
       'a certain gammon loss is an evaluation');
assert(_hasEval([0, 0, 0, 0, 0], -1) === true,
       'a certain plain loss -- all five zero -- is an evaluation, by its equity');
assert(_hasEval([0.5, 0.13, 0.006, 0.129, 0.004], 0.0152) === true,
       'an ordinary evaluation is an evaluation');

// --- 2. The corpus ---------------------------------------------------------
const xgFiles = fs.existsSync(SAMPLES_DIR)
  ? fs.readdirSync(SAMPLES_DIR).filter(f => f.endsWith('.xg')).sort()
  : [];

if (xgFiles.length === 0) {
  console.log(`SKIP: no sample .xg files at ${SAMPLES_DIR}`);
} else {
  // Counted across the corpus so the assertions below can prove the corpus
  // actually reaches the case, rather than passing on files that never do.
  let zeroWin = 0;
  let allZero = 0;
  const blank = [];

  for (const file of xgFiles) {
    const doc = await convertXg(new Uint8Array(fs.readFileSync(path.join(SAMPLES_DIR, file))));
    for (const [gi, game] of doc.games.entries()) {
      for (const [pi, ply] of game.plies.entries()) {
        for (const [ai, alt] of (ply.analysis?.alternatives ?? []).entries()) {
          if (!alt.eval) { blank.push(`${file} g${gi + 1} ply${pi} alt${ai}`); continue; }
          const e = alt.eval;
          if (e.win === 0) zeroWin++;
          if (e.win === 0 && e.gammon_win === 0 && e.bg_win === 0
              && e.gammon_loss === 0 && e.bg_loss === 0) allZero++;
        }
      }
    }
  }

  // XG lists a candidate only when it evaluated one, so every alternative the
  // converter emits should carry probabilities.
  assert(blank.length === 0,
         `every alternative in the corpus carries probabilities (${blank.slice(0, 3).join('; ')})`);
  assert(zeroWin > 0, `the corpus exercises zero-win evaluations (${zeroWin} of them)`);
  assert(allZero > 0,
         `the corpus exercises the all-zero case a zero equity would reject (${allZero})`);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
