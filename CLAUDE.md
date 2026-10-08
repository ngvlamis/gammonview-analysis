# bg-analysis

Backgammon analysis on the bgsage engine, split into two importable packages.
**Licensed MIT** — every source file carries an SPDX header; `LICENSE` holds the
full text. MIT because the deliverable is a format, and because upstream
HedgeHog is MIT. bgsage being MPL-2.0 constrains nothing (file-level copyleft,
§3.3, and we never modify or redistribute it). Two third-party notices must be
preserved — HedgeHog's, for the four files ported from its C++, and the
Kazaross-XG2 MET's — see `THIRD-PARTY-NOTICES.md` and the README's Licence
section.

- **`gvformat`** — the pure-stdlib GVA/OGXM codec (write/read `.gvab`, OGXM-JSON
  conversion, compute-on-read stats, OGID both ways — `board_to_ogid` /
  `parse_ogid`). **Zero third-party deps** — install
  and `import gvformat` anywhere without bgsage. This is the format layer the
  GammonView client-side JS mirrors. All source-format → OGXM converters live
  here, since none needs the engine: `convert_xg` (`.xg`), `convert_bgf`
  (`.bgf`), and `convert_mat` (`.mat`, plus `mat_to_ogxm(text)` for callers
  holding the text — the JS `convertMat` mirrors that one). `convert_xg` /
  `convert_bgf` (and `read_xg` / `read_bgf`) take **a path or the bytes**, since
  a server is handed an upload and `load_ogxm` has already decompressed a `.gz`
  before it knows what it holds; the JS mirrors are bytes-only for that reason.
- **`gvanalysis`** — the bgsage-powered analysis (`analyze_ogxm`/`analyze_file`,
  position analysis, the `analyze_match` one-call). Depends on `gvformat` + bgsage
  (the `[engine]` extra). Deps flow one way: `gvanalysis` → `gvformat`, never back.
- **`gvformat-js/`** — the JavaScript ESM mirror of `gvformat`, and **the definitive
  copy**. Published as **`@gammonview/gvformat`** to the public npm registry
  straight from this directory:

  ```bash
  cd gvformat-js && npm publish     # prepublishOnly runs the suite first
  ```

  `publishConfig.access` is `"public"` because **scoped packages default to
  `restricted`**, and a restricted publish on a free account fails with `402
  Payment Required` — an error that does not name its cause. The `@gammonview`
  scope is an npm **org**; a scope is not free-form, so publishing under one you
  do not own fails before anything is uploaded.

  A published version is permanent: unpublish is blocked after 72 hours and a
  version number can never be reused. That is what `prepublishOnly` is for.

  `gvformat-js/` keeps its own `LICENSE` and `THIRD-PARTY-NOTICES.md` — copies of
  the root pair, so the npm tarball carries them. They are the one thing here that
  must not drift: the package ships `binary.js` and `export.js`, which are ports of
  HedgeHog's C++, and MIT requires its notice to travel with them. `cp` both from
  the root if either changes.

  Set the version to the monorepo tag that ships it (`vX.Y.Z` → `X.Y.Z`), so one
  number describes both packages; the JS line simply skips releases where no JS
  changed. GammonView consumes it with a plain version pin and nothing else —
  no `.npmrc`, since the package resolves from npmjs.org like any other.

  `README.md` here is the npmjs package page. Its Install section is the first
  thing a visitor reads, so it has to stay true.

  **Before 1.0.0 this went to a private registry.** That history is not in the
  public tree (it was squashed at the 1.0.0 tag), so nothing here should carry a
  `publishConfig.registry`, and GammonView must not carry a scoped `.npmrc`
  line — either one silently routes the scope away from npmjs. (The retired
  server's details are in the author's local notes, which are not tracked here.)

**OGXM is the pipeline's internal representation.** Every input — `.xg`,
`.bgf`, `.mat`, `.gva`/`.ogxm` (OGXM JSON), or `.gvab` (OGXM binary) — is loaded
to an OGXM dict (`gvanalysis.loader.load_ogxm`, which dispatches to `gvformat`'s
converters — a `.mat` through `mat_to_ogxm`, with no analysis; an `.xg`/`.bgf`
with the source's own), its plies are turned back into engine decisions
(`ogxm_reconstructor`), analyzed (`analyze_ogxm`), and the result is **appended**
as a new analysis block (`gvformat.append_analysis`) — any analysis the input
already carried is preserved (up to 64 blocks in v2: `analyses_info` +
per-ply `analyses[]`, each block with its own `analysis_id`). `analyze_match(path)` returns those bytes; `analyze_file(path)` is the same
path stopping at the dict (load → analyze → append →
merged OGXM); `analyze_mat(path)` is the back-compat wrapper returning the
internal analyzed `data` dict.

Server one-liner (dropped match → `.gvab` bytes for the client):

```python
from gvanalysis import analyze_match      # mat_to_gvab is the old name, still exported
data = analyze_match("match.mat", preset="world_class", jobs=0)   # analyzed .gvab bytes
# also accepts .xg/.bgf/.gva/.ogxm/.gvab; an input's existing analysis is kept, ours appended
```

**Pass `jobs=0` from a server.** The API defaults to `jobs=1` (serial) while the
CLI defaults to `0` (auto), deliberately: a CLI owns its entry point, an
imported function cannot know whether its caller guarded `if __name__ ==
"__main__":`, and spawning without that guard fails as a bare
`BrokenProcessPool`. Serial is the safe default but no longer a cheap one —
measured 2026-09-19, it costs ~4% on 8 cores and ~44% on 24.

## Setup

```bash
uv sync                 # dev env: gvformat + gvanalysis + bgsage + tests
```

Consumers install what they need:
- codec only: `pip install gammonview` (`gvformat`, no bgsage)
- analysis: `pip install "gammonview[engine]"` (adds bgsage for `gvanalysis`)

## Dependency

`bgsage` is the engine (PyPI), an **optional `[engine]` extra**, pinned
`>=2.0.20260907`. Note it belongs in the extra, never in the base
`dependencies` — `gvformat` is zero-dep and `uv add` will put it in the wrong
place if you let it.

```bash
uv add bgsage --upgrade      # then check it landed under [engine], not base
```

**2.0 (2026-09-07) is a licence change as much as an engine one: AGPL-3.0 →
MPL-2.0.** MPL is file-level copyleft, so hosting analysis no longer carries
AGPL §13's obligation to offer source to users; it attaches only to bgsage's
own files if we modified them, which we don't.

The engine itself moved to Stage 11: 24 NNs instead of 19, Stage 9's 17
standard nets carried unchanged plus specialists for backgames, containment,
massive backgames and the snake. Ordinary play barely moves (PR within 0.02 on
the sample matches, no best-move changes across the four goldens); the gains
are concentrated in the position families the specialists exist for. Luck
figures move more than PR does, since luck is 1-ply and the routing reaches it
directly.

**An engine upgrade invalidates `tests/golden/`** — the goldens pin the
pipeline byte-for-byte, so every golden check fails at once. That is the
signal, not a fault. Regenerate and read the diff:

```bash
uv run python tests/regen_golden.py --check    # what would move
uv run python tests/regen_golden.py            # rewrite them
```

Regenerate **on the machine that owns them** — bgsage is not bit-reproducible
across CPU architectures, and `regen_golden.py` restamps
`tests/golden/PROVENANCE.json` with wherever it ran. That stamp is what gates
the two exact comparisons (`test_ogxm_pipeline`'s bytes, `test_luck_fill`'s
floats), so regenerating on a second machine silently moves the gate there and
turns the first machine's run into skips. `--check` says so when it is run off
the goldens' own platform.

Also re-read `_CubefulAnalyzer._convert_move` against `checker_eval._elevate`
— see the checker-play section below for why a green suite is not enough.

## CLIs / entry points

**`docs/CLI.md` is the user-facing reference** — every flag, in a register a
reader can be sent to. What follows is the maintainer's copy: the same commands
plus the measurements and reasoning behind how the tiers are tuned. Keep the two
in step when a flag changes; the rationale belongs only here.

- **`gvan-position`** (`gvanalysis/position.py`) — Parse an **XGID or OGID** and run
  checker play or cube analysis. Which format was given is detected from the string
  (`gvformat.looks_like_ogid`; an explicit `XGID=`/`OGID=` label wins, else the
  OpenGammon position-shape regex — an XGID never matches it).
  ```bash
  uv run gvan-position "XGID=-b----E-C---eE---c-e----B-:0:0:1:31:0:0:0:0"
  uv run gvan-position "11ccccchhhjjjjj:66666888dddddoo:N0N:13:B:R:0:0:0:0"
  uv run gvan-position "OGID=..." --level 1ply       # or: python -m gvanalysis.position
  uv run gvan-position "XGID=..." --level rollout    # progress bar on stderr
  uv run gvan-position "XGID=..." --level rollout --no-progress
  ```
  At a rollout level (`truncated1/2/3`, `rollout`) a bar is drawn on stderr —
  a no-op off a TTY, so piping the report stays clean, and `--no-progress`
  turns it off outright. It reports **two** phases, because bgsage does: the
  planned trials, then a *finalizing* pass where moves its filter dropped are
  promoted and rolled out one at a time. That tail is not a rounding error —
  on a 393-move position at `truncated1` it was 302 promotions and the bulk of
  a 93s run, which the bar used to spend sitting at 100%. The trial
  denominator is fixed before any promotion is known, so bgsage counts past its
  own total there and sends each promotion's real 0 → n_trials arc to
  `finalize_progress` instead; `analyze_position(on_progress=...)` surfaces
  both, tagged `"trials"` and `"finalizing"`. `finalize_progress` needs bgsage
  2.0 — the signature is probed, and an older engine just rolls the same moves
  out silently.
  OGID decoding lives in `gvformat.parse_ogid` (the inverse of `board_to_ogid`,
  still zero-dep); `position.py` only adapts it — picking the decision the
  position poses the way GammonView's `normalizeForAnalysis` does. Note OGID
  field 5 is the player who *acted*, so the on-roll player is its complement,
  and a pending double (`game_state == "D"`) is flipped onto the doubler.

- **`gvan-match`** (`gvanalysis/match.py`, `analyze_file`) — Analyze a match file
  (`.xg`, `.bgf`, `.mat`, `.gva`/`.ogxm`, or `.gvab`; optionally `.gz`) and
  compute per-move PR. An input's existing analysis blocks are preserved and
  ours is appended, so an `.xg` comes back carrying XG's judgement and ours.
  Format detection is content-first (`gvanalysis/loader.py`): three of the five
  name themselves in their opening bytes, so a mislabeled file still reads as
  what it is, and a file nothing identifies raises instead of being guessed at —
  guessing meant parsing an `.xg` as `.mat` text and returning an empty match.
  ```bash
  uv run gvan-match match.mat --preset fast                        # writes match.gva beside input
  uv run gvan-match match.xg --preset fast                         # .xg/.bgf too; source analysis kept
  uv run gvan-match match.gvab --preset world_class --gvab         # analyze OGXM binary, append analysis
  uv run gvan-match match.gva --preset fast                        # append to an OGXM JSON (in place)
  uv run gvan-match match.mat --gvab                               # .gvab instead of .gva
  uv run gvan-match match.mat --no-file                            # terminal output only, no file
  uv run gvan-match match.mat --link                               # also print a gammonview.com viewing link
  uv run gvan-match match.mat --browser                            # open that link in the default browser
  uv run gvan-match match.mat --quiet                              # progress bar + output path only
  uv run gvan-match match.mat --silent                            # no output (file still written)
  ```
  One file is written beside the input, extension swapped: a `.gva`, or with
  `--gvab` the binary instead — same as `gvan-batch`, and the two encode the
  same document, so writing both would only duplicate it. `-o` names whichever
  one is written. (With no `-o`, a `.gva`/`.gvab` input is overwritten in place
  with the appended result.) `--no-file` suppresses file output,
  `--quiet` shows only the progress bar and final path, `--silent` shows nothing.
  `--link` prints a self-contained `gammonview.com` viewing link — the whole
  match encoded into the URL fragment (`.gvab` → zlib-deflate → base64url via the
  site-agnostic `gvformat.share` codec; the `gammonview.com` URL itself is the
  app-level `gvanalysis.share`, mirroring GammonView's `sharelink.js`), so it
  needs no hosted file and works alongside `--no-file`. `--browser` opens that
  link in the default
  browser (via the platform opener; a very long link is routed through a temp
  HTML redirect to dodge argument-length limits) and implies the same encoding;
  pass both to print the URL and open it.
  Also `reconstruct-mat` and `compare-gva`. `--preset` selects an XG-style
  two-pass scheme: a cheap first pass screens every decision, a stronger second
  pass runs only on an error (the played checker move or cube action disagrees
  with the first pass). Single-pass presets judge everything at the first pass.
  The five built-in presets live in `gvanalysis/presets.py` (always available). Optional
  `presets.yaml` overrides are layered on top from two locations, project-local
  winning over global: `~/.config/bgsage/presets.yaml` then `./presets.yaml`.
  Create either with `--init-presets` (add `--global` for the config-dir one).
  Overrides merge into a built-in by key (field-by-field) or add new presets;
  `default:` sets the default. Restore defaults by deleting the file(s); a
  malformed file is skipped with a warning (the other layer still loads).

  | Preset (aliases) | 1st pass | 2nd pass | XG equivalent |
  |---|---|---|---|
  | `very_quick` (`vq`) | `2ply` | — | Very quick |
  | `fast` (`f`) | `2ply` | `3ply` | Fast |
  | `deep` (`d`) | `3ply` | — | Deep |
  | `world_class` (`wc`) | `3ply` → `4ply` on close | `truncated2` | World Class (XG Roller+) |
  | `world_class_fast` (`wcf`) | `3ply` (cube: → rollout on close too) | `truncated2` | World Class (3-tier) |

  `world_class_fast` is a 3-tier scheme (`mid_pass` + `close_threshold` on the
  preset): `3ply` baseline, `truncated2` rollout on error, and a middle tier on
  borderline decisions — but only for cubes. A middle tier is added on top of
  the screen — it fires on decisions the screen would otherwise have settled —
  so what decides whether it pays is its cost against the *screen below*, not
  against the tier above. Measured per decision off a `3ply` screen, 4-ply is
  19x the screen on a move list (25ms → 467ms) and only 4x on one pre-roll cube
  (24ms → 100ms). Dropping the checker middle tier made the preset ~36%
  faster while it ran twice as many rollouts, so borderline checker plays now go
  straight from the 3-ply screen to `truncated2` when they are wrong, and are
  left at the screen when they are right. The argument now rests on the
  borderline *rate* alone: at a 0.04 window a cube is borderline ~5% of the time
  and a checker play 53.8%, so the same rule costs ~0.4s per match on one and
  tens of seconds on the other. The old second argument — that restoring the
  tier "diverts 65 of the 83 checker rollouts into 4-ply, the weaker estimator"
  — described the **escalation defect**, not the tier: a borderline decision
  used to be capped at the middle tier however large its error proved. It
  escalates now, so a middle tier no longer steals rollouts (it would still
  cost, and more, since those decisions pay both tiers). Note also that the cost
  order has flipped since that measurement: on 760 paired checker decisions
  4-ply beats `truncated2` on 710, median 2.23x, because `checker_eval.py`
  screens candidates before the full-width pass. `mid_pass` takes a
  `{checker, cube}` mapping as well as a bare level (which still sets both); an
  omitted kind is plain 2-tier.

  The cube middle tier names `truncated2` — the same level as `second_pass`, so
  the cube rule reads "borderline **or** wrong → roll it out". It was `4ply`
  until 632 borderline cubes from XG-analyzed matches were scored against an
  independent `truncated3` arbiter: 4-ply disagreed with the arbiter's verdict
  on 14% of them (mean gap error 0.0127) against 12%/0.0091 for a single
  `truncated2` run, and averaging ten `truncated2` seeds improved that to only
  0.0090 — so a 360-trial rollout is limited by its horizon, not its variance,
  and 4-ply's zero variance buys nothing back. Naming an existing level in
  `mid_pass` costs no extra analyzer (levels are built once and shared), and
  routing the ~6% of cubes that are borderline to the rollout adds roughly
  0.4s per match.

  **Two thresholds, not one — they answer different questions.**
  `close_threshold` gates the *middle* tier: how near a tie before the screen's
  verdict stops being trusted (top two checker moves within it, or a cube that
  close to its double or take point). `error_threshold` gates the *second pass*:
  how much a decision has to cost before its size is worth a rollout. Both take
  a bare number or a `{checker, cube}` mapping, as `mid_pass` does.

  They want opposite values and have wildly different price tags — a 3-ply
  middle tier costs ~0.036s on a move list against a rollout's ~1.2s — so
  `close_threshold` can afford to be generous where `error_threshold` cannot.
  Mining XG's own World Class routing over 78,661 decisions finds the same
  split: its depth crossover sits near a top-2 gap of 0.08 (4-ply share falls
  64.9% → 15.0% across that boundary) while its rollout cliff is at an error of
  0.02 (22.9% rolled out in 0.015–0.020, 94.7% in 0.020–0.030).

  `error_threshold` defaults to **0.02**. It exists to ration rollouts, so
  `fast` sets it to `0` — its second pass is `3ply`, not a rollout, and 0.02
  there saves 6% of wall clock while moving PR by a mean of 0.14.

  **A borderline decision that the middle tier then finds to be a real error
  escalates to the second pass.** Both paths test the error again on the middle
  tier's own numbers, not the screen's. Until Sep 2026 neither did: a borderline
  checker play was capped at the middle tier (10% of them cost more than the
  threshold, worst 0.56) and a borderline cube was re-checked never (0.4% of
  cubes, median 0.07, max 0.22). The escalation is skipped when `mid_pass` names
  `second_pass`'s own level — analyzers are memoized by level, so identity is
  exact and re-running would buy the same answer twice. That makes it a no-op
  for `world_class_fast`'s cube tier by construction.

  **Every sizing tier is `truncated2`, never `4ply`, and that holds for both
  decision kinds.** The cube half is the arbiter test above (12%/0.0091 against
  4-ply's 14%/0.0127); the same test was repeated on 175 real sizing-tier
  *checker* errors and lands in the same place — `truncated2` is nearer the
  `truncated3` arbiter on 123 of the 175, mean gap 0.0095 against 4-ply's
  0.0132. Accuracy is now the whole of the argument. The rollout was once also
  the cheaper option, but `checker_eval.py` inverted that: 4-ply sizing would
  now run ~13% quicker (34.3s vs 38.9s on three matches), so the better
  estimator costs a few seconds a match rather than saving them. Cost stays
  bounded and cacheable — fixed seed, fixed trial count, fixed truncation depth
  — so the presets remain suited to server-hosted analysis.

  **`balanced` was retired 2026-09-20.** It was the same 3-tier machinery tuned
  for quality/speed rather than XG parity — `2ply` screen, `3ply` on near-ties,
  `truncated2` sizing — and the point of it was to be the cheap strong preset.
  It stopped being one. Because the sizing tier dominates the cost and it paid
  the same one, cross-machine timing put it at 170.4 / 751.8 / 1030 ms per ply
  against `world_class_fast`'s 165.6 / 611.0 / 1016 — within a few percent on
  two machines and 23% *worse* on the laptop — while agreeing with XG less
  often (0.540 mean PR gap against 0.441, on pre-retune figures). A preset that
  is neither faster nor better has no argument left. `resolve_preset` keeps the
  name in `_RETIRED` and fails with an explanation rather than a bare "unknown
  preset"; it is deliberately **not** aliased to `world_class_fast`, since the
  preset name is written into the output document and a silent redirect would
  mislabel the analysis. The gap below `world_class_fast` is now `deep`, an
  order of magnitude cheaper. Don't reinvent it without a sizing tier cheaper
  than `truncated2`, because that is the whole of what went wrong.

- **`gvan-batch`** (`gvanalysis/batch.py`) — Batch-analyze many match files
  (every format `load_ogxm` reads — it expands a directory by
  `loader.INPUT_EXTENSIONS`, so the two cannot fall out of step), writing one
  output per input. A thin wrapper
  over `analyze_file(..., quiet=True)`, so all engine logic stays in `match.py`.
  ```bash
  uv run gvan-batch matches/*.mat                    # .gva beside each input
  uv run gvan-batch matches/*.xg                     # .xg/.bgf too
  uv run gvan-batch matches/ --preset world_class    # a dir -> its match files
  uv run gvan-batch matches/*.mat --gvab             # .gvab instead of .gva
  uv run gvan-batch a.mat b.gvab --out-dir out/ --force
  ```
  The terminal shows only a file-level progress bar (stderr; a no-op off a TTY)
  plus a one-line summary. Each output takes the input's name with the extension
  swapped to `.gva` (canonical OGXM JSON) or, with `--gvab`/`--binary`, `.gvab`
  (compact binary) — written beside the input or into `--out-dir`. Both forms
  derive from a single `write_gvab`, so `.gva` == `read_gvab(.gvab)` and the two
  can't drift. An OGXM input's existing analysis is preserved (ours appended).
  Passes through `--preset`, `--jobs`, `--threads`, `--all-moves` and
  `--count-illegal`. A
  directory argument expands to its top-level match files;
  existing outputs are skipped unless `--force` (so a `.gva`/`.gvab` input whose
  output name equals it is skipped by default); a bad file is reported and the
  batch continues, exiting `1` if any failed. Analyzers are rebuilt per file
  (via `analyze_file`), so a large batch repays net-loading cost each file.

## OGXM v2: what we write, and the reference codec as an oracle

**`write_gvab` writes OGXM v2** (`gvformat/ogxm2_writer.py`), HedgeHog's current
format; upstream froze v1 at 1.6 and writes nothing else. `read_gvab` reads both
and always will — every file and share link from before the switch is v1 — and
`write_gvab_v1` keeps the old writer, for tests that exercise the v1 reader and
for comparison. **`docs/OGXM_V2_PROFILE.md` is the profile**: how our document
maps onto v2, what travels outside its fields, and every way reading a file back
differs from what was written. `docs/OGXM_FORMAT_SPEC.md` in the HedgeHog
checkout is the v2 spec; `OGXM_FORMAT_SPEC_GAMMONVIEW.md` here now describes v1
only.

**Nothing GammonView stores is dropped, and nothing outside v2 is invented.**
What v2 has no field for travels in `ANNO` records keyed `x-gammonview-…` —
the namespace the spec reserves for producers outside it (N6), which a
conforming reader keeps and never interprets. That was a deliberate choice over
a GVAN-style ancillary section or squatting on unassigned presence bits: the
first is an extension by another name, and upstream hands presence bits out in
order, so a squatted bit is a future collision. Three things needed it, each
found by measurement rather than assumed:

- **An illegal play.** v2 records one as a `set position` ply (dice, `illegal`,
  the board produced) and allows **no** decision on that ply — the reference
  writer silently drops one. So its whole analysis (checker, cube, luck) goes in
  a ply-scope annotation, and so do the steps when our document held the play as
  a dice ply. The same mechanism carries any checker decision that breaks a v2
  invariant: XG interleaving bearoff-database plays among ply-evaluated ones
  (A1 — `DECS` gets the list regrouped by level, the annotation XG's order), a
  played move listed twice by the pre-`c7891a4` blot split (A4), a played move
  judged at another level than the best (A5).
- **PR-counting flags.** Re-deriving them on read disagreed with what is stored
  on 129 BGBlitz decisions across the corpus (BGBlitz's own counting is
  authoritative there) and 8 of ours, so the block annotation lists only the
  exceptions to `basefill`'s rule — usually none.
- **`site`.** v2's `site` is the platform's lowercase host name and scopes its
  user ids; ours is free text from `[Site]` or XG's "location", sometimes a
  platform, sometimes a city, and nothing tells which. It goes in an annotation
  always — "annotation only for now"; a known-platform → host-name table can
  fill v2's `site` later without losing the text.

**Match-play equities are written as MWC** (v2's `cubeful match` currency),
mapped through each ply's own score frame — the honest v2 unit, and the one any
other reader understands. v2 stores 1e-6, so a normalized equity comes back
exactly except at the most lopsided scores, worst case ±6e-4 at 2-away/25-away
(span 0.00159); every value in the corpus round-trips exactly. The option of
keeping normalized equity with the currency left unrecorded was declined: exact,
but meaningless to everyone else.

**A source with its own table keeps it.** BGBlitz's MET is not ours (a point at
7-away/7-away: 0.05954 against 0.0626), and a `.bgf` stores both the MWC and the
normalized equity for every decision, as an exact linear map — so `convert_bgf`
measures that map per score and cube (`analysis.mwc_frame`), the writer converts
those plies through it, and the file holds BGBlitz's own MWCs with `met_id`
`bgblitz`. **Key it on the real cube value**: BGF's own `eq.cube` stays 1 after a
double, which is what first made the frames look inconsistent. Crawford
positions have no cube record, and there `matchEquity` is the cubeful MWC. We do
not ship BGBlitz's table, and don't need it: each file describes its own.

**Equity loss is derived where v2 derives it**: best minus played, when the
played move is listed. Fresh analysis carries full precision, so that is exact;
a document that already went through v1 (4-place values) moves by 1e-4 on ~5%
of decisions, once.

**Size.** The first cut was 9% *larger* than v1, because the block level was
`analysis_info.eval_level` (`3ply` for `fast`) while most decisions sit at
`2ply`, so nearly every decision and every luck record carried an override.
v2's block level means "the default for every decision", so it is now the label
most decisions share, with the block's own label in its annotation, and luck
records state their depth (`checker_ply`, one byte) rather than a label: corpus
total now **+2.8% raw, +6.9% deflated** against v1, for strictly more content
(1e-6 precision, unclamped equities, recorded zero probabilities). The deflated
cost is the 1e-6 MWC digits, which have no trailing zeros to compress.

**HedgeHog's own codec builds as a shared library with a C ABI**, so the rules
can be asked rather than inferred from prose:

```bash
cd ~/projects/hedgehog-public && make libogxm      # -> build/libogxm.so
```

`tests/ogxm2_oracle.py` binds it (`check_json`, `json_to_binary`,
`binary_to_json`, `v1_to_v2`, `is_field_name`) and holds no second copy of the
format. It is **not a dependency**: the library is a build artifact of a separate
MIT checkout, found if present and skipped if not, exactly as GammonView treats
its sibling repo. `LIBOGXM_PATH` overrides the search and is authoritative — set
to something that does not resolve, it fails rather than falling back. A library
built before v2 loads and then fails on the first v2 call, so the loader checks
its symbols and says "rebuild" instead.

**`tests/test_ogxm2_writer.py` is the gate.** Over every corpus match in every
form, plus synthetic two-block and resignation documents, it checks that
`read(write_v2(D))` equals `read(write_v1(D))` except where a **named rule**
explains the difference (one sentence each in the profile's §4 — anything
unexplained fails), that rewriting is byte-stable, and, with the library, that
the reference loads each file, finds no rule broken, replays it to the end and
re-encodes it to the **same bytes**. That last check is what caught the step
order (v2 replays steps in stored order and requires every intermediate position
legal — our `.mat` path put bar entries last) and the duplicate played move.

`tests/test_ogxm2_reference.py` is the older ratchet: the reference `v1_to_v2`
over each golden's content written as v1, against a **pinned** table. Never edit
a row to make a run pass. The goldens themselves are v2 now.

The two v1 parity suites, `test_gvab_writer` and `test_ogxm_conformance`,
compare against libogxm's *v1* writer and projection, and skip — saying so —
against a 2.x library, which writes and projects v2 only. They need a pre-v2
build to run.

**Another producer's v2 file is rewritten without losing what we don't model**
(spec I7). `read_ogxm2` attaches `_ogxm2_passthrough` — the source's own `MTCH`,
`GAME`, `ANAL`/`DECS`/`SIGN`, `CLCK`, `VIDO`, `MSIG`, foreign `ANNO` records and
unknown sections, base64 so it survives `.gva` — and the writer emits a part's
original bytes when the document still says what they say. That is decided by
fingerprint, not by a flag: each part is stamped with the SHA-256 of *our
writer's canonical encoding of it*, and a part that still hashes the same is
unedited. This is the signature argument: `SIGN`/`MSIG` digest the bytes as
stored, so re-encoding an unedited part could break a signature on bytes alone,
while an edit changes the canonical bytes and the stale original is never used.
Three cases: blocks added or removed keep everything (an unedited rewrite is the
source, byte for byte); a metadata edit re-encodes `MTCH` (keeping every field we
don't model and its unknown tail) and drops `SIGN`/`MSIG`; a move edit also drops
`CLCK`, `VIDO` and every ply-addressed annotation. Files we wrote get no record
(the writer already reproduces them), and `sha256.js` exists because
`crypto.subtle` is async and `write_gvab` is not. A v1 file's `SIGN`/`CLCK`/`VIDO`
chunks are written as the reference's `v1_to_v2` writes them. `docs/OGXM_V2_PROFILE.md`
§5 has the rules. `.gva` (JSON) is still our document's JSON; making it v2's JSON
projection is the planned second step.

## Checker play goes through `gvanalysis/checker_eval.py`

Not `analyzer.checker_play` directly. bgsage screens every legal move at 1-ply,
keeps a handful, evaluates those at N-ply — and then re-runs the N-ply
cube-aware tree over **every** candidate anyway. Screening ourselves evaluates
1.8x fewer moves for the same answer, which is the whole of the case as of
bgsage 2.0.

Through bgsage 1.x there was a second, sharper reason: the discarded candidates
were left labelled `"1-ply"` while carrying full-depth numbers, and that label
is not cosmetic — `game_eval` compares the played move's `eval_level` against
the best move's to decide whether the played move needs re-scoring, so a fossil
label fired that branch and replaced a correct error with a weaker
`post_move_analytics` estimate. On the sample corpus it moved two matches' PR
by 0.28 and 0.12. **Fixed upstream in 2.0**, and `test_screened_checker` now
asserts the fix rather than the bug. Keep the reasoning in mind anyway: any
consumer that reads `eval_level` is trusting a label, and we depend on it.

`checker_eval.checker_play(analyzer, board, d1, d2, ...)` screens at 1-ply
itself, elevates a chosen set through the same
`bgbot_cpp.cubeful_probs_and_equity_nply` call (so elevated moves match bgsage
to the last digit), and leaves the tail at honest 1-ply numbers with an honest
label. The best move agrees with bgsage's full-width answer on every decision
measured — 4 differences in 1567, all exact ties — while evaluating 1.8x fewer
moves.

Three constants tune it, all documented with their measurements in the module:
`ELEVATE_THRESHOLD` / `ELEVATE_MAX_MOVES` (the accuracy filter) and
`ELEVATE_MIN_MOVES` (a display-quality floor, so the top ten a viewer reads is
always N-ply). Rollout tiers, cubeless and 1-ply analyzers, and any bgsage whose
internals have moved all fall back to `analyzer.checker_play` unchanged;
`GVAN_NO_SCREEN=1` forces that fallback everywhere, which is how the A/B
measurements above were taken.

**Because it duplicates a bgsage call site, it inherits that call's signature.**
bgsage 2.0 added `root_board=` to `cubeful_probs_and_equity_nply` — Stage 11's
snake NN is chosen once from the root position and held for the whole tree,
where every other net is picked per node. Omitting it neither errors nor warns;
it evaluates a different function. Before we passed it, the best move disagreed
with bgsage on five of eight snake seeds, by up to 0.19 equity, while every
ordinary position in the parity suite stayed exact. `_has_root_routing()` probes
the signature so pre-2.0 bgsage still works, and
`test_screened_checker.check_root_routing` asserts parity holds *with* routing
and breaks without it. **On any bgsage upgrade, diff
`_CubefulAnalyzer._convert_move` against `_elevate` before trusting a green
suite.**

The upstream defects this works around are catalogued in
`docs/upstream-bgsage.md` — file them if bgsage ever takes issues. Issues 2 and
3 are fixed in 2.0; issue 1, the one that pays for this module, is not.

## Key bgsage API

```python
from bgsage import BgBotAnalyzer

analyzer = BgBotAnalyzer(eval_level="3ply", cubeful=True)

# Checker play (dice already rolled)
result = analyzer.checker_play(board, die1, die2, cube_value=1, cube_owner="centered")
# result.moves: list[MoveAnalysis], best first

# Cube action (pre-roll)
cube = analyzer.cube_action(board, cube_value=1, cube_owner="centered")
# cube.should_double, cube.should_take, cube.optimal_action

# Match play: add away1, away2, is_crawford to any call
```

Eval levels: `"1ply"`, `"2ply"`, `"3ply"`, `"4ply"`, `"rollout"`.
