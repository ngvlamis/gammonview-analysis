# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Time analyze_ogxm over a corpus and histogram which tier each decision landed on."""
import argparse, collections, json, resource, time
from pathlib import Path
from gvanalysis.loader import load_ogxm
from gvanalysis.match import analyze_ogxm


def tiers(res):
    """Count decisions by the eval level of the engine's best option."""
    hist = collections.Counter()
    n = 0
    for g in res.get("games", []):
        for m in g.get("moves", []):
            kind = m.get("kind")
            opts = m.get("move_options") or m.get("cube_options") or []
            lvl = None
            for o in opts:
                if isinstance(o, dict) and o.get("eval_level"):
                    lvl = o["eval_level"]
                    break
            if lvl is None:
                lvl = m.get("eval_level") or "n/a"
            hist[f"{kind}:{lvl}"] += 1
            n += 1
    return n, dict(sorted(hist.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--preset", default="fast")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--tag", default="")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default="/tmp/gvbench/results.jsonl")
    a = ap.parse_args()
    docs = [load_ogxm(Path(f)) for f in a.files]        # load outside the timer
    best = None
    for _ in range(a.repeat):
        t0 = time.perf_counter()
        results = [analyze_ogxm(d, preset=a.preset, jobs=a.jobs, threads=a.threads,
                                quiet=True, show_progress=False) for d in docs]
        el = time.perf_counter() - t0
        best = el if best is None else min(best, el)
    ndec = 0
    hist = collections.Counter()
    for r in results:
        n, h = tiers(r)
        ndec += n
        hist.update(h)
    prs = [[r["summary"].get("player1_pr"), r["summary"].get("player2_pr"),
            r["summary"].get("player1_total_error"), r["summary"].get("player2_total_error")]
           for r in results]
    rec = {"tag": a.tag, "preset": a.preset, "jobs": a.jobs, "threads": a.threads,
           "files": len(docs), "decisions": ndec, "sec": round(best, 2),
           "ms_per_dec": round(best * 1000 / max(ndec, 1), 1),
           "self_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6),
           "child_rss_mb": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1e6),
           "pr": prs, "tiers": dict(sorted(hist.items()))}
    print(json.dumps(rec))
    with open(a.out, "a") as f:
        f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()
