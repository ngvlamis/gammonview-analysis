# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""End-to-end checks for the OGXM-native analysis pipeline.

Covers the rework where OGXM is the internal representation (every input --
.mat, .gva/.ogxm, .gvab -- becomes OGXM, is analyzed from its plies, and gets
this analysis appended, preserving any it already carried):

  1. Golden regression: analyzing the sample .mat files through the OGXM path
     reproduces the committed tests/golden/*.fast.{gva,gvab} byte-for-byte.
     This one is platform-gated -- see tests/fixtures.py:goldens_are_native.
  2. Reconstruction parity: decisions rebuilt from a match's OGXM equal those
     the .mat reconstructor produces (the eval-relevant fields).
  3. Append / multi-analysis: feeding an analyzed .gva back in yields a
     two-block OGXM (analyses_info + per-ply analyses[]), and that round-trips
     through the binary (write==read==write) with min_reader_minor == 3.
  5. The producer label: analysis_info.model_id names the engine's exact build
     and carries no gammonview version -- the property that lets 1. pin bytes.

Run: uv run python tests/test_ogxm_pipeline.py   (exit 0 = all passed)
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gvanalysis.match import analyze_file
from gvformat.mat_parser import parse_mat_file
from gvformat.game_reconstructor import find_crawford_game_index, reconstruct_decisions
from gvformat.mat import mat_to_ogxm
from gvanalysis.ogxm_reconstructor import reconstruct_decisions_from_ogxm
from gvformat import write_gvab, read_gvab
from fixtures import goldens_are_native  # noqa: E402  (after sys.path fix-up)

SAMPLES = _ROOT / "samples" / "mat"
GOLDEN = _ROOT / "tests" / "golden"
PRESET = "fast"


def _dist_version(name: str) -> str | None:
    """Installed distribution version, or None when it has no metadata."""
    from importlib import metadata
    try:
        return metadata.version(name)
    except Exception:
        return None


def bgsage_version() -> str | None:
    return _dist_version("bgsage")


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


# Eval-relevant decision fields (orientation-independent; player labels excluded).
_DEC_KEYS = ("kind", "board", "dice", "board_played", "cube_value", "cube_owner",
             "away1", "away2", "is_crawford", "doubled", "response", "no_legal")


def _norm(d: dict) -> dict:
    return {k: d.get(k) for k in _DEC_KEYS}


def _golden_stems() -> list[str]:
    return sorted(p.name[: -len(f".{PRESET}.gvab")]
                  for p in GOLDEN.glob(f"*.{PRESET}.gvab"))


def main() -> int:
    stems = _golden_stems()
    if not stems:
        print(f"SKIP: no golden files in {GOLDEN}")
        return 0

    # 1. Golden regression (byte-for-byte) via the OGXM-native pipeline.
    #
    # Only where the goldens were generated. bgsage is not bit-reproducible
    # across CPU architectures -- the divergence is not rounding either, it
    # reaches best moves and cube verdicts -- so off that platform a byte
    # comparison measures the CPU, not this pipeline. It is skipped by name
    # rather than failed, and everything below still runs. The measurement and
    # the gate both live in tests/fixtures.py.
    native, why = goldens_are_native()
    if not native:
        print(f"NOTE  skipping the byte-exact golden checks: {why}.")
        print("NOTE  bgsage is not bit-reproducible across CPU architectures; "
              "regenerate only on the goldens' own machine.")
    for stem in stems:
        mat = SAMPLES / f"{stem}.mat"
        if not mat.exists():
            print(f"SKIP  {stem}: source .mat missing")
            continue
        if not native:
            print(f"SKIP  {stem}: byte-exact golden comparison (off-platform)")
            continue
        merged = analyze_file(str(mat), preset=PRESET, quiet=True, show_progress=False, jobs=1)
        gvab = write_gvab(merged)
        gva = json.dumps(read_gvab(gvab), separators=(",", ":"))
        check(gvab == (GOLDEN / f"{stem}.{PRESET}.gvab").read_bytes(),
              f"{stem}: .gvab byte-identical to golden")
        check(gva == (GOLDEN / f"{stem}.{PRESET}.gva").read_text(),
              f"{stem}: .gva byte-identical to golden")

    # 2. Reconstruction parity: mat decisions == OGXM decisions (eval fields).
    for stem in stems:
        mat = SAMPLES / f"{stem}.mat"
        if not mat.exists():
            continue
        text = mat.read_text(encoding="utf-8", errors="replace")
        md = parse_mat_file(text)
        p1, p2, ml = md["player1"], md["player2"], md["match_length"]
        cidx = find_crawford_game_index(md["games"], ml, md["crawford_rule"])
        mat_decs = []
        for i, g in enumerate(md["games"]):
            mat_decs += reconstruct_decisions(g, ml, p1, p2, is_crawford=(i == cidx))["decisions"]
        ogxm_decs = [d for gr in reconstruct_decisions_from_ogxm(mat_to_ogxm(text))
                     for d in gr["decisions"]]
        same = (len(mat_decs) == len(ogxm_decs)
                and all(_norm(a) == _norm(b) for a, b in zip(mat_decs, ogxm_decs)))
        check(same, f"{stem}: OGXM decisions match .mat decisions ({len(mat_decs)})")

    # 3. Append / multi-analysis round-trip.
    stem = stems[0]
    gva_path = GOLDEN / f"{stem}.{PRESET}.gva"
    merged = analyze_file(str(gva_path), preset=PRESET, quiet=True, show_progress=False, jobs=1)
    check(len(merged.get("analyses_info", [])) == 2,
          "append: analyzing an analyzed .gva yields 2 analysis blocks")
    check("analysis_info" in merged,
          "append: primary analysis_info mirror present")
    idx_ok = True
    saw_multi = False
    for g in merged["games"]:
        for ply in g["plies"]:
            if "analyses" in ply:
                saw_multi = True
                if [a.get("analysis_index") for a in ply["analyses"]] != [0, 1]:
                    idx_ok = False
    check(saw_multi and idx_ok, "append: decision plies carry analyses[] indexed [0, 1]")

    b1 = write_gvab(merged)
    rt = read_gvab(b1)
    b2 = write_gvab(rt)
    check(b1 == b2, "multi round-trip: write_gvab(read_gvab(b)) == b")
    check(len(rt.get("analyses_info", [])) == 2, "multi round-trip: 2 blocks survive the binary")
    rmin = struct.unpack_from("<IHHHHII", b1, 0)[4]
    check(rmin == 3, "multi: min_reader_minor == 3 (pre-1.3 readers must reject)")

    # 4. A ply with no legal move still carries an evaluation of the position it
    # leaves behind. It is the only thing a viewer can show probabilities from
    # on such a ply -- the cube decision beside it is pre-roll and describes a
    # different position -- and the analyzer used to skip these plies entirely,
    # leaving an all-zero eval. Read off the goldens (no engine): a regenerated
    # golden that lost this would otherwise pass section 1 silently.
    danced = zero = alt_count = 0
    for stem in stems:
        gva = json.loads((GOLDEN / f"{stem}.{PRESET}.gva").read_text())
        for g in gva["games"]:
            for ply in g["plies"]:
                if ply.get("d1") is None or ply.get("moves"):
                    continue
                a = ply.get("analysis") or {}
                ev = a.get("eval") or {}
                danced += 1
                if not any(ev.get(k) for k in
                           ("win", "gammon_win", "bg_win", "gammon_loss", "bg_loss")):
                    zero += 1
                if len(a.get("alternatives") or []) != 1:
                    alt_count += 1
    check(danced > 0, f"no-move plies present in the corpus ({danced})")
    check(zero == 0, f"every no-move ply carries a real eval ({zero} all-zero)")
    check(alt_count == 0,
          f"every no-move ply carries exactly one 'no move' option ({alt_count} do not)")

    # 5. The producer label. The goldens pin the exact string, but not the two
    # rules that make it safe to pin: it names the engine's build, and it names
    # no gammonview version. A hatch-vcs version here would move daily in a dev
    # checkout and go circular at release -- tagging would redden the suite the
    # tag was cut from -- so this is the check that would catch someone
    # "improving" model_id by adding one.
    from gvanalysis.match import MODEL_NAME, model_id
    mid = model_id()
    check(mid.startswith(MODEL_NAME + "/"),
          f"model_id is '{MODEL_NAME}/<engine version>' ({mid})")
    engine_ver = mid.split("/", 1)[1]
    check(engine_ver == bgsage_version(),
          f"model_id names the installed bgsage build ({engine_ver})")
    gv_ver = _dist_version("gammonview")
    check(gv_ver is None or gv_ver not in mid,
          f"model_id carries no gammonview version ({gv_ver})")
    for stem in stems:
        doc = read_gvab((GOLDEN / f"{stem}.{PRESET}.gvab").read_bytes())
        if doc["analysis_info"]["model_id"] != mid:
            check(False, f"{stem}: golden model_id is stale ({doc['analysis_info']['model_id']})")
            break
    else:
        check(True, f"every golden carries the current model_id ({mid})")

    print()
    print(f"{_passed}/{_passed + _failed} checks passed.")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
