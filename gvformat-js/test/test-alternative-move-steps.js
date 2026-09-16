// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Tests for the step split behind an *alternative's* `move`.
//
// Sibling of test-xg-move-steps.js / test-bgf-move-steps.js, which cover the
// same split for the move that was actually played. The played move had to be
// fixed because its steps are replayed into the running board: a hop onto a
// point the opponent has made reads back as a hit and corrupts every board from
// that ply on.
//
// An alternative is never replayed, so a bad split there cannot corrupt a
// match -- but the steps are exactly what the viewer draws its move arrows
// from, so the rejected candidate is *shown* with a checker landing on a stack
// of enemy checkers and then carrying on. Same root cause, visible instead of
// fatal.
//
// Mirrors tests/test_alternative_move_steps.py.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { _flipBoard, _checkerAnalysis } from '../src/export.js';
import { buildAlternatives } from '../src/bgf2gva.js';
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

// The same shape the played-move tests use: a checker on the mover's 24-point
// runs to 18 with a 5-1. Going 5 first lands on 19, which the opponent has five
// checkers on; going 1 first stops on 23, which is open.
const primed19 = new Array(26).fill(0);
primed19[24] = 1;
primed19[19] = -5;

const OPTIONS = [{
  equity: 0.1, played: true, move: '24/18', notation: '24/18',
  probs: [0.5, 0.1, 0.01, 0.1, 0.01],
}];

// --- 1. The BGF alternatives builder ---------------------------------------

assert(JSON.stringify(pips(buildAlternatives(OPTIONS, false, 5, 1, primed19)[0].move)) === '[1,5]',
       'bgf: an alternative steps around a made intermediate (1 first, via 23)');
assert(JSON.stringify(pips(buildAlternatives(OPTIONS, false, 5, 1)[0].move)) === '[5,1]',
       'bgf: with no board the canonical larger-die-first order is kept');

// --- 2. The analyze-path alternatives builder ------------------------------

const entry = { move_options: OPTIONS };
assert(JSON.stringify(pips(_checkerAnalysis(entry, false, 5, 1, 0, primed19).alternatives[0].move)) === '[1,5]',
       'analyze: an alternative steps around a made intermediate');
assert(JSON.stringify(pips(_checkerAnalysis(entry, false, 5, 1, 0).alternatives[0].move)) === '[5,1]',
       'analyze: with no board the canonical larger-die-first order is kept');

// --- 3. The whole-file invariant -------------------------------------------
// A play cannot change how many checkers the *opponent* has. Bearing off only
// ever removes the mover's own, and a real hit moves an opponent checker to the
// bar rather than off the board -- so the count is exactly conserved.
//
// A hop through a made point breaks it: the replay scores it as a hit, and a
// point holding five enemy checkers collapses to one of the mover's plus a
// single bar checker, so the opponent silently loses four. (Counting *up* to an
// impossible 16 is the same corruption seen several plies later, once the wrong
// board has been played on; one ply in isolation only ever loses checkers.)

function toP1(ogid) {
  const st = parseOgid(ogid);
  const board = Array.from(st.board);
  return st.onRoll === 'W' ? board : _flipBoard(board);
}

// Points 1-24 are signed (+white / -black); the two bars are unsigned counts
// at either end -- index 25 is white's, index 0 is black's (see _flipBoard,
// which swaps them without negating).
function sideCounts(boardP1) {
  let white = boardP1[25], black = boardP1[0];
  for (let i = 1; i <= 24; i++) {
    if (boardP1[i] > 0) white += boardP1[i];
    else black -= boardP1[i];
  }
  return [white, black];
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
  let stolen = 0;
  for (const file of xgFiles) {
    const buf = fs.readFileSync(path.join(SAMPLES_DIR, file));
    const gva = await convertXg(new Uint8Array(buf));
    for (const game of gva.games) {
      for (const ply of game.plies) {
        const alts = ply.analysis?.alternatives;
        if (!alts || !ply.ogid_before) continue;
        const moverIsWhite = Boolean(ply.color);
        const before = toP1(ply.ogid_before);
        const oppBefore = sideCounts(before)[moverIsWhite ? 1 : 0];
        for (const alt of alts) {
          if (!alt.move?.length) continue;
          total++;
          const after = _applyMovesP1(before, alt.move, moverIsWhite);
          const oppAfter = sideCounts(after)[moverIsWhite ? 1 : 0];
          if (oppAfter !== oppBefore) {
            stolen++;
            if (stolen <= 3) {
              console.error(`      ${file}: dice ${ply.d1}-${ply.d2} "${alt.notation}"`
                            + ` -> ${JSON.stringify(alt.move)}`
                            + ` (opponent ${oppBefore} -> ${oppAfter})`);
            }
          }
        }
      }
    }
  }
  assert(total > 0, `corpus alternatives replayed (${total})`);
  assert(stolen === 0,
         `no alternative's steps take checkers off the opponent (${stolen} do)`);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
