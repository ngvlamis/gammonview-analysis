<!--
SPDX-License-Identifier: MIT
Copyright (C) 2026 Nicholas Vlamis
-->

# The sample corpus

Eleven matches in four formats, committed so the test suite can run anywhere.
Every player name, match ID, date and site string here is synthetic — see
[Anonymization](#anonymization).

Most of what they buy is breadth. The suite replays **2,101 plies with 14,032
alternatives** through the XG converter and the same 2,101 plies with 11,791
alternatives through the BGF one, and the two bugs this directory exists to
catch — the XG and BGF step splitters inferring the wrong intermediate point —
were both found by a board shape that no hand-picked match happened to contain.
This is why the corpus is chosen for coverage rather than trimmed to a minimum.

**Everything here is portable.** Seven tests read these files and none of them
imports bgsage: parsing, byte round-trips and board replay are pure stdlib and
land identically on any machine. That is worth separating from
`tests/golden/`, which is engine output and does *not* travel — see
[Goldens are not part of this corpus](#goldens-are-not-part-of-this-corpus).

Run `uv run python tests/audit_corpus.py samples` to check the corpus against
everything below. It needs no engine, and it is the fastest way to find out
whether a corpus change broke a requirement.

## What is here

| Directory | Files | Read by |
|---|---|---|
| `mat/` | 11 `.mat` | `test_ogxm_pipeline`, `test_count_illegal`, and every engine test via `tests/fixtures.py` |
| `xg/` | 11 `.xg` | `test_xg_move_steps`, `test_alternative_move_steps`, `test_xg_eval_levels`, `test_xg_zero_win_eval` |
| `bgf/` | 11 `.bgf` | `test_bgf_move_steps` |
| `gv/` | 11 `.gvab` + 1 `.gva` | `test_share_link`, `test_chunk_passthrough` |

The JS suite reads `mat/`, `xg/` and `bgf/` from here too — see
[The JavaScript mirror](#the-javascript-mirror).

`mat/`, `xg/` and `bgf/` are source data. `gv/` is pipeline *output* — don't
curate it, regenerate it (see [below](#regenerating-the-derived-files)).

**Why `gv/` ships one `.gva` and eleven `.gvab`.** The JSON is fully derivable:
`.gva == read_gvab(.gvab)`, because both come out of a single `write_gvab`.
Shipping all eleven as JSON cost 4.0 MB against 340 KB for the binaries, so the
binaries carry the corpus and one readable copy stays behind —
`B4_SrGcsKAQmoTyHlgJCbM`, the smallest. It earns its place twice: it keeps
`test_share_link`'s section 5 (`.gvab` and `.gva` encode to the same payload)
alive, which is the one thing shipping both forms can catch, and it gives
anyone reading `docs/OGXM_JSON_SPEC_GAMMONVIEW.md` a worked example they can
open in a text editor.

## What a replacement corpus must contain

Almost every requirement is collective: it constrains the corpus as a whole,
not each file. `tests/audit_corpus.py` checks all of them.

### `xg/` — the eval levels are the fragile part

* **At least one alternative with `win == 0`**, and at least one with **all
  five probabilities zero**. `test_xg_zero_win_eval` asserts both, because the
  converter used to decide "did XG evaluate this?" by testing `win > 0` and so
  blanked the probabilities on every play in a hopelessly lost position. The
  last plies of a lost game supply these for free (today: 238 and 116).
* **All four non-ply eval levels must appear across the corpus** —
  `truncated1`, `truncated2`, `truncated3` and `database`.
  `test_xg_eval_levels` checks each by name, precisely so the round-trip check
  above it cannot pass on a corpus that never exercised the level. These are
  the codes that used to encode as 0 and read back as the header's plain ply
  depth, silently.

  The mapping is to XG's **analysis level**, not to its rollout command:

  | XG setting | code | stored as |
  |---|---|---|
  | XG Roller | 1000 | `truncated1` |
  | XG Roller+ | 1001 | `truncated2` |
  | XG Roller++ | 1002 | `truncated3` |
  | opening book | 998/999 | `database` |
  | Rollout | 100 | `rollout` |

  A match analyzed at a plain ply depth yields none of the three Roller levels,
  so this is the requirement a re-analysis loses first. It does **not** need
  three files: re-analyzing a single ply overrides just that ply's
  alternatives, so one file can carry several levels.
  `B4_SrGcsKAQmoTyHlgJCbM` is that file here — it alone supplies `truncated1`,
  `truncated2`, `truncated3` and `rollout`, on four separate plies, on top of
  the `1ply`/`2ply`/`3ply`/`database` its match-wide pass produced.

### `bgf/` — breadth only

No content assertion beyond "the corpus is present and its plies carry dice".
The property under test is the whole-file invariant that a ply's `moves`
replay onto its own `ogid_after`, which held for every sample in the corpus
when the splitter was still wrong — the shape that exposes it is rare, and
more files is the only defence. Keep both match play and money sessions:
BGBlitz's `luckPlain`/`luckWeighted` swap meaning by `mode`, so the two kinds
exercise different fields.

### `mat/` — the one match every engine test analyzes

`tests/fixtures.py:MATCH` names it. Two requirements are easy to miss:

* **Match play, not a money session.** `test_reconstruct_mat` compares a
  derived `score_start` against the original for every game; a `0 point match`
  has no running score to derive and every game mismatches. Four of the eleven
  here are money sessions.
* **A double, a take *and* a drop** (action_ids 21, 22, 23).
  `test_ogxm_export` looks for all three and checks each one's `ogid_after`
  state letter (`D`/`O`, `A`/`T`, `G`/`P`). A match whose cube is never
  dropped does not fail that check — it silently stops exercising the drop
  branch, which is the failure mode the explicit lookup exists to prevent.
  `eXBNG5wZHS5Alu3P` and `XCu3RJ0UvDg_Tldl` are the two that fall short.

Five of the eleven satisfy both. The current pick is
`3WNK_g1Z-PLsh_HyvQ5j4a` — 7-point, 7 games, 272 plies, the longest.

There is deliberately no fallback to "whatever `.mat` is present". A silent
substitution is how the drop branch would stop being tested without anyone
noticing; a loud skip naming `MATCH` is the better failure.

### The two matches that are not like the others

* **`5nqfGw9bWG3deTaU`** contains a genuine **illegal play** — `14/10 13/12`
  off a 1-3, three die-moves for a two-hop roll — and is the only file in the
  corpus that exercises `illegal_move` end to end. Two tests exist for that
  path and both name this match: `test_illegal_move` (the flag through the
  binary and into `compute_aggregates`, no engine) and `test_count_illegal`
  (what `--count-illegal` does to PR). Without it they skip, and the
  `illegal_moves` counter goes back to being checked only as `0 == 0`. It is also transcribed
  rather than server-exported, so it carries an `[Event]` distinct from its
  `[Site]` and has no `[Match ID]` or `[Cubelimit]`: a header shape the other
  ten do not provide. It found the bug that `export.fit_move_steps` now fixes.
  Its `.gvab` is a golden, so the flag is pinned byte-for-byte.
* **`B4_SrGcsKAQmoTyHlgJCbM`** carries the Roller levels and the rollout, as
  above, and is the readable `.gva`.

Losing either one costs a requirement no other file supplies.

## Names the suite hard-codes

Four places, all of which `tests/audit_corpus.py` verifies:

1. **`tests/fixtures.py:MATCH`** — the engine tests' match.
2. **`tests/golden/*.fast.gvab`** — five stems, each needing a matching
   `mat/<stem>.mat`, or `test_ogxm_pipeline` and `regen_golden.py` skip it.
3. **`tests/test_chunk_passthrough.py`** and **`tests/test_share_link.py`** —
   both name `B4_SrGcsKAQmoTyHlgJCbM` literally, and between them need its
   `.gvab` and its `.gva`, both in `gv/`.
4. **`tests/test_illegal_move.py`** and **`tests/test_count_illegal.py`** —
   both name `5nqfGw9bWG3deTaU`, the one match with an illegal play; the first
   reads its golden `.gvab`, the second its `.mat`. It is already a golden
   stem, so check 2 covers the name.

## Regenerating the derived files

After changing the source matches:

```bash
uv run gvan-batch samples/mat/*.mat --preset fast --gvab --out-dir samples/gv/
uv run gvan-match samples/mat/B4_SrGcsKAQmoTyHlgJCbM.mat --preset fast \
    -o samples/gv/B4_SrGcsKAQmoTyHlgJCbM.gva        # the one readable copy
uv run python tests/regen_golden.py --check         # what would move
uv run python tests/regen_golden.py                 # rewrite them
uv run python tests/audit_corpus.py samples         # and check the result
```

Changing `mat/` also means regenerating the JS parity references — see
[The JavaScript mirror](#the-javascript-mirror).

Read the golden diff rather than accepting it: a golden only certifies the
pipeline if someone looked at what moved.
`regen_golden.py` derives its stem list from the files already in
`tests/golden/`, so adding a *new* golden means writing its pair once by hand.

## Goldens are not part of this corpus

`tests/golden/` is bgsage output, and bgsage is **not bit-reproducible across
CPU architectures**. Measured 2026-09-13 on Linux x86_64 against goldens
generated on macOS arm64, with the same bgsage build (2.0.20260907) on both:
the full suite came back 25 passed / 2 skipped / 2 failed, and the only
failures were the exact comparisons — `test_ogxm_pipeline`'s byte-parity checks
and `test_luck_fill`'s identical-float demand. Every structural check in those
same two files passed.

The divergence is not purely rounding. Across four goldens one best move of 521
changed, one cube verdict of 296 changed, the largest equity gap was 0.0649,
and on one match a player's PR moved 6.448 → 6.246. The interpreter is not the
variable — the goldens reproduce across Python 3.10 and 3.13 on the machine
that owns them.

That is not something to discover twice, so both exact comparisons are gated on
`tests/golden/PROVENANCE.json` — the platform stamp `regen_golden.py` writes
beside the goldens. Off that machine they skip by name and everything
structural in the same two files still runs, so a different CPU produces `SKIP`
lines rather than a red run. The corpus in *this* directory is unaffected, and
is exactly the part of the suite's coverage that does travel.

One consequence worth knowing: the reference OGXM codec replays with the rules
enforced, so `5nqfGw9bWG3deTaU` makes it report `replay_complete: false` and
derive no OGIDs. `test_ogxm_conformance` expects that, and checks the file
really does hold an illegal play before accepting it.

## Anonymization

The matches are real games, so every identifying field was replaced. Player
handles are length-preserving and order-preserving, which matters for two
reasons: the `.mat` score line pads player 2 to a fixed column, and **white is
the alphabetically-first name** (`gvformat.export._canonical_orientation`), so
a reordering would mirror every board and swap `score_start` throughout.

Identifying data lives in:

* **`.mat`** — `Player 1`/`Player 2`, `Site`, `Match ID`, `EventDate`,
  `EventTime`, and each game's score line. Plain text.
* **`.xg`** — the *uncompressed* header record: ANSI Pascal strings at offsets
  9 and 50, UTF-16 `TShortUnicodeString`s at 880 and 1138, ELO ratings at 104
  and 112, the date at 128, event at 136/622, location at 1396. All
  fixed-width, so an in-place patch never shifts a length. The embedded JPEG
  thumbnail is a rendered board only — no names.
* **`.bgf`** — inside the gzip/zlib Smile payload. `gvformat` decodes Smile but
  has no encoder, so this is a byte patch on the decompressed stream, where
  strings are length-prefixed.

The `.xg` and `.bgf` files here were re-analyzed from the anonymized `.mat`
rather than patched, which is why they carry no trace of the originals.

## The JavaScript mirror

`gvformat-js/` reads **this** directory — the same files, not a copy. It used
to keep its own `gvformat-js/samples/`, which drifted and, being covered only
by a *nested* `.gitignore`, shipped the pre-anonymization corpus inside the
Python sdist: hatchling honours the root `.gitignore` and ignores nested ones.

One thing the JS side does keep: `gvformat-js/test/fixtures/mat/*.json`, a
`mat_to_ogxm` output per match. Those are expectations rather than corpus —
`test-mat.js` asserts `convertMat` reproduces them, and JavaScript cannot
generate them. **Regenerate them whenever `mat/` changes**, with the command in
that file's header; `audit_corpus.py` fails if a `.mat` has no reference or a
reference outlives its `.mat`. A missing one makes `test-mat.js` skip that
match rather than fail, so the audit is what catches it.

`samples/og/` is the one uncommitted exception, gitignored: `test-og.js`'s
parity section replays live OpenGammon API data, which carries real handles.
