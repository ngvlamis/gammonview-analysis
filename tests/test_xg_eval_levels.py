# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Tests that XG's eval levels survive a .gvab round trip.

XG names its own analysis levels -- "XG Roller++", the opening book -- and the
converter used to carry those names through verbatim ("xgroller++", "ob_v2").
Nothing could store them: GVAN encodes a level as one byte, a 4-bit depth plus
the truncated/rollout/database flags, so an unknown name encoded as 0, and 0
means "same as the header level" on read. A match dragged into the viewer
showed "xgroller++" on the alternatives XG had judged that way; the same match
saved and reloaded showed the header's plain ply depth on them instead --
silently, with no way to tell the two apart.

The converter now emits the canonical names for those codes, which describe
what XG is actually doing: the three XG Roller settings are short truncated
rollouts, and the opening book is a lookup rather than a search.

Section 3 covers the *cube* rows, which had a second version of the same
complaint: the level was there in the file and not in the document. Only the
doubler ply reported one. A take, a pass, a held cube and a missed double each
showed nothing -- and since a match holds far more held cubes than doubles,
most of the cube decisions in an XG match had no level on them. The doubler's
own level was wrong besides: it was read from the *following move record*
rather than the cube record, and XG searches a cube deeper than the roll after
it often enough that 12 of this corpus's 30 in-move doubles reported the
shallower of the two. Section 3 is stricter than its JS mirror on one point,
because it can be: `read_xg` is importable here, so the levels the document
reports are checked against the raw records they came from, in order.

Run directly:
    uv run python tests/test_xg_eval_levels.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import convert_xg  # noqa: E402
from gvformat.binary import _encode_eval_level, write_gvab  # noqa: E402
from gvformat.reader import read_gvab  # noqa: E402
from gvformat.xg import _eval_level_name, read_xg  # noqa: E402

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


def _alt_levels(doc: dict) -> list[str | None]:
    """Every alternative's eval level, in document order."""
    out: list[str | None] = []
    for game in doc["games"]:
        for ply in game["plies"]:
            for alt in (ply.get("analysis") or {}).get("alternatives") or []:
                out.append(alt.get("eval_level"))
    return out


def _cube_levels(doc: dict) -> list[tuple[str, str | None]]:
    """Every cube decision's eval level, in document order, as (what, level).

    "Cube decision" is all five shapes one takes in a document: the three
    standalone cube plies (double, take, pass) and the two sub-objects a
    checker ply carries when the cube was live (a held cube, or a double that
    should have been offered). All five are the same XG cube record
    underneath, so all five have a level to report.
    """
    out: list[tuple[str, str | None]] = []
    for game in doc["games"]:
        for ply in game["plies"]:
            analysis = ply.get("analysis")
            if not analysis:
                continue
            if 21 <= ply["action_id"] <= 23:
                out.append((f"action{ply['action_id']}", analysis.get("eval_level")))
            for key in ("missed_double", "cube_decision"):
                if analysis.get(key):
                    out.append((key, analysis[key].get("eval_level")))
    return out


def _raw_cube_levels(path: Path) -> list[str | None]:
    """The level each emitted cube record carries, straight from the file.

    One entry per cube record the converter turns into a row, in the order it
    meets them. A real double (``doubled == 1``) becomes two rows -- the offer
    and the response -- off one record, so it is listed twice; a held cube or
    missed double (``doubled == 0``) becomes one. Any other value of
    ``doubled`` is a record the converter emits nothing for.
    """
    out: list[str | None] = []
    for rtype, rdata in read_xg(path):
        if rtype != "cube":
            continue
        if rdata["doubled"] == 1:
            out.extend([_eval_level_name(rdata["level"])] * 2)
        elif rdata["doubled"] == 0:
            out.append(_eval_level_name(rdata["level"]))
    return out


def main() -> int:
    # --- 1. The codes XG uses for its non-ply levels ----------------------
    check(_eval_level_name(1000) == "truncated1", "XG Roller -> truncated1")
    check(_eval_level_name(1001) == "truncated2", "XG Roller+ -> truncated2")
    check(_eval_level_name(1002) == "truncated3", "XG Roller++ -> truncated3")
    check(_eval_level_name(998) == "database", "opening book v2 -> database")
    check(_eval_level_name(999) == "database", "opening book v1 -> database")

    # Every name the converter can produce has to encode to a non-zero byte,
    # since zero is not "unknown" but "same as the header level".
    codes = [0, 1, 2, 3, 4, 5, 6, 12, 100, 998, 999, 1000, 1001, 1002]
    unstorable = [c for c in codes if not _encode_eval_level(_eval_level_name(c))]
    check(not unstorable,
          f"every known XG level encodes to a real byte (unstorable: {unstorable})")

    # An unrecognised code says nothing rather than inventing a name no
    # `.gvab` can hold -- the same trap under a different label.
    check(_eval_level_name(7777) is None, "an unknown level code -> None, not 'level_7777'")

    # --- 2. The round trip, over the corpus -------------------------------
    files = sorted(_SAMPLES.glob("*.xg"))
    check(bool(files), f"XG samples to convert ({len(files)})")

    seen: set[str] = set()
    lossy: list[str] = []
    for path in files:
        doc = convert_xg(path)
        before = _alt_levels(doc)
        seen.update(lvl for lvl in before if lvl)
        after = _alt_levels(read_gvab(write_gvab(doc)))
        if before != after:
            differing = {(b, a) for b, a in zip(before, after) if b != a}
            lossy.append(f"{path.name}: {sorted(differing)[:3]}")

    check(not lossy, f"alternative levels survive write+read ({'; '.join(lossy[:3])})")

    # The corpus has to actually contain the levels this is about, or the
    # check above passes on a file that never exercised it.
    for level in ("truncated1", "truncated2", "truncated3", "database"):
        check(level in seen, f"the corpus exercises {level}")

    # --- 3. Cube rows report a level, and it is the cube's own ------------
    blank: list[str] = []
    cube_lossy: list[str] = []
    wrong_source: list[str] = []
    cube_seen: set[str] = set()

    for path in files:
        doc = convert_xg(path)
        before = _cube_levels(doc)

        for what, lvl in before:
            if lvl:
                cube_seen.add(lvl)
            else:
                blank.append(f"{path.name}: {what}")

        # The levels the document reports, against the records they came
        # from, in order and one for one. `convert_xg` emits a row per
        # emittable cube record and no others, so these sequences are equal
        # rather than merely compatible -- which is what makes this a check on
        # the *source* of each level and not just on its shape. A future
        # sample holding a cube record the converter declines (an unanalyzed
        # held cube, every equity zero) would fail here; the divergence in the
        # message is then the thing to read, not a reason to loosen the check.
        raw = _raw_cube_levels(path)
        emitted = [lvl for _, lvl in before]
        if emitted != raw:
            first = next(
                (i for i in range(max(len(emitted), len(raw)))
                 if emitted[i:i + 1] != raw[i:i + 1]),
                0,
            )
            wrong_source.append(
                f"{path.name}: at {first} document says {emitted[first:first + 1]}, "
                f"records say {raw[first:first + 1]} "
                f"({len(emitted)} rows vs {len(raw)} records)"
            )

        # Reading back derives the companion `cube_decision` the spec says
        # belongs beside each `missed_double`, so the two lists are not the
        # same length; compare as counted multisets of what survived.
        after = _cube_levels(read_gvab(write_gvab(doc)))
        counts_after: dict[str, int] = {}
        for what, lvl in after:
            counts_after[f"{what}={lvl}"] = counts_after.get(f"{what}={lvl}", 0) + 1
        counts_before: dict[str, int] = {}
        for what, lvl in before:
            counts_before[f"{what}={lvl}"] = counts_before.get(f"{what}={lvl}", 0) + 1
        for key, n in counts_before.items():
            if counts_after.get(key, 0) < n:
                cube_lossy.append(f"{path.name}: {key} ({n} -> {counts_after.get(key, 0)})")

    check(not blank,
          f"every analyzed cube row reports a level ({len(blank)} blank: {blank[:3]})")
    check(not cube_lossy, f"cube levels survive write+read ({cube_lossy[:3]})")
    check(not wrong_source,
          f"every cube level is its own record's ({wrong_source[:3]})")
    # Not decoration: a corpus of nothing but plain ply depths would pass the
    # checks above without ever running a cube level through the mapping.
    check(any(not lvl.endswith("ply") or not lvl[:-3].isdigit() for lvl in cube_seen),
          f"the corpus exercises a non-ply level on a cube row (saw: {sorted(cube_seen)})")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
