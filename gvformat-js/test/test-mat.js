// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Parity test: convertMat(.mat text) must produce byte-identical OGXM-JSON to
// Python's gvformat.mat_to_ogxm(text) for every sample .mat.
//
// The .mat files come from the repository's shared corpus at samples/mat --
// the same files the Python suite reads, not a copy. (They used to be
// duplicated into gvformat-js/samples/, which drifted and, being covered only
// by a *nested* .gitignore, leaked the pre-anonymization corpus into the
// Python sdist -- hatchling honours the root .gitignore and ignores nested
// ones.)
//
// The Python references beside them are committed under test/fixtures/mat/,
// because they are expectations rather than corpus and JS cannot produce them.
// That means this test actually RUNS in a fresh clone; it used to skip for
// everyone but the author. A .mat with no reference still skips rather than
// fails, so adding a match to the corpus cannot break the suite -- but
// tests/audit_corpus.py flags the missing reference.
//
// Regenerate the references after changing samples/mat (from the repo root):
//
//   uv run python -c "
//   import json, pathlib
//   from gvformat import mat_to_ogxm
//   dst = pathlib.Path('gvformat-js/test/fixtures/mat')
//   dst.mkdir(parents=True, exist_ok=True)
//   for m in sorted(pathlib.Path('samples/mat').glob('*.mat')):
//       out = mat_to_ogxm(m.read_text())
//       (dst / (m.stem + '.json')).write_text(json.dumps(out, separators=(',', ':')) + '\n')
//   "

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { convertMat, convert_mat } from '../src/mat2gva.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MAT_DIR = path.join(__dirname, '..', '..', 'samples', 'mat');
const FIXTURES_DIR = path.join(__dirname, 'fixtures', 'mat');

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

// Deep-equality diff that reports the first mismatching path, for a useful
// failure message instead of a flat "not equal".
function firstDiff(a, b, pathStr = '$') {
  if (a === b) return null;
  const aIsArr = Array.isArray(a), bIsArr = Array.isArray(b);
  if (aIsArr !== bIsArr) return `${pathStr}: array/non-array mismatch (${JSON.stringify(a)} vs ${JSON.stringify(b)})`;
  if (aIsArr && bIsArr) {
    if (a.length !== b.length) return `${pathStr}: length ${a.length} vs ${b.length}`;
    for (let i = 0; i < a.length; i++) {
      const d = firstDiff(a[i], b[i], `${pathStr}[${i}]`);
      if (d) return d;
    }
    return null;
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const aKeys = Object.keys(a).sort();
    const bKeys = Object.keys(b).sort();
    if (aKeys.length !== bKeys.length || aKeys.some((k, i) => k !== bKeys[i])) {
      return `${pathStr}: key sets differ (${JSON.stringify(aKeys)} vs ${JSON.stringify(bKeys)})`;
    }
    for (const k of aKeys) {
      const d = firstDiff(a[k], b[k], `${pathStr}.${k}`);
      if (d) return d;
    }
    return null;
  }
  return `${pathStr}: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`;
}

assert(typeof convert_mat === 'function' && convert_mat === convertMat,
  'convert_mat snake_case alias === convertMat');

// ---------------------------------------------------------------------------
// Resignations (self-contained; these do not need samples)
//
// Written against OpenGammon's export, which says "Resigned Game" where
// Jellyfish says "Resigns" -- a wording the parser did not know, so every
// resigned game came through as an ordinary win on action_id 24.
// ---------------------------------------------------------------------------

/** A one-game .mat with the given turn lines, verbatim. */
function matWith(turns, { matchLength = 7 } = {}) {
  return [
    '; [Player 1 "alice"]',
    '; [Player 2 "bob"]',
    '',
    `${matchLength} point match`,
    '',
    ' Game 1',
    ' alice: 0                                 bob: 0',
    ...turns,
  ].join('\n');
}

/** [end-ply action_id, winner code, points, decision-ply count] of game 1. */
function gameEnd(text) {
  const g = convertMat(text).games[0];
  const isDecision = (p) => p.action_id >= 0 && p.action_id <= 23;
  return [
    g.plies[g.plies.length - 1].action_id,
    g.winner,
    g.points_won,
    g.plies.filter(isDecision).length,
  ];
}

/** The end ply's colour and its OGID on-roll, as "W"/"B" apiece. A ply is white
 *  at color 1; the OGID's field 5 is the *complement* of on-roll, so reading it
 *  back means flipping it. */
function endActor(text) {
  const g = convertMat(text).games[0];
  const end = g.plies[g.plies.length - 1];
  const reached = end.ogid_before.split(':')[4];
  return [end.color === 1 ? 'W' : 'B', reached === 'W' ? 'B' : 'W'];
}

// The OpenGammon shape: the marker lands in the *winner's* column (the layout
// alternates by ply, so the side it falls on says nothing), and the line after
// it carries both the winner and the points.
{
  const [action, winner, points, decisions] = gameEnd(matWith([
    '  1) 31: 8/5 6/5                              42: 8/4 6/4',
    '  2)                                          Resigned Game',
    '                                            Wins 2 points',
  ]));
  assert(action === 27, `"Resigned Game" ends the game on action 27, got ${action}`);
  assert(winner === 1, `the "Wins" line names the winner, not the column (got code ${winner})`);
  assert(points === 2, `and carries the points a resignation cannot derive (got ${points})`);
  assert(decisions === 2, `the resignation is not a decision ply (got ${decisions})`);
}

// A resignation is the one terminal ply with an actor, and it is the loser:
// nobody resigns a game they won. Every other end marker (24/26/29) has no
// actor and keeps the winner, which is what the plain-win case below checks.
{
  const resigned = matWith([
    '  1) 31: 8/5 6/5                              42: 8/4 6/4',
    '  2)                                          Resigned Game',
    '                                            Wins 2 points',
  ]);
  const [, winner] = gameEnd(resigned);
  const [color, onRoll] = endActor(resigned);
  // winner code 1 is bob (alice is canonical white), so alice resigned.
  assert(winner === 1, `precondition: bob won (got code ${winner})`);
  assert(color === 'W', `the resign ply belongs to the resigner, got ${color}`);
  assert(onRoll === 'W',
    `and the resigner is the one on roll, facing the roll they declined, got ${onRoll}`);
}

// "Resigned Match" is the same marker; the ply becomes 28 by completing the
// match, which is the existing rule and not a second wording to know.
{
  const [action] = gameEnd(matWith([
    '  1) 31: 8/5 6/5                              42: 8/4 6/4',
    '  2)                                          Resigned Match',
    '                                            Wins 2 points and the match',
  ], { matchLength: 2 }));
  assert(action === 28, `a match-ending resignation is action 28, got ${action}`);
}

// Jellyfish's wording, and no "Wins" line after it: the column is all there is
// to go on, and it means the resigner.
{
  const [action, winner, points] = gameEnd(matWith([
    '  1) 31: 8/5 6/5                              42: 8/4 6/4',
    '  2) Resigns',
  ]));
  assert(action === 27, `"Resigns" alone still ends on action 27, got ${action}`);
  // alice is canonical white (code 0), so code 1 is bob -- the other column.
  assert(winner === 1, `with no "Wins" line, the other column wins (got code ${winner})`);
  assert(points === 0, `and the points are unknown, so zero (got ${points})`);
}

// "???" stands where a play should be. The board cannot be carried past it, so
// reconstruction stops there even though a later line would have said more.
{
  // The trailing "Wins" line is in alice's column, so reading it would name
  // alice (code 0) for one point. Stopping at "???" names bob for zero.
  const [, winner, points, decisions] = gameEnd(matWith([
    '  1) 31: 8/5 6/5                              42: 8/4 6/4',
    '  2) 51: ???                                  32: 6/3 6/4',
    '  3) 21: 6/4 6/5                              11: 6/5 6/5 5/4 5/4',
    '   Wins 1 points',
  ]));
  assert(decisions === 2, `nothing is replayed past "???" (got ${decisions} decisions)`);
  assert(winner === 1 && points === 0,
    `and the later "Wins" line is not read either (got code ${winner}, ${points} points)`);
}

// A plain win is untouched by any of the above.
{
  const [action, winner, points] = gameEnd(matWith([
    '  1) 31: 8/5 6/5                              42: 8/4 6/4',
    '  2) 65: 13/7 13/8                            Wins 1 points',
  ]));
  assert(action === 24, `an ordinary win still ends on action 24, got ${action}`);
  assert(winner === 1 && points === 1, 'and still reads its winner and points');
  // ...and, having no actor, still carries the winner rather than the loser.
  const [color] = endActor(matWith([
    '  1) 31: 8/5 6/5                              42: 8/4 6/4',
    '  2) 65: 13/7 13/8                            Wins 1 points',
  ]));
  assert(color === 'B', `a game-over marker still names the winner, got ${color}`);
}

// The corpus at samples/ is committed, so this normally runs; the guard is
// for a partial checkout (same convention as the Python tests, which skip on
// a missing fixture rather than failing).
if (!fs.existsSync(MAT_DIR)) {
  console.log(`SKIP: no sample .mat files at ${MAT_DIR}`);
  console.log(`\n${passed + failed} tests, ${failed} failures`);
  process.exit(failed > 0 ? 1 : 0);
}

const matFiles = fs.readdirSync(MAT_DIR).filter(f => f.endsWith('.mat')).sort();
if (matFiles.length === 0) {
  console.log(`SKIP: no sample .mat files at ${MAT_DIR}`);
  console.log(`\n${passed + failed} tests, ${failed} failures`);
  process.exit(failed > 0 ? 1 : 0);
}
console.log(`Comparing ${matFiles.length} sample .mat file(s) against the Python reference`);

for (const matFile of matFiles) {
  const stem = matFile.replace(/\.mat$/, '');
  const fixturePath = path.join(FIXTURES_DIR, `${stem}.json`);
  if (!fs.existsSync(fixturePath)) {
    // A .mat with no Python reference beside it: nothing to compare against
    // (the references are generated locally -- see the header). Skip, don't
    // fail, so dropping extra .mat files into samples/ can't break the suite.
    console.log(`SKIP  ${stem}: no Python reference (${stem}.json) beside it`);
    continue;
  }

  const text = fs.readFileSync(path.join(MAT_DIR, matFile), 'utf-8');
  const expected = JSON.parse(fs.readFileSync(fixturePath, 'utf-8'));

  let actual;
  try {
    actual = convertMat(text);
  } catch (e) {
    failed++;
    console.error(`FAIL  ${stem}: convertMat threw: ${e.stack || e}`);
    continue;
  }

  // Round-trip through JSON so `undefined`-valued keys (which JSON.stringify
  // drops, matching Python's json.dumps of an absent key) don't spuriously
  // register as a mismatch against the parsed fixture.
  const actualNormalized = JSON.parse(JSON.stringify(actual));

  const diff = firstDiff(expected, actualNormalized);
  assert(diff === null, `${stem}: matches Python reference` + (diff ? ` -- first diff at ${diff}` : ''));
}

console.log(`\n${passed + failed} tests, ${failed} failures`);
process.exit(failed > 0 ? 1 : 0);
