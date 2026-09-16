# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""``analyze_position`` — the single-position analysis a server answers inline.

Two things are worth pinning down here, and neither is "the engine picks good
moves" (that is bgsage's own business):

  1. **The shape is OGXM's.** Every consumer of this — the position panel above
     all — already renders stored match analysis, and does so by reading
     ``move`` steps, ``eval`` objects and the ND/DT/DP triple. If this endpoint
     invented its own vocabulary, that renderer would need a second one.
  2. **The frame is the caller's.** Steps come back in absolute point
     numbering, which is only meaningful relative to who is on roll. Get
     ``mover_is_white`` wrong and every arrow the client draws is mirrored —
     a bug that looks like an engine error and isn't.

Requires the bgsage engine (the ``[engine]`` extra).

Run directly:
    uv run python tests/test_analyze_position.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvanalysis.position import EVAL_LEVELS, analyze_position

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


#: The opening 31, in both notations. White is on roll in the OGID (field 5 "B"
#: names who *acted*), and XGID turn -1 is the same statement, so the two must
#: analyze to the same board, the same frame and the same best move.
OPENING_31_OGID = "11ccccchhhjjjjj:66666888dddddoo:N0N:13:B:R:0:0:0:0"
OPENING_31_XGID = "XGID=-b----E-C---eE---c-e----B-:0:0:-1:31:0:0:0:0:8"

#: The same opening position with no dice: a cube decision (nobody doubles on
#: the first roll, so the correct action is a no-double either way).
OPENING_CUBE_XGID = "XGID=-b----E-C---eE---c-e----B-:0:0:-1:00:0:0:0:0:8"


def main() -> int:
    print("1. a checker play comes back as ranked OGXM alternatives")
    res = analyze_position(OPENING_31_OGID, level="1ply")
    check(res["kind"] == "checker", "kind is 'checker' when dice are set")
    check(res["dice"] == [1, 3], "the dice are echoed back")
    alts = res["alternatives"]
    check(len(alts) == 16, f"all 16 legal plays are ranked (got {len(alts)})")
    check(alts[0]["notation"] == "8/5 6/5",
          f"the 31 play is 8/5 6/5 (got {alts[0]['notation']!r})")
    equities = [a["equity"] for a in alts]
    check(equities == sorted(equities, reverse=True), "sorted best-first")
    check(alts[0]["diff"] == 0.0 and all(a["diff"] <= 0 for a in alts),
          "diff is 0 for the best move and <= 0 for the rest")
    check(all(not a["is_played"] for a in alts),
          "nothing is 'played': an edited position has no played move")

    print("2. each alternative carries an Eval object and its level")
    top = alts[0]
    ev = top["eval"]
    check(set(ev) == {"win", "gammon_win", "bg_win", "gammon_loss", "bg_loss", "equity"},
          "the Eval object has exactly the OGXM keys")
    check(all(0.0 <= ev[k] <= 1.0 for k in
              ("win", "gammon_win", "bg_win", "gammon_loss", "bg_loss")),
          "probabilities are in [0, 1]")
    check(top["eval_level"] == "1ply",
          f"eval_level is canonical, not bgsage's '1-ply' (got {top['eval_level']!r})")
    check(res["eval_level"] == "1ply", "the run's level is reported")

    print("3. moves are structured steps in absolute point numbering")
    # White on roll: mover-own point p sits at absolute 25 - p. So 8/5 6/5 is
    # a 3-pip step off absolute 17 and a 1-pip step off absolute 19.
    check(res["mover_is_white"] is True, "White is on roll in this OGID")
    check(top["move"] == [{"from": 17, "pips": 3}, {"from": 19, "pips": 1}],
          f"8/5 6/5 -> absolute steps (got {top['move']})")
    check(all(isinstance(s["from"], int) and 0 <= s["from"] <= 25
              and 1 <= s["pips"] <= 6
              for a in alts for s in a["move"]),
          "every step is a legal {from 0-25, pips 1-6} pair")

    print("4. the same position as an XGID analyzes identically")
    xg = analyze_position(OPENING_31_XGID, level="1ply")
    check(xg["mover_is_white"] is True, "XGID turn -1 means White is on roll")
    check([a["notation"] for a in xg["alternatives"]]
          == [a["notation"] for a in alts], "same ranking as the OGID")
    check(xg["alternatives"][0]["move"] == top["move"],
          "and the same absolute steps, so a client's arrows match either way")

    print("5. flipping who is on roll mirrors the frame, not the ranking")
    # Field 5 "W" -> Black acted last -> Black on roll. Same checkers, other
    # side of the board: the best play is still 8/5 6/5 in the mover's own
    # numbering, but now those points are absolute 8 and 6.
    black = analyze_position(
        "11ccccchhhjjjjj:66666888dddddoo:N0N:13:W:R:0:0:0:0", level="1ply")
    check(black["mover_is_white"] is False, "Black is on roll")
    check(black["alternatives"][0]["notation"] == "8/5 6/5",
          "notation is mover-relative, so it is unchanged")
    check(black["alternatives"][0]["move"] == [{"from": 8, "pips": 3},
                                               {"from": 6, "pips": 1}],
          f"steps mirror to Black's absolute points (got {black['alternatives'][0]['move']})")

    print("5b. a two-die move is split around a point the opponent has made")
    # One checker on the mover's 24-point playing a 5-1 to 18. Going 5 first
    # lands on 19, which the opponent has five checkers on; going 1 first stops
    # on 23, which is open. bgsage names the move by its endpoints ("24/18"),
    # so the stop in between is inferred -- and the position editor draws its
    # board arrows straight from these steps, so getting it wrong shows a
    # checker landing on a stack of enemy checkers and carrying on.
    # Black on roll, so mover-own numbering is already absolute.
    primed = analyze_position(
        "XGID=-A---bEcC---eE-----e----A-:0:0:1:51:0:0:0:0:8", level="1ply")
    run = next(a for a in primed["alternatives"] if a["notation"] == "24/18")
    check(run["move"] == [{"from": 24, "pips": 1}, {"from": 23, "pips": 5}],
          f"24/18 off a 5-1 goes round via 23, not through the made 19 (got {run['move']})")

    print("6. no dice means a cube decision")
    cube_res = analyze_position(OPENING_CUBE_XGID, level="1ply")
    check(cube_res["kind"] == "cube", "kind is 'cube'")
    check(cube_res["dice"] is None, "no dice to echo")
    check("alternatives" not in cube_res, "and no move list")
    cube = cube_res["cube"]
    check(cube["correct_action"] == "no_double",
          f"nobody doubles the opening position (got {cube['correct_action']!r})")
    check(cube["correct_response"] in ("take", "pass"),
          "the responder's correct answer is named too")
    check(cube["double_pass_equity"] == 1.0,
          "a money-game pass is worth exactly the cube")
    check(cube["no_double_equity"] > cube["double_take_equity"],
          "holding beats doubling here, which is why the action is no_double")
    check(set(cube["eval"]) == set(top["eval"]),
          "the cube's Eval object matches the alternatives' shape")

    print("7. an OGID cube state is a cube decision even with dice set")
    # "C" (an offer to be made) is a cube decision by construction; the dice
    # field is stale and must not turn it back into a checker play.
    offered = analyze_position(
        "11ccccchhhjjjjj:66666888dddddoo:N0N:31:B:C:0:0:0", level="1ply")
    check(offered["kind"] == "cube", "game_state 'C' forces the cube reading")
    check(offered["dice"] is None, "the stale dice are dropped")

    print("8. match context reaches the engine")
    # The textbook case: at 2-away 2-away you double the opening position on
    # sight (winning the game wins the match, so the cube costs nothing and
    # gammons are worthless), where at money you hold. Same checkers, opposite
    # answers — which no amount of dropping the score on the floor reproduces.
    # `double_pass_equity` is *not* the check: a pass is worth exactly the cube
    # by definition, so it reads 1.0 at every score.
    money = analyze_position(
        "11ccccchhhjjjjj:66666888dddddoo:N0N::B:C:0:0:0", level="1ply")
    match_2away = analyze_position(
        "11ccccchhhjjjjj:66666888dddddoo:N0N::B:C:3:3:5", level="1ply")
    check(match_2away["away1"] == 2 and match_2away["away2"] == 2,
          "3-3 in a 5-pointer is 2-away 2-away")
    check(money["cube"]["correct_action"] == "no_double"
          and match_2away["cube"]["correct_action"] == "double",
          "hold at money, double at 2-away 2-away")
    check(money["cube"]["double_take_equity"]
          != match_2away["cube"]["double_take_equity"],
          "and the take equity moves with the score")
    crawford = analyze_position(
        "11ccccchhhjjjjj:66666888dddddoo:N0N::B:C:4:3:5C:0", level="1ply")
    check(crawford["is_crawford"] is True, "the Crawford flag is carried through")

    print("9. bad input is rejected before the engine sees it")
    raises(lambda: analyze_position(OPENING_31_OGID, level="9ply"),
           "an unknown eval level")
    raises(lambda: analyze_position("not-a-position", level="1ply"),
           "an unparseable position id")
    check("rollout" in EVAL_LEVELS and "5ply" not in EVAL_LEVELS,
          "the level allowlist is closed, and includes rollout for CLI use")
    check("truncated2" in EVAL_LEVELS and "truncated3" in EVAL_LEVELS,
          "the truncated rollouts are offered: seconds, not minutes, so a "
          "server can answer one inline")

    print("9b. a truncated rollout runs and names itself")
    # The one level whose label is not the "<n>ply" form, so it is the one that
    # can come back mislabelled. ~0.7s.
    trunc = analyze_position(OPENING_31_OGID, level="truncated2")
    check(trunc["eval_level"] == "truncated2", "the run's level is reported as given")
    # Per-alternative levels are not the run's level, and should not be made to
    # look like it: a truncated rollout rolls out the candidates it keeps and
    # leaves the rest on the 1-ply screen that ranked them, which is exactly
    # what a reader wants to see beside a move it is being asked to trust.
    alt_levels = {a["eval_level"] for a in trunc["alternatives"]}
    check(alt_levels <= {"rollout", "1ply"} and "rollout" in alt_levels,
          f"each alternative says how it was evaluated (got {sorted(alt_levels)})")

    print("10. max_alternatives bounds a big move list")
    # A bear-in double can have ~99 legal plays; no panel shows them all.
    many = analyze_position(
        "XGID=-CBBBBB-----------bbbbbc--:0:0:1:11:0:0:0:0", level="1ply")
    check(len(many["alternatives"]) == 50,
          f"the default cap is 50 (got {len(many['alternatives'])})")
    few = analyze_position(
        "XGID=-CBBBBB-----------bbbbbc--:0:0:1:11:0:0:0:0",
        level="1ply", max_alternatives=5)
    check([a["notation"] for a in few["alternatives"]]
          == [a["notation"] for a in many["alternatives"][:5]],
          "a smaller cap truncates the same ranking, it does not reorder it")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
