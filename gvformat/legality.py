# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Rules-based backgammon move legality, independent of any import format.

Importers disagree about how (or whether) they flag a rules violation the
player actually made: XG has an explicit ``invalid_m == 2`` flag, while
BGBlitz has no flag at all -- it records the real move at a move record's top
level and lists only *legal* plays in its analysis candidates. Guessing from
candidate lists is unsound because engines cap how many candidates they store
(BGBlitz keeps 8), so a merely-bad legal play looks identical to an illegal
one.

This module decides the question from the position and dice alone, so every
importer can share one answer:

    from gvformat.legality import legal_plays, is_play_legal

Board convention (mover-relative, the natural frame for legality):

  - ``mine[p]`` / ``opp[p]`` are non-negative counts, ``p`` in 1..24, where 1
    is the mover's ace point and 24 the mover's entry point.
  - Index 25 is the *bar*: ``mine[25]`` is the mover's own bar count.
  - Checkers borne off are not tracked; a play's ``to`` of ``0`` means "off".

A "play" is the whole turn: a tuple of ``(from, to)`` sub-move pairs, sorted,
with ``to == 0`` meaning borne off. Sorting makes the comparison independent
of the order an importer happened to record the sub-moves in.
"""

from __future__ import annotations

__all__ = [
    "legal_plays", "is_play_legal", "normalize_play", "max_dice_playable",
    "board_problems",
]

_BAR = 25


def board_problems(board: list[int]) -> list[str]:
    """Reasons ``board`` could not be a real backgammon position, or ``[]``.

    Takes the *signed* 26-slot mover frame the engines use (not the
    ``mine``/``opp`` pair the rest of this module works in): points 1-24 signed
    +mover/-opponent, index 25 the mover's bar, index 0 the opponent's. Both
    bars are plain counts, unsigned -- summing sign across all 26 slots credits
    the opponent's bar to the mover, which reads a legal position as one where
    the mover has 17 checkers.

    This catches what a *board* cannot be (16 checkers on a side, 20 on a
    point), not what a *play* may not do. It is the shape of corruption a
    replay produces when it lifts a checker off a point that has none -- and
    the shape that makes an engine walk off the end of a bearoff table rather
    than fail, so anything handing boards to one wants this first.
    """
    problems = []
    points = board[1:25]
    mover = sum(c for c in points if c > 0) + board[25]
    opponent = -sum(c for c in points if c < 0) + board[0]
    for label, count in (("mover", mover), ("opponent", opponent)):
        if count > 15:
            problems.append(f"the {label} has {count} checkers (15 maximum)")
    for i, count in enumerate(board):
        if abs(count) > 15:
            problems.append(f"point {i} holds {abs(count)} checkers")
    return problems


def normalize_play(pairs) -> tuple[tuple[int, int], ...]:
    """Canonicalise ``(from, to)`` pairs: drop empties, bear-off -> 0, sort."""
    out = []
    for f, t in pairs:
        if f is None or int(f) < 0:
            continue
        ti = int(t) if t is not None and int(t) > 0 else 0
        out.append((int(f), ti))
    return tuple(sorted(out))


def _all_home(mine: list[int]) -> bool:
    """True when every mover checker sits on points 1..6 (none on bar or 7+)."""
    if mine[_BAR]:
        return False
    return not any(mine[p] for p in range(7, 25))


def _submoves(mine: list[int], opp: list[int], die: int) -> list[tuple[int, int]]:
    """Legal single-die sub-moves. ``to == 0`` means bearing off."""
    # On the bar, entering is the only thing allowed.
    if mine[_BAR] > 0:
        t = 25 - die
        if 1 <= t <= 24 and opp[t] <= 1:
            return [(_BAR, t)]
        return []

    out: list[tuple[int, int]] = []
    home = None  # computed lazily; only bear-offs need it
    for p in range(1, 25):
        if not mine[p]:
            continue
        t = p - die
        if t >= 1:
            if opp[t] <= 1:
                out.append((p, t))
            continue
        # t <= 0: bearing off, only once everything is home
        if home is None:
            home = _all_home(mine)
        if not home:
            continue
        if t == 0:
            out.append((p, 0))
        elif not any(mine[q] for q in range(p + 1, 7)):
            # Overshoot is legal only from the highest occupied point.
            out.append((p, 0))
    return out


def _apply(mine: list[int], opp: list[int], f: int, t: int):
    """Return new (mine, opp) with sub-move ``f -> t`` applied (hits removed)."""
    m = list(mine)
    o = list(opp)
    m[f] -= 1
    if t >= 1:
        m[t] += 1
        if o[t] == 1:
            o[t] = 0  # blot hit: the point is now the mover's
    return m, o


def _extend(mine, opp, dice, acc, results) -> None:
    """Depth-first over remaining dice, collecting maximal sequences."""
    extended = False
    seen: set[tuple[int, tuple[int, int]]] = set()
    for i, die in enumerate(dice):
        for mv in _submoves(mine, opp, die):
            # Identical dice produce identical branches; explore each once.
            if (die, mv) in seen:
                continue
            seen.add((die, mv))
            extended = True
            m2, o2 = _apply(mine, opp, mv[0], mv[1])
            _extend(m2, o2, dice[:i] + dice[i + 1:], acc + [(mv, die)], results)
    if not extended:
        results.append(acc)


def _raw_sequences(mine: list[int], opp: list[int], d1: int, d2: int) -> list[list]:
    results: list[list] = []
    dice = [d1] * 4 if d1 == d2 else [d1, d2]
    _extend(mine, opp, dice, [], results)
    return results


def _best_sequences(
    mine: list[int], opp: list[int], d1: int, d2: int,
) -> tuple[list, int]:
    """Maximal legal sub-move sequences for this roll, plus dice used.

    Each sequence is a list of ``(move, die)``. Enforces the two maximisation
    rules: play as many dice as possible, and when a non-double allows only one
    die, the larger one must be played if either alone is playable.
    """
    sequences = _raw_sequences(mine, opp, d1, d2)
    if not sequences:
        return [], 0
    longest = max(len(s) for s in sequences)
    if longest == 0:
        return [], 0

    best = [s for s in sequences if len(s) == longest]

    # Non-double that can only ever play one die: the larger die wins out.
    if d1 != d2 and longest == 1:
        high = max(d1, d2)
        high_plays = [s for s in best if s[0][1] == high]
        if high_plays:
            best = high_plays

    return best, longest


def legal_plays(
    mine: list[int], opp: list[int], d1: int, d2: int,
) -> tuple[set[tuple[tuple[int, int], ...]], int]:
    """All legal plays for this roll, plus how many dice a legal play must use.

    Returns ``(plays, dice_used)`` where ``plays`` is a set of canonical plays
    (see ``normalize_play``). A forced dance is ``(set(), 0)``.
    """
    best, longest = _best_sequences(mine, opp, d1, d2)
    return {normalize_play(mv for mv, _ in s) for s in best}, longest


def max_dice_playable(mine: list[int], opp: list[int], d1: int, d2: int) -> int:
    """Number of dice any legal play must consume (0 == forced dance)."""
    return _best_sequences(mine, opp, d1, d2)[1]


def _outcome_key(mine: list[int], pairs) -> tuple[int, ...]:
    """Sorted key of the mover's checker positions after applying ``pairs``.

    Each ``(from, to)`` pair moves one mover checker (``to == 0`` bears it off);
    the effect is summed, so chained sub-moves (24/18 then 18/15) and the single
    combined pair (24/15) that importers like BGBlitz record for them collapse
    to the same outcome. Only the mover's own checkers matter for legality.
    """
    m = list(mine)
    for f0, t0 in pairs:
        f = int(f0)
        t = int(t0) if t0 is not None and int(t0) > 0 else 0
        if 1 <= f <= 25:
            m[f] -= 1
        if 1 <= t <= 25:
            m[t] += 1
    return tuple(p for p in range(1, 26) for _ in range(m[p]))


def is_play_legal(
    mine: list[int], opp: list[int], d1: int, d2: int, play,
) -> bool:
    """True if ``play`` (an iterable of ``(from, to)`` pairs) is legal here.

    A play is legal exactly when it leaves the mover's checkers where some legal
    (maximal) play would. Comparing the resulting position -- rather than the
    sub-move spelling -- makes this agnostic to how many dice a checker's move
    was recorded as (per-die 24/18 18/15 vs combined 24/15, or a two-die bear-off
    written as one ``(f, off)``), while still rejecting an under-play: a move that
    leaves a die unplayed lands somewhere no maximal play can reach.

    An empty ``play`` means "no checkers moved", legal only in a genuine dance.
    """
    play = list(play)
    best, longest = _best_sequences(mine, opp, d1, d2)
    target_norm = normalize_play(play)
    if not target_norm:
        return longest == 0
    plays = {normalize_play(mv for mv, _ in s) for s in best}
    if target_norm in plays:
        return True
    target = _outcome_key(mine, play)
    return any(_outcome_key(mine, [mv for mv, _ in s]) == target for s in best)
