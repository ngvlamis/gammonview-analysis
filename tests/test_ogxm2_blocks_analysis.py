# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Our own analysis states where it came from, and what a rollout was.

``gvanalysis`` stamps every block it writes with ``engine_build`` (the bgsage
version) and ``complete`` (the whole match was looked at), and gives each
decision a rollout judged at one of bgsage's truncated levels the settings of that
rollout as v2's ``level.rollout`` -- trials, truncation depth, move depth, seed.
Nothing here is wall-clock, so the bytes stay deterministic and the goldens can
pin them.

Needs the engine (the ``[engine]`` extra). Run directly:
    uv run python tests/test_ogxm2_blocks_analysis.py
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_fields_cases as C  # noqa: E402
import ogxm2_oracle as oracle  # noqa: E402
from bgsage import BgBotAnalyzer  # noqa: E402
from gvanalysis import presets  # noqa: E402
from gvanalysis.match import ROLLOUT_LEVELS, analyze_ogxm, engine_build  # noqa: E402
from gvformat import append_analysis, read_gvab, to_ogxm_json, write_gvab  # noqa: E402

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


def alternatives(doc: dict):
    for g in doc["games"]:
        for p in g["plies"]:
            yield from ((p.get("analysis") or {}).get("alternatives") or [])


def short_match() -> dict:
    """The first twelve plies of a game, unfinished: enough for a few decisions."""
    base = C.midmatch()
    g = base["games"][0]
    g["plies"] = g["plies"][:12]
    g.update(winner=255, points_won=0, is_lastgame=False)
    base["games"] = [g]
    base.update(white_score=2, black_score=2, result=0)
    return base


def main() -> int:
    # --- the table is bgsage's ------------------------------------------------
    for label, want in ROLLOUT_LEVELS.items():
        cfg = BgBotAnalyzer(eval_level=label, cubeful=True)._analyzer._inner._rollout_config
        check(cfg["n_trials"] == want["trials"] and cfg["truncation_depth"] == want["truncation_depth"]
              and cfg["decision_ply"] == want["move_ply"] and str(cfg["seed"]) == want["seed"],
              f"{label}: ROLLOUT_LEVELS states what bgsage runs ({cfg['n_trials']} trials, "
              f"truncation {cfg['truncation_depth']}, ply {cfg['decision_ply']}, seed {cfg['seed']})")

    # --- a run with no rollout ------------------------------------------------
    base = short_match()
    data = analyze_ogxm(base, preset="very_quick", quiet=True, jobs=1)
    check(data["summary"]["engine_build"] == engine_build() and engine_build()
          and data["summary"]["complete"] is True, "the summary names the build and says the run was complete")
    out = append_analysis(base, to_ogxm_json(data, keep_orientation=True))
    check(out["analysis_info"]["engine_build"] == engine_build() and out["analysis_info"]["complete"] is True,
          "the block carries them")
    check(not any("level" in a for a in alternatives(out)),
          "and no alternative states a level: the labels give it")
    back = read_gvab(write_gvab(out))
    check(back["analysis_info"]["engine_build"] == engine_build() and back["analysis_info"]["complete"] is True
          and "coverage" not in back["analysis_info"] and "level" not in back["analysis_info"],
          "the file reads back with them, and with no coverage (every ply) and no level")

    # --- a run with a rollout -------------------------------------------------
    presets.PRESETS["rolltest"] = dataclasses.replace(
        presets.PRESETS["fast"], key="rolltest", first_pass="1ply", second_pass="truncated1",
        error_threshold_checker=0.0, error_threshold_cube=0.0)
    data = analyze_ogxm(base, preset="rolltest", quiet=True, jobs=1)
    out = append_analysis(base, to_ogxm_json(data, keep_orientation=True))
    rolled = [a for a in alternatives(out) if a.get("eval_level") == "truncated1"]
    check(len(rolled) > 3 and all(a["level"]["rollout"] == ROLLOUT_LEVELS["truncated1"] for a in rolled),
          f"each alternative judged by a rollout states the rollout ({len(rolled)} of them)")
    check(all("level" not in a for a in alternatives(out) if a.get("eval_level") != "truncated1"),
          "and the rest state nothing")
    blob = write_gvab(out)
    back = read_gvab(blob)
    check(write_gvab(back) == blob, "the file rewrites byte for byte")
    check([a.get("level") for a in alternatives(back)] == [a.get("level") for a in alternatives(out)],
          "and every level is back exactly as stated, no key added and none lost")
    try:
        j = oracle.binary_to_json(blob)
        check(oracle.check_json(j) == "ok" and j["replay_complete"] and oracle.json_to_binary(j) == blob,
              "the reference accepts the file")
        ra = [a for g in j["games"] for p in g["plies"] for a in (p.get("analysis") or {}).get("alternatives", [])
              if a.get("level", {}).get("rollout")]
        check(len(ra) == len(rolled) and all(
            a["level"]["rollout"]["trials"] == 72 and a["level"]["rollout"]["truncation_depth"] == 5
            and a["level"]["rollout"]["seed"] == "42" and a.get("rolled_out") for a in ra),
            "and reads each as rolled out: 72 trials, truncated at 5, seed 42")
    except oracle.OracleUnavailable:
        pass

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
