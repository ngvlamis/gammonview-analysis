// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Tests for the step split behind a BGF ply's `moves`.
//
// BGBlitz records only a move's endpoints. When one checker plays both dice --
// "18/7" off a 5-6 -- the intermediate point is not on the record and has to be
// inferred, and only one of the two candidates may be legal. Getting it wrong is
// not cosmetic: replaying a step onto a point the opponent has made reads as a
// hit, which turns their two checkers into one of yours plus a bar checker. The
// side gains a checker, and every board replayed from that ply onward is wrong
// -- far enough wrong that bgsage segfaults indexing its bearoff table with a
// 16-checker home board.
//
// Two levels here: the splitter itself, and the whole-file invariant that a
// ply's `moves` replay onto its own `ogid_after`. The second is what actually
// failed; it holds for every sample in the corpus, none of which happened to
// contain the shape (which is why this went unnoticed).
//
// Mirrors tests/test_bgf_move_steps.py.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { _notationToSteps, _flipBoard } from '../src/export.js';
import { convertBgf } from '../src/bgf2gva.js';
import { parseOgid } from '../src/ogid.js';
import { _applyMovesP1 } from '../src/reader.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SAMPLES_DIR = path.join(__dirname, '..', '..', 'samples', 'bgf');

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

const pips = steps => steps.map(s => s.pips);
const froms = steps => steps.map(s => s.from);

// --- 1. The splitter -------------------------------------------------------
// Mover-perspective board: own checkers positive, opponent's negative. A
// checker on 18 plays 5-6 to 7. Going 6 first lands on 12; going 5 first lands
// on 13. Only one is open at a time below.
const blocked12 = new Array(26).fill(0);
blocked12[18] = 1;
blocked12[12] = -2;          // opponent's made point: 18/12 is not playable

let steps = _notationToSteps('18/7', false, 5, 6, blocked12);
assert(JSON.stringify(pips(steps)) === '[5,6]',
       'a blocked intermediate is stepped around (5 first, via 13)');
assert(JSON.stringify(froms(steps)) === '[18,13]',
       'and the hop starts where the first die left it');

const blocked13 = new Array(26).fill(0);
blocked13[18] = 1;
blocked13[13] = -2;          // the mirror case: now 18/13 is the illegal one
assert(JSON.stringify(pips(_notationToSteps('18/7', false, 5, 6, blocked13))) === '[6,5]',
       'the other block sends it the other way (6 first, via 12)');

// A lone enemy checker is a blot, not a block -- landing there is a hit and a
// perfectly legal route, so the canonical larger-die-first order stands.
const blot12 = new Array(26).fill(0);
blot12[18] = 1;
blot12[12] = -1;
assert(JSON.stringify(pips(_notationToSteps('18/7', false, 5, 6, blot12))) === '[6,5]',
       'a blot does not divert the split');

// No board (an unplayed alternative, which has only a notation string): the
// tie-break still has to produce something deterministic.
assert(JSON.stringify(pips(_notationToSteps('18/7', false, 5, 6))) === '[6,5]',
       'with no board the canonical larger-die-first order is kept');

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
const bgfFiles = fs.existsSync(SAMPLES_DIR)
  ? fs.readdirSync(SAMPLES_DIR).filter(f => f.endsWith('.bgf')).sort()
  : [];

if (bgfFiles.length === 0) {
  console.log(`SKIP: no sample .bgf files at ${SAMPLES_DIR}`);
} else {
  let total = 0;
  let mismatched = 0;
  for (const file of bgfFiles) {
    const buf = fs.readFileSync(path.join(SAMPLES_DIR, file));
    const gva = await convertBgf(new Uint8Array(buf));
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
