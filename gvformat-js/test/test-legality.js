// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Legality tests — JS mirror of tests/test_legality.py. Keep the two in sync.
//
// Boards are mover-relative: index 1 = the mover's ace point, 24 = entry
// point, 25 = the mover's own bar. See src/legality.js.

import { isPlayLegal, legalPlays, maxDicePlayable, normalizePlay } from '../src/legality.js';

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

/** Build a 26-slot count array from {point: count} ('bar' allowed). */
function board(spec = {}) {
  const b = new Array(26).fill(0);
  for (const [k, v] of Object.entries(spec)) {
    b[k === 'bar' ? 25 : Number(k)] = v;
  }
  return b;
}

// --- Opening position sanity -----------------------------------------------

const OPEN_MINE = board({ 6: 5, 8: 3, 13: 5, 24: 2 });
const OPEN_OPP = board({ 1: 2, 12: 5, 17: 3, 19: 5 });

assert(isPlayLegal(OPEN_MINE, OPEN_OPP, 5, 3, [[8, 3], [6, 3]]),
  'opening 5-3: 8/3 6/3 is legal');
assert(isPlayLegal(OPEN_MINE, OPEN_OPP, 5, 3, [[6, 3], [8, 3]]),
  'sub-move order does not matter');
assert(!isPlayLegal(OPEN_MINE, OPEN_OPP, 5, 3, [[8, 3]]),
  'opening 5-3: playing only one die is illegal when both are playable');
assert(!isPlayLegal(OPEN_MINE, OPEN_OPP, 5, 3, [[13, 7], [8, 3]]),
  'opening 5-3: 13/7 uses a 6, not a die in hand');
assert(!isPlayLegal(OPEN_MINE, OPEN_OPP, 5, 3, [[24, 19], [8, 3]]),
  'cannot land on an opponent point held by 5 checkers');

// --- Dancing and entry -----------------------------------------------------

const FULL_CLOSED = board({ 19: 2, 20: 2, 21: 2, 22: 2, 23: 2, 24: 2 });
const BAR_MINE = board({ bar: 1, 13: 5 });

assert(maxDicePlayable(BAR_MINE, FULL_CLOSED, 6, 5) === 0,
  'fully closed board with a checker on the bar is a forced dance');
assert(isPlayLegal(BAR_MINE, FULL_CLOSED, 6, 5, []),
  'playing nothing is legal when it is a genuine dance');

const OPEN_24 = board({ 19: 2, 20: 2, 21: 2, 22: 2, 23: 2 });
assert(!isPlayLegal(BAR_MINE, OPEN_24, 1, 3, []),
  'claiming a dance is illegal when entry with a 1 exists');

// Larger-die rule: the mover's last checker is on the bar. A 6 enters on 19
// and a 3 on 22, but point 16 is blocked, so whichever entry is made the other
// die is dead — exactly one die is playable, and it must be the 6.
const LAST_CHECKER = board({ bar: 1 });
const BLOCK_16 = board({ 16: 2 });
assert(isPlayLegal(LAST_CHECKER, BLOCK_16, 6, 3, [[25, 19]]),
  'when only one die can be played, entering with the larger (6) is legal');
assert(!isPlayLegal(LAST_CHECKER, BLOCK_16, 6, 3, [[25, 22]]),
  'entering with the smaller (3) is illegal when the 6 is also playable');
assert(maxDicePlayable(LAST_CHECKER, BLOCK_16, 6, 3) === 1,
  'only one die is playable in that position');

// --- Doubles ---------------------------------------------------------------

const DBL_MINE = board({ 6: 2, 8: 2 });
const EMPTY = board();
assert(legalPlays(DBL_MINE, EMPTY, 1, 1).diceUsed === 4,
  'doubles 1-1 with open board must use all four dice');
assert(!isPlayLegal(DBL_MINE, EMPTY, 1, 1, [[6, 5], [6, 5], [8, 7]]),
  'playing only 3 of 4 doubles is illegal (the real g2 m35 error)');
assert(isPlayLegal(DBL_MINE, EMPTY, 1, 1, [[6, 5], [6, 5], [8, 7], [8, 7]]),
  'playing all 4 doubles is legal');

// --- Bearing off -----------------------------------------------------------

const OFF_MINE = board({ 1: 1, 3: 1 });
assert(isPlayLegal(OFF_MINE, EMPTY, 4, 1, [[3, 0], [1, 0]]),
  'bear off both: 3/off with a 4 (overshoot from highest) and 1/off');
assert(isPlayLegal(OFF_MINE, EMPTY, 4, 1, [[3, 2], [2, 0]]),
  '3/2 2/off is an alternative legal play for 4-1');
// The bare [3, off] describes the same end position as 3/2 2/off (checker on 1
// remains), and that position is legally reachable using both dice -- so it is
// legal. Legality is about the resulting position, not the sub-move spelling.
assert(isPlayLegal(OFF_MINE, EMPTY, 4, 1, [[3, 0]]),
  'net-form 3/off is legal: same position as 3/2 2/off, reached with both dice');
// A genuine under-play: with two on the 6 and a 6-1, bearing one off obliges the
// 1 to be played on the other checker (6/5). Stopping after one bear-off leaves
// a position no maximal play reaches, so it is illegal (the g6 m41 error shape).
assert(!isPlayLegal(board({ 6: 2 }), EMPTY, 6, 1, [[6, 0]]),
  'bearing off only one checker is illegal when the other die must still play');

const HIGH_MINE = board({ 2: 1, 5: 1 });
assert(!isPlayLegal(HIGH_MINE, EMPTY, 6, 1, [[2, 0], [5, 4]]),
  'cannot bear off from point 2 with a 6 while point 5 is occupied');
assert(isPlayLegal(HIGH_MINE, EMPTY, 6, 1, [[5, 0], [2, 1]]),
  '6 bears off the highest checker (point 5)');

const NOT_HOME = board({ 3: 1, 9: 1 });
assert(!isPlayLegal(NOT_HOME, EMPTY, 3, 1, [[3, 0], [9, 8]]),
  'no bearing off while a checker sits outside the home board');

// --- Hitting ---------------------------------------------------------------

const HIT_MINE = board({ 6: 2, 8: 1 });
const HIT_OPP = board({ 3: 1, 5: 2 });
assert(isPlayLegal(HIT_MINE, HIT_OPP, 5, 2, [[8, 3], [6, 4]]),
  'landing on a lone opponent blot is legal (a hit)');
assert(!isPlayLegal(HIT_MINE, HIT_OPP, 1, 2, [[6, 5], [8, 6]]),
  'landing on an opponent point of 2 is illegal');

// --- normalizePlay ---------------------------------------------------------

assert(normalizePlay([[8, 3], [6, 3]]) === '6/3,8/3', 'normalize sorts pairs');
assert(normalizePlay([[3, -2]]) === '3/0', 'normalize maps bear-off to 0');
assert(normalizePlay([[-1, -1], [5, 2]]) === '5/2', 'normalize drops empty slots');

// --- Combined multi-die pairs (BGBlitz records a checker moved with more than
//     one die as a single from->to, e.g. 24/15 for a 6-3 rather than 24/18 18/15) -
assert(isPlayLegal(OPEN_MINE, OPEN_OPP, 6, 3, [[24, 15]]),
  'combined 24/15 (one checker, both dice) is legal');
assert(isPlayLegal(OPEN_MINE, OPEN_OPP, 6, 3, [[24, 18], [18, 15]]),
  'the same play spelled out per-die is legal too');
assert(!isPlayLegal(
  board({ 24: 2, 13: 2, 8: 3, 6: 5 }), board({ 18: 2, 21: 2, 1: 2, 12: 5 }),
  6, 3, [[24, 15]]),
  'combined 24/15 is illegal when both intermediates (18 and 21) are blocked');
assert(isPlayLegal(board({ 8: 2 }), EMPTY, 2, 2, [[8, 4], [8, 4]]),
  'combined doubles: both checkers 8/4 (2-2) is legal');
assert(isPlayLegal(board({ 5: 1 }), EMPTY, 2, 3, [[5, 0]]),
  'combined forced two-die bear-off 5/off (2-3, no single die reaches off) is legal');

console.log(`\n${passed + failed} tests, ${failed} failures`);
if (failed) process.exit(1);
