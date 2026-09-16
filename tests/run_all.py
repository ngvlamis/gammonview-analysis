# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Run every test script and report one verdict.

The suite is deliberately not pytest: each ``tests/test_*.py`` is a standalone
script with its own check/fail reporting, run directly and judged by its exit
code. This runner keeps that contract -- it launches each file as a subprocess,
exactly as running it by hand would -- and exists so that CI, and anyone new to
the repo, has a single command to type.

A test signals one of three outcomes:

* exit 0 with ``SKIP`` on its first line of output -- a fixture or an optional
  dependency is absent, which is not a failure
* exit 0 otherwise -- passed
* any non-zero exit -- failed

Skips are reported separately rather than folded into the pass count, because a
suite that is quietly skipping half of itself looks identical to a green one
otherwise. That distinction is the whole point of the runner on CI.

Usage::

    uv run python tests/run_all.py              # everything
    uv run python tests/run_all.py --fast       # skip the engine-backed tests
    uv run python tests/run_all.py -k ogxm      # only matching names
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

#: Tests that need the ``[engine]`` extra. They dominate the suite's runtime, so
#: CI can run everything else on each push and these on a slower cadence -- and
#: without the extra installed, only these fail.
#:
#: Most need bgsage, and the transitive importers are easy to miss:
#: ``test_luck_fill`` reaches it through ``gvanalysis.luck`` -> ``game_eval``,
#: and ``test_illegal_play_steps`` through ``gvanalysis.ogxm_reconstructor``.
#:
#: ``test_docs_presets`` is the odd one: it needs no engine at all, only
#: ``gvanalysis.presets``, which imports ``yaml`` at module load. pyyaml ships in
#: the same extra, so the effect is identical -- it belongs here despite never
#: touching bgsage. Don't "fix" that by removing it from this set; make the yaml
#: import lazy first, then it can move.
ENGINE_BACKED = frozenset({
    "test_analyze_position.py",
    "test_count_illegal.py",
    "test_cube_tiers.py",
    "test_docs_presets.py",
    "test_gvab_writer.py",
    "test_illegal_play_steps.py",
    "test_luck_fill.py",
    "test_ogxm_export.py",
    "test_ogxm_pipeline.py",
    "test_ogxm_stats.py",
    "test_read_gvab.py",
    "test_reconstruct_mat.py",
    "test_screened_checker.py",
})

PASS, SKIP, FAIL = "pass", "skip", "fail"


def discover(pattern: str | None, fast: bool) -> list[Path]:
    paths = sorted(_TESTS_DIR.glob("test_*.py"))
    if fast:
        paths = [p for p in paths if p.name not in ENGINE_BACKED]
    if pattern:
        paths = [p for p in paths if pattern in p.name]
    return paths


def run_one(path: Path) -> tuple[str, float, str]:
    """Run one test file; return (outcome, seconds, captured output)."""
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True,
        text=True,
        cwd=_TESTS_DIR.parent,
    )
    elapsed = time.monotonic() - started
    output = (proc.stdout or "") + (proc.stderr or "")

    if proc.returncode != 0:
        return FAIL, elapsed, output
    # A skipping test still exits 0, so the reason has to be read from output.
    if any(line.startswith("SKIP") for line in output.splitlines()):
        return SKIP, elapsed, output
    return PASS, elapsed, output


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-k", dest="pattern", metavar="TEXT",
                    help="only run tests whose filename contains TEXT")
    ap.add_argument("--fast", action="store_true",
                    help="skip the engine-backed tests (see ENGINE_BACKED)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print each test's full output, not just failures")
    args = ap.parse_args()

    paths = discover(args.pattern, args.fast)
    if not paths:
        print("no tests matched")
        return 1

    results: list[tuple[Path, str, float, str]] = []
    width = max(len(p.name) for p in paths)

    for path in paths:
        print(f"{path.name:<{width}}  ", end="", flush=True)
        outcome, elapsed, output = run_one(path)
        mark = {PASS: "ok", SKIP: "skip", FAIL: "FAIL"}[outcome]
        print(f"{mark:>4}  {elapsed:6.1f}s")
        if args.verbose or outcome == FAIL:
            print("".join(f"    {line}\n" for line in output.splitlines()))
        results.append((path, outcome, elapsed, output))

    passed = [r for r in results if r[1] == PASS]
    skipped = [r for r in results if r[1] == SKIP]
    failed = [r for r in results if r[1] == FAIL]
    total_time = sum(r[2] for r in results)

    print()
    print("=" * 66)
    print(f"{len(passed)} passed, {len(skipped)} skipped, {len(failed)} failed "
          f"in {total_time:.1f}s")
    if skipped:
        print(f"  skipped: {', '.join(r[0].name for r in skipped)}")
    if failed:
        print(f"  failed:  {', '.join(r[0].name for r in failed)}")
    print("=" * 66)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
