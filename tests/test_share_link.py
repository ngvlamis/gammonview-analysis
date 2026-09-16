# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Round-trip harness for the share codec and the GammonView URL wrapper.

The codec (``gvformat.share.encode_match`` / ``decode_match``) is site-agnostic;
the URL wrapper (``gvanalysis.share.share_link`` and its constants) adds the one
gammonview.com specific and mirrors GammonView's ``sharelink.js``.

A shareable link carries the whole match in its URL fragment: OGXM -> ``.gvab``
-> zlib-deflate -> base64url. The correctness properties mirror the JS tests:

  * ``encode_match``/``decode_match`` are exact inverses (through the same
    canonicalizing ``.gvab`` round-trip the on-disk outputs use);
  * the payload is URL-safe and within ``MAX_SHARE_LENGTH``;
  * a payload this module produces decodes with the browser's path
    (``base64url -> zlib.decompress -> read_gvab``), so links interoperate with
    ``pako.inflate``;
  * a corrupt payload raises rather than returning garbage.

Fixtures are the repo's pure-codec samples (no engine needed): every
``samples/gv/*.gvab``. The corpus ships the binary for every match and one
readable ``.gva`` beside it, so the binaries are what cover the whole corpus
and the single pair is what section 5 cross-checks.

Run directly:
    uv run python tests/test_share_link.py
"""

from __future__ import annotations

import base64
import json
import sys
import zlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import read_gvab, encode_match, decode_match
from gvformat.reader import canonicalize
# The gammonview.com URL wrapper is app-level (mirrors sharelink.js), not codec.
from gvanalysis.share import (
    share_link, MAX_SHARE_LENGTH, SHARE_PARAM, GAMMONVIEW_BASE_URL,
)

_URLSAFE = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"   FAIL: {label}")
    else:
        print(f"   ok:   {label}")


def _fixtures() -> list[tuple[str, dict]]:
    """Every sample match as a canonical OGXM dict (the form links round-trip).

    Read from the ``.gvab``, not the ``.gva``: the corpus ships one readable
    JSON copy and a binary for every match (the binary is ~12x smaller), so the
    binaries are what reach all of them. ``read_gvab`` output is already
    canonical, which is why there is no ``canonicalize`` call here.
    """
    out = []
    for gvab in sorted((_REPO_ROOT / "samples" / "gv").glob("*.gvab")):
        out.append((gvab.name, read_gvab(gvab.read_bytes())))
    return out


def main() -> int:
    fixtures = _fixtures()
    check(bool(fixtures), f"fixtures found under samples/gv/ ({len(fixtures)})")

    print("\n--- 1. encode -> decode round-trips ---")
    for name, match in fixtures:
        back = decode_match(encode_match(match))
        check(back == match, f"1. {name} survives encode -> decode unchanged")

    print("\n--- 2. payload is URL-safe and within the cap ---")
    for name, match in fixtures:
        payload = encode_match(match)
        check(set(payload) <= _URLSAFE and not payload.endswith("="),
              f"2. {name} payload is URL-safe, unpadded base64url")
        check(len(payload) <= MAX_SHARE_LENGTH, f"2. {name} payload within cap")

    print("\n--- 3. decodes via the browser (pako.inflate) path ---")
    # pako.inflate reads the zlib stream zlib.compress produces, and
    # bytes_to_base64url in the JS is byte-identical to ours. Confirm the
    # payload inflates to write_gvab(match) using only stdlib zlib.
    for name, match in fixtures:
        payload = encode_match(match)
        pad = "=" * (-len(payload) % 4)
        raw = zlib.decompress(base64.urlsafe_b64decode(payload + pad))
        check(read_gvab(raw) == match, f"3. {name} inflates + reads back to match")

    print("\n--- 4. share_link builds the expected URL ---")
    name, match = fixtures[0]
    link = share_link(match)
    prefix = f"{GAMMONVIEW_BASE_URL}/#/?{SHARE_PARAM}="
    check(link.startswith(prefix), "4. link starts with gammonview.com/#/?m=")
    check(decode_match(link[len(prefix):]) == match, "4. link payload decodes to match")

    print("\n--- 5. .gvab fixture links the same as its .gva ---")
    gvab = (_REPO_ROOT / "samples" / "gv" / "B4_SrGcsKAQmoTyHlgJCbM.gvab")
    if gvab.exists():
        from_gvab = encode_match(read_gvab(gvab.read_bytes()))
        from_gva = encode_match(canonicalize(
            json.loads((_REPO_ROOT / "samples" / "gv" / "B4_SrGcsKAQmoTyHlgJCbM.gva").read_text())
        ))
        check(from_gvab == from_gva, "5. .gvab and .gva encode to the same payload")

    print("\n--- 6. corrupt payloads raise ---")
    for bad in ("@@@not-base64@@@", base64.urlsafe_b64encode(b"hello world").decode(), ""):
        raised = False
        try:
            decode_match(bad)
        except Exception:  # noqa: BLE001
            raised = True
        check(raised, f"6. corrupt payload {bad[:16]!r} raises")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
