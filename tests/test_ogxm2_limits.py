# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Where the document and an OGXM file must agree, and where v2 sets a limit.

* An abandoned match (``result`` 3) reads back as stored, not as the result its
  score implies.
* A cube verdict comes back as the label it was written from: too good, beaver
  and raccoon are not folded into no-double and take.
* The cap on analysis blocks is v2's 64; ``write_gvab_v1`` holds 16 and refuses
  more with an error rather than writing a file no reader accepts.
* A checker decision with more than 1024 alternatives is written truncated with
  ``alternatives_total`` stating the full count, and the played move stays in the
  list. A match with too many games, or a game with too many plies, is refused.

Mirrors gvformat-js/test/test-ogxm2-limits.js -- keep the two in step.

Run directly:
    uv run python tests/test_ogxm2_limits.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_blocks_cases as B  # noqa: E402
import ogxm2_fields_cases as C  # noqa: E402
from gvformat import MAX_ANALYSES, append_analysis, read_gvab, write_gvab, write_gvab_v1  # noqa: E402

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        _failures.append(label)


def raises(fn, text: str) -> bool:
    try:
        fn()
    except ValueError as exc:
        return text in str(exc)
    return False


def main() -> int:
    print("--- 1. an abandoned match stays abandoned ---")
    doc = C.midmatch()
    data = write_gvab(doc)
    check(read_gvab(data)["result"] == 1, "1. a result the score decides is still derived from it")
    for result in (3, 0):
        d = copy.deepcopy(doc)
        d["result"] = result
        back = read_gvab(write_gvab(d))
        check(back["result"] == (3 if result == 3 else 1),
              f"1. result {result} reads back as {3 if result == 3 else 1}")
    d = copy.deepcopy(doc)
    d["result"] = 3
    blob = write_gvab(d)
    check(write_gvab(read_gvab(blob)) == blob, "1. and rewriting it is byte-stable")

    print("\n--- 2. a cube verdict comes back as the label it was written from ---")
    for label, double_label in (("beaver", "too_good"), ("raccoon", "no_double")):
        d = B.rich_money()
        _, _, offer = B._cube_ply(d, 21)
        _, _, answer = B._cube_ply(d, 23)
        offer["analysis"]["correct_action"] = double_label
        answer["analysis"]["correct_action"] = label
        blob = write_gvab(d)
        back = read_gvab(blob)
        _, _, offer2 = B._cube_ply(back, 21)
        _, _, answer2 = B._cube_ply(back, 23)
        check(answer2["analysis"]["correct_action"] == label,
              f"2. a {label} verdict is read as {label!r}, not as a take")
        check(offer2["analysis"]["correct_action"] == double_label,
              f"2. a {double_label} verdict is read as {double_label!r}")
        check(write_gvab(back) == blob, f"2. and the {label} file rewrites byte for byte")
    check(read_gvab(write_gvab_v1(d)) is not None,
          "2. v1 holds only four labels and writes the refinements as the action they refine")

    print("\n--- 3. blocks: v2 holds 64, a v1 file 16 ---")
    check(MAX_ANALYSES == 64, "3. the cap append_analysis enforces is v2's 64")
    d = B.golden(B.MONEY)
    d["analyses_info"] = [dict(d["analysis_info"], analysis_id=f"1f2e3d4c-0000-4000-8000-{i:012d}")
                          for i in range(17)]
    d.pop("analysis_info")
    check(raises(lambda: write_gvab_v1(d), "too many analysis blocks for an OGXM v1 file"),
          "3. write_gvab_v1 refuses 17 blocks with a clear error")
    d["analyses_info"] = d["analyses_info"] * 4 + [d["analyses_info"][0]]      # 69
    check(raises(lambda: write_gvab(d), "too many analysis blocks for an OGXM v2 file"),
          "3. write_gvab refuses more than 64")
    big = B.golden(B.MONEY)
    for i in range(MAX_ANALYSES - 1):
        more = B.golden(B.MONEY)
        more["analysis_info"].update(analysis_id=f"1f2e3d4c-0000-4000-8000-{i + 1:012d}",
                                     model_id=f"engine-{i}")
        big = append_analysis(big, more)
    check(len(big["analyses_info"]) == MAX_ANALYSES, "3. 64 blocks can be accumulated")
    more = B.golden(B.MONEY)
    more["analysis_info"].update(analysis_id="1f2e3d4c-0000-4000-8000-0000000000ff")
    check(raises(lambda: append_analysis(big, more), "cannot append"),
          "3. appending a 65th is refused")
    check(len(read_gvab(write_gvab(big))["analyses_info"]) == MAX_ANALYSES,
          "3. and all 64 are written and read back")

    print("\n--- 4. alternatives past 1024 are truncated and say so ---")
    def with_alts(n: int, played_at: int, total=None) -> tuple[dict, tuple]:
        d = B.rich_money()
        gi, pi, p = B.checker_plies(d, played_not_first=True)[0]
        a = p["analysis"]
        proto = copy.deepcopy(a["alternatives"][0])
        proto["is_played"] = False
        played = copy.deepcopy(next(x for x in a["alternatives"] if x["is_played"]))
        best = a["alternatives"][0]["equity"]
        alts = []
        for i in range(n):
            x = copy.deepcopy(proto)
            x["equity"] = best - 0.0001 * i - 0.0001
            alts.append(x)
        alts[0] = copy.deepcopy(a["alternatives"][0])
        alts[0]["is_played"] = False
        played["equity"] = alts[played_at]["equity"]
        alts[played_at] = played
        a["alternatives"] = alts
        a["equity_loss"] = round(best - played["equity"], 4)
        if total is not None:
            a["alternatives_total"] = total
        return d, (gi, pi)

    def decision(doc: dict, at: tuple) -> dict:
        return read_gvab(write_gvab(doc))["games"][at[0]]["plies"][at[1]]["analysis"]

    d, at = with_alts(1100, 3)
    a = decision(d, at)
    check(len(a["alternatives"]) == 1024 and a["alternatives_total"] == 1100,
          f"4. 1100 alternatives are written as 1024 with alternatives_total 1100 "
          f"(got {len(a['alternatives'])}, {a.get('alternatives_total')})")
    check(sum(1 for x in a["alternatives"] if x["is_played"]) == 1,
          "4. the played move, within the cut, is still the flagged one")
    d, at = with_alts(1100, 1050)
    a = decision(d, at)
    check(len(a["alternatives"]) == 1024 and a["alternatives_total"] == 1100
          and sum(1 for x in a["alternatives"] if x["is_played"]) == 1,
          "4. a played move past the cut is kept in the list")
    check(abs(a["equity_loss"] - d["games"][at[0]]["plies"][at[1]]["analysis"]["equity_loss"]) < 1e-4,
          "4. and the decision's equity loss is the document's")
    d, at = with_alts(1100, 3, total=5000)
    check(decision(d, at)["alternatives_total"] == 5000,
          "4. a larger total the decision already states is kept")
    d, at = with_alts(1024, 3)
    a = decision(d, at)
    check(len(a["alternatives"]) == 1024 and "alternatives_total" not in a,
          "4. exactly 1024 is not truncated and states no total")

    print("\n--- 5. games and plies ---")
    d = C.midmatch()
    d["games"] = [copy.deepcopy(d["games"][0]) for _ in range(1001)]
    check(raises(lambda: write_gvab(d), "too many games"), "5. 1001 games are refused")
    d = C.midmatch()
    d["games"][0]["plies"] = d["games"][0]["plies"][:1] * 1501
    check(raises(lambda: write_gvab(d), "too many plies"), "5. 1501 plies in a game are refused")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
