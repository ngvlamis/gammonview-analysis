// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Equity loss is stored as uint16 at 1e-4, so the representable ceiling is
// 6.5535. The writer clamped it to 1.0 until 2026-08, which truncated any
// blunder past a point of equity -- and because `played_equity` is derived
// (best_equity - equity_loss) rather than stored, the truncation moved the
// played equity too, leaving a record that disagreed with its own alternatives.
//
// These tests pin both the encoder's bounds and the round-trip behaviour that
// regression actually broke.
//
// The encoder was not the only place capping at 1.0. export.js's to_ogxm_json
// -- the analysis worker's own path from a bgsage result to OGXM-JSON -- capped
// there too, upstream of the encoder, so fixing the encoder alone left every
// analysed match still truncated. The last section covers that layer.

import { write_gvab, _enc_equity_loss, MAX_EQUITY_LOSS } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { _cubeSubAnalysis, _checkerAnalysis } from '../src/export.js';

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

function assertEq(actual, expected, msg) {
  assert(Object.is(actual, expected),
    `${msg}${Object.is(actual, expected) ? '' : `  (expected ${expected}, got ${actual})`}`);
}

// ---------------------------------------------------------------------------
// Encoder bounds
// ---------------------------------------------------------------------------

assertEq(MAX_EQUITY_LOSS, 6.5535, 'the ceiling is the full uint16 range at 1e-4');
assertEq(_enc_equity_loss(0), 0, 'zero encodes to zero');
assertEq(_enc_equity_loss(-0.5), 0, 'a negative loss floors at zero');
assertEq(_enc_equity_loss(0.0047), 47, 'a small loss keeps 1e-4 resolution');
assertEq(_enc_equity_loss(1.0), 10000, 'one point of equity is no longer the ceiling');
assertEq(_enc_equity_loss(1.8096), 18096, 'a loss past 1.0 survives instead of truncating');
assertEq(_enc_equity_loss(6.5535), 65535, 'the ceiling encodes to the uint16 maximum');
assertEq(_enc_equity_loss(9.9), 65535, 'past the ceiling it saturates rather than wrapping');

// Wrapping would be far worse than clamping: 6.6 * 10000 is 66000, which would
// come back as 464 -- a catastrophic blunder rendered as a rounding error.
assert(_enc_equity_loss(6.6) > 60000, 'an over-range loss stays large rather than wrapping to a small one');

// ---------------------------------------------------------------------------
// Round-trip: the regression this actually fixes
// ---------------------------------------------------------------------------

const EV = {
  win: 0.30, gammon_win: 0.05, bg_win: 0.002,
  gammon_loss: 0.30, bg_loss: 0.02, equity: -0.968,
};

/** A one-ply document whose single checker decision lost `loss` equity. */
function docWithCheckerLoss(bestEquity, playedEquity) {
  const loss = Math.round((bestEquity - playedEquity) * 10000) / 10000;
  return {
    match_length: 5, player_white: 'a', player_black: 'b',
    white_score: 0, black_score: 0, crawford: true,
    analysis_info: { ply: 2, eval_level: '2ply', model_id: 'x', timestamp: 0 },
    games: [{
      game_index: 0, winner: 0, points_won: 1,
      plies: [
        {
          color: 0, action_id: 9, d1: 2, d2: 5,
          moves: [{ from: 13, pips: 5 }, { from: 13, pips: 2 }],
          ogid_before: '11ccccchhhjjjjj:66666888dddddoo:N0N:25:W:IB:0:0:5:0',
          analysis: {
            eval: EV,
            best_equity: bestEquity,
            played_equity: playedEquity,
            equity_loss: loss,
            decision: true,
            alternatives: [
              { move: [{ from: 13, pips: 5 }, { from: 13, pips: 2 }], equity: bestEquity, is_played: false, eval: EV, eval_level: '2ply' },
              { move: [{ from: 24, pips: 5 }, { from: 24, pips: 2 }], equity: playedEquity, is_played: true, eval: EV, eval_level: '2ply' },
            ],
          },
        },
        { color: 1, action_id: 24, moves: [] },
      ],
    }],
  };
}

{
  const a = readGvab(write_gvab(docWithCheckerLoss(0.9, -0.6))).games[0].plies[0].analysis;
  assertEq(a.equity_loss, 1.5, 'a 1.5 checker blunder round-trips intact');
  assertEq(a.played_equity, -0.6, 'played_equity, being derived, is right again');
  const played = a.alternatives.find((x) => x.is_played);
  assert(Math.abs(played.equity - a.played_equity) < 1e-9,
    'the record agrees with its own played alternative');
}

{
  // Still correct below the old bound -- the change must not disturb the
  // ordinary case, which is the overwhelming majority of real decisions.
  const a = readGvab(write_gvab(docWithCheckerLoss(0.0145, 0.0098))).games[0].plies[0].analysis;
  assertEq(a.equity_loss, 0.0047, 'an ordinary small loss is unchanged');
  assertEq(a.played_equity, 0.0098, 'and its played_equity still reconstructs');
}

// A wrong take of a large double -- the real case that exposed this, where the
// loss lives on a cube record rather than a checker one.
{
  const doc = {
    match_length: 5, player_white: 'a', player_black: 'b',
    white_score: 0, black_score: 0, crawford: true,
    analysis_info: { ply: 2, eval_level: '2ply', model_id: 'x', timestamp: 0 },
    games: [{
      game_index: 0, winner: 0, points_won: 1,
      plies: [
        {
          color: 0, action_id: 22, moves: [],
          ogid_before: '11ccccchhhjjjjj:66666888dddddoo:N0N::W:A:0:0:5:0',
          analysis: {
            correct_action: 'pass', played_action: 'take',
            no_double_equity: 0.9967, double_take_equity: 2.8096, double_pass_equity: 1.0,
            equity_loss: 1.8096, decision: true, eval: EV, eval_level: '2ply',
          },
        },
        { color: 1, action_id: 24, moves: [] },
      ],
    }],
  };
  const a = readGvab(write_gvab(doc)).games[0].plies[0].analysis;
  assertEq(a.equity_loss, 1.8096, 'a 1.8096 wrong take round-trips intact');
  assertEq(a.double_take_equity, 2.8096, 'the cube equities were never the clamped part');
}

// ---------------------------------------------------------------------------
// The export layer: to_ogxm_json, upstream of the encoder
// ---------------------------------------------------------------------------

const PROBS = [0.30, 0.05, 0.002, 0.30, 0.02];

// A missed double worth more than a point of equity.
{
  const [key, sub] = _cubeSubAnalysis({
    lost_equity: 1.8096, optimal_action: 'double',
    equity_no_double: 0.9967, equity_double_take: 2.8096, equity_double_pass: 1.0,
  });
  assertEq(key, 'missed_double', 'a cube that should have turned reads as missed_double');
  assertEq(sub.equity_loss, 1.8096, 'a missed double past 1.0 survives to_ogxm_json');
}

// A no-double the player got wrong, on the cube_decision branch.
{
  const [, sub] = _cubeSubAnalysis({
    lost_equity: 2.5, optimal_action: 'no_double', counted: true,
    equity_no_double: 0.5, equity_double_take: -0.2, equity_double_pass: 1.0,
  });
  assertEq(sub.equity_loss, 2.5, 'a cube_decision loss past 1.0 survives too');
}

// Checker plies, both the analysed branch (lost_equity supplied by the engine)
// and the derived one (best - played).
{
  const entry = {
    lost_equity: 1.5, counted: true,
    move_options: [
      { move: '13/8 13/11', equity: 0.9, probs: PROBS },
      { move: '24/19 24/22', equity: -0.6, probs: PROBS, played: true },
    ],
  };
  const a = _checkerAnalysis(entry, true, 2, 5, 2);
  assertEq(a.equity_loss, 1.5, "the engine's own lost_equity is not capped at 1.0");
  assertEq(a.played_equity, -0.6, 'played_equity is untouched by the loss');
}

{
  const entry = {
    move_options: [
      { move: '13/8 13/11', equity: 0.9, probs: PROBS },
      { move: '24/19 24/22', equity: -0.6, probs: PROBS, played: true },
    ],
  };
  const a = _checkerAnalysis(entry, true, 2, 5, 2);
  assertEq(a.equity_loss, 1.5, 'a derived loss past 1.0 is not capped either');
  assertEq(a.decision, false, 'an unanalysed ply still counts toward nothing');
}

// The floor is the part that must stay: a negative loss is nonsense, and
// _enc_equity_loss would encode it as a huge unsigned value.
{
  const entry = {
    lost_equity: -0.25, counted: true,
    move_options: [{ move: '13/8 13/11', equity: 0.5, probs: PROBS, played: true }],
  };
  assertEq(_checkerAnalysis(entry, true, 2, 5, 2).equity_loss, 0,
    'a negative loss still floors at zero');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
