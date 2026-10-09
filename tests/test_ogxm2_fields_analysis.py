# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The engine analyzes a match that opens mid-way or with the cube turned.

``gvanalysis/ogxm_reconstructor.py`` turns an OGXM document back into the
decisions the engine evaluates. It used to assume every match opens 0-0 and every
game with a centred cube at 1; a v2 file can say otherwise (``score_start``, a
game's ``initial_cube_value`` / ``initial_cube_owner`` / ``auto_doubles``, a
cube set by hand), and the engine plays backgammon only, so a variant is refused
with a clear error instead.

Needs the engine (the ``[engine]`` extra). The documents are
``tests/ogxm2_fields_cases.py``'s.

Run directly:
    uv run python tests/test_ogxm2_fields_analysis.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_fields_cases as C  # noqa: E402
import ogxm2_oracle as oracle  # noqa: E402
from gvanalysis.match import analyze_ogxm  # noqa: E402
from gvanalysis.ogxm_reconstructor import (  # noqa: E402
    UnsupportedMatch, reconstruct_decisions_from_ogxm,
)
from gvformat import append_analysis, compute_aggregates, read_gvab, to_ogxm_json, write_gvab  # noqa: E402
from gvformat import ogxm2 as R  # noqa: E402

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


def beavered(answer: list[str]) -> dict:
    """A money game in which Black doubles on its third turn and White answers
    with a beaver, or a beaver and a raccoon. The plies are a real game's, so
    every play is legal; the game is worth the cube it ends at."""
    ids = {"double": R.ACTION_DOUBLE, "beaver": R.ACTION_BEAVER, "raccoon": R.ACTION_RACCOON}
    # Black (colour 0) opens; the doubler is the one on roll, the beaverer the other.
    colour = {"double": 0, "beaver": 1, "raccoon": 0}
    real = C._quiet_plies()
    plies = real[:2] + [{"color": colour[a], "action_id": ids[a]} for a in answer] + real[2:]
    cube = {2: 4, 3: 8}[len(answer)]
    game = {"game_index": 0, "winner": 1, "points_won": cube, "is_crawford": False,
            "is_lastgame": False, "first_to_move": 0, "plies": plies}
    return C._shell([game], beaver=True, raccoon=True, white_score=0, black_score=cube, result=0)


def raises(fn) -> str | None:
    try:
        fn()
    except UnsupportedMatch as exc:
        return str(exc)
    return None


def main() -> int:
    cases = C.all_cases()

    # --- a match joined at 2-2 ----------------------------------------------
    mid = cases["midmatch"]
    recs = reconstruct_decisions_from_ogxm(mid)
    check([(r["sw"], r["sb"]) for r in recs] == [(2, 2), (6, 2)],
          "the games start at 2-2 and then 6-2 (the starting score, then White's four)")
    d0, d1 = recs[0]["decisions"][0], recs[1]["decisions"][0]
    check((d0["away1"], d0["away2"]) == (5, 5), "the first decision is at 5-away / 5-away")
    # Black opens, so the mover is the one 5-away and the opponent White, 1-away.
    check(recs[1]["is_crawford"] and d1["is_crawford"] and (d1["away1"], d1["away2"]) == (5, 1),
          "and the second game is the Crawford game, White 1-away")

    # --- a cube that is already turned ---------------------------------------
    cube = cases["pre-turned-cube"]
    first = [r["decisions"][0] for r in reconstruct_decisions_from_ogxm(cube)]
    got = [(d["cube_value"], d["cube_owner"]) for d in first]
    # Black opens this game: a cube Black owns is the mover's.
    check(got == [(2, "player"), (4, "centered"), (8, "opponent")],
          f"each game opens with its own cube: a 2 the mover owns, a centred 4, an 8 the opponent owns ({got})")

    # --- a cube set by hand mid-game -----------------------------------------
    hand = copy.deepcopy(cases["extras"])
    g = hand["games"][0]
    g["plies"] = [p for p in g["plies"] if p["action_id"] not in (
        R.ACTION_DOUBLE, R.ACTION_BEAVER, R.ACTION_RACCOON)]
    recs = reconstruct_decisions_from_ogxm(hand)
    values = [(d["cube_value"], d["cube_owner"]) for d in recs[0]["decisions"] if d["kind"] == "checker"]
    check(values[:4] == [(1, "centered")] * 4
          and values[4:] and all(v == w for v, w in zip(values[4:], [(4, "opponent"), (4, "player")] * 9)),
          f"a cube set to 4 owned by White changes every decision after it ({values[:6]})")

    # --- what the engine cannot play -----------------------------------------
    for name in ("nackgammon", "hypergammon", "longgammon"):
        msg = raises(lambda name=name: reconstruct_decisions_from_ogxm(cases[name]))
        check(msg is not None and "variant" in msg, f"{name}: refused, saying it is a backgammon variant")

    # --- a beaver and a raccoon ----------------------------------------------
    for name, plies, cube, owner in (
            ("beaver", ["double", "beaver"], 4, "White"),
            ("raccoon", ["double", "beaver", "raccoon"], 8, "Black")):
        doc = beavered(plies)
        recs = reconstruct_decisions_from_ogxm(doc)
        ds = recs[0]["decisions"]
        resp = next(d for d in ds if d["kind"] == "cube" and d["doubled"])
        check(resp["response"] == "take" and resp["cube_value"] == 1
              and resp["responder"] == doc["player_white"],
              f"{name}: the response is analyzed as the take it implies, at the cube before the double")
        after = [d for d in ds if d["kind"] == "checker" and d["cube_value"] > 1]
        check(after and all(d["cube_value"] == cube for d in after)
              and all(d["cube_owner"] == ("player" if d["is_p1"] == (owner == "White") else "opponent")
                      for d in after),
              f"{name}: every play after it is at {cube}, owned by {owner} ({len(after)} decisions)")
        check(not any(d["kind"] == "cube" and d["doubled"] and d is not resp for d in ds),
              f"{name}: and the raccoon adds no decision of its own")

    # --- the whole pipeline, on a mid-match start and a pre-turned cube ------
    for name, doc in (("midmatch", cases["midmatch"]), ("pre-turned-cube", cases["pre-turned-cube"]),
                      ("beaver", beavered(["double", "beaver"])),
                      ("raccoon", beavered(["double", "beaver", "raccoon"]))):
        data = analyze_ogxm(doc, preset="very_quick", quiet=True, jobs=1)
        out = append_analysis(doc, to_ogxm_json(data, keep_orientation=True))
        analyzed = sum(1 for g in out["games"] for p in g["plies"] if p.get("analysis"))
        check(analyzed > 20, f"{name}: analyzed ({analyzed} plies carry analysis)")
        if name in ("beaver", "raccoon"):
            fresh = next(p for p in out["games"][0]["plies"] if p["action_id"] == R.ACTION_BEAVER)
            ans = fresh.get("analysis") or {}
            check(ans.get("played_action") == "beaver" and ans.get("correct_action") in ("take", "pass"),
                  f"{name}: a fresh analysis of the beaver says it was a beaver, judged as the take it answers")
        blob = write_gvab(out)
        back = read_gvab(blob)
        check(write_gvab(back) == blob and back.get("score_start") == doc.get("score_start"),
              f"{name}: the analyzed document writes as v2 and comes back, start and all")
        if name in ("beaver", "raccoon"):
            by_action = {p["action_id"]: p for p in back["games"][0]["plies"]
                         if p["action_id"] in (R.ACTION_BEAVER, R.ACTION_RACCOON)}
            ans = (by_action[R.ACTION_BEAVER].get("analysis") or {})
            check(ans.get("correct_action") in ("take", "pass", "beaver") and ans.get("played_action") == "beaver"
                  and "analysis" not in by_action.get(R.ACTION_RACCOON, {}),
                  f"{name}: the answer to the double is analyzed on the beaver ply, the raccoon has none")
        agg = compute_aggregates(back)["match"]
        check(agg["white"]["total_decisions"] > 10 and agg["white"]["pr"] is not None
              and agg["black"]["pr"] is not None, f"{name}: its PR is computed")
        try:
            j = oracle.binary_to_json(blob)
            check(oracle.check_json(j) == "ok" and j["replay_complete"]
                  and oracle.json_to_binary(j) == blob,
                  f"{name}: the reference accepts the analyzed file")
        except oracle.OracleUnavailable:
            pass

    # --- the engine plays on at the beavered cube ----------------------------
    # Analyzing the plies after a beaver is analyzing them at the cube it leaves:
    # the same game with that cube stated outright (a cube set by hand) must give
    # the same numbers, and a game with no beaver must not.
    def best_equities(doc: dict) -> list:
        data = analyze_ogxm(doc, preset="very_quick", quiet=True, jobs=1)
        out = append_analysis(doc, to_ogxm_json(data, keep_orientation=True))
        return [(p["action_id"], (p.get("analysis") or {}).get("best_equity"))
                for p in out["games"][0]["plies"]]

    plain = beavered(["double", "beaver"])
    stated = copy.deepcopy(plain)
    stated["games"][0]["plies"][2:4] = [{"color": 0, "action_id": R.ACTION_CUBE_SET,
                                         "cube_value": 4, "cube_owner": 0}]
    undoubled = copy.deepcopy(plain)
    undoubled["games"][0]["plies"][2:4] = []
    beaver_eq, stated_eq, undoubled_eq = (best_equities(d) for d in (plain, stated, undoubled))
    check(beaver_eq[4:] == stated_eq[3:],
          "a beavered game's plays are judged as a game whose cube was set to 4 for White")
    check(beaver_eq[4:] != undoubled_eq[2:],
          "and not as one that never doubled")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
