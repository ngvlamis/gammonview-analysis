// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

//
// Rules-based backgammon move legality, independent of any import format.
// JS mirror of gvformat/legality.py — keep the two in sync.
//
// Importers disagree about how (or whether) they flag a rules violation the
// player actually made: XG has an explicit `invalidM === 2` flag, while
// BGBlitz has no flag at all — it records the real move at a move record's
// top level and lists only *legal* plays in its analysis candidates. Guessing
// from candidate lists is unsound because engines cap how many candidates
// they store (BGBlitz keeps 8), so a merely-bad legal play looks identical to
// an illegal one.
//
// Board convention (mover-relative, the natural frame for legality):
//   - `mine[p]` / `opp[p]` are non-negative counts, p in 1..24, where 1 is the
//     mover's ace point and 24 the mover's entry point.
//   - Index 25 is the bar: `mine[25]` is the mover's own bar count.
//   - Checkers borne off are not tracked; a play's `to` of 0 means "off".
//
// A "play" is the whole turn: a list of [from, to] pairs, sorted, with to === 0
// meaning borne off. Sorting makes the comparison independent of the order an
// importer happened to record the sub-moves in.

const BAR = 25;

/**
 * Reasons `board` could not be a real backgammon position, or [].
 *
 * Takes the *signed* 26-slot mover frame the engines use (not the mine/opp
 * pair the rest of this module works in): points 1-24 signed +mover/-opponent,
 * index 25 the mover's bar, index 0 the opponent's. Both bars are plain
 * counts, unsigned — summing sign across all 26 slots credits the opponent's
 * bar to the mover, which reads a legal position as one where the mover has 17
 * checkers.
 *
 * This catches what a *board* cannot be (16 checkers on a side, 20 on a
 * point), not what a *play* may not do. It is the shape of corruption a replay
 * produces when it lifts a checker off a point that has none — and the shape
 * that makes an engine walk off the end of a bearoff table rather than fail,
 * so anything handing boards to one wants this first.
 */
export function boardProblems(board) {
  const problems = [];
  const points = board.slice(1, 25);
  const mover = points.filter(c => c > 0).reduce((a, c) => a + c, 0) + board[25];
  const opponent = -points.filter(c => c < 0).reduce((a, c) => a + c, 0) + board[0];
  for (const [label, count] of [['mover', mover], ['opponent', opponent]]) {
    if (count > 15) problems.push(`the ${label} has ${count} checkers (15 maximum)`);
  }
  board.forEach((count, i) => {
    if (Math.abs(count) > 15) problems.push(`point ${i} holds ${Math.abs(count)} checkers`);
  });
  return problems;
}

/** Canonicalise [from, to] pairs: drop empties, bear-off -> 0, sort. Returns a key string. */
export function normalizePlay(pairs) {
  const out = [];
  for (const [f, t] of pairs) {
    if (f === null || f === undefined || Number(f) < 0) continue;
    const ti = t !== null && t !== undefined && Number(t) > 0 ? Number(t) : 0;
    out.push([Number(f), ti]);
  }
  out.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return out.map((p) => `${p[0]}/${p[1]}`).join(",");
}

/** True when every mover checker sits on points 1..6 (none on bar or 7+). */
function allHome(mine) {
  if (mine[BAR]) return false;
  for (let p = 7; p <= 24; p++) if (mine[p]) return false;
  return true;
}

/** Legal single-die sub-moves. `to === 0` means bearing off. */
function submoves(mine, opp, die) {
  // On the bar, entering is the only thing allowed.
  if (mine[BAR] > 0) {
    const t = 25 - die;
    if (t >= 1 && t <= 24 && opp[t] <= 1) return [[BAR, t]];
    return [];
  }

  const out = [];
  let home = null; // computed lazily; only bear-offs need it
  for (let p = 1; p <= 24; p++) {
    if (!mine[p]) continue;
    const t = p - die;
    if (t >= 1) {
      if (opp[t] <= 1) out.push([p, t]);
      continue;
    }
    // t <= 0: bearing off, only once everything is home
    if (home === null) home = allHome(mine);
    if (!home) continue;
    if (t === 0) {
      out.push([p, 0]);
    } else {
      // Overshoot is legal only from the highest occupied point.
      let higher = false;
      for (let q = p + 1; q <= 6; q++) if (mine[q]) { higher = true; break; }
      if (!higher) out.push([p, 0]);
    }
  }
  return out;
}

/** Return new [mine, opp] with sub-move f -> t applied (hits removed). */
function applySub(mine, opp, f, t) {
  const m = mine.slice();
  const o = opp.slice();
  m[f] -= 1;
  if (t >= 1) {
    m[t] += 1;
    if (o[t] === 1) o[t] = 0; // blot hit: the point is now the mover's
  }
  return [m, o];
}

/** Depth-first over remaining dice, collecting maximal sequences. */
function extend(mine, opp, dice, acc, results) {
  let extended = false;
  const seen = new Set();
  for (let i = 0; i < dice.length; i++) {
    const die = dice[i];
    for (const mv of submoves(mine, opp, die)) {
      // Identical dice produce identical branches; explore each once.
      const key = `${die}:${mv[0]}/${mv[1]}`;
      if (seen.has(key)) continue;
      seen.add(key);
      extended = true;
      const [m2, o2] = applySub(mine, opp, mv[0], mv[1]);
      const rest = dice.slice(0, i).concat(dice.slice(i + 1));
      extend(m2, o2, rest, acc.concat([[mv, die]]), results);
    }
  }
  if (!extended) results.push(acc);
}

/**
 * All legal plays for this roll, plus how many dice a legal play must use.
 *
 * Enforces the two maximisation rules: a player must play as many dice as
 * possible, and when a non-double allows only one die, the larger one must be
 * played if either alone is playable.
 *
 * @returns {{plays: Set<string>, diceUsed: number}} plays are normalizePlay keys.
 */
export function legalPlays(mine, opp, d1, d2) {
  const results = [];
  const dice = d1 === d2 ? [d1, d1, d1, d1] : [d1, d2];
  extend(mine, opp, dice, [], results);
  if (!results.length) return { plays: new Set(), diceUsed: 0 };

  let longest = 0;
  for (const s of results) if (s.length > longest) longest = s.length;
  if (longest === 0) return { plays: new Set(), diceUsed: 0 };

  let best = results.filter((s) => s.length === longest);

  // Non-double that can only ever play one die: the larger die wins out.
  if (d1 !== d2 && longest === 1) {
    const high = Math.max(d1, d2);
    const highPlays = best.filter((s) => s[0][1] === high);
    if (highPlays.length) best = highPlays;
  }

  const plays = new Set(best.map((s) => normalizePlay(s.map(([mv]) => mv))));
  return { plays, best, diceUsed: longest };
}

/** Number of dice any legal play must consume (0 === forced dance). */
export function maxDicePlayable(mine, opp, d1, d2) {
  return legalPlays(mine, opp, d1, d2).diceUsed;
}

/**
 * Sorted key of the mover's checker positions after applying `pairs` to `mine`.
 *
 * Each [from, to] pair moves one mover checker (to === 0 bears it off); the
 * effect is summed, so chained sub-moves (24/18 then 18/15) and the single
 * combined pair (24/15) that importers like BGBlitz record for them collapse to
 * the same outcome. Only the mover's own checkers matter for legality, so the
 * opponent (hits) is not tracked here.
 */
function outcomeKey(mine, pairs) {
  const m = mine.slice();
  for (const [f0, t0] of pairs) {
    const f = Number(f0);
    const t = t0 !== null && t0 !== undefined && Number(t0) > 0 ? Number(t0) : 0;
    if (f >= 1 && f <= 25) m[f] -= 1;
    if (t >= 1 && t <= 25) m[t] += 1;
  }
  const parts = [];
  for (let p = 1; p <= 25; p++) for (let n = 0; n < m[p]; n++) parts.push(p);
  return parts.join(",");
}

/**
 * True if `play` (a list of [from, to] pairs) is legal here.
 *
 * A play is legal exactly when it leaves the mover's checkers where some legal
 * (maximal) play would. Comparing the resulting position — rather than matching
 * sub-move spelling — makes this agnostic to how many dice a checker's move was
 * recorded as (per-die 24/18 18/15 vs combined 24/15, or a two-die bear-off
 * written as one [f, off]), while still rejecting an under-play: a move that
 * leaves a die unplayed lands somewhere no maximal play can reach.
 *
 * An empty `play` means "no checkers moved", legal only in a genuine dance.
 */
export function isPlayLegal(mine, opp, d1, d2, play) {
  const { plays, best, diceUsed } = legalPlays(mine, opp, d1, d2);
  const norm = normalizePlay(play);
  if (!norm) return diceUsed === 0;
  if (plays.has(norm)) return true; // exact per-die match: fast path
  const target = outcomeKey(mine, play);
  return best.some((s) => outcomeKey(mine, s.map(([mv]) => mv)) === target);
}
