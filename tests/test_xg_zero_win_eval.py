# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Tests that an evaluation XG did record survives a zero win probability.

The converter used to decide "did XG evaluate this?" by asking whether the win
probability was above zero. That is the wrong question. A play in a hopelessly
lost position reads win = 0.0 with a real gammon_loss beside it, and the last
few plies of a lost game are full of them -- so the analysis panel showed those
rows with an equity and a blank set of probabilities, including on the
top-ranked play. Every match in the sample corpus but one contains such a ply.

What the guard was really protecting against is a record XG never wrote into,
which reads as five zero probabilities *and* a zero equity. A genuine
evaluation cannot look like that: equity 0 means a roughly even game, which
cannot sit beside a zero win probability. So the pair separates them.

Mirrors gvformat-js/test/test-xg-zero-win-eval.js.

Run directly:
    uv run python tests/test_xg_zero_win_eval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import convert_xg  # noqa: E402
from gvformat.xg import _has_eval  # noqa: E402

_SAMPLES = _REPO_ROOT / "samples" / "xg"

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"FAIL  {label}")
    else:
        print(f"OK    {label}")


def main() -> int:
    # --- 1. The predicate itself ------------------------------------------
    # [win, gammon_win, bg_win, gammon_loss, bg_loss]
    check(_has_eval([0, 0, 0, 0, 0], 0) is False,
          "an unwritten record (five zeros, zero equity) is not an evaluation")
    check(_has_eval([0, 0, 0, 0.1157, 0], -1.1186) is True,
          "zero win beside a real gammon_loss is an evaluation")
    check(_has_eval([0, 0, 0, 1, 0], -2.0251) is True,
          "a certain gammon loss is an evaluation")
    check(_has_eval([0, 0, 0, 0, 0], -1) is True,
          "a certain plain loss -- all five zero -- is an evaluation, by its equity")
    check(_has_eval([0.5, 0.13, 0.006, 0.129, 0.004], 0.0152) is True,
          "an ordinary evaluation is an evaluation")

    # --- 2. The corpus ------------------------------------------------------
    files = sorted(_SAMPLES.glob("*.xg")) if _SAMPLES.is_dir() else []
    if not files:
        print(f"SKIP: no sample .xg files at {_SAMPLES}")
        print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
        return 1 if _failures else 0

    # Counted across the corpus so the assertions below can prove the corpus
    # actually reaches the case, rather than passing on files that never do.
    zero_win = 0
    all_zero = 0
    blank: list[str] = []

    for path in files:
        doc = convert_xg(path)
        for gi, game in enumerate(doc["games"], start=1):
            for pi, ply in enumerate(game["plies"]):
                for ai, alt in enumerate(ply.get("analysis", {}).get("alternatives", [])):
                    ev = alt.get("eval")
                    if not ev:
                        blank.append(f"{path.name} g{gi} ply{pi} alt{ai}")
                        continue
                    if ev["win"] == 0:
                        zero_win += 1
                    if not any(ev[k] for k in
                               ("win", "gammon_win", "bg_win", "gammon_loss", "bg_loss")):
                        all_zero += 1

    # XG lists a candidate only when it evaluated one, so every alternative the
    # converter emits should carry probabilities.
    check(not blank,
          f"every alternative in the corpus carries probabilities ({'; '.join(blank[:3])})")
    check(zero_win > 0, f"the corpus exercises zero-win evaluations ({zero_win} of them)")
    check(all_zero > 0,
          f"the corpus exercises the all-zero case a zero equity would reject ({all_zero})")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
