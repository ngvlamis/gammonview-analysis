// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Stats computation tests

import { compute_aggregates } from '../src/stats.js';
import { mwcAnchors } from '../src/met.js';

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

function assertClose(actual, expected, tolerance, msg) {
  assert(Math.abs(actual - expected) < tolerance, `${msg} (${actual} ~= ${expected})`);
}

// Test 1: Basic PR computation
{
  const ogxm = {
    games: [{
      game_index: 0,
      plies: [
        {
          color: 1, // white
          action_id: 6, // dice 23
          analysis: {
            decision: true,
            equity_loss: 0.05,
            eval: { win: 0.5, gammon_win: 0.1, bg_win: 0.02, gammon_loss: 0.08, bg_loss: 0.01, equity: 0.53 },
            best_equity: 0.53,
            played_equity: 0.48,
            alternatives: [],
          },
        },
        {
          color: 1,
          action_id: 10,
          analysis: {
            decision: true,
            equity_loss: 0.15,
            eval: { win: 0.5, gammon_win: 0.1, bg_win: 0.02, gammon_loss: 0.08, bg_loss: 0.01, equity: 0.53 },
            best_equity: 0.53,
            played_equity: 0.38,
            alternatives: [],
          },
        },
        {
          color: 0, // black
          action_id: 1,
          analysis: {
            decision: false,
            equity_loss: 0.0,
            eval: { win: 0.5, gammon_win: 0.1, bg_win: 0.02, gammon_loss: 0.08, bg_loss: 0.01, equity: 0.53 },
            best_equity: 0.53,
            played_equity: 0.53,
            alternatives: [],
          },
        },
      ],
    }],
  };

  const agg = compute_aggregates(ogxm);
  assert(agg.match.white.total_decisions === 2, "white decisions count");
  assertClose(agg.match.white.total_error, 0.20, 0.001, "white total error");
  assertClose(agg.match.white.pr, 50.0, 0.1, "white PR = 0.20/2 * 500");
  assert(agg.match.black.total_decisions === 0, "black decisions count (forced move)");
  assert(agg.match.black.pr === null, "black PR is null (no decisions)");
}

// Test 2: Luck accumulation (single `luck` field, no preroll/postroll/mwc anchors)
{
  const ogxm = {
    games: [{
      game_index: 0,
      plies: [
        {
          color: 1,
          action_id: 6,
          analysis: {
            decision: false,
            luck: 0.2,
            eval: { win: 0.5, gammon_win: 0, bg_win: 0, gammon_loss: 0, bg_loss: 0, equity: 0.5 },
            best_equity: 0.5,
            alternatives: [],
          },
        },
      ],
    }],
  };

  const agg = compute_aggregates(ogxm);
  assertClose(agg.match.white.total_luck, 0.2, 0.001, "white luck = stored luck field");
  assert(agg.match.white.luck_rolls === 1, "luck_rolls count");
  assert(!("total_luck_mwc" in agg.match.white), "no total_luck_mwc without ogid_before (money/unscored)");
}

// Test 2b: total_luck_mwc cross-check (compute-on-read via met.js, derived
// from the ply's own ogid_before -- mirrors gvformat/stats.py's _luck_to_mwc)
{
  // OGID fields: white_pos:black_pos:cube:dice:color:game_state:score_w:score_b:match_length
  // cube "N0N" -> owner=centered, exponent=0 -> cube_value = 1<<0 = 1.
  // score_w=2, score_b=3, match_length=7 (no Crawford suffix).
  const ogidBefore = "0:0:N0N:00:W:R:2:3:7";
  const luck = 0.1;

  const ogxm = {
    games: [{
      game_index: 0,
      plies: [
        {
          color: 1, // white on roll/mover
          action_id: 6,
          ogid_before: ogidBefore,
          analysis: {
            decision: false,
            luck,
            eval: { win: 0.5, gammon_win: 0, bg_win: 0, gammon_loss: 0, bg_loss: 0, equity: 0.5 },
            best_equity: 0.5,
            alternatives: [],
          },
        },
      ],
    }],
  };

  // white is mover, match_length=7, score_w=2, score_b=3 -> away_w=5, away_b=4.
  const [mwcWin, mwcLoss] = mwcAnchors(5, 4, 1, false);
  const expectedLuckMwc = luck * (mwcWin - mwcLoss) / 2.0;

  const agg = compute_aggregates(ogxm);
  assert("total_luck_mwc" in agg.match.white, "total_luck_mwc present when ogid_before parses");
  assertClose(agg.match.white.total_luck_mwc, expectedLuckMwc, 1e-6,
    `total_luck_mwc matches met.js-derived expectation`);
}

// Test 3: Cube decisions
{
  const ogxm = {
    games: [{
      game_index: 0,
      plies: [
        {
          color: 0,
          action_id: 21, // double
          analysis: {
            decision: true,
            equity_loss: 0.03,
            eval: { win: 0.6, gammon_win: 0.1, bg_win: 0.02, gammon_loss: 0.08, bg_loss: 0.01, equity: 0.63 },
          },
        },
      ],
    }],
  };

  const agg = compute_aggregates(ogxm);
  assert(agg.match.black.cube_decisions === 1, "cube decision counted");
  assert(agg.match.black.total_decisions === 1, "cube decision counts toward total");
}

// Test 4: Missed double (trivial case should NOT count)
{
  const ogxm = {
    games: [{
      game_index: 0,
      plies: [
        {
          color: 1,
          action_id: 6,
          analysis: {
            decision: false,
            missed_double: {
              no_double_equity: 0.5,
              double_take_equity: 0.5001,
              double_pass_equity: 0.6,
              equity_loss: 0.0,
              correct_action: "double",
            },
            eval: { win: 0.5, gammon_win: 0, bg_win: 0, gammon_loss: 0, bg_loss: 0, equity: 0.5 },
            best_equity: 0.5,
            alternatives: [],
          },
        },
      ],
    }],
  };

  const agg = compute_aggregates(ogxm);
  assert(agg.match.white.cube_decisions === 0, "trivial missed double not counted");
}

// Test 5: Illegal moves
{
  const ogxm = {
    games: [{
      game_index: 0,
      plies: [
        {
          color: 1,
          action_id: 6,
          analysis: {
            decision: false,
            illegal_move: true,
            eval: { win: 0.5, gammon_win: 0, bg_win: 0, gammon_loss: 0, bg_loss: 0, equity: 0.5 },
            best_equity: 0.5,
            alternatives: [],
          },
        },
      ],
    }],
  };

  const agg = compute_aggregates(ogxm);
  assert(agg.match.illegal_moves === 1, "illegal move counted");
}

console.log(`\n${passed + failed} tests, ${failed} failures`);
process.exit(failed > 0 ? 1 : 0);
