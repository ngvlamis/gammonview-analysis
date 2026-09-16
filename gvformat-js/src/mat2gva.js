// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Pure-JS converter: Jellyfish/GNUbg/OpenGammon .mat match files -> OGXM JSON.
// No third-party dependencies, no engine -- board/cube/score tracking only.
//
// Ports (byte-for-byte, field-for-field) three Python modules:
//   gvanalysis/mat_parser.py         -> .mat text -> structured match dict
//   gvanalysis/game_reconstructor.py -> per-game decision reconstruction
//   gvanalysis/mat_to_ogxm.py        -> decisions -> unanalyzed OGXM-JSON
//
// Strategy (see mat_to_ogxm.py's docstring): build the same *unanalyzed*
// `data` dict (summary + per-game moves, no eval fields) that the analyzed
// pipeline's `game_eval` would produce minus the eval payload, then hand it
// to `toOgxmJson` (export.js) -- the one OGXM emitter, shared with the
// analyzed path. This module never builds OGXM plies itself.

import { toOgxmJson, _STARTING_BOARD_P1, _flipBoard } from "./export.js";

// ---------------------------------------------------------------------------
// Small helpers (no Python stdlib equivalent in JS)
// ---------------------------------------------------------------------------

// Split a text into lines the way Python's str.splitlines() does for the
// line-ending forms a .mat file actually uses (\r\n, \r, \n). splitlines()
// also breaks on a handful of exotic separators (\v, \f, \x1c-\x1e, \x85,
// U+2028/U+2029) that never appear in a .mat file, so they're not ported.
function _splitLines(text) {
  return text.split(/\r\n|\r|\n/);
}

// Python's int(s): optional surrounding whitespace, optional sign, digits
// only -- returns null (analogous to raising ValueError) for anything else,
// so callers can `continue` past a malformed token exactly as the try/except
// does in mat_parser.py / game_reconstructor.py.
function _pyInt(s) {
  const t = s.trim();
  if (!/^[+-]?\d+$/.test(t)) return null;
  return parseInt(t, 10);
}

// Python's re.escape().
function _escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Python's re.split(pattern, s, maxsplit=1) for a plain (non-capturing)
// pattern: split the string on only the first match, keeping the remainder
// (including any later matches) intact in the tail.
function _splitOnce(s, re) {
  const m = re.exec(s);
  if (!m) return [s];
  return [s.slice(0, m.index), s.slice(m.index + m[0].length)];
}

// ---------------------------------------------------------------------------
// Regexes (ports of mat_parser.py's module-level patterns)
// ---------------------------------------------------------------------------

// Handles both "Match of N points" and "N point(s) match"
const _MATCH_LEN_RE = /[Mm]atch\s+of\s+(\d+)|(\d+)\s+[Pp]oints?\s+[Mm]atch/;
const _GAME_HEADER_RE = /^\s*[Gg]ame\s+(\d+)\s*$/;
const _SCORE_LINE_RE = /^(.+?)\s*:\s*(\d+)\s{2,}(.+?)\s*:\s*(\d+)\s*$/;
const _TURN_NUM_RE = /^\s*\d+\)(.*)/s;
const _DOUBLE_RE = /[Dd]oubles?\s*=>\s*(\d+)/;
const _TAKES_RE = /^[Tt]akes\b|^[Aa]ccepts\b/;
const _DROPS_RE = /^[Dd]rops\b|^[Rr]ejects\b|^[Pp]asses\b/;
const _WINS_RE = /^[Ww]ins\b/;
const _WINS_DETAIL_RE = /^[Ww]ins?\s+(\d+)\s+[Pp]oints?(?:\s+with\s+a?\s*(gammon|backgammon))?/i;
// "Resigns" is the Jellyfish/GNUbg wording; OpenGammon writes "Resigned Game"
// or "Resigned Match". Missing the latter meant every OpenGammon resignation
// parsed as "unknown" and was dropped before reconstruction ever saw it.
const _RESIGNS_RE = /^[Rr]esign(?:s|ed)\b/;
const _FORFEITS_RE = /^[Ff]orfeits?\b/;
const _TIME_RE = /^[Ll]os(?:t|es)\s+on\s+time\b/i;
// Optional colon after dice: handles both "31 13/10" and "31: 13/10"
const _MOVE_RE = /^(\d)(\d):?\s*(.*)/;
// Player names from header comments: "; [Player 1 "name"]"
const _P1_COMMENT_RE = /;\s*\[Player 1 "([^"]+)"\]/;
const _P2_COMMENT_RE = /;\s*\[Player 2 "([^"]+)"\]/;
// Match rules and metadata from header comments
const _CRAWFORD_RULE_RE = /;\s*\[Crawford\s+"?(On|Off)"?\]/i;
const _JACOBY_RE = /;\s*\[Jacoby\s+"?(On|Off)"?\]/i;
const _BEAVER_RE = /;\s*\[Beaver\s+"?(On|Off)"?\]/i;
const _CUBE_LIMIT_RE = /;\s*\[CubeLimit\s+"?(\d+)"?\]/i;
const _EVENT_RE = /;\s*\[Event\s+"([^"]*)"\]/;
const _SITE_RE = /;\s*\[Site\s+"([^"]*)"\]/;
const _DATE_RE = /;\s*\[Date\s+"([^"]*)"\]/;
const _EVENT_DATE_RE = /;\s*\[EventDate\s+"([^"]*)"\]/;
const _EVENT_TIME_RE = /;\s*\[EventTime\s+"([^"]*)"\]/;

// ---------------------------------------------------------------------------
// Action parsing (mat_parser._parse_action)
// ---------------------------------------------------------------------------

/** @returns {[string, *]} [kind, data] -- mirrors Python's (atype, adata) tuple. */
function _parseAction(s) {
  s = s.trim();
  let m = _DOUBLE_RE.exec(s);
  if (m) return ["double", parseInt(m[1], 10)];
  if (_TAKES_RE.test(s)) return ["take", null];
  if (_DROPS_RE.test(s)) return ["drop", null];
  m = _WINS_DETAIL_RE.exec(s);
  if (m) {
    const winType = m[2] ? m[2].toLowerCase() : "normal";
    return ["win", { points: parseInt(m[1], 10), type: winType }];
  }
  if (_WINS_RE.test(s)) return ["win", { points: null, type: "normal" }];
  if (_RESIGNS_RE.test(s)) return ["resign", null];
  if (_FORFEITS_RE.test(s)) return ["forfeit", null];
  if (_TIME_RE.test(s)) return ["time", null];
  m = _MOVE_RE.exec(s);
  if (m) {
    const moves = m[3].trim();
    // A dice line with no move behind it. Marked so reconstruction can stop
    // here rather than read on: unlike a resign line, this one stands where a
    // checker play should be, so the board after it is not knowable.
    if (moves === "???") return ["resign", "unplayed"];
    return ["move", [parseInt(m[1], 10), parseInt(m[2], 10), moves]];
  }
  return ["unknown", s];
}

/** Old-style format: player names appear in the line before each action. */
function _splitPlayerActions(line, p1, p2) {
  if (!p1 || !p2) return [];
  // sorted([p1, p2], key=len, reverse=True): Python's sort is stable, and so
  // is Array.prototype.sort in every JS engine this repo targets (Node >= 18
  // / ES2019+), so a length tie keeps [p1, p2] source order same as Python.
  const names = [p1, p2].slice().sort((a, b) => b.length - a.length);
  const pattern = new RegExp(`(?:^|\\s)(${names.map(_escapeRegex).join("|")})\\s*:`);
  const parts = line.split(pattern);
  const result = [];
  for (let i = 1; i < parts.length; i += 2) {
    const player = parts[i];
    const rest = i + 1 < parts.length ? (parts[i + 1] || "").trim() : "";
    if (rest) result.push([player, rest]);
  }
  return result;
}

/** New-style two-column format: left = P1, right = P2, split on 3+ spaces. */
function _splitPositional(rest, p1, p2) {
  const halves = _splitOnce(rest, /\s{3,}/);
  let left = (halves[0] || "").trim();
  let right = halves.length > 1 ? halves[1].trim() : "";

  if (!right && left) {
    const sub = left.slice(3);
    const m = / (\d\d:)/.exec(sub);
    if (m) {
      const absPos = m.index + 3;
      right = left.slice(absPos + 1).trim();
      left = left.slice(0, absPos).trim();
    }
  }

  const result = [];
  for (const [player, text] of [[p1, left], [p2, right]]) {
    if (!text) continue;
    const [atype] = _parseAction(text);
    if (atype !== "unknown") result.push([player, text]);
  }
  return result;
}

// ---------------------------------------------------------------------------
// Public API: .mat text -> structured match dict (mat_parser.parse_mat_file)
// ---------------------------------------------------------------------------

/**
 * Parse a Jellyfish/GNUbg .mat file.
 *
 * Handles both the player-name-prefixed format (GNUbg) and the two-column
 * positional format used by OpenGammon / some Jellyfish variants.
 *
 * @param {string} text
 * @returns {object} {match_length, player1, player2, crawford_rule,
 *   jacoby_rule, beaver_rule, cube_limit, event, site, date, event_time,
 *   games: [{game_number, score1_start, score2_start, player_actions}]}
 */
export function parseMatFile(text) {
  const lines = _splitLines(text);

  // --- Match length ---
  let matchLength = 0;
  for (const line of lines.slice(0, 30)) {
    const m = _MATCH_LEN_RE.exec(line);
    if (m) {
      matchLength = parseInt(m[1] || m[2], 10);
      break;
    }
  }

  // --- Player names and match rules from header comments ---
  let player1 = "";
  let player2 = "";
  let crawfordRule = null;
  let jacobyRule = null;
  let beaverRule = null;
  let cubeLimit = null;
  let event = "";
  let site = "";
  let date = "";
  let eventTime = "";

  for (const line of lines.slice(0, 60)) {
    if (!player1) {
      const m = _P1_COMMENT_RE.exec(line);
      if (m) player1 = m[1].trim();
    }
    if (!player2) {
      const m = _P2_COMMENT_RE.exec(line);
      if (m) player2 = m[1].trim();
    }
    if (line.trim().startsWith(";")) {
      if (crawfordRule === null) {
        const m = _CRAWFORD_RULE_RE.exec(line);
        if (m) crawfordRule = m[1].toLowerCase() === "on";
      }
      if (jacobyRule === null) {
        const m = _JACOBY_RE.exec(line);
        if (m) jacobyRule = m[1].toLowerCase() === "on";
      }
      if (beaverRule === null) {
        const m = _BEAVER_RE.exec(line);
        if (m) beaverRule = m[1].toLowerCase() === "on";
      }
      if (cubeLimit === null) {
        const m = _CUBE_LIMIT_RE.exec(line);
        if (m) cubeLimit = parseInt(m[1], 10);
      }
      if (!event) {
        const m = _EVENT_RE.exec(line);
        if (m) event = m[1];
      }
      if (!site) {
        const m = _SITE_RE.exec(line);
        if (m) site = m[1];
      }
      if (!date) {
        const m = _DATE_RE.exec(line);
        if (m) {
          date = m[1];
        } else {
          const m2 = _EVENT_DATE_RE.exec(line);
          if (m2) date = m2[1];
        }
      }
      if (!eventTime) {
        const m = _EVENT_TIME_RE.exec(line);
        if (m) eventTime = m[1].replace(/\./g, ":");
      }
    }
  }

  const games = [];
  let currentGame = null;
  let scoreFound = false;
  // Per-game column players: the game score line tells us which player is on
  // the left vs right column, which can differ from the match-level player1/
  // player2 (e.g. when the transcriber always writes their own name on the
  // left).
  let gameLeftPlayer = "";
  let gameRightPlayer = "";

  for (const line of lines) {
    const stripped = line.trim();

    const gm = _GAME_HEADER_RE.exec(line);
    if (gm) {
      if (currentGame !== null) games.push(currentGame);
      currentGame = {
        game_number: parseInt(gm[1], 10),
        score1_start: 0,
        score2_start: 0,
        player_actions: [],
      };
      scoreFound = false;
      gameLeftPlayer = player1;
      gameRightPlayer = player2;
      continue;
    }

    if (currentGame === null) continue;

    if (!stripped || stripped.startsWith(";")) continue;

    // Score line comes before any move lines (no turn number)
    if (!scoreFound && !/^\s*\d+\)/.test(line)) {
      const sm = _SCORE_LINE_RE.exec(line);
      if (sm) {
        const leftName = sm[1].trim();
        const leftScore = parseInt(sm[2], 10);
        const rightName = sm[3].trim();
        const rightScore = parseInt(sm[4], 10);
        if (!player1 && leftName) {
          player1 = leftName;
          player2 = rightName;
        }
        // Use the score line to determine which player is in which column
        // for this specific game (may differ from match-level ordering).
        gameLeftPlayer = leftName;
        gameRightPlayer = rightName;
        // Normalize scores to match-level p1/p2 ordering so that away
        // calculations in the reconstructor are correct.
        if (gameLeftPlayer === player1) {
          currentGame.score1_start = leftScore;
          currentGame.score2_start = rightScore;
        } else {
          currentGame.score1_start = rightScore;
          currentGame.score2_start = leftScore;
        }
        scoreFound = true;
        continue;
      }
    }

    if (!(player1 && player2)) continue;

    const tm = _TURN_NUM_RE.exec(line);

    let actions = _splitPlayerActions(stripped, player1, player2);

    if (!actions.length) {
      if (tm) {
        actions = _splitPositional(tm[1], gameLeftPlayer, gameRightPlayer);
      } else {
        const [atype] = _parseAction(stripped);
        if (atype !== "unknown") {
          const leading = line.length - line.replace(/^\s+/, "").length;
          const player = leading >= 20 ? gameRightPlayer : gameLeftPlayer;
          actions = [[player, stripped]];
        }
      }
    }

    currentGame.player_actions.push(...actions);
  }

  if (currentGame !== null) games.push(currentGame);

  return {
    match_length: matchLength,
    player1: player1 || "Player 1",
    player2: player2 || "Player 2",
    crawford_rule: crawfordRule,
    jacoby_rule: jacobyRule,
    beaver_rule: beaverRule,
    cube_limit: cubeLimit,
    event,
    site,
    date,
    event_time: eventTime,
    games,
  };
}

// ---------------------------------------------------------------------------
// Board manipulation (game_reconstructor._apply_move_notation)
// ---------------------------------------------------------------------------

/** Apply move notation to board from mover's perspective. Returns new board. */
function _applyMoveNotation(board, notation) {
  const b = [...board];
  for (let token of notation.split(/\s+/).filter(Boolean)) {
    token = token.replace(/\*+$/, "");
    let count = 1;
    if (token.includes("(")) {
      const parenIdx = token.indexOf("(");
      const cntRaw = token.slice(parenIdx + 1).replace(/\)+$/, "");
      token = token.slice(0, parenIdx);
      const cnt = _pyInt(cntRaw);
      count = cnt !== null ? cnt : 1;
    }
    if (!token.includes("/")) continue;
    const slashIdx = token.indexOf("/");
    const fromStr = token.slice(0, slashIdx);
    const toStr = token.slice(slashIdx + 1).replace(/\*+$/, "");

    let fromPt, toPt;
    if (fromStr.toLowerCase() === "bar") {
      fromPt = 25;
    } else {
      fromPt = _pyInt(fromStr);
      if (fromPt === null) continue;
    }
    const toLow = toStr.toLowerCase();
    if (toLow === "off" || toLow === "bear" || toLow === "0") {
      toPt = null;
    } else {
      toPt = _pyInt(toStr);
      if (toPt === null) continue;
    }

    for (let k = 0; k < count; k++) {
      if (fromPt === 25) {
        if (b[25] <= 0) continue; // no checker on bar to move
        b[25] -= 1;
      } else if (fromPt >= 1 && fromPt <= 24) {
        if (b[fromPt] <= 0) continue; // notation references a point with no mover's checker
        b[fromPt] -= 1;
      } else {
        continue;
      }
      if (toPt !== null && toPt >= 1 && toPt <= 24) {
        if (b[toPt] === -1) { // Hit: blot -> opponent's bar (slot 0, positive)
          b[toPt] = 0;
          b[0] += 1;
        }
        b[toPt] += 1;
      }
    }
  }
  return b;
}

// ---------------------------------------------------------------------------
// Match / game helpers (game_reconstructor.py)
// ---------------------------------------------------------------------------

function _flipOwner(owner) {
  return { centered: "centered", player: "opponent", opponent: "player" }[owner];
}

/** [away_mover, away_opp]. Returns [0, 0] for money games. */
function _awayFor(isMoverP1, s1, s2, ml) {
  if (ml <= 0) return [0, 0];
  if (isMoverP1) return [Math.max(1, ml - s1), Math.max(1, ml - s2)];
  return [Math.max(1, ml - s2), Math.max(1, ml - s1)];
}

/**
 * Return the 0-based index of the Crawford game, or null.
 *
 * The Crawford game is the first game where exactly one player is 1-away.
 * All subsequent games at that score are post-Crawford (cube available).
 * Returns null for money games or when the Crawford rule is explicitly off.
 */
export function findCrawfordGameIndex(games, matchLength, crawfordRule = null) {
  if (matchLength <= 0 || crawfordRule === false) return null;
  for (let i = 0; i < games.length; i++) {
    const s1 = games[i].score1_start;
    const s2 = games[i].score2_start;
    if ((s1 === matchLength - 1) !== (s2 === matchLength - 1)) return i;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Decision reconstruction (game_reconstructor.reconstruct_decisions)
// ---------------------------------------------------------------------------

/**
 * Walk a game's player_actions and emit a decision dict for each evaluation
 * needed. See game_reconstructor.py's docstring for the exact per-kind shape
 * ('cube' / 'checker') -- ported field-for-field.
 */
export function reconstructDecisions(game, matchLength, p1, p2, isCrawford = false) {
  const s1 = game.score1_start;
  const s2 = game.score2_start;

  let board = [..._STARTING_BOARD_P1];
  let cubeValue = 1;
  let cubeOwner = "centered"; // always from current mover's perspective

  const decisions = [];
  let gameResult = null;
  // How the game ended, once a resign/forfeit/timeout line has said so. Held
  // rather than acted on, because the "Wins N points" line that normally
  // follows is the authoritative one -- see the loop below.
  let endedBy = null;

  // Cube-offer state
  let anyMoveMade = false; // cube cannot be offered before the opening roll
  let dblPending = false;
  let dblBoard = [];
  let dblCubeVal = 0;
  let dblCubeOwn = "";
  let dblPlayer = "";
  let dblIsP1 = false;

  for (const [player, actionStr] of game.player_actions) {
    const isP1 = player === p1;
    const [atype, adata] = _parseAction(actionStr);

    if (atype === "win") {
      const info = adata && typeof adata === "object" ? adata : {};
      gameResult = {
        winner: player,
        points: info.points !== undefined ? info.points : null,
        // How it ended outranks how it was scored: "Resigned Game" followed by
        // "Wins 2 points" is a resignation worth two, not a plain win.
        type: endedBy ? endedBy.type : info.type || "normal",
      };
      break;
    }
    if (atype === "resign" || atype === "forfeit" || atype === "time") {
      // Whose column the line sits in does not settle who lost: OpenGammon
      // puts "Resigned Game" in the *winner's*, and the alternating two-column
      // layout means the side it lands on is a function of the ply count, not
      // of intent. So guess from the column only as a last resort, and let the
      // "Wins N points" line that normally follows overrule it -- that one
      // names the winner outright and carries the points, which a resignation
      // otherwise loses (they are not derivable from the cube: a player may
      // resign a gammon).
      endedBy = { winner: isP1 ? p2 : p1, points: null, type: atype };
      // "???" is the exception. It stands where a checker play should be, so
      // the board is unreconstructable from here and reading on would replay
      // later moves against a stale position.
      if (adata === "unplayed") break;
      continue;
    }

    if (atype === "double") {
      const newVal = adata;
      dblPending = true;
      dblBoard = [...board];
      dblCubeVal = cubeValue;
      dblCubeOwn = cubeOwner;
      dblPlayer = player;
      dblIsP1 = isP1;
      cubeValue = newVal;
    } else if (atype === "take") {
      if (dblPending) {
        const [away1, away2] = _awayFor(dblIsP1, s1, s2, matchLength);
        decisions.push({
          kind: "cube",
          board: dblBoard,
          cube_value: dblCubeVal,
          cube_owner: dblCubeOwn,
          doubled: true,
          response: "take",
          doubler: dblPlayer,
          responder: dblIsP1 ? p2 : p1,
          is_doubler_p1: dblIsP1,
          away1,
          away2,
          is_crawford: isCrawford,
        });
        cubeOwner = "opponent"; // responder now owns cube, from doubler's view
        dblPending = false;
      }
    } else if (atype === "drop") {
      if (dblPending) {
        const [away1, away2] = _awayFor(dblIsP1, s1, s2, matchLength);
        decisions.push({
          kind: "cube",
          board: dblBoard,
          cube_value: dblCubeVal,
          cube_owner: dblCubeOwn,
          doubled: true,
          response: "pass",
          doubler: dblPlayer,
          responder: dblIsP1 ? p2 : p1,
          is_doubler_p1: dblIsP1,
          away1,
          away2,
          is_crawford: isCrawford,
        });
        gameResult = { winner: dblPlayer, points: dblCubeVal, type: "pass" };
        dblPending = false;
      }
      break; // Double/pass ends the game
    } else if (atype === "move") {
      const [die1, die2, movesStr] = adata;

      // Record "no double" cube decision if player had access and didn't
      // double
      if (anyMoveMade && !dblPending && (cubeOwner === "centered" || cubeOwner === "player") && !isCrawford) {
        const [away1, away2] = _awayFor(isP1, s1, s2, matchLength);
        // Skip dead cube: mover can already clinch the match at current stake
        if (!(matchLength > 0 && cubeValue >= away1)) {
          decisions.push({
            kind: "cube",
            board: [...board],
            cube_value: cubeValue,
            cube_owner: cubeOwner,
            doubled: false,
            response: null,
            doubler: player,
            responder: null,
            is_doubler_p1: isP1,
            away1,
            away2,
            is_crawford: isCrawford,
          });
        }
      }

      // Apply checker move
      const boardBefore = [...board];
      const noLegal = ["", "(none)", "none", "-"].includes(movesStr.trim().toLowerCase());
      const boardAfter = noLegal ? [...board] : _applyMoveNotation(board, movesStr);
      anyMoveMade = true;

      const [away1, away2] = _awayFor(isP1, s1, s2, matchLength);
      decisions.push({
        kind: "checker",
        board: boardBefore,
        dice: [die1, die2],
        notation: movesStr,
        board_played: boardAfter,
        cube_value: cubeValue,
        cube_owner: cubeOwner,
        player,
        is_p1: isP1,
        away1,
        away2,
        is_crawford: isCrawford,
        no_legal: noLegal,
      });

      board = _flipBoard(boardAfter);
      cubeOwner = _flipOwner(cubeOwner);
    }
  }

  // No "Wins N points" line ever came -- some writers end a resigned game on
  // the resign line alone. The column guess is all there is.
  if (!gameResult && endedBy) gameResult = endedBy;

  return { decisions, game_result: gameResult, is_crawford: isCrawford };
}

// ---------------------------------------------------------------------------
// Unanalyzed data -> OGXM-JSON (mat_to_ogxm.py)
// ---------------------------------------------------------------------------

/**
 * Board (stored mover-perspective) in the fixed Player-1/White frame the
 * OGXM export uses. Mirrors mat_to_ogxm._board_p1 so the no-analysis path
 * and the analyzed path produce byte-identical GAME data.
 */
function _boardP1(board, moverIsP1) {
  return moverIsP1 ? [...board] : _flipBoard(board);
}

/**
 * Turn reconstructed decision dicts into unanalyzed OGXM `moves` entries.
 *
 * Mirrors the entry shapes `game_eval` builds (checker / cube_decision /
 * cube_response) but with no eval payload. Implicit "no-double" cube
 * decisions (doubled === false) produce no ply -- they are analysis-only in
 * the analyzed path (embedded on the next checker ply) and are re-derived by
 * ogxm_reconstructor when the OGXM is later analyzed.
 */
function _decisionsToMoves(decisions) {
  const moves = [];
  for (const dec of decisions) {
    if (dec.kind === "checker") {
      const isP1 = dec.is_p1;
      moves.push({
        player: dec.player,
        kind: "checker",
        dice: [...dec.dice],
        cube_value: dec.cube_value,
        cube_owner: dec.cube_owner,
        board_before: _boardP1(dec.board, isP1),
        board_after: _boardP1(dec.board_played, isP1),
        // Carried for the illegal-play ladder only. The steps are normally
        // derived from the two boards, but a board diff cannot be collapsed
        // safely when a play overflows its ply record -- see fitMoveSteps.
        notation: dec.notation,
      });
    } else if (dec.kind === "cube") {
      if (!dec.doubled) continue; // implicit no-double: no ply in OGXM
      const boardP1 = _boardP1(dec.board, dec.is_doubler_p1);
      moves.push({ player: dec.doubler, kind: "cube_decision", board: boardP1 });
      if (dec.response !== null) {
        moves.push({
          player: dec.responder,
          kind: "cube_response",
          cube_value: dec.cube_value * 2,
          player_response: dec.response,
          board: boardP1,
        });
      }
    }
  }
  return moves;
}

/**
 * Parsed-.mat dict -> the unanalyzed `data` dict toOgxmJson consumes
 * (summary + games with per-game moves, no eval fields).
 */
export function matToData(matchData) {
  const p1 = matchData.player1;
  const p2 = matchData.player2;
  const ml = matchData.match_length;
  const crawfordIdx = findCrawfordGameIndex(matchData.games, ml, matchData.crawford_rule);

  const gamesOut = matchData.games.map((game, i) => {
    const isCrawford = i === crawfordIdx;
    const recon = reconstructDecisions(game, ml, p1, p2, isCrawford);
    return {
      game_number: game.game_number,
      score_start: { player1: game.score1_start, player2: game.score2_start },
      is_crawford: isCrawford,
      result: recon.game_result,
      moves: _decisionsToMoves(recon.decisions),
    };
  });

  const summary = {
    player1: p1,
    player2: p2,
    match_length: ml > 0 ? ml : null,
    crawford_rule: matchData.crawford_rule,
    jacoby_rule: matchData.jacoby_rule,
    beaver_rule: matchData.beaver_rule,
    cube_limit: matchData.cube_limit,
    event: matchData.event || null,
    site: matchData.site || null,
    date: matchData.date || null,
    event_time: matchData.event_time || null,
    // No eval_level/preset key -> toOgxmJson emits no analysis_info.
  };
  return { summary, games: gamesOut };
}

// ---------------------------------------------------------------------------
// Main converter
// ---------------------------------------------------------------------------

/**
 * Convert a Jellyfish/GNUbg/OpenGammon .mat file (as text) to OGXM JSON, no
 * analysis.
 * @param {string} text
 * @returns {object}
 */
export function convertMat(text) {
  return toOgxmJson(matToData(parseMatFile(text)));
}

export const convert_mat = convertMat;
