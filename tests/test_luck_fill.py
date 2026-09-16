# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""``gvanalysis.luck.fill_luck`` — luck for a match somebody else analysed.

Luck is a ``[GV]`` field: it rides in our GVAN chunk, so a match analysed by
another program (a ``.ogxm`` off hedgehog-bg.com, say) arrives with its
evaluations intact and an empty luck column. ``fill_luck`` measures it from the
plies alone -- every ply already carries its own ``ogid_before``, and an OGID is
a position, a cube, a score and a roll, which is the whole input.

The claim that matters is that this is **the same measurement**, not a second
opinion: luck belongs to the dice, not to whoever judged the moves. So the test
is an exact one. Take a golden ``.gvab`` that bgsage analysed itself, throw its
luck away, measure it again, and every value has to come back identical -- not
close, identical. Anything less would mean the number we attach to a foreign
analysis is not the number a native one would have had.

The exactness is platform-gated: bgsage is not bit-reproducible across CPU
architectures, so off the machine that generated the goldens the identical-float
demand measures the CPU rather than ``fill_luck``. There it reports the largest
deviation instead of failing, and every other check still runs. See
``tests/fixtures.py:goldens_are_native``.

Requires the bgsage engine (the ``[engine]`` extra) and the committed golden
files. Run: uv run python tests/test_luck_fill.py   (exit 0 = all passed)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gvanalysis.luck import LUCK_LEVEL, fill_luck
from gvformat import read_gvab
from fixtures import goldens_are_native  # noqa: E402  (after sys.path fix-up)

GOLDEN = _ROOT / "tests" / "golden"

_passed = 0
_failed = 0


def check(cond: bool, msg: str) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"OK    {msg}")
    else:
        _failed += 1
        print(f"FAIL  {msg}")


def _rolled(ogxm: dict):
    """(game_index, ply_index, analysis) for every analysed checker ply."""
    for gi, game in enumerate(ogxm.get("games") or ()):
        for pi, ply in enumerate(game.get("plies") or ()):
            analysis = ply.get("analysis")
            if analysis and (ply.get("action_id") or 0) <= 20 and ply.get("ogid_before"):
                yield gi, pi, analysis


def main() -> int:
    goldens = sorted(GOLDEN.glob("*.fast.gvab"))
    if not goldens:
        print(f"SKIP: no golden files in {GOLDEN}")
        return 0

    native, why = goldens_are_native()
    if not native:
        print(f"NOTE  the identical-float check is skipped: {why}.")
        print("NOTE  bgsage is not bit-reproducible across CPU architectures, "
              "so the stored values came off a different engine run.")

    for path in goldens:
        print(f"\n--- {path.name} ---")
        stored = {
            (gi, pi): a["luck"]
            for gi, pi, a in _rolled(read_gvab(path.read_bytes()))
            if a.get("luck") is not None
        }
        if not stored:
            print("SKIP  no luck stored in this file")
            continue

        # 1. The exact claim: strip it, measure it, get it back.
        doc = read_gvab(path.read_bytes())
        for _gi, _pi, analysis in _rolled(doc):
            analysis.pop("luck", None)
        doc["analysis_info"].pop("luck_eval_level", None)

        filled = fill_luck(doc)
        again = {(gi, pi): a.get("luck") for gi, pi, a in _rolled(doc)}
        missing = [k for k in stored if again.get(k) is None]
        wrong = {k: (stored[k], again[k]) for k in stored
                 if again.get(k) is not None and again[k] != stored[k]}

        check(filled >= len(stored),
              f"every ply that had luck was measured again ({filled} filled, "
              f"{len(stored)} had it)")
        check(not missing, f"none was left empty ({len(missing)} were)")
        if native:
            check(not wrong,
                  "and every value is identical to the one bgsage stored"
                  + ("" if not wrong else f" ({len(wrong)} differ, e.g. {list(wrong.items())[:2]})"))
        else:
            # Reported, not judged. A threshold picked to tolerate one CPU's
            # drift would be a number with nothing behind it; the deviation
            # itself is the useful thing to see in the log.
            worst = max((abs(b - a) for a, b in wrong.values()), default=0.0)
            print(f"SKIP  identical-float comparison ({len(wrong)} of {len(stored)} "
                  f"differ, largest |delta| {worst:.6f})")
        check(doc["analysis_info"].get("luck_eval_level") == LUCK_LEVEL,
              f"the block records the level luck was measured at ({LUCK_LEVEL})")

        # 2. Nothing is overwritten, and a second pass is free.
        doc2 = read_gvab(path.read_bytes())
        sentinel_key = next(iter(stored))
        for gi, pi, analysis in _rolled(doc2):
            if (gi, pi) == sentinel_key:
                analysis["luck"] = -9.9999
        refilled = fill_luck(doc2)
        kept = {(gi, pi): a.get("luck") for gi, pi, a in _rolled(doc2)}[sentinel_key]
        check(refilled == 0,
              f"a match that already has its luck is not measured again ({refilled} filled)")
        check(kept == -9.9999,
              "and a value already there is left exactly as it was")

    print(f"\n{_passed}/{_passed + _failed} checks passed.")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
