// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// MET tests. The values below are printed by the Python gvformat.met, not
// derived here -- the point of the file is that the two copies of the table and
// its conversions cannot drift apart silently.

import { eq2mwc, mwc2eq, scoreMwc } from '../src/met.js';

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

// Test 1: scoreMwc against Python, across the three regimes
{
  const cases = [
    [3, 5, false, 0.64795],   // ordinary, leader on roll
    [5, 3, false, 0.35205],   // its mirror
    [5, 5, false, 0.5],       // symmetric
    [1, 5, true, 0.84179],    // Crawford: the pre-Crawford table's 1-away row
    [1, 5, false, 0.80988],   // post-Crawford: cube live, so worth less
    [2, 7, false, 0.84225],
    [0, 0, false, 0.5],       // money
  ];
  for (const [a1, a2, cr, want] of cases) {
    assertClose(scoreMwc(a1, a2, cr), want, 1e-9,
                `scoreMwc(${a1}, ${a2}, ${cr})`);
  }
}

// Test 2: mwc2eq inverts eq2mwc
{
  for (const [a1, a2, cr] of [[3, 5, false], [5, 3, false], [7, 2, false], [1, 4, true]]) {
    for (const cube of [1, 2, 4]) {
      for (const eq of [-1.5, -0.3, 0.0, 0.25, 1.0]) {
        const back = mwc2eq(eq2mwc(eq, a1, a2, cube, cr), a1, a2, cube, cr);
        assertClose(back, eq, 1e-9,
                    `round trip ${eq} at ${a1}a-${a2}a cube ${cube}${cr ? ' crawford' : ''}`);
      }
    }
  }
}

// Test 3: the opening-roll baseline, which is scoreMwc pushed through mwc2eq.
// A symmetric score must give exactly 0.0 -- the anchors are complementary, so
// their midpoint is 0.5, which is also the pre-game MWC.
{
  const baseline = (a1, a2, cr = false) => mwc2eq(scoreMwc(a1, a2, cr), a1, a2, 1, cr);

  for (const a of [1, 2, 3, 5, 7, 11]) {
    assert(baseline(a, a) === 0.0, `baseline at ${a}a-${a}a is exactly 0`);
  }
  assert(baseline(0, 0) === 0.0, 'baseline for money is exactly 0');

  assertClose(baseline(3, 5), -0.111511, 1e-6, 'baseline 3a-5a');
  assertClose(baseline(5, 3), +0.111511, 1e-6, 'baseline 5a-3a (mirror)');
  assertClose(baseline(1, 5, true), -0.020644, 1e-6, 'baseline 1a-5a Crawford');
  assertClose(baseline(1, 5, false), -0.226502, 1e-6, 'baseline 1a-5a post-Crawford');
  assertClose(baseline(2, 7), -0.205304, 1e-6, 'baseline 2a-7a');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
