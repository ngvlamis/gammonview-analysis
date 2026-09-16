# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""GammonView Analysis (GVA) codec — pure-stdlib OGXM reader/writer/tools.

Engine-free: no bgsage, no C++ extension, no third-party deps. This package
is the reusable format layer — convert an analysis result to OGXM-JSON,
serialize it to the compact ``.gvab`` binary, read it back, derive OGID
position strings, and compute the compute-on-read aggregates (PR, luck,
MWC). The bgsage-powered *analysis* that produces the input lives in a
separate layer (it depends on this one, not the other way around).

Public API::

    from gvformat import (
        write_gvab,        # OGXM-JSON dict -> .gvab bytes
        read_gvab,         # .gvab bytes -> OGXM-JSON dict
        canonicalize,      # normalize an OGXM-JSON dict to read_gvab's form
        to_ogxm_json,      # analyzer result -> OGXM-JSON dict
        compute_aggregates,# OGXM-JSON dict -> PR/luck/MWC stats
        board_to_ogid,     # board -> OGID position string
        parse_ogid,        # OGID position string -> OgidState (board + fields)
        looks_like_ogid,   # tell an OGID from an XGID
        GvabError,
    )

The on-wire format is specified in the ``OGXM_*`` docs at the repo root.
"""

from .binary import write_gvab
from .reader import read_gvab, canonicalize, GvabError
from .export import to_ogxm_json
from .stats import compute_aggregates
from .ogid import board_to_ogid, parse_ogid, looks_like_ogid, OgidState
from .xg import convert_xg
from .bgf import convert_bgf
from .mat import convert_mat, mat_to_ogxm
from .share import encode_match, decode_match
from .merge import append_analysis, MAX_ANALYSES
from .place import PLACE_SEPARATOR, clean_place, split_place, join_place

__all__ = [
    "write_gvab",
    "read_gvab",
    "canonicalize",
    "GvabError",
    "to_ogxm_json",
    "compute_aggregates",
    "board_to_ogid",
    "parse_ogid",
    "looks_like_ogid",
    "OgidState",
    "convert_xg",
    "convert_bgf",
    "convert_mat",
    "mat_to_ogxm",
    "encode_match",
    "decode_match",
    "append_analysis",
    "MAX_ANALYSES",
    "PLACE_SEPARATOR",
    "clean_place",
    "split_place",
    "join_place",
]
