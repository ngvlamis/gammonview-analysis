# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Validation harness for read_gvab.read_gvab, the inverse of gvab.write_gvab.

The reader is defined as the byte-level inverse of the writer, so the primary
correctness property is a round-trip:

    write_gvab(read_gvab(write_gvab(ogxm))) == write_gvab(ogxm)

That is checked two ways -- on a real analyzed match from the
corpus (which organically exercises checker evals, alternatives, cube decisions, missed
doubles, live-checker cubes, luck, cube_limit, and event/site) and on a synthetic
match that additionally exercises set_position
(with and without dice), resign, and raccoon -- plus field-level spot checks
that decoded values match the
source dict, and (when libogxm is importable) that read_gvab's base-field
decode agrees with the reference codec's own binary_to_json.

Run directly:
    uv run python tests/test_read_gvab.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvanalysis import analyze_mat
from gvformat.export import to_ogxm_json
from gvformat.binary import write_gvab, VERSION_MAJOR, VERSION_MINOR
from gvformat.reader import read_gvab, canonicalize, GvabError
from gvformat.stats import compute_aggregates

from fixtures import sample_mat, missing_mat_message  # noqa: E402

MAT_PATH = sample_mat()

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"FAIL  {label}")
    else:
        print(f"OK    {label}")


def _first_diff(a: bytes, b: bytes) -> int | None:
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def _roundtrip(ogxm: dict, label: str) -> dict:
    """Assert write->read->write is byte-identical; return the decoded dict."""
    b1 = write_gvab(ogxm)
    decoded = read_gvab(b1)
    b2 = write_gvab(decoded)
    ok = b1 == b2
    check(ok, f"{label}: write_gvab(read_gvab(b)) == b ({len(b1)} bytes)")
    if not ok:
        idx = _first_diff(b1, b2)
        print(f"      first diff @ {idx}; len {len(b1)} vs {len(b2)}")
        if idx is not None:
            lo = max(0, idx - 6)
            print(f"      b1: {b1[lo:idx + 10].hex()}")
            print(f"      b2: {b2[lo:idx + 10].hex()}")
    return decoded


def _synthetic() -> dict:
    board = [0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5,
             5, 0, 0, 0, -3, 0, -5, 0, 0, 0, 0, 2, 0]
    return {
        "match_length": 5, "player_white": "Alice", "player_black": "Bob",
        "crawford": True, "jacoby": False, "beaver": True, "raccoon": True,
        "cube_limit": 64, "event": "Synthetic", "site": "Somewhere",
        "white_score": 1, "black_score": 2, "result": 0, "source": 0,
        "timestamp": 1234567890,
        "analysis_info": {"ply": 3, "eval_level": "3ply", "luck_eval_level": "1ply",
                          "model_id": "bgsage", "timestamp": 1234567890,
                          "duration_ms": 4200},
        "games": [
            {
                "game_index": 0, "points_won": 2, "winner": 0, "is_crawford": False,
                "is_lastgame": False, "first_to_move": 0,
                "initial_board": list(board),
                "plies": [
                    {"color": 0, "action_id": 31, "set_position": list(board),
                     "d1": 3, "d2": 1},
                    {"color": 1, "action_id": 31, "set_position": list(board)},
                    {"color": 1, "action_id": 20, "d1": 6, "d2": 6,
                     "moves": [{"from": 13, "pips": 6}, {"from": 13, "pips": 6},
                               {"from": 24, "pips": 6}, {"from": 24, "pips": 6}],
                     "analysis": {
                         "eval": {"win": 0.6, "gammon_win": 0.2, "bg_win": 0.01,
                                  "gammon_loss": 0.1, "bg_loss": 0.005, "equity": 0.705},
                         "best_equity": 0.705, "equity_loss": 0.0, "decision": True,
                         "ply": 4,
                         "luck": 0.07,
                         "alternatives": [
                             {"move": [{"from": 13, "pips": 6}], "equity": 0.705,
                              "eval": {"win": 0.6, "gammon_win": 0.2, "bg_win": 0.01,
                                       "gammon_loss": 0.1, "bg_loss": 0.005, "equity": 0.705},
                              "is_played": True, "eval_level": "4ply"},
                             {"move": [{"from": 24, "pips": 6}], "equity": 0.66,
                              "is_played": False},
                         ],
                         "cube_decision": {
                             "should_double": False, "no_double_equity": 0.7,
                             "double_take_equity": 0.9, "double_pass_equity": 1.0,
                             "action": "no_double", "decision": True, "eval_level": "3ply"},
                     }},
                    {"color": 0, "action_id": 5, "d1": 1, "d2": 6,
                     "moves": [{"from": 8, "pips": 6}, {"from": 6, "pips": 1}],
                     "analysis": {
                         "eval": {"win": 0.45, "gammon_win": 0.1, "bg_win": 0.0,
                                  "gammon_loss": 0.15, "bg_loss": 0.01, "equity": 0.24},
                         "best_equity": 0.24, "equity_loss": 0.08, "decision": True,
                         "illegal_move": True, "alternatives": [],
                         "missed_double": {
                             "no_double_equity": 0.24, "double_take_equity": 0.5,
                             "double_pass_equity": 1.0, "equity_loss": 0.3,
                             "correct_action": "double", "eval_level": "2ply",
                             "eval": {"win": 0.62, "gammon_win": 0.21, "bg_win": 0.02,
                                      "gammon_loss": 0.08, "bg_loss": 0.01,
                                      "equity": 0.24}},
                     }},
                    {"color": 1, "action_id": 21, "analysis": {
                        "correct_action": "double", "played_action": "double",
                        "no_double_equity": 0.4, "double_take_equity": 0.55,
                        "double_pass_equity": 1.0, "equity_loss": 0.0,
                        "decision": True, "eval_level": "3ply",
                        "eval": {"win": 0.7, "gammon_win": 0.3, "bg_win": 0.05,
                                 "gammon_loss": 0.05, "bg_loss": 0.0, "equity": 1.0}}},
                    {"color": 0, "action_id": 22, "analysis": {
                        "correct_action": "take", "played_action": "take",
                        "no_double_equity": 0.4, "double_take_equity": 0.55,
                        "double_pass_equity": 1.0, "equity_loss": 0.0, "decision": True}},
                    {"color": 1, "action_id": 27, "analysis": {
                        "resign_error": 0.12, "take_resign_error": 0.03,
                        "equity_loss": 0.12, "decision": True,
                        "eval": {"win": 0.9, "gammon_win": 0.4, "bg_win": 0.1,
                                 "gammon_loss": 0.01, "bg_loss": 0.0, "equity": 1.39}}},
                    {"color": 0, "action_id": 24},
                ],
            },
            {
                "game_index": 1, "points_won": 0, "winner": 255, "is_crawford": False,
                "is_lastgame": True, "first_to_move": 1, "plies": [],
            },
        ],
    }


def main() -> int:
    # --- 1. Real match round-trip + field checks ---
    if MAT_PATH is None:
        print(missing_mat_message())
        return 0

    print(f"--- 1. Real match round-trip ({MAT_PATH.name}) ---")
    result = analyze_mat(MAT_PATH, preset="very_quick", quiet=True)
    ogxm = to_ogxm_json(result)
    decoded = _roundtrip(ogxm, "1. real match")

    check(decoded["match_length"] == ogxm["match_length"], "1. match_length decodes")
    check(decoded["player_white"] == ogxm["player_white"], "1. player_white decodes")
    check(decoded["player_black"] == ogxm["player_black"], "1. player_black decodes")
    check(decoded["event"] == ogxm["event"], f"1. event decodes ({decoded['event']!r})")
    check(decoded["site"] == ogxm["site"], f"1. site decodes ({decoded['site']!r})")
    check(decoded["cube_limit"] == ogxm["cube_limit"], "1. cube_limit decodes")
    check(len(decoded["games"]) == len(ogxm["games"]), "1. game count decodes")
    check(all(len(d["plies"]) == len(o["plies"]) for d, o in zip(decoded["games"], ogxm["games"])),
          "1. per-game ply counts decode")
    # MWC anchors were removed from the format entirely (compute-on-read only,
    # via gvformat.met) -- confirm no analysis object carries them.
    has_anchor = any(
        isinstance(p.get("analysis"), dict)
        and ("mwc_on_win" in p["analysis"] or "mwc_on_loss" in p["analysis"])
        for g in decoded["games"] for p in g["plies"]
    )
    check(not has_anchor, "1. no analysis carries mwc_on_win/mwc_on_loss (removed from format)")
    luck_ply = next((p for g in decoded["games"] for p in g["plies"]
                     if isinstance(p.get("analysis"), dict) and "luck" in p["analysis"]), None)
    check(luck_ply is not None, "1. found a decision carrying luck")
    ai = decoded.get("analysis_info", {})
    check(ai.get("eval_level") == ogxm["analysis_info"]["eval_level"],
          f"1. analysis_info.eval_level decodes ({ai.get('eval_level')!r})")
    # Compared against the source, not a literal: this section tests that the
    # label survives the binary, and the label now names the engine build
    # (gv-bgsage/<version>), which a literal here would pin twice over.
    check(ai.get("model_id") == ogxm["analysis_info"]["model_id"],
          f"1. analysis_info.model_id decodes ({ai.get('model_id')!r})")

    # A decoded checker analysis matches the source's equities.
    matched = 0
    for og, dg in zip(ogxm["games"], decoded["games"]):
        for op, dp in zip(og["plies"], dg["plies"]):
            oa, da = op.get("analysis"), dp.get("analysis")
            if isinstance(oa, dict) and "best_equity" in oa and isinstance(da, dict):
                check(abs(da["best_equity"] - oa["best_equity"]) < 5e-5,
                      f"1. checker best_equity decodes (~{oa['best_equity']:.4f})")
                check(abs(da["equity_loss"] - oa["equity_loss"]) < 5e-5,
                      "1. checker equity_loss decodes")
                check(len(da["alternatives"]) == len(oa["alternatives"]),
                      "1. alternative count decodes")
                matched += 1
                break
        if matched:
            break
    check(matched > 0, "1. found a checker decision to spot-check")

    # Derived ogids must be byte-identical to what to_ogxm_json produced
    # (which is itself byte-identical to libogxm) for every ply.
    nb = na = bad_b = bad_a = 0
    for og, dg in zip(ogxm["games"], decoded["games"]):
        for op, dp in zip(og["plies"], dg["plies"]):
            if op.get("ogid_before") is not None:
                nb += 1
                bad_b += op["ogid_before"] != dp.get("ogid_before")
            if op.get("ogid_after") is not None:
                na += 1
                bad_a += op["ogid_after"] != dp.get("ogid_after")
    check(nb > 0 and bad_b == 0, f"1. all {nb} ogid_before derived correctly ({bad_b} wrong)")
    check(na > 0 and bad_a == 0, f"1. all {na} ogid_after derived correctly ({bad_a} wrong)")
    check(decoded["white_score"] == ogxm["white_score"]
          and decoded["black_score"] == ogxm["black_score"],
          "1. reconstructed final scores match (accumulation from 0-0)")
    print()

    # --- 2. Synthetic round-trip + edge-path field checks ---
    print("--- 2. Synthetic round-trip (set_position/resign/raccoon) ---")
    synth = _synthetic()
    d = _roundtrip(synth, "2. synthetic")
    check(d["raccoon"] is True, "2. raccoon decodes")
    g0 = d["games"][0]
    check(g0["plies"][0].get("d1") == 3 and g0["plies"][0].get("d2") == 1,
          "2. set_position with dice decodes")
    check("d1" not in g0["plies"][1], "2. set_position without dice omits dice")
    check(g0["plies"][0]["set_position"] == synth["games"][0]["plies"][0]["set_position"],
          "2. set_position board decodes")
    cd = g0["plies"][2]["analysis"].get("cube_decision")
    check(isinstance(cd, dict) and cd["should_double"] is False and cd["eval_level"] == "3ply",
          "2. cube_decision (type=4) decodes onto checker ply")
    a2 = g0["plies"][2]["analysis"]
    check(abs(a2.get("luck", 0) - 0.07) < 1e-6, "2. luck decodes")
    md = g0["plies"][3]["analysis"].get("missed_double")
    check(isinstance(md, dict) and md["correct_action"] == "double",
          "2. missed_double (type=2) decodes onto checker ply")
    # The pre-roll probabilities of the cube decision. Without them a viewer
    # showing the cube has only the checker ply's post-roll eval to fall back
    # on, which describes a different question entirely.
    md_eval = md.get("eval") if isinstance(md, dict) else None
    check(isinstance(md_eval, dict)
          and abs(md_eval["win"] - 0.62) < 1e-3
          and abs(md_eval["gammon_win"] - 0.21) < 1e-3
          and abs(md_eval["bg_loss"] - 0.01) < 1e-3,
          f"2. missed_double pre-roll probs round-trip ({md_eval})")
    check(isinstance(md, dict) and md.get("eval_level") == "2ply",
          "2. missed_double eval_level round-trips")
    check(g0["plies"][3]["analysis"].get("illegal_move") is True, "2. illegal_move decodes")
    ra = g0["plies"][6]["analysis"]
    check(abs(ra.get("resign_error", 0) - 0.12) < 1e-6
          and abs(ra.get("take_resign_error", 0) - 0.03) < 1e-6, "2. resign (type=3) decodes")
    check(d["event"] == "Synthetic" and d["site"] == "Somewhere",
          "2. event and site decode as separate fields")
    print()

    # --- 2b. Legacy MHDR: one combined "Event • Site" string, no site field ---
    # Files written before the two were split carry the combined form; the
    # reader recovers the pair so they decode the same as a new file.
    print("--- 2b. Legacy combined event string ---")
    legacy = _synthetic()
    legacy["event"] = "Synthetic • Somewhere"
    legacy.pop("site")
    dl = read_gvab(write_gvab(legacy))
    check(dl["event"] == "Synthetic" and dl["site"] == "Somewhere",
          f"2b. legacy combined event splits ({dl['event']!r}, {dl['site']!r})")
    # No separator -> all event. Guessing "site" would move data between two
    # columns that mean different things.
    unsplit = _synthetic()
    unsplit["event"] = "Just An Event"
    unsplit.pop("site")
    du = read_gvab(write_gvab(unsplit))
    check(du["event"] == "Just An Event" and du["site"] is None,
          "2b. an unseparated legacy string stays entirely in event")
    # Healing is a one-time fixup: re-encoding the decoded doc is stable.
    check(write_gvab(dl) == write_gvab(read_gvab(write_gvab(dl))),
          "2b. the healed document is a write/read fixed point")
    print()

    # --- 2c. canonicalize(): freshly generated JSON == read-back JSON ---
    print("--- 2c. canonicalize() idempotency / fixed point ---")

    def _first_alt(doc):
        for g in doc["games"]:
            for p in g["plies"]:
                alts = (p.get("analysis") or {}).get("alternatives")
                if alts:
                    return alts[0]
        return None

    canon = canonicalize(ogxm)  # ogxm = raw to_ogxm_json output from step 1
    check(canon == read_gvab(write_gvab(ogxm)),
          "2c. canonicalize(J) == read_gvab(write_gvab(J))")
    check(canonicalize(canon) == canon, "2c. canonicalize is idempotent")
    check(read_gvab(write_gvab(canon)) == canon,
          "2c. canonical form is a write/read fixed point (A == B)")
    raw_alt, canon_alt = _first_alt(ogxm), _first_alt(canon)
    check(raw_alt is not None and "notation" in raw_alt,
          "2c. raw exporter output carries display fields (notation)")
    check(canon_alt is not None and "notation" not in canon_alt,
          "2c. canonical form drops non-stored display fields")
    check(ogxm is not canon and ogxm["games"] is not canon["games"],
          "2c. canonicalize does not mutate its input")
    print()

    # --- 3. CRC verification guard ---
    print("--- 3. CRC guard ---")
    b = bytearray(write_gvab(synth))
    b[40] ^= 0xFF
    raised = False
    try:
        read_gvab(bytes(b))
    except GvabError:
        raised = True
    check(raised, "3. corrupted stream raises GvabError")
    ok_noverify = True
    try:
        read_gvab(bytes(b), verify_crc=False)
    except GvabError:
        ok_noverify = False
    check(ok_noverify, "3. verify_crc=False tolerates bad checksum")
    print()

    print("--- 3b. min_reader version guard ---")
    # The header states the minimum spec version a reader needs. A file that
    # demands more than we implement must fail as a version mismatch rather
    # than be parsed optimistically at the wrong layout.
    import struct as _struct

    def _with_min_reader(raw: bytes, maj: int, minr: int) -> bytes:
        b = bytearray(raw)
        _struct.pack_into("<HH", b, 8, maj, minr)
        return bytes(b)

    base = write_gvab(synth)
    hdr_rmaj, hdr_rmin = _struct.unpack_from("<HH", base, 8)
    check((hdr_rmaj, hdr_rmin) <= (VERSION_MAJOR, VERSION_MINOR),
          f"3b. we write a min_reader we can read ({hdr_rmaj}.{hdr_rmin})")

    for maj, minr, label in ((VERSION_MAJOR, VERSION_MINOR + 1, "a later minor"),
                             (VERSION_MAJOR + 1, 0, "a later major")):
        raised = ""
        try:
            read_gvab(_with_min_reader(base, maj, minr), verify_crc=False)
        except GvabError as exc:
            raised = str(exc)
        check("requires an OGXM" in raised,
              f"3b. {label} ({maj}.{minr}) is refused: {raised or 'no error!'}")

    # The boundary is >, not >=: a file needing exactly what we implement reads.
    # (verify_crc=False throughout -- patching the header invalidates the CSUM,
    # and the checksum is not what is under test here.)
    ok_exact = True
    try:
        read_gvab(_with_min_reader(base, VERSION_MAJOR, VERSION_MINOR),
                  verify_crc=False)
    except GvabError:
        ok_exact = False
    check(ok_exact, "3b. min_reader == our version still reads")

    # An older file is fine, and the check is not a checksum concern -- it
    # fires before CSUM and before the file_size compare, so neither
    # verify_crc=False nor a mangled size can talk us past it.
    ok_older = True
    try:
        read_gvab(_with_min_reader(base, 1, 0), verify_crc=False)
    except GvabError:
        ok_older = False
    check(ok_older, "3b. an older min_reader still reads")

    too_new = bytearray(_with_min_reader(base, VERSION_MAJOR, VERSION_MINOR + 1))
    _struct.pack_into("<I", too_new, 12, 0)  # zeroed file_size, would be skipped
    raised = ""
    try:
        read_gvab(bytes(too_new), verify_crc=False)
    except GvabError as exc:
        raised = str(exc)
    check("requires an OGXM" in raised,
          "3b. refused even with verify_crc=False and no file_size")
    print()

    # --- 4. Cross-check base fields vs libogxm (optional) ---
    print("--- 4. Cross-check vs libogxm (optional) ---")
    try:
        import json
        # Optional: HedgeHog's reference codec, from
        # https://gitlab.com/eranlambooij/hedgehog-public (`make libogxm`).
        sys.path.insert(0, str(Path("~/projects/hedgehog-public/examples").expanduser()))
        import ogxm_ctypes
        ref = json.loads(ogxm_ctypes.binary_to_json(write_gvab(ogxm)))
        ours = read_gvab(write_gvab(ogxm))
        check(ref.get("match_length") == ours["match_length"], "4. match_length agrees with libogxm")
        check(ref.get("player_white") == ours["player_white"], "4. player_white agrees with libogxm")
        check(len(ref.get("games", [])) == len(ours["games"]), "4. game count agrees with libogxm")
    except (Exception, SystemExit) as e:  # noqa: BLE001
        # ogxm_ctypes raises SystemExit -- not an Exception -- when the
        # .so is missing, so catching Exception alone let it kill the run.
        print(f"   skipped (libogxm unavailable: {e})")
    print()

    # --- 5. Malformed input always raises GvabError, never a stray exception ---
    #
    # read_gvab is handed untrusted uploads (GammonView's analysis service parses
    # every upload with it), so "not well-formed" has to arrive as GvabError and
    # nothing else. Before this was enforced, a corrupt stream could surface a
    # raw IndexError from the ply decoder's `data[pos]`.
    print("--- 5. Malformed input raises GvabError ---")
    import random

    blob = write_gvab(ogxm)
    rng = random.Random(20260801)
    leaked: list[str] = []
    for _ in range(4000):
        b = bytearray(blob)
        for _ in range(rng.randint(1, 12)):
            b[rng.randrange(len(b))] = rng.randrange(256)
        r = rng.random()
        if r < 0.25:
            b = b[:rng.randrange(0, len(b))]
        elif r < 0.35:
            b += bytes(rng.randrange(1, 64))
        try:
            read_gvab(bytes(b))
        except GvabError:
            pass
        except Exception as exc:  # noqa: BLE001 - the thing under test
            leaked.append(f"{type(exc).__name__}: {exc}")
    check(not leaked, f"5. 4000 mutated streams raise only GvabError (leaked {len(leaked)})")
    if leaked:
        for msg in leaked[:5]:
            print(f"      leaked: {msg}")

    # An out-of-board `from` must be rejected, not silently applied. Python
    # indexes lists from the end on a negative subscript, so this used to
    # decrement an unrelated point and hand back a quietly wrong board.
    from gvformat.reader import _apply_moves_p1

    bad_move = [{"from": 40, "pips": 3}]
    try:
        _apply_moves_p1([0] * 26, bad_move, True)
        raised = False
    except GvabError:
        raised = True
    check(raised, "5. a move from a point outside the board raises GvabError")
    print()

    # --- 6. PR survives the write ---
    #
    # The property, not any one field: a `.gvab` is what gets *stored*, so every
    # input to `compute_aggregates` has to come back out of it. It did not -- a
    # missed *re*double was filed under `cube_decision`, whose `equity_loss` is
    # zero by definition on disk (CUBE type=4), so the error was real in memory
    # and gone from the file. One BGBlitz match read PR 7.63 in the viewer and
    # 5.99 after saving.
    #
    # Asserting on the totals rather than on the fields means the next field
    # that fails to survive a write fails here too, whatever it turns out to be.
    print("--- 6. PR survives the write ---")
    stats_ogxm = {
        "match_length": 5,
        "player_white": "Alice",
        "player_black": "Bob",
        "white_score": 0,
        "black_score": 0,
        "result": 1,
        "source": 0,
        "timestamp": 0,
        "analysis_info": {"ply": 3, "eval_level": "3ply", "model_id": "test", "timestamp": 0},
        "games": [{
            "game_index": 0,
            "winner": 0,
            "points_won": 1,
            "is_crawford": False,
            "is_lastgame": True,
            "first_to_move": 0,
            "plies": [
                # A plain checker error.
                {"color": 0, "action_id": 1, "d1": 1, "d2": 2,
                 "moves": [{"from": 24, "pips": 1}],
                 "analysis": {"best_equity": 0.5, "played_equity": 0.4,
                              "equity_loss": 0.1, "decision": True,
                              "alternatives": []}},
                # A missed double: the cube error rides in `missed_double`,
                # which is the key whose `equity_loss` the format stores.
                {"color": 1, "action_id": 6, "d1": 2, "d2": 3,
                 "moves": [{"from": 13, "pips": 2}, {"from": 13, "pips": 3}],
                 "analysis": {"best_equity": 0.2, "played_equity": 0.2,
                              "equity_loss": 0.0, "decision": True,
                              "alternatives": [],
                              "missed_double": {
                                  "no_double_equity": 0.55,
                                  "double_take_equity": 0.8,
                                  "double_pass_equity": 1.0,
                                  "equity_loss": 0.25,
                                  "correct_action": "double"}}},
                # A correct no-double. Its `equity_loss` is 0 by definition --
                # CUBE type=4 has no field for anything else -- and its
                # `decision` flag does round-trip, in GVAN's cube section.
                {"color": 0, "action_id": 2, "d1": 3, "d2": 1,
                 "moves": [{"from": 13, "pips": 3}],
                 "analysis": {"best_equity": 0.3, "played_equity": 0.3,
                              "equity_loss": 0.0, "decision": True,
                              "alternatives": [],
                              "cube_decision": {
                                  "should_double": False,
                                  "no_double_equity": 0.6,
                                  "double_take_equity": 0.2,
                                  "double_pass_equity": 1.0,
                                  "action": "no_double",
                                  "equity_loss": 0.0,
                                  "decision": True}}},
            ],
        }],
    }

    before = compute_aggregates(stats_ogxm)["match"]
    after = compute_aggregates(read_gvab(write_gvab(stats_ogxm)))["match"]

    for side in ("white", "black"):
        for field in ("total_error", "total_decisions", "cube_decisions", "pr"):
            check(before[side][field] == after[side][field],
                  f"6. round-trip: {side}'s {field} is unchanged")

    # And the specific one, so a failure above says which leak came back. The
    # missed double is on the ply with `color: 1`, which is *white* (a ply's
    # colour byte is 1 = white; a game's `winner` byte is the other way round).
    check(before["white"]["total_error"] == 0.25,
          "6. the missed double's error is counted at all")
    check(after["white"]["total_error"] == 0.25, "6. and survives the write")
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
