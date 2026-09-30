# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The reference OGXM v2 codec, borrowed as a test oracle.

HedgeHog's own implementation builds as a shared library with a C ABI
(``src/match/ogxm_bindings.cpp``), and upstream ships a ctypes binding over it
in ``python/ogxm/``. Four of its entry points answer questions no amount of
reading the spec can: whether a document satisfies the v2 rules, what bytes the
reference writer produces for it, what a v2 file means, and what the reference
conversion makes of one of our v1 files.

That makes it the thing to check a v2 implementation *against*, rather than
checking our reading of the prose against itself. Every rule lives in the
library; nothing here holds a second copy of the format.

**It is not a dependency.** Upstream is a separate checkout, MIT but not
vendored, and the library is a build artifact of it -- so this module finds one
if it can and reports that it cannot otherwise, exactly as GammonView's
``upstream.test.js`` treats the sibling repo. A machine without it skips the
tests that need it; it must never be needed to build, install or release.

Build it with::

    cd ~/projects/hedgehog-public && make libogxm

and this module will find ``build/libogxm.so`` there. ``LIBOGXM_PATH`` overrides
the search and is authoritative: set to something that does not resolve, it
fails rather than falling back, since a typo would otherwise silently check
nothing.

Note the library is looked up by path and loaded with no version negotiation
beyond ``ogxm_version()``, which callers should record in any failure they
report -- a rule that moved upstream and a bug on our side look identical
otherwise.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

_u8p = ctypes.POINTER(ctypes.c_uint8)

_SEARCH = (
    Path.home() / "projects" / "hedgehog-public" / "build" / "libogxm.so",
    Path.home() / "projects" / "hedgehog-public" / "build" / "libogxm.dylib",
)


class OracleUnavailable(RuntimeError):
    """No reference library to check against; the caller should skip."""


def library_path() -> Path:
    """Where the reference library is, or raise ``OracleUnavailable``.

    ``LIBOGXM_PATH`` wins and does not fall back -- see the module docstring.
    """
    override = os.environ.get("LIBOGXM_PATH")
    if override:
        path = Path(override).expanduser()
        if not path.exists():
            raise OracleUnavailable(f"LIBOGXM_PATH is set but does not exist: {path}")
        return path
    for path in _SEARCH:
        if path.exists():
            return path
    raise OracleUnavailable(
        "no libogxm found -- build it with `make libogxm` in a hedgehog-public "
        "checkout, or point LIBOGXM_PATH at one"
    )


_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib
    lib = ctypes.CDLL(str(library_path()))
    sig = {
        "ogxm_version": ([], ctypes.c_char_p),
        "ogxm_reader_major": ([], ctypes.c_int),
        "ogxm_v2_rule_name": ([ctypes.c_int], ctypes.c_char_p),
        "ogxm_v2_check_json": ([ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int),
        "ogxm_v2_is_field_name": ([ctypes.c_char_p], ctypes.c_int),
        "ogxm_json_to_binary": ([ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(_u8p),
                                 ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int),
        "ogxm_binary_to_json": ([_u8p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_char_p),
                                 ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int),
        "ogxm_v1_to_v2": ([_u8p, ctypes.c_uint32, ctypes.POINTER(_u8p),
                           ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_int)],
                          ctypes.c_int),
    }
    for name, (argtypes, restype) in sig.items():
        try:
            fn = getattr(lib, name)
        except AttributeError:
            # A library built before v2 exports ogxm_json_to_binary and
            # ogxm_binary_to_json and nothing else here, so it loads and then
            # fails on the first v2 call. Say so instead: a stale build sitting
            # in the default search path is the likely state of any checkout
            # that was used for v1 work.
            raise OracleUnavailable(
                f"{library_path()} exports no {name} -- it predates OGXM v2. "
                "Rebuild it with `make libogxm`."
            ) from None
        fn.argtypes, fn.restype = argtypes, restype
    _lib = lib
    return lib


def version() -> str:
    """The reference implementation's version, e.g. ``"2.2.0"``."""
    return _load().ogxm_version().decode()


def rule_name(code: int) -> str:
    """The v2 rule a refusal names, e.g. ``"alternatives"``, or ``"ok"``."""
    return _load().ogxm_v2_rule_name(code).decode()


def is_field_name(name: str) -> bool:
    """Whether ``name`` is a v2 field name.

    ``ANNO`` keys may not collide with one (8.4.2), and the list is the
    library's, not a copy of it.
    """
    return bool(_load().ogxm_v2_is_field_name(name.encode()))


def check_json(doc: dict) -> str:
    """Run the v2 rules over a document. ``"ok"``, or the rule it broke."""
    raw = json.dumps(doc).encode()
    return rule_name(_load().ogxm_v2_check_json(raw, len(raw)))


def json_to_binary(doc: dict) -> bytes:
    """The bytes the reference writer produces for ``doc``."""
    lib = _load()
    raw = json.dumps(doc).encode()
    out, n = _u8p(), ctypes.c_uint32()
    rc = lib.ogxm_json_to_binary(raw, len(raw), ctypes.byref(out), ctypes.byref(n))
    if rc != 0:
        raise ValueError(f"ogxm_json_to_binary refused the document (code {rc})")
    return bytes(ctypes.cast(out, _u8p)[:n.value])


def binary_to_json(data: bytes) -> dict:
    """What the reference reader makes of ``data``."""
    lib = _load()
    buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    s, n = ctypes.c_char_p(), ctypes.c_uint32()
    rc = lib.ogxm_binary_to_json(buf, len(data), ctypes.byref(s), ctypes.byref(n))
    if rc != 0:
        raise ValueError(f"ogxm_binary_to_json refused the bytes (code {rc})")
    return json.loads(ctypes.string_at(s.value, n.value))


def v1_to_v2(data: bytes) -> tuple[bytes | None, str]:
    """Convert a v1 file the way the reference implementation does.

    Returns ``(bytes, "ok")``, or ``(None, rule)`` naming the v2 rule the file
    broke -- ``"v1 reader"`` when the frozen v1 reader refused the bytes
    themselves.
    """
    lib = _load()
    buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    out, n, rule = _u8p(), ctypes.c_uint32(), ctypes.c_int()
    rc = lib.ogxm_v1_to_v2(buf, len(data), ctypes.byref(out), ctypes.byref(n),
                           ctypes.byref(rule))
    if rc != 0:
        return None, rule_name(rule.value)
    return bytes(ctypes.cast(out, _u8p)[:n.value]), "ok"
