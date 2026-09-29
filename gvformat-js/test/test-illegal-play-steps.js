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
//
// The match the whole thing came from is in the corpus as `hQ8sVn2LbTdF4wRm`,
// in both of the forms it was reported in -- see the last section, and
// samples/README.md.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  _flipBoard, _notationToSteps, _notationToStepsUnsplit, _p1ToAbsolute,
  _stepsPerRoll, boardDiffHasNonForwardHop, fitMoveSteps,
  notationHasNonForwardHop,
} from '../src/export.js';
import { convertMat } from '../src/mat2gva.js';
import { convertXg } from '../src/xg2gva.js';
import { canonicalNotation } from '../src/notation.js';
import { parseOgid } from '../src/ogid.js';
import { boardProblems } from '../src/legality.js';
import { write_gvab } from '../src/binary.js';
import { readGvab, _absoluteToP1, _applyMovesP1 } from '../src/reader.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SAMPLES_DIR = path.join(__dirname, '..', '..', 'samples');

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


// --- a hop that runs backwards ----------------------------------------------
//
// The other illegal shape, and the one no step can hold: one real match plays a
// 6-5 as `14/8 15/10 6/8` -- both dice forward, then a checker 2 pips the wrong
// way, which the site let through. XG and HedgeHog both record it. `pips` is an
// unsigned 3-bit *forward* distance, so the hop has to reach the set-position
// rung, and it cannot get there on the step count: the splitters answer a
// non-forward span with no steps at all.

const BACKWARDS = '14/8 15/10 6/8';

assert(_notationToSteps(BACKWARDS, true, 6, 5).length === 2,
  'the dice split drops the backwards hop, leaving two steps for a three-hop play');
assert(_notationToStepsUnsplit(BACKWARDS, true, 6, 5).length === 2,
  'and so does one-step-per-span -- neither count can report the loss');

assert(notationHasNonForwardHop(BACKWARDS),
  'the backwards hop is recognised from the notation instead');
assert(!notationHasNonForwardHop(ILLEGAL),
  'an over-long but all-forward illegal play is not confused with one');
assert(!notationHasNonForwardHop('13/7 8/7'), 'nor is a legal play');
assert(!notationHasNonForwardHop('bar/22* 13/8'), 'nor an entry from the bar');
assert(!notationHasNonForwardHop('2/off 1/off'), 'nor a bear-off');

assert(fitMoveSteps(_notationToSteps(BACKWARDS, true, 6, 5), BACKWARDS, true, 6, 5) === null,
  'so no checker ply is offered for it -- straight to the set-position rung');
assert(fitMoveSteps(_notationToSteps(ILLEGAL, true, 3, 1), ILLEGAL, true, 3, 1) !== null,
  'while the all-forward illegal play still collapses into a ply that fits');

// The same judgement from a board diff alone, for a converter with no notation
// in hand. This is the real ply: white on 3(2) 5(2) 6(3) 7(4) 8(2) 14 15.
const backBefore = boardP1({
  3: 2, 5: 2, 6: 3, 7: 4, 8: 2, 14: 1, 15: 1,
  2: -2, 16: -1, 20: -1, 21: -3, 22: -4, 23: -2, 24: -2,
});
const backAfter = boardP1({
  3: 2, 5: 2, 6: 2, 7: 4, 8: 4, 10: 1,
  2: -2, 16: -1, 20: -1, 21: -3, 22: -4, 23: -2, 24: -2,
});
assert(boardDiffHasNonForwardHop(backBefore, backAfter, true),
  'the board diff says the same: no all-forward play reaches that position');
const legalAfter = boardP1({
  3: 2, 5: 2, 6: 3, 7: 4, 8: 3, 10: 1,
  2: -2, 16: -1, 20: -1, 21: -3, 22: -4, 23: -2, 24: -2,
});
assert(!boardDiffHasNonForwardHop(backBefore, legalAfter, true),
  'and clears the legal 14/8 15/10 from the same board');

// --- end to end, through the .mat converter ---------------------------------
//
// A `.mat` states the play in notation and replays it verbatim, so its board is
// right and only the ply record was losing the hop. Every board for the rest of
// the game used to sit one checker out of place, with nothing downstream able
// to tell.

const synthetic = (white2) => `; [Site "test"]

 1 point match

 Game 1
 A : 0                                 B : 0
  1) 31: 8/5 6/5                          42: 24/22 13/9
  2) ${white2}                    31: 9/6 22/21
  3) 11: 8/7 8/7 7/6 7/6
   Wins 1 point
`;

const illegalDoc = convertMat(synthetic('65: 13/7 13/8 6/8'));
const illegalPly = illegalDoc.games[0].plies[2];
assert(illegalPly.action_id === 31,
  'the .mat converter states the backwards play as a set position');
assert(illegalPly.d1 === 6 && illegalPly.d2 === 5,
  'keeping the roll it stands in for');
const statedBoard = _absoluteToP1(illegalPly.set_position);
assert(statedBoard[8] === 4 && statedBoard[6] === 3,
  'and the board it states has the backwards checker on 8, not left on 6');

const legalDoc = convertMat(synthetic('65: 13/7 13/8'));
const legalPly = legalDoc.games[0].plies[2];
assert(legalPly.action_id === 19 && legalPly.moves.length === 2 && !legalPly.set_position,
  'the same play without the backwards hop stays an ordinary 6-5 checker ply');

// The dice are what tell a restated *play* from the set-position ply that opens
// an exported saved position, so the reader gives one OGIDs and the other none.
const illegalBack = readGvab(write_gvab(illegalDoc)).games[0];
assert(illegalBack.plies[2].action_id === 31 && illegalBack.plies[2].ogid_before,
  'a set-position ply with dice reads back with the OGIDs of the turn it is');
const checkers = (id) => id.split(':').slice(0, 2).join(':');
assert(checkers(illegalBack.plies[3].ogid_before) === checkers(illegalBack.plies[2].ogid_after),
  'and the ply after it carries on from exactly the board it stated');
assert(!readGvab(write_gvab(doc({
  color: 1, action_id: 31, set_position: _p1ToAbsolute(stated),
}))).games[0].plies[0].ogid_before,
  'a set-position ply with no dice states where a game starts and gets none');

// --- the real match, in both of the forms it was reported in ---------------
//
// `hQ8sVn2LbTdF4wRm` is where this came from: a HedgeHog transcription where
// white played a 6-5 as `14/8 15/10 6/8`, the last hop running two pips
// *backwards*. It is in the corpus twice over, and the two files reach the
// set-position ply along different routes -- the .mat states the play in
// notation, so `fitMoveSteps` refuses it on the notation alone, while the .xg's
// step list is simply short and it is the played candidate's own stored position
// that gives the board away (`invalidM === 2`, see `_xgCandidateBoard`). Landing
// on the same board is the whole point: that board is what a user can see, since
// XG draws this play wrong and then draws the next one right.
const MATCH_MAT = path.join(SAMPLES_DIR, 'mat', 'hQ8sVn2LbTdF4wRm.mat');
const MATCH_XG = path.join(SAMPLES_DIR, 'xg', 'hQ8sVn2LbTdF4wRm.xg');

if (!fs.existsSync(MATCH_MAT) || !fs.existsSync(MATCH_XG)) {
  console.log('SKIP  the real match (hQ8sVn2LbTdF4wRm is not in samples/)');
} else {
  /** The ply's board from the mover's own side of it. */
  const moverBoard = (ogid, color) => {
    const st = parseOgid(ogid);
    const p1 = st.onRoll === 'W' ? Array.from(st.board) : _flipBoard(Array.from(st.board));
    return color ? p1 : _flipBoard(p1);
  };

  const pair = [
    ['the .mat', convertMat(fs.readFileSync(MATCH_MAT, 'utf8'))],
    ['the .xg', await convertXg(new Uint8Array(fs.readFileSync(MATCH_XG)))],
  ];
  const restated = {};
  for (const [name, doc] of pair) {
    const found = [];
    doc.games.forEach((game, gi) => game.plies.forEach((ply, pi) => {
      if (ply.action_id === 31) found.push([gi, pi, ply]);
    }));
    assert(found.length === 1,
      `${name} of the real match holds exactly one set-position ply`);
    const [gi, pi, ply] = found[0];
    restated[name] = ply;
    assert(gi === 1 && pi === 37,
      `${name} puts it at game 2's twentieth play, where the 6-5 was`);
    assert(ply.d1 === 6 && ply.d2 === 5,
      `${name} keeps the roll it stands in for`);
    assert(canonicalNotation(moverBoard(ply.ogid_before, ply.color),
      moverBoard(ply.ogid_after, ply.color), ply.d1, ply.d2) === '15/10 14/8 6/8',
      `${name} states a board the backwards hop reads back out of`);
    assert(!ply.analysis,
      `${name} carries no analysis on it -- the known cost of the encoding`);
    const next = doc.games[gi].plies[pi + 1];
    assert(checkers(next.ogid_before) === checkers(ply.ogid_after),
      `${name} has the next play carry on from the board it stated`);
  }

  assert(restated['the .mat'].ogid_before === restated['the .xg'].ogid_before
    && restated['the .mat'].ogid_after === restated['the .xg'].ogid_after,
    'and the two files, read by two different routes, agree on both boards');

  // Through the binary, where the hop was being dropped. The synthetic case
  // above covers the encoding; this covers it on a ply deep inside a game, with
  // a live cube and a full analysis block around it.
  const xgPly = restated['the .xg'];
  const roundTripped = readGvab(write_gvab(pair[1][1])).games[1].plies[37];
  assert(roundTripped.action_id === 31
    && roundTripped.ogid_before === xgPly.ogid_before
    && roundTripped.ogid_after === xgPly.ogid_after,
    'and a saved match brings that ply back with its boards intact');
}

console.log();
console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
