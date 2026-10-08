# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Pin the OGXM v2 analysis-field cases for the JavaScript mirror.

``gvformat-js/test/test-ogxm2-blocks.js`` reads the synthetic documents of
``tests/ogxm2_blocks_cases.py`` from ``blocks-docs.json``, writes each with the
JavaScript ``write_gvab`` and holds the bytes to the SHA-256 recorded in
``blocks-sha256.json`` -- the two writers must agree byte for byte -- and the
document the JavaScript reader makes of them to the one Python's does
(``settled_sha256``: the document as canonical text, see ``canon``). ``blocks.ogxm``
is a file the *reference* codec wrote using every analysis field (it needs
``libogxm``, so it is generated here and committed), with the document it must
read into in ``blocks.expected.json``.

The documents are built from the goldens in ``tests/golden``, which an engine
upgrade regenerates; rerun this with them, and commit the result with them:
    uv run python tests/gen_ogxm2_blocks_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_blocks_cases as C  # noqa: E402
import ogxm2_oracle as oracle  # noqa: E402
from gvformat import ogxm2_passthrough as P  # noqa: E402
from gvformat import read_gvab, write_gvab  # noqa: E402

FIXTURES = _REPO_ROOT / "gvformat-js" / "test" / "fixtures" / "ogxm2"


def canon(v) -> str:
    """A document as text both languages spell the same: keys sorted, a number
    that is whole as an integer and any other to eight places."""
    if isinstance(v, dict):
        return "{" + ",".join(f"{json.dumps(k)}:{canon(v[k])}" for k in sorted(v)) + "}"
    if isinstance(v, list):
        return "[" + ",".join(canon(x) for x in v) + "]"
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else f"{v:.8f}"
    return json.dumps(v, ensure_ascii=False)


def main() -> int:
    cases = C.all_cases()
    (FIXTURES / "blocks-docs.json").write_text(json.dumps(cases, sort_keys=True) + "\n")
    pinned = {}
    for name, doc in cases.items():
        data = write_gvab(doc)
        pinned[name] = {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data),
                        "settled_sha256": hashlib.sha256(canon(read_gvab(data)).encode()).hexdigest()}
    (FIXTURES / "blocks-sha256.json").write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n")
    print(f"wrote blocks-docs.json and blocks-sha256.json ({len(cases)} cases)")

    foreign = oracle.json_to_binary(C.foreign_json(oracle))
    (FIXTURES / "blocks.ogxm").write_bytes(foreign)
    doc = read_gvab(foreign)
    doc.pop(P.KEY, None)
    (FIXTURES / "blocks.expected.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    print(f"wrote blocks.ogxm ({len(foreign)} bytes) and blocks.expected.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
