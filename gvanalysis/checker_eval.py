# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Screen at 1-ply, elevate a chosen few — our own checker-play driver.

bgsage's ``checker_play`` runs a 1-ply screen over every legal move, keeps a
small survivor set (the "TINY filter": within ``0.08`` of best, at most 5), and
evaluates the survivors at the full N-ply depth. That much is standard and
cheap. What follows is not: for a cubeful 2/3/4-ply analyzer it then re-runs
``bgbot_cpp.cubeful_probs_and_equity_nply`` over **every** candidate, survivor
or not, to replace the cubeless-tree probabilities with cube-aware ones
(``analyzer.py:850``). The conversion is uncapped: a 393-move position pays 393
N-ply searches where its own filter asked for 5, and across 12 sample matches it
runs 31286 of them against the ~17k this module needs. The rows it touches also
keep their stale ``eval_level: "1-ply"`` label even though their numbers are now
full-depth — which is not cosmetic, because ``game_eval`` compares that label
between the played and best move to decide whether to re-score the played one.
A fossil label fires that branch and swaps a correct tree equity for a weaker
root-Janowski estimate; measured, it moved two sample matches' PR by 0.28 and
0.12. Both halves of this are upstream bugs; see ``docs/upstream-bgsage.md``.

We don't need the extra work. GammonView already lets a viewer raise the eval
level of one move on demand, so the honest shape is a deep top group and a
1-ply tail that says so. This module does exactly that:

1. Screen every candidate at 1-ply (``_score_candidates`` — the same call
   bgsage makes, cubeful via Janowski, which is what "1-ply" means here).
2. Select the leaders — within ``ELEVATE_THRESHOLD``, at most
   ``ELEVATE_MAX_MOVES``, never fewer than ``ELEVATE_MIN_MOVES`` — plus any
   ``force_boards``: the played move, so error and PR stay apples-to-apples.
3. Elevate **only** those, via the same ``cubeful_probs_and_equity_nply`` call
   ``_convert_move`` makes — so an elevated move's probabilities and cubeful
   equity match bgsage bit for bit (measured 0.0000000 on both).
4. Leave the tail at its 1-ply numbers and label it ``1-ply`` truthfully.

Two behavioural differences from ``analyzer.checker_play``, both intended:

* Tail moves carry genuine 1-ply equities rather than N-ply equities wearing a
  1-ply label. Ranks past the elevated set therefore move. Since the elevated
  set is at least ``ELEVATE_MIN_MOVES`` wide and callers display the top ten,
  what a viewer sees is still N-ply throughout.
* bgsage's promotion loop is dropped — unnecessary once the elevated set is
  chosen wide enough. Over 1567 checker decisions the best move differed from
  bgsage's full-width answer 4 times, every one an exact tie: 0.00000 equity
  lost. PR does not move because of this.

`gvan-position` deliberately does *not* use this. It analyses one position and
ranks up to 50 alternatives, so the uncapped conversion costs one position's
worth of time and the deep ranking is the entire point -- exactly the case where
paying full-width is right. This module is for match analysis, where the same
work repeats once per ply.

Only cubeful multi-ply analyzers take this path. Rollout levels return before
bgsage's blanket conversion and already prefilter in two stages, so they are
left alone, as is any analyzer whose internals don't match what we probe for
(see ``_screen_context``) — those fall back to ``analyzer.checker_play``
unchanged. Set ``GVAN_NO_SCREEN=1`` to force the fallback everywhere.
"""

from __future__ import annotations

import os
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor

from bgsage.types import CheckerPlayResult, MoveAnalysis, Probabilities

try:  # pragma: no cover - import guard for a bgsage without these internals
    import bgbot_cpp
    from bgsage.analyzer import _FLIP_OWNER, _MultiPlyAnalyzer, resolve_owner
except Exception:  # pragma: no cover
    bgbot_cpp = None
    _FLIP_OWNER = None
    _MultiPlyAnalyzer = None
    resolve_owner = None


# How wide the elevated set is. Deliberately wider than bgsage's own TINY
# filter (`FILTER_MAX_MOVES = 5`, `FILTER_THRESHOLD = 0.08`), which it uses to
# pick which moves get the N-ply *tree* -- but not, thanks to the uncapped
# conversion, which moves get an N-ply *equity*. Since bgsage's displayed
# ordering is effectively full-width, matching its best move means screening
# less aggressively than its own filter does.
#
# Measured against bgsage's full-width answer, counting decisions where the two
# disagree on the best move and the equity that costs:
#
# 12 matches, 1567 checker decisions, 31286 legal moves between them:
#
#     thr / cap   elevations        3-ply             2-ply
#     0.08 /  5         4477   18 miss  sum 0.280   17 miss  sum 0.249
#     0.08 /  8         5288   10 miss  sum 0.221    8 miss  sum 0.170
#     0.16 /  8         7202    6 miss  sum 0.092    3 miss  sum 0.110
#     0.16 / 12         8441    1 miss  sum 0.003    0 miss
#     0.32 / 20        13941    0 miss                0 miss
#     no filter        31286    0 miss  (what bgsage effectively does)
#
# The threshold is the binding dial, not the cap: at 0.08 even an uncapped set
# still missed, because the move bgsage ranks best can screen more than 0.08
# behind the 1-ply leader. 0.16/12 is where agreement stops costing anything
# real, and 0.32/20 buys the remainder for 65% more evaluations.
#
# With ELEVATE_MIN_MOVES folded in the residue disappears: over the same 1567
# decisions at 3-ply the best move differs from bgsage's full-width answer on 4
# (0.3%), every one of them a tie -- 0.00000 equity lost, on any of them.
#
# Caveat this does not cover: a screen is a screen. On a hand-built 393-move
# position the 1-ply leader was 0.53 ahead of the move 3-ply likes best, so no
# threshold recovers it. Nothing in the sample corpus behaves that way, and no
# filtering engine (XG, GNU) would catch it either, but a full-width answer is
# the only thing that always would.
ELEVATE_THRESHOLD = 0.16
ELEVATE_MAX_MOVES = 12

# Callers display a top-N window (`game_eval` uses 10). Elevate at least that
# many regardless of the threshold, so everything a viewer actually reads is
# N-ply and correctly ordered against its neighbours, and the 1-ply tail starts
# below the fold. Without this floor the threshold alone left a 1-ply row inside
# the displayed ten on 67% of decisions -- honest, but a regression against what
# the panel shows today.
#
# Above 10 it buys back display fidelity. The screen ranks by 1-ply, so a move
# the N-ply tree would rank 4th can screen 15th and never be shown; elevating a
# few extra candidates makes that rarer. Same corpus, counting decisions where a
# move from bgsage's true N-ply top ten is absent from ours:
#
#     min   evaluations   vs bgsage   decisions   displayed slots   best move
#      10        13790        2.3x       23.7%         3.56%          exact
#      14        16928        1.8x        4.8%         0.80%          exact
#      18        19651        1.6x        2.1%         0.38%          exact
#      24        22372        1.4x        1.5%         0.25%          exact
#      32        24365        1.3x        1.2%         0.21%          exact
#
# 14 is the knee: 4.5x better display fidelity for 23% more evaluations, still
# 1.8x under bgsage. Past 18 it is paying full-width prices for a rounding
# error. The *best* move is exact at every setting, so PR never depended on
# this -- it is a display-quality dial, and that is why it is set generously.
ELEVATE_MIN_MOVES = 14


@lru_cache(maxsize=1)
def _has_root_routing() -> bool:
    """Whether this bgsage's ``cubeful_probs_and_equity_nply`` takes
    ``root_board`` (2.0 and later). Before that there was no root-pinned net,
    so there is nothing to route and the kwarg is simply omitted."""
    if bgbot_cpp is None:
        return False
    try:
        # pybind11 puts the full signature in __doc__, so the kwarg's presence
        # is readable without calling anything.
        return "root_board" in (
            bgbot_cpp.cubeful_probs_and_equity_nply.__doc__ or "")
    except Exception:
        return False


def _env_disabled() -> bool:
    return os.environ.get("GVAN_NO_SCREEN", "").strip() not in ("", "0")


class _ScreenContext:
    """The bgsage internals this driver needs, resolved once per call.

    Held as a plain object rather than reached for inline so that
    ``_screen_context`` is the single place that touches bgsage privates --
    when an upgrade moves one, exactly one function needs revisiting and every
    caller degrades to ``checker_play`` in the meantime.
    """

    __slots__ = ("inner", "owner", "opp_owner", "strategy", "n_plies",
                 "bearoff_db", "threads", "max_moves", "min_moves", "threshold",
                 "root_routing")

    def __init__(self, inner, owner, cube_owner):
        self.inner = inner
        self.owner = owner
        self.opp_owner = _FLIP_OWNER[owner]
        self.strategy = inner._strategy_1ply
        self.n_plies = inner._n_plies
        self.bearoff_db = getattr(inner, "_bearoff_db", None)
        self.threads = getattr(inner, "_parallel_threads", 1) or 1
        self.max_moves = ELEVATE_MAX_MOVES
        self.min_moves = ELEVATE_MIN_MOVES
        self.threshold = ELEVATE_THRESHOLD
        self.root_routing = _has_root_routing()


def _screen_context(analyzer, cube_owner: str) -> "_ScreenContext | None":
    """Return the screening context, or None if this analyzer can't take the
    screen-then-elevate path (rollout level, cubeless, 1-ply, or a bgsage whose
    internals have moved)."""
    if _env_disabled() or bgbot_cpp is None or _MultiPlyAnalyzer is None:
        return None
    cubeful = getattr(analyzer, "_analyzer", None)
    if cubeful is None:
        return None
    inner = getattr(cubeful, "_inner", None)
    if not isinstance(inner, _MultiPlyAnalyzer):
        return None
    # The uncapped conversion we are dodging only runs when bgsage would take
    # its cube-aware branch: multi-ply AND cubeful AND a resolvable owner.
    if int(getattr(cubeful, "_cubeful_ply", 1)) <= 1:
        return None
    if not hasattr(inner, "_score_candidates") or not hasattr(inner, "_strategy_1ply"):
        return None
    if not hasattr(bgbot_cpp, "cubeful_probs_and_equity_nply"):
        return None
    try:
        owner = resolve_owner(cube_owner)
    except Exception:
        return None
    if owner is None or _FLIP_OWNER is None or owner not in _FLIP_OWNER:
        return None
    return _ScreenContext(inner, owner, cube_owner)


def _select(scored, ctx: _ScreenContext, force_boards) -> list[int]:
    """Indices into `scored` to evaluate at full depth: everything within
    `threshold` of the screen leader (at most `max_moves`), never fewer than
    `min_moves`, plus any forced boards (one already selected is not
    duplicated)."""
    best_eq = scored[0][0]
    near = [
        i for i, item in enumerate(scored)
        if (best_eq - item[0]) < ctx.threshold
    ][: ctx.max_moves]
    chosen = list(range(min(max(len(near), ctx.min_moves), len(scored))))
    if force_boards:
        taken = {tuple(scored[i][2]) for i in chosen}
        wanted = {tuple(b) for b in force_boards}
        for i, item in enumerate(scored):
            key = tuple(item[2])
            if key in wanted and key not in taken:
                chosen.append(i)
                taken.add(key)
    return chosen


def _elevate(board_after, board, ctx, cube_value, away1, away2, is_crawford,
             jacoby, beaver) -> tuple[list[float], float, float]:
    """Full-depth probs + equities for one post-move board.

    Byte-for-byte the call ``_CubefulAnalyzer._convert_move`` makes: flip to the
    opponent's pre-roll view, swap the cube owner and the away scores, then
    invert the returned opponent-POV probabilities and negate the equity.

    ``board`` is the decision's *pre-move* board, passed as ``root_board`` so
    the search tree is routed the way bgsage routes it. Since bgsage 2.0 some
    nets are root-pinned -- the snake net picks one NN from the root position
    and holds the whole tree to it -- so omitting it silently evaluates a
    different function. Measured on eight snake seeds at 3-ply: the best move
    disagreed with ``analyzer.checker_play`` on five of them, by up to 0.19
    equity, with elevated rows off by as much as 0.70.
    """
    r = bgbot_cpp.cubeful_probs_and_equity_nply(
        bgbot_cpp.flip_board(board_after), ctx.opp_owner,
        ctx.strategy, ctx.n_plies,
        n_threads=1,
        cube_value=cube_value,
        away1=away2, away2=away1, is_crawford=is_crawford,
        jacoby=jacoby, beaver=beaver,
        bearoff_db=ctx.bearoff_db,
        **({"root_board": board} if ctx.root_routing else {}),
    )
    op = r["probs"]
    probs = [1.0 - op[0], op[3], op[4], op[1], op[2]]
    cl_eq = (2.0 * probs[0] - 1.0 + probs[1] - probs[3] + probs[2] - probs[4])
    return probs, cl_eq, -r["equity"]


def _move(board_after, equity, cubeless_equity, probs, level) -> MoveAnalysis:
    return MoveAnalysis(
        board=list(board_after),
        equity=equity,
        cubeless_equity=cubeless_equity,
        probs=Probabilities.from_list(list(probs)),
        equity_diff=0.0,          # filled in once the list is ordered
        eval_level=level,
    )


def checker_play(
    analyzer,
    board: list[int],
    die1: int,
    die2: int,
    *,
    cube_value: int = 1,
    cube_owner: str = "centered",
    away1: int = 0,
    away2: int = 0,
    is_crawford: bool = False,
    jacoby: bool = True,
    beaver: bool = True,
    force_boards: list[list[int]] | None = None,
) -> CheckerPlayResult:
    """``analyzer.checker_play`` with the elevation capped at a chosen set.

    Same return type and the same guarantees callers actually rely on:
    ``moves[0]`` is the best move, ``equity_diff`` is relative to it, and every
    legal move appears exactly once. It differs from bgsage in one way worth
    knowing: the list is two sorted groups, not one. The elevated moves come
    first in N-ply order, then the 1-ply tail in its own order, so a shallow
    equity can read higher than a deep one further up. Merging them is what
    lets a screened number take rank 0 -- measured at 1.4% of decisions -- and
    rank 0 is what feeds PR.

    Falls back to ``analyzer.checker_play`` whenever the screen-then-elevate
    path does not apply.
    """
    # Mirrors BgBotAnalyzer.checker_play: match play has no Jacoby or beavers.
    if away1 > 0 or away2 > 0:
        jacoby = False
        beaver = False

    ctx = _screen_context(analyzer, cube_owner)
    if ctx is None:
        return analyzer.checker_play(
            board, die1, die2, cube_value, cube_owner,
            away1=away1, away2=away2, is_crawford=is_crawford,
            jacoby=jacoby, beaver=beaver, force_boards=force_boards,
        )

    candidates = bgbot_cpp.possible_moves(board, die1, die2)
    if not candidates:
        return CheckerPlayResult(
            moves=[], board=list(board), die1=die1, die2=die2,
            eval_level=f"{ctx.n_plies}-ply",
        )

    scored = ctx.inner._score_candidates(
        candidates, board, cube_owner,
        cube_value=cube_value, away1=away1, away2=away2,
        is_crawford=is_crawford, jacoby=jacoby,
    )
    chosen = _select(scored, ctx, force_boards)

    def _run(i):
        return _elevate(scored[i][2], board, ctx, cube_value, away1, away2,
                        is_crawford, jacoby, beaver)

    if len(chosen) > 1 and ctx.threads > 1:
        # One thread per candidate, one thread inside each call: the cube-aware
        # tree for a single move is small enough that outer parallelism overlaps
        # better than inner, and this avoids oversubscribing.
        with ThreadPoolExecutor(max_workers=min(ctx.threads, len(chosen))) as pool:
            elevated = list(pool.map(_run, chosen))
    else:
        elevated = [_run(i) for i in chosen]

    deep_level = f"{ctx.n_plies}-ply"
    deep = [
        _move(scored[i][2], eq, cl_eq, probs, deep_level)
        for i, (probs, cl_eq, eq) in zip(chosen, elevated)
    ]
    deep.sort(key=lambda m: -m.equity)

    # The tail keeps the screen's own numbers and says so. It is NOT merged into
    # the sort above: a 1-ply cubeful equity and an N-ply one are different
    # estimators, and ordering them against each other is what lets a shallow
    # number displace a deep one. Everything here ranked below every elevated
    # move on the screen, and below the display window besides, so under the
    # deep group is where it belongs.
    chosen_set = set(chosen)
    tail = [
        _move(item[2], item[0], item[1], item[3], "1-ply")
        for i, item in enumerate(scored) if i not in chosen_set
    ]

    moves = deep + tail
    best = moves[0].equity
    for m in moves:
        m.equity_diff = m.equity - best

    return CheckerPlayResult(
        moves=moves, board=list(board), die1=die1, die2=die2,
        eval_level=deep_level,
    )
