# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""`checker_eval.checker_play` elevates fewer moves without changing the numbers.

bgsage re-runs the N-ply cube-aware tree over *every* legal move, not just the
ones its own filter kept (docs/upstream-bgsage.md, issue 1).
`gvanalysis.checker_eval` screens at 1-ply and elevates only a chosen set,
using the same `cubeful_probs_and_equity_nply` call bgsage uses.

What must hold:

  * an elevated move's probabilities, cubeful equity and cubeless equity match
    bgsage exactly -- it is literally the same C++ call on the same board;
  * ...including the `root_board=` a root-pinned net is chosen from, which is
    invisible everywhere except the snake and so gets its own check;
  * every move carries a label that describes how it was actually evaluated,
    so a caller comparing two rows' `eval_level` is comparing something real;
  * the played move is elevated when forced, whatever the screen thought of it;
  * an analyzer we cannot screen (rollout level, cubeless, 1-ply) falls back to
    `checker_play` and is unchanged;
  * GVAN_NO_SCREEN=1 forces that fallback everywhere.

Calling the same C++ entry point is what makes exact parity a fair thing to
demand -- and what makes a *signature* change (bgsage 2.0's `root_board=`) a
silent correctness bug rather than a loud one.

Run: uv run python tests/test_screened_checker.py   (exit 0 = all passed)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bgsage import BgBotAnalyzer, STARTING_BOARD
from bgsage.board import possible_moves

from gvanalysis import checker_eval
from gvanalysis.checker_eval import checker_play as screened

EPS = 1e-9


def _spread_board() -> list[int]:
    """A wide contact position: 393 legal moves on 2-2, enough that bgsage's
    uncapped conversion is visible in wall-clock time."""
    b = [0] * 26
    for pt in (6, 8, 9, 11, 13, 17, 19, 20, 22, 23):
        b[pt] = 1
    b[13], b[8], b[6] = 3, 2, 2
    for pt in (1, 2, 4, 5, 7, 12, 14, 16, 18, 21):
        b[pt] = -1
    b[12], b[7], b[5] = -3, -2, -2
    return b


def _snake_board(which: int) -> list[int]:
    """A snake: a far-side prime holding a straggler, with the opponent's other
    checkers crunched home. Two seeds from bgsage's own snake benchmark.

    These exist to pin *root routing*. Since bgsage 2.0 the snake NN is chosen
    from the position a search tree is rooted at and held for the whole tree
    (`root_board=` on `cubeful_probs_and_equity_nply`), unlike every other net,
    which is picked per node. A screen that omits the kwarg silently evaluates
    a different function -- see `check_root_routing` below.
    """
    return {
        1: [0, 0, 0, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            1, 0, 1, 0, 2, 3, 3, 2, 2, 1, -7, -7, 0],
        2: [0, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            1, 1, 0, 0, 3, 3, 3, 2, 2, -5, -5, -4, 0],
    }[which]


# (name, board, dice, level, kwargs)
CASES = [
    ("opening 65, money, 3-ply", list(STARTING_BOARD), (6, 5), "3ply", {}),
    ("opening 31, 3away/5away, 3-ply", list(STARTING_BOARD), (3, 1), "3ply",
     dict(away1=3, away2=5)),
    ("wide contact, money, 3-ply", _spread_board(), (2, 2), "3ply", {}),
    ("wide contact, 2away/7away, cube 2 owned, 2-ply", _spread_board(), (2, 2),
     "2ply", dict(away1=2, away2=7, cube_value=2, cube_owner="player")),
    ("opening 65, money, 4-ply", list(STARTING_BOARD), (6, 5), "4ply", {}),
    ("snake seed 1, money, 3-ply", _snake_board(1), (6, 3), "3ply", {}),
    ("snake seed 2, money, 3-ply", _snake_board(2), (6, 3), "3ply", {}),
]


def _ref(analyzer, board, d1, d2, kw, force=None):
    return analyzer.checker_play(
        board, d1, d2, kw.get("cube_value", 1), kw.get("cube_owner", "centered"),
        away1=kw.get("away1", 0), away2=kw.get("away2", 0),
        is_crawford=kw.get("is_crawford", False), force_boards=force,
    )


def check_parity() -> int:
    failures = 0
    for name, board, (d1, d2), level, kw in CASES:
        a = BgBotAnalyzer(eval_level=level, cubeful=True)
        ref = _ref(a, board, d1, d2, kw)
        got = screened(a, board, d1, d2, **kw)
        refmap = {tuple(m.board): m for m in ref.moves}

        n_legal = len(possible_moves(board, d1, d2))
        deep = [m for m in got.moves if m.eval_level != "1-ply"]

        # Same move set, nothing dropped or invented.
        ok_set = {tuple(m.board) for m in got.moves} == set(refmap)
        ok_n = len(got.moves) == n_legal

        dp = de = dc = 0.0
        for m in deep:
            r = refmap[tuple(m.board)]
            dp = max(dp, max(abs(x - y) for x, y in
                             zip(m.probs.to_list(), r.probs.to_list())))
            de = max(de, abs(m.equity - r.equity))
            dc = max(dc, abs(m.cubeless_equity - r.cubeless_equity))
        ok_num = max(dp, de, dc) < EPS

        # Elevating fewer moves is the whole point.
        ok_cap = len(deep) <= max(checker_eval.ELEVATE_MAX_MOVES,
                                  checker_eval.ELEVATE_MIN_MOVES)
        # Two groups, each sorted, elevated first. NOT one globally sorted
        # list: a 1-ply equity and an N-ply equity are different estimators,
        # and merging them lets a screened number take rank 0 -- measured at
        # 1.4% of decisions, which would feed straight into PR.
        n_deep = len(deep)
        ok_groups = all(m.eval_level != "1-ply" for m in got.moves[:n_deep])
        d_eqs = [m.equity for m in got.moves[:n_deep]]
        t_eqs = [m.equity for m in got.moves[n_deep:]]
        ok_sort = (ok_groups
                   and all(x >= y - EPS for x, y in zip(d_eqs, d_eqs[1:]))
                   and all(x >= y - EPS for x, y in zip(t_eqs, t_eqs[1:])))
        # The displayed window must be entirely N-ply.
        ok_window = n_deep >= min(n_legal, checker_eval.ELEVATE_MIN_MOVES)
        eqs = d_eqs + t_eqs
        ok_diff = (abs(got.moves[0].equity_diff) < EPS
                   and all(abs(m.equity_diff - (m.equity - eqs[0])) < EPS
                           for m in got.moves))

        ok = (ok_set and ok_n and ok_num and ok_cap and ok_sort and ok_diff
              and ok_window)
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {n_legal} legal, "
              f"{len(deep)} elevated, max|delta| probs={dp:.1e} "
              f"cubeful={de:.1e} cubeless={dc:.1e}")
        if not ok:
            print(f"        set={ok_set} count={ok_n} numbers={ok_num} "
                  f"cap={ok_cap} sorted={ok_sort} diff={ok_diff} "
                  f"window={ok_window}")
    return failures


def check_labels_are_honest() -> int:
    """Every row says how it was really evaluated -- no N-ply number wearing a
    1-ply label, which is what trips `game_eval`'s played-vs-best level check."""
    failures = 0
    a = BgBotAnalyzer(eval_level="3ply", cubeful=True)
    board, (d1, d2) = _spread_board(), (2, 2)
    ref = _ref(a, board, d1, d2, {})
    got = screened(a, board, d1, d2)

    refmap = {tuple(m.board): m for m in ref.moves}
    # Rows we did NOT elevate must differ from bgsage's N-ply numbers -- if
    # they matched, the screen would not be doing anything and this test would
    # be vacuous.
    tail = [m for m in got.moves if m.eval_level == "1-ply"]
    moved = sum(1 for m in tail
                if abs(m.equity - refmap[tuple(m.board)].equity) > 1e-6)
    ok = bool(tail) and moved == len(tail)
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  1-ply tail carries 1-ply numbers: "
          f"{moved}/{len(tail)} differ from bgsage's N-ply values")

    # bgsage used to label these N-ply numbers "1-ply" (docs/upstream-bgsage.md
    # issue 2) -- a fossil label that fed `game_eval`'s played-vs-best level
    # check and swapped a correct error for a `post_move_analytics` estimate,
    # worth 0.28 PR on one sample match. Fixed upstream in bgsage 2.0, which
    # assigns `eval_level` alongside the numbers in the conversion loop.
    #
    # Asserted rather than deleted: it fails loudly if a future bgsage
    # reintroduces the mislabel, and `game_eval` still trusts these labels.
    fossil = sum(1 for m in ref.moves if m.eval_level == "1-ply")
    ok = fossil == 0
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  bgsage labels every row honestly: "
          f"{fossil} mislabelled (issue 2, fixed upstream in 2.0)")

    labels = {m.eval_level for m in got.moves}
    ok = labels <= {"1-ply", "3-ply"}
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  only honest labels emitted: {sorted(labels)}")
    return failures


def check_force_boards() -> int:
    """The played move is elevated even when the screen ranks it nowhere near
    the top -- error and PR compare it against the best move at equal depth."""
    failures = 0
    a = BgBotAnalyzer(eval_level="3ply", cubeful=True)
    board, (d1, d2) = _spread_board(), (2, 2)
    plain = screened(a, board, d1, d2)
    worst = plain.moves[-1].board          # bottom of a 393-move list
    forced = screened(a, board, d1, d2, force_boards=[worst])

    m = next(x for x in forced.moves if tuple(x.board) == tuple(worst))
    ok_level = m.eval_level == "3-ply"
    ref = _ref(a, board, d1, d2, {}, force=[worst])
    r = next(x for x in ref.moves if tuple(x.board) == tuple(worst))
    ok_num = abs(m.equity - r.equity) < EPS
    ok = ok_level and ok_num
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  forced board elevated: "
          f"level={m.eval_level} equity={m.equity:+.6f} (bgsage {r.equity:+.6f})")

    # Forcing a board already inside the elevated set must not duplicate it.
    best = plain.moves[0].board
    again = screened(a, board, d1, d2, force_boards=[best])
    ok = len(again.moves) == len(plain.moves)
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  forcing an already-elevated board is a "
          f"no-op: {len(again.moves)} rows (want {len(plain.moves)})")
    return failures


def check_fallbacks() -> int:
    """Anything we cannot screen must come back byte-identical to bgsage."""
    failures = 0
    board, (d1, d2) = list(STARTING_BOARD), (6, 5)

    for name, analyzer in (
        ("1-ply analyzer", BgBotAnalyzer(eval_level="1ply", cubeful=True)),
        ("cubeless analyzer", BgBotAnalyzer(eval_level="3ply", cubeful=False)),
    ):
        ctx = checker_eval._screen_context(analyzer, "centered")
        ok = ctx is None
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name} declines the screen")
        if ok:
            ref = _ref(analyzer, board, d1, d2, {})
            got = screened(analyzer, board, d1, d2)
            same = (len(ref.moves) == len(got.moves) and all(
                tuple(x.board) == tuple(y.board)
                and abs(x.equity - y.equity) < EPS
                and x.eval_level == y.eval_level
                for x, y in zip(ref.moves, got.moves)))
            failures += not same
            print(f"{'PASS' if same else 'FAIL'}  {name} passes through unchanged")

    a = BgBotAnalyzer(eval_level="3ply", cubeful=True)
    os.environ["GVAN_NO_SCREEN"] = "1"
    try:
        ok = checker_eval._screen_context(a, "centered") is None
        got = screened(a, board, d1, d2)
        ref = _ref(a, board, d1, d2, {})
        ok = ok and all(x.eval_level == y.eval_level
                        for x, y in zip(ref.moves, got.moves))
    finally:
        del os.environ["GVAN_NO_SCREEN"]
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  GVAN_NO_SCREEN=1 forces the fallback")

    # And the escape hatch really is off by default.
    ok = checker_eval._screen_context(a, "centered") is not None
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  screening is on by default afterwards")
    return failures


def check_root_routing() -> int:
    """The screen must pass `root_board=`, and it must matter.

    bgsage 2.0 root-pins the snake NN: `_CubefulAnalyzer._convert_move` hands
    `cubeful_probs_and_equity_nply` the decision's pre-move board and the whole
    search tree is held to the net chosen from it. Every other net is picked
    per node, so on ordinary positions the kwarg changes nothing and its
    absence is invisible -- which is exactly why it needs its own test.

    Two halves, and both must hold:

      * with root routing on (the default) the elevated numbers match bgsage
        exactly, as `check_parity` already asserts for the snake cases;
      * with it suppressed they do NOT. Without this half the test would still
        pass if `_has_root_routing` started returning False on every bgsage,
        and the screen would quietly drift from the engine again.

    Measured when the kwarg was first missed, eight snake seeds at 3-ply: the
    best move disagreed on five, by up to 0.19 equity, with elevated rows off
    by as much as 0.70.
    """
    failures = 0
    if not checker_eval._has_root_routing():
        print("SKIP  root routing: this bgsage takes no root_board= "
              "(pre-2.0); nothing to pin")
        return 0

    a = BgBotAnalyzer(eval_level="3ply", cubeful=True)
    for which, (d1, d2) in ((1, (5, 2)), (2, (6, 3))):
        board = _snake_board(which)
        ref = _ref(a, board, d1, d2, {})
        refmap = {tuple(m.board): m for m in ref.moves}

        def _worst(moves) -> float:
            worst = 0.0
            for m in moves:
                if m.eval_level == "1-ply":
                    continue
                r = refmap.get(tuple(m.board))
                if r is None:
                    continue
                worst = max(worst, abs(m.equity - r.equity),
                            max(abs(x - y) for x, y in
                                zip(m.probs.to_list(), r.probs.to_list())))
            return worst

        on = _worst(screened(a, board, d1, d2).moves)

        checker_eval._has_root_routing.cache_clear()
        orig = checker_eval._has_root_routing
        checker_eval._has_root_routing = lambda: False
        try:
            off = _worst(screened(a, board, d1, d2).moves)
        finally:
            checker_eval._has_root_routing = orig
            checker_eval._has_root_routing.cache_clear()

        ok = on < EPS and off > 1e-3
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  snake seed {which} root-routed: "
              f"max|delta| vs bgsage on={on:.1e} off={off:.1e} "
              f"(on must be 0, off must not)")
    return failures


def check_no_legal_moves() -> int:
    """A dance returns bgsage's single unchanged-board entry, not an empty list
    -- `game_eval` shows that ply's probabilities from it."""
    board = [0] * 26
    board[25] = 2                 # both on the bar, opponent holds every point
    for pt in (19, 20, 21, 22, 23, 24):
        board[pt] = -2
    a = BgBotAnalyzer(eval_level="3ply", cubeful=True)
    got = screened(a, board, 1, 2)
    ref = _ref(a, board, 1, 2, {})
    ok = len(got.moves) == len(ref.moves)
    print(f"{'PASS' if ok else 'FAIL'}  no legal move: {len(got.moves)} rows "
          f"(bgsage {len(ref.moves)})")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    failures += check_parity()
    failures += check_labels_are_honest()
    failures += check_force_boards()
    failures += check_fallbacks()
    failures += check_root_routing()
    failures += check_no_legal_moves()
    print(f"\n{'all passed' if not failures else str(failures) + ' failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
