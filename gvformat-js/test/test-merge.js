// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Multi-analysis: appendAnalysis + the reader/writer's ANAL-group handling.

import { write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { appendAnalysis, analysisCount, MAX_ANALYSES } from '../src/merge.js';
import { OGXM_MAGIC, CHUNK_ANAL, END_MAGIC } from '../src/constants.js';

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

function assertThrows(fn, match, msg) {
  try {
    fn();
  } catch (e) {
    if (String(e.message).includes(match)) {
      passed++;
      console.log(`OK  ${msg}`);
    } else {
      failed++;
      console.error(`FAIL  ${msg} -- wrong message: ${e.message}`);
    }
    return;
  }
  failed++;
  console.error(`FAIL  ${msg} -- did not throw`);
}

function uint8Equal(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

// Count ANAL chunks by scanning the TLV stream, independent of the reader.
function countAnalChunks(bytes) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let pos = 20;
  let n = 0;
  while (pos + 8 <= bytes.length) {
    const type = dv.getUint32(pos, true);
    if (type === END_MAGIC) break;
    const len = dv.getUint32(pos + 4, true);
    if (type === CHUNK_ANAL) n++;
    pos += 12 + len;
  }
  return n;
}

// File header is struct.pack("<IHHHHII", magic, verMajor, verMinor, verMajor,
// minReaderMinor, fileSize, headerFlags) -- min_reader_minor is the *fourth*
// uint16, at offset 10.
function minReaderMinor(bytes) {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint16(10, true);
}

// `equity` is the per-block discriminator: it is stored in EVAL/ALTS and so
// survives a round-trip. (An alternative's `notation` is *derived* by consumers,
// never serialized, so it cannot be used to tell two blocks apart.)
function makeAnalysis(equity) {
  return {
    eval: {
      win: 0.5, gammon_win: 0.1, bg_win: 0.02,
      gammon_loss: 0.08, bg_loss: 0.01, equity,
    },
    best_equity: equity,
    played_equity: equity - 0.01,
    equity_loss: 0.01,
    decision: true,
    alternatives: [{
      move: [{ from: 24, pips: 2 }],
      equity: equity - 0.02,
      is_played: false,
      diff: -0.02,
      eval: {
        win: 0.49, gammon_win: 0.1, bg_win: 0.02,
        gammon_loss: 0.09, bg_loss: 0.01, equity: equity - 0.02,
      },
    }],
    luck: 0.02,
  };
}

// A two-game match: game 0 has a checker ply carrying analysis and a cube
// decision; game 1 has a checker ply and a terminal ply (which must be skipped
// when aligning decision plies).
function makeMatch({ analysis, modelId, equity = 0.53 } = {}) {
  const ogxm = {
    match_length: 7,
    player_white: 'White',
    player_black: 'Black',
    white_score: 3,
    black_score: 2,
    result: 0,
    source: 1,
    timestamp: 1700000000,
    crawford: true,
    games: [
      {
        game_index: 0,
        winner: 0,
        points_won: 2,
        is_crawford: false,
        is_lastgame: false,
        first_to_move: 0,
        plies: [
          { color: 0, action_id: 1, d1: 1, d2: 2, moves: [{ from: 24, pips: 1 }] },
          { color: 1, action_id: 21 },  // ACTION_DOUBLE
          { color: 0, action_id: 22 },  // ACTION_TAKE
        ],
      },
      {
        game_index: 1,
        winner: 1,
        points_won: 1,
        is_crawford: false,
        is_lastgame: true,
        first_to_move: 1,
        plies: [
          { color: 1, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 2 }, { from: 13, pips: 3 }] },
          { color: 1, action_id: 24 },  // terminal -- never carries analysis
        ],
      },
    ],
  };

  if (analysis) {
    ogxm.analysis_info = {
      ply: 3, model_id: modelId || 'test-model', timestamp: 1700000001,
    };
    ogxm.games[0].plies[0].analysis = makeAnalysis(equity);
    ogxm.games[0].plies[1].analysis = {
      eval: { win: 0.55, gammon_win: 0.12, bg_win: 0.02, gammon_loss: 0.07, bg_loss: 0.01, equity: 0.6 },
      double_equity: 0.6, nodouble_equity: 0.55, take_equity: 0.58, pass_equity: 1.0,
      best_action: 'double', played_action: 'double', error: 0.0, decision: true,
    };
    ogxm.games[1].plies[0].analysis = makeAnalysis(0.31);
  }
  return ogxm;
}

// Normalize through a write/read cycle so every float is already quantized --
// comparisons downstream can then be exact.
function canonical(ogxm) {
  return readGvab(write_gvab(ogxm), { deriveOgids: false });
}

// --- 1. Appending onto a base with no analysis gives the legacy shape --------
{
  const our = canonical(makeMatch({ analysis: true }));
  const base = canonical(makeMatch({ analysis: false }));

  const merged = appendAnalysis(base, our);

  assert(merged.analyses_info === undefined,
    'single append leaves the document in legacy single-analysis shape');
  assert(merged.analysis_info.model_id === 'test-model',
    "analysis_info comes from 'our'");
  assert(merged.games[0].plies[0].analysis !== undefined,
    'checker analysis transplanted onto the base ply');
  assert(merged.games[1].plies[0].analysis !== undefined,
    'analysis transplanted in the second game too');
  assert(merged.games[1].plies[1].analysis === undefined,
    'terminal ply gets no analysis');

  assert(uint8Equal(write_gvab(merged), write_gvab(our)),
    'merging into an unanalysed base is byte-identical to analysing directly');
}

// --- 2. A second analysis promotes to multi form ----------------------------
{
  const base = canonical(makeMatch({ analysis: true, modelId: 'engine-a' }));
  const our = canonical(makeMatch({ analysis: true, modelId: 'engine-b' }));

  const merged = appendAnalysis(base, our);

  assert(merged.analyses_info.length === 2, 'analyses_info lists both blocks');
  assert(merged.analyses_info[0].model_id === 'engine-a', 'existing block stays first');
  assert(merged.analyses_info[1].model_id === 'engine-b', "'our' block is appended last");
  assert(merged.analysis_info.model_id === 'engine-a',
    'analysis_info still mirrors block 0 for naive readers');

  const ply = merged.games[0].plies[0];
  assert(ply.analyses.length === 2, 'decision ply carries both analyses');
  assert(ply.analyses[0].analysis_index === 0 && ply.analyses[1].analysis_index === 1,
    'analyses are tagged with their block index');
  assert(ply.analysis !== undefined, 'per-ply analysis still mirrors block 0');
  assert(merged.games[1].plies[1].analyses === undefined,
    'terminal ply gets no analyses array');
}

// --- 3. Multi-analysis survives a write/read round-trip ---------------------
{
  const base = canonical(makeMatch({ analysis: true, modelId: 'engine-a', equity: 0.53 }));
  const our = canonical(makeMatch({ analysis: true, modelId: 'engine-b', equity: 0.21 }));
  const merged = appendAnalysis(base, our);

  const bytes = write_gvab(merged);
  assert(countAnalChunks(bytes) === 2, 'two ANAL chunk groups are written');
  assert(minReaderMinor(bytes) === 3, 'min_reader_minor is raised to 3');

  const decoded = readGvab(bytes, { deriveOgids: false });
  assert(decoded.analyses_info.length === 2, 'reader recovers both blocks');
  assert(decoded.analyses_info[0].model_id === 'engine-a', 'block 0 model_id survives');
  assert(decoded.analyses_info[1].model_id === 'engine-b', 'block 1 model_id survives');

  const ply = decoded.games[0].plies[0];
  assert(ply.analyses.length === 2, 'ply analyses survive the round-trip');
  assert(Math.abs(ply.analyses[0].alternatives[0].equity - 0.51) < 0.001,
    'block 0 alternatives bind to block 0');
  assert(Math.abs(ply.analyses[1].alternatives[0].equity - 0.19) < 0.001,
    'block 1 alternatives bind to block 1');

  const cubePly = decoded.games[0].plies[1];
  assert(cubePly.analyses.length === 2, 'cube decision plies carry both blocks too');

  assert(uint8Equal(bytes, write_gvab(decoded)),
    'byte-for-byte round-trip (multi-analysis)');
}

// --- 4. Single-analysis files are untouched by the change -------------------
{
  const one = canonical(makeMatch({ analysis: true }));
  const bytes = write_gvab(one);
  assert(countAnalChunks(bytes) === 1, 'one analysis still writes one ANAL group');
  assert(minReaderMinor(bytes) !== 3, 'a single block does not raise min_reader_minor');
  assert(one.analyses_info === undefined, 'single analysis has no analyses_info');
}

// --- 5. 'our' carrying several blocks appends all of them -------------------
{
  const base = canonical(makeMatch({ analysis: true, modelId: 'engine-a' }));
  const twoBlock = appendAnalysis(
    canonical(makeMatch({ analysis: true, modelId: 'engine-b' })),
    canonical(makeMatch({ analysis: true, modelId: 'engine-c' })));
  assert(twoBlock.analyses_info.length === 2, 'setup: our carries two blocks');

  const merged = appendAnalysis(base, twoBlock);
  assert(merged.analyses_info.length === 3,
    'a multi-analysis `our` contributes every block it carries');
  assert(merged.analyses_info.map(i => i.model_id).join(',') === 'engine-a,engine-b,engine-c',
    'blocks land in order, existing first');
  assert(merged.games[0].plies[0].analyses.map(a => a.analysis_index).join(',') === '0,1,2',
    'appended blocks are renumbered contiguously');
}

// --- 6. 'our' with no analysis is a no-op ----------------------------------
{
  const base = canonical(makeMatch({ analysis: true }));
  const empty = canonical(makeMatch({ analysis: false }));
  const merged = appendAnalysis(base, empty);
  assert(uint8Equal(write_gvab(merged), write_gvab(base)),
    'appending an unanalysed document changes nothing');
  assert(merged !== base, 'the result is still a copy, not the base itself');
}

// --- 7. The base is never mutated ------------------------------------------
{
  const base = canonical(makeMatch({ analysis: true, modelId: 'engine-a' }));
  const before = write_gvab(base);
  appendAnalysis(base, canonical(makeMatch({ analysis: true, modelId: 'engine-b' })));
  assert(uint8Equal(write_gvab(base), before), 'appendAnalysis leaves the base untouched');
}

// --- 8. Guards --------------------------------------------------------------
{
  const base = canonical(makeMatch({ analysis: true }));

  const flipped = canonical(makeMatch({ analysis: true }));
  flipped.player_white = 'Black';
  flipped.player_black = 'White';
  assertThrows(() => appendAnalysis(base, flipped), 'orientation mismatch',
    'opposite orientation is rejected rather than mirrored');

  const shortGame = canonical(makeMatch({ analysis: true }));
  shortGame.games[0].plies.pop();
  assertThrows(() => appendAnalysis(base, shortGame), 'analysis/ply count mismatch',
    'a decision-ply count mismatch is rejected');

  const fewerGames = canonical(makeMatch({ analysis: true }));
  fewerGames.games.pop();
  assertThrows(() => appendAnalysis(base, fewerGames), 'game count mismatch',
    'a game count mismatch is rejected rather than silently truncated');
}

// --- 9. MAX_ANALYSES cap ----------------------------------------------------
{
  let doc = canonical(makeMatch({ analysis: true, modelId: 'engine-0' }));
  for (let i = 1; i < MAX_ANALYSES; i++) {
    doc = appendAnalysis(doc, canonical(makeMatch({ analysis: true, modelId: `engine-${i}` })));
  }
  assert(doc.analyses_info.length === MAX_ANALYSES,
    `${MAX_ANALYSES} blocks can be accumulated`);
  assert(countAnalChunks(write_gvab(doc)) === MAX_ANALYSES,
    'all of them serialize');

  assertThrows(
    () => appendAnalysis(doc, canonical(makeMatch({ analysis: true, modelId: 'one-too-many' }))),
    'format cap',
    'appending past MAX_ANALYSES is rejected');
}

// --- 10. analysisCount ------------------------------------------------------
{
  assert(analysisCount(canonical(makeMatch({ analysis: false }))) === 0,
    'analysisCount is 0 for an unanalysed match');
  assert(analysisCount(canonical(makeMatch({ analysis: true }))) === 1,
    'analysisCount is 1 for a single-analysis match');
  const two = appendAnalysis(
    canonical(makeMatch({ analysis: true, modelId: 'a' })),
    canonical(makeMatch({ analysis: true, modelId: 'b' })));
  assert(analysisCount(two) === 2, 'analysisCount is 2 after a merge');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
