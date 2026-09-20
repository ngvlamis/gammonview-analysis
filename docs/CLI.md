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

Two independent axes. **Threads fill a decision; processes fill a machine.**

| Flag | Axis | Meaning |
|---|---|---|
| `--threads N` | A | How many of a decision's ~15 candidate moves are evaluated at once. `0` (default) = every core. |
| `--jobs N` | B | Worker **processes**, one decision each. `0` (default) = auto = `ceil(cpu/7)`, at least 2. `1` = serial. |

Both axes exist because thread scaling has a ceiling that is not the GIL.
Analysis elevates about fifteen candidate moves per decision and overlaps them,
so a single decision draws roughly **seven cores and no more** — enough to fill
a laptop, not enough to fill a workstation. Processes supply the rest by
putting several decisions in flight at once.

Defaults are sized from that: `ceil(cpu/7)` workers, each using every core. The
floor of two is deliberate — the fifteen candidates finish unevenly, so a
second decision in flight covers the first's tail, which is worth ~4% even on a
4-core machine and makes timings markedly more stable when something else
(a browser, say) is competing for cores.

`--jobs 1` forces the old serial path. It costs about 4% on 8 cores and 44% on
24, because one decision cannot fill a large machine.

**Output is byte-identical to the serial path** at any setting — parallelism
changes wall-clock time and nothing else.

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
| `world_class` (`wc`) | `3ply` | `4ply` (both kinds) | `truncated2` | World Class (XG Roller+) |
| `world_class_fast` (`wcf`) | `3ply` | `truncated2` (**cube only**) | `truncated2` | World Class (3-tier) |

`fast` is the default.

### How the tiers fire

The two tiers answer two different questions, and each has its own threshold.

The **middle tier** is a *second look before judging*. It runs on a borderline
decision — one the screen cannot call confidently — and its job is to settle it.
Borderline means the top two checker moves are within `close_threshold`, or a
cube is that close to its double point, or to its take point on a take/pass.

The **second pass** is *sizing an error you already believe in*. It runs when a
decision is wrong by more than `error_threshold`, where the magnitude is
something a rollout can actually measure.

A decision reaches the second pass straight from the screen, or by way of the
middle tier when the closer look is what revealed the error.

Three consequences worth knowing:

- A close decision deepens **without** an error, but only on the three-tier
  presets. Two-tier presets leave it at the screen on purpose.
- An error at or under `error_threshold` stays at the screen — or takes the
  middle tier where one exists. There is nothing inside that margin for a
  rollout to size.
- A borderline decision that the middle tier then finds to be a real error
  **does** reach the second pass. A near-tied top two says nothing about how
  far down the list the player actually went.

`error_threshold` defaults to `0.02`, the standard cutoff below which an error
is not worth sizing precisely. It exists to ration rollouts, so a preset whose
second pass is an ordinary ply level rather than a rollout may sensibly set it
to `0` — `fast` does, because 3-ply depth is cheap enough to spend on every
error.

Both thresholds take either a single number or a `{checker, cube}` mapping,
the same shape `mid_pass` takes.

`world_class_fast`'s middle tier is **cube-only**, and names the same level as
its second pass — so the cube rule reads "borderline *or* wrong → roll it out".
Checker plays go straight from the 3-ply screen to `truncated2` when they are
wrong, and stay at the screen when they are right.

That asymmetry is measured, not arbitrary, and what measures it is how often
"borderline" fires. At a 0.04 window a cube is borderline about 5% of the time
and a checker play 53.8% of the time — so the same "borderline or wrong → roll
it out" rule costs well under a second per match on cubes and tens of seconds
per match on checker plays. `gvanalysis/presets.py` records the measurements
beside the constants they set.

### Choosing one

`fast` is a good default for a quick look. For analysis you intend to trust,
the three strong presets were compared against eXtreme Gammon over 493 matches:

> **Pending re-measurement.** The agreement figures in this section were
> measured before `balanced`, `world_class` and `world_class_fast` were retuned
> (the two thresholds, the middle-tier escalation, and `world_class`'s move
> from a flat 4-ply pass to 3-ply with a 4-ply middle tier). The *ordering* is
> expected to survive; the numbers are not current for any of the three. The
> timings in *What they cost* below were re-measured after the retune and are
> current.

| | mean gap to XG's match PR | same checker play as XG | PR within 1 of XG |
|---|---|---|---|
| `world_class` | 0.434 | 90.3% | 91.8% |
| `world_class_fast` | 0.441 | 90.0% | 90.7% |
| `balanced` | 0.540 | 89.2% | 86.7% |

- **`world_class_fast`** is the recommended analysis — 0.007 PR behind the
  deepest preset, and indistinguishable from it on checker play. The two sit
  three times closer to each other than either does to XG: the remaining gap is
  the engine difference, not the search depth.
- **`world_class`** matches XG's *search routing* — a 3-ply pass that deepens to
  4-ply on near-ties, which is what XG World Class actually does — so it is the
  one to reach for when reproducing XG is itself the goal. It is not more
  accurate than `world_class_fast` in any way that experiment demonstrates.
- **`balanced`** no longer trades fidelity for speed — after the retune it
  costs about what `world_class_fast` does (see *What they cost*). What it
  still offers is near-zero bias over a long record, which makes it the closest
  of the three there, and the most headroom left; on any single match it is the
  worst.

These are agreement numbers, not accuracy numbers — where bgsage and XG differ,
neither is the arbiter. **[`PRESET_ACCURACY.md`](PRESET_ACCURACY.md)** is the
full experiment, including why rolling out *lowers* agreement with XG and the
evidence that the residual difference is systematic.

### What they cost

The other half of the dial. Six tournament matches — 84 to 459 plies, 1,429 in
all — analyzed end to end at the default `--jobs`, on three machines.
**Milliseconds per ply**, because the per-match figure depends entirely on how
long your matches are:

| Preset | M2 Ultra (24c) | M3 (8c) | i5-7600 (4c) |
|---|---|---|---|
| `very_quick` | 5.8 ms | 11.6 ms | 40.8 ms |
| `fast` | 9.6 ms | 31.4 ms | 75.5 ms |
| `deep` | 18.0 ms | 65.5 ms | 143.0 ms |
| `balanced` | 170.4 ms | 751.8 ms | 1030 ms |
| `world_class_fast` | 165.6 ms | 611.0 ms | 1016 ms |
| `world_class` | 291.3 ms | 1371 ms | 2048 ms |

For a sense of scale on one 13-point match (459 plies): `world_class_fast` is
about 1 minute on the workstation, 4 minutes on the laptop and 9 minutes on the
4-core desktop; `world_class` is roughly double each.

**There is no machine-independent "relative to `fast`" number**, which is why
this table gives absolutes on named hardware. The multiple for
`world_class_fast` measures 17.2×, 19.5× and 13.5× on the three machines above,
and for `world_class` 30.3×, 43.6× and 27.1× — the ratio moves because its
denominator is machine-dependent too, and not even monotonically with how fast
the machine is. Only `deep` is stable enough to quote as a ratio (1.9–2.1×
`fast` everywhere).

Two comparisons are the argument for the lineup, and both hold on every machine:

- `world_class` costs **1.8–2.2× `world_class_fast`** and buys a little under
  0.01 PR of XG agreement. That is why `world_class_fast` is the
  recommendation and `world_class` is reserved for deliberately reproducing XG.
- `balanced` is **no cheaper than `world_class_fast`** — within a few percent
  on the workstation and the 4-core desktop, and 23% *more* expensive on the
  laptop. It is kept for its lower variance on long records, not for speed.

The jump from `deep` to `balanced` is where rollouts enter — everything above it
is pure full-width search, everything from `balanced` down sizes its errors with
a truncated rollout. That step is the one to expect in any timing you take
yourself.

Two cautions if you benchmark this yourself. Individual matches spread about 2×
around these means at every preset, because what a preset costs depends on how
often a given match trips *its* escalation triggers. And a fanless machine
slows measurably as it heats — a MacBook Air drifted 2% per hour under
sustained analysis — so compare runs taken at the same temperature, not an hour
apart.

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
