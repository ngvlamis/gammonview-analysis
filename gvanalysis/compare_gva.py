# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Compare two or more .gva analyses of the same match across different engines.

Assumes all input files analyze the same match and index moves identically
by (game_number, move_number) -- e.g. one .gva per engine (xg, bgsage,
bgblitz, gnubg, ...) produced by each engine's own conversion script.

No interpretation is applied: this reports agreement rates, pairwise diffs,
and a list of non-unanimous decisions. Judgment calls are limited to two
documented mechanics -- move-notation normalization (see normalize_move)
and restricting the "counted" agreement rate to counted=true decisions.

Usage:
    python compare_gva.py xg.gva bgsage.gva
    python compare_gva.py xg.gva bgsage.gva bgblitz.gva gnubg.gva
    python compare_gva.py xg.gva bgsage.gva --json report.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from pathlib import Path


def normalize_move(move: str | None) -> str | None:
    """Canonicalize a move string so equivalent moves compare equal.

    Handles three sources of engine-specific formatting that don't change
    the resulting board:
      - "25/x" vs "bar/x", "x/0" vs "x/off"
      - "(n)" run-length notation for n identical sub-moves
      - multi-hop legs written separately (e.g. "13/11 11/6") vs collapsed
        into a single hop ("13/6") -- these are merged by chaining any leg
        whose destination matches another leg's source.

    A hit marker (*) is preserved on the merged edge if any leg in its
    chain carried one, but which intermediate point was hit is not
    preserved -- two moves that differ only in which square along a
    multi-hop chain was hit will normalize identically.
    """
    if not move:
        return move

    expanded: list[str] = []
    for token in move.split():
        m = re.match(r"^(\d+|bar|25)/(\d+|off|0)\((\d+)\)$", token)
        if m:
            src, dst, n = m.group(1), m.group(2), int(m.group(3))
            expanded.extend([f"{src}/{dst}"] * n)
        else:
            expanded.append(token)

    edges = []
    for token in expanded:
        hit = token.endswith("*")
        body = token[:-1] if hit else token
        src, dst = body.split("/")
        if src == "25":
            src = "bar"
        if dst == "0":
            dst = "off"
        edges.append([src, dst, hit])

    changed = True
    while changed:
        changed = False
        for i, j in itertools.permutations(range(len(edges)), 2):
            if edges[i][1] == edges[j][0] and edges[i][1] != "off" and edges[j][0] != "bar":
                merged = [edges[i][0], edges[j][1], edges[i][2] or edges[j][2]]
                edges = [e for k, e in enumerate(edges) if k not in (i, j)] + [merged]
                changed = True
                break

    formatted = [f"{src}/{dst}" + ("*" if hit else "") for src, dst, hit in edges]
    formatted.sort()
    return " ".join(formatted)


def load_gva(path: Path) -> dict:
    text = path.read_text()
    return json.loads(text)


def engine_label(data: dict, path: Path) -> str:
    return data.get("summary", {}).get("engine") or path.stem


def index_moves(data: dict) -> dict[tuple[int, int], dict]:
    idx = {}
    for game in data.get("games", []):
        gn = game["game_number"]
        for move in game.get("moves", []):
            idx[(gn, move["move_number"])] = move
    return idx


def best_move(entry: dict) -> str | None:
    opts = entry.get("move_options") or []
    return normalize_move(opts[0]["move"]) if opts else None


def compare(files: list[Path]) -> dict:
    datasets = [load_gva(p) for p in files]
    labels = [engine_label(d, p) for d, p in zip(datasets, files)]
    if len(set(labels)) != len(labels):
        labels = [f"{lbl}[{i}]" for i, lbl in enumerate(labels)]
    indexes = [index_moves(d) for d in datasets]

    key_sets = [set(idx) for idx in indexes]
    common_keys = set.intersection(*key_sets)
    per_file_only = {}
    for lbl, keys in zip(labels, key_sets):
        other_keys = set.union(*[k for k in key_sets if k is not keys]) if len(key_sets) > 1 else set()
        per_file_only[lbl] = sorted(keys - other_keys)

    summary_rows = []
    for lbl, d in zip(labels, datasets):
        s = d.get("summary", {})
        summary_rows.append({
            "engine": lbl,
            "player1_pr": s.get("player1_pr"),
            "player2_pr": s.get("player2_pr"),
            "player1_total_decisions": s.get("player1_total_decisions"),
            "player2_total_decisions": s.get("player2_total_decisions"),
            "player1_total_error": s.get("player1_total_error"),
            "player2_total_error": s.get("player2_total_error"),
            "player1_total_luck": s.get("player1_total_luck"),
            "player2_total_luck": s.get("player2_total_luck"),
        })

    checker_keys = sorted(
        k for k in common_keys
        if all(idx[k].get("kind") == "checker" for idx in indexes)
    )

    unmatched_played_move = []
    all_votes = {}
    for k in checker_keys:
        entries = [idx[k] for idx in indexes]
        played_norm = [normalize_move(e.get("player_move")) for e in entries]
        if len(set(played_norm)) != 1:
            unmatched_played_move.append({
                "key": k,
                "played": dict(zip(labels, [e.get("player_move") for e in entries])),
            })
            continue
        votes = [best_move(e) for e in entries]
        all_votes[k] = {
            "votes": dict(zip(labels, votes)),
            "counted": dict(zip(labels, [e.get("counted") for e in entries])),
            "lost_equity": dict(zip(labels, [e.get("lost_equity") for e in entries])),
        }

    matrix_all = {a: {b: [0, 0] for b in labels} for a in labels}
    matrix_counted = {a: {b: [0, 0] for b in labels} for a in labels}
    for k, rec in all_votes.items():
        for a, b in itertools.combinations(labels, 2):
            va, vb = rec["votes"][a], rec["votes"][b]
            agree = 1 if va == vb else 0
            matrix_all[a][b][0] += agree
            matrix_all[a][b][1] += 1
            matrix_all[b][a][0] += agree
            matrix_all[b][a][1] += 1
            if rec["counted"][a] and rec["counted"][b]:
                matrix_counted[a][b][0] += agree
                matrix_counted[a][b][1] += 1
                matrix_counted[b][a][0] += agree
                matrix_counted[b][a][1] += 1

    def to_pct_matrix(m):
        out = {}
        for a in labels:
            out[a] = {}
            for b in labels:
                if a == b:
                    out[a][b] = None
                    continue
                agree, total = m[a][b]
                out[a][b] = {"agree": agree, "total": total,
                              "pct": round(100 * agree / total, 1) if total else None}
        return out

    disagreements = []
    for k, rec in all_votes.items():
        distinct = set(rec["votes"].values())
        if len(distinct) > 1:
            max_lost = max((v for v in rec["lost_equity"].values() if v is not None), default=0.0)
            disagreements.append({
                "game": k[0], "move": k[1],
                "votes": rec["votes"],
                "lost_equity": rec["lost_equity"],
                "max_lost_equity": max_lost,
            })
    disagreements.sort(key=lambda r: -r["max_lost_equity"])

    cube_dec_keys = sorted(
        k for k in common_keys
        if all(idx[k].get("kind") == "cube_decision" for idx in indexes)
    )
    cube_resp_keys = sorted(
        k for k in common_keys
        if all(idx[k].get("kind") == "cube_response" for idx in indexes)
    )

    def cube_agreement(keys, field):
        rows = []
        splits = []
        for k in keys:
            entries = [idx[k] for idx in indexes]
            picks = dict(zip(labels, [e.get(field) for e in entries]))
            rows.append(picks)
            if len(set(picks.values())) > 1:
                splits.append({"game": k[0], "move": k[1], "picks": picks})
        return rows, splits

    cube_dec_rows, cube_dec_splits = cube_agreement(cube_dec_keys, "optimal_action")
    cube_resp_rows, cube_resp_splits = cube_agreement(cube_resp_keys, "optimal_response")

    return {
        "engines": labels,
        "summary": summary_rows,
        "coverage": {
            "common_move_keys": len(common_keys),
            "unmatched_played_move_count": len(unmatched_played_move),
            "unmatched_played_move_examples": unmatched_played_move[:10],
            "keys_only_in": per_file_only,
        },
        "checker_play": {
            "compared": len(all_votes),
            "agreement_matrix_all": to_pct_matrix(matrix_all),
            "agreement_matrix_counted_only": to_pct_matrix(matrix_counted),
            "disagreements": disagreements,
        },
        "cube_decisions": {
            "compared": len(cube_dec_keys),
            "splits": cube_dec_splits,
        },
        "cube_responses": {
            "compared": len(cube_resp_keys),
            "splits": cube_resp_splits,
        },
    }


def print_report(report: dict) -> None:
    labels = report["engines"]
    print(f"Engines compared: {', '.join(labels)}\n")

    print("Summary:")
    header = ["engine", "p1_pr", "p2_pr", "p1_dec", "p2_dec", "p1_err", "p2_err", "p1_luck", "p2_luck"]
    print("  " + " ".join(f"{h:>9}" for h in header))
    for row in report["summary"]:
        vals = [row["engine"], row["player1_pr"], row["player2_pr"],
                row["player1_total_decisions"], row["player2_total_decisions"],
                row["player1_total_error"], row["player2_total_error"],
                row["player1_total_luck"], row["player2_total_luck"]]
        print("  " + " ".join(f"{v!s:>9}" for v in vals))

    cov = report["coverage"]
    print(f"\nCoverage: {cov['common_move_keys']} move keys common to all files; "
          f"{cov['unmatched_played_move_count']} had non-matching played moves at a shared key.")
    for lbl, keys in cov["keys_only_in"].items():
        if keys:
            print(f"  {len(keys)} keys present only in {lbl} (e.g. {keys[:5]})")

    cp = report["checker_play"]
    print(f"\nChecker play compared: {cp['compared']}")
    print("Best-move agreement matrix, % (all matched entries):")
    for a in labels:
        print(f"  {a:>12}: " + "  ".join(
            f"{b}={cp['agreement_matrix_all'][a][b]['pct']}%" if a != b else f"{b}=--"
            for b in labels
        ))
    print("Best-move agreement matrix, % (counted=true on both sides only):")
    for a in labels:
        print(f"  {a:>12}: " + "  ".join(
            f"{b}={cp['agreement_matrix_counted_only'][a][b]['pct']}%" if a != b else f"{b}=--"
            for b in labels
        ))

    print(f"\nNon-unanimous best-move picks: {len(cp['disagreements'])} (top 15 by max lost_equity)")
    for row in cp["disagreements"][:15]:
        votes = ", ".join(f"{e}='{m}'" for e, m in row["votes"].items())
        print(f"  g{row['game']}m{row['move']}: {votes}  (max_lost_equity={row['max_lost_equity']:.4f})")

    cd = report["cube_decisions"]
    print(f"\nCube decisions (doubler) compared: {cd['compared']}; non-unanimous: {len(cd['splits'])}")
    for row in cd["splits"]:
        print(f"  g{row['game']}m{row['move']}: {row['picks']}")

    cr = report["cube_responses"]
    print(f"\nCube responses (taker) compared: {cr['compared']}; non-unanimous: {len(cr['splits'])}")
    for row in cr["splits"]:
        print(f"  g{row['game']}m{row['move']}: {row['picks']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gva_files", nargs="+", type=Path, help="Two or more .gva files to compare")
    parser.add_argument("--json", type=Path, default=None, metavar="FILE",
                         help="Also write the full comparison report as JSON to FILE")
    args = parser.parse_args()

    if len(args.gva_files) < 2:
        print("Need at least two .gva files to compare.")
        sys.exit(1)

    report = compare(args.gva_files)
    print_report(report)

    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"\nJSON report written to {args.json}")


if __name__ == "__main__":
    main()
