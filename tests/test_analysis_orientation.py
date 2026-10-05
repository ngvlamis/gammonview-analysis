# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Our analysis of a document keeps that document's White.

``to_ogxm_json`` names White alphabetically, which is right for a match it is
building from a ``.mat``. Appended onto an existing document it was wrong:
``append_analysis`` requires the two to share orientation, and a document from
another program names White by its own seats. Before Oct 2026 every such match
whose White did not sort first -- a HedgeHog ``.ogxm`` with no analysis, sent
to the server to be analyzed -- ran to the last decision and then raised
``orientation mismatch``, so the player saw the job fail at the end.

Needs the engine; one short game at ``very_quick``, a few seconds.

Run: uv run python tests/test_analysis_orientation.py   (exit 0 = all passed)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gvanalysis.match import analyze_file
from gvformat import read_gvab, write_gvab

FIXTURE = _ROOT / "gvformat-js" / "test" / "fixtures" / "ogxm2" / "resign.ogxm"

_failed = 0


def check(cond: bool, msg: str) -> None:
    global _failed
    print(f"{'OK  ' if cond else 'FAIL'}  {msg}")
    if not cond:
        _failed += 1


def main() -> int:
    doc = read_gvab(FIXTURE.read_bytes())
    # The fixture's White already sorts first; rename it so it does not.
    doc["player_white"] = "Zoe"
    check(doc["player_white"] > doc["player_black"], "White sorts after Black")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "match.gvab"
        path.write_bytes(write_gvab(doc))
        try:
            out = analyze_file(path, preset="very_quick", quiet=True)
        except ValueError as exc:
            check(False, f"analysis appends without error ({exc})")
            return 1

    check(out["player_white"] == "Zoe", "the document's White is kept")
    plies = [p for g in out["games"] for p in g["plies"]]
    before = [p for g in doc["games"] for p in g["plies"]]
    check([p.get("ogid_before") for p in plies] == [p.get("ogid_before") for p in before],
          "the match body is untouched")
    checkers = [p["analysis"] for p in plies if (p.get("analysis") or {}).get("alternatives")]
    check(bool(checkers) and all(any(x.get("is_played") for x in a["alternatives"]) for a in checkers),
          "every analysed play is found among its alternatives (not mirrored)")

    print("\nall passed" if not _failed else f"\n{_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
