# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Synthetic documents for the OGXM v2 analysis fields: block records, levels,
and the keys on decisions and alternatives.

Shared by ``test_ogxm2_blocks.py`` and by ``gen_ogxm2_blocks_fixtures.py``, which
pins what the Python writer makes of each so that
``gvformat-js/test/test-ogxm2-blocks.js`` can be held to the same bytes.

Every document is one of the analyzed goldens (real plies and real analysis, so
every play is legal and the reference codec can replay them) with the fields set
by hand. Each case is given in the form a writer is handed -- the keys a person
or a producer would set -- and ``settled`` reads it back: the document a reader
makes of the file, which is the one that must survive another round trip.
Nothing here needs the engine.
"""

from __future__ import annotations

import copy
from pathlib import Path

from gvformat import read_gvab, write_gvab

_ROOT = Path(__file__).resolve().parent.parent
GOLDEN = _ROOT / "tests" / "golden"

MONEY = "B4_SrGcsKAQmoTyHlgJCbM"        # a money game, three games
MATCH = "baR-U643iUDpvzTC"              # a 5-point match

SOURCES = ["c0a80101-0000-4000-8000-000000000001", "c0a80101-0000-4000-8000-000000000002"]

#: A rollout with every field v2 has for one (6.2). The seed does not fit in
#: 53 bits, which is why a document holds it as a decimal string.
ROLLOUT = {"trials": 360, "truncation_depth": 7, "move_ply": 2, "variance_reduction": 2,
           "seed": "18446744073709551615", "budget_ms": 90000, "match_policy": 1}
SMALL_ROLLOUT = {"trials": 72, "truncation_depth": 5, "move_ply": 1}

DIALS = {"jacoby_resolved": True, "jacoby_mode": 1, "cube_limit_resolved": 64, "cube_limit_mode": 2,
         "exact_bearoff": True, "race_order": True, "top_deep": True, "top_deep_threshold": 0.04,
         "rollout_budget_on": True, "cube_rule": 2, "top_deep_keep": 5, "top_deep_accept": 2}

BLOCK = {
    "producer": 0, "complete": True,
    "model_id": "16fcd41c-9f64-4fcc-bca2-c89e6de07721", "model_name": "Xerxes",
    "model_digest": "0123456789abcdef" * 4, "engine_build": "7a1be2d941e2-dirty",
    "cube_efficiency": 0.6, "tables": "egtb-2026.1", "dials": DIALS,
    "completed_at": 1790380900123, "sources": SOURCES,
}


def golden(stem: str, games: int = 1) -> dict:
    """The first ``games`` games of an analyzed golden, scored as played. Whole
    goldens would be megabytes of fixture for the JavaScript mirror."""
    doc = read_gvab((GOLDEN / f"{stem}.fast.gvab").read_bytes())
    doc["games"] = doc["games"][:games]
    scores = [0, 0]
    for g in doc["games"]:
        if g["winner"] in (0, 1):
            scores[g["winner"]] += g["points_won"]
    doc.update(white_score=scores[0], black_score=scores[1], result=0)
    for g in doc["games"]:
        g["is_lastgame"] = False
    return doc


def settled(doc: dict) -> dict:
    """What a reader makes of the file the writer makes of ``doc``."""
    return read_gvab(write_gvab(doc))


def plies(doc: dict):
    """Every ply with its address, in document order."""
    for gi, g in enumerate(doc["games"]):
        for pi, p in enumerate(g["plies"]):
            yield gi, pi, p


def checker_plies(doc: dict, *, played_not_first: bool = False, min_alts: int = 6) -> list:
    out = []
    for gi, pi, p in plies(doc):
        a = p.get("analysis")
        alts = (a or {}).get("alternatives") or []
        if len(alts) < min_alts:
            continue
        idx = next((i for i, x in enumerate(alts) if x["is_played"]), None)
        if idx is None or (idx > 0) != played_not_first:
            continue
        out.append((gi, pi, p))
    return out


def _sub(doc: dict, key: str, skip: int = 0):
    """A dice ply whose analysis has the cube record ``key``."""
    hits = [(gi, pi, p) for gi, pi, p in plies(doc)
            if isinstance((p.get("analysis") or {}).get(key), dict)]
    return hits[skip]


def _cube_ply(doc: dict, action: int, skip: int = 0):
    hits = [(gi, pi, p) for gi, pi, p in plies(doc)
            if p["action_id"] == action and p.get("analysis")]
    return hits[skip]


def rich_money() -> dict:
    """A money game whose block states every optional field v2's block record
    has, whose levels include rollouts, and whose decisions carry every key v2
    adds to them -- some of which the ``DECS`` stream can hold, some only the
    annotation that keeps a decision exactly."""
    doc = golden(MONEY)
    info = doc["analysis_info"]
    info.update(copy.deepcopy(BLOCK))
    # Not the default currency (a money game's is cubeful money): cubeless.
    info["currency"] = 0
    # The level the labels give is `2ply` at depth 2; this one also has a cube depth.
    info["level"] = {"preset": "2ply", "checker_ply": 2, "cube_ply": 2}
    info["luck_eval_level"] = "1ply"

    # A decision DECS can hold: a truncated list (the total, what was rolled out,
    # what was searched deep), a rollout at the decision's level that its first
    # three alternatives inherit and the rest leave with a level of their own.
    gi, pi, p = checker_plies(doc)[0]
    a = p["analysis"]
    a.update(alternatives_total=40, rollouts_done=3, deep_searched=6, position_tags=5,
             producer_ref=1, source_band=1800,
             level={"preset": "truncated2", "rollout": copy.deepcopy(ROLLOUT)},
             luck_producer_ref=0, luck_level={"preset": "x1", "checker_ply": 1})
    for i, alt in enumerate(a["alternatives"]):
        if i < 3:
            alt["eval_level"] = "truncated2"
            alt["rollout_se"] = 0.0123 + i / 1000
            alt["cubeless_equity"] = alt["equity"] - 0.01
        else:
            alt["eval_level"] = "1ply"
            alt["level"] = {"preset": "1ply", "checker_ply": 1}

    # A decision the annotation keeps exactly: the best alternative is rolled
    # out and the played one is not (A5).
    gi, pi, p = checker_plies(doc, played_not_first=True)[0]
    a = p["analysis"]
    a["alternatives"][0].update(
        eval_level="truncated1", level={"preset": "truncated1", "rollout": copy.deepcopy(SMALL_ROLLOUT)})
    a["alternatives"][1]["cubeless_equity"] = a["alternatives"][1]["equity"] - 0.02

    # More of the same: counts DECS has no place for.
    cps = checker_plies(doc)
    cps[1][2]["analysis"].update(alternatives_total=2, rollouts_done=4)       # a total below the list
    cps[2][2]["analysis"].update(rollouts_done=2, deep_searched=2)            # no total to be truncated by
    cps[3][2]["analysis"]["producer_ref"] = 7                                 # past the sources
    cps[4][2]["analysis"]["position_tags"] = 0xFFFFFFFF

    # Cube decisions embedded in a dice ply.
    _gi, _pi, p = _sub(doc, "cube_decision")
    sub = p["analysis"]["cube_decision"]
    sub.update(take_point=0.3125, window_searched=True, is_optional=True, is_free_cube=True,
               cubeful_take_value=0.4321, producer_ref=0, eval_level="truncated1",
               level={"preset": "truncated1", "rollout": copy.deepcopy(SMALL_ROLLOUT)})
    _gi, _pi, p = _sub(doc, "missed_double")
    p["analysis"]["missed_double"].update(
        take_point=0.25, cubeful_take_value=-0.12, currency=1, producer_ref=1,
        level={"preset": "x2", "cube_ply": 4})
    _gi, _pi, p = _sub(doc, "cube_decision", skip=2)
    p["analysis"]["cube_decision"]["producer_ref"] = 9                        # past the sources

    # A double and its answer.
    _gi, _pi, p = _cube_ply(doc, 21)
    p["analysis"].update(take_point=0.2917, is_optional=True, producer_ref=1, eval_level="truncated3",
                         level={"preset": "truncated3", "cube_ply": 3,
                                "rollout": {"trials": 360, "truncation_depth": 7, "move_ply": 3}})
    _gi, _pi, p = _cube_ply(doc, 23)
    p["analysis"].update(window_searched=True, cubeful_take_value=0.9, currency=2)
    return doc


def resigned() -> dict:
    """The money game with a resigned game that carries a resign decision."""
    import ogxm2_fields_cases as C
    from gvformat import ogxm2 as R
    doc = C.extras()
    doc["analysis_info"] = {"ply": 2, "eval_level": "2ply", "model_id": "synthetic",
                            "timestamp": 1790380800, "sources": SOURCES[:1]}
    eval_ = {"win": 0.18, "gammon_win": 0.05, "bg_win": 0.0, "gammon_loss": 0.28, "bg_loss": 0.01}
    for g in doc["games"][1:]:
        for p in g["plies"]:
            if p["action_id"] == R.ACTION_RESIGN_GAME:
                p["analysis"] = {"resign_error": 0.31, "take_resign_error": -0.02,
                                 "equity_loss": 0.31, "decision": True, "eval": eval_,
                                 "correct_value": 1, "producer_ref": 0,
                                 "level": {"preset": "rollout", "checker_ply": 2,
                                           "rollout": copy.deepcopy(SMALL_ROLLOUT)}}
    return doc


def partial() -> dict:
    """An incremental analysis: only the first game was analysed, the block says
    which plies it attempted (more than it stored) and that it is not complete."""
    doc = golden(MONEY, games=2)
    for g in doc["games"][1:]:
        for p in g["plies"]:
            p.pop("analysis", None)
    info = doc["analysis_info"]
    info.pop("complete", None)
    info["coverage"] = [[0, i] for i in range(len(doc["games"][0]["plies"]))] + [[1, 0], [1, 1], [9, 9]]
    info["producer"] = 2
    return doc


def match_currencies() -> dict:
    """A 5-point match whose first cube decision is stated in cubeful money
    (not the block's match winning chances) and whose second says its own
    currency is the block's, which v2 does not write."""
    doc = golden(MATCH)
    subs = [p["analysis"] for _gi, _pi, p in plies(doc)
            if isinstance((p.get("analysis") or {}).get("cube_decision"), dict)]
    subs[0]["cube_decision"].update(currency=1, take_point=0.28)
    subs[1]["cube_decision"]["currency"] = 2
    doc["analysis_info"]["cube_efficiency"] = 0.7
    return doc


def money_block_in_a_match() -> dict:
    """A match analysis in cubeful money: its numbers are not winning chances,
    so they are held as they are."""
    doc = golden(MATCH)
    doc["analysis_info"]["currency"] = 1
    return doc


def money_in_a_match_currency() -> dict:
    """A money game whose block claims match winning chances. There is no match
    to be a chance in, so it is written in cubeful money."""
    doc = golden(MONEY)
    doc["analysis_info"]["currency"] = 2
    return doc


#: Values v2 cannot hold, one per rule they break (P6). Each travels in the
#: block's ``x-gammonview-analysis`` annotation and comes back whole.
def unholdable() -> dict:
    doc = golden(MONEY)
    info = doc["analysis_info"]
    info.update(producer=-1, model_name="n" * 5000, model_digest="NOT-HEX", engine_build="e" * 5000,
                tables="t" * 5000, completed_at=-5,
                sources=["not-a-uuid", "C0A80101-0000-4000-8000-000000000001"],
                model_id="u" * 5000,
                dials={"jacoby_mode": -1, "top_deep_keep": -3, "top_deep_threshold": -0.5})
    return doc


def two_blocks() -> dict:
    """Two blocks in one document, the second with its own provenance."""
    from gvformat import append_analysis
    one = rich_money()
    two = golden(MONEY)
    two["analysis_info"].update(model_id="gv-other/1", analysis_id="1f2e3d4c-0000-4000-8000-0000000000aa",
                                engine_build="b2", complete=True, producer=4, timestamp=0)
    return append_analysis(one, two)


# ---------------------------------------------------------------------------
# A file from another producer, built by the reference codec
# ---------------------------------------------------------------------------

def foreign_base() -> dict:
    """The money game as two games, the second cut short by a resignation, so
    that a file has a decision of every kind."""
    doc = golden(MONEY, games=2)
    g0, g1 = doc["games"][0], doc["games"][1]
    cut = 6
    assert all(p["action_id"] < 21 for p in g1["plies"][:cut])
    plies = copy.deepcopy(g1["plies"][:cut])
    color = g1["plies"][cut]["color"]
    plies.append({"color": color, "action_id": 27, "analysis": {
        "resign_error": 0.31, "take_resign_error": -0.02, "equity_loss": 0.31, "decision": True,
        "eval": {"win": 0.18, "gammon_win": 0.05, "bg_win": 0.0, "gammon_loss": 0.28, "bg_loss": 0.01}}})
    plies.append({"color": 1 - color, "action_id": 24})
    g1 = {**g1, "plies": plies, "winner": 0 if color == 0 else 1, "points_won": 1, "is_lastgame": True}
    doc["games"] = [g0, g1]
    white = sum(g["points_won"] for g in doc["games"] if g["winner"] == 0)
    black = sum(g["points_won"] for g in doc["games"] if g["winner"] == 1)
    doc.update(white_score=white, black_score=black)
    return doc


CURRENCY = {0: "cubeless", 1: "cubeful", 2: "cubeful_match"}
RESOLUTION = {0: "auto", 1: "forced_on", 2: "forced_off"}
CUBE_RULE = {0: "unavailable", 1: "janowski", 2: "scalar_equity"}
VARIANCE = {0: "none", 1: "standard", 2: "extended"}
POLICY = {0: "money", 1: "match_aware"}
RESIGN_VALUE = {0: "none", 1: "single", 2: "gammon", 3: "backgammon"}


def _json_level(level: dict) -> dict:
    """A level in the reference's vocabulary (J3, J6)."""
    out = {k: v for k, v in level.items() if k != "rollout"}
    if "rollout" in level:
        r = dict(level["rollout"])
        if "variance_reduction" in r:
            r["variance_reduction"] = VARIANCE[r["variance_reduction"]]
        if "match_policy" in r:
            r["match_policy"] = POLICY[r["match_policy"]]
        out["rollout"] = r
    return out


#: The levels the foreign file's decisions have. The block's is the default for
#: all of them; a decision whose level is the block's states none.
FOREIGN_BLOCK = {"preset": "2ply", "checker_ply": 2}
FOREIGN_DEEP = {"preset": "++", "checker_ply": 3, "rollout": ROLLOUT}
FOREIGN_TAIL = {"preset": "1ply", "checker_ply": 1}
FOREIGN_CUBE = {"preset": "+", "checker_ply": 2, "cube_ply": 3, "rollout": SMALL_ROLLOUT}
FOREIGN_LUCK = {"preset": "1ply", "checker_ply": 1}


def foreign_json(oracle) -> dict:
    """The reference codec's JSON for a money game analysed by another producer
    that sets every field v2's analysis records have, in the reference's own
    vocabulary: the plies come from our writer, the fields are put on by hand,
    so the bytes are the reference's and not ours."""
    j = oracle.binary_to_json(write_gvab(foreign_base()))
    j.pop("annotations", None)
    info = j["analysis_info"]
    info.pop("timestamp", None)
    info.pop("ply", None)
    info.update(
        producer="ogx", complete=True,
        coverage=list(range(0, 30)),
        model_id=BLOCK["model_id"], model_name=BLOCK["model_name"],
        model_digest=BLOCK["model_digest"], engine_build=BLOCK["engine_build"],
        currency=CURRENCY[0], cube_efficiency=BLOCK["cube_efficiency"], tables=BLOCK["tables"],
        dials={**{k: v for k, v in DIALS.items() if not k.endswith("_mode") and k != "cube_rule"},
               "jacoby_mode": RESOLUTION[DIALS["jacoby_mode"]],
               "cube_limit_mode": RESOLUTION[DIALS["cube_limit_mode"]],
               "cube_rule": CUBE_RULE[DIALS["cube_rule"]]},
        completed_at=BLOCK["completed_at"], sources=list(SOURCES),
        level=_json_level(FOREIGN_BLOCK), started_at=1767534720123)
    plies = j["games"][0]["plies"]
    a = plies[0]["analysis"]
    for k in ("level",):
        a[k] = _json_level(FOREIGN_DEEP)
    a.update(alternatives_total=40, rollouts_done=3, deep_searched=6, position_tags=5,
             producer_ref=1, source_band=1800)
    for i, alt in enumerate(a["alternatives"]):
        alt["level"] = _json_level(FOREIGN_DEEP if i < 3 else FOREIGN_TAIL)
        if i < 3:
            alt["rollout_se"] = 0.0123 + i / 1000
            alt["cubeless_equity"] = round(alt["equity"] - 0.01, 6)
    a["roll"].update(level=_json_level(FOREIGN_LUCK), producer_ref=0)

    sub = plies[1]["analysis"]["cube_decision"]
    sub.update(take_point=0.3125, window_static=True, is_optional=True, is_free_cube=True,
               cubeful_take_value=0.4321, currency=CURRENCY[1], producer_ref=1,
               level=_json_level(FOREIGN_CUBE))
    plies[9]["analysis"]["missed_double"].update(take_point=0.25, producer_ref=0)

    double = plies[20]["analysis"]
    double.update(take_point=0.2917, is_optional=True, producer_ref=1, level=_json_level(FOREIGN_CUBE))
    plies[21]["analysis"].update(cubeful_take_value=0.9, window_static=True)

    resign = j["games"][1]["plies"][6]["analysis"]
    resign.update(correct_value=RESIGN_VALUE[1], producer_ref=1, level=_json_level(FOREIGN_CUBE))
    return j


def all_cases() -> dict[str, dict]:
    return {
        "rich-money": rich_money(),
        "resigned": resigned(),
        "partial": partial(),
        "match-currencies": match_currencies(),
        "money-block-in-a-match": money_block_in_a_match(),
        "money-in-a-match-currency": money_in_a_match_currency(),
        "unholdable": unholdable(),
        "two-blocks": two_blocks(),
    }
