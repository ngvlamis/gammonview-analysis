# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Per-ply luck for a match whose analysis was made somewhere else.

Luck is the one number in a GammonView match that no other producer's file
carries. It is a `[GV]` field -- it rides in our GVAN chunk -- so a match
analysed by another program arrives with evaluations, ranked moves and cube
equities intact and nothing at all in the luck column.

That is worth fixing rather than living with, because **luck does not belong to
the analysis**. It is a property of the dice: how good was this roll, in this
position, against the twenty-one that could have come. Two engines that
disagree about every move still agree about that, so computing it here and
attaching it to somebody else's analysis is not mixing two opinions -- it is
supplying a measurement the file simply did not take.

The measurement itself is `game_eval._compute_luck`, unchanged: a 1-ply
probability-weighted average over all 21 dice pairs against the equity the roll
actually reached. What this module adds is the ability to run it over a match
*we did not analyse*, which needs no replay at all -- every ply already carries
its own `ogid_before`, and an OGID is a position, a cube, a score and a roll.
That is the whole input. Held against a match bgsage analysed itself, the values
come back identical to the ones it stored (`tests/test_luck_fill.py`).

Cost is ~17 ms a ply, so a whole match is a few seconds -- which is why the
service answers this in the request rather than through the job queue that a
real analysis needs.
"""

from __future__ import annotations

from gvformat.ogid import parse_ogid

#: Checker plays; 21-23 are the cube and carry no roll to be lucky about.
_MAX_CHECKER_ACTION_ID = 20

#: The level luck is measured at. Not the level the match was analysed at, and
#: deliberately fixed: luck is a comparison between one roll and twenty-one
#: others, and it only means anything if all twenty-two are judged the same way.
#: bgsage's own runs use 1-ply here whatever preset they were given, so a filled
#: block and a native one are measuring the same thing.
LUCK_LEVEL = "1ply"


def _analyses_of(ply: dict):
    """Every analysis object attached to `ply`, primary first.

    A single-analysis document keeps one under `analysis`; a multi-analysis one
    keeps a list under `analyses` *and* aliases the primary onto `analysis`.
    Yielding both is intended -- the alias is the same object, so it is filled
    once, and a second block that also lacks luck gets the same value without a
    second sweep of the dice.
    """
    primary = ply.get("analysis")
    if isinstance(primary, dict):
        yield primary
    for entry in ply.get("analyses") or ():
        if isinstance(entry, dict) and entry is not primary:
            yield entry


def fill_luck(ogxm: dict, *, analyzer=None, on_progress=None, verbose: bool = False) -> int:
    """Fill in the luck of every rolled ply whose analysis has none, in place.

    Returns the number of plies that gained a value. Nothing is overwritten: a
    ply that already carries luck is passed over, so a match with one analysed
    block and one imported block ends up with both measured and neither
    disturbed.

    A ply is skipped, silently, when it has no analysis, no roll, or no
    `ogid_before` to read a position out of -- and when the engine declines to
    evaluate one of the 21 rolls, which `_compute_luck` reports as `None`
    rather than guessing. A missing luck value is an ordinary state; a wrong one
    would not be.

    @param analyzer  a prebuilt 1-ply `BgBotAnalyzer`, if the caller keeps one.
    @param on_progress  `f(done, total)`, called as plies are filled.
    """
    from .game_eval import _compute_luck

    todo = []
    for game in ogxm.get("games") or ():
        for ply in game.get("plies") or ():
            if ply.get("action_id") is None or ply["action_id"] > _MAX_CHECKER_ACTION_ID:
                continue
            if not ply.get("ogid_before"):
                continue
            wanting = [a for a in _analyses_of(ply) if a.get("luck") is None]
            if wanting:
                todo.append((ply, wanting))

    total = len(todo)
    if on_progress is not None:
        on_progress(0, total)
    if not total:
        return 0

    if analyzer is None:
        from bgsage import BgBotAnalyzer
        analyzer = BgBotAnalyzer(eval_level=LUCK_LEVEL, cubeful=True)

    filled = 0
    for done, (ply, wanting) in enumerate(todo, start=1):
        try:
            state = parse_ogid(ply["ogid_before"])
        except Exception:  # noqa: BLE001 - an unreadable id is one ply, not a failure
            if on_progress is not None:
                on_progress(done, total)
            continue
        # A dead cube has no doubling decision left in it; the checker play is
        # evaluated with the cube centered, exactly as `position.py` does.
        cube_owner = "centered" if state.cube_owner == "dead" else state.cube_owner
        # The best equity the analysis already found for the roll that came:
        # `_compute_luck` uses it to guard the sign errors 1-ply makes on
        # near-terminal boards, not as the post-roll value itself.
        hint = next((a.get("best_equity") for a in wanting if a.get("best_equity") is not None), None)
        luck, _preroll = _compute_luck(
            state.board, state.die1, state.die2,
            state.cube_value, cube_owner,
            state.away1, state.away2, state.crawford,
            analyzer, verbose=verbose, hint_eq=hint,
        )
        if luck is not None:
            for analysis in wanting:
                analysis["luck"] = luck
            filled += 1
        if on_progress is not None:
            on_progress(done, total)

    if filled:
        for info in _block_infos(ogxm):
            info.setdefault("luck_eval_level", LUCK_LEVEL)
    return filled


def _block_infos(ogxm: dict) -> list[dict]:
    """The analysis block headers, however many the document has."""
    blocks = ogxm.get("analyses_info")
    if isinstance(blocks, list) and blocks:
        return [b for b in blocks if isinstance(b, dict)]
    info = ogxm.get("analysis_info")
    return [info] if isinstance(info, dict) else []
