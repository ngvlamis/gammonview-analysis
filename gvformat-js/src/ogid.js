// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Pure-JS codec: bgsage mover-perspective board <-> OGID position string.
// ESM port of gvformat/ogid.py — no third-party dependencies.
//
// `boardToOgid` is the encoder; `parseOgid` is its inverse (OGID string ->
// OgidState, whose `board` is the same mover-perspective array).
// `looksLikeOgid` tells an OGID from an XGID for callers that accept either.

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** @type {string} base-26 pip characters: '0'-'9' then 'a'-'p'. */
const _PIP_CHARS = "0123456789abcdefghijklmnop";

/** @type {Set<string>} OGID cube-owner characters. */
const _ABSOLUTE_CUBE_OWNERS = new Set(["N", "W", "B", "D", "?"]);

/** @type {Set<string>} OGID cube-action characters. */
const _CUBE_ACTIONS = new Set(["N", "O", "T", "P"]);

// ---------------------------------------------------------------------------
// Field encoders
// ---------------------------------------------------------------------------

/**
 * @param {number} pip 0–25
 * @returns {string}
 */
function _pipChar(pip) {
  if (pip < 0 || pip > 25) {
    throw new RangeError(`pip out of range 0-25: ${pip}`);
  }
  return _PIP_CHARS[pip];
}

/**
 * Convert a mover-perspective board to (white_positions, black_positions).
 *
 * Both returned strings are the OGID base-26 checker encoding, sorted in
 * ascending pip order.
 *
 * @param {number[]} board 26-element mover-perspective array
 * @param {boolean} moverIsWhite
 * @returns {[string, string]}
 */
function _absolutePositions(board, moverIsWhite) {
  if (board.length !== 26) {
    throw new RangeError(`board must have exactly 26 entries, got ${board.length}`);
  }

  /** @type {number[]} */
  const whitePips = [];
  /** @type {number[]} */
  const blackPips = [];

  for (let i = 0; i < 26; i++) {
    const count = board[i];
    if (count === 0) continue;

    let ownerIsMover;
    let n;

    if (i === 0) {
      // Opponent's bar: stored as a plain non-negative count.
      ownerIsMover = false;
      n = count;
    } else if (i === 25) {
      // Mover's own bar: stored as a plain non-negative count.
      ownerIsMover = true;
      n = count;
    } else {
      ownerIsMover = count > 0;
      n = Math.abs(count);
    }

    const absolutePip = moverIsWhite ? (25 - i) : i;
    const ownerIsWhite = ownerIsMover ? moverIsWhite : !moverIsWhite;
    const target = ownerIsWhite ? whitePips : blackPips;
    for (let j = 0; j < n; j++) {
      target.push(absolutePip);
    }
  }

  whitePips.sort((a, b) => a - b);
  blackPips.sort((a, b) => a - b);

  const whitePositions = whitePips.map(_pipChar).join("");
  const blackPositions = blackPips.map(_pipChar).join("");
  return [whitePositions, blackPositions];
}

/**
 * Log2 exponent for the cube-value field (1->0, 2->1, 4->2, … 64->6).
 *
 * @param {number} cubeValue
 * @returns {number}
 */
function _cubeValueExponent(cubeValue) {
  if (cubeValue < 1 || (cubeValue & (cubeValue - 1)) !== 0) {
    throw new RangeError(
      `cube_value must be a power of two >= 1, got ${cubeValue}`,
    );
  }
  // Math.clz32 returns 32 for 0; for a power of two v >= 1 the exponent is
  // 31 - Math.clz32(v).
  return 31 - Math.clz32(cubeValue);
}

/**
 * Resolve a cube owner into an OGID owner character (N/W/B/D/?).
 *
 * Accepts either an absolute OGID character directly ("N", "W", "B", "D",
 * "?"), or one of the mover-relative keywords ("centered", "player",
 * "opponent", "dead", "unknown"), or the literal color names
 * "white"/"black".
 *
 * @param {string} cubeOwner
 * @param {boolean} moverIsWhite
 * @returns {string}
 */
function _cubeOwnerChar(cubeOwner, moverIsWhite) {
  if (_ABSOLUTE_CUBE_OWNERS.has(cubeOwner)) return cubeOwner;

  const key = cubeOwner.toLowerCase();
  if (key === "centered") return "N";
  if (key === "dead") return "D";
  if (key === "unknown") return "?";
  if (key === "player") return moverIsWhite ? "W" : "B";
  if (key === "opponent") return moverIsWhite ? "B" : "W";
  if (key === "white") return "W";
  if (key === "black") return "B";
  throw new Error(`Unrecognized cube_owner: "${cubeOwner}"`);
}

/**
 * @param {number} cubeValue
 * @param {string} cubeOwner
 * @param {string} cubeAction
 * @param {boolean} moverIsWhite
 * @returns {string}
 */
function _encodeCube(cubeValue, cubeOwner, cubeAction, moverIsWhite) {
  if (!_CUBE_ACTIONS.has(cubeAction)) {
    throw new RangeError(
      `cube_action must be one of ${[..._CUBE_ACTIONS].join(",")}, got "${cubeAction}"`,
    );
  }
  const ownerChar = _cubeOwnerChar(cubeOwner, moverIsWhite);
  return `${ownerChar}${_cubeValueExponent(cubeValue)}${cubeAction}`;
}

/**
 * @param {[number, number] | null | undefined} dice
 * @returns {string}
 */
function _encodeDice(dice) {
  if (!dice || dice.length === 0) return "";
  const [d1, d2] = dice;
  if (d1 < 1 || d1 > 6 || d2 < 1 || d2 > 6) {
    throw new RangeError(`dice values must each be 1-6, got (${d1}, ${d2})`);
  }
  // Always ascending (min first) regardless of roll order.
  return d1 <= d2 ? `${d1}${d2}` : `${d2}${d1}`;
}

/**
 * @param {number} matchLength
 * @param {boolean} crawford
 * @param {boolean} postCrawford
 * @param {number | null | undefined} maxGames
 * @returns {string}
 */
function _encodeMatchLength(matchLength, crawford, postCrawford, maxGames) {
  let s = String(matchLength);
  if (crawford) {
    s += "C";
  } else if (maxGames != null) {
    s += `G${maxGames}`;
  } else if (postCrawford) {
    s += "L";
  }
  return s;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Encode a bgsage mover-perspective board as an OGID position string.
 *
 * @param {number[]} board 26-element mover-perspective array
 * @param {object} opts
 * @param {boolean}   opts.moverIsWhite
 * @param {number}    [opts.cubeValue=1]
 * @param {string}    [opts.cubeOwner="centered"]
 * @param {string}    [opts.cubeAction="N"]
 * @param {[number, number] | null} [opts.dice=null]
 * @param {string | null} [opts.onRoll=null]
 * @param {string}    [opts.gameState=""]
 * @param {number}    [opts.scoreWhite=0]
 * @param {number}    [opts.scoreBlack=0]
 * @param {number}    [opts.matchLength=0]
 * @param {boolean}   [opts.crawford=false]
 * @param {boolean}   [opts.postCrawford=false]
 * @param {number | null} [opts.maxGames=null]
 * @param {number}    [opts.moveId=0]
 * @param {number}    [opts.nrofCheckers=15]
 * @returns {string}
 */
export function boardToOgid(board, {
  moverIsWhite,
  cubeValue = 1,
  cubeOwner = "centered",
  cubeAction = "N",
  dice = null,
  onRoll = null,
  gameState = "",
  scoreWhite = 0,
  scoreBlack = 0,
  matchLength = 0,
  crawford = false,
  postCrawford = false,
  maxGames = null,
  moveId = 0,
  nrofCheckers = 15,
} = {}) {
  const [whitePositions, blackPositions] = _absolutePositions(board, moverIsWhite);
  const cubeField = _encodeCube(cubeValue, cubeOwner, cubeAction, moverIsWhite);
  const diceField = _encodeDice(dice);

  let onRollColor;
  if (onRoll === null || onRoll === undefined) {
    onRollColor = moverIsWhite ? "W" : "B";
  } else {
    if (onRoll !== "W" && onRoll !== "B") {
      throw new RangeError(`on_roll must be "W" or "B", got "${onRoll}"`);
    }
    onRollColor = onRoll;
  }
  // OGID color field = the player who REACHED this position (complement of
  // on-roll), matching ogid.cpp's color_char derivation.
  const color = onRollColor === "W" ? "B" : "W";

  const matchLengthField = _encodeMatchLength(
    matchLength, crawford, postCrawford, maxGames,
  );

  const parts = [
    whitePositions,
    blackPositions,
    cubeField,
    diceField,
    color,
    gameState,
    String(scoreWhite),
    String(scoreBlack),
    matchLengthField,
    String(moveId),
  ];

  let ogid = parts.join(":");
  if (nrofCheckers !== 15) {
    ogid += `:${nrofCheckers}`;
  }
  return ogid;
}

export { boardToOgid as board_to_ogid };

// ---------------------------------------------------------------------------
// Decoder: OGID position string -> mover-perspective board + state
// ---------------------------------------------------------------------------

/**
 * The shape the OpenGammon backend accepts (`backend/match/boardstate.py`):
 * only the first five fields are constrained, the numeric tail is optional.
 * Used to tell an OGID from an XGID — an XGID fails it on field 1 (it carries
 * '-' and uppercase A-O) and on field 3 (a bare signed integer, not
 * `[BWND?]\d+[OTPN]`).
 */
const _OGID_SHAPE = /^[0-9a-p]*:[0-9a-p]*:[BWND?]\d+[OTPN]:\d{0,2}:[WB]/;

/** Field 9, `<length>[LCG][<max_games>]`. */
const _MATCH_LENGTH_RE = /^(\d+)([LCG]?)(\d*)$/;

/**
 * Mover-relative cube owner per OGID owner character, as
 * [whenMoverIsWhite, whenMoverIsBlack]. "?" (uninitialized) reads as a
 * centered cube, matching ogid.js's sanitizeBoard.
 */
const _RELATIVE_CUBE_OWNER = {
  N: ["centered", "centered"],
  D: ["dead", "dead"],
  "?": ["centered", "centered"],
  W: ["player", "opponent"],
  B: ["opponent", "player"],
};

/**
 * Game states that are — or have just resolved — a cube decision
 * (`CUBE_STATES` in gammonview's ogid.js): C offered-to-be-made, D pending
 * double, A taken, P passed.
 * @type {Set<string>}
 */
export const CUBE_GAME_STATES = new Set(["C", "D", "A", "P"]);

/**
 * Swap a mover-perspective board to the other player's perspective.
 *
 * Mirrors bgsage's `flip_board`: index `i` maps to `25 - i`, sign-negated for
 * the points (1-24) and copied unsigned for the two bar slots (0 = opponent's
 * bar, 25 = mover's own bar).
 *
 * @param {number[]} board 26-element mover-perspective array
 * @returns {number[]}
 */
export function flipBoard(board) {
  if (board.length !== 26) {
    throw new RangeError(`board must have exactly 26 entries, got ${board.length}`);
  }
  const flipped = new Array(26).fill(0);
  flipped[0] = board[25];
  flipped[25] = board[0];
  for (let i = 1; i < 25; i++) {
    flipped[25 - i] = -board[i];
  }
  return flipped;
}

/**
 * A parsed OGID, with the board in the on-roll player's perspective.
 *
 * The field names are `boardToOgid`'s option names, so
 * `boardToOgid(state.board, { ...state, dice })` round-trips back to the
 * original string. (Only the dice differ: they are carried here as the two
 * scalars `die1`/`die2`, which the encoder takes as one `dice` pair.)
 */
export class OgidState {
  /**
   * @param {object} fields
   * @param {number[]} fields.board 26-element, on-roll player's perspective
   * @param {boolean}  fields.moverIsWhite true when the on-roll player is White
   * @param {number}   fields.die1 0 when no dice are set
   * @param {number}   fields.die2
   * @param {number}   fields.cubeValue 1, 2, 4, … 64
   * @param {string}   fields.cubeOwner mover-relative: centered/player/opponent/dead
   * @param {string}   fields.cubeAction raw OGID action char: N/O/T/P
   * @param {string}   fields.onRoll "W" or "B" — who owes the next action
   * @param {string}   fields.color raw field 5: who *made* the last action
   * @param {string}   fields.gameState
   * @param {number}   fields.scoreWhite
   * @param {number}   fields.scoreBlack
   * @param {number}   fields.matchLength 0 = money game
   * @param {boolean}  fields.crawford
   * @param {boolean}  fields.postCrawford
   * @param {number | null} fields.maxGames
   * @param {number}   fields.moveId
   * @param {number}   fields.nrofCheckers
   */
  constructor(fields) {
    Object.assign(this, fields);
  }

  /** @returns {boolean} */
  get isMoney() {
    return this.matchLength === 0;
  }

  /** Points the on-roll player still needs; 0 for a money game. @returns {number} */
  get away1() {
    if (this.isMoney) return 0;
    return this.matchLength - (this.moverIsWhite ? this.scoreWhite : this.scoreBlack);
  }

  /** Points the opponent still needs; 0 for a money game. @returns {number} */
  get away2() {
    if (this.isMoney) return 0;
    return this.matchLength - (this.moverIsWhite ? this.scoreBlack : this.scoreWhite);
  }

  /** True when the position is (or just resolved) a cube decision. @returns {boolean} */
  get isCubeDecision() {
    return CUBE_GAME_STATES.has(this.gameState);
  }

  /**
   * The same position read from the other player's perspective.
   * @returns {OgidState}
   */
  flipped() {
    const swap = { player: "opponent", opponent: "player" };
    return new OgidState({
      ...this,
      board: flipBoard(this.board),
      moverIsWhite: !this.moverIsWhite,
      cubeOwner: swap[this.cubeOwner] ?? this.cubeOwner,
      onRoll: this.onRoll === "W" ? "B" : "W",
    });
  }
}

/**
 * True when `text` parses as an OGID rather than an XGID.
 *
 * An explicit `OGID=`/`OGID:` or `XGID=`/`XGID:` label decides it; otherwise
 * the OpenGammon backend's position-shape regex does. An XGID never matches
 * that shape (its board field carries '-' and A-O, and its cube field is a
 * bare integer).
 *
 * @param {string} text
 * @returns {boolean}
 */
export function looksLikeOgid(text) {
  const s = String(text).trim().replace(/^["'`]+|["'`]+$/g, "");
  if (/^ogid[=:]/i.test(s)) return true;
  if (/^xgid[=:]/i.test(s)) return false;
  return _OGID_SHAPE.test(s);
}

/**
 * `"W1O"` -> [cubeValue, ownerChar, actionChar].
 * @param {string} field
 * @returns {[number, string, string]}
 */
function _parseCube(field) {
  const m = /^([BWND?])(\d+)([OTPN])$/.exec(field);
  if (!m) throw new Error(`Invalid OGID cube field: "${field}"`);
  const exponent = parseInt(m[2], 10);
  if (exponent > 6) {
    throw new RangeError(`OGID cube exponent out of range 0-6: ${exponent}`);
  }
  return [1 << exponent, m[1], m[3]];
}

/**
 * @param {string} field
 * @returns {[number, number]}
 */
function _parseDice(field) {
  if (field === "") return [0, 0];
  if (!/^[1-6][1-6]$/.test(field)) {
    throw new Error(`Invalid OGID dice field: "${field}"`);
  }
  return [Number(field[0]), Number(field[1])];
}

/**
 * `"7C"` -> [matchLength, crawford, postCrawford, maxGames].
 * @param {string} field
 * @returns {[number, boolean, boolean, number | null]}
 */
function _parseMatchLength(field) {
  const m = _MATCH_LENGTH_RE.exec(field.trim());
  // board.js falls back to a money game rather than throwing.
  if (!m) return [0, false, false, null];
  return [
    parseInt(m[1], 10),
    m[2] === "C",
    m[2] === "L",
    m[3] ? parseInt(m[3], 10) : null,
  ];
}

/**
 * @param {string} ch
 * @returns {number}
 */
function _pipValue(ch) {
  const pip = _PIP_CHARS.indexOf(ch);
  if (pip < 0) throw new Error(`Invalid OGID position character: "${ch}"`);
  return pip;
}

/**
 * Parse an integer field, falling back like board.js's NaN guards.
 * @param {string | undefined} field
 * @param {number} dflt
 * @returns {number}
 */
function _intOr(field, dflt) {
  const s = String(field ?? "").trim();
  return /^[+-]?\d+$/.test(s) ? parseInt(s, 10) : dflt;
}

/**
 * Inverse of `_absolutePositions`.
 *
 * Absolute pip -> raw index is the inverse of the encoder's mapping:
 * `i = 25 - pip` when the mover is White, `i = pip` when Black.
 *
 * @param {string} whitePositions
 * @param {string} blackPositions
 * @param {boolean} moverIsWhite
 * @returns {number[]}
 */
function _boardFromPositions(whitePositions, blackPositions, moverIsWhite) {
  const board = new Array(26).fill(0);
  for (const [positions, ownerIsWhite] of [
    [whitePositions, true], [blackPositions, false],
  ]) {
    for (const ch of positions) {
      const pip = _pipValue(ch);
      const ownerIsMover = ownerIsWhite === moverIsWhite;
      const i = moverIsWhite ? (25 - pip) : pip;
      if (i === 0 || i === 25) {
        // Bar slots hold a plain count: index 25 is the mover's own bar,
        // index 0 the opponent's. A checker on the *other* colour's bar pip
        // (White on 25, Black on 0) is not a position that exists.
        if ((i === 25) !== ownerIsMover) {
          const side = ownerIsWhite ? "White" : "Black";
          throw new Error(
            `${side} checker on pip ${pip} is not a valid OGID position`,
          );
        }
        board[i] += 1;
      } else {
        board[i] += ownerIsMover ? 1 : -1;
      }
    }
  }
  return board;
}

/**
 * Parse an OGID position string into an `OgidState`.
 *
 * Accepts an optional `OGID=`/`OGID:` label and tolerates the wrapping quotes
 * and trailing URL punctuation a pasted id arrives with. Fields 1-5 are
 * required; the rest default as the spec says (game state "IW", 0-0, money
 * game, move 0, 15 checkers).
 *
 * The returned `board` is in the perspective of the player who owes the next
 * action — `playerToAct()` in gammonview's board.js, i.e. the *complement* of
 * field 5, which records whoever made the last action. (The one exception
 * board.js carves out, and this mirrors: the no-dice "IW" that opens a game,
 * where White still owes the opening roll.)
 *
 * @param {string} ogid
 * @returns {OgidState}
 */
export function parseOgid(ogid) {
  let s = String(ogid).trim().replace(/^["'`]+|["'`]+$/g, "").trim();
  // A trailing fragment/slash left over from a URL. Note we do NOT split on
  // "?" the way gammonview's clean_raw_id does: "?" is a legal cube-owner
  // character ("?0N", an uninitialized cube), and splitting there would eat
  // the rest of the id.
  s = s.split("#")[0].replace(/\/+$/, "").trim();
  if (!s.includes(":") && s.toLowerCase().includes("%3a")) {
    s = decodeURIComponent(s); // the id came straight out of a URL
  }
  s = s.replace(/^ogid[=:]/i, "").trim();

  const parts = s.split(":");
  if (parts.length < 5) {
    throw new Error(
      `OGID needs at least 5 fields, got ${parts.length}: "${ogid}"`,
    );
  }

  const [cubeValue, ownerChar, cubeAction] = _parseCube(parts[2]);
  const [die1, die2] = _parseDice(parts[3]);

  const color = parts[4];
  if (color !== "W" && color !== "B") {
    throw new Error(`OGID color field must be "W" or "B", got "${color}"`);
  }

  const gameState = parts.length > 5 ? parts[5] : "IW";

  // Field 5 names the player who *acted*; the next actor is the other one.
  // (board.js's playerToAct() returns null for a finished game; there is no
  // board to analyze there, so we still report the complement and leave the
  // gameState for the caller to notice.)
  const onRoll = (gameState === "IW" && die1 === 0)
    ? "W"
    : (color === "W" ? "B" : "W");
  const moverIsWhite = onRoll === "W";

  const board = _boardFromPositions(parts[0], parts[1], moverIsWhite);

  const [matchLength, crawford, postCrawford, maxGames] = parts.length > 8
    ? _parseMatchLength(parts[8])
    : [0, false, false, null];

  return new OgidState({
    board,
    moverIsWhite,
    die1,
    die2,
    cubeValue,
    cubeOwner: _RELATIVE_CUBE_OWNER[ownerChar][moverIsWhite ? 0 : 1],
    cubeAction,
    onRoll,
    color,
    gameState,
    scoreWhite: parts.length > 6 ? _intOr(parts[6], 0) : 0,
    scoreBlack: parts.length > 7 ? _intOr(parts[7], 0) : 0,
    matchLength,
    crawford,
    postCrawford,
    maxGames,
    moveId: parts.length > 9 ? _intOr(parts[9], 0) : 0,
    nrofCheckers: parts.length > 10 ? _intOr(parts[10], 15) : 15,
  });
}

export {
  parseOgid as parse_ogid,
  looksLikeOgid as looks_like_ogid,
  flipBoard as flip_board,
};
