// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Round-trip test: write_gvab(read_gvab(bytes)) === bytes

import { write_gvab, _encode_gvan, _build_checker_eval, _build_cube_eval_decision } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { compute_aggregates } from '../src/stats.js';
import { OGXM_MAGIC, END_MAGIC, CHUNK_GVAN } from '../src/constants.js';

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

function uint8Equal(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

// Minimal TLV chunk scanner: file header is 20 bytes
// (struct.pack("<IHHHHII", magic, verMajor, verMinor, verMajor, minReaderMinor,
// fileSize, headerFlags)), followed by chunks (struct.pack("<IIHH", type, len,
// flags, 0) + data), terminated by a raw 8-byte end marker (magic + total_size).
function findChunk(bytes, type) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  assert(dv.getUint32(0, true) === OGXM_MAGIC, "file starts with OGXM magic");
  let pos = 20;
  while (pos + 8 <= bytes.length) {
    const chunkType = dv.getUint32(pos, true);
    if (chunkType === END_MAGIC) break;
    const len = dv.getUint32(pos + 4, true);
    pos += 12;
    if (chunkType === type) return bytes.slice(pos, pos + len);
    pos += len;
  }
  return null;
}

// Test 1: Minimal match (no analysis)
{
  const ogxm = {
    match_length: 0,
    player_white: "Alice",
    player_black: "Bob",
    white_score: 0,
    black_score: 0,
    result: 0,
    source: 0,
    timestamp: 0,
    games: [{
      game_index: 0,
      winner: 0,
      points_won: 1,
      is_crawford: false,
      is_lastgame: false,
      first_to_move: 0,
      plies: [
        { color: 0, action_id: 1, d1: 1, d2: 2, moves: [{ from: 24, pips: 1 }] },
        { color: 1, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 2 }, { from: 13, pips: 3 }] },
      ],
    }],
  };

  const bytes = write_gvab(ogxm);
  assert(bytes instanceof Uint8Array, "write_gvab returns Uint8Array");
  assert(bytes.length > 28, "output has reasonable size");

  const decoded = readGvab(bytes, { deriveOgids: false });
  assert(decoded.player_white === "Alice", "player_white round-trips");
  assert(decoded.player_black === "Bob", "player_black round-trips");
  assert(decoded.games.length === 1, "game count round-trips");
  assert(decoded.games[0].plies.length === 2, "ply count round-trips");

  const rebytes = write_gvab(decoded);
  assert(uint8Equal(bytes, rebytes), "byte-for-byte round-trip (no analysis)");
}

// Test 2: Match with analysis
{
  const ogxm = {
    match_length: 7,
    player_white: "White",
    player_black: "Black",
    white_score: 3,
    black_score: 2,
    result: 0,
    source: 1,
    timestamp: 1700000000,
    crawford: true,
    games: [{
      game_index: 0,
      winner: 0,
      points_won: 2,
      is_crawford: false,
      is_lastgame: false,
      first_to_move: 0,
      plies: [
        {
          color: 0, action_id: 1, d1: 1, d2: 2,
          moves: [{ from: 24, pips: 1 }],
          analysis: {
            eval: { win: 0.5, gammon_win: 0.1, bg_win: 0.02, gammon_loss: 0.08, bg_loss: 0.01, equity: 0.53 },
            best_equity: 0.53,
            played_equity: 0.52,
            equity_loss: 0.01,
            decision: true,
            alternatives: [{
              move: [{ from: 24, pips: 2 }],
              notation: "24/22",
              equity: 0.51,
              is_played: false,
              diff: -0.02,
              eval: { win: 0.49, gammon_win: 0.1, bg_win: 0.02, gammon_loss: 0.09, bg_loss: 0.01, equity: 0.51 },
            }],
            luck: 0.02,
            // The player held a cube that should have turned. Its eval is the
            // *pre-roll* read of that decision -- a different question from the
            // post-roll `eval` above, which describes the play made instead.
            missed_double: {
              no_double_equity: 0.53, double_take_equity: 0.72,
              double_pass_equity: 1.0, equity_loss: 0.19,
              correct_action: "double", eval_level: "2ply",
              eval: { win: 0.68, gammon_win: 0.24, bg_win: 0.03, gammon_loss: 0.06, bg_loss: 0.01, equity: 0.53 },
            },
          },
        },
        { color: 1, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 2 }, { from: 13, pips: 3 }] },
      ],
    }],
  };

  const bytes = write_gvab(ogxm);
  const decoded = readGvab(bytes, { deriveOgids: false });
  const rebytes = write_gvab(decoded);
  assert(uint8Equal(bytes, rebytes), "byte-for-byte round-trip (with analysis)");

  // Verify analysis fields survived
  const ply0 = decoded.games[0].plies[0];
  assert(ply0.analysis !== undefined, "analysis present on decoded ply");
  assert(ply0.analysis.decision === true, "decision flag round-trips");
  assert(Math.abs(ply0.analysis.best_equity - 0.53) < 0.0001, "best_equity round-trips (quantized)");
  assert(Math.abs(ply0.analysis.equity_loss - 0.01) < 0.0001, "equity_loss round-trips");

  const md = ply0.analysis.missed_double;
  assert(md !== undefined, "missed_double round-trips");
  assert(md.eval !== undefined && Math.abs(md.eval.win - 0.68) < 0.0001
         && Math.abs(md.eval.gammon_win - 0.24) < 0.0001
         && Math.abs(md.eval.bg_loss - 0.01) < 0.0001,
    "missed_double pre-roll probs round-trip");
  assert(md.eval_level === "2ply", "missed_double eval_level round-trips");
  // And they are the cube's own numbers, not the checker play's.
  assert(Math.abs(md.eval.win - ply0.analysis.eval.win) > 0.01,
    "missed_double probs are distinct from the checker ply's post-roll probs");
}

// Test 3: Empty match
{
  const ogxm = {
    match_length: 0,
    player_white: "A",
    player_black: "B",
    white_score: 0,
    black_score: 0,
    result: 0,
    source: 0,
    timestamp: 0,
    games: [],
  };
  const bytes = write_gvab(ogxm);
  const decoded = readGvab(bytes, { deriveOgids: false });
  const rebytes = write_gvab(decoded);
  assert(uint8Equal(bytes, rebytes), "byte-for-byte round-trip (empty match)");
}

// Test 4: Multiple games
{
  const ogxm = {
    match_length: 3,
    player_white: "W",
    player_black: "B",
    white_score: 0,
    black_score: 0,
    result: 0,
    source: 0,
    timestamp: 0,
    games: [
      {
        game_index: 0, winner: 0, points_won: 1,
        is_crawford: false, first_to_move: 0,
        plies: [{ color: 0, action_id: 6, d1: 3, d2: 4, moves: [] }],
      },
      {
        game_index: 1, winner: 1, points_won: 2,
        is_crawford: true, first_to_move: 1,
        plies: [{ color: 1, action_id: 10, d1: 4, d2: 5, moves: [] }],
      },
    ],
  };
  const bytes = write_gvab(ogxm);
  const decoded = readGvab(bytes, { deriveOgids: false });
  assert(decoded.games.length === 2, "multiple games round-trip");
  const rebytes = write_gvab(decoded);
  assert(uint8Equal(bytes, rebytes), "byte-for-byte round-trip (multiple games)");
}

// Test 5: GVAN v3 layout -- version byte, 3-byte checker records, 2-byte
// cube records, reserved zero slots (former mwc_on_win/mwc_on_loss anchors).
{
  const ogxm = {
    match_length: 7,
    player_white: "White",
    player_black: "Black",
    white_score: 2,
    black_score: 3,
    result: 0,
    source: 1,
    timestamp: 1700000000,
    games: [{
      game_index: 0,
      winner: 0,
      points_won: 1,
      is_crawford: false,
      is_lastgame: false,
      first_to_move: 0,
      plies: [
        {
          color: 0, action_id: 1, d1: 1, d2: 2,
          moves: [{ from: 24, pips: 1 }],
          analysis: {
            eval: { win: 0.5, gammon_win: 0.1, bg_win: 0.02, gammon_loss: 0.08, bg_loss: 0.01, equity: 0.53 },
            best_equity: 0.53,
            equity_loss: 0.01,
            decision: true,
            alternatives: [],
            luck: 0.02,
          },
        },
        {
          color: 1, action_id: 21, // standalone double decision
          analysis: {
            decision: true,
            equity_loss: 0.0,
            correct_action: "no_double",
            played_action: "no_double",
            no_double_equity: 0.1,
            double_take_equity: -0.2,
            double_pass_equity: 1.0,
          },
        },
      ],
    }],
  };

  const bytes = write_gvab(ogxm);
  const gvan = findChunk(bytes, CHUNK_GVAN);
  assert(gvan !== null, "GVAN chunk present");
  if (gvan !== null) {
    const dv = new DataView(gvan.buffer, gvan.byteOffset, gvan.byteLength);
    assert(dv.getUint8(0) === 3, "GVAN version byte == 3");
    const sectionFlags = dv.getUint8(2);
    // 1 checker record (3 bytes), 0 alt bytes, 1 cube record (2 bytes).
    const expectedLen = 4
      + ((sectionFlags & 0x01) ? 3 * 1 : 0)
      + 0
      + ((sectionFlags & 0x04) ? 2 * 1 : 0);
    assert(gvan.length === expectedLen,
      `GVAN chunk length matches 3-byte checker / 2-byte cube records (${gvan.length} == ${expectedLen})`);

    // Checker record at offset 4: <Bh> flags, luck(i16). v3 dropped the two
    // blanked mwc-anchor uint16 that used to follow.
    const checkerFlags = dv.getUint8(4);
    const luckRaw = dv.getInt16(5, true);
    assert((checkerFlags & 0x02) !== 0, "checker has_luck flag set");
    assert(luckRaw === Math.round(0.02 * 10000), "checker luck i16 encodes 0.02");

    // Cube record at offset 7: <BB> flags, evlvl.
    const cubeFlags = dv.getUint8(7);
    assert((cubeFlags & 0x01) !== 0, "cube decision flag set");
  }
}

// Test 6: _encode_gvan byte-level unit check directly on builder records
// (belt-and-suspenders on top of Test 5's full round-trip check).
{
  const checkerEval = _build_checker_eval(0, 0, {
    decision: true, luck: -0.5, illegal_move: false,
    alternatives: [], eval: undefined,
  });
  const cubeEval = _build_cube_eval_decision(0, 1, {
    decision: false, correct_action: "no_double", played_action: "no_double",
  }, 0);

  const gvan = _encode_gvan({}, [checkerEval], [cubeEval]);
  assert(gvan.length === 4 + 3 + 2, "encode_gvan: 4-byte header + 3-byte checker + 2-byte cube");
  assert(gvan[0] === 3, "encode_gvan: version byte 3");

  const dv = new DataView(gvan.buffer, gvan.byteOffset, gvan.byteLength);
  assert(dv.getInt16(5, true) === Math.round(-0.5 * 10000), "encode_gvan: checker luck i16");
  // v3: the cube record follows the checker record immediately -- the two
  // blanked mwc-anchor uint16 that used to pad each are gone.
  assert(dv.getUint8(7) === 0, "encode_gvan: cube flags byte follows at offset 7");
}

// Test 7: PR survives the write.
//
// The property, not any one field: a `.gvab` is what gets *stored*, so every
// input to `compute_aggregates` has to come back out of it. It did not -- a
// missed *re*double was filed under `cube_decision`, whose `equity_loss` is
// zero by definition on disk (CUBE type=4), so the error was real in memory
// and gone from the file. One BGBlitz match read PR 7.63 in the viewer and
// 5.99 after saving.
//
// Asserting on the totals rather than on the fields means the next field that
// fails to survive a write fails here too, whatever it turns out to be.
{
  const ogxm = {
    match_length: 5,
    player_white: "Alice",
    player_black: "Bob",
    white_score: 0,
    black_score: 0,
    result: 1,
    source: 0,
    timestamp: 0,
    analysis_info: { ply: 3, eval_level: "3ply", model_id: "test", timestamp: 0 },
    games: [{
      game_index: 0,
      winner: 0,
      points_won: 1,
      is_crawford: false,
      is_lastgame: true,
      first_to_move: 0,
      plies: [
        // A plain checker error.
        {
          color: 0, action_id: 1, d1: 1, d2: 2, moves: [{ from: 24, pips: 1 }],
          analysis: {
            best_equity: 0.5, played_equity: 0.4, equity_loss: 0.1,
            decision: true, alternatives: [],
          },
        },
        // A missed double: the cube error rides in `missed_double`, which is
        // the key whose `equity_loss` the format actually stores.
        {
          color: 1, action_id: 6, d1: 2, d2: 3,
          moves: [{ from: 13, pips: 2 }, { from: 13, pips: 3 }],
          analysis: {
            best_equity: 0.2, played_equity: 0.2, equity_loss: 0,
            decision: true, alternatives: [],
            missed_double: {
              no_double_equity: 0.55, double_take_equity: 0.8,
              double_pass_equity: 1.0, equity_loss: 0.25,
              correct_action: "double",
            },
          },
        },
        // A correct no-double. Its `equity_loss` is 0 by definition -- CUBE
        // type=4 has no field for anything else -- and its `decision` flag
        // does round-trip, in GVAN's cube section.
        {
          color: 0, action_id: 2, d1: 3, d2: 1, moves: [{ from: 13, pips: 3 }],
          analysis: {
            best_equity: 0.3, played_equity: 0.3, equity_loss: 0,
            decision: true, alternatives: [],
            cube_decision: {
              should_double: false, no_double_equity: 0.6,
              double_take_equity: 0.2, double_pass_equity: 1.0,
              action: "no_double", equity_loss: 0.0, decision: true,
            },
          },
        },
      ],
    }],
  };

  const before = compute_aggregates(ogxm).match;
  const after = compute_aggregates(readGvab(write_gvab(ogxm))).match;

  for (const side of ["white", "black"]) {
    assert(before[side].total_error === after[side].total_error,
      `round-trip: ${side}'s total error is unchanged`);
    assert(before[side].total_decisions === after[side].total_decisions,
      `round-trip: ${side}'s decision count is unchanged`);
    assert(before[side].cube_decisions === after[side].cube_decisions,
      `round-trip: ${side}'s cube-decision count is unchanged`);
    assert(before[side].pr === after[side].pr, `round-trip: ${side}'s PR is unchanged`);
  }

  // And the specific one, so a failure above says which leak came back. The
  // missed double is on the ply with `color: 1`, which is *white* (a ply's
  // colour byte is 1 = white; a game's `winner` byte is the other way round).
  assert(before.white.total_error === 0.25, "the missed double's error is counted at all");
  assert(after.white.total_error === 0.25, "and survives the write");
}

console.log(`\n${passed + failed} tests, ${failed} failures`);
process.exit(failed > 0 ? 1 : 0);
