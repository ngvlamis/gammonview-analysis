<p align="center">
  <img src="https://raw.githubusercontent.com/ngvlamis/gammonview-analysis/main/assets/logo.png"
       alt="" width="104" height="104">
</p>

<h1 align="center">GammonView</h1>

<p align="center">
  <a href="https://github.com/ngvlamis/gammonview-analysis/actions/workflows/ci.yml"><img src="https://github.com/ngvlamis/gammonview-analysis/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/ngvlamis/gammonview-analysis/actions/workflows/engine.yml"><img src="https://github.com/ngvlamis/gammonview-analysis/actions/workflows/engine.yml/badge.svg" alt="engine"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-MIT-green.svg" alt="Licence: MIT"></a>
</p>

The backgammon analysis backend behind
**[gammonview.com](https://gammonview.com)**: read and write the site's
`.gvab`/`.gva` match files, import `.mat`/`.xg`/`.bgf`, analyze a match or a
single position with the [bgsage](https://pypi.org/project/bgsage/) engine, and
compute per-decision error rate (PR), luck and cube statistics.

Those files are based on **OGXM** — the chunked match format from
[HedgeHog](https://gitlab.com/eranlambooij/hedgehog-public), not ours — extended
to carry what bgsage's analysis produces. This repo reads OGXM; what it writes is
that extended form. See [The format](#the-format).

It ships as two importable Python packages, plus a JavaScript port of the codec
for the browser:

- **`gvformat`** — the pure-stdlib codec: read/write the `.gvab` binary, `.gva`
  JSON conversion, `.xg`/`.bgf` import, compute-on-read stats, OGID. **Zero
  third-party deps** — `pip install gammonview` and `import gvformat` anywhere,
  no engine required. This is the format layer: everything else here, Python and
  JavaScript alike, reads and writes through it.
- **`gvanalysis`** — the [bgsage](https://pypi.org/project/bgsage/)-powered
  analysis: `analyze_match` (a match file in, analyzed `.gvab` bytes out),
  `analyze_file` (the same work stopping at the OGXM document), and single-position
  analysis. Depends on `gvformat` + bgsage (the `[engine]` extra). Dependencies
  flow one way: `gvanalysis` → `gvformat`, never back.
- **`@gammonview/gvformat`** — the same codec in JavaScript, on npm. What reads
  the analysis in the browser. See [In the browser](#in-the-browser).

## Install

To use the packages, install from PyPI — no clone needed:

```bash
pip install gammonview               # the codec alone (gvformat), no engine
pip install "gammonview[engine]"     # adds bgsage, for gvanalysis
```

To work on them, clone the repo and let [uv](https://docs.astral.sh/uv/) build
the environment — it installs both packages in editable mode, plus bgsage and
the test dependencies:

```bash
git clone https://github.com/ngvlamis/gammonview-analysis.git
cd gammonview-analysis
uv sync
uv run python tests/run_all.py --fast     # ~3s, no engine needed
```

Every command below is written `uv run …` for that checkout; from a plain
`pip install`, drop the prefix.

## Quickstart

Server one-liner — analyze a dropped `.mat` and get the analyzed `.gvab` back,
ready to store and ship to the client:

```python
from gvanalysis import analyze_match
data = analyze_match("match.mat", preset="world_class")   # analyzed .gvab bytes
```

It runs the engine over every decision, and the bytes carry the match *and* the
analysis. Any input the codec reads works — `.mat`, `.gva`, `.gvab` — and one
that already carries analysis keeps it, with ours appended.

It returns `.gvab` (binary), always — that is the artifact to store and ship.
For the JSON, `read_gvab(data)` is the `.gva`; to stop a step earlier and work
with the document itself, `analyze_file` returns it directly. (The CLI is the
other way round: `gvan-match` writes `.gva` unless asked for `--gvab`, because
a file you open by hand is likelier to want to be readable.)

## The format

The files this repo reads and writes — `.gvab` (binary) and `.gva` (JSON) — are
based directly on **OGXM** (OpenGammon eXtensible Match), the chunked match
format from [HedgeHog](https://gitlab.com/eranlambooij/hedgehog-public), and
extend it with what GammonView's viewer shows and OGXM has no field for: luck,
per-decision PR and illegal-move flags, and per-alternative eval levels — all in
one `GVAN` chunk — plus match metadata like `event`, `site` and `cube_limit`.

**We read OGXM.** `read_gvab` takes a file this repo never wrote, including one
carrying another engine's analysis, and `gvan-match` appends its own analysis
block rather than replacing what is already there.

We make no claim in the other direction, and that is about us, not about OGXM.
One of our choices is a genuine departure: in match play we store **normalized
equity** in the three cube equity fields, where the base spec specifies **raw
MWC**. We do it for quantization — these fields are an `int16` at 1e-4, which
gives normalized equity about 20,000 usable steps at any score, while raw MWC
has as few as **16** at a lopsided one — and we recover MWC on read from the
shipped MET. It is the right trade for us and it is still a difference a reader
cannot see: same fields, same layout, different numbers.

So: treat these as GammonView files that an OGXM reader may well be able to make
partial sense of, not as OGXM files with extras. The two spec documents describe
**what this repo writes** — the base spec with every addition marked
`[GammonView extension]` and every divergence marked, including where a base
reader would go wrong. The upstream specs they track are in HedgeHog's `docs/`
and remain the authority on the base format.

## In the browser

The codec has a JavaScript twin, published to npm as
**[`@gammonview/gvformat`](https://www.npmjs.com/package/@gammonview/gvformat)**:

```bash
npm install @gammonview/gvformat
```

```js
import { read_gvab, convert_mat, parse_ogid } from '@gammonview/gvformat';

const match = read_gvab(bytes);           // .gvab -> .gva JSON
const dropped = convert_mat(matText);     // .mat  -> .gva JSON, no analysis
const pos   = parse_ogid(match.games[0].plies[0].ogid_before);
```

An ESM port of `gvformat` — the same reads, writes, source converters and OGID
handling, with **no engine, no WebAssembly and no build step**. It is how
gammonview.com parses a dropped `.xg`/`.bgf`/`.mat` client-side and renders the
`.gvab` the Python side produced.

The port lives in [`gvformat-js/`](gvformat-js/) and is maintained here rather
than downstream. Python and JavaScript must agree byte-for-byte — the same
`.gvab` in gives the same `.gva` JSON out — and the two test suites check that
from either side.

There is no JavaScript port of `gvanalysis`: analysis needs the engine, and the
engine is Python-side only.

## Command-line tools

Entry points are defined in `pyproject.toml` and run with `uv run`:

| CLI | What it does |
|---|---|
| `gvan-match` | Analyze a match (`.mat`, `.gva`/`.ogxm`, `.gvab`), compute per-move PR → `.gva`/`.gvab` |
| `gvan-batch` | The same over many files or a directory, one output each |
| `gvan-position` | Analyze a single position from an **XGID or OGID** |
| `xg2gva` | Convert an eXtreme Gammon `.xg` file to `.gva` JSON (no engine) |
| `bgf2gva` | Convert a BGBlitz `.bgf` file to `.gva` JSON (no engine) |

```bash
uv run gvan-match match.mat --preset world_class --gvab   # binary out, not .gva
uv run gvan-match match.gvab --preset fast                # re-analyze a binary, append
uv run gvan-match match.mat --link                        # print a shareable URL
uv run gvan-batch matches/ --preset fast                  # a dir -> its match files
uv run gvan-position "XGID=-b----E-C---eE---c-e----B-:0:-1:1:31:0:0:0:0"
uv run gvan-position "11ccccchhhjjjjj:66666888dddddoo:N0N:13:B:R:0:0:0:0"
```

An OGXM input keeps any analysis it already carries — ours is appended as a new
block rather than replacing it. `--link` encodes the whole match into a
`gammonview.com` URL fragment, so it needs no hosted file; `--browser` opens it.

### Analysis presets

`--preset` selects an eXtreme Gammon–style scheme: a cheap first pass screens
every decision, and a stronger pass runs only where it is needed. The six
built-ins:

| Preset (aliases) | 1st pass | Middle tier | 2nd pass | XG equivalent |
|---|---|---|---|---|
| `very_quick` (`vq`) | `2ply` | — | — | Very quick |
| `fast` (`f`) | `2ply` | — | `3ply` | Fast |
| `deep` (`d`) | `3ply` | — | — | Deep |
| `balanced` (`b`) | `2ply` | `3ply` (both kinds) | `truncated2` | — (quality/speed) |
| `world_class` (`wc`) | `4ply` | — | `truncated2` | World Class (XG Roller+) |
| `world_class_fast` (`wcf`) | `3ply` | `truncated2` (**cube only**) | `truncated2` | World Class (3-tier) |

The second pass runs on an **error**; the middle tier runs on a **borderline**
decision the screen would otherwise have settled. `fast` is the default.

#### Which one to use

493 matches were analyzed by XG at World Class and by each of the three strong
presets, then compared decision by decision:

| | mean gap to XG's match PR | same checker play as XG | PR within 1 of XG | relative cost |
|---|---|---|---|---|
| `world_class` | 0.434 | 90.3% | 91.8%             | 45× |
| `world_class_fast` | 0.441 | 90.0% | 90.7%             | 24× |
| `balanced` | 0.540 | 89.2% | 86.7%             | 6× |

**Mean gap** is the average distance between the two *ratings* of the same
player-match, in either direction: rate a match with `world_class` and XG rates
it 0.434 PR away, typically. It is not what following the preset costs you —
priced by XG's own equities, a player who made `world_class`'s recommended
checker play every time would post a PR of 0.333, `world_class_fast` 0.353,
`balanced` 0.412. All three are inside what XG itself calls world-class play.

Cost is mean wall clock relative to `fast` over four matches, and varies with
the match — see [the timings](docs/CLI.md#what-they-cost).

- **`world_class_fast` is the recommended analysis.** It lands 0.007 PR behind
  the deepest preset for a little under half the time. The two sit three times
  closer to each other (0.136) than either does to XG, because what separates
  them from XG is the *engine*, not the search depth — 95% of
  `world_class_fast`'s squared PR error against XG is that engine floor, which
  no preset can spend its way past.
- **`world_class` is for reproducing XG specifically**, not for being more
  accurate. Its only real edge is on borderline cubes, and it gets there by
  rolling them out *less* often than `world_class_fast` does — which is
  XG-like, not demonstrably better. It costs roughly twice as much for that.
- **`balanced` is the quality/speed option**, and the only one of the three with
  real headroom: upgrading it recovers eight times as much error as upgrading
  `world_class_fast` does.

Every number above measures *similarity to XG*, which is not the same as being
right — where the two engines differ there is no arbiter here to say which is
correct. **[`docs/PRESET_ACCURACY.md`](docs/PRESET_ACCURACY.md)** is the full
experiment: how the presets split on checker play versus the cube, why rolling
out *costs* agreement with XG, and the evidence that the residual disagreement
is systematic rather than noise.

Presets live in `gvanalysis/presets.py` and are overridable via `presets.yaml`.
See **[`docs/CLI.md`](docs/CLI.md)** for the full reference — every flag, how the
tiers fire, parallelism, and preset overrides.

## Documentation

| Doc | What it covers |
|---|---|
| [`docs/CLI.md`](docs/CLI.md) | **Full command-line reference** — every flag, presets, parallelism |
| [`gvformat-js/README.md`](gvformat-js/README.md) | The JavaScript package — API, layout, parity rules |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to run the tests, the conventions, what a good change looks like |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history, including the measurements behind several design decisions |
| [`CLAUDE.md`](CLAUDE.md) | Maintainer and coding-agent notes: design rationale, measurements, and the rituals around an engine upgrade |
| [`docs/OGXM_JSON_SPEC_GAMMONVIEW.md`](docs/OGXM_JSON_SPEC_GAMMONVIEW.md) | The JSON shape this repo writes (match → game → ply → analysis) — base spec plus every GammonView addition, marked |
| [`docs/OGXM_FORMAT_SPEC_GAMMONVIEW.md`](docs/OGXM_FORMAT_SPEC_GAMMONVIEW.md) | The binary `.gvab` chunk layout, including the `GVAN` extension chunk |
| [`docs/OGXM_COMPUTED_FIELDS.md`](docs/OGXM_COMPUTED_FIELDS.md) | Fields the reader computes rather than stores (PR, luck-MWC, MWC, classification) |
| [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) | PR / luck / cube-decision methodology and filters |
| [`docs/PRESET_ACCURACY.md`](docs/PRESET_ACCURACY.md) | What each preset costs in accuracy, measured against XG over 493 matches |

## Licence

[MIT](LICENSE). Every source file carries an SPDX header; `LICENSE` holds the
full text.

MIT because what this repo mainly offers is a **format**, and a format wants to
be copied in pieces — one routine lifted into another project, or the codec
ported to another language. It also stays compatible in both directions at once,
which matters in this ecosystem: GNU Backgammon is GPL, eXtreme Gammon and
BGBlitz are proprietary, and all of them are welcome to interoperate.
[HedgeHog](https://gitlab.com/eranlambooij/hedgehog-public), whose format this
implements, is MIT too.

[bgsage](https://pypi.org/project/bgsage/), the engine behind the optional
`[engine]` extra, is MPL-2.0, which constrains nothing here. MPL is *file-level*
copyleft with no linking trigger: it attaches to its own files, and §3.3
expressly permits distributing a larger work under other terms. bgsage resolves
from PyPI at install time and is never modified or redistributed here, and
`gvformat` does not touch the engine at all.

The JavaScript mirror ([`gvformat-js/`](gvformat-js/), published as
`@gammonview/gvformat`) is under the same licence and ships its own copy.

The **GammonView logo** (`assets/logo.png`) is excluded: it identifies the site,
so it is not part of the MIT grant. Fork the code freely; use your own mark.

### Third-party material

Two pieces of third-party material are incorporated, both permissively licensed.
Their notices must be preserved — see **[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)**,
which reproduces them in full:

- **HedgeHog** (MIT, © 2026 Eran Lambooij) — OGXM is HedgeHog's format. Most of
  `gvformat` is an independent implementation, but `binary.py` and `export.py`
  (and their JS mirrors) are genuine ports of its C++ sources: the chunk struct
  layouts and `parse_ply_analysis_json` from `ogxm_io.cpp`/`ogxm_json.cpp`, and
  `_TurnState` from `ogxm_replay.cpp`'s `replay_game()`. Those four files say so
  in their headers.
- **Kazaross-XG2 MET** (© 2011 Neil Kazaross; transcribed for GNUbg by Michael
  Petch) — the match-equity table embedded in `gvformat/met.py` and
  `gvformat-js/src/met.js`. It ships with GNU Backgammon but is *not* under its
  GPL; its own terms require only that the notice be preserved, and it is
  reproduced at the top of both files.
