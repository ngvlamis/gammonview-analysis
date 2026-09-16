# Command-line reference

Five entry points are installed with the package; two more are development
utilities. Everything below runs under `uv run` in a checkout, or directly by
name once `pip install "gammonview[engine]"` has put them on your `PATH`.

| Command | Needs the engine? | What it does |
|---|---|---|
| [`gvan-match`](#gvan-match) | yes | Analyze one match, compute PR, write `.gva`/`.gvab` |
| [`gvan-batch`](#gvan-batch) | yes | The same over many files, one output each |
| [`gvan-position`](#gvan-position) | yes | Analyze a single position from an XGID or OGID |
| [`xg2gva`](#xg2gva--bgf2gva) | no | Convert an eXtreme Gammon `.xg` to OGXM JSON |
| [`bgf2gva`](#xg2gva--bgf2gva) | no | Convert a BGBlitz `.bgf` to OGXM JSON |
| `reconstruct-mat` | no | Rebuild a `.mat` from an analyzed file (development) |
| `compare-gva` | no | Diff analyses of the same match from different engines |

"Needs the engine" means the `[engine]` extra (bgsage). Only the three analysis
commands do. The converters live in `gvformat` and are pure stdlib, and the two
development utilities happen to be stdlib-only as well, despite living in
`gvanalysis`.

---

## `gvan-match`

Analyze a match file and compute per-decision PR.

```bash
uv run gvan-match match.mat                          # writes match.gva beside it
uv run gvan-match match.mat --preset world_class --gvab   # binary out
uv run gvan-match match.gvab --preset fast           # analyze a binary, append
uv run gvan-match match.mat --link                   # print a shareable URL
```

### Input

Takes `.mat` (Jellyfish/GNUbg), `.gva`/`.ogxm` (OGXM JSON) or `.gvab` (OGXM
binary), optionally `.gz`. OGXM is the pipeline's internal representation, so a
`.mat` is converted first.

**An OGXM input keeps any analysis it already carries; ours is appended as a new
block.** The format allows up to 16. Re-analyzing a file at a stronger preset
therefore adds to it rather than replacing it.

### Output

One file is written beside the input with the extension swapped: a `.gva`, or
with `--gvab` the binary instead. The two carry the same match — `read_gvab` of
the `.gvab` *is* the `.gva` — so only one is written. With no `-o`, a
`.gva`/`.gvab` input is rewritten in place with the appended result.

| Flag | Effect |
|---|---|
| `-o`, `--output PATH` | Output path, naming whichever file is written. |
| `--gvab`, `--binary` | Write the compact binary (`.gvab`) instead of the `.gva`. |
| `-z`, `--compress` | gzip the output. Appended to an explicit `-o` path too, unless it already ends in `.gz`. |
| `--pretty` | Indented JSON. |
| `--no-file` | Terminal output only; write nothing. |
| `--quiet` | Only the progress bar and the final path. |
| `--silent` | No output at all. Files are still written. |
| `--verbose` | Warn about skipped or unmatched moves. |

Both output forms derive from a single `write_gvab`, so the `.gva` always equals
`read_gvab(.gvab)` and the two cannot drift.

### Sharing

| Flag | Effect |
|---|---|
| `--link` | Print a self-contained `gammonview.com` viewing link. |
| `--browser` | Open that link in the default browser. |

The whole match is encoded into the URL fragment (`.gvab` → zlib-deflate →
base64url), so the link needs no hosted file and works alongside `--no-file`. A
very long link is routed through a temporary HTML redirect to dodge
argument-length limits. Pass both flags to print the URL *and* open it.

### Analysis

| Flag | Effect |
|---|---|
| `--preset NAME` | See [Presets](#presets). Default `fast`. |
| `--all-moves` | Put every legal move in `move_options` (default: top 6 plus the played move). |
| `--count-illegal` | Include illegal or unmatched moves in the PR calculation (default: excluded). |

### Parallelism

Two independent axes, because bgsage's core is C++ holding the GIL:

| Flag | Axis | Meaning |
|---|---|---|
| `--threads N` | A | Engine-internal threads, per worker. `0` = auto. |
| `--jobs N` | B | Worker **processes**, one decision each. `0` = auto (~cpu/2), `1` = serial. |

Threads alone do not scale past the GIL wall, which is why the decision-level
axis exists. Auto deliberately oversubscribes (jobs × threads ≈ 3× cores)
because the 1-ply luck sweep cannot use engine threads. **Output is
byte-identical to the serial path** at any setting.

---

## `gvan-batch`

`analyze_file` over many inputs, one output per input.

```bash
uv run gvan-batch matches/*.mat                   # .gva beside each input
uv run gvan-batch matches/ --preset world_class   # a directory = its matches
uv run gvan-batch matches/*.mat --gvab
uv run gvan-batch a.mat b.gvab --out-dir out/ --force
```

A directory argument expands to its top-level `.mat`/`.gva`/`.ogxm`/`.gvab`.
Existing outputs are skipped unless `--force` — so a `.gva` input whose output
name equals it is skipped by default. A bad file is reported and the batch
continues, exiting `1` if any failed.

The terminal shows only a file-level progress bar and a one-line summary; the
per-file analysis output is suppressed.

| Flag | Effect |
|---|---|
| `--gvab`, `--binary` | Write `.gvab` instead of `.gva`. |
| `--out-dir DIR` | Write outputs here instead of beside each input. |
| `--force` | Overwrite existing outputs instead of skipping. |
| `--preset`, `--threads`, `--jobs`, `--all-moves`, `--count-illegal` | As `gvan-match`. |

`--jobs` parallelizes decisions *within* each file; the file loop stays
sequential. Analyzers are rebuilt per file, so a large batch pays net-loading
cost once per file.

---

## `gvan-position`

Analyze one position — checker play or cube action, whichever the position
poses.

```bash
uv run gvan-position "XGID=-b----E-C---eE---c-e----B-:0:0:1:31:0:0:0:0"
uv run gvan-position "11ccccchhhjjjjj:66666888dddddoo:N0N:13:B:R:0:0:0:0"
uv run gvan-position "XGID=..." --level rollout
```

**XGID and OGID are both accepted**, and which one you passed is detected from
the string: an explicit `XGID=`/`OGID=` label wins, otherwise the OpenGammon
position shape is matched (an XGID never matches it).

| Flag | Effect |
|---|---|
| `--level LEVEL` | `1ply`, `2ply`, `3ply`, `4ply`, `truncated1/2/3`, `rollout`. Default `3ply`. |
| `--no-progress` | Suppress the rollout progress bar. |

At a rollout level a progress bar is drawn on **stderr**, so piping the report
stays clean, and it is already a no-op when stderr is not a TTY.

The bar reports **two phases**, because bgsage has two. After the planned trials
comes a *finalizing* pass, where moves the filter dropped are promoted and rolled
out one at a time. That tail is not a rounding error: on a 393-move position at
`truncated1` it was 302 promotions and the bulk of a 93-second run. The trial
denominator is fixed before any promotion is known, so the two are reported
separately rather than letting the bar sit at 100%.

---

## `xg2gva` / `bgf2gva`

Convert a source file to OGXM JSON. **No engine required** — these are
`gvformat` entry points and pure stdlib. They convert; they do not analyze.

```bash
uv run xg2gva match.xg  [out.gva]
uv run bgf2gva match.bgf [out.gva]
```

A `.mat` needs no CLI: `gvformat.mat_to_ogxm(text)` converts it in one call, and
`gvan-match` does it for you.

---

## Presets

`--preset` selects an eXtreme Gammon–style scheme: a cheap first pass screens
every decision, and a stronger pass runs only where it is needed. Single-pass
presets judge everything at the first pass.

| Preset (aliases) | 1st pass | Middle tier | 2nd pass | XG equivalent |
|---|---|---|---|---|
| `very_quick` (`vq`) | `2ply` | — | — | Very quick |
| `fast` (`f`) | `2ply` | — | `3ply` | Fast |
| `deep` (`d`) | `3ply` | — | — | Deep |
| `balanced` (`b`) | `2ply` | `3ply` (both kinds) | `truncated2` | — (quality/speed) |
| `world_class` (`wc`) | `4ply` | — | `truncated2` | World Class (XG Roller+) |
| `world_class_fast` (`wcf`) | `3ply` | `truncated2` (**cube only**) | `truncated2` | World Class (3-tier) |

`fast` is the default.

### How the tiers fire

The **second pass** runs on an *error* — the played checker move or cube action
disagreed with the first pass.

The **middle tier** runs on a *borderline* decision, one the screen would
otherwise have settled. A cube is borderline when it is within
`close_threshold` of its double point, or of its take point on a take/pass.

Two consequences worth knowing:

- A close decision deepens **without** an error, but only on the three-tier
  presets. Two-tier presets leave it at the screen on purpose.
- An error only reaches the second pass if it costs *more* than
  `close_threshold`. Inside that margin there is nothing for a rollout to size,
  so the middle tier takes it where one exists.

`world_class_fast`'s middle tier is **cube-only**, and names the same level as
its second pass — so the cube rule reads "borderline *or* wrong → roll it out".
Checker plays go straight from the 3-ply screen to `truncated2` when they are
wrong, and stay at the screen when they are right.

That asymmetry is measured, not arbitrary. A middle tier fires on decisions the
screen would otherwise have settled, so what decides whether it pays is its cost
against the *screen below*, not against the tier above — and off a 3-ply screen,
4-ply costs 19× on a move list but only 4× on one pre-roll cube. Restoring a
checker middle tier runs the preset 48% slower *and* diverts most of its
rollouts into 4-ply, which a separate test found to be the weaker estimator on
exactly these decisions. `gvanalysis/presets.py` records both measurements
beside the constants they set.

### Choosing one

`fast` is a good default for a quick look. For analysis you intend to trust,
the three strong presets were compared against eXtreme Gammon over 493 matches:

| | mean gap to XG's match PR | same checker play as XG | PR within 1 of XG |
|---|---|---|---|
| `world_class` | 0.434 | 90.3% | 91.8% |
| `world_class_fast` | 0.441 | 90.0% | 90.7% |
| `balanced` | 0.540 | 89.2% | 86.7% |

- **`world_class_fast`** is the recommended analysis — 0.007 PR behind the
  deepest preset, and indistinguishable from it on checker play. The two sit
  three times closer to each other than either does to XG: the remaining gap is
  the engine difference, not the search depth.
- **`world_class`** matches XG's *search tier* (a 4-ply first pass, as XG World
  Class uses), so it is the one to reach for when reproducing XG is itself the
  goal. It is not more accurate than `world_class_fast` in any way that
  experiment demonstrates.
- **`balanced`** trades PR fidelity for speed and is the only one of the three
  with real headroom left. Over a long record its near-zero bias makes it the
  closest of the three; on any single match it is the worst.

These are agreement numbers, not accuracy numbers — where bgsage and XG differ,
neither is the arbiter. **[`PRESET_ACCURACY.md`](PRESET_ACCURACY.md)** is the
full experiment, including why rolling out *lowers* agreement with XG and the
evidence that the residual difference is systematic.

### What they cost

The other half of the dial. Four corpus matches — 99 to 272 plies, 709 in all —
analyzed end to end on a 24-core Apple M2 Ultra at the default `--jobs`:

| Preset | Mean per match | Per ply | Relative to `fast` |
|---|---|---|---|
| `very_quick` | 1.1 s | 6 ms | 0.6× |
| `fast` | 1.8 s | 10 ms | 1× |
| `deep` | 2.9 s | 16 ms | 1.6× |
| `balanced` | 10.0 s | 56 ms | 5.7× |
| `world_class_fast` | 42.1 s | 238 ms | 24× |
| `world_class` | 79.2 s | 447 ms | 45× |

Per ply is the figure that travels; the per-match column depends entirely on how
long the matches are. Neither is a constant. Even per ply, the four matches
spread by about 2× at every preset — repeat runs land within 2%, so that is
content, not noise. What a preset costs depends on how often a given match
trips *its* escalation triggers, and a cube-heavy match is disproportionately
expensive at the presets that roll cubes out.

Two of these ratios are the argument for the lineup:

- `world_class` costs **1.9× `world_class_fast`** — a tight 1.7–2.3× across the
  four matches — and buys 0.007 PR of XG agreement. That is why
  `world_class_fast` is the recommendation and `world_class` is reserved for
  deliberately reproducing XG.
- `balanced` costs **a quarter of `world_class_fast`** for about 0.1 PR, which
  is the trade that makes it right for bulk or interactive work. This is the
  least stable ratio here: 3.0× on one match and 5.4× on another.

The jump from `deep` to `balanced` is where rollouts enter — everything above it
is pure full-width search, everything from `balanced` down sizes its errors with
a truncated rollout. That step is the one to expect in any timing you take
yourself. Process startup and engine construction cost about 0.1 s, so they do
not distort even the cheapest row.

### Custom presets

Built-ins always exist. Optional `presets.yaml` overrides layer on top, from two
locations, project-local winning over global:

1. `~/.config/bgsage/presets.yaml`
2. `./presets.yaml`

```bash
uv run gvan-match --init-presets            # write ./presets.yaml
uv run gvan-match --init-presets --global   # write the config-dir one
uv run gvan-match --init-presets --force    # overwrite an existing file
```

Overrides merge into a built-in field-by-field, or add new presets; `default:`
sets which preset is used when `--preset` is omitted. Delete the file to restore
defaults. A malformed file is skipped with a warning, and the other layer still
loads.
