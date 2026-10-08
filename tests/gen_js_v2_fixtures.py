# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Pin what the Python v2 writer makes of each v1 sample, for the JS mirror.

``gvformat-js/test/test-ogxm2-writer.js`` reads every ``samples/gv/*.gvab``
(v1), writes it with the JavaScript ``write_gvab`` (v2), and checks the bytes
against the SHA-256 recorded here -- the two writers must agree byte for byte,
because a file is the same file whichever side of GammonView wrote it.

Rerun after a change to either writer, and commit the result with it:
    uv run python tests/gen_js_v2_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import read_gvab, write_gvab  # noqa: E402

OUT = _REPO_ROOT / "gvformat-js" / "test" / "fixtures" / "ogxm2" / "writer-sha256.json"


def main() -> int:
    pinned = {}
    for f in sorted((_REPO_ROOT / "samples" / "gv").glob("*.gvab")):
        data = write_gvab(read_gvab(f.read_bytes()))
        pinned[f.name] = {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}
    OUT.write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(_REPO_ROOT)} ({len(pinned)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
