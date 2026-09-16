# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Reading an analysis block written without the [GV] extensions.

Every producer but us writes OGXM without a GVAN chunk: no decision flags, no
luck, and -- in match play -- the three cube values as raw MWC rather than as
the normalized equity we store. ``basefill.py`` completes such a block on
read. Mirrors gvformat-js/test/test-basefill.js -- keep the two in step.

Run directly:
    uv run python tests/test_basefill.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat.basefill import complete_base_block
from gvformat.binary import CHUNK_GVAN, write_gvab
from gvformat.met import eq2mwc
from gvformat.reader import read_gvab
from gvformat.stats import compute_aggregates

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        _failures.append(label)


def close(a: float, b: float, eps: float = 1e-3) -> bool:
    return abs(a - b) < eps


# ---------------------------------------------------------------------------
# A file with its GVAN chunk removed: what any other producer's file looks
# like. The header's file_size is patched; the CSUM is not, so reads pass
# verify_crc=False -- the checksum is not what these tests are about.
# ---------------------------------------------------------------------------

def _strip_gvan(data: bytes) -> bytes:
    buf = bytearray(data)
    kept = [bytes(buf[0:20])]
    pos = 20
    while len(buf) - pos > 8:
        ctype, clen = struct.unpack_from("<II", buf, pos)
        if ctype != CHUNK_GVAN:
            kept.append(bytes(buf[pos:pos + 12 + clen]))
        pos += 12 + clen
    kept.append(bytes(buf[len(buf) - 8:]))
    out = bytearray(b"".join(kept))
    struct.pack_into("<I", out, 12, len(out))
    return bytes(out)


def _read_foreign(doc: dict) -> dict:
    return read_gvab(_strip_gvan(write_gvab(doc)), verify_crc=False)


def _eval_of(win: float) -> dict:
    return {
        "win": win, "gammon_win": 0.12, "bg_win": 0.01,
        "gammon_loss": 0.11, "bg_loss": 0.01,
        "equity": round(win + 0.12 + 0.01 - 0.11 - 0.01, 4),
    }


def _checker(equities: list[float], decision: bool, extra: dict | None = None) -> dict:
    """A checker analysis whose candidates are ``equities``, best first.

    ``decision`` is set by hand throughout this file, to what a person
    reading the candidates would say. That hand labelling is the
    specification these tests hold the derivation to, and it is what makes
    the last one meaningful: strip the flags, derive them back, and the
    rating has to be the same number.
    """
    out = {
        "eval": _eval_of(0.50), "best_equity": equities[0],
        "played_equity": equities[0], "equity_loss": 0.0, "decision": decision,
        "alternatives": [
            {"move": [{"from": 13, "pips": 3}], "equity": equity, "is_played": i == 0,
             "eval": _eval_of(0.5 - i * 0.01)}
            for i, equity in enumerate(equities)
        ],
    }
    if extra:
        out.update(extra)
    return out


def _match() -> dict:
    return {
        "match_length": 7, "player_white": "W", "player_black": "B",
        "white_score": 0, "black_score": 0,
        "analysis_info": {"ply": 2, "model_id": "test", "timestamp": 0},
        "games": [{
            "game_index": 0, "winner": 1, "points_won": 1, "plies": [
                # 0: a real choice -- the candidates disagree
                {"color": 1, "action_id": 6, "d1": 2, "d2": 3,
                 "moves": [{"from": 13, "pips": 3}, {"from": 13, "pips": 2}],
                 "analysis": _checker([0.20, 0.05, -0.10], True)},
                # 1: forced -- one candidate is no decision at all
                {"color": 0, "action_id": 6, "d1": 2, "d2": 3,
                 "moves": [{"from": 13, "pips": 3}, {"from": 13, "pips": 2}],
                 "analysis": _checker([0.10], False)},
                # 2: every candidate scores the same -- decided before it was reached
                {"color": 1, "action_id": 6, "d1": 2, "d2": 3,
                 "moves": [{"from": 13, "pips": 3}, {"from": 13, "pips": 2}],
                 "analysis": _checker([0.10, 0.10], False)},
                # 3: a live cube above a checker play, and a close one
                {"color": 0, "action_id": 6, "d1": 2, "d2": 3,
                 "moves": [{"from": 13, "pips": 3}, {"from": 13, "pips": 2}],
                 "analysis": _checker([0.30, 0.10], True, {"cube_decision": {
                     "should_double": False, "no_double_equity": 0.40,
                     "double_take_equity": 0.35, "double_pass_equity": 1.0,
                     "action": "no_double", "decision": True}})},
                # 4: a cube nowhere near being turned. Its negative values are
                # also what tells a reader this block is in equity and not in
                # MWC -- a real match is full of them, and a probability
                # never is.
                {"color": 1, "action_id": 6, "d1": 2, "d2": 3,
                 "moves": [{"from": 13, "pips": 3}, {"from": 13, "pips": 2}],
                 "analysis": _checker([0.05, -0.05], True, {"cube_decision": {
                     "should_double": False, "no_double_equity": -0.30,
                     "double_take_equity": -0.75, "double_pass_equity": 1.0,
                     "action": "no_double", "decision": False}})},
                # 5: a double worth making
                {"color": 1, "action_id": 21, "analysis": {
                    "correct_action": "double", "played_action": "double",
                    "no_double_equity": 0.55, "double_take_equity": 0.62,
                    "double_pass_equity": 1.0, "equity_loss": 0.0, "decision": True}},
                # 6: the take that answers it
                {"color": 0, "action_id": 22, "analysis": {
                    "correct_action": "take", "played_action": "take",
                    "no_double_equity": 0.55, "double_take_equity": 0.62,
                    "double_pass_equity": 1.0, "equity_loss": 0.0, "decision": True}},
                {"color": 1, "action_id": 24},
            ],
        }],
    }


def main() -> int:
    print("--- 1. a block with GVAN is left exactly as it was written ---")
    native = read_gvab(write_gvab(_match()))
    check("_base_analyses" not in native,
          "1. our own file reports no base-only block")
    check(native["games"][0]["plies"][0]["analysis"]["decision"] is True
          and native["games"][0]["plies"][1]["analysis"]["decision"] is False,
          "1. and its stored flags are read as stored, not re-derived")

    print()
    print("--- 2. decision flags are derived when GVAN is absent ---")
    foreign = _read_foreign(_match())
    flags = [(p.get("analysis") or {}).get("decision") for p in foreign["games"][0]["plies"]]
    check(foreign.get("_base_analyses") == [0],
          "2. the block is reported as base-only")
    check(flags[0] is True, "2. candidates that disagree are a decision")
    check(flags[1] is False, "2. a forced move is not")
    check(flags[2] is False, "2. nor is a position where every move scores the same")
    check(flags[5] is True, "2. a double worth making counts")
    check(flags[6] is True, "2. so does the take that answers it")
    check(foreign["games"][0]["plies"][4]["analysis"]["cube_decision"]["decision"] is False,
          "2. and a cube nowhere near being turned does not")
    check(foreign["games"][0]["plies"][3]["analysis"]["cube_decision"]["decision"] is True,
          "2. a live cube above a checker play is judged on its own triviality")

    print()
    print("--- 3. a decision too clear to count is not one ---")
    doc = _match()
    # The doubler is 0.5 ahead of both answers: nobody had to think.
    doc["games"][0]["plies"][5]["analysis"].update(
        no_double_equity=0.9, double_take_equity=0.4, double_pass_equity=1.0)
    # A take and a pass worth the same: no choice was posed.
    doc["games"][0]["plies"][6]["analysis"].update(
        no_double_equity=0.55, double_take_equity=1.0, double_pass_equity=1.0)
    flags = [(p.get("analysis") or {}).get("decision") for p in _read_foreign(doc)["games"][0]["plies"]]
    check(flags[5] is False, "3. a cube nobody had to think about does not count")
    check(flags[6] is False, "3. nor a take/pass worth the same either way")

    print()
    print("--- 4. an illegal play is excluded, however wide the spread ---")
    # Through the function rather than through a file: `illegal_move` is
    # itself a [GV] flag, so a block with no GVAN cannot be carrying one. The
    # guard is here for a caller that completes a block for some other
    # reason, and it is still the rule -- the player broke the rules, they
    # did not choose badly.
    analysis = _checker([0.20, 0.05, -0.10], True, {"illegal_move": True})
    ply = {"color": 1, "action_id": 6, "ogid_before": None}
    complete_base_block({(0, 0): analysis}, {(0, 0): ply}, None)
    check(analysis["decision"] is False,
          "4. an illegal play poses no decision, whatever its candidates say")

    print()
    print("--- 5. match-play cube values arrive as MWC and are converted back ---")
    # Forward-map the file's normalized values into the MWC the base spec
    # stores, which is what another producer would have written. Reading
    # must give the originals back.
    doc = _match()

    def to_mwc(eq: float) -> float:
        return round(eq2mwc(eq, 7, 7, 1, False) * 10000) / 10000

    original = []
    for ply in doc["games"][0]["plies"]:
        for sub in (ply.get("analysis"), (ply.get("analysis") or {}).get("cube_decision")):
            if not sub or sub.get("no_double_equity") is None:
                continue
            original.append((sub["no_double_equity"], sub["double_take_equity"], sub["double_pass_equity"]))
            sub["no_double_equity"] = to_mwc(sub["no_double_equity"])
            sub["double_take_equity"] = to_mwc(sub["double_take_equity"])
            sub["double_pass_equity"] = to_mwc(sub["double_pass_equity"])
    foreign = _read_foreign(doc)
    got = []
    for ply in foreign["games"][0]["plies"]:
        for sub in (ply.get("analysis"), (ply.get("analysis") or {}).get("cube_decision")):
            if not sub or sub.get("no_double_equity") is None:
                continue
            got.append((sub["no_double_equity"], sub["double_take_equity"], sub["double_pass_equity"]))
    check(len(got) == len(original) and len(got) == 4,
          "5. every cube payload was seen (two live cubes, the double, the take)")
    check(all(all(close(v, original[i][j], 2e-3) for j, v in enumerate(row)) for i, row in enumerate(got)),
          "5. and each came back to the equity it was written from")
    check(all(close(row[2], 1.0, 2e-3) for row in got),
          "5. a pass is +1 on the normalized scale, which is what makes this checkable")

    print()
    print("--- 6. money play is left alone: those values are already equity ---")
    doc = _match()
    doc["match_length"] = 0
    doc["white_score"] = 0
    doc["black_score"] = 0
    cube = _read_foreign(doc)["games"][0]["plies"][5]["analysis"]
    check(close(cube["no_double_equity"], 0.55) and close(cube["double_pass_equity"], 1.0),
          "6. a money cube keeps the values it was written with")

    print()
    print("--- 7. a value outside [0,1] proves the block is already equity ---")
    # Our own numbers with the GVAN dropped -- a stripped file, not a foreign
    # one. A doubler who is behind is negative, and no MWC ever is, so the
    # conversion must not fire.
    doc = _match()
    doc["games"][0]["plies"][5]["analysis"]["no_double_equity"] = -0.35
    cube = _read_foreign(doc)["games"][0]["plies"][5]["analysis"]
    check(close(cube["no_double_equity"], -0.35) and close(cube["double_take_equity"], 0.62),
          "7. the values are recognised as equity and left where they are")

    print()
    print("--- 8. EVAL probabilities left unset fall back to the best move ---")
    doc = _match()
    doc["games"][0]["plies"][0]["analysis"]["eval"] = {
        "win": 0, "gammon_win": 0, "bg_win": 0, "gammon_loss": 0, "bg_loss": 0, "equity": 0}
    native = read_gvab(write_gvab(doc))["games"][0]["plies"][0]["analysis"]
    check(close(native["eval"]["win"], native["alternatives"][0]["eval"]["win"]),
          "8. all-zero probabilities mean \"not recorded\", and the best move has them")
    check(not close(native["eval"]["win"], 0),
          "8. so the position is not reported as 0% to win")

    print()
    print("--- 9. the header adopts a depth its decisions agree on ---")
    doc = _match()
    doc["analysis_info"] = {"ply": 0, "model_id": "", "timestamp": 0}
    for ply in doc["games"][0]["plies"]:
        if ply.get("analysis") is not None:
            ply["analysis"]["ply"] = 3
    info = _read_foreign(doc)["analysis_info"]
    check(info["ply"] == 3, "9. a block whose every decision says 3-ply is a 3-ply block")
    check(info["eval_level"] == "3ply", "9. and can say so where a level is shown")

    print()
    print("--- 10. a completed block rates like the one it was written from ---")
    # The whole point, end to end: strip our own flags, derive them back, and
    # the performance rating has to land on the number bgsage itself computed.
    doc = _match()
    for ply in doc["games"][0]["plies"]:
        analysis = ply.get("analysis")
        if analysis is not None and analysis.get("alternatives") is not None:
            analysis["equity_loss"] = 0.15
    native = compute_aggregates(read_gvab(write_gvab(doc)))["match"]
    foreign = compute_aggregates(_read_foreign(doc))["match"]
    check(native["white"]["total_decisions"] == foreign["white"]["total_decisions"]
          and native["black"]["total_decisions"] == foreign["black"]["total_decisions"],
          "10. the same plies count on both sides")
    check(native["white"]["pr"] == foreign["white"]["pr"]
          and native["black"]["pr"] == foreign["black"]["pr"],
          "10. and the ratings are identical")

    print()
    print(f"{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
