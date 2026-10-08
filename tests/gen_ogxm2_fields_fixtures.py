# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Pin the OGXM v2 field cases for the JavaScript mirror.

``gvformat-js/test/test-ogxm2-fields.js`` reads the synthetic documents of
``tests/ogxm2_fields_cases.py`` from ``fields-docs.json``, writes each with the
JavaScript ``write_gvab`` and holds the bytes to the SHA-256 recorded in
``fields-sha256.json`` -- the two writers must agree byte for byte. ``fields.ogxm``
is a file the *reference* codec wrote using every Phase 1 field (it needs
``libogxm``, so it is generated here and committed), with the document it must
read into in ``fields.expected.json``.

Rerun after a change to either writer, and commit the result with it:
    uv run python tests/gen_ogxm2_fields_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_fields_cases as C  # noqa: E402
import ogxm2_oracle as oracle  # noqa: E402
from gvformat import ogxm2_passthrough as P  # noqa: E402
from gvformat import read_gvab, write_gvab  # noqa: E402

FIXTURES = _REPO_ROOT / "gvformat-js" / "test" / "fixtures" / "ogxm2"


def main() -> int:
    cases = C.all_cases()
    (FIXTURES / "fields-docs.json").write_text(json.dumps(cases, sort_keys=True) + "\n")
    pinned = {}
    for name, doc in cases.items():
        data = write_gvab(doc)
        pinned[name] = {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}
    (FIXTURES / "fields-sha256.json").write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n")
    print(f"wrote fields-docs.json and fields-sha256.json ({len(cases)} cases)")

    foreign = oracle.json_to_binary(C.foreign_json(oracle))
    (FIXTURES / "fields.ogxm").write_bytes(foreign)
    doc = read_gvab(foreign)
    doc.pop(P.KEY, None)
    (FIXTURES / "fields.expected.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    print(f"wrote fields.ogxm ({len(foreign)} bytes) and fields.expected.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
