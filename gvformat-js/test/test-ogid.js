// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// OGID golden vector tests (ported from gvformat/ogid.py __main__ and the
// decoder half of the Python repo's tests/test_position_id.py)

import {
  board_to_ogid, parseOgid, looksLikeOgid, flipBoard,
} from '../src/ogid.js';

const STARTING_BOARD = [
  0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5,
  5, 0, 0, 0, -3, 0, -5, 0, 0, 0, 0, 2, 0,
];

let passed = 0;
let failed = 0;

function check(actual, expected, label) {
  if (actual === expected) {
    passed++;
    console.log(`OK  ${label}: ${actual}`);
  } else {
    failed++;
    console.error(`FAIL  ${label}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
  }
}

function ok(cond, label) {
  check(cond ? "true" : "false", "true", label);
}

function throws(fn, label) {
  try {
    fn();
  } catch {
    check("threw", "threw", label);
    return;
  }
  check("returned", "threw", label);
}

/** Re-encode a parsed state; the encoder wants one `dice` pair, not two scalars. */
function reencode(st) {
  return board_to_ogid(st.board, {
    ...st, dice: st.die1 ? [st.die1, st.die2] : null,
  });
}

// 1. Standard starting position, White on roll
let ogid = board_to_ogid(STARTING_BOARD, {
  moverIsWhite: true, gameState: "IW", matchLength: 1,
});
check(ogid, "11ccccchhhjjjjj:66666888dddddoo:N0N::B:IW:0:0:1:0",
  "starting position, White on roll");

// 2. Same position, Black on roll
ogid = board_to_ogid(STARTING_BOARD, {
  moverIsWhite: false, gameState: "IB", matchLength: 1,
});
check(ogid, "11ccccchhhjjjjj:66666888dddddoo:N0N::W:IB:0:0:1:0",
  "starting position, Black on roll");

// 3. Asymmetric position, White on roll
let board = Array(26).fill(0);
board[1] = board[2] = board[3] = 2;
board[4] = board[5] = board[6] = 3;
board[19] = board[20] = board[21] = -2;
board[22] = board[23] = board[24] = -3;
ogid = board_to_ogid(board, { moverIsWhite: true, gameState: "C" });
check(ogid, "jjjkkklllmmnnoo:111222333445566:N0N::B:C:0:0:0:0",
  "asymmetric position, White on roll");

// 4. Same asymmetric position, Black on roll
board = Array(26).fill(0);
board[1] = board[2] = board[3] = 3;
board[4] = board[5] = board[6] = 2;
board[19] = board[20] = board[21] = -3;
board[22] = board[23] = board[24] = -2;
ogid = board_to_ogid(board, { moverIsWhite: false, gameState: "C" });
check(ogid, "jjjkkklllmmnnoo:111222333445566:N0N::W:C:0:0:0:0",
  "asymmetric position, Black on roll");

// 5. Checkers on both bars
board = Array(26).fill(0);
board[0] = 3;
board[6] = 3;
board[19] = -2;
board[25] = 2;
ogid = board_to_ogid(board, { moverIsWhite: true, gameState: "C" });
check(ogid, "00jjj:66ppp:N0N::B:C:0:0:0:0",
  "checkers on both bars");

// 6. Cube ownership: mover owns it
ogid = board_to_ogid(STARTING_BOARD, {
  moverIsWhite: true, gameState: "C", cubeValue: 2, cubeOwner: "player",
});
check(ogid.split(":")[2], "W1N", "cube: mover owns -> W1N");

// 7. Cube ownership: opponent owns it
ogid = board_to_ogid(STARTING_BOARD, {
  moverIsWhite: true, gameState: "C", cubeValue: 2, cubeOwner: "opponent",
});
check(ogid.split(":")[2], "B1N", "cube: opponent owns -> B1N");

// 8. Scores, match length, dice, crawford, nrof_checkers
ogid = board_to_ogid(STARTING_BOARD, {
  moverIsWhite: true, gameState: "C",
  dice: [6, 3], scoreWhite: 3, scoreBlack: 5, matchLength: 7, crawford: true,
});
const fields = ogid.split(":");
check(fields[3], "36", "dice sorted ascending");
check(fields.slice(6, 9).join(":"), "3:5:7C", "score/crawford fields");

ogid = board_to_ogid(STARTING_BOARD, { moverIsWhite: true, nrofCheckers: 15 });
check(String(ogid.split(":").length), "10", "nrof_checkers omitted when 15");
ogid = board_to_ogid(STARTING_BOARD, { moverIsWhite: true, nrofCheckers: 20 });
check(String(ogid.endsWith(":20")), "true", "nrof_checkers appended when != 15");

// 9. Cube value exponent table
const exponentTests = [[1,0],[2,1],[4,2],[8,3],[16,4],[32,5],[64,6]];
for (const [value, expected] of exponentTests) {
  // Test by encoding cube with that value and checking the exponent digit
  ogid = board_to_ogid(STARTING_BOARD, {
    moverIsWhite: true, cubeValue: value,
  });
  const cubeField = ogid.split(":")[2];
  const exp = parseInt(cubeField[1], 10);
  check(String(exp), String(expected), `cube value ${value} exponent`);
}

// --- decoder ---------------------------------------------------------------

// 10. parseOgid round-trips every encoder golden vector
for (const [label, vector] of [
  ["starting, W on roll", "11ccccchhhjjjjj:66666888dddddoo:N0N::B:R:0:0:1:0"],
  ["starting, B on roll", "11ccccchhhjjjjj:66666888dddddoo:N0N::W:IB:0:0:1:0"],
  ["asymmetric, W on roll", "jjjkkklllmmnnoo:111222333445566:N0N::B:C:0:0:0:0"],
  ["asymmetric, B on roll", "jjjkkklllmmnnoo:111222333445566:N0N::W:C:0:0:0:0"],
  ["both bars", "00jjj:66ppp:N0N::B:C:0:0:0:0"],
  ["owned cube + dice + crawford",
    "11ccccchhhjjjjj:66666888dddddoo:W1N:36:B:R:3:5:7C:0"],
  ["variant checker count",
    "11ccccchhhjjjjj:66666888dddddoo:N0N::B:R:0:0:0:0:20"],
]) {
  check(reencode(parseOgid(vector)), vector, `round-trip: ${label}`);
}

// 11. flipped() is an involution, and agrees with the encoder
let st = parseOgid("00jjj:66ppp:N0N::B:C:0:0:0:0");
ok(st.onRoll === "W" && st.board[25] === 2 && st.board[0] === 3,
  "both bars decode into the right bar slots");
const flipped = st.flipped();
ok(flipped.onRoll === "B" && flipped.board[25] === 3 && flipped.board[0] === 2,
  "flipped(): the bars swap sides");
check(JSON.stringify(flipped.flipped().board), JSON.stringify(st.board),
  "flipped() is an involution");
check(flipBoard(st.board).join(","), flipped.board.join(","),
  "flipped() uses flipBoard");
// Same absolute position either way -- only the color field differs.
check(board_to_ogid(flipped.board, {
  moverIsWhite: false, gameState: "C", onRoll: "B",
}), "00jjj:66ppp:N0N::W:C:0:0:0:0", "flipped(): same absolute position");

// 12. on-roll is the complement of field 5 (who acted)
check(parseOgid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:W:R:0:0:0").onRoll, "B",
  "color W -> Black on roll");
check(parseOgid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0").onRoll, "W",
  "color B -> White on roll");
check(parseOgid("11jjjjjhhhccccc:ooddddd88866666:N0N::W:IW:0:0:1:0").onRoll, "W",
  "the no-dice IW exception: White owes the opening roll");

// 13. cube owner is read relative to the mover
check(parseOgid("11ccccchhhjjjjj:66666888dddddoo:W1N::B:R:0:0:0:0").cubeOwner,
  "player", "White owns + White on roll -> player");
check(parseOgid("11ccccchhhjjjjj:66666888dddddoo:W1N::W:R:0:0:0:0").cubeOwner,
  "opponent", "White owns + Black on roll -> opponent");
check(parseOgid("11ccccchhhjjjjj:66666888dddddoo:D1N::B:R:0:0:0:0").cubeOwner,
  "dead", "a dead cube parses as dead");
check(parseOgid("11ccccchhhjjjjj:66666888dddddoo:?0N:31:B:R:0:0:0").cubeOwner,
  "centered", '"?" is a legal owner char, not a URL query to be split off');

// 14. away counts, cube decisions, optional-field defaults
st = parseOgid("11ccccchhhjjjjj:66666888dddddoo:N0N:36:B:R:3:5:7C:0");
ok(st.onRoll === "W" && st.away1 === 4 && st.away2 === 2,
  "White on roll at 3-5/7: 4-away vs 2-away");
ok(st.crawford && st.die1 === 3 && st.die2 === 6, "crawford + dice");
check(String(parseOgid(
  "11ccccchhhjjjjj:66666888dddddoo:N0N:36:W:R:3:5:7C:0").away1), "2",
  "the same position with Black on roll swaps the away counts");
st = parseOgid("11ccccchhhjjjjj:66666888dddddoo:N0N::W");
ok(st.gameState === "IW" && st.matchLength === 0 && st.nrofCheckers === 15,
  "fields 6-11 default per the spec");
ok(st.isMoney && st.away1 === 0 && st.away2 === 0, "no match length -> money");
ok(parseOgid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:C:0:0:0").isCubeDecision,
  '"C" is a cube decision');
ok(!parseOgid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0").isCubeDecision,
  '"R" is not');

// 15. a pasted id survives labels, quotes and URL noise
const plain = JSON.stringify(
  parseOgid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0"));
for (const messy of [
  "  11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0  ",
  '"11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0"',
  "OGID=11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0",
  "ogid:11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0",
  "11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0/",
  "11ccccchhhjjjjj%3A66666888dddddoo%3AN0N%3A31%3AB%3AR%3A0%3A0%3A0",
]) {
  check(JSON.stringify(parseOgid(messy)), plain, `cleans ${JSON.stringify(messy)}`);
}

// 16. malformed ids throw rather than decoding into a wrong board
for (const [bad, why] of [
  ["too:few:fields", "fewer than 5 fields"],
  ["11ccccchhhjjjjj:66666888dddddoo:XXX:31:B", "bad cube field"],
  ["11ccccchhhjjjjj:66666888dddddoo:N0N:79:B", "dice out of range"],
  ["11ccccchhhjjjjj:66666888dddddoo:N0N:31:X", "bad color"],
  ["11ccccchhhjjjjjz:66666888dddddoo:N0N:31:B", "bad pip character"],
  ["11ccccchhhjjjjjp:66666888dddddoo:N0N:31:B", "White on Black's bar"],
  ["11ccccchhhjjjjj:066666888dddddoo:N0N:31:B", "Black on White's bar"],
]) {
  throws(() => parseOgid(bad), why);
}

// 17. format detection: OGID vs XGID. The pairs are lifted from opengammon's
// own xgid.test.js, so each line is the same position in both notations.
for (const [name, ogidStr, xgid] of [
  ["starting position, white acted",
    "11jjjjjhhhccccc:ooddddd88866666:N0N::W:C:0:0:0",
    "XGID=-b----E-C---eE---c-e----B-:0:0:1:00:0:0:0:0:8"],
  ["starting position, black acted",
    "11jjjjjhhhccccc:ooddddd88866666:N0N::B:C:0:0:0",
    "XGID=-b----E-C---eE---c-e----B-:0:0:-1:00:0:0:0:0:8"],
  ["asymmetric",
    "jjjkkklllmmnnoo:111222333445566:N0N::W:C:0:0:0",
    "XGID=-CCCBBB------------cccbbb-:0:0:1:00:0:0:0:0:8"],
  ["checkers on both bars",
    "00jjj:ppp66:N0N::W:C:0:0:0",
    "XGID=b-----B------------c-----C:0:0:1:00:0:0:0:0:8"],
  ["rolled dice",
    "jjjkkklllmmnnoo:111222333445566:N0N:63:W:R:0:0:0",
    "XGID=-CCCBBB------------cccbbb-:0:0:1:63:0:0:0:0:8"],
  ["white owns the cube",
    "11jjjjjhhhccccc:ooddddd88866666:W1N::W:C:0:0:0",
    "XGID=-b----E-C---eE---c-e----B-:1:-1:1:00:0:0:0:0:8"],
  ["scores with match length",
    "11jjjjjhhhccccc:ooddddd88866666:N0N::W:C:3:5:7",
    "XGID=-b----E-C---eE---c-e----B-:0:0:1:00:5:3:0:7:8"],
]) {
  ok(looksLikeOgid(ogidStr), `${name}: OGID detected`);
  ok(!looksLikeOgid(xgid), `${name}: XGID detected (labelled)`);
  ok(!looksLikeOgid(xgid.replace(/^XGID=/, "")), `${name}: XGID detected (bare)`);
}

console.log(`\n${passed + failed} tests, ${failed} failures`);
process.exit(failed > 0 ? 1 : 0);
