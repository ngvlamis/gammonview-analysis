// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// BGF embedded-cube routing: which key a live cube on a checker ply lands in.
//
// Regression. `embeddedCubeAnalysis` builds a missed_double whenever the
// optimal action is to double, which `stateAction` says of both `DOUBLE` and
// `RE_DOUBLE`. Its caller used to re-derive the key from the state and matched
// only `"DOUBLE"`, so a missed *re*double was filed under `cube_decision`.
//
// That key's `equity_loss` is zero by definition on disk -- it maps to CUBE
// type=4, where a correct no-double costs nothing and there is no field for a
// loss. So the error was real in memory, showed correctly in the viewer, and
// vanished the moment the match was written to a `.gvab`: one real 13-point
// BGBlitz match read PR 7.63 on screen and 5.99 after saving.
//
// The fix is structural -- the builder returns `[key, sub]`, so there is no
// second copy of the rule to get wrong. These checks pin the pairing.

import { embeddedCubeAnalysis } from '../src/bgf2gva.js';

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

// Money-frame normalization params (emg=0, meq=0, eqDoublePass=1.0) make the
// normalization the identity: eqDt == raw eqDoubleTake, eqDp == 1.0.
const EQ_FULL = { emg: 0.0, matchEquity: 0.0 };

// A live cube on a checker ply: `hasDoubled` absent is what makes it embedded.
const cd = (stateOnMove, eqNoDouble, eqDoubleTake) => ({
  eqNoDouble,
  eqDoublePass: 1.0,
  eqDoubleTake,
  stateOnMove,
});

// A missed double: should have doubled, didn't. Loss = min(dt, dp) - nd.
{
  const [key, sub] = embeddedCubeAnalysis(cd("DOUBLE", 0.55, 0.8), EQ_FULL, true, null, true);
  assert(key === "missed_double", "a missed double is keyed missed_double");
  assert(Math.abs(sub.equity_loss - 0.25) < 1e-9, "and carries its 0.25 loss");
}

// A missed RE-double: the case that broke. Same shape, same loss, same key.
{
  const [key, sub] = embeddedCubeAnalysis(cd("RE_DOUBLE", 0.55, 0.8), EQ_FULL, true, null, true);
  assert(key === "missed_double", "a missed REdouble is keyed missed_double, not cube_decision");
  assert(Math.abs(sub.equity_loss - 0.25) < 1e-9,
    "and keeps its 0.25 loss, which cube_decision would have zeroed");
  assert(sub.should_double === undefined,
    "it is a missed-double object, not a cube_decision wearing the wrong key");
}

// A correct no-double: the other key, and no loss to lose.
{
  const [key, sub] = embeddedCubeAnalysis(cd("NO_DOUBLE", 0.55, 0.8), EQ_FULL, true, null, true);
  assert(key === "cube_decision", "a correct no-double is keyed cube_decision");
  assert(sub.equity_loss === 0.0,
    "and carries no error, which is the only value that key can store");
}

// BGBlitz's counted-ness rides through on the cube_decision branch, which maps
// to CUBE type=4 and has a decision bit in GVAN to hold it. The missed_double
// branch deliberately carries none: CUBE type=2's single bit cannot express
// "the source said nothing", so a stored value would not survive a round trip
// intact and readers derive it instead.
{
  const [, held] = embeddedCubeAnalysis(cd("NO_DOUBLE", 0.55, 0.8), EQ_FULL, true, null, true);
  assert(held.decision === true, "a counted no-double stays counted");
  const [, unheld] = embeddedCubeAnalysis(cd("NO_DOUBLE", 0.55, 0.8), EQ_FULL, true, null, false);
  assert(unheld.decision === false, "an uncounted no-double stays uncounted");
  const [, missed] = embeddedCubeAnalysis(cd("RE_DOUBLE", 0.55, 0.8), EQ_FULL, true, null, false);
  assert(!("decision" in missed),
    "a missed double carries no decision flag -- the format cannot keep one");
}

// Nothing to build: no cube state, or the ply is an actual double (hasDoubled
// set), which is a standalone cube ply rather than an embedded decision.
{
  assert(embeddedCubeAnalysis({}, EQ_FULL, true, null, true) === null,
    "no cube state -> nothing to attach");
  assert(embeddedCubeAnalysis(
    { ...cd("DOUBLE", 0.55, 0.8), hasDoubled: true }, EQ_FULL, true, null, true) === null,
    "an actual double is not an embedded cube decision");
}

console.log(`\n${passed + failed} tests, ${failed} failures`);
process.exit(failed > 0 ? 1 : 0);
