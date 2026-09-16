// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// An illegal play must not corrupt the boards that follow it.
// JS mirror of tests/test_illegal_play_steps.py. Keep the two in sync.
//
// A source can record a play that broke the rules (XG flags one with
// `invalidM === 2`; sites do let them through). Such a play can use more
// die-moves than the roll has -- 13/9 with a 3-1, then 12/11 -- and the natural
// step expansion then needs three steps for a two-hop roll. A ply record has
// room for exactly the roll's hops, so the third step used to be dropped on
// write; from that ply on the replayed board was one checker off, later plies
// lifted checkers off empty points, and the resulting 16-checker position
// segfaulted the engine's bearoff lookup.

import {
  _notationToSteps, _notationToStepsUnsplit, _p1ToAbsolute, _stepsPerRoll,
} from '../src/export.js';
import { boardProblems } from '../src/legality.js';
import { write_gvab } from '../src/binary.js';
import { readGvab, _absoluteToP1, _applyMovesP1 } from '../src/reader.js';

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

const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** P1/White frame board from {point: signed count} (25 = white bar). */
function boardP1(spec = {}) {
  const b = new Array(26).fill(0);
  for (const [k, v] of Object.entries(spec)) b[Number(k)] = v;
  return b;
}

// The real ply, in the mover's own numbering: one checker 13/9 (both dice on a
// 3-1) and then a third die-move, 12/11.
const ILLEGAL = '13/9 12/11';

// --- the expansion overflows, the collapse does not -------------------------

const split = _notationToSteps(ILLEGAL, true, 3, 1);
assert(split.length === 3, 'the dice split three hops out of an illegal 3-1 play');
assert(split.length > _stepsPerRoll(3, 1),
  'which is one more step than a 3-1 ply record holds');

const collapsed = _notationToStepsUnsplit(ILLEGAL, true, 3, 1);
assert(eq(collapsed, [{ from: 12, pips: 4 }, { from: 13, pips: 1 }]),
  'collapsed to one step per checker, the 4-pip hop kept whole');
assert(collapsed.length <= _stepsPerRoll(3, 1), 'which fits a 3-1 ply record');
assert(collapsed.every(s => s.pips >= 1 && s.pips <= 6),
  'every collapsed step stays inside the 1-6 pips a step can encode');

// --- and both replay to the same board --------------------------------------

const before = boardP1({ 12: 1, 13: 1, 20: -2 });
assert(eq(_applyMovesP1(before, split, true), _applyMovesP1(before, collapsed, true)),
  'collapsing the hops replays to exactly the same board');

// --- a legal play is untouched ----------------------------------------------

assert(eq(_notationToSteps('13/10 13/12', true, 3, 1),
          _notationToStepsUnsplit('13/10 13/12', true, 3, 1)),
  "a legal play's steps are the same either way");
assert(eq(_notationToStepsUnsplit('13/9', true, 4, 4), [{ from: 12, pips: 4 }]),
  'a span the roll does explain is still one step when it fits');

// --- the writer refuses what it cannot carry --------------------------------

const doc = (ply) => ({
  match_length: 7,
  player_white: 'W',
  player_black: 'B',
  games: [{ game_index: 0, plies: [ply] }],
});

let threw = null;
try {
  write_gvab(doc({ color: 1, action_id: 2, d1: 3, d2: 1, moves: split }));
} catch (err) {
  threw = err;
}
assert(threw !== null && /room for 2/.test(threw.message),
  'writing an over-long ply throws instead of dropping a step');

const fits = doc({ color: 1, action_id: 2, d1: 3, d2: 1, moves: collapsed });
const back = readGvab(write_gvab(fits));
assert(eq(back.games[0].plies[0].moves, collapsed),
  'the collapsed steps survive the .gvab round trip, 4 pips and all');

// --- a set-position ply carries the board it states -------------------------

const stated = boardP1({ 6: 5, 8: 3, 13: 5, 24: 2, 1: -2, 12: -5, 17: -3, 19: -5 });
const spBack = readGvab(write_gvab(doc({
  color: 0, action_id: 31, d1: 3, d2: 1, set_position: _p1ToAbsolute(stated),
}))).games[0].plies[0];
assert(spBack.d1 === 3 && spBack.d2 === 1,
  'a set-position ply keeps the dice of the play it stands in for');
assert(eq(_absoluteToP1(spBack.set_position), stated),
  'and round-trips the board it states');

// --- the corruption this all prevents is recognisable -----------------------

assert(eq(boardProblems(stated), []), 'a real position has nothing wrong with it');
assert(boardProblems(boardP1({ 6: 8, 8: 8, 20: -2 })).length > 0,
  '16 checkers on a side is reported');
assert(eq(boardProblems(boardP1({
  6: 5, 8: 3, 13: 5, 24: 1, 25: 1, 1: -2, 12: -5, 17: -3, 19: -5,
})), []), 'checkers on both bars are counted to the right side, not the mover');

console.log();
console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
