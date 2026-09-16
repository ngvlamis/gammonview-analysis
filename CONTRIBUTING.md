# Contributing

Bug reports, format questions and patches are all welcome. This file covers what
you need to know before sending one.

## Getting set up

```bash
git clone https://github.com/ngvlamis/gammonview-analysis
cd gammonview-analysis
uv sync          # gvformat + gvanalysis + bgsage + everything the tests need
```

`uv sync` installs the engine too, so the full suite runs out of the box. If you
only care about `gvformat` — the codec — you never need bgsage at all; it is an
optional extra and the codec is pure stdlib.

The packages support **Python 3.10+**. `.python-version` pins the development
environment to 3.13; that is a convenience, not a requirement.

## Running the tests

**There is no pytest.** Each `tests/test_*.py` is a standalone script with its
own check/fail reporting, run directly and judged by its exit code. The runner
preserves that contract — it launches each file as a subprocess, exactly as
running it by hand would.

```bash
uv run python tests/run_all.py              # everything, one verdict
uv run python tests/run_all.py --fast       # skip the engine-backed tests
uv run python tests/run_all.py -k ogxm      # only matching filenames
uv run python tests/test_ogxm_export.py     # a single file, directly
```

A test reports one of three outcomes, and **skips are counted separately from
passes on purpose**: a suite quietly skipping half of itself otherwise looks
identical to a green one.

Some tests cross-check against HedgeHog's reference C++ codec
(<https://gitlab.com/eranlambooij/hedgehog-public>, built with `make libogxm`).
They skip cleanly when it is absent, which is the normal case for a fresh clone —
the suite is green either way. Note that `ogxm_ctypes` raises `SystemExit`, not
`ImportError`, when the shared library is missing, so any new guard must catch
both.

The JavaScript mirror has its own suite:

```bash
cd gvformat-js && npm test
```

**Both suites should be green before you send a patch**, and a change that
touches the format needs to keep them agreeing with each other.

## What CI runs

Nothing you cannot run yourself. On every pull request, `.github/workflows/ci.yml`
runs the fast suite across Python 3.10 through 3.14 on Linux, plus one Windows
and one macOS row; `npm test` on Node 18 and 24; and a build job that installs
the wheel into a bare interpreter to confirm `gvformat` still imports with no
third-party packages present. The Python rows deliberately install *without* the
engine, so an accidental bgsage or pyyaml import in the codec shows up as a
failure rather than as a passing test on a developer machine that happens to
have both.

The engine-backed tests are slower, so they run on a separate schedule
(`engine.yml`): on every push to `main`, weekly, and on demand. If your change
touches `gvanalysis`, run the full suite locally before sending it — the PR will
not.

The byte-exact goldens under `tests/golden/` *are* exercised by that job —
`test_ogxm_pipeline` re-analyzes the corpus and requires the result to match
byte-for-byte, and `test_luck_fill` requires identical floats. Both of those
comparisons are **platform-gated**, because bgsage is not bit-reproducible
across CPU architectures: the goldens were generated on macOS arm64, and on a
Linux x86_64 runner with the same bgsage build one best move of 521 and one cube
verdict of 296 come out differently. The gate reads
`tests/golden/PROVENANCE.json` (see `tests/fixtures.py:goldens_are_native`), so
off that machine you get `SKIP` lines naming the checks, not a red run — and
every structural check in those same two files still runs everywhere.

Regenerating the goldens is a maintainer action on the machine that owns them
(`tests/regen_golden.py`, which restamps the provenance), never something a CI
run should do.

## The two packages

Dependencies flow one way and must keep doing so:

```
gvanalysis  ->  gvformat  ->  (stdlib)
```

- **`gvformat`** is the codec. **Zero third-party dependencies** — this is a
  hard constraint, not a preference. Someone should be able to `pip install
  gammonview` and read a `.gvab` with nothing else installed. A patch that adds
  an import here will be asked to remove it.
- **`gvanalysis`** is the bgsage-powered analysis. It may depend on `gvformat`;
  `gvformat` may never depend on it.

## The JavaScript mirror

`gvformat-js/` is an ESM port of `gvformat`, and the two must agree **byte for
byte** — the same `.gvab` in gives the same OGXM JSON out. If you fix a bug in
one, fix it in the other in the same change. The MET tables and the OGID
encoders in particular are kept numerically identical, and there are tests that
will catch you if they drift.

Files ported from HedgeHog's C++ (`binary.py`, `export.py`, and their JS mirrors)
carry an attribution header. Keep it, and add one to any new file that ports
upstream code — see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Style

Match the surrounding code; there is no formatter or linter configured, and
please do not add one in a patch that also changes behaviour.

The one convention worth stating explicitly is about **comments**. This codebase
documents *why*, not *what* — where a constant came from, what was measured, what
the alternative was and why it lost. Several modules record the experiment that
settled a design choice. If you change one of those decisions, update the
reasoning with it; if you add one, say what convinced you.

Every source file carries a two-line SPDX header:

```python
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis
```

New files need it. By contributing you agree your work is licensed under the
MIT terms in [LICENSE](LICENSE).

## Changes to the format

`gvformat` writes a format that other software reads, so a format change is not
a local decision:

- **Additive changes** — a new chunk, a new optional field — are the safe kind.
  Undecoded chunks survive a rewrite, so a reader that does not know yours will
  not destroy it.
- **Anything a current reader could not parse** needs `min_reader_minor` raised,
  and a good reason.
- OGXM is **HedgeHog's** format and this is one implementation of it. A change to
  the shared part of the format belongs upstream first
  (<https://gitlab.com/eranlambooij/hedgehog-public>); the GammonView-specific
  extensions live in the ancillary `GVAN` chunk precisely so they do not collide
  with it.

Bear in mind that rewriting a file **canonicalizes** it. `write_gvab(read_gvab(x))`
reaches a fixed point after one pass and preserves the decoded content exactly,
but it does not preserve byte-level slack another producer left. When you test
against a foreign file, **compare decoded dicts, not bytes**.

## Reporting a bug

The most useful report includes the file that triggered it. If the match is
private, an XGID or OGID of the position, or a cut-down file that still
reproduces, is nearly as good. Say which command you ran and what you expected
instead.
