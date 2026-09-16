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
from gvformat.xg import _eval_level_name  # noqa: E402

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

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
