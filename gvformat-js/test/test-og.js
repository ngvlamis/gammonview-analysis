// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// convertOg: OpenGammon analysis payload -> OGXM analysis on a base match.
//
// The synthetic cases below are self-contained and always run. Every expected
// value in them was read off a real opengammon.com match and cross-checked
// against that match's .mat export, so they encode observed behaviour rather
// than a reading of the source.
//
// The parity section additionally replays a whole real match when samples are
// present. Unlike the rest of samples/, this data is NOT committed -- it is
// pulled live from an OpenGammon account and carries real handles, so
// /.gitignore excludes samples/og/ and the section SKIPS on a fresh clone.
// To populate it, drop these two files into samples/og/:
//
//   <match_id>.mat            the "match" string from /app/match/<id>/export/?format=mat
//   <match_id>.analysis.json  the "analysis.analysis" payload from /app/match/<id>/analysis/

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { convertOg, convert_og } from '../src/og2gva.js';
import { convertMat } from '../src/mat2gva.js';
import { compute_aggregates } from '../src/stats.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SAMPLES_DIR = path.join(__dirname, '..', '..', 'samples', 'og');

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
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  assert(a === e, `${msg}${a === e ? '' : `\n      expected ${e}\n      actual   ${a}`}`);
}

function assertThrows(fn, match, msg) {
  try {
    fn();
    assert(false, `${msg} (did not throw)`);
  } catch (e) {
    assert(String(e.message).includes(match), `${msg}${String(e.message).includes(match) ? '' : `\n      got "${e.message}"`}`);
  }
}

/** A base document with `n` checker plies of the given colours. */
function baseDoc(colors) {
  return {
    match_length: 5,
    games: [{
      game_index: 0,
      plies: colors.map((color) => ({ color, action_id: 9, d1: 2, d2: 5, moves: [] })),
    }],
  };
}

const OPENING_W = '11ccccchhhjjjjj:66666888dddddoo:N0N:25:W:IB:0:0:5:0';
const OPENING_B = '11cccehhhhjjjjj:66666888dddddoo:N0N:51:B:R:0:0:5:1';

// ---------------------------------------------------------------------------
// Move decoding
// ---------------------------------------------------------------------------

// W plays 25: 13/8 13/11. m=13, h=8, k=11 under the White alphabet.
{
  const base = baseDoc([0]);
  const payload = {
    config: '2ply',
    games: [{ moves: [[OPENING_W, '25mhmk', [['2C', 'mhmk', 0.0023, 0.4977, 0.1486, 0.0077, 0.1415, 0.0066]], []]] }],
  };
  convertOg(base, payload);
  const alt = base.games[0].plies[0].analysis.alternatives[0];
  assertEq(alt.move, [{ from: 13, pips: 5 }, { from: 13, pips: 2 }], 'W move decodes to own-frame 13/8 13/11');
  assert(alt.is_played === true, 'played candidate is flagged');
  assertEq(alt.eval_level, '2ply', '"2C" maps to "2ply"');
}

// B plays 51: 24/23 13/8. a=24, b=23, l=13, q=8; colour 1 mirrors into absolute.
{
  const base = baseDoc([1]);
  const payload = {
    config: '2ply',
    games: [{ moves: [[OPENING_B, '51ablq', [['2C', 'ablq', -0.15, 0.4659, 0.1259, 0.0054, 0.1619, 0.0062]], []]] }],
  };
  convertOg(base, payload);
  assertEq(base.games[0].plies[0].analysis.alternatives[0].move,
    [{ from: 1, pips: 1 }, { from: 12, pips: 5 }],
    'B move mirrors into the absolute frame for colour 1');
}

// Bar entry: 'y' is the bar (25), never off. W plays 43: bar/21 24/21.
{
  const base = baseDoc([0]);
  const payload = {
    config: '2ply',
    games: [{ moves: [['x:y:N0N:43:W:R:0:0:5:4', '43xuyu', [['2C', 'xuyu', 0.126, 0.5072, 0.1898, 0.0039, 0.0998, 0.003]], []]] }],
  };
  convertOg(base, payload);
  assertEq(base.games[0].plies[0].analysis.alternatives[0].move,
    [{ from: 24, pips: 3 }, { from: 25, pips: 4 }],
    "'y' decodes as the bar, not as off");
}

// ---------------------------------------------------------------------------
// Equity bookkeeping
// ---------------------------------------------------------------------------

{
  const base = baseDoc([0]);
  // Played move ranked below best, and appended out of order as OpenGammon does.
  const payload = {
    config: '2ply',
    games: [{
      moves: [[OPENING_W, '25mhxv', [
        ['2C', 'mhmk', 0.0100, 0.50, 0.13, 0.005, 0.13, 0.004],
        ['2C', 'fdmh', -0.0400, 0.48, 0.13, 0.006, 0.14, 0.007],
        ['2C', 'mhxv', 0.0030, 0.50, 0.13, 0.005, 0.13, 0.004],
      ], []]],
    }],
  };
  convertOg(base, payload);
  const a = base.games[0].plies[0].analysis;
  assertEq(a.best_equity, 0.01, 'best_equity is the highest equity, not the first entry');
  assertEq(a.played_equity, 0.003, 'played_equity comes from the played candidate');
  assertEq(a.equity_loss, 0.007, 'equity_loss is best minus played');
  assertEq(a.eval.equity, Math.round((0.50 + 0.13 + 0.005 - 0.13 - 0.004) * 10000) / 10000,
    'eval.equity uses win+gw+bgw-gl-bgl');
}

// A ply with no candidates (forced play or a dance) gets no analysis at all.
{
  const base = baseDoc([0]);
  convertOg(base, { config: '2ply', games: [{ moves: [[OPENING_W, '25mhmk', [], []]] }] });
  assert(base.games[0].plies[0].analysis === undefined, 'ply with no candidates carries no analysis');
}

// ---------------------------------------------------------------------------
// Cube handling
// ---------------------------------------------------------------------------

// [ply, W, WG, WBG, LG, LBG, EQ, ND, DT, DP]
const CUBE_DOUBLER = ['2C', 0.6922, 0.2, 0.01, 0.1, 0.005, 0.4878, 0.6844, 0.7909, 1.0];
// OpenGammon serves the responder's ply with ND/DT/DP negated.
const CUBE_RESPONDER = ['2C', 0.6922, 0.2, 0.01, 0.1, 0.005, 0.4878, -0.6844, -0.7909, -1.0];

{
  const base = baseDoc([0, 1]);
  base.games[0].plies[0].action_id = 21;
  base.games[0].plies[1].action_id = 22;
  const payload = {
    config: '2ply',
    games: [{ moves: [
      [OPENING_W, 'double', [], CUBE_DOUBLER],
      [OPENING_B, 'take', [], CUBE_RESPONDER],
    ] }],
  };
  convertOg(base, payload);
  const dbl = base.games[0].plies[0].analysis;
  const tk = base.games[0].plies[1].analysis;

  assertEq(dbl.correct_action, 'double', 'doubling is correct when min(DT,DP) beats ND');
  assertEq(dbl.played_action, 'double', 'played_action recorded for the doubler');
  assertEq(dbl.equity_loss, 0, 'a correct double costs nothing');

  assertEq([tk.no_double_equity, tk.double_take_equity, tk.double_pass_equity],
    [0.6844, 0.7909, 1.0],
    "responder's cube equities are negated back into the doubler's frame");
  assertEq(tk.correct_action, 'take', 'taking is correct when DT costs the doubler less than DP');
  assertEq(tk.equity_loss, 0, 'a correct take costs nothing');
}

// Dropping when the take was right costs the difference between the branches.
{
  const base = baseDoc([1]);
  base.games[0].plies[0].action_id = 23;
  convertOg(base, { config: '2ply', games: [{ moves: [[OPENING_B, 'drop', [], CUBE_RESPONDER]] }] });
  const a = base.games[0].plies[0].analysis;
  assertEq(a.played_action, 'pass', 'a drop is recorded as a pass');
  assertEq(a.correct_action, 'take', 'the take was the cheaper branch');
  assertEq(a.equity_loss, Math.round(Math.abs(0.7909 - 1.0) * 10000) / 10000, 'dropping a takeable cube costs DP-DT');
}

// A checker ply whose cube should have been turned carries missed_double, and
// *only* missed_double: the format allows one cube record per checker ply, and
// binary.js drops cube_decision when both are set, so emitting both would count
// the ply twice toward PR and then lose that inflation on a save/reload.
{
  const base = baseDoc([0]);
  const payload = {
    config: '2ply',
    games: [{ moves: [[OPENING_W, '25mhmk',
      [['2C', 'mhmk', 0.0023, 0.4977, 0.1486, 0.0077, 0.1415, 0.0066]], CUBE_DOUBLER]] }],
  };
  convertOg(base, payload);
  const a = base.games[0].plies[0].analysis;
  assert(a.missed_double != null, 'missed_double emitted when doubling beat rolling');
  assert(a.cube_decision === undefined, 'no cube_decision alongside a missed_double');
  // min(DT, DP) = min(0.7909, 1.0) = 0.7909; ND = 0.6844.
  assertEq(a.missed_double.equity_loss, 0.1065, 'missed_double loss is min(DT,DP) - ND');
  assertEq(a.missed_double.correct_action, 'double', 'missed_double names the right action');
  // The pre-roll eval belongs on either classification -- which way the
  // decision came out is no reason to drop the probabilities that describe it.
  assert(a.missed_double.eval != null, 'missed_double carries the cube eval');
  // The cube record's own pre-roll win (CUBE_DOUBLER), not the checker
  // alternative's post-roll 0.4977 -- they answer different questions.
  assertEq(a.missed_double.eval.win, 0.6922, 'and it is the pre-roll win probability');
  assertEq(a.missed_double.eval_level, '2ply', 'with the depth it was evaluated at');
}

// A cube that should be held produces cube_decision but no missed_double.
{
  const base = baseDoc([0]);
  const held = ['2C', 0.502, 0.146, 0.008, 0.154, 0.009, 0.0, -0.0046, -0.34, 1.0];
  convertOg(base, {
    config: '2ply',
    games: [{ moves: [[OPENING_W, '25mhmk',
      [['2C', 'mhmk', 0.0023, 0.4977, 0.1486, 0.0077, 0.1415, 0.0066]], held]] }],
  });
  const a = base.games[0].plies[0].analysis;
  assertEq(a.cube_decision.action, 'no_double', 'a cube not worth turning reads as no_double');
  assertEq(a.cube_decision.decision, false, 'ND-DT of 0.335 is too clear to count');
  assert(a.missed_double === undefined, 'no missed_double when holding was right');
}

// A close cube that was right to hold still counts toward PR: it is a decision
// the player got right, not a non-decision. ND -0.0046 vs DT -0.05.
{
  const base = baseDoc([0]);
  const close = ['2C', 0.502, 0.146, 0.008, 0.154, 0.009, 0.0, -0.0046, -0.05, 1.0];
  convertOg(base, {
    config: '2ply',
    games: [{ moves: [[OPENING_W, '25mhmk',
      [['2C', 'mhmk', 0.0023, 0.4977, 0.1486, 0.0077, 0.1415, 0.0066]], close]] }],
  });
  assertEq(base.games[0].plies[0].analysis.cube_decision.decision, true,
    'a close no-double counts toward PR');
}

// ---------------------------------------------------------------------------
// Which plies count toward PR
//
// OpenGammon's own count_xg_decisions is not what we apply -- we use the house
// rule shared with xg2gva and gvanalysis.game_eval, so that importing a match
// and re-analysing it produce the same PR. The two agree on every checker ply
// of the reference match; see the parity section.
// ---------------------------------------------------------------------------

/** One checker ply whose candidates carry the given equities. */
function checkerPly(equities) {
  const base = baseDoc([0]);
  const cands = equities.map((eq, i) => ['2C', ['mhmk', 'mhml', 'mimk'][i] || `x${i}`,
    eq, 0.4977, 0.1486, 0.0077, 0.1415, 0.0066]);
  convertOg(base, { config: '2ply', games: [{ moves: [[OPENING_W, '25mhmk', cands, []]] }] });
  return base.games[0].plies[0].analysis;
}

assertEq(checkerPly([0.0023]).decision, false,
  'a forced move (one candidate) is not a decision');
assertEq(checkerPly([0.0023, 0.00225]).decision, false,
  'candidates within 1e-4 are an already-decided position, not a decision');
assertEq(checkerPly([0.0023, 0.0021]).decision, true,
  'a spread of 2e-4 is a real choice');
assertEq(checkerPly([0.0023, 0.0021]).equity_loss, 0,
  'playing the best move costs nothing either way');

// The spread is over the whole candidate list, which OpenGammon does not sort
// strictly: the played move is appended out of order when it misses the top six.
assertEq(checkerPly([0.5, 0.4999, 0.2]).decision, true,
  'spread is max-min across all candidates, not the gap to the runner-up');

// A take/pass whose two branches are indistinguishable is not a decision.
{
  const base = baseDoc([0, 0]);
  base.games[0].plies[1].action_id = 22;
  convertOg(base, {
    config: '2ply',
    games: [{ moves: [
      [OPENING_W, '25mhmk', [['2C', 'mhmk', 0.0023, 0.4977, 0.1486, 0.0077, 0.1415, 0.0066]], []],
      [OPENING_W, 'take', [], ['2C', 0.502, 0.146, 0.008, 0.154, 0.009, 0.0, -0.6844, -1.0, -1.0]],
    ] }],
  });
  assertEq(base.games[0].plies[1].analysis.decision, false,
    'a take/pass with DT === DP is not a decision');
}

// ---------------------------------------------------------------------------
// Structure and failure modes
// ---------------------------------------------------------------------------

{
  const base = baseDoc([0]);
  convertOg(base, { config: '2ply', games: [{ moves: [[OPENING_W, '25mhmk', [['2C', 'mhmk', 0, 0.5, 0.1, 0.01, 0.1, 0.01]], []]] }] },
    { timestamp: 1785633886, model_id: 'gnubg' });
  assertEq(base.analysis_info,
    { ply: 2, eval_level: '2ply', luck_eval_level: null, model_id: 'gnubg', timestamp: 1785633886 },
    'analysis_info is populated from config and options');
}

assertThrows(() => convertOg(baseDoc([0]), { config: '2ply', games: [] }),
  'game count mismatch', 'a game-count mismatch is rejected');

assertThrows(() => convertOg(baseDoc([0, 1]), { config: '2ply', games: [{ moves: [[OPENING_W, '25mhmk', [], []]] }] }),
  'count mismatch in game 1', 'a ply-count mismatch names the game');

assertThrows(() => convertOg(baseDoc([0]), { config: '2ply' }),
  'carries no games', 'passing the job record instead of the payload is rejected');

// Nothing is written when a later game fails to line up.
{
  const base = { match_length: 5, games: [
    { game_index: 0, plies: [{ color: 0, action_id: 9, moves: [] }] },
    { game_index: 1, plies: [{ color: 0, action_id: 9, moves: [] }] },
  ] };
  try {
    convertOg(base, { config: '2ply', games: [
      { moves: [[OPENING_W, '25mhmk', [['2C', 'mhmk', 0, 0.5, 0.1, 0.01, 0.1, 0.01]], []]] },
      { moves: [] },
    ] });
  } catch { /* expected */ }
  assert(base.games[0].plies[0].analysis === undefined,
    'a mismatch in a later game leaves the document untouched');
}

// ---------------------------------------------------------------------------
// Resignations and timeouts
//
// Values below are from match nNgBvJ6hp8RrYzj1, whose first game ends in a
// resignation and which failed to import at all until the appended marker was
// recognised: 37 decision plies against 38 entries.
// ---------------------------------------------------------------------------

/** A resign/timeout entry of the appended kind: `prev`'s position, no evals. */
const marker = (prev, label) => [prev[0], label, [], []];

// The winner moved last, so the marker is appended and belongs to no ply.
{
  const played = [OPENING_W, '25mhmk', [['2C', 'mhmk', 0.0023, 0.4977, 0.1486, 0.0077, 0.1415, 0.0066]], []];
  const base = baseDoc([0]);
  convertOg(base, { config: '2ply', games: [{ moves: [played, marker(played, 'resigned')] }] });
  assert(base.games[0].plies[0].analysis != null,
    'an appended "resigned" marker consumes no ply');
  assertEq(base.games[0].plies[0].analysis.alternatives[0].is_played, true,
    'and the move it was appended after keeps its own analysis');
}

{
  const played = [OPENING_W, '25mhmk', [['2C', 'mhmk', 0.0023, 0.4977, 0.1486, 0.0077, 0.1415, 0.0066]], []];
  const base = baseDoc([0]);
  convertOg(base, { config: '2ply', games: [{ moves: [played, marker(played, 'timed out')] }] });
  assert(base.games[0].plies[0].analysis != null,
    'a timeout is marked the same way and skipped the same way');
}

// The discriminator is the repeated position, not the label: an entry carrying
// a fresh position is a real ply however it is labelled, so the count still has
// to match.
assertThrows(
  () => convertOg(baseDoc([0]), { config: '2ply', games: [{ moves: [
    [OPENING_W, '25mhmk', [], []],
    [OPENING_B, 'resigned', [], []],
  ] }] }),
  'count mismatch in game 1',
  'an extra entry at a new position is still a hard mismatch');

// The resigner moved last, so the label was written over their move. The entry
// is real and keeps its evaluations; only which candidate was played is lost.
{
  const base = baseDoc([0]);
  convertOg(base, { config: '2ply', games: [{ moves: [
    [OPENING_W, 'resigned', [['2C', 'mhmk', 0.0023, 0.4977, 0.1486, 0.0077, 0.1415, 0.0066]], []],
  ] }] });
  const a = base.games[0].plies[0].analysis;
  assertEq(a.best_equity, 0.0023, 'a label written over a move keeps that ply\'s evaluations');
  assertEq(a.played_equity, null, 'but the played candidate is unrecoverable from the payload');
}

// Same overwrite on a cube ply: read the kind from the base, or the take's
// analysis is dropped as a checker play with no candidates.
{
  const base = { match_length: 5, games: [{ game_index: 0, plies: [
    { color: 0, action_id: 22, moves: [] },
  ] }] };
  convertOg(base, { config: '2ply', games: [{ moves: [
    [OPENING_W, 'resigned', [], ['2C', 0.502, 0.146, 0.008, 0.154, 0.009, 0.0, -0.6844, -0.9, -1.0]],
  ] }] });
  const a = base.games[0].plies[0].analysis;
  assertEq(a.played_action, 'take', 'a label over a take still reads as a take');
  assertEq(a.correct_action, 'take', 'and its cube equities are still scored');
}

assert(convert_og === convertOg, 'snake_case alias is exported');

// ---------------------------------------------------------------------------
// Parity against a real match (skipped when samples are absent)
// ---------------------------------------------------------------------------

const samples = fs.existsSync(SAMPLES_DIR)
  ? fs.readdirSync(SAMPLES_DIR).filter((f) => f.endsWith('.analysis.json'))
  : [];

if (samples.length === 0) {
  console.log('SKIP  real-match parity (no samples in samples/og/)');
} else {
  for (const file of samples) {
    const id = file.replace(/\.analysis\.json$/, '');
    const matPath = path.join(SAMPLES_DIR, `${id}.mat`);
    if (!fs.existsSync(matPath)) {
      console.log(`SKIP  ${id} (no .mat alongside the payload)`);
      continue;
    }
    const payload = JSON.parse(fs.readFileSync(path.join(SAMPLES_DIR, file), 'utf8'));
    const base = convertMat(fs.readFileSync(matPath, 'utf8'));
    convertOg(base, payload);

    const isDecision = (p) => p.action_id >= 0 && p.action_id <= 23;
    let plies = 0;
    let analysed = 0;
    let badLoss = 0;
    let badRoute = 0;

    base.games.forEach((game) => {
      game.plies.filter(isDecision).forEach((ply) => {
        plies++;
        const a = ply.analysis;
        if (!a) return;
        analysed++;
        if (a.equity_loss != null && a.equity_loss < -1e-9) badLoss++;
        for (const alt of a.alternatives || []) {
          for (const s of alt.move) {
            if (s.pips < 1 || s.pips > 24 || s.from < 0 || s.from > 25) badRoute++;
          }
        }
      });
    });

    assert(analysed > 0, `${id}: analysis attached to ${analysed}/${plies} decision plies`);
    assertEq(badLoss, 0, `${id}: no negative equity losses`);
    assertEq(badRoute, 0, `${id}: every decoded move step is in range`);

    // PR parity against the numbers opengammon.com itself shows. Their XR is
    // the same 500 * error / decisions this repo computes, over their own
    // count_xg_decisions. The two counting rules
    // are close but not identical -- theirs drops a cube ply as "both sides
    // losing" at -0.500 where ours waits for -0.900 -- so the tolerances below
    // are deliberately not zero. They are tight enough that a regression of
    // the kind this section was written for (counting forced moves, or
    // counting a missed double twice) blows straight through them.
    const stats = payload.match_stats;
    if (stats) {
      const agg = compute_aggregates(base).match;

      // Which of their colours is which of ours is a property of the match, not
      // a constant. Their W is always Player 1 in the .mat, but OGXM white is
      // `_canonicalOrientation`'s pick -- the alphabetically first name -- so
      // the two line up or invert depending on who the players are.
      // nNgBvJ6hp8RrYzj1 is a match where they invert. Read the pairing off the
      // first entry instead: both sides agree about who moved first, and
      // colours are fixed for a match. (Nothing in the converter needs this; it
      // reads each entry's colour from that entry.)
      const firstPly = base.games[0].plies.find(isDecision);
      const firstEntry = payload.games[0].moves[0];
      const theirW = String(firstEntry[0]).split(':')[4] === 'W';
      const oursFirst = firstPly.color === 0 ? agg.black : agg.white;
      const oursOther = firstPly.color === 0 ? agg.white : agg.black;
      const pairs = theirW
        ? [['W', oursFirst], ['B', oursOther]]
        : [['B', oursFirst], ['W', oursOther]];

      for (const [ogSide, ours] of pairs) {
        const og = stats[ogSide];
        assert(Math.abs(ours.total_error - og.EQ) < 0.01,
          `${id}/${ogSide}: total error ${ours.total_error} within 0.01 of OG's ${og.EQ.toFixed(4)}`);
        assert(Math.abs(ours.total_decisions - og['NR unforced']) <= 1,
          `${id}/${ogSide}: ${ours.total_decisions} decisions counted vs OG's ${og['NR unforced']}`);
        assert(Math.abs(ours.pr - og.XR) < 0.5,
          `${id}/${ogSide}: PR ${ours.pr} within 0.5 of OG's ${og.XR.toFixed(2)}`);
      }
    }
  }
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
