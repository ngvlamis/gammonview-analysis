# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Accepted resignations are judged, and the cube has a screen of its own.

Both are in BMAB's configuration of XG World Class: every cube screened at
4-ply (checker plays stay at 3-ply), and resign errors counted rather than "not
calculated" -- the first in `world_class`, the second only when an analysis
asks for it (`resign_errors`), never in a preset. A resignation is one decision for each player -- the
resigner's to concede, the acceptor's to accept -- and the two signed errors are
equal and opposite, so at most one of them is ever charged.

What is pinned here, none of it needing the engine:

  * the reconstructor emits a resign decision for the position the resignation
    was made in -- after a roll the record shows was seen, that roll -- and
    none on a pending double;
  * the value conceded, in money and at a match score;
  * the evaluator charges the right side, judges a seen roll by its best play,
    skips a resignation that conceded the
    position's exact value, routes an error to the second pass, and does
    nothing at all for a preset that does not judge resignations;
  * a cube is screened by the cube screen when the preset names one;
  * the analysis survives the v2 write/read, and the PR count reads it.

Run: uv run python tests/test_resign_errors.py   (exit 0 = all passed)
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bgsage import STARTING_BOARD  # noqa: E402

from gvanalysis.game_eval import (  # noqa: E402
    _EvalCtx, _eval_cube_decision, _eval_resign_decision, _resign_equity)
from gvanalysis.ogxm_reconstructor import reconstruct_game_decisions  # noqa: E402
from gvformat import read_gvab, write_gvab  # noqa: E402
from gvformat.met import mwc2eq, mwc_anchors  # noqa: E402
from gvformat.stats import compute_aggregates  # noqa: E402

failures = 0


def check(ok: bool, what: str, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}" + (f"\n        {detail}" if detail and not ok else ""))


class _Probs:
    win = 0.2
    gammon_win = 0.0
    backgammon_win = 0.0
    gammon_loss = 0.3
    backgammon_loss = 0.01


class _Result:
    def __init__(self, level, nd, dt, dp):
        self.eval_level = level
        self.equity_nd, self.equity_dt, self.equity_dp = nd, dt, dp
        self.probs = _Probs()


class _Analyzer:
    def __init__(self, level, log, equities):
        self.level, self._log, self._eq = level, log, equities

    def cube_action(self, board, **kw):
        self._log.append(self.level)
        return _Result(self.level, *self._eq)

    def checker_play(self, board, d1, d2, *a, **kw):
        """A seen roll: its best play is worth the first equity given."""
        self._log.append(("roll", self.level, d1, d2))
        best = _Result(self.level, *self._eq)
        best.equity = self._eq[0]
        return type("R", (), {"moves": [best]})()


def ctx_for(log, equities, *, resign=True, cube_screen=True, err_thr=0.0):
    return _EvalCtx(
        analyzer=_Analyzer("2T", log, equities),
        base_analyzer=_Analyzer("3ply", log, equities),
        base_analyzer_cube=_Analyzer("4ply", log, equities) if cube_screen else None,
        mid_analyzer_checker=None, mid_analyzer_cube=None, luck_analyzer=None,
        close_threshold_checker=None, close_threshold_cube=None,
        error_threshold_checker=0.02, error_threshold_cube=err_thr,
        verbose=False, all_moves=False, count_illegal=False, level_display="2T",
        game={"game_number": 1}, resign_errors=resign)


def resign_dec(**over):
    dec = {"kind": "resign", "board": list(STARTING_BOARD), "cube_value": 1,
           "cube_owner": "centered", "away1": 0, "away2": 0, "is_crawford": False,
           "resigner": "W", "acceptor": "B", "is_resigner_p1": True,
           "resigner_on_roll": True, "points": 1, "dice": None}
    dec.update(over)
    return dec


def check_reconstruction() -> None:
    # White opens, Black plays, White resigns on their own roll.
    plies = [
        # Absolute steps: White's 8/5 6/5 is from 17 and 19; Black's 24/13 is
        # from 24 and then 18.
        {"color": 1, "action_id": 0, "d1": 3, "d2": 1,
         "moves": [{"from": 17, "pips": 3}, {"from": 19, "pips": 1}]},
        {"color": 0, "action_id": 0, "d1": 6, "d2": 5,
         "moves": [{"from": 24, "pips": 6}, {"from": 18, "pips": 5}]},
        {"color": 1, "action_id": 27},
    ]
    game = {"plies": plies, "points_won": 2, "winner": 1}
    decs = reconstruct_game_decisions(game, 0, 0, 7, "W", "B")
    res = [d for d in decs if d["kind"] == "resign"]
    check(len(res) == 1, "a resign ply after the opponent's play is one resign decision")
    if res:
        d = res[0]
        check(d["resigner_on_roll"] and d["is_resigner_p1"] and d["points"] == 2
              and (d["away1"], d["away2"]) == (7, 7) and d["acceptor"] == "B",
              f"judged from the resigner's own roll, conceding the game's points ({d})")
    # The same resignation answering a double is a pass by another name.
    pending = plies[:2] + [{"color": 0, "action_id": 21}, {"color": 1, "action_id": 27}]
    decs = reconstruct_game_decisions({"plies": pending, "points_won": 1, "winner": 0},
                                      0, 0, 7, "W", "B")
    check(not any(d["kind"] == "resign" for d in decs),
          "a resignation on a pending double is not judged")
    check(res and res[0]["dice"] is None, "after a play, the resignation is pre-roll")

    # White rolls 6-4, looks at it, and resigns without playing: that roll.
    rolled = plies[:2] + [{"color": 1, "action_id": 0, "d1": 6, "d2": 4},
                          {"color": 1, "action_id": 27}]
    decs = reconstruct_game_decisions({"plies": rolled, "points_won": 2, "winner": 0},
                                      0, 0, 7, "W", "B")
    r = [d for d in decs if d["kind"] == "resign"]
    check(len(r) == 1 and r[0]["dice"] == [6, 4] and r[0]["resigner_on_roll"],
          f"a roll seen and left unplayed is the position resigned ({r})")
    # Black rolls 2-1 and does not play it; White resigns having seen it.
    seen = plies[:1] + [{"color": 0, "action_id": 0, "d1": 2, "d2": 1},
                        {"color": 1, "action_id": 27}]
    decs = reconstruct_game_decisions({"plies": seen, "points_won": 1, "winner": 0},
                                      0, 0, 7, "W", "B")
    r = [d for d in decs if d["kind"] == "resign"]
    check(len(r) == 1 and r[0]["dice"] == [2, 1] and not r[0]["resigner_on_roll"],
          f"so is the opponent's roll, seen before resigning ({r})")
    # A dance is a turn taken: White, on the bar against a closed board, rolls
    # and cannot enter, then resigns -- Black is on roll, before Black's roll.
    start = [0] * 26                      # absolute frame: 0 = White's bar
    start[0], start[19], start[24] = 1, 14, -3
    for pt in range(1, 7):
        start[pt] = -2
    dance = [{"color": 1, "action_id": 31, "set_position": start},
             {"color": 1, "action_id": 0, "d1": 6, "d2": 6},
             {"color": 1, "action_id": 27}]
    decs = reconstruct_game_decisions({"plies": dance, "points_won": 1, "winner": 0},
                                      0, 0, 7, "W", "B")
    r = [d for d in decs if d["kind"] == "resign"]
    check(len(r) == 1 and r[0]["dice"] is None and not r[0]["resigner_on_roll"],
          f"after a dance the opponent is on roll, pre-roll ({r})")


def check_conceded_value() -> None:
    check(_resign_equity(resign_dec(points=4, cube_value=2)) == -2.0,
          "money: a gammon at cube 2 concedes 2 cube units")
    # 5-away vs 3-away, cube 1, resigning a gammon: the acceptor reaches 1-away.
    d = resign_dec(away1=5, away2=3, points=2)
    want = mwc2eq(mwc_anchors(5, 3, 2, False)[1], 5, 3, 1, False)
    check(abs(_resign_equity(d) - want) < 1e-12 and want < -1.0,
          f"match: a gammon concedes the MWC of the score it reaches ({want:.4f})")
    # Not on roll: the frame is the resigner's, so the away counts swap.
    d2 = resign_dec(away1=3, away2=5, points=2, resigner_on_roll=False)
    check(abs(_resign_equity(d2) - want) < 1e-12,
          "the resigner's frame is used whoever is on roll")


def check_evaluation() -> None:
    # Resigner on roll can double: worth max(nd, min(dt, dp)) = -0.6 playing on,
    # conceded -1 -> the resigner gave away 0.4.
    log: list = []
    res = _eval_resign_decision(resign_dec(), ctx_for(log, (-0.6, -1.5, 1.0)))
    e = res.entries[0] if res.entries else {}
    check(abs(e.get("resign_error", 0) - 0.4) < 1e-9 and abs(res.p1_err - 0.4) < 1e-9
          and res.p2_err == 0.0 and res.p1_dec == 1 and res.p2_dec == 1,
          f"a premature resignation charges the resigner, counts for both ({e})")
    check(log == ["4ply", "2T"],
          f"screened by the cube screen, rolled out on an error ({log})")
    check(e.get("probs") == [0.2, 0.0, 0.0, 0.3, 0.01],
          "probabilities are the resigner's, unchanged when they are on roll")

    # Acceptor's error: the resigner is worse than the single conceded.
    log = []
    res = _eval_resign_decision(resign_dec(points=1), ctx_for(log, (-1.3, -2.6, 1.0)))
    check(abs(res.p2_err - 0.3) < 1e-9 and res.p1_err == 0.0,
          f"accepting a resignation worth less than the position charges the acceptor "
          f"({res.p1_err}, {res.p2_err})")

    # Not on roll: the acceptor rolls, and their equity is the resigner's negative.
    log = []
    res = _eval_resign_decision(resign_dec(resigner_on_roll=False, cube_owner="opponent"),
                                ctx_for(log, (0.7, 1.6, 1.0)))
    e = res.entries[0]
    check(abs(e["equity_play"] + 0.7) < 1e-9 and e["probs"][0] == 0.8
          and e["probs"][1:] == [0.3, 0.01, 0.0, 0.0],
          f"the opponent on roll: equity and probabilities from the resigner's side ({e})")

    # A game already decided: conceded exactly what it was worth.
    log = []
    res = _eval_resign_decision(resign_dec(), ctx_for(log, (-1.0, -2.0, 1.0)))
    check(res.p1_dec == 0 and res.p2_dec == 0 and not res.entries[0]["counted"]
          and log == ["4ply"],
          "a resignation conceding the position's exact value counts for neither side")

    # Float noise off a decided game is not an error, even at a threshold of 0.
    log = []
    _eval_resign_decision(resign_dec(), ctx_for(log, (-1.0 + 1e-9, -2.0, 1.0)))
    check(log == ["4ply"], f"float noise never earns the rollout ({log})")

    # Below the cube error threshold: judged at the screen.
    log = []
    _eval_resign_decision(resign_dec(), ctx_for(log, (-0.99, -2.0, 1.0), err_thr=0.02))
    check(log == ["4ply"], f"an error under the threshold stays at the screen ({log})")

    # A seen roll: its best play, through the checker tiers (screen, then the
    # rollout past the checker threshold), and never a cube evaluation.
    log = []
    res = _eval_resign_decision(resign_dec(dice=[6, 4]), ctx_for(log, (-0.6, -1.5, 1.0)))
    e = res.entries[0]
    check(abs(e["resign_error"] - 0.4) < 1e-9 and e["dice"] == [6, 4]
          and log == [("roll", "3ply", 6, 4), ("roll", "2T", 6, 4)],
          f"a seen roll is judged by its best play ({e}, {log})")
    log = []
    res = _eval_resign_decision(resign_dec(dice=[2, 1], resigner_on_roll=False),
                                ctx_for(log, (0.99, 1.5, 1.0)))
    e = res.entries[0]
    check(abs(e["equity_play"] + 0.99) < 1e-9 and log == [("roll", "3ply", 2, 1)],
          f"the opponent's seen roll, from the resigner's side, under the checker "
          f"threshold ({e}, {log})")

    log = []
    res = _eval_resign_decision(resign_dec(), ctx_for(log, (-0.6, -1.5, 1.0), resign=False))
    check(not res.entries and log == [] and res.p1_dec == 0,
          "an analysis that does not judge resignations does nothing")


def check_cube_screen() -> None:
    dec = {"kind": "cube", "board": list(STARTING_BOARD), "cube_value": 1,
           "cube_owner": "centered", "doubled": False, "response": None,
           "doubler": "W", "responder": None, "is_doubler_p1": True,
           "away1": 0, "away2": 0, "is_crawford": False}
    log: list = []
    _eval_cube_decision(dec, ctx_for(log, (0.1, 0.0, 1.0)))
    check(log == ["4ply"], f"a cube is screened by the cube screen when there is one ({log})")
    log = []
    _eval_cube_decision(dec, ctx_for(log, (0.1, 0.0, 1.0), cube_screen=False))
    check(log == ["3ply"], f"and by the shared screen when there is not ({log})")


def check_document() -> None:
    path = _ROOT / "tests" / "golden" / "5nqfGw9bWG3deTaU.fast.gva"
    if not path.exists():
        print("SKIP  document checks: golden fixture absent")
        return
    doc = json.loads(path.read_text(encoding="utf-8"))
    where = next(((gi, pi) for gi, g in enumerate(doc["games"])
                  for pi, p in enumerate(g["plies"]) if p.get("action_id") in (27, 28)), None)
    check(where is not None, "the fixture has a resignation")
    if where is None:
        return
    gi, pi = where
    before = compute_aggregates(doc)["match"]
    judged = copy.deepcopy(doc)
    ply = judged["games"][gi]["plies"][pi]
    ply["analysis"] = {"resign_error": 0.05, "take_resign_error": -0.05,
                       "equity_loss": 0.05, "decision": True, "eval_level": "4ply", "ply": 4,
                       "eval": {"win": 0.1, "gammon_win": 0.0, "bg_win": 0.0,
                                "gammon_loss": 0.2, "bg_loss": 0.0, "equity": -1.1}}
    after = compute_aggregates(judged)["match"]
    res, acc = ("white", "black") if ply["color"] == 1 else ("black", "white")
    check(abs(after[res]["total_error"] - before[res]["total_error"] - 0.05) < 1e-9
          and after[res]["total_decisions"] == before[res]["total_decisions"] + 1,
          "the resigner is charged the error and one decision")
    check(after[acc]["total_error"] == before[acc]["total_error"]
          and after[acc]["total_decisions"] == before[acc]["total_decisions"] + 1,
          "the acceptor is charged one decision and nothing else")

    back = read_gvab(write_gvab(judged))
    got = back["games"][gi]["plies"][pi].get("analysis") or {}
    want = ply["analysis"]
    same = all(abs(got.get(k, 99) - want[k]) < 1e-6
               for k in ("resign_error", "take_resign_error", "equity_loss"))
    check(same and got.get("decision") is True and got.get("eval_level") == "4ply",
          f"the analysis survives the v2 write/read, level and flag included ({got})")
    check(compute_aggregates(back)["match"][res]["total_decisions"]
          == after[res]["total_decisions"],
          "and the count read back is the count written")


def main() -> int:
    check_reconstruction()
    check_conceded_value()
    check_evaluation()
    check_cube_screen()
    check_document()
    print(f"\n{'all passed' if not failures else str(failures) + ' failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
