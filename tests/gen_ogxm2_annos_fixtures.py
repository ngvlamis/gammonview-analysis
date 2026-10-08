# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Pin the OGXM v2 clock, video and annotation cases for the JavaScript mirror.

``gvformat-js/test/test-ogxm2-annos.js`` reads the synthetic documents of
``tests/ogxm2_annos_cases.py`` from ``annos-docs.json``, writes each with the
JavaScript ``write_gvab`` and holds the bytes to the SHA-256 in
``annos-sha256.json``. ``annos.ogxm`` is a file the *reference* codec wrote using
every field of the clock, the video and the annotations (it needs ``libogxm``,
so it is generated here and committed), with the document it must read into in
``annos.expected.json``; ``annos-edits-sha256.json`` pins what each edit of
``ogxm2_annos_cases.EDITS`` writes from it.

Rerun after a change to either writer, and commit the result with it:
    uv run python tests/gen_ogxm2_annos_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_annos_cases as C  # noqa: E402
import ogxm2_oracle as oracle  # noqa: E402
from gvformat import ogxm2_passthrough as P  # noqa: E402
from gvformat import read_gvab, write_gvab  # noqa: E402

FIXTURES = C.FIXTURES


def main() -> int:
    cases = C.all_cases()
    (FIXTURES / "annos-docs.json").write_text(json.dumps(cases, sort_keys=True) + "\n")
    pinned = {}
    for name, doc in cases.items():
        data = write_gvab(doc)
        pinned[name] = {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}
    (FIXTURES / "annos-sha256.json").write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n")
    print(f"wrote annos-docs.json and annos-sha256.json ({len(cases)} cases)")

    foreign = oracle.json_to_binary(C.foreign_json(oracle))
    assert oracle.check_json(oracle.binary_to_json(foreign)) == "ok"
    (FIXTURES / "annos.ogxm").write_bytes(foreign)
    doc = read_gvab(foreign)
    doc.pop(P.KEY, None)
    (FIXTURES / "annos.expected.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    edits = {}
    for name, edit in C.EDITS.items():
        data = write_gvab(edit(read_gvab(foreign)))
        edits[name] = {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}
    (FIXTURES / "annos-edits-sha256.json").write_text(json.dumps(edits, indent=2, sort_keys=True) + "\n")
    print(f"wrote annos.ogxm ({len(foreign)} bytes), annos.expected.json, annos-edits-sha256.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
