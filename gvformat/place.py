# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Where a match was played: the ``event`` / ``site`` pair.

OGXM carries these as two independent top-level strings, matching every source
format we read (a ``.mat``'s ``[Event]``/``[Site]`` headers, XG's event/
location, BGF's event/site) and matching GammonView's two database columns.

They were once folded into a single ``event`` string joined by
``PLACE_SEPARATOR``, so files written before that changed carry the combined
form with no ``site`` at all. :func:`split_place` recovers the pair from such a
string and is applied on read when a document has an ``event`` but no ``site``
— old files self-heal, and nothing downstream has to know they were ever
folded. :func:`join_place` is its inverse, for writing a source format that
only has one slot for the two.
"""

#: Joins the two halves in a legacy combined string. Whitespace-padded: it has
#: to survive round-tripping through formats that trim their header values.
PLACE_SEPARATOR = " • "


def clean_place(value) -> str | None:
    """Trim; an empty or blank string becomes ``None``, never ``""``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def split_place(combined) -> tuple[str | None, str | None]:
    """Split a legacy ``"Event • Site"`` string into ``(event, site)``.

    Only the *first* separator splits, so a site that contains one of its own
    keeps it. A string with no separator is all event — guessing "site" would
    move data between two columns that mean different things, and every source
    format we read writes the event when it has only one of the two.
    """
    text = clean_place(combined)
    if text is None:
        return None, None
    at = text.find(PLACE_SEPARATOR)
    if at < 0:
        return text, None
    return clean_place(text[:at]), clean_place(text[at + len(PLACE_SEPARATOR):])


def join_place(event, site) -> str | None:
    """Inverse of :func:`split_place`: ``(event, site)`` -> one string."""
    e, s = clean_place(event), clean_place(site)
    if e and s:
        return f"{e}{PLACE_SEPARATOR}{s}"
    return e or s or None
