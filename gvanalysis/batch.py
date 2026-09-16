# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Batch-analyze many match files, writing one .gva (or .gvab) per input.

Thin driver over `gvanalysis.match.analyze_file`: each input (`.mat`, `.gva`/
`.ogxm`, or `.gvab` -- OGXM is the internal representation, so `.mat` is
converted first) is analyzed quietly and written beside it with the extension
swapped to `.gva` (canonical OGXM JSON) or, with --gvab/--binary, `.gvab` (the
compact binary encoding). An OGXM input's existing analysis blocks are
preserved and ours is appended. The terminal shows only a progress bar; per-file
analysis output is suppressed.

Usage:
    uv run gvan-batch matches/*.mat
    uv run gvan-batch matches/ --preset world_class          # a dir = its matches
    uv run gvan-batch matches/*.mat --gvab                    # write .gvab instead
    uv run gvan-batch a.mat b.gvab --out-dir out/ --preset fast
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .match import analyze_file
from gvformat import write_gvab, read_gvab
from .presets import DEFAULT_PRESET, PRESETS

#: Input extensions a directory argument expands to (OGXM in any form + mat).
_INPUT_EXTS = (".mat", ".gva", ".ogxm", ".gvab")


class _FileBar:
    """In-place file-count progress bar on stderr (a no-op off a TTY).

    Shows completed/total files, elapsed time, and the file currently being
    analyzed. Stays clean under redirection or non-interactive runs.
    """

    def __init__(self, total: int, stream=None, width: int = 24):
        self.total = total
        self.n = 0
        self.stream = stream if stream is not None else sys.stderr
        self.width = width
        self.t0 = time.perf_counter()
        self._last_len = 0
        try:
            tty = self.stream.isatty()
        except (AttributeError, ValueError):
            tty = False
        self.enabled = total > 0 and tty

    def _line(self, current: str = "") -> str:
        frac = self.n / self.total if self.total else 1.0
        filled = round(self.width * frac)
        bar = "█" * filled + "░" * (self.width - filled)
        mins, secs = divmod(int(time.perf_counter() - self.t0), 60)
        head = f"Batch  [{bar}]  {frac * 100:3.0f}%  {self.n}/{self.total} files  {mins}:{secs:02d}"
        return f"{head}  {current}" if current else head

    def _draw(self, current: str = "", newline: bool = False) -> None:
        line = self._line(current)
        pad = " " * max(0, self._last_len - len(line))
        self.stream.write("\r" + line + pad + ("\n" if newline else ""))
        self.stream.flush()
        self._last_len = 0 if newline else len(line)

    def start(self, current: str) -> None:
        if self.enabled:
            self._draw(current)

    def advance(self) -> None:
        if not self.enabled:
            return
        self.n += 1
        self._draw()

    def close(self) -> None:
        if self.enabled:
            self._draw(newline=True)


def _collect_inputs(paths: list[Path]) -> list[Path]:
    """Expand the given paths into a de-duplicated, sorted list of match files.

    A directory contributes its top-level match files (`.mat`/`.gva`/`.ogxm`/
    `.gvab`, non-recursive); a file is taken as given. Shell globs are already
    expanded by the shell before we see them, so only directory expansion
    happens here.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        if p.is_dir():
            candidates = sorted(c for c in p.iterdir()
                                if c.is_file() and c.suffix.lower() in _INPUT_EXTS)
        else:
            candidates = [p]
        for c in candidates:
            rp = c.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(c)
    return out


def _write_output(merged: dict, out_path: Path, gvab: bool) -> None:
    """Write one merged-OGXM result as .gva (canonical JSON) or .gvab (binary).

    Both derive from a single write_gvab() so the JSON and binary forms can never
    describe different data (the .gva is the read-back of the same bytes).
    """
    gvab_bytes = write_gvab(merged)
    if gvab:
        out_path.write_bytes(gvab_bytes)
    else:
        out_path.write_text(json.dumps(read_gvab(gvab_bytes), separators=(",", ":")))


def main() -> None:
    t0 = time.perf_counter()

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "inputs", nargs="+", type=Path,
        help="match files (.mat/.gva/.ogxm/.gvab), or directories of them, to analyze",
    )
    parser.add_argument(
        "--gvab", "--binary", action="store_true", dest="gvab",
        help="Write the compact OGXM binary (.gvab) instead of .gva JSON",
    )
    parser.add_argument(
        "--preset", default=None,
        help="Analysis preset ("
             f"{'|'.join(PRESETS)}; built-ins overridable in presets.yaml). "
             f"Default: {DEFAULT_PRESET}",
    )
    parser.add_argument(
        "--threads", type=int, default=0,
        help="Parallel threads (default: 0 = auto)",
    )
    parser.add_argument(
        "--jobs", type=int, default=0,
        help="Decision-level parallel worker processes per file (default: 0 = "
             "auto ~cpu/3). The batch's file loop stays sequential; this "
             "parallelizes decisions within each file. Pass --jobs 1 to force "
             "the serial per-file path.",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None, dest="out_dir",
        help="Write outputs here instead of beside each input file",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing output files (default: skip files already done)",
    )
    parser.add_argument(
        "--all-moves", action="store_true", dest="all_moves",
        help="Include all legal moves in move_options (default: top 6 + played)",
    )
    parser.add_argument(
        "--count-illegal", action="store_true", dest="count_illegal",
        help="Include illegal/mismatched moves in PR calculation (default: excluded)",
    )
    args = parser.parse_args()

    files = _collect_inputs(args.inputs)
    if not files:
        parser.error("no match files (.mat/.gva/.ogxm/.gvab) found in the given inputs")

    if args.out_dir is not None:
        args.out_dir.mkdir(parents=True, exist_ok=True)

    suffix = ".gvab" if args.gvab else ".gva"

    def out_for(mat: Path) -> Path:
        name = mat.with_suffix(suffix).name
        return (args.out_dir / name) if args.out_dir is not None else mat.with_suffix(suffix)

    bar = _FileBar(len(files))
    done = 0
    skipped = 0
    failures: list[tuple[Path, str]] = []

    for mat in files:
        out_path = out_for(mat)
        if out_path.exists() and not args.force:
            skipped += 1
            bar.advance()
            continue
        bar.start(mat.name)
        try:
            merged = analyze_file(
                mat,
                preset=args.preset,
                threads=args.threads,
                all_moves=args.all_moves,
                count_illegal=args.count_illegal,
                quiet=True,
                show_progress=False,
                jobs=args.jobs,
            )
            _write_output(merged, out_path, args.gvab)
            done += 1
        except Exception as e:  # noqa: BLE001 -- one bad file must not abort the batch
            failures.append((mat, f"{type(e).__name__}: {e}"))
        bar.advance()

    bar.close()

    # Final summary (also printed under redirection, where the bar is a no-op).
    parts = [f"{done} written"]
    if skipped:
        parts.append(f"{skipped} skipped (exists; use --force)")
    if failures:
        parts.append(f"{len(failures)} failed")
    print(f"Batch complete: {', '.join(parts)}  ({time.perf_counter() - t0:.1f}s)",
          file=sys.stderr)
    for mat, msg in failures:
        print(f"  FAILED  {mat}: {msg}", file=sys.stderr)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
