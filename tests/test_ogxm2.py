# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Reading OGXM v2, HedgeHog's current format, into the v1 document shape.

Mirrors gvformat-js/test/test-ogxm2.js -- keep the two in step -- and reads the
same fixtures: written by HedgeHog's own reference writer from a real
hedgehog-bg.com export, players renamed, signatures removed
(``gvformat-js/test/fixtures/ogxm2/gen.py``). Each ``.expected.json`` is the
reference reader's replay of the same bytes, so positions are checked against
HedgeHog's, not against ourselves.

Run directly:
    uv run python tests/test_ogxm2.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat.binary import write_gvab
from gvformat.reader import GvabError, read_gvab

FIXTURES = _REPO_ROOT / "gvformat-js" / "test" / "fixtures" / "ogxm2"

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        _failures.append(label)


def data(name: str) -> bytes:
    return (FIXTURES / f"{name}.ogxm").read_bytes()


def expected(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.expected.json").read_text())


def raises_gvab(fn, needle: str) -> bool:
    try:
        fn()
    except GvabError as exc:
        return needle in str(exc)
    return False


def ogid_mismatches(doc: dict, exp: dict) -> list[str]:
    """Every ply's OGIDs against the reference's. A game we prefix with a
    set-position ply is compared from the ply after it."""
    out = []
    for gi, g in enumerate(doc["games"]):
        ref = exp["ogids"][gi]
        off = len(g["plies"]) - len(ref)
        for pi, (p, (before, after)) in enumerate(zip(g["plies"][off:], ref)):
            if p.get("ogid_before") != before or p.get("ogid_after") != after:
                out.append(f"{gi},{pi + off}")
    return out


def plies(doc: dict) -> list[dict]:
    return [p for g in doc["games"] for p in g["plies"]]


def has_luck(p: dict) -> bool:
    return isinstance((p.get("analysis") or {}).get("luck"), float)


def main() -> int:
    # A HedgeHog match, analysed ----------------------------------------------
    doc = read_gvab(data("match"))
    exp = expected("match")
    check(len(doc["games"]) == 3 and len(doc["games"][0]["plies"]) == 19
          and len(doc["games"][1]["plies"]) == 49, "match: three games, every ply")
    check(not ogid_mismatches(doc, exp), "match: every ply replays to the reference position")
    check(doc["player_white"] == "Alice" and doc["player_black"] == "Bob"
          and doc["match_length"] == 9, "match: header fields")
    check(doc["white_score"] == 1 and doc["black_score"] == 5 and doc["result"] == 0,
          "match: an unfinished match keeps its running score and no result")
    check(doc["games"][0]["winner"] == 0 and doc["games"][1]["winner"] == 1
          and doc["games"][1]["points_won"] == 4, "match: game winners and points")
    info = doc["analysis_info"]
    check(info["model_id"] == "16fcd41c-9f64-4fcc-bca2-c89e6de07721" and info["model_name"] == "xerxes", "match: the model id and name as stated")
    check(info["ply"] == 2 and info["eval_level"] == "2ply", "match: block depth")

    rolled = [p for p in plies(doc) if p["action_id"] <= 20]
    lucky = [p for p in rolled if has_luck(p)]
    check(len(lucky) == len(rolled) - len(doc["games"]),
          "match: every roll but each game's opening one carries its luck")
    check(any(p["analysis"]["luck"] > 0 for p in lucky)
          and any(p["analysis"]["luck"] < 0 for p in lucky), "match: luck runs both ways")
    check(any(abs(p["analysis"]["luck"]) > 0.1 for p in lucky),
          "match: luck is on the equity scale (MWC swings are a fraction of it)")
    check(info.get("luck_eval_level") == "1ply", "match: luck level from the rolls' own")
    check("_base_analyses" not in doc, "match: luck is stored, so none needs measuring")

    doubles = [p for p in plies(doc) if p["action_id"] == 21 and p.get("analysis")]
    check(doubles and all(abs(p["analysis"]["double_pass_equity"] - 1) < 0.02 for p in doubles),
          "match: a cube ply's double/pass reads as +1, not as an MWC")
    checkers = [p for p in plies(doc) if (p.get("analysis") or {}).get("alternatives")]
    check(any(p["analysis"]["best_equity"] < 0 for p in checkers),
          "match: checker equities are on the equity scale (an MWC is never negative)")

    def loss_consistent(a: dict) -> bool:
        played = next((x for x in a["alternatives"] if x["is_played"]), None)
        return played is None or abs(a["equity_loss"] - (a["best_equity"] - played["equity"])) < 2e-4
    check(all(loss_consistent(p["analysis"]) for p in checkers),
          "match: equity_loss is best minus played, in the same units")
    check(any(p["analysis"]["decision"] for p in checkers)
          and any(not p["analysis"]["decision"] for p in checkers),
          "match: decision flags derived (some plays count, a forced one does not)")
    missed = [p for p in plies(doc) if (p.get("analysis") or {}).get("missed_double")]
    check(missed and all(p["analysis"].get("cube_decision")
                         and p["analysis"]["missed_double"]["equity_loss"] > 0 for p in missed),
          "match: a missed double carries its error and the live decision beside it")

    again = read_gvab(write_gvab(doc))
    check(not ogid_mismatches(again, exp), "match: survives a v1 write and re-read")
    check(sum(1 for p in plies(again) if p.get("analysis"))
          == sum(1 for p in plies(doc) if p.get("analysis")),
          "match: every analysis survives the v1 write")
    check(sum(1 for p in plies(again) if has_luck(p)) == len(lucky)
          and again["analysis_info"].get("luck_eval_level") == "1ply",
          "match: luck survives the v1 write")

    # Container checks ---------------------------------------------------------
    raw = data("match")
    corrupt = bytearray(raw)
    corrupt[40] ^= 0xFF
    check(raises_gvab(lambda: read_gvab(bytes(corrupt)), "CSUM mismatch"),
          "a corrupted byte fails the checksum")
    later = bytearray(raw)
    later[10] = 3
    check(raises_gvab(lambda: read_gvab(bytes(later)), "2.3 reader"),
          "a newer minimum reader is refused")
    check(raises_gvab(lambda: read_gvab(raw[:-10]), "file_size"), "a truncated file is refused")

    # Two blocks: HedgeHog's deeper re-run of a few decisions ------------------
    doc = read_gvab(data("two-blocks"))
    check(len(doc.get("analyses_info") or []) == 2, "two blocks: both listed")
    check(doc["analyses_info"][1]["ply"] == 3, "two blocks: the second reads at its own depth")
    both = [p for p in plies(doc) if len(p.get("analyses") or []) == 2]
    check(len(both) == 3 and all(p["analyses"][1]["analysis_index"] == 1 for p in both),
          "two blocks: the re-run decisions sit beside the first block's")
    check(doc.get("_base_analyses") == [1],
          "two blocks: only the block with no rolls is left for a luck pass")

    # A game from a set-up position ----------------------------------------------
    doc = read_gvab(data("set-up"))
    g = doc["games"][1]
    check(g["plies"][0]["action_id"] == 31 and not g["plies"][0].get("d1")
          and g["plies"][0]["set_position"] == g["initial_board"],
          "set-up: the starting board becomes a leading set-position ply")
    check(not ogid_mismatches(doc, expected("set-up")),
          "set-up: every ply after it replays to the reference position")
    check((g["plies"][1].get("analysis") or {}).get("alternatives") is not None,
          "set-up: decisions still find their plies past the inserted one")

    # A resignation ------------------------------------------------------------
    doc = read_gvab(data("resign"))
    g = doc["games"][0]
    last = g["plies"][-1]
    check(last["action_id"] == 27, "resign: the resignation is a ply")
    check(g["winner"] == (1 if last["color"] == 1 else 0) and g["points_won"] == 1,
          "resign: the other side wins")
    check(not ogid_mismatches(doc, expected("resign")), "resign: replays to the reference")

    # A game that opens with the cube turned: read, not refused ----------------
    doc = read_gvab(data("cube-on-two"))
    g = doc["games"][0]
    check(g.get("initial_cube_value") == 2 and "initial_cube_owner" not in g,
          "cube-on-two: the cube the game opens with is on the game")
    check(not ogid_mismatches(doc, expected("cube-on-two")),
          "cube-on-two: every position carries that cube, as the reference states it")

    # Untrusted bytes: whatever is wrong with them, the failure is a GvabError --
    rng = random.Random(12345)
    leaked = 0
    for _ in range(2000):
        m = bytearray(raw)
        for _k in range(1 + rng.randrange(4)):
            m[16 + rng.randrange(len(m) - 20)] = rng.randrange(256)
        try:
            read_gvab(bytes(m), verify_crc=False)
        except GvabError:
            pass
        except Exception:  # noqa: BLE001 - counting exactly these
            leaked += 1
    check(leaked == 0, f"2000 mutated v2 files raise only GvabError (leaked {leaked})")

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
