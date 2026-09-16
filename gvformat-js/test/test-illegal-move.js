// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// The `illegal_move` flag, from the binary through to the aggregates -- the
// JavaScript half of tests/test_illegal_move.py, on the same file.
//
// The corpus holds one genuine illegal play (5nqfGw9bWG3deTaU, game 1,
// `14/10 13/12` off a 1-3: three die-moves for a two-hop roll). GammonView
// reads a .gvab in the browser and shows the illegal-move count from it, so
// this side has to agree with the Python codec rather than merely work: the
// same ply, the same count, before and after a rewrite.
//
// The synthetic checks in test-stats.js cover the counter's arithmetic. What
// they cannot cover is a real analysed file, where a count of 1 fails if the
// flag is dropped anywhere between the chunk and the aggregate -- and where
// the Python suite's own `illegal_moves` assertions would otherwise be
// comparing 0 to 0.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { read_gvab, write_gvab, compute_aggregates } from '../src/index.js';
import { DICE_TABLE } from '../src/constants.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// The shared corpus at samples/ -- the same files the Python suite reads.
const GVAB = path.join(__dirname, '..', '..', 'samples', 'gv', '5nqfGw9bWG3deTaU.gvab');

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) {
    passed++;
    console.log(`OK  ${msg}`);
  } else {
    failed++;
    console.error(`FAIL  ${msg}`);
  }
}

function flagged(doc) {
  const out = [];
  (doc.games || []).forEach((game, gi) => {
    (game.plies || []).forEach((ply, pi) => {
      const a = ply.analysis || {};
      if (a.illegal_move || ply.illegal_move) out.push([gi, pi, ply]);
    });
  });
  return out;
}

function done() {
  console.log(`\n${passed + failed} tests, ${failed} failures`);
  process.exit(failed > 0 ? 1 : 0);
}

// The corpus is committed, so this normally runs; the guard is for a partial
// checkout (same convention as test-mat.js).
if (!fs.existsSync(GVAB)) {
  console.log(`SKIP: ${GVAB} not found`);
  done();
}

const doc = read_gvab(new Uint8Array(fs.readFileSync(GVAB)));

// --- 1. The corpus still holds the illegal play -----------------------------
// Asserted, not assumed: everything below is vacuous on an empty set.
const flags = flagged(doc);
assert(flags.length === 1, `exactly one ply is flagged illegal (got ${flags.length})`);
if (flags.length !== 1) done();

const [gi, pi, ply] = flags[0];
const analysis = ply.analysis || {};
assert(analysis.illegal_move === true,
  'the flag decodes onto the ply analysis, where the format puts it');
assert(ply.action_id <= 20, `and it is a checker ply (action_id ${ply.action_id})`);
assert((ply.d1 === 1 && ply.d2 === 3) || (ply.d1 === 3 && ply.d2 === 1),
  `the known case: the 1-3 in game ${gi + 1} (got ${ply.d1}-${ply.d2})`);

// The steps were made to fit by fitMoveSteps: the played move used three
// die-moves, one more than a 1-3 allows, and was restated as one step per
// span. A ply that kept all three could not be encoded at all.
const room = DICE_TABLE[ply.action_id][2];
const steps = ply.moves || [];
assert(steps.length <= room,
  `its steps fit what the roll allows (${steps.length} of ${room})`);

// --- 2. It survives the binary ----------------------------------------------
const again = read_gvab(write_gvab(doc));
const flags2 = flagged(again).map(([g, p]) => `${g}:${p}`);
assert(flags2.length === 1 && flags2[0] === `${gi}:${pi}`,
  `write_gvab -> read_gvab keeps the flag on the same ply (${flags2.join(',') || 'none'})`);

// --- 3. compute_aggregates counts it ----------------------------------------
const agg = compute_aggregates(doc);
assert(agg.match.illegal_moves === 1,
  `the match reports 1 illegal move (got ${agg.match.illegal_moves})`);
const here = doc.games[gi].game_index === undefined ? gi : doc.games[gi].game_index;
const perGame = Object.fromEntries(agg.games.map(g => [g.game_index, g.illegal_moves]));
assert(perGame[here] === 1, 'attributed to the game it happened in');
assert(Object.values(perGame).reduce((a, b) => a + b, 0) === 1,
  `and to no other game (${JSON.stringify(perGame)})`);
assert(compute_aggregates(again).match.illegal_moves === 1,
  'the count survives the rewrite too');

// --- 4. The flag is not the PR denominator ----------------------------------
// Two independent axes: `illegal_move` says the played board matched no legal
// move, `decision` says whether the ply counts toward PR. Flipping `decision`
// has to move the total, or the counter is reading the wrong flag.
const color = ply.color === 1 ? 'white' : 'black';
assert(analysis.decision === false,
  'by default the illegal ply is not counted as a decision');

const before = agg.match[color].total_decisions;
analysis.decision = true;
const after = compute_aggregates(doc).match[color].total_decisions;
assert(after === before + 1,
  `and the denominator follows \`decision\`, not the flag (${color}: ${before} -> ${after})`);
assert(compute_aggregates(doc).match.illegal_moves === 1,
  'while the illegal-move count is unmoved by it');

done();
