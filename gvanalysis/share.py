# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""GammonView share links (the app-level wrapper around gvformat's codec).

The *payload* codec -- match <-> URL-safe string -- is site-agnostic and lives
in :mod:`gvformat.share` (``encode_match`` / ``decode_match``). This module adds
the one thing that *is* GammonView-specific: the viewing URL. It mirrors
GammonView's own ``sharelink.js``, which composes the same gvformat codec.
"""

from __future__ import annotations

from gvformat import encode_match

# The GammonView viewing route: ``<base>/#/?m=<payload>``. The whole match rides
# in the URL *fragment* (after ``#``), which browsers never send to the server,
# so no file is hosted and the payload never reaches GammonView's backend.
GAMMONVIEW_BASE_URL = "https://gammonview.com"
SHARE_PARAM = "m"

# Largest encoded payload (base64url chars) proven safe across browsers while
# staying under Chrome's hard URL cap. The fragment never hits the server, so
# server request-line limits don't apply. Mirrors MAX_SHARE_LENGTH in the JS.
MAX_SHARE_LENGTH = 2_000_000


def share_link(match: dict, base_url: str = GAMMONVIEW_BASE_URL) -> str:
    """Build the full GammonView viewing URL for an OGXM match."""
    return f"{base_url.rstrip('/')}/#/?{SHARE_PARAM}={encode_match(match)}"
