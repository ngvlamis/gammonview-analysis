# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Regenerate tests/golden/*.fast.{gva,gvab} from the sample .mat files.

The goldens pin the whole analysis pipeline byte-for-byte, so they are valid
only for the bgsage they were produced with: an engine upgrade moves the
numbers and every golden check fails at once. That is the point -- the failure
is the signal to come here, regenerate, and *read the diff* before committing
it.

Writes exactly what ``test_ogxm_pipeline`` compares against, by the same route
(``analyze_file`` -> ``write_gvab`` -> ``read_gvab``), so the two cannot drift.

Also stamps ``tests/golden/PROVENANCE.json`` with the platform that produced
them. bgsage is not bit-reproducible across CPU architectures, so the exact
comparisons are only meaningful where the goldens were made; that file is how
the suite knows where that was. Regenerating here moves the gate here --
see ``tests/fixtures.py`` for the measurement behind it.

Run: uv run python tests/regen_golden.py            (rewrite the goldens)
     uv run python tests/regen_golden.py --check    (report drift, write nothing)
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gvanalysis.match import analyze_file
from gvformat import write_gvab, read_gvab

SAMPLES = _ROOT / "samples" / "mat"
GOLDEN = _ROOT / "tests" / "golden"
PRESET = "fast"

_PROVENANCE_NOTE = (
    "Platform gate only. The exact checks in test_ogxm_pipeline (byte-identical "
    ".gvab/.gva) and test_luck_fill (identical floats) run only where system and "
    "machine match this file; elsewhere they skip. The bgsage version is recorded "
    "for information and is NOT gated on -- an engine upgrade must fail the "
    "goldens loudly, which is the signal to regenerate and read the diff."
)


def _bgsage_version() -> str | None:
    from importlib import metadata
    try:
        return metadata.version("bgsage")
    except Exception:
        return None


def _write_provenance() -> None:
    """Record the platform these goldens were produced on."""
    doc = {
        "system": platform.system(),
        "machine": platform.machine(),
        "bgsage": _bgsage_version(),
        "generated": date.today().isoformat(),
        "note": _PROVENANCE_NOTE,
    }
    (GOLDEN / "PROVENANCE.json").write_text(json.dumps(doc, indent=2) + "\n")
    print(f"WROTE PROVENANCE.json ({doc['system']} {doc['machine']}, "
          f"bgsage {doc['bgsage']})")


def _stems() -> list[str]:
    return sorted(p.name[: -len(f".{PRESET}.gvab")]
                  for p in GOLDEN.glob(f"*.{PRESET}.gvab"))


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    stems = _stems()
    if not stems:
        print(f"nothing to do: no *.{PRESET}.gvab in {GOLDEN}")
        return 0

    changed = 0
    for stem in stems:
        mat = SAMPLES / f"{stem}.mat"
        if not mat.exists():
            print(f"SKIP  {stem}: source .mat missing")
            continue
        merged = analyze_file(str(mat), preset=PRESET, quiet=True,
                              show_progress=False, jobs=1)
        gvab = write_gvab(merged)
        gva = json.dumps(read_gvab(gvab), separators=(",", ":"))

        gvab_path = GOLDEN / f"{stem}.{PRESET}.gvab"
        gva_path = GOLDEN / f"{stem}.{PRESET}.gva"
        same = (gvab_path.read_bytes() == gvab
                and gva_path.exists() and gva_path.read_text() == gva)
        if same:
            print(f"OK    {stem}: unchanged")
            continue
        changed += 1
        if check_only:
            print(f"DRIFT {stem}: would be rewritten")
            continue
        gvab_path.write_bytes(gvab)
        gva_path.write_text(gva)
        print(f"WROTE {stem}: .gvab ({len(gvab)} bytes) + .gva")

    if check_only:
        print(f"\n{changed} of {len(stems)} goldens would change.")
        if changed:
            sys.path.insert(0, str(_ROOT / "tests"))
            from fixtures import goldens_are_native  # noqa: E402
            native, why = goldens_are_native()
            if not native:
                print(f"NOTE  {why}. Drift here is expected and is not a "
                      f"regression; regenerate only on the goldens' own machine.")
        return 1 if changed else 0
    _write_provenance()
    print(f"\n{changed} of {len(stems)} goldens rewritten.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
