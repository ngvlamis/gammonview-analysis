// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// BGF alternatives builder tests.
//
// Regression: buildAlternatives read a non-existent `opt.eq` field, so every
// checker-play alternative was emitted without win/gammon/backgammon eval.
// The move options it receives carry a pre-computed `probs` vector instead.

import { buildAlternatives, checkerAnalysis } from '../src/bgf2gva.js';

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

// Options as checkerAnalysis builds them: probs = [win, gWin, bgWin, gLoss, bgLoss].
const moveOptions = [
  { equity: 0.5, played: true, probs: [0.60, 0.20, 0.05, 0.15, 0.03], ply: 3, move: "", notation: "13/10 13/9" },
  { equity: 0.4, played: false, probs: [0.55, 0.18, 0.04, 0.20, 0.05], ply: 3, move: "", notation: "24/21 13/9" },
];

const alts = buildAlternatives(moveOptions, true, 3, 4);

assert(alts.length === 2, "one alternative per move option");
assert(alts.every(a => a.eval != null), "every alternative carries an eval");

const e = alts[0].eval;
assert(e && Math.abs(e.win - 0.60) < 1e-9, "eval.win comes from probs[0]");
assert(e && Math.abs(e.gammon_win - 0.20) < 1e-9, "eval.gammon_win from probs[1]");
assert(e && Math.abs(e.bg_win - 0.05) < 1e-9, "eval.bg_win from probs[2]");
assert(e && Math.abs(e.gammon_loss - 0.15) < 1e-9, "eval.gammon_loss from probs[3]");
assert(e && Math.abs(e.bg_loss - 0.03) < 1e-9, "eval.bg_loss from probs[4]");

// A move option with no probs (e.g. an eval-less candidate) must not fabricate one.
const noProbs = buildAlternatives([{ equity: 0.1, played: true, probs: [], move: "", notation: "" }], true, 1, 1);
assert(noProbs[0].eval === undefined, "no eval when probs are absent");

// A dance: no candidate list, but BGBlitz evaluated the position the non-play
// leaves behind and put it in `dancingEquity`, separately from the pre-roll
// `equity` holding the cube decision. Both are on the record -- you can double
// when you cannot move -- and the checker analysis must take the dancing one,
// or the ply's probabilities are the cube's.
const money = { hasEMG: false, matchEquity: -999, emg: -999 };
const preRoll = { ...money, myWins: "0.60", myGammon: "0.18", myBackGammon: "0.003",
                  oppWins: "0.40", oppGammon: "0.04", oppBackGammon: "0.0007",
                  cubeDecision: { eqCubeFul: "0.49" } };
const danced = { ...money, myWins: "0.41", myGammon: "0.05", myBackGammon: "0.0006",
                 oppWins: "0.59", oppGammon: "0.05", oppBackGammon: "0.001",
                 cubeDecision: { eqCubeFul: "-0.26" } };
const board = new Array(26).fill(0); // a dance plays no checkers; nothing is read off it

const a = checkerAnalysis(preRoll, [], {}, -1, 4, 6, board, true, danced, 3);
assert(a != null, "a dance with dancingEquity yields an analysis");
assert(a.alternatives.length === 1, "exactly one 'no move' option");
assert(a.alternatives[0].move.length === 0 && a.alternatives[0].notation === "",
       "the option moves no checkers");
assert(a.alternatives[0].is_played === true, "and is the played one");
assert(Math.abs(a.eval.win - 0.41) < 1e-9,
       "eval.win comes from dancingEquity, not the pre-roll equity");
assert(Math.abs(a.best_equity - (-0.26)) < 1e-9 && a.equity_loss === 0,
       "no play to get wrong, so no equity is lost");
assert(a.decision === false, "and it is not a checker decision");
assert(a.alternatives[0].eval_level === "3ply", "eval_level from the record's ply");

assert(checkerAnalysis(preRoll, [], {}, -1, 4, 6, board, true) === null,
       "no candidates and no dancingEquity is still nothing to report");

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
