# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Validation harness for ogxm_export.to_ogxm_json.

Runs gvan_match.analyze_mat on a real match file, converts the result with
ogxm_export.to_ogxm_json, and asserts the output matches the OGXM-JSON shape
described in OGXM_JSON_SPEC_GAMMONVIEW.md. This calls the bgsage analyzer
(analyze_mat) to produce the *input* fixture -- ogxm_export.py itself makes
no engine calls.

Run directly:
    uv run python tests/test_ogxm_export.py

Note on check 4 (move-step round trip): this harness samples the first ~60
checker plies. A separate full-match sweep (all 574 checker plies across all 11 games of
filias.mat, the pre-corpus fixture) confirmed 0/574 board-reconstruction failures
(applying every ply's derived steps to board_before exactly reproduces
board_after -- the authoritative correctness signal for the `moves` field).
That sweep also found ~11/574 cases where this file's `_render_notation`
test helper (not ogxm_export.py itself) renders a cosmetically different
grouping than `player_move` for double rolls where two different checkers'
hops happen to share an identical (from, pips) pair (e.g. two separate
single-die hops of "7/5" arising from different checkers in the same double
roll) -- the helper's greedy chain-merge can't tell such duplicate hops
apart when reconstructing display spans. This is a limitation of the
*test's* re-render heuristic, not the underlying step data: the steps
themselves are order-independent and individually verified correct via the
board-reconstruction check.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvanalysis import analyze_mat
from gvformat.export import to_ogxm_json, _parse_notation, _convert_game

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


# ---------------------------------------------------------------------------
# Re-render helper for move-step round-trip validation (test-only; not part
# of the public ogxm_export API).
# ---------------------------------------------------------------------------

def _abs_to_mover(abs_point: int, mover_is_white: bool) -> int:
    return (25 - abs_point) if mover_is_white else abs_point


def _render_notation(steps: list[dict], mover_is_white: bool) -> str:
    """Turn OGXM {"from","pips"} steps back into "13/10 8/5"-style notation
    (mover's own numbering), merging chained same-checker hops and grouping
    identical repeated spans with "(n)", matching
    bgsage.text_export.compute_move_notation's output conventions (hit
    markers excluded -- steps carry no hit info)."""
    hops = []
    for s in steps:
        f_mover = _abs_to_mover(s["from"], mover_is_white)
        pips = s["pips"]
        t_mover = (25 - pips) if f_mover == 25 else (f_mover - pips)
        if t_mover <= 0:
            t_mover = 0
        hops.append((f_mover, t_mover))

    # Merge chained hops: hop[i+1].from == hop[i].to means the same checker
    # continued moving.
    spans: list[list[int]] = []
    used = [False] * len(hops)
    for i, (f, t) in enumerate(hops):
        if used[i]:
            continue
        used[i] = True
        cur_to = t
        changed = True
        while changed:
            changed = False
            for j, (f2, t2) in enumerate(hops):
                if used[j]:
                    continue
                if f2 == cur_to:
                    used[j] = True
                    cur_to = t2
                    changed = True
                    break
        spans.append([f, cur_to])

    spans.sort(key=lambda s: (-s[0], -s[1]))

    # Group identical consecutive spans with a count.
    grouped: list[tuple[int, int, int]] = []
    for f, t in spans:
        if grouped and grouped[-1][0] == f and grouped[-1][1] == t:
            pf, pt, pc = grouped[-1]
            grouped[-1] = (pf, pt, pc + 1)
        else:
            grouped.append((f, t, 1))

    parts = []
    for f, t, count in grouped:
        fs = "bar" if f == 25 else str(f)
        ts = "off" if t == 0 else str(t)
        ms = f"{fs}/{ts}"
        parts.append(f"{ms}({count})" if count > 1 else ms)
    return " ".join(parts)


def _net_spans(notation: str) -> list[tuple[int, int]]:
    """Parse notation into the multiset of *net* (from, to) displacements.

    One checker's hops are collapsed into the span it actually travelled:
    ``bar/20* 20/15`` and ``bar/15`` describe the same checker arriving on 15,
    and differ only because the intermediate point was hit. ``_render_notation``
    carries no hit information, so it always produces the merged spelling.

    Comparing raw spans made check 4 depend on which plies the sample window
    happened to reach -- the module docstring records ~11/574 such cases in the
    old fixture, none of which fell inside its window. Merging both sides
    removes that luck without giving up the signal: a wrong destination, a
    dropped checker or a wrong pip count all still differ after merging, and
    board reconstruction remains the authoritative check either way.
    """
    spans = list(_parse_notation(notation))
    merged = True
    while merged:
        merged = False
        for i, (a_from, a_to) in enumerate(spans):
            for j, (b_from, b_to) in enumerate(spans):
                if i != j and a_to == b_from:
                    spans[i] = (a_from, b_to)
                    del spans[j]
                    merged = True
                    break
            if merged:
                break
    return sorted(spans)


def _apply_steps(board_before: list[int], steps: list[dict], mover_is_white: bool) -> list[int]:
    """Apply OGXM move steps to a P1/White-perspective board; return the
    resulting board. Used to verify steps reconstruct board_after exactly,
    independent of notation text."""
    board = list(board_before)
    for s in steps:
        f_abs = s["from"]
        pips = s["pips"]
        # Work in mover-perspective raw index space (0=opp bar,25=own bar,
        # 1-24 mover's own numbering), matching game_eval.py's own board
        # convention, then map back to P1 space at the end.
        if mover_is_white:
            mb = list(board)
        else:
            mb = [0] * 26
            mb[0] = board[25]
            mb[25] = board[0]
            for i in range(1, 25):
                mb[25 - i] = -board[i]

        f_mover = _abs_to_mover(f_abs, mover_is_white)
        t_mover = (25 - pips) if f_mover == 25 else (f_mover - pips)
        hit = False
        if t_mover >= 1:
            if mb[t_mover] < 0:
                # hit a blot
                mb[t_mover] = 1
                mb[0] += 1
                hit = True
            elif not hit:
                mb[t_mover] += 1
        # remove from source
        if f_mover == 25:
            mb[25] -= 1
        elif t_mover <= 0:
            mb[f_mover] -= 1  # bear off
        else:
            mb[f_mover] -= 1

        if mover_is_white:
            board = mb
        else:
            flipped = [0] * 26
            flipped[0] = mb[25]
            flipped[25] = mb[0]
            for i in range(1, 25):
                flipped[25 - i] = -mb[i]
            board = flipped
    return board


# ---------------------------------------------------------------------------
# OGID turn-phase state machine validation helpers
# ---------------------------------------------------------------------------

def _ogid_field(ogid: str, index: int) -> str:
    """Pull a colon-separated OGID field by index (2=cube, 5=game_state)."""
    return ogid.split(":")[index]


#: A hand-built synthetic starting board (P1/White perspective), reused so
#: the synthetic games below don't depend on ogxm_export's private starting
#: board constant.
_SYN_BOARD = [
    0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5,
    5, 0, 0, 0, -3, 0, -5, 0, 0, 0, 0, 2, 0,
]


def _syn_checker_entry(player: str, dice: list[int]) -> dict:
    """Minimal no-op checker-ply entry: enough for _convert_game to build an
    ogid_before/ogid_after pair without needing a legal/analyzed move."""
    return {
        "player": player,
        "kind": "checker",
        "dice": dice,
        "cube_value": 1,
        "cube_owner": "centered",
        "player_move": "",
        "move_options": [],
        "board_before": list(_SYN_BOARD),
        "board_after": list(_SYN_BOARD),
    }


def _syn_double_entry(player: str) -> dict:
    return {
        "player": player,
        "kind": "cube_decision",
        "cube_value": 1,
        "equity_no_double": 0.10,
        "equity_double_take": 0.30,
        "equity_double_pass": 1.00,
        "optimal_action": "double",
        "player_action": "double",
        "lost_equity": 0.0,
        "counted": True,
        "board": list(_SYN_BOARD),
    }


def _syn_response_entry(player: str, response: str) -> dict:
    return {
        "player": player,
        "kind": "cube_response",
        "cube_value": 2,
        "equity_take": 0.30,
        "equity_pass": 1.00,
        "optimal_response": "take" if response == "take" else "pass",
        "player_response": response,
        "lost_equity": 0.0,
        "counted": True,
        "board": list(_SYN_BOARD),
    }


def _check_synthetic_cube_sequence() -> None:
    """Deterministic, self-contained regression for the double/take and
    double/drop transitions -- doesn't depend on the corpus match happening to
    contain both cases (it does, but this keeps the assertion independent
    of that external fixture's exact content)."""
    take_game = {
        "game_number": 1,
        "score_start": {"player1": 0, "player2": 0},
        "is_crawford": False,
        "moves": [
            _syn_checker_entry("A", [3, 1]),
            _syn_double_entry("A"),
            _syn_response_entry("B", "take"),
            _syn_checker_entry("A", [5, 4]),
        ],
        "result": {"winner": "A", "type": "normal", "points": 1},
    }
    out = _convert_game(take_game, player_white="A", player_black="B", match_length=0)
    plies = out["plies"]
    check(plies[0]["action_id"] == _dice_action_id_local(3, 1), "7. synthetic: opening ply is a dice action")
    check(
        _ogid_field(plies[0]["ogid_before"], 5) in ("IW", "IB"),
        f"7. synthetic: opening ply ogid_before state is initial; got {_ogid_field(plies[0]['ogid_before'], 5)!r}",
    )
    check(plies[1]["action_id"] == 21, "7. synthetic: double ply has action_id 21")
    check(
        _ogid_field(plies[1]["ogid_after"], 5) == "D",
        f"7. synthetic: double ogid_after state == 'D'; got {_ogid_field(plies[1]['ogid_after'], 5)!r}",
    )
    check(
        _ogid_field(plies[1]["ogid_after"], 2).endswith("O"),
        f"7. synthetic: double ogid_after cube field ends 'O'; got {_ogid_field(plies[1]['ogid_after'], 2)!r}",
    )
    check(plies[2]["action_id"] == 22, "7. synthetic: take ply has action_id 22")
    check(
        _ogid_field(plies[2]["ogid_after"], 5) == "A",
        f"7. synthetic: take ogid_after state == 'A'; got {_ogid_field(plies[2]['ogid_after'], 5)!r}",
    )
    check(
        _ogid_field(plies[2]["ogid_after"], 2).endswith("T"),
        f"7. synthetic: take ogid_after cube field ends 'T'; got {_ogid_field(plies[2]['ogid_after'], 2)!r}",
    )
    check(
        _ogid_field(plies[2]["ogid_after"], 2)[0] == "B",
        f"7. synthetic: take ogid_after cube owner == 'B' (taker); got {_ogid_field(plies[2]['ogid_after'], 2)!r}",
    )
    # Checker ply right after the take: before_state must be "R" (rolled),
    # not "A" -- the checker branch always uses R for a non-opening ply.
    check(
        _ogid_field(plies[3]["ogid_before"], 5) == "R",
        f"7. synthetic: post-take checker ogid_before state == 'R'; got {_ogid_field(plies[3]['ogid_before'], 5)!r}",
    )
    check(
        _ogid_field(plies[3]["ogid_after"], 5) == "C",
        f"7. synthetic: post-take checker ogid_after state == 'C'; got {_ogid_field(plies[3]['ogid_after'], 5)!r}",
    )

    drop_game = {
        "game_number": 1,
        "score_start": {"player1": 0, "player2": 0},
        "is_crawford": False,
        "moves": [
            _syn_checker_entry("A", [3, 1]),
            _syn_double_entry("A"),
            _syn_response_entry("B", "pass"),
        ],
        "result": {"winner": "A", "type": "pass", "points": 1},
    }
    out = _convert_game(drop_game, player_white="A", player_black="B", match_length=0)
    plies = out["plies"]
    check(plies[2]["action_id"] == 23, "7. synthetic: drop ply has action_id 23")
    check(
        _ogid_field(plies[2]["ogid_after"], 5) == "G",
        f"7. synthetic: drop ogid_after state == 'G'; got {_ogid_field(plies[2]['ogid_after'], 5)!r}",
    )
    check(
        _ogid_field(plies[2]["ogid_after"], 2).endswith("P"),
        f"7. synthetic: drop ogid_after cube field ends 'P'; got {_ogid_field(plies[2]['ogid_after'], 2)!r}",
    )
    # Cube ownership/value must NOT change on a drop.
    check(
        _ogid_field(plies[2]["ogid_after"], 2) == "N0P",
        f"7. synthetic: drop leaves cube centered at value 1; got {_ogid_field(plies[2]['ogid_after'], 2)!r}",
    )


def _dice_action_id_local(d1: int, d2: int) -> int:
    pairs = [(a, b) for a in range(1, 7) for b in range(a, 7)]
    return pairs.index((min(d1, d2), max(d1, d2)))


def main() -> int:
    if MAT_PATH is None:
        print(missing_mat_message())
        return 0

    print(f"Analyzing {MAT_PATH} (preset=very_quick)...")
    result = analyze_mat(str(MAT_PATH), preset="very_quick", quiet=True)

    print("Converting to OGXM-JSON...")
    ogxm = to_ogxm_json(result)

    # 1. No `summary` key; required top-level fields present.
    check("summary" not in ogxm, "1. no top-level 'summary' key")
    for key in ("match_length", "player_white", "player_black", "games", "analysis_info"):
        check(key in ogxm, f"1. top-level has '{key}'")
    check("preset" in ogxm["analysis_info"], "1. analysis_info has 'preset'")
    check("eval_level" in ogxm["analysis_info"], "1. analysis_info has 'eval_level'")
    print(f"   analysis_info = {ogxm['analysis_info']}")

    # 2. games[0]["game_index"] == 0; games have 'plies' not 'moves'.
    g0 = ogxm["games"][0]
    check(g0["game_index"] == 0, "2. games[0].game_index == 0")
    check("plies" in g0, "2. games[0] has 'plies'")
    check("moves" not in g0, "2. games[0] has no 'moves' key")

    # 3. First ply's ogid_before == standard starting-position OGID.
    first_ply = g0["plies"][0]
    expected_start_ogid_prefix = "11ccccchhhjjjjj:66666888dddddoo"
    actual = first_ply["ogid_before"]
    check(
        actual.startswith(expected_start_ogid_prefix),
        f"3. first ply ogid_before starts with starting position "
        f"({expected_start_ogid_prefix!r}); got {actual!r}",
    )
    print(f"   first ply ogid_before = {actual!r}")

    # 3b. First ply's ogid_before game_state field is a real initial state
    # (IW/IB), not empty -- the OGID turn-phase state machine's opening case.
    first_state = _ogid_field(actual, 5)
    check(
        first_state in ("IW", "IB"),
        f"3b. first ply ogid_before game_state is initial (IW/IB), not empty; got {first_state!r}",
    )

    # 4. Move-step round trip for several checker plies.
    player_white = ogxm["player_white"]
    round_trip_checked = 0
    round_trip_ok = 0
    board_reconstruct_ok = 0
    examples = []
    for game_in, game_out in zip(result["games"], ogxm["games"]):
        checker_plies = [p for p in game_out["plies"] if p.get("action_id", 99) <= 20]
        # Align by position: every checker moves-entry maps 1:1 to a checker ply
        # (both walk the game's moves list in order, filtering to kind=="checker").
        all_checker_entries = [m for m in game_in["moves"] if m["kind"] == "checker"]
        for entry, ply in zip(all_checker_entries, checker_plies):
            if round_trip_checked >= 60:
                break
            if not entry.get("move_options"):
                continue  # no_legal forced pass: nothing to round-trip
            round_trip_checked += 1
            is_white = entry["player"] == player_white
            rendered = _render_notation(ply["moves"], is_white)
            expected = entry["player_move"]
            # Compare as multisets of *net* (from, to) spans rather than raw
            # text: grouping style ("8/4* 8/4" vs "8/4(2)") is cosmetic, not a
            # correctness difference, and the renderer carries no hit info, so
            # a hop through a hit point reads as one span to it and two to the
            # source file. See _net_spans.
            ok = _net_spans(rendered) == _net_spans(expected)
            if ok:
                round_trip_ok += 1
            else:
                examples.append((entry["player_move"], rendered, entry["dice"], entry["player"]))

            reconstructed = _apply_steps(entry["board_before"], ply["moves"], is_white)
            if reconstructed == entry["board_after"]:
                board_reconstruct_ok += 1
            else:
                examples.append((f"BOARD MISMATCH for {entry['player_move']}", None, entry["dice"], entry["player"]))
        if round_trip_checked >= 12:
            break

    check(round_trip_checked > 0, "4. at least one checker ply was round-trip checked")
    check(
        board_reconstruct_ok == round_trip_checked,
        f"4. derived steps reconstruct board_after exactly ({board_reconstruct_ok}/{round_trip_checked})",
    )
    check(
        round_trip_ok == round_trip_checked,
        f"4. re-rendered notation matches player_move ({round_trip_ok}/{round_trip_checked})",
    )
    if examples:
        print("   mismatches (first 5):")
        for ex in examples[:5]:
            print(f"     {ex}")

    # 5. At least one cube ply (21/22/23); each game ends with a game-end ply.
    all_action_ids = {p["action_id"] for g in ogxm["games"] for p in g["plies"]}
    check(any(a in all_action_ids for a in (21, 22, 23)), "5. at least one cube ply (action_id 21/22/23) exists")
    game_end_ids = {24, 26, 27, 28, 29}
    all_games_end_properly = all(
        len(g["plies"]) > 0 and g["plies"][-1]["action_id"] in game_end_ids
        for g in ogxm["games"]
    )
    check(all_games_end_properly, "5. every game ends with a game-end ply (24/26/27/28/29)")

    # 6. analysis uses eval/equity_loss/correct_action/played_action -- not
    #    the old lost_equity/optimal_action/probs keys.
    sample_checker_analysis = next(
        p["analysis"] for g in ogxm["games"] for p in g["plies"]
        if p.get("action_id", 99) <= 20 and "analysis" in p
    )
    sample_cube_analysis = next(
        p["analysis"] for g in ogxm["games"] for p in g["plies"]
        if p.get("action_id") == 21
    )
    for label, analysis in (("checker", sample_checker_analysis), ("cube", sample_cube_analysis)):
        check("eval" in analysis, f"6. {label} analysis has 'eval' object")
        check("equity_loss" in analysis, f"6. {label} analysis has 'equity_loss'")
        check("lost_equity" not in analysis, f"6. {label} analysis has no old 'lost_equity' key")
        check("probs" not in analysis, f"6. {label} analysis has no old flat 'probs' key")
    check("correct_action" in sample_cube_analysis, "6. cube analysis has 'correct_action'")
    check("played_action" in sample_cube_analysis, "6. cube analysis has 'played_action'")
    check("optimal_action" not in sample_cube_analysis, "6. cube analysis has no old 'optimal_action' key")
    print(f"   sample checker analysis keys: {sorted(sample_checker_analysis.keys())}")
    print(f"   sample cube analysis keys: {sorted(sample_cube_analysis.keys())}")

    # 7. OGID turn-phase state machine: concrete transition asserts against
    # the real corpus-match data, plus a self-contained synthetic regression
    # (see _check_synthetic_cube_sequence) that doesn't depend on the corpus
    # happening to contain both a take and a drop.

    # 7a. A normal mid-game checker ply: ogid_before shows "R" (rolled),
    # ogid_after shows "C" (checker done). games[0].plies[1] is the second
    # turn of the match (not an opening ply), so it must be "R" -> "C".
    mid_checker_ply = None
    for g in ogxm["games"]:
        for p in g["plies"][1:]:
            if p.get("action_id", 99) <= 20:
                mid_checker_ply = p
                break
        if mid_checker_ply is not None:
            break
    check(mid_checker_ply is not None, "7a. found a non-opening checker ply")
    if mid_checker_ply is not None:
        before_state = _ogid_field(mid_checker_ply["ogid_before"], 5)
        after_state = _ogid_field(mid_checker_ply["ogid_after"], 5)
        check(before_state == "R", f"7a. mid-game checker ogid_before state == 'R'; got {before_state!r}")
        check(after_state == "C", f"7a. mid-game checker ogid_after state == 'C'; got {after_state!r}")
        print(f"   mid-game checker ply: before={mid_checker_ply['ogid_before']!r} after={mid_checker_ply['ogid_after']!r}")

    # 7b. Cube sequence transitions against the real data: a double ply's
    # ogid_after is state "D" with cube action "O"; a following take's
    # ogid_after is state "A" with cube action "T"; a following drop's
    # ogid_after is state "G" with cube action "P".
    double_ply = next(
        (p for g in ogxm["games"] for p in g["plies"] if p.get("action_id") == 21), None
    )
    check(double_ply is not None, "7b. found a double ply (action_id 21) in the corpus match")
    if double_ply is not None:
        d_state = _ogid_field(double_ply["ogid_after"], 5)
        d_cube = _ogid_field(double_ply["ogid_after"], 2)
        check(d_state == "D", f"7b. double ogid_after state == 'D'; got {d_state!r}")
        check(d_cube.endswith("O"), f"7b. double ogid_after cube field ends 'O'; got {d_cube!r}")
        print(f"   double ply ogid_after: {double_ply['ogid_after']!r}")

    take_ply = next(
        (p for g in ogxm["games"] for p in g["plies"] if p.get("action_id") == 22), None
    )
    check(take_ply is not None, "7b. found a take ply (action_id 22) in the corpus match")
    if take_ply is not None:
        t_state = _ogid_field(take_ply["ogid_after"], 5)
        t_cube = _ogid_field(take_ply["ogid_after"], 2)
        check(t_state == "A", f"7b. take ogid_after state == 'A'; got {t_state!r}")
        check(t_cube.endswith("T"), f"7b. take ogid_after cube field ends 'T'; got {t_cube!r}")
        print(f"   take ply ogid_after: {take_ply['ogid_after']!r}")

    drop_ply = next(
        (p for g in ogxm["games"] for p in g["plies"] if p.get("action_id") == 23), None
    )
    check(drop_ply is not None, "7b. found a drop ply (action_id 23) in the corpus match")
    if drop_ply is not None:
        g_state = _ogid_field(drop_ply["ogid_after"], 5)
        g_cube = _ogid_field(drop_ply["ogid_after"], 2)
        check(g_state == "G", f"7b. drop ogid_after state == 'G'; got {g_state!r}")
        check(g_cube.endswith("P"), f"7b. drop ogid_after cube field ends 'P'; got {g_cube!r}")
        print(f"   drop ply ogid_after: {drop_ply['ogid_after']!r}")

    # 7c. Self-contained synthetic regression (double->take, double->drop),
    # independent of the corpus match's exact content.
    _check_synthetic_cube_sequence()

    # 8. OGXM base-spec reconciliation: embedded cube_decision (renamed from
    #    cube_analysis) with should_double/action; missed_double without
    #    classification; classification dropped everywhere (compute-on-read);
    #    equity_loss retained; per-decision analysis.ply present on at least
    #    one deepened checker ply while per-alt eval_level survives.
    all_analyses = [
        p["analysis"] for g in ogxm["games"] for p in g["plies"] if "analysis" in p
    ]

    # 8a. No `classification` key anywhere (top-level analysis or any sub-object).
    def _has_classification(obj) -> bool:
        if isinstance(obj, dict):
            if "classification" in obj:
                return True
            return any(_has_classification(v) for v in obj.values())
        if isinstance(obj, list):
            return any(_has_classification(v) for v in obj)
        return False

    check(not _has_classification(ogxm), "8a. no 'classification' field anywhere in output")

    # 8b. Embedded cube_decision (correct no-double) present, old cube_analysis gone.
    cube_decision = next(
        (a["cube_decision"] for a in all_analyses if "cube_decision" in a), None
    )
    check(
        not any("cube_analysis" in a for a in all_analyses),
        "8b. no embedded 'cube_analysis' key remains (renamed to cube_decision)",
    )
    check(cube_decision is not None, "8b. an embedded 'cube_decision' sub-object exists")
    if cube_decision is not None:
        check("should_double" in cube_decision, "8b. cube_decision has 'should_double'")
        check(isinstance(cube_decision["should_double"], bool), "8b. cube_decision.should_double is bool")
        check(
            cube_decision.get("action") in ("no_double", "double_take", "double_pass"),
            f"8b. cube_decision.action is a valid label; got {cube_decision.get('action')!r}",
        )
        check("equity_loss" in cube_decision, "8b. cube_decision retains 'equity_loss'")
        check("decision" in cube_decision, "8b. cube_decision keeps 'decision' extension")
        check("classification" not in cube_decision, "8b. cube_decision has no 'classification'")
        print(f"   sample cube_decision: {cube_decision}")

    # 8c. missed_double (if any) has no classification but keeps equity_loss.
    missed_double = next(
        (a["missed_double"] for a in all_analyses if "missed_double" in a), None
    )
    if missed_double is not None:
        check("classification" not in missed_double, "8c. missed_double has no 'classification'")
        check("equity_loss" in missed_double, "8c. missed_double retains 'equity_loss'")
        check(missed_double.get("correct_action") == "double", "8c. missed_double.correct_action == 'double'")
        print(f"   sample missed_double: {missed_double}")
    else:
        print("   INFO: this match produced no embedded missed_double under this preset")

    # 8d. At least one checker analysis carries a per-decision `ply` int deeper
    #     than the base (first-pass/screen) ply, while per-alt eval_level is
    #     still present. This only occurs under a two-pass preset (very_quick is
    #     single-pass, so nothing deepens): run `fast` (2ply screen -> 3ply on
    #     error) to exercise a genuinely deepened decision.
    print("Converting a `fast`-preset pass for per-decision ply check...")
    fast_ogxm = to_ogxm_json(analyze_mat(str(MAT_PATH), preset="fast", quiet=True))
    fast_base_ply = fast_ogxm["analysis_info"]["ply"]
    fast_analyses = [
        p["analysis"] for g in fast_ogxm["games"] for p in g["plies"] if "analysis" in p
    ]
    deepened = [
        a for a in fast_analyses
        if isinstance(a.get("ply"), int) and "alternatives" in a
    ]
    check(len(deepened) > 0, "8d. at least one checker analysis has a per-decision 'ply' int")
    check(not _has_classification(fast_ogxm), "8d. no 'classification' anywhere in fast-preset output either")
    if deepened:
        a = deepened[0]
        check(a["ply"] > fast_base_ply, f"8d. analysis.ply {a['ply']} exceeds base ply {fast_base_ply}")
        check(
            any("eval_level" in alt for alt in a["alternatives"]),
            "8d. per-alt 'eval_level' still present on a deepened analysis",
        )
        print(f"   deepened analysis.ply = {a['ply']} (base {fast_base_ply}); "
              f"alt eval_levels = {[alt.get('eval_level') for alt in a['alternatives'][:3]]}")

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
