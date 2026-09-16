// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// OpenGammon analysis payload -> OGXM analysis, merged onto a base match.
//
// Unlike the other converters here, this one does not build a match: it takes a
// base OGXM document (in practice `convertMat` applied to opengammon.com's
// `/export/?format=mat`) and attaches the evaluations from that same match's
// `/app/match/<id>/analysis/` payload. OpenGammon serves the two separately and
// the analysis payload alone carries no player names, dates or result, so the
// match body always comes from the .mat and only the analysis comes from here.
//
// The base's match body is kept **verbatim**. Only per-ply `analysis` and the
// top-level `analysis_info` are written, exactly as `appendAnalysis` promises.
//
// ## Why this alignment is safe
//
// OpenGammon emits one analysis entry per decision ply, in ply order, including
// doubles, takes, drops and plies with no legal move. That is precisely the set
// `appendAnalysis` calls a decision ply (action_id 0-23), so the two zip 1:1 and
// a length mismatch is a hard error rather than something to paper over.
//
// The one exception is how a game ends. A resignation or a timeout is not a
// decision ply, but OpenGammon still records it in the move list, in whichever
// of two shapes fits (see `_TERMINAL_LABELS`) -- so a resigned game arrives one
// entry longer than its decision plies, or with a real move's played field
// overwritten. Both are handled below; neither is papered over anywhere else.
//
// ## Frames and conventions, all verified against real matches
//
//   * Colour. OpenGammon's `W` is the base's `color === 0`. Their position
//     strings disagree with ours about which OGID field holds which player, but
//     we never read their positions -- the base's body is authoritative -- so
//     that discrepancy cannot leak in.
//   * Moves. Encoded as letter pairs after the two dice characters, in the
//     *mover's own* frame (bar 25, off 0): `y` is the bar, `z` is off, and
//     `a`..`x` run 1..24 for W and 24..1 for B. Note this is NOT what
//     `to_move()` in the OpenGammon backend does -- that display helper reverses
//     `y`/`z` for White and would read a bar entry as a bear-off.
//   * Routes. A chained play ("15/14 14/11") may pick different intermediate
//     points than the base's own step list for the same net move. That is
//     expected and harmless: the ply's `moves` stays authoritative, and the read
//     layer already prefers it over an alternative's route.
//   * Cube sign. OpenGammon negates no-double/take/pass equities on the
//     responder's ply. OGXM stores all three in the *doubler's* frame on every
//     ply and lets the read layer flip them for take/pass rows, so they are
//     negated back here. Probabilities are mover-perspective in both formats and
//     pass through untouched.

/** Decision plies (action_id 0-23) are the ones that can carry analysis. */
function _isDecision(ply) {
  const aid = ply.action_id;
  return aid !== null && aid !== undefined && aid >= 0 && aid <= 23;
}

/**
 * How a game ended, written where a played move should be.
 *
 * OpenGammon's `AnalysisSerializer` overloads the move field to mark a
 * resignation ("resigned") or a timeout ("timed out"), and which entry gets it
 * depends on who moved last. If the resigner did, the label *replaces* their
 * move and the entry keeps its evaluations. If the winner did, a *duplicate* of
 * that entry is appended carrying the label and two empty arrays.
 */
const _TERMINAL_LABELS = new Set(['resigned', 'timed out']);

/**
 * True for the appended form, which has no ply to attach to and so must not
 * consume one -- the base's game ends at the last real decision.
 *
 * Told apart from a label written over a real move by the position string,
 * which the appended entry copies wholesale from its predecessor. Every real
 * entry carries its own ply index in that string, so a repeat can only be the
 * marker.
 */
function _isTerminalMarker(entry, prev) {
  return _TERMINAL_LABELS.has(entry?.[1]) && prev !== undefined && entry[0] === prev[0];
}

/**
 * The kind of action a base ply records, for reading an entry whose own played
 * field was overwritten by a terminal label. Without this a resigned-on take
 * looks like a checker play with no candidates, and its cube analysis is
 * dropped in silence.
 */
function _actionFromPly(ply) {
  if (ply.action_id === 21) return 'double';
  if (ply.action_id === 22) return 'take';
  if (ply.action_id === 23) return 'drop';
  return null;
}

/** OpenGammon move letter -> the mover's own-frame point (bar 25, off 0). */
function _ownPoint(ch, ogColor) {
  if (ch === 'y') return 25;
  if (ch === 'z') return 0;
  const i = ch.charCodeAt(0) - 97; // 'a'
  if (i < 0 || i > 23) return null;
  return ogColor === 'W' ? i + 1 : 24 - i;
}

/**
 * Decode an OpenGammon move string into OGXM `{from, pips}` steps.
 *
 * @param {string} sgf e.g. "25mhmk" -- two dice characters then from/to pairs
 * @param {string} ogColor 'W' or 'B'
 * @param {number} gvaColor the base ply's `color`
 * @returns {{from: number, pips: number}[]|null} null for a non-checker action
 */
function _decodeMove(sgf, ogColor, gvaColor) {
  if (typeof sgf !== 'string' || !sgf.length || !'123456'.includes(sgf[0])) return null;
  const body = sgf.slice(2);
  const steps = [];
  for (let i = 0; i + 1 < body.length; i += 2) {
    const from = _ownPoint(body[i], ogColor);
    const to = _ownPoint(body[i + 1], ogColor);
    if (from === null || to === null) {
      throw new Error(`unreadable move step "${body[i]}${body[i + 1]}" in "${sgf}"`);
    }
    // The own frame always counts down towards 0 (off); the absolute frame is
    // mirrored for color 1, but `pips` is a distance and so is frame-free.
    steps.push({ from: gvaColor === 0 ? from : 25 - from, pips: from - to });
  }
  return steps;
}

/** Same equity convention the rest of gvformat uses. */
function _evalFromProbs(win, gwin, bgwin, gloss, bgloss) {
  const equity = win + gwin + bgwin - gloss - bgloss;
  return {
    win, gammon_win: gwin, bg_win: bgwin,
    gammon_loss: gloss, bg_loss: bgloss,
    equity: Math.round(equity * 10000) / 10000,
  };
}

/** OpenGammon writes cubeful levels as "2C" / "0C"; OGXM wants "2ply". */
function _evalLevel(level) {
  if (typeof level !== 'string') return null;
  const m = /^(\d+)C$/.exec(level);
  return m ? `${m[1]}ply` : level;
}

/** Doubling is right when the opponent's better reply still beats holding. */
function _shouldDouble(nd, dt, dp) {
  return Math.min(dt, dp) > nd;
}

// --- PR decision counting ---------------------------------------------------
//
// OpenGammon counts decisions with its own `count_xg_decisions`; we count with
// the house rule below, so that an OG import and a re-analysis of the same
// match agree with each other. The two rules are close but not identical -- on
// the reference match they select the same checker plies and differ on a single
// cube ply -- so a PR here can sit slightly off the one opengammon.com shows.

// Minimum spread across a ply's candidate equities for the checker play to
// count: forced moves and already-decided positions are excluded. Mirrors
// gvformat.xg._CHECKER_SPREAD_EPS / xg2gva's CHECKER_SPREAD_EPS.
const CHECKER_SPREAD_EPS = 1e-4;

// Cube decision so clear it should not count toward PR. Mirrors
// gvanalysis.game_eval._trivial_cube / xg2gva's trivialCube.
function _trivialCube(nd, dt, dp) {
  return (
    Math.abs(nd - Math.min(dt, dp)) < 0.001
    || (nd - dt) > 0.200
    || (nd - dp) > 0.200
    || (nd < -0.900 && dt < -0.900)
  );
}

// Take/pass response so clear it should not count toward PR. Mirrors
// gvanalysis.game_eval._trivial_take_pass / xg2gva's trivialTakePass.
function _trivialTakePass(dt, dp) {
  return Math.abs(dt - dp) < 0.001;
}

/** Mirrors reader.js's `_cubeActionLabel`. */
function _cubeActionLabel(shouldDouble, dt, dp) {
  if (!shouldDouble) return 'no_double';
  return dp >= dt ? 'double_take' : 'double_pass';
}

/**
 * Split OpenGammon's 10-element cube vector.
 * `[ply, W, WG, WBG, LG, LBG, EQ, EQ_ND, EQ_DT, EQ_DP]`
 *
 * @param {number[]} cube
 * @param {boolean} responder true on a take/drop ply, where the equities arrive
 *   negated into the responder's frame and must be put back.
 */
function _cubeParts(cube, responder) {
  const s = responder ? -1 : 1;
  return {
    eval_level: _evalLevel(cube[0]),
    evaluation: _evalFromProbs(cube[1], cube[2], cube[3], cube[4], cube[5]),
    nd: s * cube[7],
    dt: s * cube[8],
    dp: s * cube[9],
  };
}

/** Checker-play analysis for one ply, or null when nothing was evaluated. */
function _checkerAnalysis(entry, ply, ogColor) {
  const [, played, candidates] = entry;
  if (!Array.isArray(candidates) || candidates.length === 0) return null;

  const playedBody = typeof played === 'string' ? played.slice(2) : null;
  const alternatives = candidates.map((c) => {
    // [ply, move, EQ, W, WG, WBG, LG, LBG]
    const [level, move, equity, win, gwin, bgwin, gloss, bgloss] = c;
    return {
      move: _decodeMove(`11${move}`, ogColor, ply.color) || [],
      equity,
      is_played: move === playedBody,
      eval: _evalFromProbs(win, gwin, bgwin, gloss, bgloss),
      eval_level: _evalLevel(level),
    };
  });

  // OpenGammon ranks best-first, but the played move is appended out of order
  // when it falls outside the top six, so take the best by value not position.
  const best = alternatives.reduce((a, b) => (b.equity > a.equity ? b : a));
  const playedAlt = alternatives.find((a) => a.is_played) || null;
  const best_equity = best.equity;
  const played_equity = playedAlt ? playedAlt.equity : null;

  // A play counts toward PR only if there was a genuine choice. OpenGammon
  // lists the top six candidates plus the played one, so the spread is over
  // what it chose to send, not over every legal move -- which is the right
  // frame anyway: if the top six are indistinguishable, so is the rest.
  const equities = alternatives.map((a) => a.equity);
  const spread = equities.length >= 2
    ? Math.max(...equities) - Math.min(...equities)
    : 0.0;

  return {
    eval: best.eval,
    best_equity,
    played_equity,
    equity_loss: played_equity === null
      ? 0
      : Math.round((best_equity - played_equity) * 10000) / 10000,
    decision: spread >= CHECKER_SPREAD_EPS,
    alternatives,
  };
}

/**
 * The cube analysis a checker ply carries when the cube was live, as
 * `{key, sub}` for `analysis[key] = sub`. The two keys are mutually exclusive
 * -- the format allows one cube record per checker ply, and the binary writer
 * drops `cube_decision` outright when both are present (see binary.js's
 * `_write_cube_evals`), so emitting both would make a saved match disagree
 * with the one in memory. Mirrors xg2gva's `embeddedCube`.
 */
function _embeddedCube(cube) {
  const { eval_level, evaluation, nd, dt, dp } = _cubeParts(cube, false);

  // Doubling was right and the player rolled instead: always a real decision,
  // sized by what holding the cube cost. It carries the same pre-roll eval as
  // the branch below -- one position, judged the other way.
  if (_shouldDouble(nd, dt, dp)) {
    return { key: 'missed_double', sub: {
      no_double_equity: nd,
      double_take_equity: dt,
      double_pass_equity: dp,
      equity_loss: Math.round((Math.min(dt, dp) - nd) * 10000) / 10000,
      correct_action: 'double',
      eval: evaluation,
      eval_level,
    } };
  }

  // Not doubling was right. It still counts toward PR unless the cube was
  // never in question.
  return { key: 'cube_decision', sub: {
    should_double: false,
    no_double_equity: nd,
    double_take_equity: dt,
    double_pass_equity: dp,
    action: _cubeActionLabel(false, dt, dp),
    equity_loss: 0.0,
    decision: !_trivialCube(nd, dt, dp),
    eval: evaluation,
    eval_level,
  } };
}

/** Analysis for an actual cube action: double (21), take (22) or drop (23). */
function _cubeActionAnalysis(cube, action) {
  const responder = action === 'take' || action === 'drop';
  const { eval_level, evaluation, nd, dt, dp } = _cubeParts(cube, responder);
  const should_double = _shouldDouble(nd, dt, dp);

  let correct_action;
  let played_action;
  if (action === 'double') {
    correct_action = should_double ? 'double' : 'no_double';
    played_action = 'double';
  } else {
    // The responder keeps whichever branch costs the doubler less.
    correct_action = dt < dp ? 'take' : 'pass';
    played_action = action === 'take' ? 'take' : 'pass';
  }

  const equity_loss = correct_action === played_action
    ? 0
    : Math.abs(action === 'double' ? Math.min(dt, dp) - nd : dt - dp);
  const loss = Math.round(equity_loss * 10000) / 10000;

  // A double counts toward PR unless the cube was trivial *and* the doubler
  // got it right anyway; a take/pass counts unless the two branches are
  // indistinguishable. Mirrors game_eval's doubler_counts / resp_counts.
  const decision = action === 'double'
    ? !(_trivialCube(nd, dt, dp) && loss < 0.001)
    : !_trivialTakePass(dt, dp);

  return {
    correct_action,
    played_action,
    no_double_equity: nd,
    double_take_equity: dt,
    double_pass_equity: dp,
    equity_loss: loss,
    decision,
    eval: evaluation,
    eval_level,
  };
}

/** One analysis object for one decision ply, or null when there is nothing. */
function _plyAnalysis(entry, ply) {
  const [position, played, , cube] = entry;
  const ogColor = typeof position === 'string' ? position.split(':')[4] : 'W';
  const hasCube = Array.isArray(cube) && cube.length >= 10;
  const action = _TERMINAL_LABELS.has(played) ? _actionFromPly(ply) : played;

  if (action === 'double' || action === 'take' || action === 'drop') {
    return hasCube ? _cubeActionAnalysis(cube, action) : null;
  }

  const analysis = _checkerAnalysis(entry, ply, ogColor);
  if (!analysis) return null;
  if (hasCube) {
    const { key, sub } = _embeddedCube(cube);
    analysis[key] = sub;
  }
  return analysis;
}

/**
 * Attach an OpenGammon analysis payload to a base OGXM match.
 *
 * @param {object} base OGXM document, normally `convertMat(matText)`. Mutated
 *   in place and returned; its games, plies and OGIDs are left untouched.
 * @param {object} payload the `analysis` payload -- the inner object, with
 *   `config`, `games` and `match_stats`, not the outer job record.
 * @param {object} [options]
 * @param {number} [options.timestamp] unix seconds for `analysis_info`; the job
 *   record's `analysed_time` is the right source when you have it.
 * @param {string} [options.model_id] engine label, default "gnubg".
 * @returns {object} base
 */
export function convertOg(base, payload, options = {}) {
  if (!base || !Array.isArray(base.games)) {
    throw new Error('base is not an OGXM match');
  }
  if (!payload || !Array.isArray(payload.games)) {
    throw new Error('payload carries no games; pass the analysis payload, not the job record');
  }
  if (base.games.length !== payload.games.length) {
    throw new Error(
      `game count mismatch: base has ${base.games.length}, analysis has ${payload.games.length}`);
  }

  const attached = [];
  base.games.forEach((game, gi) => {
    const plies = (game.plies || []).filter(_isDecision);
    const moves = payload.games[gi].moves || [];
    const entries = moves.filter((entry, i) => !_isTerminalMarker(entry, moves[i - 1]));
    if (plies.length !== entries.length) {
      throw new Error(
        `analysis/ply count mismatch in game ${gi + 1}: `
        + `${plies.length} decision plies vs ${entries.length} analysis entries`);
    }
    plies.forEach((ply, i) => attached.push([ply, _plyAnalysis(entries[i], ply)]));
  });

  // Nothing is written until every game has been read, so a mismatch late in
  // the match cannot leave a half-analysed document behind.
  for (const [ply, analysis] of attached) {
    if (analysis) ply.analysis = analysis;
  }

  const level = _evalLevel(payload.config) || payload.config || null;
  const plyDepth = /^(\d+)/.exec(level || '');
  base.analysis_info = {
    ply: plyDepth ? Number(plyDepth[1]) : null,
    eval_level: level,
    luck_eval_level: null,
    model_id: options.model_id || 'gnubg',
    timestamp: options.timestamp ?? null,
  };

  return base;
}

export { convertOg as convert_og };
