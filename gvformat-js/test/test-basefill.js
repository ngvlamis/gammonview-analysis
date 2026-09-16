// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Reading an analysis block written without the [GV] extensions.
//
// Every producer but us writes OGXM without a GVAN chunk: no decision flags, no
// luck, and -- in match play -- the three cube values as raw MWC rather than as
// the normalized equity we store. `basefill.js` completes such a block on read.
// Mirrors tests/test_basefill.py -- keep the two in step.

import { write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { completeBaseBlock } from '../src/basefill.js';
import { compute_aggregates } from '../src/stats.js';
import { eq2mwc } from '../src/met.js';
import { CHUNK_GVAN } from '../src/constants.js';

let passed = 0, failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}
const close = (a, b, eps = 1e-3) => Math.abs(a - b) < eps;

// ---------------------------------------------------------------------------
// A file with its GVAN chunk removed: what any other producer's file looks like.
// The header's file_size is patched; the CSUM is not, so reads pass verifyCrc:
// false -- the checksum is not what these tests are about.
// ---------------------------------------------------------------------------

function stripGvan(bytes) {
  const buf = Uint8Array.from(bytes);
  const dv = new DataView(buf.buffer);
  const kept = [buf.subarray(0, 20)];
  let pos = 20;
  while (buf.length - pos > 8) {
    const type = dv.getUint32(pos, true);
    const len = dv.getUint32(pos + 4, true);
    if (type !== CHUNK_GVAN) kept.push(buf.subarray(pos, pos + 12 + len));
    pos += 12 + len;
  }
  kept.push(buf.subarray(buf.length - 8));
  let total = 0;
  for (const part of kept) total += part.length;
  const out = new Uint8Array(total);
  let at = 0;
  for (const part of kept) { out.set(part, at); at += part.length; }
  new DataView(out.buffer).setUint32(12, out.length, true);
  return out;
}

const readForeign = (doc) => readGvab(stripGvan(write_gvab(doc)), { verifyCrc: false });

const evalOf = (win) => ({
  win, gammon_win: 0.12, bg_win: 0.01, gammon_loss: 0.11, bg_loss: 0.01,
  equity: Math.round((win + 0.12 + 0.01 - 0.11 - 0.01) * 10000) / 10000,
});

/** A checker analysis whose candidates are `equities`, best first.
 *
 *  `decision` is set by hand throughout this file, to what a person reading the
 *  candidates would say. That hand labelling is the specification these tests
 *  hold the derivation to, and it is what makes the last one meaningful: strip
 *  the flags, derive them back, and the rating has to be the same number. */
function checker(equities, decision, extra = {}) {
  return {
    eval: evalOf(0.50), best_equity: equities[0],
    played_equity: equities[0], equity_loss: 0.0, decision,
    alternatives: equities.map((equity, i) => ({
      move: [{ from: 13, pips: 3 }], equity, is_played: i === 0, eval: evalOf(0.5 - i * 0.01),
    })),
    ...extra,
  };
}

const MATCH = () => ({
  match_length: 7, player_white: 'W', player_black: 'B',
  white_score: 0, black_score: 0,
  analysis_info: { ply: 2, model_id: 'test', timestamp: 0 },
  games: [{
    game_index: 0, winner: 1, points_won: 1, plies: [
      // 0: a real choice -- the candidates disagree
      { color: 1, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 3 }, { from: 13, pips: 2 }],
        analysis: checker([0.20, 0.05, -0.10], true) },
      // 1: forced -- one candidate is no decision at all
      { color: 0, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 3 }, { from: 13, pips: 2 }],
        analysis: checker([0.10], false) },
      // 2: every candidate scores the same -- decided before it was reached
      { color: 1, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 3 }, { from: 13, pips: 2 }],
        analysis: checker([0.10, 0.10], false) },
      // 3: a live cube above a checker play, and a close one
      { color: 0, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 3 }, { from: 13, pips: 2 }],
        analysis: checker([0.30, 0.10], true, { cube_decision: {
          should_double: false, no_double_equity: 0.40, double_take_equity: 0.35,
          double_pass_equity: 1.0, action: 'no_double', decision: true } }) },
      // 4: a cube nowhere near being turned. Its negative values are also what
      // tells a reader this block is in equity and not in MWC -- a real match
      // is full of them, and a probability never is.
      { color: 1, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 3 }, { from: 13, pips: 2 }],
        analysis: checker([0.05, -0.05], true, { cube_decision: {
          should_double: false, no_double_equity: -0.30, double_take_equity: -0.75,
          double_pass_equity: 1.0, action: 'no_double', decision: false } }) },
      // 5: a double worth making
      { color: 1, action_id: 21, analysis: {
        correct_action: 'double', played_action: 'double',
        no_double_equity: 0.55, double_take_equity: 0.62, double_pass_equity: 1.0,
        equity_loss: 0.0, decision: true } },
      // 6: the take that answers it
      { color: 0, action_id: 22, analysis: {
        correct_action: 'take', played_action: 'take',
        no_double_equity: 0.55, double_take_equity: 0.62, double_pass_equity: 1.0,
        equity_loss: 0.0, decision: true } },
      { color: 1, action_id: 24 },
    ],
  }],
});

console.log('--- 1. a block with GVAN is left exactly as it was written ---');
{
  const native = readGvab(write_gvab(MATCH()));
  assert(native._base_analyses === undefined,
    '1. our own file reports no base-only block');
  assert(native.games[0].plies[0].analysis.decision === true
      && native.games[0].plies[1].analysis.decision === false,
    '1. and its stored flags are read as stored, not re-derived');
}

console.log('\n--- 2. decision flags are derived when GVAN is absent ---');
{
  const foreign = readForeign(MATCH());
  const flags = foreign.games[0].plies.map((p) => p.analysis && p.analysis.decision);
  assert(JSON.stringify(foreign._base_analyses) === '[0]',
    '2. the block is reported as base-only');
  assert(flags[0] === true, '2. candidates that disagree are a decision');
  assert(flags[1] === false, '2. a forced move is not');
  assert(flags[2] === false, '2. nor is a position where every move scores the same');
  assert(flags[5] === true, '2. a double worth making counts');
  assert(flags[6] === true, '2. so does the take that answers it');
  assert(foreign.games[0].plies[4].analysis.cube_decision.decision === false,
    '2. and a cube nowhere near being turned does not');
  assert(foreign.games[0].plies[3].analysis.cube_decision.decision === true,
    '2. a live cube above a checker play is judged on its own triviality');
}

console.log('\n--- 3. a decision too clear to count is not one ---');
{
  const doc = MATCH();
  // The doubler is 0.5 ahead of both answers: nobody had to think.
  Object.assign(doc.games[0].plies[5].analysis, {
    no_double_equity: 0.9, double_take_equity: 0.4, double_pass_equity: 1.0 });
  // A take and a pass worth the same: no choice was posed.
  Object.assign(doc.games[0].plies[6].analysis, {
    no_double_equity: 0.55, double_take_equity: 1.0, double_pass_equity: 1.0 });
  const flags = readForeign(doc).games[0].plies.map((p) => p.analysis && p.analysis.decision);
  assert(flags[5] === false, '3. a cube nobody had to think about does not count');
  assert(flags[6] === false, '3. nor a take/pass worth the same either way');
}

console.log('\n--- 4. an illegal play is excluded, however wide the spread ---');
{
  // Through the function rather than through a file: `illegal_move` is itself a
  // [GV] flag, so a block with no GVAN cannot be carrying one. The guard is
  // here for a caller that completes a block for some other reason, and it is
  // still the rule -- the player broke the rules, they did not choose badly.
  const analysis = checker([0.20, 0.05, -0.10], true, { illegal_move: true });
  const ply = { color: 1, action_id: 6, ogid_before: null };
  completeBaseBlock(new Map([['0,0', analysis]]), new Map([['0,0', ply]]), null);
  assert(analysis.decision === false,
    '4. an illegal play poses no decision, whatever its candidates say');
}

console.log('\n--- 5. match-play cube values arrive as MWC and are converted back ---');
{
  // Forward-map the file's normalized values into the MWC the base spec stores,
  // which is what another producer would have written. Reading must give the
  // originals back.
  const doc = MATCH();
  const toMwc = (eq) => Math.round(eq2mwc(eq, 7, 7, 1, false) * 10000) / 10000;
  const original = [];
  for (const ply of doc.games[0].plies) {
    for (const sub of [ply.analysis, ply.analysis && ply.analysis.cube_decision]) {
      if (!sub || sub.no_double_equity == null) continue;
      original.push([sub.no_double_equity, sub.double_take_equity, sub.double_pass_equity]);
      sub.no_double_equity = toMwc(sub.no_double_equity);
      sub.double_take_equity = toMwc(sub.double_take_equity);
      sub.double_pass_equity = toMwc(sub.double_pass_equity);
    }
  }
  const foreign = readForeign(doc);
  const got = [];
  for (const ply of foreign.games[0].plies) {
    for (const sub of [ply.analysis, ply.analysis && ply.analysis.cube_decision]) {
      if (!sub || sub.no_double_equity == null) continue;
      got.push([sub.no_double_equity, sub.double_take_equity, sub.double_pass_equity]);
    }
  }
  assert(got.length === original.length && got.length === 4,
    '5. every cube payload was seen (two live cubes, the double, the take)');
  assert(got.every((row, i) => row.every((v, j) => close(v, original[i][j], 2e-3))),
    '5. and each came back to the equity it was written from');
  assert(got.every((row) => close(row[2], 1.0, 2e-3)),
    '5. a pass is +1 on the normalized scale, which is what makes this checkable');
}

console.log('\n--- 6. money play is left alone: those values are already equity ---');
{
  const doc = MATCH();
  doc.match_length = 0;
  doc.white_score = 0; doc.black_score = 0;
  const foreign = readForeign(doc);
  const cube = foreign.games[0].plies[5].analysis;
  assert(close(cube.no_double_equity, 0.55) && close(cube.double_pass_equity, 1.0),
    '6. a money cube keeps the values it was written with');
}

console.log('\n--- 7. a value outside [0,1] proves the block is already equity ---');
{
  // Our own numbers with the GVAN dropped -- a stripped file, not a foreign one.
  // A doubler who is behind is negative, and no MWC ever is, so the conversion
  // must not fire.
  const doc = MATCH();
  doc.games[0].plies[5].analysis.no_double_equity = -0.35;
  const cube = readForeign(doc).games[0].plies[5].analysis;
  assert(close(cube.no_double_equity, -0.35) && close(cube.double_take_equity, 0.62),
    '7. the values are recognised as equity and left where they are');
}

console.log('\n--- 8. EVAL probabilities left unset fall back to the best move ---');
{
  const doc = MATCH();
  doc.games[0].plies[0].analysis.eval = {
    win: 0, gammon_win: 0, bg_win: 0, gammon_loss: 0, bg_loss: 0, equity: 0 };
  const native = readGvab(write_gvab(doc)).games[0].plies[0].analysis;
  assert(close(native.eval.win, native.alternatives[0].eval.win),
    '8. all-zero probabilities mean "not recorded", and the best move has them');
  assert(!close(native.eval.win, 0),
    '8. so the position is not reported as 0% to win');
}

console.log('\n--- 9. the header adopts a depth its decisions agree on ---');
{
  const doc = MATCH();
  doc.analysis_info = { ply: 0, model_id: '', timestamp: 0 };
  for (const ply of doc.games[0].plies) if (ply.analysis) ply.analysis.ply = 3;
  const info = readForeign(doc).analysis_info;
  assert(info.ply === 3, '9. a block whose every decision says 3-ply is a 3-ply block');
  assert(info.eval_level === '3ply', '9. and can say so where a level is shown');
}

console.log('\n--- 10. a completed block rates like the one it was written from ---');
{
  // The whole point, end to end: strip our own flags, derive them back, and the
  // performance rating has to land on the number bgsage itself computed.
  const doc = MATCH();
  for (const ply of doc.games[0].plies) {
    if (ply.analysis && ply.analysis.alternatives) ply.analysis.equity_loss = 0.15;
  }
  const native = compute_aggregates(readGvab(write_gvab(doc))).match;
  const foreign = compute_aggregates(readForeign(doc)).match;
  assert(native.white.total_decisions === foreign.white.total_decisions
      && native.black.total_decisions === foreign.black.total_decisions,
    '10. the same plies count on both sides');
  assert(native.white.pr === foreign.white.pr && native.black.pr === foreign.black.pr,
    '10. and the ratings are identical');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
