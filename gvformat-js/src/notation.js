// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Canonical OGXM move-notation rendering. JS mirror of gvformat/notation.py.
//
// The one place every writer renders a checker play to its display string, so
// XG-import, BGF-import, the match analyzer, and reconstruction all agree
// byte-for-byte on the `notation` field:
//   - `bar` for an entry from the bar (never `25`), `off` for a bear-off
//     (never `0`);
//   - hops collapse to a single span (`13/7 7/3` -> `13/3`) unless the checker
//     was hit-and-continued through the intermediate point, which stays split
//     with the `*` on the leg that hit (`8/7* 7/1`);
//   - identical legs group with a repeat count (`4/2 4/2` -> `4/2(2)`).
//
// Faithful port of bgsage.text_export.compute_move_notation. Board convention
// (identical to that function): the mover's own perspective -- index 1..24 are
// signed point counts (positive = mover, negative = opponent), index 25 is the
// mover's bar (non-negative count), borne-off checkers are implicit (derived
// from the drop in the mover's on-board total). Keep in lockstep with
// gvformat/notation.py.

/**
 * Render a checker play from mover-perspective before/after boards. Returns
 * notation like "13/7 8/7", "bar/20*", or "6/off(2)" -- "" for a no-move.
 */
export function canonicalNotation(before, after, die1, die2) {
  const hitPoints = new Set();
  for (let i = 1; i < 25; i++) {
    if (before[i] < 0 && (after[i] >= 0 || after[i] > before[i])) hitPoints.add(i);
  }

  const fromPts = [];
  const toPts = [];

  const barDiff = after[25] - before[25];
  if (barDiff < 0) {
    for (let k = 0; k < -barDiff; k++) fromPts.push(25);
  }

  for (let i = 1; i < 25; i++) {
    let wb = before[i] > 0 ? before[i] : 0;
    let wa = after[i] > 0 ? after[i] : 0;
    if (before[i] < 0 && after[i] > 0) {
      wa = after[i];
      wb = 0;
    } else if (before[i] > 0 && after[i] < 0) {
      wb = before[i];
      wa = 0;
    }
    const diff = wa - wb;
    if (diff > 0) {
      for (let k = 0; k < diff; k++) toPts.push(i);
    } else if (diff < 0) {
      for (let k = 0; k < -diff; k++) fromPts.push(i);
    }
  }

  let onBoardBefore = before[25];
  let onBoardAfter = after[25];
  for (let i = 1; i < 25; i++) {
    if (before[i] > 0) onBoardBefore += before[i];
    if (after[i] > 0) onBoardAfter += after[i];
  }
  const borneOff = onBoardBefore - onBoardAfter;
  for (let k = 0; k < borneOff; k++) toPts.push(0);

  fromPts.sort((a, b) => b - a);
  toPts.sort((a, b) => b - a);

  const dice = die1 === die2 ? [die1, die1, die1, die1] : [die1, die2];
  const moves = [];
  const usedFrom = fromPts.map(() => false);
  const usedTo = toPts.map(() => false);
  const usedDie = dice.map(() => false);

  for (let di = 0; di < dice.length; di++) {
    if (usedDie[di]) continue;
    const d = dice[di];
    for (let fi = 0; fi < fromPts.length; fi++) {
      if (usedFrom[fi]) continue;
      const f = fromPts[fi];
      const expected = f === 25 ? 25 - d : f - d;
      for (let ti = 0; ti < toPts.length; ti++) {
        if (usedTo[ti]) continue;
        const t = toPts[ti];
        if (t === expected || (expected <= 0 && t === 0)) {
          const isHit = hitPoints.has(t);
          if (isHit) hitPoints.delete(t);
          moves.push([f, t, isHit]);
          usedFrom[fi] = usedTo[ti] = usedDie[di] = true;
          break;
        }
        if (usedDie[di]) break;
      }
    }
  }

  for (let fi = 0; fi < fromPts.length; fi++) {
    if (usedFrom[fi]) continue;
    const f = fromPts[fi];
    for (let ti = 0; ti < toPts.length; ti++) {
      if (usedTo[ti]) continue;
      const t = toPts[ti];
      const isHit = hitPoints.has(t);
      if (isHit) hitPoints.delete(t);
      moves.push([f, t, isHit]);
      usedFrom[fi] = usedTo[ti] = true;
      break;
    }
  }

  // Split a span that crosses an intermediate point the checker *hit* on the
  // way through, so the hit shows (e.g. "24/20" with 2-2 through a blot on 22
  // -> "24/22* 22/20"). A clean span (no intermediate hit) stays collapsed.
  if (die1 === die2) {
    const die = die1;
    for (let mi = moves.length - 1; mi >= 0; mi--) {
      const [f, t, h] = moves[mi];
      const dist = f === 25 ? 25 - t : f - t;
      if (dist <= die || dist % die !== 0) continue;
      const nDice = Math.floor(dist / die);
      const hitMids = [];
      for (let i = 1; i < nDice; i++) {
        const mid = f === 25 ? 25 - i * die : f - i * die;
        if (mid >= 1 && mid <= 24 && hitPoints.has(mid)) hitMids.push(mid);
      }
      if (!hitMids.length) continue;
      for (const hm of hitMids) hitPoints.delete(hm);
      const subMoves = [];
      let prev = f;
      for (const hm of hitMids) {
        subMoves.push([prev, hm, true]);
        prev = hm;
      }
      subMoves.push([prev, t, h]);
      moves.splice(mi, 1, ...subMoves);
    }
  } else {
    for (let mi = moves.length - 1; mi >= 0; mi--) {
      const [f, t, h] = moves[mi];
      const dist = f === 25 ? 25 - t : f - t;
      if (dist !== die1 + die2) continue;
      for (const [dA, dB] of [[die1, die2], [die2, die1]]) {
        void dB;
        const mid = f === 25 ? 25 - dA : f - dA;
        if (mid >= 1 && mid <= 24 && hitPoints.has(mid)) {
          hitPoints.delete(mid);
          moves.splice(mi, 1, [f, mid, true], [mid, t, h]);
          break;
        }
      }
    }
  }

  moves.sort((a, b) => (b[0] - a[0]) || (b[1] - a[1]));

  const combined = [];
  for (const [f, t, h] of moves) {
    const last = combined[combined.length - 1];
    if (last && last[0] === f && last[1] === t && last[2] === h) {
      last[3] += 1;
    } else {
      combined.push([f, t, h, 1]);
    }
  }

  const parts = [];
  for (const [f, t, h, count] of combined) {
    const fs = f === 25 ? "bar" : String(f);
    const ts = t === 0 ? "off" : String(t);
    const hs = h ? "*" : "";
    const ms = `${fs}/${ts}${hs}`;
    parts.push(count > 1 ? `${ms}(${count})` : ms);
  }
  return parts.join(" ");
}

/**
 * Convert an XG raw P1-frame board (index 1-24 signed +P1/-P2, index 0 = P1's
 * bar as a positive count, index 25 = P2's bar stored negative) into the
 * mover-perspective frame `canonicalNotation` expects.
 */
export function fromXgP1Frame(board, moverIsP1) {
  const m = new Array(26).fill(0);
  if (moverIsP1) {
    for (let i = 1; i < 25; i++) m[i] = board[i];
    m[25] = board[0]; // P1's own bar (positive count)
  } else {
    for (let i = 1; i < 25; i++) m[i] = -board[25 - i]; // P2 point i at P1 index 25-i
    m[25] = -board[25]; // P2's bar stored negative -> positive count
  }
  return m;
}

/**
 * Convert a BGF absolute board (index 1-24 signed +green/-red, index 25 =
 * green's bar count, index 0 = red's bar count) into the mover-perspective
 * frame `canonicalNotation` expects. `pid` is BGF's raw mover flag (green =
 * -1, red = 1).
 */
export function fromBgfAbsFrame(board, pid) {
  const m = new Array(26).fill(0);
  if (pid === -1) { // green mover (green stored positive)
    for (let i = 1; i < 25; i++) m[i] = board[i];
    m[25] = board[25];
  } else { // red mover; red point i lives at abs 25-i
    for (let i = 1; i < 25; i++) m[i] = -board[25 - i];
    m[25] = board[0];
  }
  return m;
}
