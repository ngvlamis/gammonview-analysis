// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Tests for the step split behind an XG ply's `moves`.
//
// XG records a played move as from/to endpoints, so a checker that plays both
// dice -- "24/18" off a 5-1 -- leaves the intermediate point off the record. It
// has to be inferred, and only one of the two routes may be open. Getting it
// wrong is not cosmetic: a step onto a point the opponent has *made* replays as
// a hit, turning their five checkers into one of yours plus a bar checker. From
// that ply on every board is wrong -- far enough wrong that a later ply lifts a
// checker off an empty point and mints checkers until the position is
// impossible, which is where bgsage segfaults indexing its bearoff table.
//
// The BGF converter passes the pre-move board to `_notationToSteps` for exactly
// this reason (see test-bgf-move-steps.js); the XG one did not, and a 5-1 run
// off the midpoint past a made 5-prime is the shape that exposes it.
//
// Mirrors tests/test_xg_move_steps.py.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { _notationToSteps, _flipBoard } from '../src/export.js';
import { convertXg } from '../src/xg2gva.js';
import { parseOgid } from '../src/ogid.js';
import { _applyMovesP1 } from '../src/reader.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SAMPLES_DIR = path.join(__dirname, '..', '..', 'samples', 'xg');

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

const pips = steps => steps.map(s => s.pips);
const froms = steps => steps.map(s => s.from);

// --- 1. The splitter -------------------------------------------------------
// The real shape, from game 4 of a 13-point match: a checker on the mover's
// 24-point runs to 18 with a 5-1. Going 5 first lands on 19, which the opponent
// has five checkers on; going 1 first stops on 23, which is open.
const primed19 = new Array(26).fill(0);
primed19[24] = 1;
primed19[19] = -5;           // opponent's made point: 24/19 is not playable

let steps = _notationToSteps('24/18', false, 5, 1, primed19);
assert(JSON.stringify(pips(steps)) === '[1,5]',
       'a made intermediate is stepped around (1 first, via 23)');
assert(JSON.stringify(froms(steps)) === '[24,23]',
       'and the hop starts where the first die left it');

// Without the board the tie-break takes the larger die first and routes the
// checker straight through the made point -- the bug this guards.
assert(JSON.stringify(pips(_notationToSteps('24/18', false, 5, 1))) === '[5,1]',
       'with no board the canonical larger-die-first order is kept');

// A lone enemy checker is a blot, not a block: landing there is a hit and a
// perfectly legal route, so the canonical order stands.
const blot19 = new Array(26).fill(0);
blot19[24] = 1;
blot19[19] = -1;
assert(JSON.stringify(pips(_notationToSteps('24/18', false, 5, 1, blot19))) === '[5,1]',
       'a blot does not divert the split');

// --- 2. The whole-file invariant -------------------------------------------
// `parseOgid` hands back the board from the perspective of whoever owes the
// next action, which alternates every ply -- comparing without this flip makes
// every second ply look broken.
function toP1(ogid) {
  const st = parseOgid(ogid);
  const board = Array.from(st.board);
  return st.onRoll === 'W' ? board : _flipBoard(board);
}

// Reads the repository's shared corpus at samples/ -- the same files the
// Python suite uses, not a copy. Committed, so this normally runs; the
// guard is for a partial checkout (same convention as the other
// sample-backed suites here).
const xgFiles = fs.existsSync(SAMPLES_DIR)
  ? fs.readdirSync(SAMPLES_DIR).filter(f => f.endsWith('.xg')).sort()
  : [];

if (xgFiles.length === 0) {
  console.log(`SKIP: no sample .xg files at ${SAMPLES_DIR}`);
} else {
  let total = 0;
  let mismatched = 0;
  for (const file of xgFiles) {
    const buf = fs.readFileSync(path.join(SAMPLES_DIR, file));
    const gva = await convertXg(new Uint8Array(buf));
    for (const game of gva.games) {
      for (const ply of game.plies) {
        if (!ply.ogid_before || !ply.ogid_after || ply.d1 == null) continue;
        total++;
        const replayed = _applyMovesP1(toP1(ply.ogid_before), ply.moves || [], Boolean(ply.color));
        if (JSON.stringify(replayed) !== JSON.stringify(toP1(ply.ogid_after))) {
          mismatched++;
          if (mismatched <= 3) {
            console.error(`      ${file}: dice ${ply.d1}-${ply.d2} moves ${JSON.stringify(ply.moves)}`);
          }
        }
      }
    }
  }
  assert(total > 0, `corpus plies with dice replayed (${total})`);
  assert(mismatched === 0,
         `every ply's moves replay onto its own ogid_after (${mismatched} do not)`);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
