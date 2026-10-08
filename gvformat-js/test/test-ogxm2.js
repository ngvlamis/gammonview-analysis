// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Reading OGXM v2, HedgeHog's current format, into the v1 document shape.
//
// The fixtures were written by HedgeHog's own reference writer
// (`ogxm_convert --to-binary`) from a real hedgehog-bg.com export, with the
// players renamed and its signatures removed; `fixtures/ogxm2/gen.py` says how.
// Each `.expected.json` is the reference reader's replay of the same bytes --
// the OGID before and after every ply -- so the positions here are checked
// against HedgeHog's, not against ourselves.

import { readFileSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';
import { readGvab, GvabError } from '../src/reader.js';
import { write_gvab } from '../src/binary.js';

let passed = 0, failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'ogxm2');
const bytes = (name) => new Uint8Array(readFileSync(join(FIXTURES, `${name}.ogxm`)));
const expected = (name) => JSON.parse(readFileSync(join(FIXTURES, `${name}.expected.json`), 'utf8'));

function throwsGvab(fn, pattern) {
  try { fn(); } catch (e) { return e instanceof GvabError && pattern.test(e.message); }
  return false;
}

/** Every ply's OGIDs against the reference's. A game we prefix with a
 *  set-position ply is compared from the ply after it. */
function ogidMismatches(doc, exp) {
  const out = [];
  doc.games.forEach((g, gi) => {
    const off = g.plies.length - exp.ogids[gi].length;
    g.plies.slice(off).forEach((p, pi) => {
      const [before, after] = exp.ogids[gi][pi];
      if (p.ogid_before !== before || p.ogid_after !== after) out.push(`${gi},${pi + off}`);
    });
  });
  return out;
}

const plies = (doc) => doc.games.flatMap((g) => g.plies);

// ---------------------------------------------------------------------------
// A HedgeHog match, analysed
// ---------------------------------------------------------------------------

{
  const doc = readGvab(bytes('match'));
  const exp = expected('match');
  assert(doc.games.length === 3 && doc.games[0].plies.length === 19 && doc.games[1].plies.length === 49,
    'match: three games, every ply');
  assert(ogidMismatches(doc, exp).length === 0, 'match: every ply replays to the reference position');
  assert(doc.player_white === 'Alice' && doc.player_black === 'Bob' && doc.match_length === 9,
    'match: header fields');
  assert(doc.white_score === 1 && doc.black_score === 5 && doc.result === 0,
    'match: an unfinished match keeps its running score and no result');
  assert(doc.games[0].winner === 0 && doc.games[1].winner === 1 && doc.games[1].points_won === 4,
    'match: game winners and points');
  assert(doc.analysis_info.model_id === 'hedgehog/xerxes', 'match: the model is named, not its UUID');
  assert(doc.analysis_info.ply === 2 && doc.analysis_info.eval_level === '2ply', 'match: block depth');
  // HedgeHog stores luck per roll; it is read, as an .xg's or a .bgf's is.
  const rolled = plies(doc).filter((p) => p.action_id <= 20);
  const lucky = rolled.filter((p) => typeof p.analysis?.luck === 'number');
  assert(lucky.length === rolled.length - doc.games.length,
    'match: every roll but each game\'s opening one carries its luck');
  assert(lucky.some((p) => p.analysis.luck > 0) && lucky.some((p) => p.analysis.luck < 0),
    'match: luck runs both ways');
  assert(lucky.some((p) => Math.abs(p.analysis.luck) > 0.1),
    'match: luck is on the equity scale (MWC swings are a fraction of it)');
  assert(doc.analysis_info.luck_eval_level === '1ply', 'match: luck level from the rolls\' own');
  assert(doc._base_analyses === undefined, 'match: luck is stored, so none needs measuring');

  // HedgeHog analyses match play in MWC; these must arrive as normalized equity.
  const doubles = plies(doc).filter((p) => p.action_id === 21 && p.analysis);
  assert(doubles.length > 0
    && doubles.every((p) => Math.abs(p.analysis.double_pass_equity - 1) < 0.02),
    'match: a cube ply\'s double/pass reads as +1, not as an MWC');
  const checkers = plies(doc).filter((p) => p.analysis?.alternatives);
  assert(checkers.some((p) => p.analysis.best_equity < 0),
    'match: checker equities are on the equity scale (an MWC is never negative)');
  assert(checkers.every((p) => {
    const a = p.analysis;
    const played = a.alternatives.find((x) => x.is_played);
    return !played || Math.abs(a.equity_loss - (a.best_equity - played.equity)) < 2e-4;
  }), 'match: equity_loss is best minus played, in the same units');
  assert(checkers.filter((p) => p.analysis.decision).length > 0
    && checkers.some((p) => !p.analysis.decision),
    'match: decision flags derived (some plays count, a forced one does not)');
  const missed = plies(doc).filter((p) => p.analysis?.missed_double);
  assert(missed.length > 0
    && missed.every((p) => p.analysis.cube_decision && p.analysis.missed_double.equity_loss > 0),
    'match: a missed double carries its error and the live decision beside it');

  // Saving writes v1; reading that back must give the same match.
  const again = readGvab(write_gvab(doc));
  assert(ogidMismatches(again, exp).length === 0, 'match: survives a v1 write and re-read');
  assert(plies(again).filter((p) => p.analysis).length === plies(doc).filter((p) => p.analysis).length,
    'match: every analysis survives the v1 write');
  assert(plies(again).filter((p) => typeof p.analysis?.luck === 'number').length
    === plies(doc).filter((p) => typeof p.analysis?.luck === 'number').length
    && again.analysis_info.luck_eval_level === '1ply',
    'match: luck survives the v1 write');
}

// ---------------------------------------------------------------------------
// Container checks
// ---------------------------------------------------------------------------

{
  const data = bytes('match');                     // written with a CRC32 CSUM
  const corrupt = data.slice();
  corrupt[40] ^= 0xFF;
  assert(throwsGvab(() => readGvab(corrupt), /CSUM mismatch/), 'a corrupted byte fails the checksum');

  const later = data.slice();
  later[10] = 3;                                   // min_reader_minor 3: a reader we are not
  assert(throwsGvab(() => readGvab(later), /2\.3 reader/), 'a newer minimum reader is refused');

  assert(throwsGvab(() => readGvab(data.subarray(0, data.length - 10)), /file_size/),
    'a truncated file is refused');
}

// ---------------------------------------------------------------------------
// Two blocks: HedgeHog's deeper re-run of a few decisions
// ---------------------------------------------------------------------------

{
  const doc = readGvab(bytes('two-blocks'));
  assert(doc.analyses_info?.length === 2, 'two blocks: both listed');
  assert(doc.analyses_info[1].ply === 3, 'two blocks: the second reads at its own depth');
  const both = plies(doc).filter((p) => p.analyses?.length === 2);
  assert(both.length === 3 && both.every((p) => p.analyses[1].analysis_index === 1),
    'two blocks: the re-run decisions sit beside the first block\'s');
  assert(JSON.stringify(doc._base_analyses) === '[1]',
    'two blocks: only the block with no rolls is left for a luck pass');
}

// ---------------------------------------------------------------------------
// A game from a set-up position
// ---------------------------------------------------------------------------

{
  const doc = readGvab(bytes('set-up'));
  const g = doc.games[1];
  assert(g.plies[0].action_id === 31 && !g.plies[0].d1
    && JSON.stringify(g.plies[0].set_position) === JSON.stringify(g.initial_board),
    'set-up: the starting board becomes a leading set-position ply');
  assert(ogidMismatches(doc, expected('set-up')).length === 0,
    'set-up: every ply after it replays to the reference position');
  assert(g.plies[1].analysis && g.plies[1].analysis.alternatives,
    'set-up: decisions still find their plies past the inserted one');
}

// ---------------------------------------------------------------------------
// A resignation
// ---------------------------------------------------------------------------

{
  const doc = readGvab(bytes('resign'));
  const g = doc.games[0];
  const last = g.plies[g.plies.length - 1];
  assert(last.action_id === 27, 'resign: the resignation is a ply');
  assert(g.winner === (last.color === 1 ? 1 : 0) && g.points_won === 1, 'resign: the other side wins');
  assert(ogidMismatches(doc, expected('resign')).length === 0, 'resign: replays to the reference');
}

// ---------------------------------------------------------------------------
// What the v1 shape cannot replay is refused, in words for a player
// ---------------------------------------------------------------------------

assert(throwsGvab(() => readGvab(bytes('cube-on-two')), /cube already turned.*cannot show/),
  'a game opening with the cube turned is refused, not shown wrongly');

// ---------------------------------------------------------------------------
// Untrusted bytes: whatever is wrong with them, the failure is a GvabError
// ---------------------------------------------------------------------------

{
  const data = bytes('match');
  let seed = 12345;
  const rand = (n) => { seed = (seed * 1103515245 + 12345) >>> 0; return seed % n; };
  let leaked = 0;
  for (let i = 0; i < 3000; i++) {
    const m = data.slice();
    for (let k = 1 + rand(4); k > 0; k--) m[16 + rand(m.length - 20)] = rand(256);
    try { readGvab(m, { verifyCrc: false }); } catch (e) { if (!(e instanceof GvabError)) leaked++; }
  }
  assert(leaked === 0, `3000 mutated v2 files raise only GvabError (leaked ${leaked})`);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
