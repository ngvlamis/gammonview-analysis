# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Analyze a Jellyfish/GNUbg .mat match file and compute PR using Sage's evaluation.

Sage evaluates each checker play and cube decision at the configured level.
PR = sum(equity errors) / decision count * 500

Decision filters (XG-compatible):
  Checker:   skip if < 2 legal moves, or the top ten moves' equities span
             less than 1e-4 (an already-decided position).
  Doubler:   skip if trivial AND played correctly.
  Responder: skip if DT and DP within 0.001.

Usage:
    uv run python gvan_match.py match.mat                       # writes match.gva
    uv run python gvan_match.py match.mat -o out.gva
    uv run python gvan_match.py match.mat --gvab                # match.gvab instead
    uv run python gvan_match.py match.mat --gvab -o out.gvab    # names the .gvab
    uv run python gvan_match.py match.mat --link                # also print a gammonview.com link
    uv run python gvan_match.py match.mat --browser             # open that link in the browser
    uv run python gvan_match.py match.mat --preset fast
    uv run python gvan_match.py match.mat --preset world_class --threads 4
"""

from __future__ import annotations

import argparse
import faulthandler
import gzip
import json
import math
import os
import sys
import time
from importlib import metadata as _metadata
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

faulthandler.enable()

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
for _p in (_PROJECT_ROOT / "python", _PROJECT_ROOT / "build"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

if sys.platform == "win32":
    import os as _os
    _cuda_x64 = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.1\bin\x64"
    if _os.path.isdir(_cuda_x64):
        _os.add_dll_directory(_cuda_x64)
    if (_PROJECT_ROOT / "build").is_dir():
        _os.add_dll_directory(str(_PROJECT_ROOT / "build"))

from bgsage import BgBotAnalyzer

from .loader import load_ogxm
from .ogxm_reconstructor import reconstruct_decisions_from_ogxm
from .game_eval import (
    evaluate_game, _EvalCtx, _eval_cube_decision, _eval_checker_decision,
    _collate_game, _LEVEL_DISPLAY,
)
from gvformat import to_ogxm_json, write_gvab, read_gvab, encode_match, append_analysis
from .share import GAMMONVIEW_BASE_URL, MAX_SHARE_LENGTH, SHARE_PARAM
from .presets import (
    resolve_preset, write_template, DEFAULT_PRESET, PRESETS,
    PROJECT_PRESETS_FILE, GLOBAL_PRESETS_FILE,
)
from .progress import ProgressBar

# The ``analysis_info.model_id`` stamped on every analysis this package writes:
# the engine, its exact version, and the fact that gammonview's analysis layer
# shaped the answer. "bgsage" alone would overstate the engine's share of it --
# `checker_eval`'s screening chooses which moves get N-ply numbers and the
# preset tiers choose the depth, so two model_ids reading "bgsage" can hold
# different numbers from the same engine.
#
# Deliberately carries no gammonview version. Everything else in
# ``analysis_info`` is deterministic -- ``timestamp`` is the *match's*, not
# ``now()``, and ``duration_ms`` is never set -- which is precisely what lets
# ``tests/golden/`` pin these bytes. A hatch-vcs version would move daily in a
# dev checkout (the ``.dYYYYMMDD`` local segment), and tagging a release would
# redden the suite the tag was cut from, then leave the regen commit past the
# tag. bgsage's version costs nothing by comparison: it moves exactly when an
# engine upgrade invalidates the goldens anyway.
MODEL_NAME = "gv-bgsage"


def model_id() -> str:
    """``gv-bgsage/<bgsage version>`` -- the producer of this analysis.

    Falls back to the bare name if bgsage's distribution metadata is missing
    (a source tree on ``sys.path`` rather than an installed wheel); the label
    stays truthful, it just stops naming the build.
    """
    try:
        return f"{MODEL_NAME}/{_metadata.version('bgsage')}"
    except Exception:
        return MODEL_NAME


#: Historical name; `match.py` owned the bar before `position.py` needed one.
_ProgressBar = ProgressBar


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _pr_for_json(pr: float) -> float | None:
    return None if math.isnan(pr) else round(pr, 3)


def _fmt_pr(pr: float) -> str:
    return f"{pr:.3f}" if not math.isnan(pr) else "nan"


def _open_in_browser(url: str) -> bool:
    """Open `url` in the default browser; return True on a best-effort success.

    A share URL can be tens of thousands of chars (the whole match rides in the
    fragment). That's fine as a single argument to the platform opener, but a
    near-cap match could approach the OS argument-length limit, so for very long
    URLs we hand the browser a tiny local HTML file that redirects to the real
    URL instead -- the fragment survives the redirect and no argv limit applies.
    """
    target = url
    if len(url) > 100_000:
        import html
        import tempfile
        fd, path = tempfile.mkstemp(prefix="gvlink-", suffix=".html")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write('<!doctype html><meta http-equiv="refresh" '
                    f'content="0;url={html.escape(url, quote=True)}">')
        target = Path(path).as_uri()
    try:
        if sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", target], check=True)
        elif sys.platform == "win32":
            os.startfile(target)   # type: ignore[attr-defined]
        else:
            import subprocess
            subprocess.run(["xdg-open", target], check=True)
        return True
    except Exception:              # noqa: BLE001 -- fall back to the stdlib opener
        import webbrowser
        return webbrowser.open(target)


def _gz_path(path: Path, compress: bool) -> Path:
    """Append `.gz` to an explicit -o path under --compress, unless it already
    ends in `.gz`."""
    if compress and path.suffix != ".gz":
        return path.with_name(path.name + ".gz")
    return path


# ---------------------------------------------------------------------------
# Process-pool workers (Axis B: decision-level parallelism)
#
# bgsage's evaluation is a C++ extension that holds the GIL, so Python threads
# over decisions don't scale -- separate processes (separate GILs) do. These
# are module-level (not closures) so they pickle under the `spawn` start method
# macOS/Windows use. Each worker builds its own analyzers ONCE in an
# initializer (analyzers can't cross the process boundary); only plain dicts
# (a decision in, a `_DecResult` out) are pickled per task.
# ---------------------------------------------------------------------------
_WORKER: dict = {}


def _worker_init(base_level, mid_level_checker, mid_level_cube, level,
                 close_threshold, all_moves, count_illegal, level_display,
                 engine_threads, verbose) -> None:
    built: dict = {}

    def mk(lvl):
        # Memoized so two tiers naming the same level share one analyzer
        # instead of building (and holding the weights for) it twice.
        if not lvl:
            return None
        if lvl not in built:
            built[lvl] = BgBotAnalyzer(eval_level=lvl, cubeful=True,
                                       parallel_threads=engine_threads)
        return built[lvl]

    analyzer = mk(level)
    base_analyzer = mk(base_level)
    mid_analyzer_checker = mk(mid_level_checker)
    mid_analyzer_cube = mk(mid_level_cube)
    luck_analyzer = mk("1ply")
    _WORKER["ctx_kw"] = dict(
        analyzer=analyzer, base_analyzer=base_analyzer,
        mid_analyzer_checker=mid_analyzer_checker, mid_analyzer_cube=mid_analyzer_cube,
        luck_analyzer=luck_analyzer, close_threshold=close_threshold,
        verbose=verbose, all_moves=all_moves, count_illegal=count_illegal,
        level_display=level_display,
    )


def _worker_run(item):
    gi, di, game_number, dec = item
    # `game` is only read for verbose diagnostics; a minimal stub suffices.
    ctx = _EvalCtx(game={"game_number": game_number}, **_WORKER["ctx_kw"])
    if dec["kind"] == "cube":
        res = _eval_cube_decision(dec, ctx)
    else:
        res = _eval_checker_decision(dec, ctx)
    return gi, di, res


def analyze_ogxm(
    ogxm: dict,
    preset: str | None = None,
    threads: int = 0,
    verbose: bool = False,
    all_moves: bool = False,
    count_illegal: bool = False,
    quiet: bool = False,
    show_progress: bool = True,
    on_progress: "Callable[[int, int], None] | None" = None,
    jobs: int = 1,
) -> dict:
    """Analyze an OGXM match dict; return the internal analyzed ``data`` dict
    (``summary`` + ``games[].moves[]``) that ``gvformat.to_ogxm_json`` consumes.

    This is the single analysis core for every input: ``.mat`` (converted to
    OGXM by ``mat_to_ogxm`` first), ``.gva``/``.ogxm`` JSON, and ``.gvab``
    binary all become an OGXM dict, whose plies are turned back into engine
    decisions by ``ogxm_reconstructor``. Any analysis the OGXM already carries
    is untouched here -- callers append this result with
    ``gvformat.append_analysis`` so existing blocks are preserved.

    ``player1``/``player2`` in the returned summary are the OGXM's
    white/black (OGXM carries no other player ordering).

    `preset` names an XG-style two-pass scheme defined in presets.yaml: a cheap
    first pass screens every decision and a stronger second pass runs on
    disagreement. Single-pass presets judge everything at the first-pass level.
    None selects the configured default preset.

    Raises ValueError for unknown presets. Pass quiet=True to suppress the
    human-readable table/summary; pass show_progress=False to suppress the
    stderr progress bar (independent of quiet, so a caller can show the bar
    while hiding the table).

    `on_progress(done, total)` is the programmatic counterpart of the stderr
    bar, for callers that draw progress somewhere else entirely (a server
    reporting to a browser). It fires once with (0, total) as soon as the total
    is known -- before the first, slow engine build -- then once per decision.
    It is independent of show_progress and of stderr being a TTY.

    `jobs` controls decision-level parallelism (Axis B): 1 (default) runs the
    original serial path unchanged; a positive value is that many worker
    PROCESSES; 0 means auto. Each decision (screen -> escalate-on-error ->
    luck) is one unit of work dispatched to a `ProcessPoolExecutor`. Processes,
    not threads, because bgsage's evaluation is a C++ extension that holds the
    GIL -- threads plateau at ~2.8x, separate processes scale past it.

    `threads` is bgsage's own internal parallelism (Axis A) *within* a worker.
    In serial mode it behaves as before. In parallel mode it is the engine
    threads per worker; auto picks a hybrid (~3 internal threads, ~cpu/3
    workers) that fills the machine and lets a single heavy decision -- which
    Axis B cannot split -- still be sped up internally.

    Output is byte-identical to the serial path regardless of `jobs`
    (parallelism only changes wall-clock time, never the result: collation is a
    single deterministic function shared by both paths, and bgsage's rollout
    seed is fixed).
    """
    p = resolve_preset(preset)
    if p.second_pass is None:      # single pass
        base_level_r = None
        level = p.first_pass
        mid_level_checker = mid_level_cube = None
        close_threshold = None
    else:                          # two pass: 1st = screen, 2nd = authoritative
        base_level_r = p.first_pass
        level = p.second_pass
        # Optional 3rd tier, on a close decision. Named per kind because the
        # same level costs very differently on a move list than on a single
        # cube position, so a preset may want one and not the other.
        mid_level_checker = p.mid_pass_checker
        mid_level_cube = p.mid_pass_cube
        close_threshold = p.close_threshold

    ml = int(ogxm.get("match_length", 0) or 0)
    p1 = ogxm.get("player_white") or "White"
    p2 = ogxm.get("player_black") or "Black"
    crawford_rule = bool(ogxm.get("crawford"))
    jacoby_rule = bool(ogxm.get("jacoby"))
    beaver_rule = bool(ogxm.get("beaver"))
    cube_limit = ogxm.get("cube_limit") or 0
    event = ogxm.get("event")
    site = ogxm.get("site")
    timestamp = ogxm.get("timestamp") or 0

    # Reconstruct every game's decisions from the OGXM plies up front (no engine
    # calls, cheap) so the total decision count is known before analysis begins
    # -> exact progress bar, and a caller watching on_progress learns the job
    # size immediately (before the slow analyzer build).
    recons = reconstruct_decisions_from_ogxm(ogxm)
    game_metas = [
        {"game_number": gi + 1, "score1_start": recon["sw"], "score2_start": recon["sb"]}
        for gi, recon in enumerate(recons)
    ]
    total_decisions = sum(len(recon["decisions"]) for recon in recons)

    if not quiet:
        print(f"Match: {p1} vs {p2}")
        print(f"Match length: {ml if ml > 0 else 'money game'}")
        print(f"Games: {len(recons)}")
        if base_level_r and (mid_level_checker or mid_level_cube):
            if mid_level_checker == mid_level_cube:
                mid_desc = mid_level_checker
            else:
                mid_desc = " / ".join(
                    f"{kind} {lvl or 'none'}" for kind, lvl in
                    (("checker", mid_level_checker), ("cube", mid_level_cube)))
            print(f"Preset: {p.display} ({base_level_r} screen -> {mid_desc} on close "
                  f"(<= {close_threshold}) -> {level} on error)")
        elif base_level_r:
            print(f"Preset: {p.display} ({base_level_r} screen, upgrade to {level})")
        else:
            print(f"Preset: {p.display} ({level})")
        print()

    if on_progress is not None:
        on_progress(0, total_decisions)

    # Two axes of parallelism (see docstring). Axis B = `n_jobs` worker
    # processes over decisions; Axis A = `worker_threads` bgsage-internal
    # threads inside each. Serial (jobs==1) keeps `threads` as the engine's
    # internal parallelism, exactly as before.
    #
    # In parallel mode auto DELIBERATELY OVERSUBSCRIBES, to ~3x the core count.
    # Sizing the product to ~cpu (the obvious choice, and what this did before)
    # leaves the machine idle for a large share of the run: engine threads
    # parallelise *within* one evaluation, and a 1-ply evaluation is too small to
    # use them -- measured 0.98-1.00x from 1 to 24 threads, against 0.34x for a
    # 2-ply play. The per-roll luck sweep is all 1-ply and ~29% of a `fast`
    # analysis, so during it each worker really occupies one core. Overcommitting
    # fills that gap and costs little in the thread-hungry passes.
    #
    # Measured over the 12 sample matches on a 24-core box (16P+8E), best of two
    # passes, against the previous 8x3 default: 12x6 is 1.22x on `fast` and 1.12x
    # on `deep`; the whole threads=6 family lands within ~3% of that, and threads
    # is by far the stronger lever (every threads=6 combo beat every threads=4
    # one). Both levers must move together -- 4x6, which `cpu // worker_threads`
    # would have produced from threads=6 alone, is 0.89x, i.e. a regression.
    # Only the 24-core point is measured; the ratios below reproduce it and
    # degrade to roughly the old commitment on small machines.
    cpu = os.cpu_count() or 1
    if jobs == 1:
        worker_threads = threads
        n_jobs = 1
    else:
        worker_threads = threads if threads > 0 else max(2, cpu // 4)
        n_jobs = jobs if jobs > 0 else max(1, cpu // 2)

    # Serial builds analyzers once here; parallel workers build their own (they
    # can't cross the process boundary), so skip the main-process build then.
    analyzer = base_analyzer = luck_analyzer = None
    mid_analyzer_checker = mid_analyzer_cube = None
    if jobs == 1:
        # One analyzer per distinct level, shared across every tier that names
        # it. Tiers overlap by design -- a mid tier may name the second-pass
        # level to mean "borderline as well as wrong earns the top tier" -- and
        # holding two copies of the same weights would be pure waste.
        _built: dict[str, "BgBotAnalyzer"] = {}

        def _mk(lvl, what):
            if not lvl:
                return None
            if lvl not in _built:
                if not quiet:
                    print(f"Building {what} ({lvl})...")
                _built[lvl] = BgBotAnalyzer(eval_level=lvl, cubeful=True,
                                            parallel_threads=worker_threads)
            return _built[lvl]

        # Deepest first, so a level shared with a shallower tier is announced
        # once under the name of the tier that actually needs it.
        analyzer = _mk(level, "analyzer")
        base_analyzer = _mk(base_level_r, "base analyzer")
        mid_analyzer_checker = _mk(mid_level_checker, "mid analyzer")
        mid_analyzer_cube = _mk(mid_level_cube, "mid analyzer")
        luck_analyzer = _mk("1ply", "luck analyzer")
    elif not quiet:
        print(f"Analyzing {total_decisions} decisions across {n_jobs} worker "
              f"process(es) x {worker_threads or 'auto'} engine threads...")

    if not quiet:
        print()
        print(
            f"{'Game':>4}  {'Score':>7}  "
            f"{p1[:12]:>12}  {'PR':>6}  {'# dec (cube)':>12}  {'Luck':>7}  "
            f"{p2[:12]:>12}  {'PR':>6}  {'# dec (cube)':>12}  {'Luck':>7}"
        )
        print("-" * 101)

    # Bar on stderr; stays a no-op when show_progress is off, verbose
    # (diagnostics would clobber it), or stderr is not a TTY.
    bar = _ProgressBar(0 if (not show_progress or verbose) else total_decisions)

    # One callable feeds both sinks. The bar keeps its own count (and may be a
    # no-op), so the callback count is tracked separately here.
    advance = bar.advance
    if on_progress is not None:
        done = 0

        def advance(k: int = 1) -> None:
            nonlocal done
            done += k
            bar.advance(k)
            on_progress(done, total_decisions)

    game_results = []

    def _print_game_row(r: dict) -> None:
        score_str = f"{r['score1_start']}-{r['score2_start']}"
        p1_pr_str = f"{r['p1_pr']:.2f}" if not math.isnan(r["p1_pr"]) else "nan"
        p2_pr_str = f"{r['p2_pr']:.2f}" if not math.isnan(r["p2_pr"]) else "nan"
        p1_dec_str = f"{r['p1_dec']} ({r['p1_cube_dec']})"
        p2_dec_str = f"{r['p2_dec']} ({r['p2_cube_dec']})"
        p1_luck_str = f"{r['p1_luck']:+.3f}" if r["p1_luck_count"] else "n/a"
        p2_luck_str = f"{r['p2_luck']:+.3f}" if r["p2_luck_count"] else "n/a"
        bar.clear()
        print(
            f"{r['game_number']:>4}  {score_str:>7}  "
            f"{'':12}  {p1_pr_str:>6}  {p1_dec_str:>12}  {p1_luck_str:>7}  "
            f"{'':12}  {p2_pr_str:>6}  {p2_dec_str:>12}  {p2_luck_str:>7}"
        )
        bar.redraw()

    if jobs == 1:
        for gi, recon in enumerate(recons):
            r = evaluate_game(game_metas[gi], analyzer, recon,
                              is_crawford=recon["is_crawford"],
                              verbose=verbose,
                              base_analyzer=base_analyzer, luck_analyzer=luck_analyzer,
                              mid_analyzer_checker=mid_analyzer_checker,
                              mid_analyzer_cube=mid_analyzer_cube,
                              close_threshold=close_threshold,
                              level=level, all_moves=all_moves,
                              count_illegal=count_illegal,
                              progress=advance)
            game_results.append(r)
            if not quiet:
                _print_game_row(r)
    else:
        # Flatten every game's decisions into one work list so the pool
        # load-balances across the whole match, not just within a game. Each
        # item is fully picklable (plain dict + ints); analyzers live in the
        # workers, built once by _worker_init.
        work = []
        for gi, recon in enumerate(recons):
            game_number = game_metas[gi]["game_number"]
            for di, dec in enumerate(recon["decisions"]):
                work.append((gi, di, game_number, dec))

        init_args = (
            base_level_r, mid_level_checker, mid_level_cube, level, close_threshold, all_moves,
            count_illegal, _LEVEL_DISPLAY.get(level, level), worker_threads, verbose,
        )
        buckets: "list[list[tuple[int, object]]]" = [[] for _ in recons]
        with ProcessPoolExecutor(max_workers=n_jobs, initializer=_worker_init,
                                 initargs=init_args) as pool:
            futures = [pool.submit(_worker_run, item) for item in work]
            for fut in as_completed(futures):
                gi, di, res = fut.result()
                buckets[gi].append((di, res))
                advance(1)

        for gi, recon in enumerate(recons):
            ordered = [res for _di, res in sorted(buckets[gi], key=lambda t: t[0])]
            r = _collate_game(ordered, game_metas[gi], recon["game_result"],
                              recon["is_crawford"])
            game_results.append(r)

        if not quiet:
            for r in game_results:
                _print_game_row(r)

    bar.close()

    sum_p1_err = 0.0
    sum_p1_dec = 0
    sum_p1_cube_dec = 0
    sum_p1_luck = 0.0
    sum_p1_luck_mwc = 0.0
    sum_p1_luck_count = 0
    sum_p2_err = 0.0
    sum_p2_dec = 0
    sum_p2_cube_dec = 0
    sum_p2_luck = 0.0
    sum_p2_luck_mwc = 0.0
    sum_p2_luck_count = 0
    sum_illegal_moves = 0
    for r in game_results:
        sum_p1_err += r["p1_err"]
        sum_p1_dec += r["p1_dec"]
        sum_p1_cube_dec += r["p1_cube_dec"]
        sum_p1_luck += r["p1_luck"]
        sum_p1_luck_mwc += r["p1_luck_mwc"]
        sum_p1_luck_count += r["p1_luck_count"]
        sum_p2_err += r["p2_err"]
        sum_p2_dec += r["p2_dec"]
        sum_p2_cube_dec += r["p2_cube_dec"]
        sum_p2_luck += r["p2_luck"]
        sum_p2_luck_mwc += r["p2_luck_mwc"]
        sum_p2_luck_count += r["p2_luck_count"]
        sum_illegal_moves += r["illegal_moves"]

    p1_pr = (sum_p1_err / sum_p1_dec * 500.0) if sum_p1_dec > 0 else float("nan")
    p2_pr = (sum_p2_err / sum_p2_dec * 500.0) if sum_p2_dec > 0 else float("nan")

    if not quiet:
        p1_luck_str = f"{sum_p1_luck:+.4f}" if sum_p1_luck_count else "n/a"
        p2_luck_str = f"{sum_p2_luck:+.4f}" if sum_p2_luck_count else "n/a"
        print()
        print("=" * 60)
        print(f"{p1:<20} PR:  {_fmt_pr(p1_pr)}  ({sum_p1_dec} dec,  {sum_p1_cube_dec} cube,  err={sum_p1_err:.4f},  luck={p1_luck_str})")
        print(f"{p2:<20} PR:  {_fmt_pr(p2_pr)}  ({sum_p2_dec} dec,  {sum_p2_cube_dec} cube,  err={sum_p2_err:.4f},  luck={p2_luck_str})")
        if sum_illegal_moves:
            print(f"\nWarning: {sum_illegal_moves} checker move(s) could not be matched to a legal position (illegal move or transcription error).")

    return {
        "summary": {
            "engine": model_id(),
            "player1": p1,
            "player2": p2,
            "match_length": ml if ml > 0 else None,
            "crawford_rule": crawford_rule,
            "jacoby_rule": jacoby_rule,
            "beaver_rule": beaver_rule,
            "cube_limit": cube_limit,
            "event": event or None,
            "site": site or None,
            # Unix seconds carried straight from the OGXM (the source's date/time
            # strings are already folded into it); to_ogxm_json uses this
            # directly rather than re-parsing a header.
            "timestamp": timestamp,
            "preset": p.key,
            "first_pass_level": p.first_pass,
            # Kept scalar for the checker tier (its meaning before the tier
            # became per-kind, so every preset naming one level still reports
            # what it always did); the cube tier sits alongside it.
            "mid_pass_level": mid_level_checker,
            "mid_pass_cube_level": mid_level_cube,
            "close_threshold": close_threshold,
            "second_pass_level": p.second_pass,
            "eval_level": level,   # back-compat: authoritative level for existing .gva consumers
            # Level the luck-analyzer runs at (always 1-ply today; will become
            # configurable when a "luck eval level" option is added).
            "luck_eval_level": "1ply",
            "player1_pr": _pr_for_json(p1_pr),
            "player2_pr": _pr_for_json(p2_pr),
            "player1_total_decisions": sum_p1_dec,
            "player2_total_decisions": sum_p2_dec,
            "player1_cube_decisions": sum_p1_cube_dec,
            "player2_cube_decisions": sum_p2_cube_dec,
            "player1_total_error": round(sum_p1_err, 4),
            "player2_total_error": round(sum_p2_err, 4),
            "player1_total_luck": round(sum_p1_luck, 4),
            **({"player1_total_luck_mwc": round(sum_p1_luck_mwc, 6)} if ml > 0 else {}),
            "player1_luck_rolls": sum_p1_luck_count,
            "player2_total_luck": round(sum_p2_luck, 4),
            **({"player2_total_luck_mwc": round(sum_p2_luck_mwc, 6)} if ml > 0 else {}),
            "player2_luck_rolls": sum_p2_luck_count,
            "illegal_moves": sum_illegal_moves,
        },
        "games": [
            {
                "game_number": r["game_number"],
                "score_start": {"player1": r["score1_start"], "player2": r["score2_start"]},
                "is_crawford": r["is_crawford"],
                "result": r["game_result"],
                # Equity<->MWC conversion is compute-on-read (gvformat.met);
                # luck_mwc here is a summary-only aggregate computed the same
                # way, not a stored per-decision anchor.
                "player1_pr": _pr_for_json(r["p1_pr"]),
                "player1_decisions": r["p1_dec"],
                "player1_luck": round(r["p1_luck"], 4),
                **({"player1_luck_mwc": round(r["p1_luck_mwc"], 6)} if ml > 0 else {}),
                "player1_luck_rolls": r["p1_luck_count"],
                "player2_pr": _pr_for_json(r["p2_pr"]),
                "player2_decisions": r["p2_dec"],
                "player2_luck": round(r["p2_luck"], 4),
                **({"player2_luck_mwc": round(r["p2_luck_mwc"], 6)} if ml > 0 else {}),
                "player2_luck_rolls": r["p2_luck_count"],
                "moves": r["moves"],
            }
            for r in game_results
        ],
    }


def analyze_mat(input_path: "Path | str", **kwargs) -> dict:
    """Back-compat convenience: load any supported input (``.mat`` / ``.gva`` /
    ``.ogxm`` / ``.gvab``, optionally ``.gz``) and return the internal analyzed
    ``data`` dict (``analyze_ogxm``'s output).

    Named for the historical ``.mat``-only entry point and still accepts a
    ``.mat``; the input is converted to OGXM (``load_ogxm``) before analysis, so
    every format now works. To preserve an OGXM's existing analysis blocks in
    the output, use ``analyze_file`` (or ``append_analysis`` on the result)
    instead -- this returns only the fresh analysis.
    """
    return analyze_ogxm(load_ogxm(input_path), **kwargs)


def analyze_file(input_path: "Path | str", **kwargs) -> dict:
    """Load any supported input, analyze it, and return the OGXM dict with this
    analysis **appended** (existing analysis blocks preserved).

    The full server/CLI path: ``load_ogxm`` -> ``analyze_ogxm`` ->
    ``to_ogxm_json`` -> ``append_analysis``. When the input carries no analysis
    the result is a single-analysis OGXM identical to analyzing it directly;
    when it already carries one or more, ours becomes an additional block.
    """
    base = load_ogxm(input_path)
    data = analyze_ogxm(base, **kwargs)
    return append_analysis(base, to_ogxm_json(data))


def main() -> None:
    t0 = time.perf_counter()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_file", nargs="?", type=Path,
        help="Match file to analyze: .mat, .gva/.ogxm (OGXM JSON), or .gvab "
             "(OGXM binary), optionally .gz. An OGXM input keeps any analysis "
             "it already carries; ours is appended.",
    )
    parser.add_argument(
        "-o", "--output", dest="output_file", type=Path, default=None,
        help="Output file path (default: <input>.gva beside the input, "
             "or <input>.gvab with --gvab).",
    )
    parser.add_argument(
        "--preset", default=None,
        help="Analysis preset ("
             f"{'|'.join(PRESETS)}; built-ins overridable in presets.yaml). "
             f"Default: {DEFAULT_PRESET}",
    )
    parser.add_argument(
        "--init-presets", action="store_true", dest="init_presets",
        help="Write a starter presets.yaml (project-local) for custom presets and exit",
    )
    parser.add_argument(
        "--global", action="store_true", dest="global_presets",
        help=f"With --init-presets, write to the global {GLOBAL_PRESETS_FILE} instead",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="With --init-presets, overwrite an existing presets.yaml",
    )
    parser.add_argument(
        "--threads", type=int, default=0,
        help="bgsage-internal engine threads (Axis A), per worker in parallel "
             "mode (default: 0 = auto)",
    )
    parser.add_argument(
        "--jobs", type=int, default=0,
        help="Decision-level parallel worker PROCESSES (Axis B; default: 0 = "
             "auto ~cpu/2). Each checker/cube decision is analyzed in its own "
             "process (bgsage's C++ core holds the GIL, so processes scale "
             "where threads don't); output is byte-identical to serial. Auto "
             "oversubscribes (jobs x threads ~ 3x cores) because the 1-ply luck "
             "sweep cannot use engine threads. Pass --jobs 1 to force the "
             "serial path.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print warnings for skipped/unmatched moves",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Write indented JSON (human-readable)",
    )
    parser.add_argument(
        "-z", "--compress", action="store_true",
        help="gzip the output (.gva.gz, or .gvab.gz with --gvab). A .gz is "
             "appended to an explicit -o path too, unless it already ends in .gz.",
    )
    parser.add_argument(
        "--no-file", action="store_true", dest="no_file",
        help="Only show terminal output; write no file",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Only show the progress bar and the output file location",
    )
    parser.add_argument(
        "--silent", action="store_true",
        help="Suppress all output (files are still written)",
    )
    parser.add_argument(
        "--all-moves", action="store_true", dest="all_moves",
        help="Include all legal moves in move_options (default: top 6 + played)",
    )
    parser.add_argument(
        "--count-illegal", action="store_true", dest="count_illegal",
        help="Include illegal/mismatched moves in PR calculation (default: excluded)",
    )
    parser.add_argument(
        "--gvab", "--binary", action="store_true", dest="gvab",
        help="Write the compact binary encoding (.gvab) instead of the .gva "
             "JSON. The two carry the same match.",
    )
    parser.add_argument(
        "--link", action="store_true",
        help="Print a self-contained gammonview.com viewing link (whole match "
             "encoded in the URL fragment). Works with --no-file.",
    )
    parser.add_argument(
        "--browser", action="store_true",
        help="Open the gammonview.com viewing link in the default browser. "
             "Implies --link's encoding; add --link too to also print the URL.",
    )
    args = parser.parse_args()

    if args.init_presets:
        target = GLOBAL_PRESETS_FILE if args.global_presets else PROJECT_PRESETS_FILE
        try:
            path = write_template(target, force=args.force)
        except FileExistsError as e:
            print(e)
            sys.exit(1)
        print(f"Wrote starter presets to {path}")
        return

    if args.input_file is None:
        parser.error("input_file is required (or use --init-presets)")

    # Verbosity: --silent hides everything; --quiet hides the table/summary but
    # keeps the progress bar and the final file location; both suppress the
    # closing "Total time" line. --no-file writes nothing but keeps the normal
    # terminal output.
    silent = args.silent
    quiet = args.quiet or silent
    write_file = not args.no_file

    try:
        merged = analyze_file(
            args.input_file,
            preset=args.preset,
            threads=args.threads,
            verbose=args.verbose,
            all_moves=args.all_moves,
            count_illegal=args.count_illegal,
            quiet=quiet,
            show_progress=not silent,
            jobs=args.jobs,
        )
    except (ValueError, FileNotFoundError) as e:
        if not silent:
            print(e)
        sys.exit(1)

    # The .gvab bytes back the on-disk outputs and the --link/--browser payload,
    # so compute them whenever any is needed. Emit the *canonical* OGXM JSON --
    # exactly what read_gvab yields from the .gvab -- so the JSON, binary, and
    # link all describe identical data. The .gva is literally read back from the
    # same bytes we write to the .gvab (one write_gvab, so the two can never
    # drift). The .gvab binary is a lossy encoding (probs/equities quantized to
    # 1/10000, equities clamped to +/-3, dice stored unordered, no
    # notation/diff/preset), and canonicalizing here means downstream code --
    # whether it loads the .gva, reads the .gvab, or opens a link -- always sees
    # the same, fixed-point form. `merged` already carries our analysis appended
    # (existing OGXM analysis blocks preserved).
    ogxm = None
    if write_file or args.link or args.browser:
        gvab_bytes = write_gvab(merged)
        ogxm = read_gvab(gvab_bytes)

    if write_file:
        mat_path = args.input_file.resolve()

        # One file out: the .gva, or with --gvab the binary instead (as in
        # gvan-batch). Either is the whole match -- read_gvab(.gvab) is the
        # .gva -- so writing both would just be the same document twice.
        suffix = (".gvab" if args.gvab else ".gva") + (".gz" if args.compress else "")
        out_path = (
            _gz_path(args.output_file, args.compress) if args.output_file
            else mat_path.with_suffix(suffix)
        )

        if args.gvab:
            out_path.write_bytes(gzip.compress(gvab_bytes) if args.compress else gvab_bytes)
        else:
            payload = (
                json.dumps(ogxm, indent=2) if args.pretty
                else json.dumps(ogxm, separators=(",", ":"))
            )
            if args.compress:
                out_path.write_bytes(gzip.compress(payload.encode()))
            else:
                out_path.write_text(payload)
        if not silent:
            print(f"\n{'GVAB' if args.gvab else 'GVA'} written to {out_path}")

    if args.link or args.browser:
        payload = encode_match(ogxm)
        url = f"{GAMMONVIEW_BASE_URL}/#/?{SHARE_PARAM}={payload}"
        if len(payload) > MAX_SHARE_LENGTH and not silent:
            print(f"\nWarning: link payload is {len(payload):,} chars, over the "
                  f"{MAX_SHARE_LENGTH:,}-char size proven safe across browsers; "
                  f"some clients may truncate it (use --gvab and share the file instead).")
        if args.link and not silent:
            print(f"\nLink: {url}")
        if args.browser:
            opened = _open_in_browser(url)
            if not silent:
                print("\nOpening in browser..." if opened
                      else f"\nCould not open a browser. Link:\n{url}")

    if not quiet:
        print(f"\nTotal time: {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
