# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The OGXM v2 analysis fields are document keys.

Phase 2 of bringing every v2 field into the document: the analysis block's own
fields (producer, completeness, coverage, the model's name and digest, the
engine build, currency, cube efficiency, tables, dials, completion time,
sources), the level records at all three tiers -- rollouts included -- and the
keys v2 adds to a checker, cube, resign and roll decision and to an alternative.

What is checked, over synthetic documents (``ogxm2_blocks_cases.py``):

1. ``read(write(D)) == D`` once D is what a reader makes of the file, rewriting
   is byte-stable, and the document survives the ``.gva`` JSON route.
2. The reference codec (``libogxm``, skipped without it) loads each file, finds
   no rule broken, replays it, re-encodes it to the same bytes -- and reads
   the fields under the names and values the document gave.
3. Each field lands where v2 puts it: the ones ``DECS`` can hold are in
   ``DECS``, the ones it cannot (a decision that breaks an invariant) are in the
   annotation that keeps a decision exactly, the values v2 cannot hold at all
   travel in the block's annotation, and all of them come back.
4. A file the *reference* wrote, using every field, reads into the expected
   document and rewrites byte for byte. Edits to it keep what they do not touch.

Run directly:
    uv run python tests/test_ogxm2_blocks.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_blocks_cases as C  # noqa: E402
import ogxm2_oracle as oracle  # noqa: E402
from gvformat import append_analysis, read_gvab, write_gvab  # noqa: E402
from gvformat.export import attach_rollout_levels  # noqa: E402
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


def sections(data: bytes) -> dict[bytes, list[bytes]]:
    out: dict = {}
    for stype, _pos, payload in R._walk_sections(data, len(data)):
        out.setdefault(stype, []).append(payload)
    return out


def annotations(data: bytes) -> list[dict]:
    return [r for p in sections(data).get(b"ANNO", []) for r in R._decode_anno(p)]


def decisions(data: bytes, block: int = 0) -> list[dict]:
    return R._decode_decs(sections(data)[b"DECS"][block])


def block_record(data: bytes, block: int = 0) -> dict:
    return R._decode_anal(sections(data)[b"ANAL"][block])


def decision_at(data: bytes, ref: int, kind: int, block: int = 0):
    return next((d for d in decisions(data, block) if d["ply_ref"] == ref and d["kind"] == kind), None)


def exact_refs(data: bytes) -> set[int]:
    """The plies whose decisions the annotation that keeps them exactly holds."""
    return {r["ref"] for r in annotations(data) if r.get("key", "").startswith(R.GV_KEY_DECISIONS)}


def analysis_at(doc: dict, gi: int, pi: int) -> dict:
    return doc["games"][gi]["plies"][pi]["analysis"]


def main() -> int:
    try:
        lib = oracle.version()
        print(f"reference: libogxm {lib}")
    except oracle.OracleUnavailable as exc:
        lib = None
        print(f"reference: unavailable ({exc}) -- reference checks skipped")

    cases = C.all_cases()
    written = {name: write_gvab(doc) for name, doc in cases.items()}
    settled = {name: read_gvab(data) for name, data in written.items()}
    refs: dict = {}

    # --- 1. every document round-trips --------------------------------------
    print("--- 1. the document comes back whole ---")
    for name, data in written.items():
        back = settled[name]
        again = write_gvab(back)
        d = first_diff(back, read_gvab(again))
        check(d is None, f"1. {name}: read(write(D)) == D" + ("" if d is None else f" -- {d}"))
        check(again == data, f"1. {name}: rewriting is byte-stable")
        check(write_gvab(json.loads(json.dumps(back))) == data, f"1. {name}: and so is the .gva route")
        if lib is not None:
            try:
                ref = oracle.binary_to_json(data)
                problems = []
                if oracle.check_json(ref) != "ok":
                    problems.append(f"breaks {oracle.check_json(ref)}")
                if not ref.get("replay_complete", True):
                    problems.append("does not replay to the end")
                if oracle.json_to_binary(ref) != data:
                    problems.append("re-encodes to other bytes")
                refs[name] = ref
            except ValueError as exc:
                problems = [f"refused: {exc}"]
            check(not problems, f"1. {name}: the reference accepts it" + (
                "" if not problems else f" -- {'; '.join(problems)}"))

    # --- 2. the block ---------------------------------------------------------
    print("\n--- 2. the block's own fields ---")
    rich = settled["rich-money"]
    info = rich["analysis_info"]
    for key, want in C.BLOCK.items():
        check(info.get(key) == want, f"2. {key} is a key of the block")
    check(info["currency"] == 0 and info["level"] == {"preset": "2ply", "checker_ply": 2, "cube_ply": 2},
          "2. so are a currency that is not the default and a level with more than the labels give")
    rec = block_record(written["rich-money"])
    check(rec["producer"] == 0 and rec["complete"] and rec["currency"] == 0
          and rec["model_name"] == "Xerxes" and rec["model_id"] == C.BLOCK["model_id"]
          and rec["model_digest"] == C.BLOCK["model_digest"] and rec["cube_efficiency"] == 0.6
          and rec["dials"] == C.DIALS and rec["sources"] == C.SOURCES
          and rec["completed_at"] == C.BLOCK["completed_at"] and rec["level"]["cube_ply"] == 2,
          "2. and each is v2's own field in the block's ANAL record")
    check(not any(r["key"].startswith(R.GV_PREFIX + "model") or r["key"] == R.GV_PREFIX + "sources"
                  for r in annotations(written["rich-money"])
                  if r["scope"] == 0 and "analysis" not in r["key"]),
          "2. nothing about the block needed an annotation of ours")
    items = next(r["value"] for r in annotations(written["rich-money"])
                 if r["key"].startswith(R.GV_KEY_ANALYSIS))
    check("engine_build" not in items and "dials" not in items and "producer" not in items,
          "2. not even in the block's own")
    if "rich-money" in refs:
        ai = refs["rich-money"]["analysis_info"]
        check(ai["producer"] == "ogx" and ai["complete"] is True and ai["currency"] == "cubeless"
              and ai["model_name"] == "Xerxes" and ai["engine_build"] == C.BLOCK["engine_build"]
              and ai["cube_efficiency"] == 0.6 and ai["tables"] == C.BLOCK["tables"]
              and ai["completed_at"] == C.BLOCK["completed_at"] and ai["sources"] == C.SOURCES
              and ai["dials"]["jacoby_mode"] == "forced_on" and ai["dials"]["cube_rule"] == "scalar_equity"
              and ai["dials"]["top_deep_threshold"] == 0.04 and ai["level"]["cube_ply"] == 2,
              "2. the reference reads them under its own names")

    part = settled["partial"]["analysis_info"]
    want_cov = [[0, i] for i in range(len(settled["partial"]["games"][0]["plies"]))] + [[1, 0], [1, 1]]
    check(part["coverage"] == want_cov and part["producer"] == 2 and "complete" not in part,
          "2. coverage lists the plies attempted (one that names no ply is gone), and not complete is no key")
    check(block_record(written["partial"])["coverage"] == list(range(len(want_cov))),
          "2. -- as the ascending ply_refs v2 states (a ply after a game's first is one more)")
    if "partial" in refs:
        check(refs["partial"]["analysis_info"]["coverage"] == list(range(len(want_cov))),
              "2. and the reference reads the same list")

    # --- 3. levels -------------------------------------------------------------
    print("\n--- 3. levels and what sits on decisions ---")
    gi, pi, _p = C.checker_plies(C.golden(C.MONEY))[0]
    ref0 = sum(len(g["plies"]) for g in cases["rich-money"]["games"][:gi]) + pi
    a = analysis_at(rich, gi, pi)
    for key in ("alternatives_total", "rollouts_done", "deep_searched", "position_tags",
                "producer_ref", "source_band"):
        check(a[key] == cases["rich-money"]["games"][gi]["plies"][pi]["analysis"][key],
              f"3. checker: {key}")
    check(a["level"]["rollout"] == C.ROLLOUT and a["level"]["preset"] == "truncated2",
          "3. checker: a level with a rollout of every field, a 64-bit seed as a decimal string")
    check(all("rollout" not in alt.get("level", {}) for alt in a["alternatives"][3:])
          and all("level" not in alt for alt in a["alternatives"][:3])
          and a["alternatives"][3]["level"] == {"preset": "1ply", "checker_ply": 1, "cube_ply": 2},
          "3. alternatives: the first three inherit the rollout, the rest state a level without one")
    check(all(alt["rollout_se"] > 0 and alt["cubeless_equity"] < alt["equity"]
              for alt in a["alternatives"][:3]) and "rollout_se" not in a["alternatives"][3],
          "3. alternatives: rollout_se and cubeless_equity")
    check(a["luck_producer_ref"] == 0 and a["luck_level"]["preset"] == "x1",
          "3. roll: producer_ref and a level of its own")
    d = decision_at(written["rich-money"], ref0, R.KIND_CHECKER)
    check(d is not None and d["alternatives_total"] == 40 and d["rollouts_done"] == 3
          and d["deep_searched"] == 6 and d["position_tags"] == 5 and d["producer_ref"] == 1
          and d["source_band"] == 1800 and d["level"]["rollout"]["seed"] == "18446744073709551615"
          and ref0 not in exact_refs(written["rich-money"]),
          "3. that decision is in DECS, every field in the record v2 gives it")
    check("level" not in d["alternatives"][0] and "level" in d["alternatives"][3],
          "3. -- and the first alternative states no level of its own, the fourth does")
    if "rich-money" in refs:
        ra = refs["rich-money"]["games"][gi]["plies"][pi]["analysis"]
        check(ra["alternatives_total"] == 40 and ra["rollouts_done"] == 3 and ra["deep_searched"] == 6
              and ra["position_tags"] == 5 and ra["producer_ref"] == 1 and ra["source_band"] == 1800
              and ra["level"]["rollout"]["variance_reduction"] == "extended"
              and ra["level"]["rollout"]["match_policy"] == "match_aware"
              and ra["alternatives"][0]["rollout_se"] == 0.0123
              and ra["alternatives"][4]["level"].get("rollout") is None,
              "3. the reference reads the decision under its names, the tail with no rollout")

    exact = exact_refs(written["rich-money"])
    cps = C.checker_plies(C.golden(C.MONEY))
    first_error = C.checker_plies(C.golden(C.MONEY), played_not_first=True)[0]

    def ref_of(g: int, p: int) -> int:
        return sum(len(x["plies"]) for x in cases["rich-money"]["games"][:g]) + p

    b = analysis_at(rich, first_error[0], first_error[1])
    check(ref_of(first_error[0], first_error[1]) in exact
          and b["alternatives"][0]["level"]["rollout"] == C.SMALL_ROLLOUT,
          "3. a rolled-out best move the played one is not (A5): kept exactly, in the annotation")
    check(all(ref_of(g, p) in exact for g, p, _x in cps[1:4]) and ref_of(cps[4][0], cps[4][1]) not in exact,
          "3. so are counts DECS has no place for: a total below the list, counts of a list that was not cut, "
          "a source that does not exist (and a tag of every bit is no reason)")
    t = analysis_at(rich, cps[1][0], cps[1][1])
    check(t["alternatives_total"] == 2 and t["rollouts_done"] == 4, "3. -- and they come back as stated")
    check(analysis_at(rich, cps[2][0], cps[2][1])["deep_searched"] == 2
          and analysis_at(rich, cps[3][0], cps[3][1])["producer_ref"] == 7
          and analysis_at(rich, cps[4][0], cps[4][1])["position_tags"] == 0xFFFFFFFF,
          "3. -- all of them")

    sub = None
    for gi_, pi_, p in C.plies(rich):
        a_ = p.get("analysis") or {}
        if isinstance(a_.get("cube_decision"), dict) and a_["cube_decision"].get("is_free_cube"):
            sub, sub_at = a_["cube_decision"], (gi_, pi_)
            break
    check(sub is not None and sub["take_point"] == 0.3125 and sub["window_searched"] is True
          and sub["is_optional"] is True and sub["cubeful_take_value"] == 0.4321
          and sub["producer_ref"] == 0 and sub["level"]["rollout"] == C.SMALL_ROLLOUT,
          "3. cube: take_point, the flags, cubeful_take_value, producer_ref and a rollout level")
    cube_rec = decision_at(written["rich-money"], ref_of(*sub_at), R.KIND_CUBE)
    check(cube_rec is not None and cube_rec["take_point"] == 0.3125 and cube_rec["window_searched"]
          and cube_rec["is_optional"] and cube_rec["is_free_cube"]
          and cube_rec["cubeful_take_value"] == 0.4321 and cube_rec["producer_ref"] == 0,
          "3. cube: in the DECS record")
    missed = next(p["analysis"]["missed_double"] for _g, _p, p in C.plies(rich)
                  if (p.get("analysis") or {}).get("missed_double", {}).get("currency") == 1)
    check(missed["currency"] == 1 and missed["level"]["cube_ply"] == 4,
          "3. cube: a currency of its own (cubeful money, in a block of cubeless equities) and a depth")
    cp = next(p["analysis"] for _g, _p, p in C.plies(rich)
              if p["action_id"] == R.ACTION_DOUBLE and p["analysis"].get("is_optional"))
    check(cp["take_point"] == 0.2917 and cp["producer_ref"] == 1
          and cp["level"]["cube_ply"] == 3 and cp["level"]["rollout"]["move_ply"] == 3,
          "3. cube: the same on the ply of a double itself")

    res = settled["resigned"]
    ra = next(p["analysis"] for _g, _p, p in C.plies(res) if p.get("analysis"))
    check(ra["correct_value"] == 1 and ra["producer_ref"] == 0
          and ra["level"]["rollout"] == C.SMALL_ROLLOUT and ra["level"]["preset"] == "rollout",
          "3. resign: correct_value, producer_ref and a level")
    if "resigned" in refs:
        rr = next(p["analysis"] for g in refs["resigned"]["games"] for p in g["plies"] if p.get("analysis"))
        check(rr["correct_value"] == "single" and rr["producer_ref"] == 0
              and rr["level"]["rollout"]["trials"] == 72, "3. the reference reads them too")

    # --- 4. currency ------------------------------------------------------------
    print("\n--- 4. currencies ---")
    mc = settled["match-currencies"]
    subs = [p["analysis"]["cube_decision"] for _g, _p, p in C.plies(mc)
            if isinstance((p.get("analysis") or {}).get("cube_decision"), dict)]
    check("currency" not in mc["analysis_info"] and mc["analysis_info"]["cube_efficiency"] == 0.7,
          "4. a match block is in match winning chances unless it says otherwise: no key")
    check(subs[0]["currency"] == 1 and subs[0]["take_point"] == 0.28 and "currency" not in subs[1],
          "4. a cube decision keeps a currency of its own; one equal to the block's is not written")
    src = C.golden(C.MATCH)
    src_subs = [p["analysis"]["cube_decision"] for _g, _p, p in C.plies(src)
                if isinstance((p.get("analysis") or {}).get("cube_decision"), dict)]
    check(all(abs(subs[0][k] - src_subs[0][k]) < 1e-9 for k in
              ("no_double_equity", "double_take_equity", "double_pass_equity")),
          "4. its equities are the document's own numbers, not converted twice")
    if "match-currencies" in refs:
        rj = refs["match-currencies"]["games"]
        rsub = [p["analysis"]["cube_decision"] for g in rj for p in g["plies"]
                if isinstance((p.get("analysis") or {}).get("cube_decision"), dict)]
        check(rsub[0]["currency"] == "cubeful" and rsub[0]["no_double_equity"] == src_subs[0]["no_double_equity"]
              and "currency" not in rsub[1] and refs["match-currencies"]["analysis_info"]["currency"] == "cubeful_match",
              "4. the file holds that one as cubeful money and the rest as match winning chances")
    mb = settled["money-block-in-a-match"]
    check(mb["analysis_info"]["currency"] == 1, "4. a block in cubeful money in a match keeps it")
    check(first_diff(
        {k: v for k, v in mb["games"][0]["plies"][2].items() if k != "analysis"},
        {k: v for k, v in src["games"][0]["plies"][2].items() if k != "analysis"}) is None
        and mb["games"][0]["plies"][2]["analysis"]["best_equity"]
        == src["games"][0]["plies"][2]["analysis"]["best_equity"],
        "4. -- and its numbers are held as they are")
    mm = settled["money-in-a-match-currency"]
    check("currency" not in mm["analysis_info"] and block_record(written["money-in-a-match-currency"])["currency"] == 1,
          "4. a money game's block cannot be match winning chances: it is written, and reads back, as cubeful money")

    # --- 5. what v2 cannot hold -------------------------------------------------
    print("\n--- 5. values v2 cannot hold travel in the block's annotation ---")
    un = settled["unholdable"]["analysis_info"]
    raw = cases["unholdable"]["analysis_info"]
    check(all(un[k] == raw[k] for k in ("producer", "model_name", "model_digest", "engine_build", "tables",
                                        "completed_at", "sources", "model_id")),
          "5. each comes back whole")
    check(un["dials"] == raw["dials"], "5. the dials too, field by field")
    rec = block_record(written["unholdable"])
    check(not any(k in rec for k in ("producer", "model_name", "model_digest", "engine_build",
                                     "tables", "completed_at", "sources", "dials")),
          "5. and none of them is in a field of v2's")
    items = "".join(v for _i, v in sorted(
        (int(r["key"].partition("~")[2] or 0), r["value"][len(R.GV_FORMAT):] if "~" not in r["key"] else r["value"])
        for r in annotations(written["unholdable"]) if r["key"].startswith(R.GV_KEY_ANALYSIS)))
    check(all(f"{k}=" in items for k in ("producer", "model_name", "model_digest", "engine_build",
                                         "tables", "completed_at", "sources", "dials.jacoby_mode",
                                         "dials.top_deep_keep", "dials.top_deep_threshold")),
          "5. each as an item of the annotation that marks the block as ours")
    check("annotations" not in settled["unholdable"], "5. the reader consumes them: no annotations key")

    two = settled["two-blocks"]
    check(len(two["analyses_info"]) == 2 and two["analyses_info"][1]["engine_build"] == "b2"
          and two["analyses_info"][1]["producer"] == 4 and two["analyses_info"][1]["complete"] is True
          and two["analyses_info"][0]["sources"] == C.SOURCES,
          "5. two blocks each keep their own fields")

    # --- 6. a file from the reference ------------------------------------------
    print("\n--- 6. a file the reference wrote, with every field ---")
    foreign_bytes = (FIXTURES / "blocks.ogxm").read_bytes()
    if lib is not None:
        check(oracle.json_to_binary(C.foreign_json(oracle)) == foreign_bytes,
              "6. blocks.ogxm is what the reference writes for the case's JSON")
        fj = oracle.binary_to_json(foreign_bytes)
        check(oracle.check_json(fj) == "ok" and fj["replay_complete"],
              "6. it is valid v2 and replays")
    doc = read_gvab(foreign_bytes)
    held = doc.pop(P.KEY, None)
    expected = json.loads((FIXTURES / "blocks.expected.json").read_text())
    d = first_diff(expected, doc)
    check(d is None, "6. read_gvab gives the expected document" + ("" if d is None else f" -- {d}"))
    fi = doc["analysis_info"]
    check(fi["producer"] == 0 and fi["complete"] and fi["model_name"] == "Xerxes"
          and fi["model_id"] == C.BLOCK["model_id"]
          and fi["currency"] == 0 and fi["sources"] == C.SOURCES and fi["dials"] == C.DIALS
          and fi["completed_at"] == C.BLOCK["completed_at"] and len(fi["coverage"]) == 30,
          "6. the block: every field, the name and the identifier as stated")
    d0 = analysis_at(doc, 0, 0)
    check(d0["level"]["preset"] == "++" and d0["level"]["rollout"] == C.ROLLOUT
          and d0["alternatives"][5]["level"]["preset"] == "1ply"
          and d0["alternatives_total"] == 40 and d0["luck_producer_ref"] == 0,
          "6. the decisions: levels, counts, sources")
    check(held is not None and not held.get("anno") and not held.get("unknown"),
          "6. nothing is kept beside the document but the bytes of what it still says")
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

    base = read_gvab(foreign_bytes)
    base.pop(P.KEY)

    out, back = edited(lambda d: d.update(player_white="Alicia"))
    back.pop(P.KEY, None)
    check(first_diff({**base, "player_white": "Alicia"}, back) is None,
          "6. a rename keeps every field of the block and its decisions")
    check(sections(out)[b"ANAL"] == sections(foreign_bytes)[b"ANAL"],
          "6. -- and the block is the source's own bytes")
    valid("6. a rename", out)

    def edit_block(d: dict) -> None:
        d["analysis_info"]["engine_build"] = "next-build"
    out, back = edited(edit_block)
    back.pop(P.KEY, None)
    want = copy.deepcopy(base)
    want["analysis_info"]["engine_build"] = "next-build"
    # The edited block is ours now: its labels are what it states, which for a
    # preset the producer named is that name (profile section 4).
    rec = block_record(out)
    check(rec["engine_build"] == "next-build" and rec["model_name"] == "Xerxes"
          and rec["dials"] == C.DIALS and rec["sources"] == C.SOURCES and rec["producer"] == 0
          and rec["complete"] and rec["currency"] == 0 and rec["coverage"] == list(range(30)),
          "6. editing one field of the block re-encodes it with every other field")
    check(rec["started_at"] % 1000 == 123 and rec["started_at"] == block_record(foreign_bytes)["started_at"],
          "6. -- and the milliseconds the source stated for its start (P4)")
    check(decision_at(out, 0, R.KIND_CHECKER)["level"]["rollout"] == C.ROLLOUT
          and decision_at(out, 0, R.KIND_CHECKER)["rollouts_done"] == 3,
          "6. -- and its decisions, rollouts included")
    # What labels an edited block carries is the one thing that moves: the
    # block is ours once rewritten, so its labels are its presets (profile
    # section 4), and a level the labels now give is no longer kept.
    check(first_diff({k: v for k, v in want["analysis_info"].items() if k not in ("eval_level", "level")},
                     {k: v for k, v in back["analysis_info"].items() if k not in ("eval_level", "level")}) is None
          and back["analysis_info"]["engine_build"] == "next-build",
          "6. the edited block reads back with the edit and nothing else changed")
    check(analysis_at(doc, 0, 0)["alternatives"][0].get("eval_level") == "rollout"
          and analysis_at(back, 0, 0)["alternatives"][0].get("eval_level") == "++",
          "6. rule foreign-labels: rewritten by us, the block's labels are its presets, where depth had labelled them")
    valid("6. a block edit", out)

    def edit_decision(d: dict) -> None:
        a = analysis_at(d, 0, 0)
        a["alternatives"][0]["rollout_se"] = 0.05
        a["alternatives"][1]["cubeless_equity"] = 0.1234
    out, back = edited(edit_decision)
    check(analysis_at(back, 0, 0)["alternatives"][0]["rollout_se"] == 0.05
          and analysis_at(back, 0, 0)["alternatives"][1]["cubeless_equity"] == 0.1234
          and analysis_at(back, 0, 0)["rollouts_done"] == 3,
          "6. a decision edit keeps its other fields")
    valid("6. a decision edit", out)

    def drop_sources(d: dict) -> None:
        del d["analysis_info"]["sources"]
    out, back = edited(drop_sources)
    check(analysis_at(back, 0, 0)["producer_ref"] == 1 and "sources" not in back["analysis_info"]
          and analysis_at(back, 1, 6)["producer_ref"] == 1,
          "6. a block that loses its sources keeps the producer_refs, in the annotation that holds them exactly")
    valid("6. no sources", out)

    def move_edit(d: dict) -> None:
        d["games"][0]["plies"].insert(0, {"color": 1, "action_id": R.ACTION_PASS})
    out, back = edited(move_edit)
    check(analysis_at(back, 0, 1)["alternatives_total"] == 40
          and analysis_at(back, 0, 1)["level"]["rollout"] == C.ROLLOUT
          and [0, 0] in back["analysis_info"]["coverage"] and [0, 22] in back["analysis_info"]["coverage"],
          "6. an inserted ply moves the decisions with their plies; coverage is in the document's terms, "
          "and names every ply a decision is on")
    valid("6. a move edit", out)

    # --- 7. a beaver answers a double ----------------------------------------
    print("\n--- 7. a beaver answers a double ---")

    def game(rows):
        return {"game_index": 0, "plies": [{"color": 0, "action_id": a, **({"analysis": an} if an else {})}
                                          for a, an in rows]}
    base = {"player_white": "A", "games": [game([(0, None), (21, None), (32, None), (33, None),
                                                 (1, None), (24, None)])]}
    our = {"player_white": "A", "analysis_info": {"ply": 2},
           "games": [game([(0, {"n": 0}), (21, {"n": 1}), (22, {"n": 2}), (1, {"n": 3})])]}
    merged = append_analysis(base, our)
    check("".join(str(p["analysis"]["n"]) if p.get("analysis") else "-"
                  for p in merged["games"][0]["plies"]) == "012-3-",
          "7. a take is paired with the beaver that answers the double, and the raccoon has no analysis")

    # --- 8. the rollouts a producer states ----------------------------------------
    print("\n--- 8. levels stamped from labels (attach_rollout_levels) ---")
    table = {"truncated2": {"trials": 360, "truncation_depth": 7, "move_ply": 2, "seed": "42"},
             "truncated3": {"trials": 360, "truncation_depth": 7, "move_ply": 3, "seed": "42"}}
    d = C.golden(C.MONEY)
    n_alt = 0
    for _g, _p, p in C.plies(d):
        a = p.get("analysis") or {}
        for alt in (a.get("alternatives") or [])[:3]:
            alt["eval_level"] = "truncated2"
            n_alt += 1
        if "alternatives" not in a and a.get("eval_level"):
            a["eval_level"] = "truncated3"
        for sub in (a.get("cube_decision"), a.get("missed_double")):
            if isinstance(sub, dict):
                sub["eval_level"] = "truncated2"
    attach_rollout_levels(d, table)

    def levels(doc: dict):
        out = []
        for gi_, pi_, p in C.plies(doc):
            a = p.get("analysis") or {}
            out += [(gi_, pi_, "alt", i, alt.get("level")) for i, alt in enumerate(a.get("alternatives") or [])
                    if alt.get("level")]
            out += [(gi_, pi_, k, a[k].get("level")) for k in ("cube_decision", "missed_double")
                    if isinstance(a.get(k), dict) and a[k].get("level")]
            if a.get("level"):
                out.append((gi_, pi_, "self", a["level"]))
        return out
    stamped, back = levels(d), levels(read_gvab(write_gvab(d)))
    check(len(stamped) > n_alt and back == stamped,
          f"8. every level stamped comes back exactly as stated, and none is added ({len(stamped)})")
    check(any(k[2] == "self" for k in stamped) and any(k[2] == "cube_decision" for k in stamped)
          and any(k[2] == "alt" for k in stamped),
          "8. -- on alternatives, on cube records of a dice ply, and on the ply of a cube action")
    if lib is not None:
        blob = write_gvab(d)
        j = oracle.binary_to_json(blob)
        check(oracle.check_json(j) == "ok" and oracle.json_to_binary(j) == blob and j["replay_complete"],
              "8. the reference accepts it")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
