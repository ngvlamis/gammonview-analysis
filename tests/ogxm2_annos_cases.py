# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Synthetic documents for the OGXM v2 clock, video and annotations, and the edits
the foreign-file tests make.

Shared by ``test_ogxm2_annos.py`` and by ``gen_ogxm2_annos_fixtures.py``, which
pins what the Python writer makes of each so that
``gvformat-js/test/test-ogxm2-annos.js`` can be held to the same bytes.

The clock and video documents are built from ``samples/mat`` plies (see
``ogxm2_fields_cases``), the annotated ones from an analyzed golden (see
``ogxm2_blocks_cases``): real plays, so the reference codec can replay them, and
real analysis, so there are decisions and alternatives to address. Each case is
given in the form ``settled`` leaves it -- the document a reader makes of the file
-- so that ``read(write(D)) == D`` is a strict check. Nothing here needs the
engine.
"""

from __future__ import annotations

import copy

import ogxm2_blocks_cases as B
import ogxm2_fields_cases as F
from gvformat.export import _STARTING_BOARD_P1, _p1_to_absolute
from gvformat.reader import _apply_moves_p1


def v2_plies(doc: dict):
    """The plies v2 holds, in ``ply_ref`` order, with their address: every ply
    but a game's leading set-up position."""
    for gi, g in enumerate(doc["games"]):
        for pi, p in enumerate(g["plies"]):
            if not (pi == 0 and p["action_id"] == 31 and not p.get("d1")):
                yield gi, pi, p


def with_setup_start(game: dict) -> dict:
    """``game`` started from a set-up position: the board after its first play,
    stated by the side that was to move next."""
    p0, p1 = game["plies"][0], game["plies"][1]
    after = _apply_moves_p1(list(_STARTING_BOARD_P1), p0["moves"], p0["color"] == 1)
    game["plies"] = ([{"color": p1["color"], "action_id": 31, "set_position": _p1_to_absolute(after)}]
                     + game["plies"][1:])
    return game


def base() -> dict:
    """Two games, the second from a set-up position (so a ply v2 has no record
    of exists), settled."""
    games = F._games(F.QUIET_GAME, F.QUIET_GAME)
    for g in games:
        g.update(winner=1, points_won=1, is_crawford=False)
    with_setup_start(games[1])
    doc = F._shell(games, white_score=0, black_score=2, result=0)
    return B.settled(doc)


#: Every field of the clock (8.2): the four numbers, both berserk flags, an
#: unassigned flag bit, and a step other than the canonical 10 ms.
CLOCK = {"reserve_ms": 120000, "delay_ms": 12000, "increment_ms": 5000,
         "start_timestamp": 1790380800, "white_berserk": True, "black_berserk": True,
         "flags_other": 0x20, "precision": 100}

#: Every field of the video (8.3).
VIDEO = {"kind": 2, "is_live": True, "offset_ms": -250, "url": "https://twitch.tv/videos/123"}


def add_clock(doc: dict, clock: dict, readings: int, step: int = 100) -> None:
    """``clock`` on the document and a reading on each of the first ``readings``
    plies v2 holds (the series may end before the last ply). The steps grow, and
    one is long, so the series needs its unary part."""
    doc["clock"] = copy.deepcopy(clock)
    t = 0
    for i, (_gi, _pi, p) in enumerate(v2_plies(doc)):
        if i >= readings:
            break
        p["clock_ms"] = t
        t += step * (1 + i % 7) + (65000 if i == 5 else 0)


def clock_video() -> dict:
    """The clock and the video, with marks on plies of both games (one with
    every field, one with only its position, one with the wall-clock time)."""
    doc = base()
    add_clock(doc, CLOCK, 30)
    doc["video"] = copy.deepcopy(VIDEO)
    g0, g1 = doc["games"]
    g0["plies"][3].update(video_ms=5000, wall_ms=1790380805000, behind_live_ms=3000,
                          video_hand_anchored=True)
    g0["plies"][4].update(video_ms=9100)
    g1["plies"][2].update(video_ms=31000, wall_ms=1790380840000)
    g1["plies"][5].update(video_ms=40000, behind_live_ms=65534000)
    return B.settled(doc)


def clock_canonical() -> dict:
    """A clock at the canonical step, which is no key; no berserk."""
    doc = base()
    clock = {k: v for k, v in CLOCK.items() if k not in ("precision", "white_berserk", "flags_other")}
    add_clock(doc, clock, 12, step=70)
    return B.settled(doc)


def clock_empty() -> dict:
    """A clock that has no reading yet, and a video that has no marks."""
    doc = base()
    doc["clock"] = {"reserve_ms": 0, "delay_ms": 0, "increment_ms": 0, "start_timestamp": 0}
    doc["video"] = {"kind": 0}
    return B.settled(doc)


def video_url_unholdable() -> dict:
    """A URL v2 drops (8.3: not https) travels in an annotation of ours."""
    doc = base()
    doc["video"] = {"kind": 1, "url": "ftp://example.com/match.mp4"}
    doc["games"][0]["plies"][1]["video_ms"] = 100
    return B.settled(doc)


def video_dropped_mark() -> tuple[dict, dict]:
    """``(written, read back)``: a mark on a game's set-up position is on no ply
    v2 holds, so it is not written (profile section 4)."""
    doc = clock_video()
    read = copy.deepcopy(doc)
    doc["games"][1]["plies"][0]["video_ms"] = 500
    return doc, read


def clock_gap() -> tuple[dict, dict]:
    """``(written, read back)``: a reading on a ply after one without cannot be
    written (8.2), so the clock is dropped, the plies' readings with it."""
    doc = clock_video()
    del doc["games"][0]["plies"][6]["clock_ms"]
    read = copy.deepcopy(doc)
    read.pop("clock")
    for _gi, _pi, p in v2_plies(read):
        p.pop("clock_ms", None)
    return doc, read


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

#: Drawings of every shape and colour: arrows and highlights in the four colours
#: v2 names, a highlight with the default colour, and a shape and colour it does
#: not (kept as they are, I7).
DRAWINGS = [
    {"shape": 1, "at": 6, "to": 3, "color": 1},
    {"shape": 0, "at": 20},
    {"shape": 0, "at": 0, "color": 3},
    {"shape": 1, "at": 24, "to": 18, "color": 0},
    {"shape": 1, "at": 13, "to": 7, "color": 2},
    {"shape": 7, "at": 5, "color": 9},
]

#: Annotations at the match, in v2's order (N2): keyed first by ``(key, lang)``
#: with an absent language first, then prose in the order given.
MATCH = [
    {"value": "no language", "key": "x-note"},
    {"value": "Ein Kommentar", "key": "x-note", "lang": "de", "author": "Ann", "at": 1790380800123},
    {"value": "A comment", "key": "x-note", "lang": "en"},
    {"value": "A note on the match"},
    {"value": "A second note, which stays second", "author": "Bo"},
    {"value": "A third", "at": 0},
]


def annotated() -> dict:
    """An analyzed game annotated at every scope: the match, the game, plies
    (one with drawings of every shape), the checker, cube and roll decisions of
    a dice ply, the cube decision of a double, and two alternatives."""
    doc = B.settled(B.golden(B.MONEY))
    doc["annotations"] = copy.deepcopy(MATCH)
    g = doc["games"][0]
    g["annotations"] = [{"value": "The game", "key": "x-game"}, {"value": "and its prose"}]
    plies = g["plies"]
    plies[3]["annotations"] = [{"value": "", "drawings": copy.deepcopy(DRAWINGS)}]
    plies[4]["annotations"] = [{"value": "after the roll", "key": "x-tag", "author": "Cy", "at": 5},
                               {"value": "more", "drawings": [{"shape": 0, "at": 12}]}]

    a = plies[5]["analysis"]
    a["annotations"] = [{"value": "the checker play"}, {"value": "second", "key": "x-n"},
                        {"value": "the roll", "kind": 3}]
    a["annotations"].sort(key=lambda r: (r.get("kind", 0), "key" not in r))
    a["alternatives"][0]["annotations"] = [{"value": "best", "lang": "en"}]
    a["alternatives"][2]["annotations"] = [{"value": "third", "key": "x-alt"}, {"value": "and prose"}]
    a["cube_decision"]["annotations"] = [{"value": "the cube", "key": "x-c", "author": "Di"}]

    plies[9]["analysis"]["missed_double"]["annotations"] = [{"value": "a missed double"}]
    plies[20]["analysis"]["annotations"] = [{"value": "the double"}]
    return B.settled(doc)


def annotated_fallback() -> dict:
    """Annotations ``ANNO`` cannot address or hold, which travel in
    ``x-gammonview-annotations``: on a decision the ``DECS`` stream does not carry
    (its record is kept in an annotation of ours), a value past the string cap,
    drawings ``D1``/``D2``/``D3`` forbid, and a ply v2 has no record of. The ones
    ``ANNO`` can hold are first on their target, as they read back."""
    doc = clock_video()
    plies = doc["games"][0]["plies"]
    plies[1]["annotations"] = [{"value": "native"}, {"value": "x" * 5000}]
    plies[2]["annotations"] = [{"value": "", "drawings": [{"shape": 1, "at": 6}]}]
    plies[3]["annotations"] = [{"value": "", "drawings": [{"shape": 0, "at": 40}]}]
    doc["annotations"] = [{"value": "ok"}, {"value": "a drawing", "drawings": [{"shape": 0, "at": 3}]}]
    doc["games"][1]["plies"][0]["annotations"] = [{"value": "the set-up position"}]
    doc["games"][1]["annotations"] = [{"value": "game", "lang": "x" * 4097}]
    return B.settled(doc)


def annotated_decisions_fallback() -> dict:
    """A decision only the annotation holds (``producer_ref`` names a source the
    block does not state), annotated, with its alternatives."""
    doc = B.settled(B.golden(B.MONEY))
    a = doc["games"][0]["plies"][7]["analysis"]
    a["producer_ref"] = 4
    a["annotations"] = [{"value": "not in DECS"}]
    a["alternatives"][1]["annotations"] = [{"value": "nor this"}]
    a["cube_decision"]["annotations"] = [{"value": "but this is"}]
    doc["games"][0]["plies"][8]["analysis"]["annotations"] = [{"value": "native, next door"}]
    return B.settled(doc)


def all_cases() -> dict[str, dict]:
    return {
        "clock-video": clock_video(),
        "clock-canonical": clock_canonical(),
        "clock-empty": clock_empty(),
        "video-url-unholdable": video_url_unholdable(),
        "annotated": annotated(),
        "annotated-fallback": annotated_fallback(),
        "annotated-decisions-fallback": annotated_decisions_fallback(),
        "video-dropped-mark": video_dropped_mark()[0],
        "clock-gap": clock_gap()[0],
    }


#: Documents the writer must refuse (a writer error, not a quiet change).
def errors() -> dict[str, dict]:
    out = {}
    for name, ann in {
        "field-name-key": [{"value": "x", "key": "value"}],
        "our-namespace-key": [{"value": "x", "key": "x-gammonview-note"}],
        "duplicate-key": [{"value": "a", "key": "x-k"}, {"value": "b", "key": "x-k"}],
    }.items():
        doc = base()
        doc["annotations"] = ann
        out[name] = doc
    doc = B.settled(B.golden(B.MONEY))
    doc["games"][0]["plies"][5]["analysis"]["annotations"] = [{"value": "x", "kind": 9}]
    out["unknown-decision-kind"] = doc
    doc = base()
    doc["clock"] = {"reserve_ms": -1}
    out["negative-clock"] = doc
    return out


# ---------------------------------------------------------------------------
# A file from another producer, built by the reference codec
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "gvformat-js" / "test" / "fixtures" / "ogxm2"
#: Plies the foreign clock has a reading for (the series ends before the last).
FOREIGN_READINGS = 15


def _b64(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode("ascii")


def foreign_json(oracle) -> dict:
    """The reference codec's JSON for a foreign file using every field of the
    clock, the video and the annotations: HedgeHog's two-block export with a clock
    (both berserk flags and an unassigned bit), a video (a live Twitch recording
    with an offset, marks with and without wall time, lag and the hand-anchored
    flag), a signature per block, two match signatures (one covering the clock),
    and annotations at all five scopes -- prose in a stated order, keyed with and
    without a language, authors and times, drawings of every shape and colour --
    plus the two kinds it cannot place (an unknown scope, an unknown decision
    kind)."""
    j = oracle.binary_to_json((FIXTURES / "two-blocks.ogxm").read_bytes())
    first = j["analyses_info"][0]["analysis_id"]
    second = j["analyses_info"][1]["analysis_id"]

    j["clock_info"] = {"reserve_ms": 120000, "delay_ms": 12000, "increment_ms": 5000,
                       "start_timestamp": 1790442000, "flags": 3 | 0x10}
    t = n = 0
    for g in j["games"]:
        for p in g["plies"]:
            if n < FOREIGN_READINGS:
                p["timestamp_ms"] = t
                t += 1000 + 37 * n + (70000 if n == 4 else 0)
            n += 1
    j["video_info"] = {"url": "https://www.twitch.tv/videos/99", "kind": "twitch", "is_live": True,
                       "offset_ms": -300}
    g0, g1 = j["games"]
    g0["plies"][1].update(video_ms=4000, wall_ms=1790442004000, behind_live_ms=2000,
                          video_hand_anchored=True)
    g0["plies"][5]["video_ms"] = 9000
    g1["plies"][10].update(video_ms=20000, wall_ms=1790442020000, behind_live_ms=7000)

    for i, info in enumerate(j["analyses_info"]):
        info["signature"] = {"algorithm": "ed25519", "signature": _b64(bytes(range(i, i + 64))),
                             "key_id": f"test-key-{i}"}
    j["analysis_info"] = j["analyses_info"][0]
    j["match_signatures"] = [
        {"algorithm": "ed25519", "signature": _b64(bytes(range(2, 66))), "key_id": "platform"},
        {"algorithm": "ed25519", "signature": _b64(bytes(range(3, 67))), "key_id": "player",
         "covers": ["clck"]},
    ]

    def a(scope, ref, value, **kw):
        return {"scope": scope, "ref": ref, "value": value, **kw}

    drawings = [{"shape": "arrow", "at": 6, "to": 3, "color": "red"},
                {"shape": "arrow", "at": 24, "to": 18, "color": "green"},
                {"shape": "arrow", "at": 13, "to": 7, "color": "blue"},
                {"shape": "highlight", "at": 20, "color": "yellow"},
                {"shape": "highlight", "at": 0},
                {"shape": 7, "at": 5, "color": 9}]
    j["annotations"] = [
        a("match", 0, "Ein Kommentar", key="x-note", lang="de", author="Bo", at=1790442100123),
        a("match", 0, "A comment", key="x-note", lang="en"),
        a("match", 0, "A note on the match"),
        a("match", 0, "A second note", author="Ann"),
        a("game", 0, "Game zero", key="x-game"),
        a("game", 1, "Game one"),
        a("ply", 3, "hello", key="x-tag"),
        a("ply", 4, "", drawings=drawings),
        a("ply", 4, "and a word", author="Cy", at=7),
        a("decision", 2, "the checker play", kind="checker", analysis=first),
        a("decision", 2, "the cube", kind="cube", analysis=first, key="x-c"),
        a("decision", 2, "the roll", kind="roll", analysis=first, author="Di"),
        a("decision", 17, "the double", kind="cube", analysis=first),
        a("alternative", 2, "the best", kind="checker", alt_index=0, analysis=first, lang="en"),
        a("alternative", 2, "drawn", kind="checker", alt_index=3, analysis=first,
          drawings=[{"shape": "arrow", "at": 8, "to": 5, "color": "red"}]),
        a("decision", 4, "second block", kind="checker", analysis=second),
        a("alternative", 4, "and its alternative", kind="checker", alt_index=1, analysis=second),
        a(9, 0, "a scope nobody knows"),
        a("decision", 2, "a decision kind nobody knows", kind=9, analysis=first),
    ]
    j["annotations"] = _in_order(j["annotations"])
    j["checksum"] = "crc32"
    return j


def _in_order(annos: list) -> list:
    """The reference writer takes annotations as N1 and N2 order them: by target,
    keyed first by ``(key, lang)``, then prose in the order given."""
    import uuid
    scopes = {"match": 0, "game": 1, "ply": 2, "decision": 3, "alternative": 4}
    kinds = {"checker": 0, "cube": 1, "resign": 2, "roll": 3}

    def key(a):
        return (scopes.get(a["scope"], a["scope"]), a["ref"],
                uuid.UUID(a["analysis"]).bytes if "analysis" in a else b"",
                -1 if "kind" not in a else kinds.get(a["kind"], a["kind"]),
                -1 if "alt_index" not in a else a["alt_index"],
                0 if "key" in a else 1, a.get("key", "").encode(), a.get("lang", "").encode())
    return sorted(annos, key=key)


# Edits, as functions from the document a foreign file reads into to the document
# a program would hand the writer.

def _find(doc: dict, pred):
    for gi, g in enumerate(doc["games"]):
        for pi, p in enumerate(g["plies"]):
            if pred(p):
                return gi, pi, p
    raise AssertionError("no such ply")


def comment_edited(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["games"][0]["plies"][3]["annotations"][0]["value"] = "hello, edited"
    return doc


def annotation_added(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["games"][0]["plies"][6]["annotations"] = [
        {"value": "new", "key": "x-new", "drawings": [{"shape": 0, "at": 12, "color": 1}]}]
    doc["annotations"].append({"value": "one more at the match"})
    alts = doc["games"][0]["plies"][2]["analyses"][0]["alternatives"]
    alts[1]["annotations"] = [{"value": "a fresh note on alternative one"}]
    return doc


def annotation_removed(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    del doc["games"][0]["plies"][3]["annotations"]
    return doc


def clock_edited(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["games"][0]["plies"][6]["clock_ms"] += 40
    return doc


def _cube_set(on_roll: int) -> dict:
    """The cube set, by hand, to what it is: no change to the game."""
    return {"color": on_roll, "action_id": 36, "cube_value": 1, "cube_owner": 2}


def ply_removed(doc: dict) -> dict:
    """Two of game zero's plies (so the turns still alternate), which have clock
    readings and analysis and nothing else."""
    doc = copy.deepcopy(doc)
    del doc["games"][0]["plies"][7:9]
    return doc


def ply_inserted_after_clock(doc: dict) -> dict:
    """A ply after the last reading of the clock: the series is intact."""
    doc = copy.deepcopy(doc)
    plies = doc["games"][0]["plies"]
    plies.insert(FOREIGN_READINGS + 1, _cube_set(plies[FOREIGN_READINGS + 1]["color"]))
    return doc


def ply_inserted_in_clock(doc: dict) -> dict:
    """A ply among those with a reading, and none of its own: the clock cannot be written."""
    doc = copy.deepcopy(doc)
    plies = doc["games"][0]["plies"]
    plies.insert(2, _cube_set(plies[2]["color"]))
    return doc


def block_removed(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["analyses_info"].pop(1)
    for g in doc["games"]:
        for p in g["plies"]:
            if "analyses" in p:
                p["analyses"] = [x for x in p["analyses"] if x.get("analysis_index") == 0]
    return doc


EDITS = {
    "unchanged": lambda d: d,
    "comment_edited": comment_edited,
    "annotation_added": annotation_added,
    "annotation_removed": annotation_removed,
    "clock_edited": clock_edited,
    "ply_removed": ply_removed,
    "ply_inserted_after_clock": ply_inserted_after_clock,
    "ply_inserted_in_clock": ply_inserted_in_clock,
    "block_removed": block_removed,
}
