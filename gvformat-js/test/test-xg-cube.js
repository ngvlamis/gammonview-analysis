// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// XG embedded cube classifier tests (mirror of tests/test_xg_cube.py)

import { embeddedCube, trivialCube } from '../src/xg2gva.js';

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) { passed++; console.log(`OK  ${msg}`); }
  else { failed++; console.error(`FAIL  ${msg}`); }
}

// Missed double, opponent takes: loss = min(dt,dp) - nd = dt - nd.
{
  const r = embeddedCube(0.6669, 0.7358, 1.0);
  assert(r.key === "missed_double" && Math.abs(r.sub.equity_loss - 0.0689) < 1e-9,
    "missed double (take) -> 0.0689 loss");
}

// Missed double, opponent passes (dt > dp): loss CAPPED at dp.
{
  const r = embeddedCube(0.925, 1.2652, 1.0);
  assert(r.key === "missed_double" && Math.abs(r.sub.equity_loss - 0.075) < 1e-9,
    "missed double (pass) -> 0.075 loss (capped, not dt-nd=0.3402)");
}

// Deep double/pass missed double (hid under a doubleChoice==-1 sentinel).
{
  const r = embeddedCube(0.674, 1.034, 1.0);
  assert(r.key === "missed_double" && Math.abs(r.sub.equity_loss - 0.326) < 1e-9,
    "deep missed double (pass) -> 0.326 loss");
}

// Correct no-double, non-trivial: cube_decision with decision=true.
{
  const r = embeddedCube(0.3782, 0.2324, 1.0);
  assert(r.key === "cube_decision" && r.sub.decision === true && r.sub.equity_loss === 0.0,
    "correct no-double (close) -> cube_decision, decision=true");
}

// Sentinel-immunity: XG doubleChoice==-1 row; equities say no-double.
{
  const r = embeddedCube(-0.0813, -0.4848, 1.0);
  assert(r.key === "cube_decision" && r.sub.decision === false,
    "sentinel row -> cube_decision (not false missed double), trivial");
}

// Boundary: best == nd exactly -> not a missed double (and trivial).
{
  const r = embeddedCube(0.5, 0.5, 1.0);
  assert(r.key === "cube_decision" && r.sub.decision === false,
    "best == nd -> cube_decision, decision=false");
}

// Unanalyzed row (drop equity 0) -> skipped entirely.
assert(embeddedCube(0.0, 0.0, 0.0) === null, "unanalyzed row (dp=0) -> null");

// trivialCube spot checks.
assert(trivialCube(0.5, 0.5, 1.0) === true, "trivial: nd == min(dt,dp)");
assert(trivialCube(0.9, -1.5, 1.0) === true, "trivial: nd - dt > 0.2");
assert(trivialCube(0.3782, 0.2324, 1.0) === false, "non-trivial close cube");

console.log(`\n${passed + failed} tests, ${failed} failures`);
process.exit(failed > 0 ? 1 : 0);
