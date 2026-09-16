// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Robustness test: readGvab's error contract, and src/ import-safety.
//
// `readGvab` is handed untrusted files -- GammonView reads whatever the user
// drops, and its analysis service parses every upload with the Python twin -- so
// "not well-formed" has to arrive as a `GvabError` and nothing else. JS makes
// that easy to get wrong in a way Python does not: a typed array reads
// `undefined` past its end instead of throwing (`undefined & 0x1F` is 0, a
// perfectly valid-looking action id), and writing `mb[-3]` sets a stray property
// rather than raising. Both used to happen here, producing a silently wrong
// board instead of an error.
//
// Mirrors tests/test_read_gvab.py sections 3b and 5 in the Python repo.

import { write_gvab } from '../src/binary.js';
import { readGvab, GvabError, _applyMovesP1 } from '../src/reader.js';
import { VERSION_MAJOR, VERSION_MINOR } from '../src/constants.js';

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

const SAMPLE = {
  match_length: 7,
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
    points_won: 2,
    is_crawford: false,
    is_lastgame: false,
    first_to_move: 0,
    plies: [
      { color: 0, action_id: 1, d1: 1, d2: 2, moves: [{ from: 24, pips: 1 }] },
      { color: 1, action_id: 6, d1: 2, d2: 3, moves: [{ from: 13, pips: 2 }, { from: 13, pips: 3 }] },
      { color: 0, action_id: 21 },
      { color: 1, action_id: 22 },
    ],
  }],
};

// --- 1. Every malformed stream raises GvabError -----------------------------
{
  const good = write_gvab(SAMPLE);

  // Deterministic PRNG: a flaky fuzz test is worse than no fuzz test.
  let seed = 20260801;
  const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  const ri = (n) => Math.floor(rnd() * n);

  const leaked = [];
  let accepted = 0;
  for (let i = 0; i < 4000; i++) {
    let b = good.slice();
    const nmut = 1 + ri(12);
    for (let k = 0; k < nmut; k++) b[ri(b.length)] = ri(256);
    const r = rnd();
    if (r < 0.25) b = b.subarray(0, ri(b.length));
    else if (r < 0.35) b = new Uint8Array([...b, ...new Uint8Array(1 + ri(63))]);

    try {
      readGvab(b);
      accepted++;
    } catch (e) {
      if (e.name !== 'GvabError') leaked.push(`${e.name}: ${e.message}`);
    }
  }
  assert(leaked.length === 0, `4000 mutated streams raise only GvabError (leaked ${leaked.length}, ${accepted} still parsed)`);
  for (const msg of leaked.slice(0, 5)) console.error(`      leaked: ${msg}`);
}

// --- 2. A truncated ply record is an error, not a phantom ply ---------------
{
  const good = write_gvab(SAMPLE);
  // Lop off the tail. Something inside a GAME chunk now runs past its end.
  let threw = null;
  try {
    readGvab(good.subarray(0, good.length - 40));
  } catch (e) {
    threw = e;
  }
  assert(threw instanceof GvabError, "a truncated stream throws GvabError");
}

// --- 3. An out-of-board `from` is rejected, not silently applied ------------
{
  let threw = null;
  try {
    _applyMovesP1(new Array(26).fill(0), [{ from: 40, pips: 3 }], true);
  } catch (e) {
    threw = e;
  }
  assert(threw instanceof GvabError, "a move from a point outside the board throws GvabError");
}

// --- 3b. min_reader version guard -------------------------------------------
//
// The header states the minimum spec version a reader needs. A file demanding
// more than we implement must fail as a clean version mismatch rather than be
// parsed optimistically at the wrong layout -- which is exactly how a pre-GVAN-
// v3 build misreads a v3 file: silent garbage, no error.
//
// verifyCrc:false throughout, since patching the header invalidates the CSUM
// and the checksum is not what is under test.
{
  const withMinReader = (raw, maj, min) => {
    const b = Uint8Array.from(raw);
    const view = new DataView(b.buffer, b.byteOffset, b.byteLength);
    view.setUint16(8, maj, true);
    view.setUint16(10, min, true);
    return b;
  };

  const base = write_gvab(SAMPLE);
  const baseView = new DataView(base.buffer, base.byteOffset, base.byteLength);
  const hdrMaj = baseView.getUint16(8, true);
  const hdrMin = baseView.getUint16(10, true);
  assert(hdrMaj < VERSION_MAJOR || (hdrMaj === VERSION_MAJOR && hdrMin <= VERSION_MINOR),
    `we write a min_reader we can read (${hdrMaj}.${hdrMin})`);

  for (const [maj, min, label] of [[VERSION_MAJOR, VERSION_MINOR + 1, 'a later minor'],
                                   [VERSION_MAJOR + 1, 0, 'a later major']]) {
    let msg = '';
    try {
      readGvab(withMinReader(base, maj, min), { verifyCrc: false });
    } catch (e) {
      if (e instanceof GvabError) msg = e.message;
      else throw e;
    }
    assert(msg.includes('requires an OGXM'),
      `${label} (${maj}.${min}) is refused: ${msg || 'no error!'}`);
  }

  // The boundary is >, not >=.
  let okExact = true;
  try {
    readGvab(withMinReader(base, VERSION_MAJOR, VERSION_MINOR), { verifyCrc: false });
  } catch (e) {
    okExact = false;
  }
  assert(okExact, 'min_reader == our version still reads');

  let okOlder = true;
  try {
    readGvab(withMinReader(base, 1, 0), { verifyCrc: false });
  } catch (e) {
    okOlder = false;
  }
  assert(okOlder, 'an older min_reader still reads');

  // The check fires before the file_size compare, so a zeroed size (which the
  // reader would otherwise skip over) cannot talk us past it.
  const tooNew = withMinReader(base, VERSION_MAJOR, VERSION_MINOR + 1);
  new DataView(tooNew.buffer, tooNew.byteOffset, tooNew.byteLength).setUint32(12, 0, true);
  let msg = '';
  try {
    readGvab(tooNew, { verifyCrc: false });
  } catch (e) {
    if (e instanceof GvabError) msg = e.message;
    else throw e;
  }
  assert(msg.includes('requires an OGXM'), 'refused even with no file_size to check');
}

// --- 4. src/ stays free of Node-only / CommonJS-hostile code ----------------
//
// src/ is a browser library. Two things must never leak back into it, both of
// which did once and both of which break a *consumer's* build, not ours:
//
//   `import.meta`  -- a syntax error the moment a toolchain rewrites a module
//                     to CommonJS (Babel under Jest), so importing the module
//                     explodes at parse time before any consumer code runs.
//   `node:*`       -- resolves nowhere in a browser; a bundler has to stub it
//                     and warns ("externalized for browser compatibility").
//
// Both belong to the CLIs, which live in cli/ (see cli/README.md).
//
// A third thing must never leak in, for a different reason: an **absolute URL
// import**. xg2gva.js once fell back to a jsdelivr CDN when `import("pako")`
// failed, which gave a published package an undeclared runtime dependency on a
// third party, on an unpinned major. Dependencies are declared in package.json
// and resolved by the consumer; a module that fetches its own is not one this
// package ships.
{
  const fs = await import('node:fs');
  const path = await import('node:path');
  const url = await import('node:url');

  const srcDir = path.join(path.dirname(url.fileURLToPath(import.meta.url)), '..', 'src');
  const srcFiles = fs.readdirSync(srcDir).filter((f) => f.endsWith('.js'));
  const read = (f) => fs.readFileSync(path.join(srcDir, f), 'utf8');

  const metaUsers = srcFiles.filter((f) => /import\s*\.\s*meta/.test(read(f)));
  assert(metaUsers.length === 0, `no src/ module uses import.meta (found: ${metaUsers.join(', ') || 'none'})`);

  const nodeUsers = srcFiles.filter((f) => /["']node:[a-z/]+["']/.test(read(f)));
  assert(nodeUsers.length === 0, `no src/ module imports a node: builtin (found: ${nodeUsers.join(', ') || 'none'})`);

  const urlImporters = srcFiles.filter((f) => /(?:\bfrom|\bimport)\s*\(?\s*["']https?:\/\//.test(read(f)));
  assert(urlImporters.length === 0, `no src/ module imports an absolute URL (found: ${urlImporters.join(', ') || 'none'})`);
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
