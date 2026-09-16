# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The preset tables in the docs must match ``gvanalysis/presets.py``.

This test exists because they did not. The README advertised ``balanced``'s
second pass as ``4ply`` and described it as having no rollout, long after it had
become ``truncated2``; ``world_class_fast``'s middle tier was listed as applying
to checker plays when it is cube-only. Both are the kind of error a reader has no
way to detect and no reason to doubt -- the table looks authoritative, and
nothing in a green test suite contradicted it.

So the tables are pinned here. Two documents carry one, for different audiences:

  - ``README.md``     -- the overview table
  - ``docs/CLI.md``   -- the full command-line reference

Both are parsed and checked against the live ``PRESETS`` mapping, so adding or
retuning a preset fails this test until the docs follow. That is the point: the
failure is the reminder.

The middle-tier column is prose rather than a bare level, because the tier is not
uniform -- ``balanced`` applies it to both decision kinds while
``world_class_fast`` applies it only to cubes. The check therefore requires the
level to appear in the cell, and requires a cube-only tier to say so, rather than
demanding an exact string.

Run directly:
    uv run python tests/test_docs_presets.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvanalysis.presets import PRESETS

#: Docs carrying a preset table, relative to the repo root.
DOCS = ("README.md", "docs/CLI.md")

#: A table row: | `name` (`alias`) | 1st | middle | 2nd | XG equivalent |
_ROW = re.compile(r"\|\s*`(\w+)`\s*\(`?\w+`?\)\s*\|(.*)\|\s*$")

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"   FAIL: {label}")
    else:
        print(f"   ok:   {label}")


def _cell(text: str) -> str:
    return text.replace("`", "").replace("**", "").strip()


def _spec(name: str) -> dict:
    spec = PRESETS[name]
    return spec if isinstance(spec, dict) else vars(spec)


def _parse(md: str) -> dict[str, list[str]]:
    rows = {}
    for line in md.splitlines():
        m = _ROW.match(line)
        if m:
            rows[m.group(1)] = [_cell(c) for c in m.group(2).split("|")]
    return rows


def _mid_ok(cell: str, checker: str | None, cube: str | None) -> bool:
    """Does the middle-tier cell describe this preset's mid_pass honestly?"""
    if checker is None and cube is None:
        return cell == "—"          # em dash: no middle tier
    if checker == cube:                  # uniform: name the level
        return checker in cell
    # asymmetric: name the level that fires, and say which kind it applies to
    named = cube if checker is None else checker
    kind = "cube" if checker is None else "checker"
    return named in cell and kind in cell.lower()


def main() -> int:
    for doc in DOCS:
        path = _REPO_ROOT / doc
        print(f"--- {doc} ---")
        if not path.is_file():
            check(False, f"{doc} exists")
            continue

        rows = _parse(path.read_text(encoding="utf-8"))
        check(set(rows) == set(PRESETS),
              f"{doc} lists exactly the built-in presets "
              f"(missing: {sorted(set(PRESETS) - set(rows))}, "
              f"extra: {sorted(set(rows) - set(PRESETS))})")

        for name, cells in rows.items():
            if name not in PRESETS:
                continue
            d = _spec(name)
            if len(cells) < 3:
                check(False, f"{doc}:{name} row has a first/middle/second column")
                continue
            first, mid, second = cells[0], cells[1], cells[2]

            check(first == (d.get("first_pass") or "—"),
                  f"{doc}:{name} 1st pass is {d.get('first_pass')!r} (doc says {first!r})")
            check(second == (d.get("second_pass") or "—"),
                  f"{doc}:{name} 2nd pass is {d.get('second_pass')!r} (doc says {second!r})")
            check(_mid_ok(mid, d.get("mid_pass_checker"), d.get("mid_pass_cube")),
                  f"{doc}:{name} middle tier is checker={d.get('mid_pass_checker')!r} "
                  f"cube={d.get('mid_pass_cube')!r} (doc says {mid!r})")

    print()
    if _failures:
        print(f"{len(_failures)} of {_checks} checks FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"All {_checks} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
