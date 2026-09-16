# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Load any supported match file into a canonical OGXM-JSON dict.

OGXM is the pipeline's internal representation: every input — a Jellyfish/GNUbg
``.mat``, an OGXM-JSON ``.gva``/``.ogxm``, or the compact ``.gvab`` binary —
becomes one OGXM document here, and the analyzer works from that (see
``ogxm_reconstructor`` + ``match.analyze_ogxm``). An OGXM input may already
carry analysis blocks; they are preserved (the analyzer appends, never
replaces).

Dispatch is by extension, with a byte/char content sniff as a fallback for
unknown or missing extensions. A trailing ``.gz`` is transparently
decompressed, then the inner name/content decides the format.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from gvformat import read_gvab, canonicalize, mat_to_ogxm

#: `.gvab` file-header magic (see gvformat.binary / the OGXM binary spec).
_GVAB_MAGIC = b"OGXM"

_JSON_EXTS = {".gva", ".ogxm", ".json"}
_BINARY_EXTS = {".gvab"}
_MAT_EXTS = {".mat"}


def _from_bytes(data: bytes, hint_ext: str | None) -> dict:
    """Decode raw file bytes into an OGXM dict, using ``hint_ext`` (a lowercased
    extension) when it is decisive and sniffing content otherwise."""
    if hint_ext in _BINARY_EXTS or data[:4] == _GVAB_MAGIC:
        return read_gvab(data)
    if hint_ext in _JSON_EXTS:
        return canonicalize(json.loads(data.decode("utf-8")))
    if hint_ext in _MAT_EXTS:
        return mat_to_ogxm(data.decode("utf-8", errors="replace"))
    # Unknown extension: sniff. A JSON OGXM starts (after whitespace) with '{';
    # anything else is treated as .mat text.
    stripped = data.lstrip()
    if stripped[:1] == b"{":
        return canonicalize(json.loads(data.decode("utf-8")))
    return mat_to_ogxm(data.decode("utf-8", errors="replace"))


def load_ogxm(path: "Path | str") -> dict:
    """Read ``path`` (``.mat`` / ``.gva`` / ``.ogxm`` / ``.gvab``, optionally
    ``.gz``) and return a canonical OGXM-JSON dict.

    Raises ``FileNotFoundError`` if the path does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    data = path.read_bytes()
    name = path.name.lower()
    if name.endswith(".gz"):
        data = gzip.decompress(data)
        name = name[:-3]
    hint_ext = Path(name).suffix or None
    return _from_bytes(data, hint_ext)
