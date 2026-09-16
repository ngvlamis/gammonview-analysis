# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Canonical OGXM move-notation rendering (pure stdlib, zero deps).

The one place every writer renders a checker play to its display string, so
XG-import, BGF-import, the match analyzer, and reconstruction all agree
byte-for-byte on the ``notation`` field:

  - ``bar`` for an entry from the bar (never ``25``), ``off`` for a bear-off
    (never ``0``);
  - hops collapse to a single span (``13/7 7/3`` -> ``13/3``) unless the
    checker was hit-and-continued through the intermediate point, which stays
    split with the ``*`` on the leg that hit (``8/7* 7/1``);
  - identical legs group with a repeat count (``4/2 4/2`` -> ``4/2(2)``).

This is a faithful port of ``bgsage.text_export.compute_move_notation`` (the
reference the analyzer path already renders through, via bgsage). ``gvformat``
is deliberately engine-free, so the algorithm is reproduced here rather than
imported; keep it in lockstep with the bgsage function and with the JS mirror
(``gvformat-js/src/notation.js``).

Board convention (identical to ``compute_move_notation``): the mover's own
perspective -- index ``1``..``24`` are signed point counts (positive = the
mover's checkers, negative = the opponent's), index ``25`` is the mover's bar
(a non-negative count), and borne-off checkers are implicit (derived from the
drop in the mover's on-board checker total). Opponent bar/off are irrelevant to
the string and need not be represented. Helpers below convert the XG P1-frame
and BGF absolute frame into this frame.
"""

from __future__ import annotations


def canonical_notation(
    before: list[int], after: list[int], die1: int, die2: int
) -> str:
    """Render a checker play from mover-perspective before/after boards.

    ``before``/``after`` follow the mover-perspective convention documented in
    this module. Returns notation like ``"13/7 8/7"``, ``"bar/20*"``, or
    ``"6/off(2)"`` -- an empty string for a no-move (dance).
    """
    hit_points: set[int] = set()
    for i in range(1, 25):
        if before[i] < 0 and (after[i] >= 0 or after[i] > before[i]):
            hit_points.add(i)

    from_pts: list[int] = []
    to_pts: list[int] = []

    bar_diff = after[25] - before[25]
    if bar_diff < 0:
        from_pts.extend([25] * (-bar_diff))

    for i in range(1, 25):
        wb = before[i] if before[i] > 0 else 0
        wa = after[i] if after[i] > 0 else 0
        if before[i] < 0 and after[i] > 0:
            wa = after[i]
            wb = 0
        elif before[i] > 0 and after[i] < 0:
            wb = before[i]
            wa = 0
        diff = wa - wb
        if diff > 0:
            to_pts.extend([i] * diff)
        elif diff < 0:
            from_pts.extend([i] * (-diff))

    on_board_before = before[25] + sum(v for v in before[1:25] if v > 0)
    on_board_after = after[25] + sum(v for v in after[1:25] if v > 0)
    borne_off = on_board_before - on_board_after
    to_pts.extend([0] * borne_off)

    from_pts.sort(reverse=True)
    to_pts.sort(reverse=True)

    dice = [die1, die1, die1, die1] if die1 == die2 else [die1, die2]
    moves: list[tuple[int, int, bool]] = []
    used_from = [False] * len(from_pts)
    used_to = [False] * len(to_pts)
    used_die = [False] * len(dice)

    for di, d in enumerate(dice):
        if used_die[di]:
            continue
        for fi, f in enumerate(from_pts):
            if used_from[fi]:
                continue
            expected = (25 - d) if f == 25 else (f - d)
            for ti, t in enumerate(to_pts):
                if used_to[ti]:
                    continue
                if t == expected or (expected <= 0 and t == 0):
                    is_hit = t in hit_points
                    if is_hit:
                        hit_points.discard(t)
                    moves.append((f, t, is_hit))
                    used_from[fi] = used_to[ti] = used_die[di] = True
                    break
                if used_die[di]:
                    break

    for fi, f in enumerate(from_pts):
        if used_from[fi]:
            continue
        for ti, t in enumerate(to_pts):
            if used_to[ti]:
                continue
            is_hit = t in hit_points
            if is_hit:
                hit_points.discard(t)
            moves.append((f, t, is_hit))
            used_from[fi] = used_to[ti] = True
            break

    # Split a span that crosses an intermediate point the checker *hit* on the
    # way through, so the hit shows (e.g. "24/20" with 2-2 through a blot on 22
    # -> "24/22* 22/20"). A clean span (no intermediate hit) stays collapsed.
    if die1 == die2:
        die = die1
        for mi in range(len(moves) - 1, -1, -1):
            f, t, h = moves[mi]
            dist = (25 - t) if f == 25 else (f - t)
            if dist <= die or dist % die != 0:
                continue
            n_dice = dist // die
            hit_mids: list[int] = []
            for i in range(1, n_dice):
                mid = (25 - i * die) if f == 25 else (f - i * die)
                if 1 <= mid <= 24 and mid in hit_points:
                    hit_mids.append(mid)
            if not hit_mids:
                continue
            for hm in hit_mids:
                hit_points.discard(hm)
            sub_moves: list[tuple[int, int, bool]] = []
            prev = f
            for hm in hit_mids:
                sub_moves.append((prev, hm, True))
                prev = hm
            sub_moves.append((prev, t, h))
            moves[mi:mi + 1] = sub_moves
    else:
        for mi in range(len(moves) - 1, -1, -1):
            f, t, h = moves[mi]
            dist = (25 - t) if f == 25 else (f - t)
            if dist != die1 + die2:
                continue
            for d1, d2 in [(die1, die2), (die2, die1)]:
                mid = (25 - d1) if f == 25 else (f - d1)
                if 1 <= mid <= 24 and mid in hit_points:
                    hit_points.discard(mid)
                    moves[mi:mi + 1] = [(f, mid, True), (mid, t, h)]
                    break

    moves.sort(key=lambda m: (-m[0], -m[1]))

    combined: list[list] = []
    for f, t, h in moves:
        if combined and combined[-1][0] == f and combined[-1][1] == t and combined[-1][2] == h:
            combined[-1][3] += 1
        else:
            combined.append([f, t, h, 1])

    parts = []
    for f, t, h, count in combined:
        fs = "bar" if f == 25 else str(f)
        ts = "off" if t == 0 else str(t)
        hs = "*" if h else ""
        ms = f"{fs}/{ts}{hs}"
        parts.append(f"{ms}({count})" if count > 1 else ms)
    return " ".join(parts)


def from_xg_p1_frame(board: list[int], mover_is_p1: bool) -> list[int]:
    """Convert an XG raw P1-frame board (index 1-24 signed +P1/-P2, index 0 =
    P1's bar as a positive count, index 25 = P2's bar stored negative) into the
    mover-perspective frame ``canonical_notation`` expects.

    Borne-off checkers are off-board in the P1 frame (not stored), so the
    on-board count naturally drops -- ``canonical_notation`` derives bear-offs
    from that drop, so nothing extra is needed here.
    """
    m = [0] * 26
    if mover_is_p1:
        for i in range(1, 25):
            m[i] = board[i]
        m[25] = board[0]          # P1's own bar (positive count)
    else:
        for i in range(1, 25):
            m[i] = -board[25 - i]  # P2's point i lives at P1 index 25-i, sign flip
        m[25] = -board[25]         # P2's bar stored negative -> positive count
    return m


def from_bgf_abs_frame(board: list[int], pid: int) -> list[int]:
    """Convert a BGF absolute board (index 1-24 signed +green/-red, index 25 =
    green's bar count, index 0 = red's bar count) into the mover-perspective
    frame ``canonical_notation`` expects. ``pid`` is BGF's raw mover flag
    (green = -1, red = 1)."""
    m = [0] * 26
    if pid == -1:                  # green mover (green stored positive)
        for i in range(1, 25):
            m[i] = board[i]
        m[25] = board[25]          # green's bar
    else:                          # red mover; red point i lives at abs 25-i
        for i in range(1, 25):
            m[i] = -board[25 - i]  # red stored negative -> positive; opp -> negative
        m[25] = board[0]           # red's bar
    return m
