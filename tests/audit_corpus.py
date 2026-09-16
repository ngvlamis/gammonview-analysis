# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Check a sample corpus against every requirement the suite imposes on it.

``samples/README.md`` is the specification; this is the executable form of it,
for use *before* a new corpus is committed. Running the suite would tell you
the same thing eventually, but it needs the engine, it takes minutes, and a
corpus shortfall surfaces as a failure in a test whose subject is something
else -- ``test_xg_eval_levels`` going red says nothing about eval levels if the
real problem is that no file was analyzed at XG Roller++.

Three requirements are collective and quiet, in the sense that a corpus can
lose them without any single file looking wrong:

* the ``.xg`` corpus must exercise ``truncated1``, ``truncated2``,
  ``truncated3`` and ``database`` *between them*;
* it must contain at least one alternative with a zero win probability and at
  least one with all five probabilities zero;
* at least one ``.mat`` must be match play carrying a double, a take and a drop.

Everything here is engine-free -- it imports ``gvformat`` only, so it runs on a
machine with no bgsage.

Usage::

    uv run python tests/audit_corpus.py samples
    uv run python tests/audit_corpus.py samples_v2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat import (  # noqa: E402
    convert_bgf, convert_xg, mat_to_ogxm, parse_ogid, read_gvab,
)
from gvformat.export import _flip_board  # noqa: E402
from gvformat.reader import _apply_moves_p1  # noqa: E402

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


def _to_p1(ogid: str) -> list[int]:
    """A ply's OGID board in the P1/White frame ``_apply_moves_p1`` works in."""
    st = parse_ogid(ogid)
    board = list(st.board)
    return board if st.on_roll == "W" else _flip_board(board)


def _replay_holds(doc: dict) -> tuple[int, list[str]]:
    """Every ply's ``moves`` must replay onto its own ``ogid_after``.

    The whole-file invariant behind test_xg_move_steps / test_bgf_move_steps: a
    step onto a point the opponent has made reads back as a hit, and every
    board from that ply on is wrong.
    """
    total, bad = 0, []
    for gi, game in enumerate(doc["games"], start=1):
        for pi, ply in enumerate(game["plies"]):
            before, after = ply.get("ogid_before"), ply.get("ogid_after")
            if not before or not after or ply.get("d1") is None:
                continue
            total += 1
            replayed = _apply_moves_p1(
                _to_p1(before), ply.get("moves") or [], bool(ply.get("color")),
            )
            if replayed != _to_p1(after):
                bad.append(f"g{gi} ply{pi} ({ply['d1']}-{ply['d2']})")
    return total, bad


def audit_mat(root: Path) -> None:
    files = sorted((root / "mat").glob("*.mat"))
    print(f"\n--- mat/ ({len(files)} files) ---")
    check(bool(files), "mat/ is present")
    eligible, money = [], 0
    for path in files:
        doc = mat_to_ogxm(path.read_text())
        ids = {p.get("action_id") for g in doc["games"] for p in g["plies"]}
        cube = sorted(i for i in (21, 22, 23) if i in ids)
        if doc["match_length"] == 0:
            money += 1
        elif cube == [21, 22, 23]:
            eligible.append(path.stem)
    check(bool(eligible),
          f"a match-play .mat carries a double, a take and a drop "
          f"({len(eligible)} qualify for fixtures.MATCH)")
    for stem in eligible:
        print(f"        eligible: {stem}")
    print(f"        {money} money session(s), {len(files) - money} match play")


def audit_xg(root: Path) -> None:
    files = sorted((root / "xg").glob("*.xg"))
    print(f"\n--- xg/ ({len(files)} files) ---")
    check(bool(files), "xg/ is present")
    levels: set[str] = set()
    zero_win = all_zero = blank = 0
    plies = alts = 0
    for path in files:
        doc = convert_xg(path)
        total, bad = _replay_holds(doc)
        plies += total
        check(not bad, f"{path.stem}: every played move replays onto ogid_after"
                       f"{' -- ' + ', '.join(bad[:3]) if bad else ''}")
        for game in doc["games"]:
            for ply in game["plies"]:
                for alt in (ply.get("analysis") or {}).get("alternatives") or []:
                    alts += 1
                    if alt.get("eval_level"):
                        levels.add(alt["eval_level"])
                    ev = alt.get("eval")
                    if not ev:
                        blank += 1
                        continue
                    probs = [ev[k] for k in
                             ("win", "gammon_win", "bg_win", "gammon_loss", "bg_loss")]
                    if ev["win"] == 0:
                        zero_win += 1
                    if not any(probs):
                        all_zero += 1
    check(blank == 0, f"every alternative carries probabilities ({blank} do not)")
    check(zero_win > 0, f"the corpus exercises zero-win evaluations ({zero_win})")
    check(all_zero > 0, f"the corpus exercises the all-zero case ({all_zero})")
    for level in ("truncated1", "truncated2", "truncated3", "database"):
        check(level in levels, f"the corpus exercises {level}")
    print(f"        {plies} plies, {alts} alternatives, levels seen: "
          f"{', '.join(sorted(levels))}")


def audit_bgf(root: Path) -> None:
    files = sorted((root / "bgf").glob("*.bgf"))
    print(f"\n--- bgf/ ({len(files)} files) ---")
    check(bool(files), "bgf/ is present")
    plies = 0
    for path in files:
        doc = convert_bgf(path)
        total, bad = _replay_holds(doc)
        plies += total
        check(not bad, f"{path.stem}: every played move replays onto ogid_after"
                       f"{' -- ' + ', '.join(bad[:3]) if bad else ''}")
    check(plies > 0, f"bgf plies with dice replayed ({plies})")


def audit_gv(root: Path) -> None:
    gvab = sorted((root / "gv").glob("*.gvab"))
    gva = sorted((root / "gv").glob("*.gva"))
    print(f"\n--- gv/ ({len(gvab)} .gvab, {len(gva)} .gva) ---")
    check(bool(gvab) or bool(gva), "gv/ is present")
    for path in gvab:
        try:
            read_gvab(path.read_bytes())
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"        {path.name}: {exc}")
        check(ok, f"{path.stem}: .gvab reads back")
    # The one readable reference has to agree with its binary, or the two forms
    # have drifted -- which is the only thing shipping both of them can catch.
    for path in gva:
        pair = path.with_suffix(".gvab")
        if pair.exists():
            check(json.loads(path.read_text()) == read_gvab(pair.read_bytes()),
                  f"{path.stem}: .gva == read_gvab(.gvab)")
        else:
            print(f"        {path.name}: no paired .gvab (nothing to cross-check)")


def audit_stems(root: Path) -> None:
    """The names the suite hard-codes have to resolve inside this corpus."""
    print("\n--- hard-coded names ---")
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
    from fixtures import MATCH  # noqa: E402  (after sys.path fix-up)
    check((root / "mat" / MATCH).is_file(),
          f"fixtures.MATCH resolves ({MATCH})")
    golden = sorted(p.name[: -len(".fast.gvab")]
                    for p in (_REPO_ROOT / "tests" / "golden").glob("*.fast.gvab"))
    for stem in golden:
        check((root / "mat" / f"{stem}.mat").is_file(),
              f"golden stem has a source .mat ({stem})")


def audit_js_references(root: Path) -> None:
    """Every corpus .mat needs a Python reference for the JS parity test.

    ``gvformat-js/test/test-mat.js`` reads the .mat files from this corpus and
    compares ``convertMat`` against a committed ``mat_to_ogxm`` reference. A
    .mat with no reference *skips* rather than fails there -- deliberately, so
    adding a match cannot redden the JS suite -- which means the silent failure
    mode is a match that quietly stops being checked. This is where it gets
    caught. Regenerate with the command in that file's header.
    """
    print("\n--- JS parity references ---")
    ref_dir = _REPO_ROOT / "gvformat-js" / "test" / "fixtures" / "mat"
    mats = sorted(p.stem for p in (root / "mat").glob("*.mat"))
    missing = [m for m in mats if not (ref_dir / f"{m}.json").is_file()]
    check(not missing,
          f"every .mat has a JS parity reference ({len(mats) - len(missing)}/{len(mats)})"
          + (f" -- missing: {', '.join(missing)}" if missing else ""))
    orphans = sorted(p.stem for p in ref_dir.glob("*.json") if p.stem not in mats)
    check(not orphans,
          "no reference outlives its .mat"
          + (f" -- orphaned: {', '.join(orphans)}" if orphans else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default="samples",
                    help="corpus directory to audit (default: samples)")
    args = ap.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"no such directory: {root}")
        return 1

    print(f"auditing {root}/")
    for name, fn in (("mat", audit_mat), ("xg", audit_xg),
                     ("bgf", audit_bgf), ("gv", audit_gv)):
        if (root / name).is_dir():
            fn(root)
        else:
            print(f"\n--- {name}/ --- absent, skipped")
    audit_stems(root)
    audit_js_references(root)

    print()
    print("=" * 66)
    print(f"{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("\nshortfalls:")
        for f in _failures:
            print(f"  {f}")
    print("=" * 66)
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
