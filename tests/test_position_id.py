# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""OGID parsing, and the XGID/OGID detection `gvan-position` routes on.

The centrepiece is section 1: OpenGammon's own ``xgid.test.js`` carries a
table of OGID strings paired with the XGID each encodes to, which makes an
independent cross-check of two parsers that share no code -- this repo's new
``gvformat.parse_ogid`` and the pre-existing ``gvanalysis.position.parse_xgid``.
Fed the same position in either notation, both must hand bgsage the same
board, cube and match context. The vectors are the reference implementation's,
not ones derived from the code under test.

Run directly:
    uv run python tests/test_position_id.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import board_to_ogid, looks_like_ogid, parse_ogid
from gvanalysis.position import parse_position_id, parse_xgid, state_from_ogid

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"   FAIL: {label}")
    else:
        print(f"   ok:   {label}")


def raises(fn, label: str) -> None:
    try:
        fn()
    except ValueError:
        check(True, label)
    else:
        check(False, f"{label} (no ValueError)")


# [name, OGID, the XGID it encodes to].
#
# These used to be lifted verbatim from opengammon-src's own xgid.test.js,
# which read the turn field as a perspective marker ("the board is written from
# the on-roll player's side"). That is true only when the field is +1, and the
# misreading travelled from there into gammonview's board.js and into this
# parser. Eight of the nine pairs below could not catch it: the opening
# position is flip-symmetric, the asymmetric one is mirror-symmetric between
# the colours, and `mover_is_white` -- the field the bug actually corrupts --
# was not checked at all. The XGIDs here are what the corrected encoder emits
# (White is always player 1, so the 26-point field is always mirrored; the turn
# field says only who is on roll), and the real OpenGammon ids in section 7 are
# what pins them.
OGID_XGID_PAIRS = [
    ("starting position, White acted -> Black on roll -> turn -1",
     "11jjjjjhhhccccc:ooddddd88866666:N0N::W:C:0:0:0",
     "XGID=-b----E-C---eE---c-e----B-:0:0:-1:00:0:0:0:0:8"),
    ("starting position, Black acted -> White on roll -> turn 1",
     "11jjjjjhhhccccc:ooddddd88866666:N0N::B:C:0:0:0",
     "XGID=-b----E-C---eE---c-e----B-:0:0:1:00:0:0:0:0:8"),
    ("asymmetric, White acted -> Black on roll -> turn -1",
     "jjjkkklllmmnnoo:111222333445566:N0N::W:C:0:0:0",
     "XGID=-BBBCCC------------bbbccc-:0:0:-1:00:0:0:0:0:8"),
    ("asymmetric, Black acted -> White on roll -> turn 1",
     "jjjkkklllmmnnoo:111222333445566:N0N::B:C:0:0:0",
     "XGID=-BBBCCC------------bbbccc-:0:0:1:00:0:0:0:0:8"),
    ("checkers on both bars swap ends with the mirror",
     "00jjj:ppp66:N0N::W:C:0:0:0",
     "XGID=c-----C------------b-----B:0:0:-1:00:0:0:0:0:8"),
    ("rolled dice are encoded",
     "jjjkkklllmmnnoo:111222333445566:N0N:63:W:R:0:0:0",
     "XGID=-BBBCCC------------bbbccc-:0:0:-1:63:0:0:0:0:8"),
    ("White owns the cube -> cube position 1 (White is player 1)",
     "11jjjjjhhhccccc:ooddddd88866666:W1N::W:C:0:0:0",
     "XGID=-b----E-C---eE---c-e----B-:1:1:-1:00:0:0:0:0:8"),
    ("Black owns the cube -> cube position -1",
     "11jjjjjhhhccccc:ooddddd88866666:B1N::W:C:0:0:0",
     "XGID=-b----E-C---eE---c-e----B-:1:-1:-1:00:0:0:0:0:8"),
    ("scores are emitted White-first with match length",
     "11jjjjjhhhccccc:ooddddd88866666:N0N::W:C:3:5:7",
     "XGID=-b----E-C---eE---c-e----B-:0:0:-1:00:3:5:0:7:8"),
]


def main() -> int:
    print("1. the same position in either notation parses to the same state")
    for name, ogid, xgid in OGID_XGID_PAIRS:
        a, b = state_from_ogid(ogid), parse_xgid(xgid)
        check(a.board == b.board, f"{name}: board")
        check(a.cube_value == b.cube_value and a.cube_owner == b.cube_owner,
              f"{name}: cube ({a.cube_value}/{a.cube_owner} vs "
              f"{b.cube_value}/{b.cube_owner})")
        check((a.die1, a.die2) == (b.die1, b.die2), f"{name}: dice")
        check((a.away1, a.away2) == (b.away1, b.away2), f"{name}: away scores")
        check(a.is_crawford == b.is_crawford, f"{name}: crawford")
        # The field the turn-as-perspective bug corrupted, and the one no
        # fixture here used to look at: which player the dice belong to.
        check(a.mover_is_white == b.mover_is_white,
              f"{name}: mover_is_white ({a.mover_is_white} vs {b.mover_is_white})")

    print("2. format detection routes each string to the right parser")
    for name, ogid, xgid in OGID_XGID_PAIRS:
        check(looks_like_ogid(ogid), f"{name}: OGID detected")
        check(not looks_like_ogid(xgid), f"{name}: XGID detected (labelled)")
        check(not looks_like_ogid(xgid.removeprefix("XGID=")),
              f"{name}: XGID detected (bare)")
        check(parse_position_id(ogid).board == parse_position_id(xgid).board,
              f"{name}: parse_position_id agrees either way")

    print("3. parse_ogid is the exact inverse of board_to_ogid")
    ogid = "11ccccchhhjjjjj:66666888dddddoo:W1N:36:B:R:3:5:7C:0"
    st = parse_ogid(ogid)
    check(board_to_ogid(
        st.board, mover_is_white=st.mover_is_white, cube_value=st.cube_value,
        cube_owner=st.cube_owner, cube_action=st.cube_action,
        dice=(st.die1, st.die2), on_roll=st.on_roll, game_state=st.game_state,
        score_white=st.score_white, score_black=st.score_black,
        match_length=st.match_length, crawford=st.crawford, move_id=st.move_id,
    ) == ogid, "round-trip through the encoder")

    print("4. on-roll is the complement of field 5 (who acted)")
    check(parse_ogid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:W:R:0:0:0").on_roll == "B",
          "color W -> Black on roll")
    check(parse_ogid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0").on_roll == "W",
          "color B -> White on roll")
    check(parse_ogid("11jjjjjhhhccccc:ooddddd88866666:N0N::W:IW:0:0:1:0").on_roll == "W",
          "the no-dice IW exception: White owes the opening roll")

    print("5. away counts come from the on-roll player's score")
    st = parse_ogid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:3:5:7:0")
    check(st.on_roll == "W" and (st.score_white, st.score_black) == (3, 5),
          "scores read as (white, black)")
    check((st.away1, st.away2) == (4, 2), "White on roll: 4-away vs 2-away")
    check(parse_ogid(
        "11ccccchhhjjjjj:66666888dddddoo:N0N:31:W:R:3:5:7:0").away1 == 2,
        "the same position with Black on roll swaps them")

    print("6. cube states are analyzed from the doubler's side")
    on_roll_board = parse_ogid(
        "11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0").board
    other_board = parse_ogid(
        "11ccccchhhjjjjj:66666888dddddoo:N0N:31:W:R:0:0:0").board
    offered = state_from_ogid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:C:0:0:0")
    check(offered.force_cube and (offered.die1, offered.die2) == (0, 0),
          "'C' forces a cube decision and drops the dice")
    check(offered.board == on_roll_board, "'C': the doubler is the on-roll player")
    pending = state_from_ogid("11ccccchhhjjjjj:66666888dddddoo:N0O:31:B:D:0:0:0")
    check(pending.board == other_board,
          "'D': a pending double flips back onto the doubler")

    print("7. cube owner, dead cube, optional fields")
    check(parse_ogid("11ccccchhhjjjjj:66666888dddddoo:D1N:31:B:R:0:0:0").cube_owner
          == "dead", "a dead cube parses as 'dead'")
    check(state_from_ogid("11ccccchhhjjjjj:66666888dddddoo:D1N:31:B:R:0:0:0").cube_owner
          == "centered", "...and is analyzed as centered")
    st = parse_ogid("11ccccchhhjjjjj:66666888dddddoo:N0N::W")
    check(st.game_state == "IW" and st.match_length == 0 and st.nrof_checkers == 15,
          "fields 6-11 default per the spec")
    check(parse_ogid("11ccccchhhjjjjj:66666888dddddoo:?0N:31:B:R:0:0:0").cube_owner
          == "centered",
          "'?' is a legal owner char, not a URL query to be split off")

    print("8. a pasted id survives labels, quotes and URL noise")
    plain = parse_ogid("11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0")
    for messy in [
        "  11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0  ",
        '"11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0"',
        "OGID=11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0",
        "ogid:11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0",
        "11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:R:0:0:0/",
        "11ccccchhhjjjjj%3A66666888dddddoo%3AN0N%3A31%3AB%3AR%3A0%3A0%3A0",
    ]:
        check(parse_ogid(messy) == plain, f"cleans {messy!r}")

    print("9. malformed ids raise rather than decode into a wrong board")
    for bad, why in [
        ("too:few:fields", "fewer than 5 fields"),
        ("11ccccchhhjjjjj:66666888dddddoo:XXX:31:B", "bad cube field"),
        ("11ccccchhhjjjjj:66666888dddddoo:N0N:79:B", "dice out of range"),
        ("11ccccchhhjjjjj:66666888dddddoo:N0N:31:X", "bad color"),
        ("11ccccchhhjjjjjz:66666888dddddoo:N0N:31:B", "bad pip character"),
        ("11ccccchhhjjjjjp:66666888dddddoo:N0N:31:B", "White on Black's bar"),
        ("11ccccchhhjjjjj:066666888dddddoo:N0N:31:B", "Black on White's bar"),
    ]:
        raises(lambda b=bad: parse_ogid(b), why)

    print("7. real OpenGammon ids: the same ply in both notations")
    # Six consecutive plies of one game, turns alternating, plus the cube
    # position that exposed the bug. Every one is a *pair*, so the OGID is an
    # independent control on the XGID rather than a restatement of it -- which
    # is what the synthetic fixtures above could never be, having been
    # generated by the very encoder they were meant to check.
    real_pairs = [
        ("XGID=-aaa-CBBB--AdB---b-cabA-AA:0:0:1:31:0:0:0:5:8",
         "013ccehhiijjkkk:44566688ddddmno:N0N:31:B:R:0:0:5:12"),
        ("XGID=-aaa-CBBB--AdB---b-cabBA--:0:0:-1:52:0:0:0:5:8",
         "233ccehhiijjkkk:44566688ddddmno:N0N:52:W:R:0:0:5:13"),
        ("XGID=--ab-CBBB--AcB---c-cabBA--:0:0:1:54:0:0:0:5:8",
         "233ccehhiijjkkk:445666888dddmmn:N0N:54:B:R:0:0:5:14"),
        ("XGID=--ab-CBBB--AcB---cBcabA---:0:0:-1:41:0:0:0:5:8",
         "377ccehhiijjkkk:445666888dddmmn:N0N:41:W:R:0:0:5:15"),
        ("XGID=--ab-CBBB--AcB---bBbbcA---:0:0:1:51:0:0:0:5:8",
         "377ccehhiijjkkk:444556688dddmmn:N0N:51:B:R:0:0:5:16"),
        ("XGID=--ab-DBBB---cB---bBbbcA---:0:0:-1:65:0:0:0:5:8",
         "377cchhiijjkkkk:444556688dddmmn:N0N:65:W:R:0:0:5:17"),
        # A 2-cube owned by Black, Black on roll, 2-0 in a 5-pointer.
        ("XGID=-CDbC-D-----A----a-ccbbb--:1:-1:-1:21:2:0:0:5:8",
         "djjjjlllnnnnooo:2233445556668mm:B1N:21:W:R:2:0:5:97"),
    ]
    for i, (xgid, ogid) in enumerate(real_pairs):
        a, b = parse_xgid(xgid), state_from_ogid(ogid)
        check(a.board == b.board, f"pair {i}: board")
        check(a.mover_is_white == b.mover_is_white,
              f"pair {i}: mover_is_white ({a.mover_is_white} vs {b.mover_is_white})")
        check(a.cube_owner == b.cube_owner and a.cube_value == b.cube_value,
              f"pair {i}: cube")
        check((a.away1, a.away2) == (b.away1, b.away2), f"pair {i}: away scores")
        check({a.die1, a.die2} == {b.die1, b.die2}, f"pair {i}: dice")

    # The turns alternate, so a parser that gets one sign right and the other
    # wrong still fails here.
    movers = [parse_xgid(x).mover_is_white for x, _ in real_pairs[:6]]
    check(movers == [True, False, True, False, True, False],
          f"the six plies alternate who is on roll (got {movers})")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
