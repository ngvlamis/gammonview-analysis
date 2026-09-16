// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// event / site: the pair travels as two independent MHDR strings, and a file
// written before they were split (one combined "Event • Site" string in the
// `event` slot, no `site` at all) still decodes into the same pair.
//
// Mirrors the "2b. Legacy combined event string" section of the Python
// tests/test_read_gvab.py.

import { write_gvab } from '../src/binary.js';
import { readGvab } from '../src/reader.js';
import { PLACE_SEPARATOR, splitPlace, joinPlace, cleanPlace } from '../src/place.js';

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
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function baseMatch(extra) {
  return {
    match_length: 5,
    player_white: 'Alice',
    player_black: 'Bob',
    white_score: 0,
    black_score: 0,
    result: 0,
    source: 0,
    timestamp: 0,
    games: [{
      game_index: 0, winner: 0, points_won: 0, is_crawford: false,
      is_lastgame: true, first_to_move: 0, plies: [],
    }],
    ...extra,
  };
}

// --- The pair round-trips as two separate fields -------------------------
{
  const doc = baseMatch({ event: 'Spring League', site: 'Heroes Lounge' });
  const back = readGvab(write_gvab(doc));
  assert(back.event === 'Spring League', 'event round-trips');
  assert(back.site === 'Heroes Lounge', 'site round-trips');
}

// Either half alone stays in its own field -- neither is inferred from the
// other, so a match with only a site does not report a bogus event.
{
  const onlyEvent = readGvab(write_gvab(baseMatch({ event: 'Spring League' })));
  assert(onlyEvent.event === 'Spring League' && onlyEvent.site === null,
    'an event with no site keeps site null');

  const onlySite = readGvab(write_gvab(baseMatch({ site: 'Heroes Lounge' })));
  assert(onlySite.event === null && onlySite.site === 'Heroes Lounge',
    'a site with no event keeps event null');
}

// --- Legacy files: one combined string, no site field --------------------
{
  const legacy = baseMatch({ event: `Spring League${PLACE_SEPARATOR}Heroes Lounge` });
  const back = readGvab(write_gvab(legacy));
  assert(back.event === 'Spring League' && back.site === 'Heroes Lounge',
    'a legacy combined event string splits into the pair');

  // No separator -> all event. Guessing "site" would move data between two
  // columns that mean different things.
  const unsplit = readGvab(write_gvab(baseMatch({ event: 'Just An Event' })));
  assert(unsplit.event === 'Just An Event' && unsplit.site === null,
    'an unseparated legacy string stays entirely in event');

  // Healing is a one-time fixup: re-encoding the decoded doc is stable.
  const once = write_gvab(back);
  const twice = write_gvab(readGvab(once));
  assert(uint8Equal(once, twice), 'the healed document is a write/read fixed point');
}

// --- splitPlace / joinPlace units ----------------------------------------
{
  assert(JSON.stringify(splitPlace('Cup • Monte Carlo'))
    === JSON.stringify({ event: 'Cup', site: 'Monte Carlo' }), 'splitPlace splits on the separator');
  assert(JSON.stringify(splitPlace('Cup'))
    === JSON.stringify({ event: 'Cup', site: null }), 'splitPlace leaves an unseparated string as event');
  assert(JSON.stringify(splitPlace(''))
    === JSON.stringify({ event: null, site: null }), 'splitPlace on empty gives nulls');
  assert(JSON.stringify(splitPlace(null))
    === JSON.stringify({ event: null, site: null }), 'splitPlace on null gives nulls');
  // Only the first separator splits, so a site keeps one of its own.
  assert(JSON.stringify(splitPlace(`Cup${PLACE_SEPARATOR}A${PLACE_SEPARATOR}B`))
    === JSON.stringify({ event: 'Cup', site: `A${PLACE_SEPARATOR}B` }),
    'only the first separator splits');

  assert(joinPlace('Cup', 'Monte Carlo') === 'Cup • Monte Carlo', 'joinPlace joins both');
  assert(joinPlace('Cup', null) === 'Cup', 'joinPlace with one half returns it alone');
  assert(joinPlace(null, 'Monte Carlo') === 'Monte Carlo', 'joinPlace with only a site returns it');
  assert(joinPlace(null, null) === null, 'joinPlace with neither returns null');
  assert(joinPlace('  Cup  ', '  Monte Carlo  ') === 'Cup • Monte Carlo', 'joinPlace trims');

  assert(cleanPlace('  x  ') === 'x' && cleanPlace('   ') === null && cleanPlace(5) === null,
    'cleanPlace trims, blanks to null, ignores non-strings');

  // splitPlace and joinPlace are inverses on a well-formed pair.
  const pair = splitPlace(joinPlace('Cup', 'Monte Carlo'));
  assert(pair.event === 'Cup' && pair.site === 'Monte Carlo', 'split(join(x)) === x');
}

console.log(`\n${passed + failed} tests, ${failed} failures`);
process.exit(failed > 0 ? 1 : 0);
