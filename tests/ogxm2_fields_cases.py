# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Synthetic documents for the OGXM v2 match, player and game fields.

Shared by ``test_ogxm2_fields.py`` and by ``gen_ogxm2_fields_fixtures.py``, which
pins what the Python writer makes of each so that
``gvformat-js/test/test-ogxm2-fields.js`` can be held to the same bytes.

Every document is built from ``samples/mat/3WNK_g1Z-PLsh_HyvQ5j4a.mat`` (real
plies, so every play is legal and the reference codec can replay them) in the
form ``read_gvab`` returns, so that ``read(write(D)) == D`` is a strict check.
Nothing here needs the engine.
"""

from __future__ import annotations

import base64
import copy
from pathlib import Path

from gvformat import convert_mat, read_gvab, write_gvab
from gvformat import ogxm2 as R
from gvformat.reader import _derive_ogids

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAT = _REPO_ROOT / "samples" / "mat" / "3WNK_g1Z-PLsh_HyvQ5j4a.mat"

#: Index of the game in the sample match that has no cube action and is neither
#: the Crawford game nor the last (the ply stream every pre-turned-cube case plays).
QUIET_GAME = 6
CRAWFORD_GAME = 5
FOUR_POINTER = 3


def _normalized() -> dict:
    return read_gvab(write_gvab(convert_mat(_MAT)))


def _games(*indexes: int) -> list[dict]:
    base = _normalized()
    out = []
    for new, i in enumerate(indexes):
        g = copy.deepcopy(base["games"][i])
        g["game_index"] = new
        out.append(g)
    return out


def _shell(games: list[dict], **keys) -> dict:
    doc = {k: v for k, v in _normalized().items() if k != "games"}
    doc.update(keys)
    doc["games"] = games
    _derive_ogids(doc)
    return doc


def midmatch() -> dict:
    """A 7-point match joined at 2-2. White's four pointer makes it 6-2, so the
    next game is the Crawford game, which White wins to finish 7-2."""
    games = _games(FOUR_POINTER, CRAWFORD_GAME)
    games[0]["is_crawford"], games[1]["is_crawford"] = False, True
    return _shell(games, score_start=[2, 2], white_score=7, black_score=2, result=1)


def pre_turned_cube() -> dict:
    """Games that open with the cube already turned: owned at 2, then two
    automatic doubles (a centred 4), then owned at 8 (2 doubled twice)."""
    games = _games(QUIET_GAME, QUIET_GAME, QUIET_GAME)
    games[0].update(initial_cube_value=2, initial_cube_owner=1)
    games[1].update(auto_doubles=2)
    games[2].update(initial_cube_value=2, initial_cube_owner=0, auto_doubles=2, termination=0)
    for g in games:
        g.update(winner=1, points_won=1, is_crawford=False)
    return _shell(games, auto_doubles=True, white_score=0, black_score=3, result=0,
                  crawford=False)


def _ply(color: int, d1: int, d2: int, moves: list[dict]) -> dict:
    a = R._dice_action_id(d1, d2)
    return {"color": color, "action_id": a, "d1": min(d1, d2), "d2": max(d1, d2), "moves": moves}


#: Two opening plays that are legal in each variant's opening position
#: (White 3-1 then Black, using the variant's own checkers).
_VARIANT_PLAYS = {
    1: [(1, 3, 1, [(17, 3), (19, 1)]), (0, 6, 5, [(24, 6), (18, 5)])],
    2: [(1, 3, 1, [(1, 3), (2, 1)]), (0, 4, 2, [(24, 4), (22, 2)])],
    3: [(1, 6, 5, [(1, 6), (1, 5)]), (0, 4, 3, [(24, 4), (24, 3)])],
}


def variant(v: int) -> dict:
    """An unfinished game of nackgammon (1), hypergammon (2) or longgammon (3)."""
    plies = [_ply(c, a, b, [{"from": f, "pips": p} for f, p in steps])
             for c, a, b, steps in _VARIANT_PLAYS[v]]
    game = {"game_index": 0, "winner": 255, "points_won": 0, "is_crawford": False,
            "is_lastgame": False, "first_to_move": 1, "plies": plies}
    return _shell([game], variant=v, match_length=3, white_score=0, black_score=0, result=0)


CONTEXT = {
    "completed_at": 1790380800000 + 5_000_000,
    "player_seat": 1,
    "event": "Nordic Open",
    "event_year": 2026,
    "date_precision": 0,
    "stage": "Final",
    "round": 3,
    "table": "T4",
    "city": "Oslo",
    "country": "NO",
    "event_url": "https://example.com/nordic/2026",
    "platform": "opengammon.com",
    "match_ref": "m-2026.10_7",
    "rated": True,
    "white_profile": {"user_id": "u-1001", "rating": 1523.47, "rating_system": "opengammon",
                      "country": "NO", "kind": 1},
    "black_profile": {"user_id": "u-2002", "rating": 1400.0, "rating_system": "opengammon",
                      "country": "SE", "kind": 0},
}


def context() -> dict:
    """Every match-level field v2 defines, each one valid."""
    games = _games(QUIET_GAME)
    games[0]["termination"] = 2
    return _shell(games, timestamp=1790380800, site="Oslo", rules_other=0x20,
                  crawford_before_start=True, auto_doubles=True, white_score=0, black_score=1,
                  result=0, **copy.deepcopy(CONTEXT))


#: Every value v2 cannot hold, one per rule it breaks (P6). Each travels in an
#: ``x-gammonview-<field>`` annotation and comes back whole.
UNHOLDABLE = {
    "event_year": 2025,                      # needs an event: this document has none
    "date_precision": 2,                     # needs a started_at on a year boundary
    "stage": "S" * 61,                       # 60 bytes at most
    "round": 100,                            # 1-99
    "table": "t" * 25,                       # 24 bytes at most
    "city": "c" * 81,                        # 80 bytes at most
    "country": "Norway",                     # two upper-case letters
    "event_url": "http://example.com/",      # https only
    "platform": "OpenGammon.com",            # lower case
    "match_ref": "m-1",                      # needs a platform that is written
    "player_seat": 0,                        # White is a bot below
    "rules_other": 0x03,                     # overlaps the rules v2 assigns
    "white_profile": {"user_id": "u-1", "rating": 1500.123, "rating_system": "elo",
                      "country": "usa", "kind": 1},
    "black_profile": {"user_id": "x" * 65, "rating": 1500.0, "country": "SE"},
}


def unholdable() -> dict:
    games = _games(QUIET_GAME, QUIET_GAME)
    games[0].update(initial_cube_value=3, initial_cube_owner=3, auto_doubles=-1)
    games[1].update(initial_cube_value=128, termination=4)
    for g in games:
        g.update(winner=1, points_won=1, is_crawford=False)
    # `timestamp` is mid-year, not on a boundary.
    return _shell(games, timestamp=1790380800 + 3600, site="Somewhere", white_score=0,
                  black_score=2, result=0, **copy.deepcopy(UNHOLDABLE))


def _quiet_plies() -> list[dict]:
    return copy.deepcopy(_games(QUIET_GAME)[0]["plies"])


def extras() -> dict:
    """The plies v2 has beyond the ones a move list needs: a double that is
    beavered and raccooned, a cube set by hand, turns with no recorded roll, a
    settlement, resignations (one worth other than its points), and an action
    nothing here knows."""
    real = _quiet_plies()
    w, b = 1, 0                       # colours: Black opens this game
    a = real[:2] + [
        {"color": b, "action_id": R.ACTION_DOUBLE},
        {"color": w, "action_id": R.ACTION_BEAVER},
        {"color": b, "action_id": R.ACTION_RACCOON},
    ] + real[2:4] + [
        {"color": b, "action_id": R.ACTION_CUBE_SET, "cube_value": 4, "cube_owner": 0},
    ] + real[4:6] + [
        {"color": b, "action_id": R.ACTION_PASS},
        {"color": w, "action_id": R.ACTION_PASS},
    ] + real[6:8] + [
        {"color": b, "action_id": R.ACTION_SETTLE, "settle_value": 4.0},
        {"color": b, "action_id": 24},
    ]
    settled = {"game_index": 0, "winner": 1, "points_won": 4, "is_crawford": False,
               "is_lastgame": False, "first_to_move": 0, "termination": 3,
               "plies": copy.deepcopy(a)}

    def resigned(index: int, value: int | None, points: int) -> dict:
        plies = copy.deepcopy(real[:4]) + [{"color": b, "action_id": R.ACTION_RESIGN_GAME},
                                           {"color": w, "action_id": 24}]
        if value is not None:
            plies[4]["resign_value"] = value
        return {"game_index": index, "winner": 0, "points_won": points, "is_crawford": False,
                "is_lastgame": False, "first_to_move": 0, "termination": 2, "plies": plies}

    games = [settled, resigned(1, None, 1), resigned(2, 2, 1), resigned(3, None, 2)]
    return _shell(games, beaver=True, raccoon=True, cube_limit=64, white_score=3, black_score=4,
                  result=0)


def unknown_action() -> dict:
    """An action id past the ones assigned: kept whole, extras and all."""
    real = _quiet_plies()
    # Extras records (3.2) of a producer's own: bit 9, which v2 leaves to later
    # versions, and for an escaped id also bit 7, the id itself (70 = 0x46).
    mark = {"color": 0, "action_id": 40,
            "extras_raw": base64.b64encode(bytes([4, 0x80, 0x04, 0x61, 0x62])).decode()}
    escaped = {"color": 1, "action_id": 70,
               "extras_raw": base64.b64encode(bytes([4, 0x80, 0x05, 0x46, 0x63])).decode()}
    plies = real[:2] + [mark] + real[2:3] + [escaped] + real[3:6] + [{"color": 0, "action_id": 24}]
    game = {"game_index": 0, "winner": 1, "points_won": 1, "is_crawford": False,
            "is_lastgame": False, "first_to_move": 0, "plies": plies}
    return _shell([game], white_score=0, black_score=1, result=0)


# ---------------------------------------------------------------------------
# A file from another producer, built by the reference codec
# ---------------------------------------------------------------------------

#: Seat, termination, date precision and player kind as the reference's JSON
#: projection names them (J3).
SEAT = {0: "white", 1: "black", 2: "neither"}
TERMINATION = {0: "played_out", 1: "dropped", 2: "resigned", 3: "settled", 4: "forfeited"}
PRECISION = {0: "day", 1: "month", 2: "year"}
KIND = {0: "human", 1: "bot", 2: "engine"}


def foreign_doc() -> dict:
    """The document a nackgammon match joined at 3-5 reads into, using every
    field this phase adds: a pre-turned cube and an automatic double, match
    context (event and year, city, country and platform), and two players with
    ratings."""
    g0, g1 = (copy.deepcopy(variant(1)["games"][0]) for _ in range(2))
    g0.update(game_index=0, winner=0, points_won=2, initial_cube_owner=1, auto_doubles=1,
              termination=1)
    g1.update(game_index=1, initial_cube_value=4, initial_cube_owner=0)
    return _shell([g0, g1], variant=1, match_length=9, score_start=[3, 5], white_score=5,
                  black_score=5, result=0, auto_doubles=True,
                  **{k: v for k, v in CONTEXT.items() if k != "date_precision"},
                  timestamp=1790380800 + 7200, player_white="Astrid", player_black="Bjorn",
                  crawford=True, jacoby=False, site="Oslo")


def foreign_json(oracle) -> dict:
    """The reference codec's JSON for ``foreign_doc``: the plain nackgammon
    match through our writer for its plies, then every new field set by hand in
    the reference's own vocabulary, so the bytes are the reference's and not
    ours."""
    doc = foreign_doc()
    plain = {k: v for k, v in doc.items() if k in (
        "match_length", "variant", "player_white", "player_black", "crawford", "jacoby",
        "timestamp", "source", "cube_limit", "event", "site")}
    plain["games"] = [
        {k: v for k, v in g.items() if k in ("game_index", "winner", "points_won", "is_crawford",
                                              "is_lastgame", "first_to_move", "plies")}
        for g in copy.deepcopy(doc["games"])]
    for g in plain["games"]:
        for p in g["plies"]:
            p.pop("ogid_before", None)
            p.pop("ogid_after", None)
    plain.update(white_score=2, black_score=0, result=0, event=None, site=None)
    j = oracle.binary_to_json(write_gvab(plain))
    j.pop("annotations", None)
    for g in j["games"]:
        for p in g["plies"]:
            p.pop("ogid_before", None)
            p.pop("ogid_after", None)
    c = CONTEXT
    j["started_at"] = doc["timestamp"] * 1000 + 123       # the document counts seconds
    j.update(
        white_score_start=3, black_score_start=5, white_score=5, black_score=5, result=0,
        auto_doubles=True, completed_at=c["completed_at"], player_seat=SEAT[c["player_seat"]],
        event=c["event"], event_year=c["event_year"], stage=c["stage"], round=c["round"],
        table=c["table"], city=c["city"], country=c["country"], event_url=c["event_url"],
        site=c["platform"], match_ref=c["match_ref"], rated=True,
        white_profile={**{k: v for k, v in c["white_profile"].items() if k != "kind"},
                       "kind": KIND[c["white_profile"]["kind"]]},
        black_profile={**{k: v for k, v in c["black_profile"].items() if k != "kind"},
                       "kind": KIND[c["black_profile"]["kind"]]},
    )
    j["games"][0].update(initial_cube_owner="black", auto_doubles=1, termination=TERMINATION[1])
    j["games"][1].update(initial_cube_value=4, initial_cube_owner="white")
    return j


def all_cases() -> dict[str, dict]:
    return {
        "midmatch": midmatch(),
        "pre-turned-cube": pre_turned_cube(),
        "nackgammon": variant(1),
        "hypergammon": variant(2),
        "longgammon": variant(3),
        "context": context(),
        "unholdable": unholdable(),
        "extras": extras(),
        "unknown-action": unknown_action(),
        "foreign": foreign_doc(),
    }
