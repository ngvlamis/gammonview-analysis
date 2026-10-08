# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The edits ``test_ogxm2_passthrough`` makes to a foreign v2 file, as functions
from the file's bytes to the bytes our writer produces.

Shared by the test and by ``gen_ogxm2_passthrough_fixtures.py``, which pins what
each writes; ``gvformat-js/test/test-ogxm2-passthrough.js`` makes the same edits
with the JavaScript codec and must land on the same bytes. Keep the three in
step.
"""

from __future__ import annotations

import copy
import json

from gvformat import append_analysis, read_gvab, write_gvab


def second_analysis(doc: dict) -> dict:
    """A document carrying one analysis block of ours, aligned with ``doc``:
    its primary block, renamed and re-levelled. No engine needed."""
    our = copy.deepcopy(doc)
    for g in our["games"]:
        for p in g["plies"]:
            p.pop("analyses", None)
    our.pop("analyses_info", None)
    info = dict(doc["analysis_info"])
    info.pop("analysis_id", None)
    info.update({"model_id": "test/appended", "eval_level": "3ply", "ply": 3,
                 "timestamp": 1791300000})
    our["analysis_info"] = info
    return our


def appended(doc: dict) -> dict:
    return append_analysis(doc, second_analysis(doc))


def renamed(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["player_white"] = "Alicia"
    return doc


def last_ply_dropped(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["games"][1]["plies"].pop()
    return doc


def second_block_removed(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["analyses_info"].pop(1)
    for g in doc["games"]:
        for p in g["plies"]:
            if "analyses" in p:
                p["analyses"] = [a for a in p["analyses"] if a.get("analysis_index") == 0]
    return doc


def first_block_edited(doc: dict) -> dict:
    """One equity of the first block's first checker decision, nudged."""
    doc = copy.deepcopy(doc)
    for g in doc["games"]:
        for p in g["plies"]:
            for a in p.get("analyses") or []:
                if a.get("analysis_index") == 0 and a.get("alternatives"):
                    a["alternatives"][0]["equity"] = round(a["alternatives"][0]["equity"] + 0.01, 6)
                    return doc
    raise AssertionError("no decision to edit")


def via_gva(doc: dict) -> dict:
    """The ``.gva`` route: the document as JSON text and back."""
    return json.loads(json.dumps(doc))


def _case(*edits):
    def run(data: bytes) -> bytes:
        doc = read_gvab(data)
        for e in edits:
            doc = e(doc)
        return write_gvab(doc)
    return run


CASES = {
    "unchanged": _case(),
    "append": _case(appended),
    "metadata_edit": _case(renamed),
    "move_edit": _case(last_ply_dropped),
    "block_removed": _case(second_block_removed),
    "analysis_edit": _case(first_block_edited),
    "gva_unchanged": _case(via_gva),
    "gva_append": _case(via_gva, appended),
    "append_then_rename": _case(appended, renamed),
}

V1_CASES = {
    "v1_chunks": _case(),
}
