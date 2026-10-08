# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The OGXM v2 writer: lossless against v1, and conforming by the reference.

``write_gvab`` writes OGXM v2 since 1.6.0. Three things are checked, over every
match in the sample corpus in every form it ships (``.gvab``, ``.xg``, ``.bgf``,
``.mat``) and the goldens, plus synthetic shapes the corpus lacks (two analysis
blocks, analysed resignations):

1. **Nothing is lost.** ``read(write_v2(D))`` equals ``read(write_v1(D))`` -- the
   document v1 kept, which every consumer was built against -- except where a
   rule in ``_RULES`` says how and why the two legitimately differ. Each rule is
   one sentence in ``docs/OGXM_V2_PROFILE.md``; a difference no rule explains
   fails.
2. **Rewriting is stable.** ``write(read(b)) == b``, byte for byte.
3. **The reference accepts it** (needs ``libogxm``; skipped without it): the
   file loads, passes every v2 rule, replays to the end, and the reference
   writer re-encodes it to the same bytes -- which is to say each value has the
   one encoding v2 allows.

Run directly:
    uv run python tests/test_ogxm2_writer.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_oracle as oracle  # noqa: E402
from gvformat import (  # noqa: E402
    append_analysis, convert_bgf, convert_mat, convert_xg, read_gvab, write_gvab, write_gvab_v1,
)
from gvformat import ogxm2 as R  # noqa: E402
from gvformat.bgf import decode_smile, read_bgf  # noqa: E402

_SAMPLES = _REPO_ROOT / "samples"
_GOLDEN = _REPO_ROOT / "tests" / "golden"
_PROBS = ("win", "gammon_win", "bg_win", "gammon_loss", "bg_loss")

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------

def _corpus() -> list[tuple[str, dict]]:
    out = []
    for f in sorted(_GOLDEN.glob("*.fast.gvab")):
        out.append((f"golden/{f.name}", read_gvab(f.read_bytes())))
    for f in sorted((_SAMPLES / "gv").glob("*.gvab")):
        out.append((f"gv/{f.name}", read_gvab(f.read_bytes())))
    for f in sorted((_SAMPLES / "xg").glob("*.xg")):
        out.append((f"xg/{f.name}", convert_xg(f)))
    for f in sorted((_SAMPLES / "bgf").glob("*.bgf")):
        out.append((f"bgf/{f.name}", convert_bgf(f)))
    for f in sorted((_SAMPLES / "mat").glob("*.mat")):
        out.append((f"mat/{f.name}", convert_mat(f)))
    return out


def _two_blocks() -> dict | None:
    xg = _SAMPLES / "xg" / "3WNK_g1Z-PLsh_HyvQ5j4a.xg"
    golden = _GOLDEN / "3WNK_g1Z-PLsh_HyvQ5j4a.fast.gvab"
    if not (xg.exists() and golden.exists()):
        return None
    return append_analysis(convert_xg(xg), read_gvab(golden.read_bytes()))


def _resign_analysis() -> dict | None:
    f = _SAMPLES / "xg" / "rK7pXm4TqLb9NzWd.xg"
    if not f.exists():
        return None
    d = convert_xg(f)
    for g in d["games"]:
        for p in g["plies"]:
            if p["action_id"] in (R.ACTION_RESIGN_GAME, R.ACTION_RESIGN_MATCH):
                p["analysis"] = {
                    "resign_error": 0.1234, "take_resign_error": -0.05, "equity_loss": 0.1234,
                    "decision": True,
                    "eval": {"win": 0.1, "gammon_win": 0.01, "bg_win": 0.0, "gammon_loss": 0.3,
                             "bg_loss": 0.02, "equity": -0.21},
                }
    return d


# ---------------------------------------------------------------------------
# What v1 kept, against what v2 keeps
# ---------------------------------------------------------------------------

def _zero(ev) -> bool:
    return isinstance(ev, dict) and not any(ev.get(k) for k in _PROBS)


def _rule(path: list, key: str, v1, v2, ctx: dict) -> str | None:
    """The rule that explains ``v1 -> v2`` at ``path + [key]``, or None."""
    if key == "analysis_id" and v1 is None:
        return "analysis-id"                       # a block's identifier, new in v2
    if key == "eval" and v1 is None and _zero(v2):
        return "zero-probs"                        # v1 read all-zero as "not recorded"
    if key == "eval" and v2 is None and _zero(v1):
        return "zero-probs"
    if key in ("equity_loss", "played_equity") and isinstance(v1, float) and isinstance(v2, float):
        if abs(v1 - v2) <= 1.0001e-4 and ctx.get("played_listed"):
            return "derived-loss"                  # best - played, as v2 derives it
    if key.endswith("_equity") and isinstance(v1, float) and isinstance(v2, float):
        if abs(v1) == 3.0 and abs(v2) > 3.0 and (v1 > 0) == (v2 > 0):
            return "unclamped"                     # v1 stored equity at most +-3
    if key == "eval_level" and v1 is None and v2 == ctx.get("block_level"):
        return "level-filled"                      # no level means the block's
    if key == "moves" and isinstance(v1, list) and isinstance(v2, list):
        if sorted(map(tuple, (m.items() for m in v1))) == sorted(map(tuple, (m.items() for m in v2))):
            return "step-order"                    # stored in an order that replays (M3)
    if key == "cube_limit" and v2 == 0 and (v1 & (v1 - 1)) != 0:
        return "no-limit"                          # a source's "no limit", not a power of two
    if key == "first_to_move" and ctx.get("no_play"):
        return "no-first-move"                     # a game with no play to take it from
    if key == "luck_eval_level" and v2 is None and not ctx.get("has_luck"):
        return "luck-level"                        # v1's default, on a block with no luck
    if key == "met_id" and v1 is None and isinstance(v2, str):
        return "met-id"                            # the table a block's equities come from, v2's ANAL
    if key == "mwc_frame" and v1 is None and isinstance(v2, list):
        return "mwc-frame"                         # a source's own MWC frame, in the block annotation
    return None


def _compare(v1, v2, path: list, ctx: dict, unexplained: list, used: dict) -> None:
    if isinstance(v1, dict) and isinstance(v2, dict):
        if "alternatives" in v1 or "alternatives" in v2:
            alts = v2.get("alternatives") or []
            ctx = {**ctx, "played_listed": any(a.get("is_played") for a in alts)}
        for key in sorted(set(v1) | set(v2)):
            a, b = v1.get(key), v2.get(key)
            if a == b:
                continue
            rule = _rule(path, key, a, b, ctx)
            if rule is not None:
                used[rule] = used.get(rule, 0) + 1
            elif isinstance(a, (dict, list)) and isinstance(b, (dict, list)) and type(a) is type(b):
                _compare(a, b, path + [key], ctx, unexplained, used)
            else:
                unexplained.append(f"{'.'.join(map(str, path + [key]))}: {a!r} -> {b!r}")
    elif isinstance(v1, list) and isinstance(v2, list) and len(v1) == len(v2):
        for i, (a, b) in enumerate(zip(v1, v2)):
            if a != b:
                if isinstance(a, dict) and isinstance(b, dict):
                    _compare(a, b, path + [i], ctx, unexplained, used)
                else:
                    unexplained.append(f"{'.'.join(map(str, path + [i]))}: {a!r} -> {b!r}")
    else:
        unexplained.append(f"{'.'.join(map(str, path))}: {v1!r} -> {v2!r}")


def _context_walk(v1: dict, v2: dict, unexplained: list, used: dict) -> None:
    """``_compare`` with the per-block and per-game facts the rules need."""
    infos = v1.get("analyses_info") or ([v1["analysis_info"]] if "analysis_info" in v1 else [])
    has_luck = [any("luck" in (p.get("analysis") if len(infos) == 1 else next(
        (a for a in p.get("analyses") or [] if a.get("analysis_index") == k), None) or {})
        for g in v1["games"] for p in g["plies"]) for k in range(len(infos))]
    top1 = {k: v for k, v in v1.items() if k not in ("games", "analysis_info", "analyses_info")}
    top2 = {k: v for k, v in v2.items() if k not in ("games", "analysis_info", "analyses_info")}
    _compare(top1, top2, [], {}, unexplained, used)
    for key in ("analysis_info", "analyses_info"):
        a, b = v1.get(key), v2.get(key)
        if isinstance(a, dict):
            a, b = [a], [b] if isinstance(b, dict) else b
        for k, (ia, ib) in enumerate(zip(a or [], b or [])):
            _compare(ia, ib, [key, k], {"has_luck": has_luck[k] if k < len(has_luck) else False},
                     unexplained, used)
    for gi, (g1, g2) in enumerate(zip(v1["games"], v2["games"])):
        no_play = not any(p.get("action_id", 30) <= 20 for p in g1["plies"])
        gctx = {"no_play": no_play}
        _compare({k: v for k, v in g1.items() if k != "plies"},
                 {k: v for k, v in g2.items() if k != "plies"}, ["games", gi], gctx, unexplained, used)
        if len(g1["plies"]) != len(g2["plies"]):
            unexplained.append(f"games.{gi}.plies: {len(g1['plies'])} -> {len(g2['plies'])} plies")
            continue
        for pi, (p1, p2) in enumerate(zip(g1["plies"], g2["plies"])):
            if p1 == p2:
                continue
            for k, (ia, ib) in enumerate(_ply_blocks(p1, p2)):
                level = (infos[k] if k < len(infos) else {}).get("eval_level")
                _compare(ia, ib, ["games", gi, "plies", pi, k], {"block_level": level},
                         unexplained, used)
            rest1 = {k: v for k, v in p1.items() if k not in ("analysis", "analyses")}
            rest2 = {k: v for k, v in p2.items() if k not in ("analysis", "analyses")}
            _compare(rest1, rest2, ["games", gi, "plies", pi], {}, unexplained, used)


def _ply_blocks(p1: dict, p2: dict):
    if "analyses" in p1 or "analyses" in p2:
        return list(zip(p1.get("analyses") or [], p2.get("analyses") or []))
    return [(p1.get("analysis") or {}, p2.get("analysis") or {})]


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# BGBlitz's own frame
# ---------------------------------------------------------------------------

def _bgf_frame_checks() -> None:
    """The v2 file of a BGBlitz match holds BGBlitz's own MWCs, not ours."""
    print("\n--- 5. BGBlitz's own match equity frame ---")
    total = covered = 0
    missed: list = []
    worst = 0.0          # largest deviation, in units of the tolerance
    worst_abs = 0.0
    compared = 0
    luck_compared = 0
    for f in sorted((_SAMPLES / "bgf").glob("*.bgf")):
        doc = convert_bgf(f)
        if not doc["match_length"]:
            continue
        data = write_gvab(doc)
        back = read_gvab(data)

        # Coverage: every analysed ply has a frame.
        for gi, g in enumerate(doc["games"]):
            for pi, p in enumerate(g["plies"]):
                if isinstance(p.get("analysis"), dict):
                    total += 1
                    if "mwc_frame" in p["analysis"]:
                        covered += 1
                    else:
                        missed.append((f.name, gi, pi, p["action_id"]))
        check(doc["analysis_info"].get("met_id") == "bgblitz"
              and back["analysis_info"].get("met_id") == "bgblitz",
              f"5. {f.name}: the block names BGBlitz's table")

        # Read-back: the document's own normalized equities, and its frames.
        same = True
        for g1, g2 in zip(doc["games"], back["games"]):
            for p1, p2 in zip(g1["plies"], g2["plies"]):
                a1, a2 = p1.get("analysis") or {}, p2.get("analysis") or {}
                if a1.get("mwc_frame") != a2.get("mwc_frame") or a1.get("luck") != a2.get("luck"):
                    same = False
                if [x["equity"] for x in a1.get("alternatives") or []] != [
                        x["equity"] for x in a2.get("alternatives") or []]:
                    same = False
                for key in ("cube_decision", "missed_double"):
                    c1, c2 = a1.get(key), a2.get(key)
                    if isinstance(c1, dict) and any(
                            c1.get(k) != (c2 or {}).get(k) for k in (
                                "no_double_equity", "double_take_equity", "double_pass_equity")):
                        same = False
                for k in ("no_double_equity", "double_take_equity", "double_pass_equity"):
                    if k in a1 and a1[k] != a2.get(k):
                        same = False
        check(same, f"5. {f.name}: read back, equities, luck and frames equal the document's")

        # The stored MWC of every checker alternative against BGBlitz's cubeful MWC.
        decs = []
        for stype, _pos, payload in R._walk_sections(data, len(data)):
            if stype == b"DECS":
                decs = R._decode_decs(payload)
        by_ref = {d["ply_ref"]: d for d in decs if d["kind"] == R.KIND_CHECKER}
        luck_by_ref = {d["ply_ref"]: d["luck"] for d in decs if d["kind"] == R.KIND_ROLL}
        header, smile = read_bgf(f)
        raw = decode_smile(smile)
        refs = [(gi, pi, p) for gi, g in enumerate(doc["games"]) for pi, p in enumerate(g["plies"])]
        ref_of = {(gi, pi): i for i, (gi, pi, _p) in enumerate(refs)}
        bad = 0
        for gi, (g, rg) in enumerate(zip(doc["games"], raw["games"])):
            checkers = [pi for pi, p in enumerate(g["plies"]) if p["action_id"] <= 20 or p["action_id"] == 31]
            records = [m for m in rg["moves"] if m.get("from", [-1])[0] != -1]
            if len(checkers) != len(records):
                bad += 1
                continue
            for pi, m in zip(checkers, records):
                a = g["plies"][pi].get("analysis") or {}
                half = a["mwc_frame"][1] if "mwc_frame" in a else None
                ref = ref_of[(gi, pi)]
                if half is not None and ref in luck_by_ref and (m.get("luck") or {}).get("luckPlain") is not None:
                    dev = abs(luck_by_ref[ref] - float(m["luck"]["luckPlain"]))
                    luck_compared += 1
                    if dev > 0.5e-4 * half + 2e-6:
                        bad += 1
                d = by_ref.get(ref)
                mas = m.get("moveAnalysis") or []
                if d is None or half is None or len(d.get("alternatives") or []) != len(mas):
                    continue
                for alt, ma in zip(d["alternatives"], mas):
                    eq = ma.get("eq") or {}
                    if not eq.get("hasEMG", True):
                        continue
                    cd = eq.get("cubeDecision")
                    want = float(cd["eqCubeFul"] if cd else eq["matchEquity"])
                    dev = abs(alt["equity"] - want)
                    tol = 0.5e-4 * half + 2e-6
                    compared += 1
                    worst = max(worst, dev / tol)
                    worst_abs = max(worst_abs, dev)
                    if dev > tol:
                        bad += 1
        check(bad == 0, f"5. {f.name}: stored MWCs are BGBlitz's own ({bad} off)")

    pct = 100.0 * covered / total if total else 0.0
    print(f"coverage: {covered}/{total} analysed BGBlitz plies carry a frame ({pct:.1f}%)")
    if missed:
        print(f"  without: {missed[:20]}")
    print(f"alternatives compared: {compared}; luck: {luck_compared}; max deviation "
          f"{worst_abs:.2e} MWC ({worst:.2f} of the tolerance)")
    check(total > 0 and covered == total, "5. every analysed BGBlitz match-play ply carries a frame")
    check(compared > 500, f"5. checker alternatives were compared ({compared})")

    # Points awarded in the last game of a session.
    for name, white_wins, points in (("B4_SrGcsKAQmoTyHlgJCbM", False, 6), ("Sz-2PgKnvb6aFls69-qFXg", True, 1)):
        f = _SAMPLES / "bgf" / f"{name}.bgf"
        if f.exists():
            last = convert_bgf(f)["games"][-1]
            check(last["points_won"] == points and last["winner"] == (0 if white_wins else 1),
                  f"5. {name}: the last game is worth {points} (got {last['points_won']})")


def main() -> int:
    try:
        lib = oracle.version()
        print(f"reference: libogxm {lib}")
    except oracle.OracleUnavailable as exc:
        lib = None
        print(f"reference: unavailable ({exc}) -- section 3 skipped")

    cases = _corpus()
    for label, make in (("synthetic/two-blocks", _two_blocks), ("synthetic/resign", _resign_analysis)):
        doc = make()
        if doc is not None:
            cases.append((label, doc))
    check(len(cases) >= 50, f"the corpus is here ({len(cases)} documents)")

    rules_used: dict = {}
    for name, doc in cases:
        v1 = read_gvab(write_gvab_v1(doc))
        data = write_gvab(doc)
        v2 = read_gvab(data)
        unexplained: list = []
        _context_walk(v1, v2, unexplained, rules_used)
        check(not unexplained, f"1. {name}: nothing lost" + (
            "" if not unexplained else f" -- {len(unexplained)} unexplained, e.g. {unexplained[:3]}"))
        check(write_gvab(v2) == data, f"2. {name}: rewriting is byte-stable")
        if lib is not None:
            try:
                ref = oracle.binary_to_json(data)
                problems = []
                rule = oracle.check_json(ref)
                if rule != "ok":
                    problems.append(f"breaks {rule}")
                if not ref.get("replay_complete", True):
                    problems.append("does not replay to the end")
                if oracle.json_to_binary(ref) != data:
                    problems.append("re-encodes to other bytes")
            except ValueError as exc:
                problems = [f"refused: {exc}"]
            check(not problems, f"3. {name}: the reference accepts it" + (
                "" if not problems else f" -- {'; '.join(problems)}"))

    print(f"\nnormalizations applied: {dict(sorted(rules_used.items()))}")

    # --- 4. the cases the annotations exist for ---------------------------
    print("\n--- 4. what only the annotations carry ---")
    golden = _GOLDEN / "5nqfGw9bWG3deTaU.fast.gvab"
    if golden.exists():
        doc = read_gvab(golden.read_bytes())
        data = write_gvab(doc)
        v2 = read_gvab(data)
        p1, p2 = doc["games"][0]["plies"][19], v2["games"][0]["plies"][19]
        check(p1.get("analysis", {}).get("illegal_move") and p2 == p1,
              "4. an illegal play comes back whole: its steps, its analysis, its flag")
        if lib is not None:
            ref = oracle.binary_to_json(data)["games"][0]["plies"][19]
            check(ref.get("action_id") == R.ACTION_SET_POSITION and ref.get("illegal")
                  and "analysis" not in ref,
                  "4. to the reference it is the board the play produced, flagged illegal")
            keys = {a.get("key", "").split("/")[0] for a in oracle.binary_to_json(data).get("annotations", [])}
            check({"x-gammonview-illegal-ply", "x-gammonview-decisions", "x-gammonview-analysis"} <= keys,
                  f"4. and the rest travels as x- annotations ({sorted(keys)})")

    xg = _SAMPLES / "xg" / "MYdvw1qGuyaRUH__.xg"
    if xg.exists():
        doc = convert_xg(xg)
        v1 = read_gvab(write_gvab_v1(doc))
        v2 = read_gvab(write_gvab(doc))
        interleaved = [
            (p1["analysis"]["alternatives"], p2["analysis"]["alternatives"])
            for g1, g2 in zip(v1["games"], v2["games"]) for p1, p2 in zip(g1["plies"], g2["plies"])
            if "alternatives" in (p1.get("analysis") or {})
            and len({a.get("eval_level") for a in p1["analysis"]["alternatives"]}) > 1
            and [a.get("eval_level") for a in p1["analysis"]["alternatives"]].count("database")
            not in (0, len(p1["analysis"]["alternatives"]))]
        check(bool(interleaved) and all(a == b for a, b in interleaved),
              f"4. XG's interleaved database/ply lists keep their order ({len(interleaved)} decisions)")

    # A decision flag that the derivation would not give survives.
    bgf = _SAMPLES / "bgf" / "Sz-2PgKnvb6aFls69-qFXg.bgf"
    if bgf.exists():
        doc = convert_bgf(bgf)
        v1 = read_gvab(write_gvab_v1(doc))
        v2 = read_gvab(write_gvab(doc))
        flags = lambda d: [(p.get("analysis") or {}).get("decision")  # noqa: E731
                           for g in d["games"] for p in g["plies"]]
        check(flags(v1) == flags(v2), "4. BGBlitz's own PR counting survives, flag for flag")

    # A score a source states that its games do not add up to.
    stated = copy.deepcopy(cases[0][1])
    stated["white_score"] = max(0, stated["white_score"] - 1)
    check(read_gvab(write_gvab(stated))["white_score"] == stated["white_score"],
          "4. a stated score the games do not add up to is kept")

    _bgf_frame_checks()

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
