# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Fixture resolution for the test suite.

Several tests need a real, reasonably long match to exercise round-trips,
aggregate statistics and the cube-sequence state machine. They used to read
``~/Desktop/bgtest/filias.mat`` -- a file that exists only on the author's
machine -- so the suite could not run anywhere else, CI included. They now draw
from the committed corpus under ``samples/``.

``MATCH`` names the match they use. It is a single constant so that swapping the
corpus is a one-line change. A replacement has to be chosen for its content, not
picked off the top of a sorted listing -- two requirements are easy to miss.
They are repeated here because this is where the constant lives;
``samples/README.md`` is the full specification, including what the ``.xg`` and
``.bgf`` corpora have to supply and what else to update when the corpus changes.

* **Match play, not a money session.** ``test_reconstruct_mat`` compares derived
  ``score_start`` against the original for every game. A money session (``0
  point match``) has no running score to derive, and every game mismatches.
  Four of the eleven corpus matches are money sessions.
* **A double, a take *and* a drop** (action_ids 21, 22, 23). ``test_ogxm_export``
  looks for all three. A match whose cube is never dropped does not fail that
  check -- it silently stops exercising the drop branch. ``eXBNG5wZHS5Alu3P``
  and ``XCu3RJ0UvDg_Tldl`` are the two that fall short.

The current pick is a 7-point match over 7 games and 272 plies, carrying all
three cube actions -- the closest analogue in the corpus to ``filias.mat``, which
was an 11-game match. Of the eleven, five satisfy both requirements:
``3WNK_g1Z-PLsh_HyvQ5j4a``, ``5nqfGw9bWG3deTaU``, ``MYdvw1qGuyaRUH__``,
``UL-CV6F9jMM-E1Pu`` and ``baR-U643iUDpvzTC``.

There is deliberately no fallback to "whatever .mat is present". A silent
substitution is how the drop branch would stop being tested without anyone
noticing; a loud skip that names this constant is the better failure.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

SAMPLES_DIR = _REPO_ROOT / "samples"
MAT_DIR = SAMPLES_DIR / "mat"

#: The corpus match the engine-backed tests analyze. See the module docstring
#: for what a replacement has to contain.
MATCH = "3WNK_g1Z-PLsh_HyvQ5j4a.mat"


def sample_mat() -> Path | None:
    """Return the corpus match to analyze, or ``None`` if it is not present."""
    path = MAT_DIR / MATCH
    return path if path.is_file() else None


def missing_mat_message() -> str:
    """Why the fixture is missing and what to do about it."""
    if not MAT_DIR.is_dir():
        return (
            f"SKIP: no match corpus at {MAT_DIR} -- the samples/ directory is "
            f"required to run the engine-backed tests."
        )
    return (
        f"SKIP: {MATCH} not found in {MAT_DIR}. If the corpus was replaced, "
        f"point tests/fixtures.py:MATCH at a match containing a double, a take "
        f"and a drop."
    )


# --------------------------------------------------------------------------
# Golden provenance
#
# ``tests/golden/`` is engine output, and bgsage is not bit-reproducible across
# CPU architectures. Measured 2026-09-13, same bgsage build (2.0.20260907) on
# both sides, goldens generated on macOS arm64 and replayed on Linux x86_64:
# one best move of 521 changed, one cube verdict of 296 changed, the largest
# equity gap was 0.0649, and on one match a player's PR moved 6.448 -> 6.246.
# The interpreter is not the variable -- the goldens reproduce across Python
# 3.10 and 3.13 on the machine that owns them.
#
# So the *exact* checks (``test_ogxm_pipeline``'s byte comparison,
# ``test_luck_fill``'s identical-float demand) only mean anything where the
# goldens were produced. Off that platform they are skipped by name, loudly,
# rather than failing: a red CI run that says nothing but "different CPU" is
# noise, and noise is what gets a suite ignored. Every structural check in both
# files runs everywhere.
#
# The platform is read from the goldens themselves (``PROVENANCE.json``, written
# by ``tests/regen_golden.py``), not hard-coded, so regenerating on a different
# machine moves the gate with them.
#
# The bgsage version in that file is recorded for information and is
# deliberately *not* gated on. An engine upgrade must fail the goldens loudly --
# that failure is the signal to regenerate and read the diff.
# --------------------------------------------------------------------------

GOLDEN_DIR = _REPO_ROOT / "tests" / "golden"
PROVENANCE = GOLDEN_DIR / "PROVENANCE.json"


def current_platform() -> dict[str, str]:
    """The platform identity the goldens are pinned to."""
    import platform
    return {"system": platform.system(), "machine": platform.machine()}


def golden_provenance() -> dict | None:
    """What ``PROVENANCE.json`` records, or ``None`` if it is absent/unreadable."""
    import json
    try:
        return json.loads(PROVENANCE.read_text())
    except Exception:
        return None


def goldens_are_native() -> tuple[bool, str]:
    """``(exact_checks_are_meaningful, why)``.

    True when this machine matches the one that generated the goldens. A
    missing provenance file counts as native: an old checkout, or a corpus
    someone regenerated without it, should still get the strict comparison
    rather than a silent pass.
    """
    here = current_platform()
    prov = golden_provenance()
    if prov is None:
        return True, f"no {PROVENANCE.name} beside the goldens; assuming they are native"
    there = {"system": prov.get("system"), "machine": prov.get("machine")}
    if there == here:
        return True, f"{here['system']} {here['machine']}"
    return False, (
        f"goldens were generated on {there['system']} {there['machine']}, "
        f"this is {here['system']} {here['machine']}"
    )
