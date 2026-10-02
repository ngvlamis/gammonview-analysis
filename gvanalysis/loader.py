# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Load any supported match file into a canonical OGXM-JSON dict.

OGXM is the pipeline's internal representation: every input — an eXtreme Gammon
``.xg``, a BGBlitz ``.bgf``, a Jellyfish/GNUbg ``.mat``, an OGXM-JSON
``.gva``/``.ogxm``, or the compact ``.gvab`` binary — becomes one OGXM document
here, and the analyzer works from that (see ``ogxm_reconstructor`` +
``match.analyze_ogxm``). An OGXM input may already carry analysis blocks; they
are preserved (the analyzer appends, never replaces). The source-format
converters all live in ``gvformat``, since none of them needs the engine.

Dispatch reads the **content** first and the extension second. Three of the five
formats name themselves in their opening bytes, so a mislabeled or
extension-less file is still read as what it is; only ``.mat`` and OGXM JSON
have to fall back to the name, and both are recognizable as text anyway. A
format nothing identifies raises rather than being guessed at: guessing used to
mean parsing an ``.xg`` as ``.mat`` text and returning an empty match.

A trailing ``.gz`` is transparently decompressed first, then the inner
name/content decides the format.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

from gvformat import (
    canonicalize,
    convert_bgf,
    convert_xg,
    mat_to_ogxm,
    read_gvab,
)

#: File-header magic, each the first four bytes of its format: the OGXM binary
#: (see gvformat.binary / the OGXM binary spec) and XG's rich-game header
#: (gvformat.xg._MAGIC, 0x484D4752 little-endian).
_GVAB_MAGIC = b"OGXM"
_XG_MAGIC = b"RGMH"

#: A .bgf opens with a one-line JSON header that names the format (the rest is
#: Smile, possibly compressed) -- so the first line identifies it, and nothing
#: else that starts with '{' says this.
_BGF_HEADER_RE = re.compile(rb'"format"\s*:\s*"BGF"')

#: A .mat is plain text with no magic, so these stand in for one: the two lines
#: the parser itself looks for (mat_parser._MATCH_LEN_RE / _GAME_HEADER_RE).
#: Without one of them the file holds no match, and reading it as .mat would
#: hand the caller an empty document instead of an error.
_MAT_MARKER_RE = re.compile(
    rb"^[ \t]*[Gg]ame[ \t]+\d+[ \t]*\r?$"
    rb"|[Mm]atch\s+of\s+\d+"
    rb"|\d+\s+[Pp]oints?\s+[Mm]atch",
    re.MULTILINE,
)

#: What each extension names, for the files whose bytes cannot say. ``.json`` is
#: an accepted alias for OGXM JSON but is deliberately left out of
#: ``INPUT_EXTENSIONS``: a directory of matches may hold unrelated JSON, and
#: sweeping it up is worse than making the caller name the file.
_EXT_KIND = {
    ".xg": "xg",
    ".bgf": "bgf",
    ".mat": "mat",
    ".gva": "json",
    ".ogxm": "json",
    ".gvab": "gvab",
    ".json": "json",
}

#: Every extension worth looking for: what the refusal message lists, and what
#: a directory argument expands to (``gvanalysis.batch``).
INPUT_EXTENSIONS = tuple(e for e in _EXT_KIND if e != ".json")


def _sniff(data: bytes) -> str | None:
    """The format ``data``'s own content identifies, if any."""
    if data[:4] == _GVAB_MAGIC:
        return "gvab"
    if data[:4] == _XG_MAGIC:
        return "xg"
    first_line = data[:4096].split(b"\n", 1)[0]
    if data[:1] == b"{" and _BGF_HEADER_RE.search(first_line):
        return "bgf"
    if data[:64].lstrip()[:1] == b"{":
        return "json"
    if _MAT_MARKER_RE.search(data):
        return "mat"
    return None


def _from_bytes(data: bytes, hint_ext: str | None) -> dict:
    """Decode raw file bytes into an OGXM dict.

    ``hint_ext`` is a lowercased extension, used only where the bytes do not
    identify themselves.
    """
    kind = _sniff(data) or _EXT_KIND.get(hint_ext or "")

    if kind == "gvab":
        return read_gvab(data)
    if kind == "json":
        return canonicalize(json.loads(data.decode("utf-8")))
    if kind == "mat":
        doc = mat_to_ogxm(data.decode("utf-8", errors="replace"))
        # A .mat has no file signature, so "it parsed" proves nothing -- the
        # parser finds no games in anything that is not one and reports an empty
        # match rather than an error. This is the last point at which the two can
        # be told apart, and a zero-game match is not a thing to analyze either
        # way.
        if not doc.get("games"):
            raise ValueError(
                "No games found: this file holds no match (read as .mat text, "
                "which is what an unsigned text file can be)"
            )
        return doc
    if kind == "xg":
        return convert_xg(data)
    if kind == "bgf":
        return convert_bgf(data)
    raise ValueError(
        f"Unrecognized match file{f' ({hint_ext})' if hint_ext else ''}: "
        f"not any of {', '.join(INPUT_EXTENSIONS)} (optionally .gz)"
    )


def load_ogxm(path: "Path | str") -> dict:
    """Read ``path`` (``.mat`` / ``.xg`` / ``.bgf`` / ``.gva`` / ``.ogxm`` /
    ``.gvab``, optionally ``.gz``) and return a canonical OGXM-JSON dict.

    Raises ``FileNotFoundError`` if the path does not exist, and ``ValueError``
    if nothing identifies the file as a match.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    data = path.read_bytes()
    name = path.name.lower()
    if name.endswith(".gz"):
        # A ValueError, like every other "this is not a match I can read": the
        # CLIs catch that and exit 1 with the message, where a BadGzipFile
        # (an OSError) would reach the user as a traceback.
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as e:
            raise ValueError(f"Not a valid .gz file: {path} ({e})") from e
        name = name[:-3]
    hint_ext = Path(name).suffix or None
    return _from_bytes(data, hint_ext)
