# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The OGXM v2 match, player and game fields are document keys.

Phase 1 of bringing every v2 field into the document: the match context
(event year, stage, round, table, city, country, event URL, platform, match
reference, ``rated``, ``completed_at``, ``player_seat``, ``date_precision``,
``crawford_before_start``), the two player profiles, the rules beyond the four
we had, the starting score, the variant, each game's cube and termination, and
the plies v2 has beyond a move list (a beaver, a raccoon, a cube set by hand, a
settlement, a turn with no recorded roll, an action nothing here knows).

Four kinds of valid file used to be refused outright -- a match joined part-way
through, a game opening with the cube turned, automatic doubles, a variant.
They are read now, and the score, the cube and the opening position flow into
every position string.

What is checked, over synthetic documents (``ogxm2_fields_cases.py``):

1. ``read(write(D)) == D``, rewriting is byte-stable, and the document survives
   the ``.gva`` JSON route.
2. The reference codec (``libogxm``, skipped without it) loads each file, finds
   no rule broken, replays it, re-encodes it to the same bytes -- and derives
   the same position strings (OGIDs) our reader does.
3. A value v2 cannot hold goes into an ``x-gammonview-<field>`` annotation and
   comes back whole; the file is still valid v2 (P6).
4. A file the *reference* wrote, using every field, reads into the expected
   document and rewrites byte for byte. Edits to it keep what they do not touch.

Run directly:
    uv run python tests/test_ogxm2_fields.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_fields_cases as C  # noqa: E402
import ogxm2_oracle as oracle  # noqa: E402
from gvformat import GvabError, read_gvab, write_gvab  # noqa: E402
from gvformat import ogxm2 as R  # noqa: E402
from gvformat import ogxm2_passthrough as P  # noqa: E402

FIXTURES = _REPO_ROOT / "gvformat-js" / "test" / "fixtures" / "ogxm2"

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


def first_diff(a, b, path: str = "") -> str | None:
    """Where two documents first differ, for a failure message."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return f"{path}.{k}: {a.get(k, '<absent>')!r} vs {b.get(k, '<absent>')!r}"
            d = first_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} items vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r} vs {b!r}"


def annotations(data: bytes) -> list[dict]:
    out = []
    for stype, _pos, payload in R._walk_sections(data, len(data)):
        if stype == b"ANNO":
            out += R._decode_anno(payload)
    return out


def gv_keys(data: bytes) -> set[tuple[int, int, str]]:
    return {(r["scope"], r["ref"], r["key"]) for r in annotations(data)
            if r.get("key", "").startswith(R.GV_PREFIX)
            and not r["key"].startswith(R.GV_KEY_ANALYSIS)}


def reference_ogids(label: str, ref: dict, back: dict, games: int | None = None) -> None:
    """The reference derives the same position strings. A marker's colour is a
    separate matter (the reference stamps it by seat 0) and is left out; the
    hypergammon OGID's trailing ``:3`` (its checker count) is ours only."""
    bad = []
    hyper = back.get("variant") == 2
    for gi, (rg, g) in enumerate(zip(ref["games"][:games], back["games"][:games])):
        for pi, (a, b) in enumerate(zip(rg["plies"], g["plies"])):
            if b["action_id"] in (24, 25, 26, 30):
                continue
            for key in ("ogid_before", "ogid_after"):
                want, got = a.get(key), b.get(key)
                if hyper and got:
                    got = got.rsplit(":", 1)[0]
                if want != got and not (want is None and got is None):
                    bad.append((gi, pi, key, want, got))
    check(not bad, f"{label}: the reference derives the same OGIDs ({bad[:2]})")


def main() -> int:
    try:
        lib = oracle.version()
        print(f"reference: libogxm {lib}")
    except oracle.OracleUnavailable as exc:
        lib = None
        print(f"reference: unavailable ({exc}) -- reference checks skipped")

    cases = C.all_cases()
    cases["foreign"] = copy.deepcopy(cases["foreign"])

    # --- 1. every document round-trips --------------------------------------
    print("--- 1. the document comes back whole ---")
    refs: dict = {}
    for name, doc in cases.items():
        data = write_gvab(doc)
        back = read_gvab(data)
        if name == "foreign":
            back.pop(P.KEY, None)               # the reference stores a score_final we do not
        d = first_diff(doc, back)
        check(d is None, f"1. {name}: read(write(D)) == D" + ("" if d is None else f" -- {d}"))
        again = read_gvab(data)
        check(write_gvab(again) == data, f"1. {name}: rewriting is byte-stable")
        via_gva = json.loads(json.dumps(again))
        check(write_gvab(via_gva) == data, f"1. {name}: and so is the .gva route")
        if lib is not None:
            try:
                ref = oracle.binary_to_json(data)
                problems = []
                if oracle.check_json(ref) != "ok":
                    problems.append(f"breaks {oracle.check_json(ref)}")
                if not ref.get("replay_complete", True) and name != "unknown-action":
                    problems.append("does not replay to the end")
                if oracle.json_to_binary(ref) != data:
                    problems.append("re-encodes to other bytes")
                refs[name] = ref
            except ValueError as exc:
                problems = [f"refused: {exc}"]
            check(not problems, f"1. {name}: the reference accepts it" + (
                "" if not problems else f" -- {'; '.join(problems)}"))
            if name in refs and name != "unknown-action":
                # A cube above the limit is held in an annotation (4), which only
                # we read: the reference sees a game that opens at 1.
                reference_ogids(f"1. {name}", refs[name], again,
                                games=1 if name == "unholdable" else None)

    # --- 2. the score, the cube and the opening flow into the positions -----
    print("\n--- 2. what each field does to the positions ---")
    mid = read_gvab(write_gvab(cases["midmatch"]))
    check(mid["score_start"] == [2, 2] and (mid["white_score"], mid["black_score"]) == (7, 2),
          "2. a match joined at 2-2 ends 7-2 (the final score includes the start)")
    check([g["is_crawford"] for g in mid["games"]] == [False, True],
          "2. its Crawford game is found from the starting score (6-2 after the first)")
    check(mid["games"][0]["plies"][0]["ogid_before"].split(":")[6:9] == ["2", "2", "7"],
          "2. the first position says 2-2 in a 7-point match")
    check(mid["games"][1]["plies"][0]["ogid_before"].split(":")[6:9] == ["6", "2", "7C"],
          "2. the second says 6-2, Crawford")

    cube = read_gvab(write_gvab(cases["pre-turned-cube"]))
    cube_fields = [g["plies"][0]["ogid_before"].split(":")[2] for g in cube["games"]]
    check(cube_fields == ["B1N", "N2N", "W3N"],
          f"2. games open with the cube as stated: a 2 Black owns, a centred 4, an 8 White owns ({cube_fields})")
    check(cube["auto_doubles"] is True and cube["games"][1]["auto_doubles"] == 2
          and cube["games"][0]["initial_cube_value"] == 2 and cube["games"][0]["initial_cube_owner"] == 1
          and cube["games"][2]["termination"] == 0,
          "2. the rule, each game's automatic doubles, cube and termination are keys")

    for v, suffix in ((1, "nackgammon"), (2, "hypergammon"), (3, "longgammon")):
        doc = read_gvab(write_gvab(cases[suffix]))
        first = doc["games"][0]["plies"][0]["ogid_before"]
        check(doc["variant"] == v and first.endswith(":3") == (v == 2),
              f"2. {suffix}: variant {v}, and only hypergammon's OGID states its checker count")
    nack = read_gvab(write_gvab(cases["nackgammon"]))["games"][0]["plies"][0]["ogid_before"]
    check(nack.startswith("1122cccchhhjjjj:6666888ddddnnoo:"), "2. the nackgammon opening is its own position")

    # --- 3. the fields and the profiles -------------------------------------
    print("\n--- 3. match context and profiles ---")
    ctx_data = write_gvab(cases["context"])
    ctx = read_gvab(ctx_data)
    check(all(ctx[k] == v for k, v in C.CONTEXT.items()),
          "3. every context field is a key, the profiles with their ratings")
    check(ctx["site"] == "Oslo" and not any(k[2] == R.GV_KEY_SITE for k in gv_keys(ctx_data)),
          "3. site is the city, and stated nowhere else")
    check(gv_keys(ctx_data) == set(), "3. nothing needed an annotation")
    if "context" in refs:
        j = refs["context"]
        want = {"completed_at": C.CONTEXT["completed_at"], "player_seat": "black",
                "event": "Nordic Open", "event_year": 2026, "date_precision": "day",
                "stage": "Final", "round": 3, "table": "T4", "city": "Oslo", "country": "NO",
                "event_url": "https://example.com/nordic/2026", "site": "opengammon.com",
                "match_ref": "m-2026.10_7", "rated": True, "auto_doubles": True,
                "crawford_played_before_start": True, "rules_unknown": [5]}
        check(all(j.get(k) == v for k, v in want.items()),
              f"3. the reference reads them under its own names ({[k for k, v in want.items() if j.get(k) != v]})")
        check(j["white_profile"] == {"user_id": "u-1001", "rating": 1523.47,
                                     "rating_system": "opengammon", "country": "NO", "kind": "bot"}
              and j["black_profile"]["kind"] == "human",
              "3. and the profiles, a rating to the hundredth")
        check(j["games"][0]["termination"] == "resigned", "3. and a game's termination")

    # --- 4. what v2 cannot hold ----------------------------------------------
    print("\n--- 4. values v2 cannot hold travel in annotations ---")
    un_data = write_gvab(cases["unholdable"])
    expected = {(R.SCOPE_MATCH, 0, R.GV_PREFIX + k) for k in (
        "event_year", "date_precision", "stage", "round", "table", "city", "country", "event_url",
        "platform", "match_ref", "player_seat", "rules_other", "site",
        "white_profile.user_id", "white_profile.rating", "white_profile.rating_system",
        "white_profile.country", "black_profile.user_id", "black_profile.rating")}
    expected |= {(R.SCOPE_GAME, 0, R.GV_PREFIX + k) for k in (
        "initial_cube_value", "initial_cube_owner", "auto_doubles")}
    expected |= {(R.SCOPE_GAME, 1, R.GV_PREFIX + "initial_cube_value")}
    got = gv_keys(un_data)
    check(got == expected, f"4. each value is in an annotation, and nothing else is "
                           f"(missing {sorted(expected - got)}, extra {sorted(got - expected)})")
    if "unholdable" in refs:
        j = refs["unholdable"]
        absent = ("event_year", "date_precision", "stage", "round", "table", "city", "country",
                  "event_url", "match_ref", "player_seat")
        check(not any(k in j for k in absent) and j.get("site") != "OpenGammon.com",
              "4. and none of them is in a v2 field")
        check(j["white_profile"] == {"kind": "bot"} and j["black_profile"] == {"country": "SE"},
              f"4. a profile keeps what v2 can hold ({j['white_profile']}, {j['black_profile']})")
        check("initial_cube_value" not in j["games"][0] and j["games"][1]["termination"] == "forfeited",
              "4. a game keeps its termination, loses a cube v2 would refuse")
    # No annotation of ours is left in the document's own annotation lists.
    check("annotations" not in read_gvab(un_data), "4. the reader consumes them: no annotations key")

    # --- 5. defaults are not written ----------------------------------------
    print("\n--- 5. a value equal to v2's default is not written ---")
    plain = copy.deepcopy(cases["midmatch"])
    plain.pop("score_start")
    plain.update(white_score=plain["white_score"], black_score=plain["black_score"])
    with_defaults = copy.deepcopy(plain)
    with_defaults.update(variant=0, score_start=[0, 0], rated=False, crawford_before_start=False,
                         auto_doubles=False, rules_other=0, completed_at=None)
    for g in with_defaults["games"]:
        g.update(initial_cube_value=1, initial_cube_owner=2, auto_doubles=0)
    check(write_gvab(with_defaults) == write_gvab(plain),
          "5. variant 0, a 0-0 start, cube 1 centred, no automatic doubles and false flags write nothing")

    # The read-back rules (profile section 4): what a document may say that
    # comes back as something else, and why.
    ex = copy.deepcopy(cases["extras"])
    resign = next(p for p in ex["games"][1]["plies"] if p["action_id"] == R.ACTION_RESIGN_GAME)
    resign["resign_value"] = 1                       # what the points and the cube derive
    settle = next(p for p in ex["games"][0]["plies"] if p["action_id"] == R.ACTION_SETTLE)
    settle["settle_value"] = 4.0000004               # v2 holds millionths
    ex_back = read_gvab(write_gvab(ex))
    check("resign_value" not in next(p for p in ex_back["games"][1]["plies"]
                                     if p["action_id"] == R.ACTION_RESIGN_GAME)
          and next(p for p in ex_back["games"][2]["plies"]
                   if p["action_id"] == R.ACTION_RESIGN_GAME)["resign_value"] == 2,
          "5. rule resign-derived: a resign_value the points and cube give is not kept; another is")
    check(next(p for p in ex_back["games"][0]["plies"]
               if p["action_id"] == R.ACTION_SETTLE)["settle_value"] == 4.0,
          "5. rule settle-millionths: a settlement is held to a millionth of a point")
    bare = copy.deepcopy(cases["context"])
    del bare["site"]
    check(read_gvab(write_gvab(bare))["site"] == "Oslo",
          "5. rule site-follows-place: a document with a city and no site reads with the city as its site")

    # --- 6. a file from the reference ----------------------------------------
    print("\n--- 6. a file the reference wrote, with every field ---")
    foreign_bytes = (FIXTURES / "fields.ogxm").read_bytes()
    if lib is not None:
        check(oracle.json_to_binary(C.foreign_json(oracle)) == foreign_bytes,
              "6. fields.ogxm is what the reference writes for the case's JSON")
        fj = oracle.binary_to_json(foreign_bytes)
        check(oracle.check_json(fj) == "ok" and fj["replay_complete"]
              and fj["variant"] == "nackgammon" and fj["white_score_start"] == 3,
              "6. it is valid v2, replays, and is a nackgammon match joined at 3-5")
    doc = read_gvab(foreign_bytes)
    held = doc.pop(P.KEY, None)
    d = first_diff(cases["foreign"], doc)
    check(d is None, "6. read_gvab gives the expected document" + ("" if d is None else f" -- {d}"))
    check({"variant", "score_start", "auto_doubles", "white_profile", "black_profile", "platform",
           "event_year", "city", "country", "match_ref", "rated"} <= set(doc)
          and doc["games"][0]["auto_doubles"] == 1 and doc["games"][1]["initial_cube_value"] == 4,
          "6. the nackgammon match, its start, cube, automatic double, profiles, city, platform and year")
    check(held is not None and set(held) >= {"mtch"}
          and not held.get("anno") and not held.get("unknown") and not held.get("blocks"),
          "6. all that is kept beside the document is the stated final score, which is derived")
    if lib is not None:
        reference_ogids("6. fields.ogxm", oracle.binary_to_json(foreign_bytes), doc)
    check(write_gvab(read_gvab(foreign_bytes)) == foreign_bytes,
          "6. rewriting it unedited is byte for byte the same")
    check(write_gvab(json.loads(json.dumps(read_gvab(foreign_bytes)))) == foreign_bytes,
          "6. and so is the .gva route")

    def edited(change) -> tuple[bytes, dict]:
        d = read_gvab(foreign_bytes)
        change(d)
        out = write_gvab(d)
        return out, read_gvab(out)

    def valid(label: str, data: bytes) -> None:
        if lib is not None:
            j = oracle.binary_to_json(data)
            check(oracle.check_json(j) == "ok" and oracle.json_to_binary(j) == data
                  and j["replay_complete"], f"{label}: still valid v2, and the reference re-encodes it")

    out, back = edited(lambda d: d.update(player_white="Alicia"))
    base = read_gvab(foreign_bytes)
    base.pop(P.KEY)
    base["player_white"] = "Alicia"
    back.pop(P.KEY, None)
    check(first_diff(base, back) is None, "6. a rename keeps every other field")
    valid("6. a rename", out)

    mtch_of = lambda data: R._decode_mtch(next(  # noqa: E731
        p for t, _o, p in R._walk_sections(data, len(data)) if t == b"MTCH"))
    check(mtch_of(foreign_bytes)["started_at"] % 1000 == 123 and mtch_of(out)["started_at"] % 1000 == 123,
          "6. a rename keeps the milliseconds the source stated for the start (P4)")
    out2, back2 = edited(lambda d: d.update(timestamp=d["timestamp"] + 1))
    check(mtch_of(out2)["started_at"] == (back2["timestamp"]) * 1000,
          "6. and a changed second is written as a whole one")

    out, back = edited(lambda d: d.update(event=None))
    check("event" not in back or not back["event"], "6. an event removed is gone")
    check(back["event_year"] == 2026, "6. its year is not lost: a year needs an event, so it travels in an annotation")
    check(("x-gammonview-event_year" in {k[2] for k in gv_keys(out)}), "6. -- and the annotation says so")
    valid("6. no event", out)

    out, back = edited(lambda d: d.update(city="X" * 90))
    check(back["city"] == "X" * 90 and back["site"] == "Oslo",
          "6. a city too long for v2 comes back whole; the site, now another text, has its own record")
    valid("6. a long city", out)

    out, back = edited(lambda d: d.pop("platform"))
    check("platform" not in back and back["match_ref"] == "m-2026.10_7"
          and back["white_profile"]["user_id"] == "u-1001",
          "6. a platform removed leaves the match reference and the user ids, in annotations")
    valid("6. no platform", out)

    out, back = edited(lambda d: d["white_profile"].update(rating=1600.5))
    check(back["white_profile"]["rating"] == 1600.5 and back["black_profile"] == doc["black_profile"],
          "6. a rating edit keeps the other profile")
    valid("6. a rating edit", out)

    out, back = edited(lambda d: d["games"][0].update(initial_cube_value=8))
    check(back["games"][0]["initial_cube_value"] == 8, "6. a game's cube edit")
    valid("6. a cube edit", out)

    out, back = edited(lambda d: d.update(score_start=[1, 6]))
    check(back["score_start"] == [1, 6], "6. a starting score edit")
    check(back["games"][0]["plies"][0]["ogid_before"].split(":")[6:8] == ["1", "6"],
          "6. -- and every position follows it")

    # --- 7. refusals that remain are the format's own -----------------------
    print("\n--- 7. what is still refused ---")
    sample = bytearray(write_gvab(cases["extras"]))
    # Action 35 is reserved: writing it is the one thing the format forbids.
    forbidden = copy.deepcopy(cases["extras"])
    forbidden["games"][1]["plies"][1]["action_id"] = 35
    try:
        write_gvab(forbidden)
        refused = False
    except ValueError:
        refused = True
    check(refused, "7. the writer will not write the reserved action 35")
    try:
        R.read_ogxm2(bytes(sample[:-20]))
        bad = False
    except GvabError:
        bad = True
    check(bad, "7. and a damaged file is still a GvabError")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
