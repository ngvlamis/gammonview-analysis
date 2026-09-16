// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Share-codec test — JS mirror of tests/test_share_link.py. Keep the two in sync.
//
// encode_match/decode_match carry a whole match through a URL-safe string:
// OGXM -> .gvab -> zlib-deflate -> base64url. They must be exact inverses, the
// payload must be URL-safe and unpadded, and the payload must inflate via the
// stdlib zlib path (so a JS link decodes in Python and vice versa).

import { encode_match, decode_match } from '../src/share.js';
import { write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { inflate } from 'pako';

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

// A realistic match with checker analysis, alternatives, and luck. Canonicalize
// it through the codec first (readGvab(write_gvab(...))), so decode_match's
// output is compared against the same normalized form it round-trips to.
const raw = {
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
        },
      },
      { color: 1, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 2 }, { from: 13, pips: 3 }] },
    ],
  }],
};
const match = readGvab(write_gvab(raw));

// 1. encode -> decode is an exact inverse.
{
  const back = decode_match(encode_match(match));
  assert(JSON.stringify(back) === JSON.stringify(match),
    "encode -> decode round-trips unchanged");
}

// 2. payload is URL-safe and unpadded base64url.
{
  const payload = encode_match(match);
  assert(/^[A-Za-z0-9\-_]+$/.test(payload), "payload is URL-safe base64url");
  assert(!payload.endsWith("="), "payload has no `=` padding");
}

// 3. payload inflates via the plain zlib path to write_gvab(match) — the
//    interop guarantee with Python (zlib.decompress) and read_gvab.
{
  const payload = encode_match(match);
  const b64 = payload.replace(/-/g, "+").replace(/_/g, "/");
  const raw2 = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const bytes = inflate(raw2);
  assert(bytesEqual(bytes, write_gvab(match)),
    "payload inflates to write_gvab(match)");
}

// 4. a corrupt payload throws rather than returning garbage.
{
  let threw = false;
  try { decode_match("@@@not-base64@@@"); } catch { threw = true; }
  assert(threw, "corrupt payload throws");
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
