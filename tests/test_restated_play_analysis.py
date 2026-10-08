# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Our own analyzer judges a play it could only record as a position.

An illegal play that no dice ply can encode is written as the board it produced
-- a set-position ply (action 31) carrying the roll's dice, a *restated play*
(see ``gvformat.export.set_position_ply``). The converters carry the source's
analysis onto such a ply; this is the other half, the analysis **we** produce.

Two things were wrong before Oct 2026, and the first hid the second:

1. ``analyze_file`` on either of these matches raised
   ``analysis/ply count mismatch`` and produced nothing at all. The exporter
   re-derives a ply's steps from the board diff, and a 4-4 bear-off can match as
   a single 11-pip span that no step can hold -- so a perfectly *legal* play was
   restated as a set position, and the result no longer lined up with the
   document it was analyzed from. The reconstructor now hands the ply's own steps
   over (``move_steps``), which is the authoritative record: they came out of a
   ply record, so they fit one.
2. ``ogxm_reconstructor`` raised no decision for the restated play itself, so our
   analysis simply had a hole where the source's had an evaluation. It is a turn
   like any other -- the player was on roll, faced the cube, and played something
   -- so it reconstructs as a checker decision with the stated board standing in
   for the play, and ``game_eval`` takes its illegal-play path, which is exactly
   what the ply records.

Both matches are in the corpus in two forms. The ``.mat`` is used for most of
this because it carries no analysis of its own, so anything on the ply afterwards
is ours; the last section analyzes one of them as a converted ``.gvab``, which is
the shape a server is handed and the one case where both documents hold an
analysis for the same restated play.

Needs the engine and analyses three matches, so it costs ~30s at ``very_quick``.

Run: uv run python tests/test_restated_play_analysis.py   (exit 0 = all passed)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gvanalysis.loader import load_ogxm
from gvanalysis.match import analyze_file
from gvformat import compute_aggregates, read_gvab, write_gvab

#: The two corpus matches whose illegal play needs restating, and where it is.
#: ``hQ8sVn2LbTdF4wRm`` sends a checker backwards (a 6-5 played 15/10 14/8 6/8);
#: ``rK7pXm4TqLb9NzWd`` hops ten pips in one span (a 3-3 played 13/3 7/4).
MATCHES = [
    ("hQ8sVn2LbTdF4wRm", (1, 37), "the backwards hop"),
    ("rK7pXm4TqLb9NzWd", (11, 18), "the ten-pip hop"),
]
PRESET = "very_quick"

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


def analyze_file_bytes(gvab: bytes) -> dict:
    """``analyze_file`` over bytes, via a temp file -- the API takes a path."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "match.gvab"
        path.write_bytes(gvab)
        return analyze_file(str(path), preset=PRESET, quiet=True,
                            show_progress=False, jobs=1)


def _restated(doc: dict) -> list[tuple[int, int, dict]]:
    """Every restated play in a document: action 31 with the roll still on it."""
    return [(gi, pi, ply)
            for gi, game in enumerate(doc.get("games") or ())
            for pi, ply in enumerate(game.get("plies") or ())
            if ply.get("action_id") == 31 and ply.get("d1") is not None]


def main() -> int:
    ran = 0
    for stem, at, what in MATCHES:
        mat = _ROOT / "samples" / "mat" / f"{stem}.mat"
        if not mat.exists():
            print(f"SKIP  {what} ({stem}.mat is not in samples/)")
            continue
        ran += 1

        # Analyzing it at all is the first assertion: this raised before.
        doc = analyze_file(str(mat), preset=PRESET, quiet=True,
                           show_progress=False, jobs=1)

        found = _restated(doc)
        check(len(found) == 1 and (found[0][0], found[0][1]) == at,
              f"{stem}: one restated play, still at {at}, where {what} was")
        if not found:
            continue
        gi, pi, ply = found[0]

        analysis = ply.get("analysis") or {}
        check(bool(analysis), f"{stem}: and we analyzed it")
        check(analysis.get("illegal_move") is True,
              f"{stem}: flagged illegal -- no legal move reaches the stated board")
        check(analysis.get("decision") is False,
              f"{stem}: and counted as no decision, so PR is untouched")
        check(len(analysis.get("alternatives") or []) > 1,
              f"{stem}: with the plays that were available instead")
        played = [a for a in analysis.get("alternatives") or () if a.get("is_played")]
        check(len(played) == 1 and played[0].get("notation"),
              f"{stem}: the played candidate named, though it has no steps to draw")
        check(played and not played[0].get("move"),
              f"{stem}: and no steps, because nothing can express the play")
        check("luck" in analysis,
              f"{stem}: the roll's luck measured, which the play cannot change")

        # The aggregates read it as the checker ply it stands in for.
        agg = compute_aggregates(doc)["match"]
        check(agg["illegal_moves"] >= 1, f"{stem}: the illegal-move count sees it")
        side = "white" if ply.get("color") else "black"
        check(agg[side]["luck_rolls"] >= 1, f"{stem}: and the luck totals do")

        # Through the binary, which is what a server returns.
        back = read_gvab(write_gvab(doc))["games"][gi]["plies"][pi]
        ba = back.get("analysis") or {}
        check(back.get("action_id") == 31
              and ba.get("illegal_move") is True
              and ba.get("decision") is False
              and len(ba.get("alternatives") or []) == len(analysis["alternatives"]),
              f"{stem}: and a saved match brings all of it back")

        # The legal play the exporter used to restate. Every ply the input had as
        # a checker play must still be one -- our analysis has to line up with the
        # document it was made from, ply for ply, or `append_analysis` cannot
        # place it (which is how this was found).
        base = load_ogxm(str(mat))
        base_shape = [(p.get("action_id"), p.get("color"))
                      for g in base["games"] for p in g["plies"]]
        our_shape = [(p.get("action_id"), p.get("color"))
                     for g in doc["games"] for p in g["plies"]]
        check(base_shape == our_shape,
              f"{stem}: and every other ply kept the shape the input gave it")

    if not ran:
        print("SKIP: neither match is in samples/")
        return 0

    # --- the production path: a converted .gvab that already carries analysis --
    #
    # This is what a server is handed (GammonView converts the .xg in the browser
    # and uploads the binary), and it is the case `append_analysis` has to place:
    # the source analyzed the restated play too, so both documents hold a ply
    # there and the pairing has to line them up rather than refuse the file.
    stem, at, what = MATCHES[0]
    xg = _ROOT / "samples" / "xg" / f"{stem}.xg"
    if not xg.exists():
        print(f"SKIP  the converted .gvab ({stem}.xg is not in samples/)")
    else:
        from gvformat.xg import convert_xg

        doc = analyze_file_bytes(write_gvab(convert_xg(xg)))
        found = _restated(doc)
        check(len(found) == 1 and (found[0][0], found[0][1]) == at,
              f"the .gvab of {stem}: the restated play survives the round trip")
        blocks = doc.get("analyses_info") or []
        check(len(blocks) == 2,
              f"and the result carries both analyses ({len(blocks)})")
        if found:
            entries = found[0][2].get("analyses") or []
            check([e.get("analysis_index") for e in entries] == [0, 1],
                  "with XG's judgement of the play and ours side by side on it")
            check(all(e.get("illegal_move") is True and e.get("decision") is False
                      for e in entries),
                  "both calling it illegal and neither counting it")

    print(f"\n{_passed}/{_passed + _failed} checks passed.")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
