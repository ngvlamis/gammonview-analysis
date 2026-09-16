# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Encode a whole match as a compact, URL-safe string (and back).

One rung above :func:`gvformat.write_gvab`: where that is match <-> bytes, this
is match <-> URL-safe text -- the same ``.gvab`` bytes, zlib-deflated and
base64url-encoded so a full match can ride inside a URL, a QR code, or any
text-only channel with no hosted file. It is deliberately site-agnostic: the
codec knows nothing about any particular viewer's URL scheme; a caller composes
its own link around :func:`encode_match` (see ``gvanalysis.share`` for the
GammonView wrapper, mirrored in GammonView's ``sharelink.js``).

The JS mirror deflates with ``pako.deflate`` (a zlib stream, RFC 1950) and reads
it back with ``pako.inflate``; :func:`zlib.compress` / :func:`zlib.decompress`
are the exact stdlib counterparts, so a string encoded on either side decodes on
the other. base64url is URL-safe (``-``/``_``) with ``=`` padding stripped.
"""

from __future__ import annotations

import base64
import zlib

from .binary import write_gvab
from .reader import read_gvab


def _bytes_to_base64url(raw: bytes) -> str:
    """URL-safe base64 with trailing ``=`` padding stripped (matches the JS)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _base64url_to_bytes(payload: str) -> bytes:
    padding = "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode(payload + padding)


def encode_match(match: dict) -> str:
    """Encode an OGXM match into a URL-safe base64url string.

    ``match`` is an OGXM-JSON dict (as produced by :func:`gvformat.to_ogxm_json`
    or returned by :func:`gvformat.read_gvab`).
    """
    return _bytes_to_base64url(zlib.compress(write_gvab(match)))


def decode_match(payload: str) -> dict:
    """Decode an encode_match string back into an OGXM match (its inverse)."""
    return read_gvab(zlib.decompress(_base64url_to_bytes(payload)))
