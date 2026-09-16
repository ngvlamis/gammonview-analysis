# OGXM Format Specification — GammonView Extended

Based on `OGXM_FORMAT_SPEC.md` from [HedgeHog](https://gitlab.com/eranlambooij/hedgehog-public) (`docs/`). Additions specific to bgsage analysis output are marked **[GammonView extension]**. Compatibility notes explain which changes require a version bump.

## OpenGammon eXtensible Match (OGXM) — Version 1.3 + GammonView Extensions

A compact binary format for storing backgammon matches with optional analysis data, following the same TLV chunk pattern as OGXF.

**Version history:** 1.1 added the set-position ply; 1.2 added optional illegal-play dice on it; 1.3 allows **multiple analysis blocks** (one per model/engine) and a per-analysis **SIGN** chunk. Since 1.3, the base spec made two additive-value additions without a version bump: `source = 4` (`bgf_import`) and cube `type = 4` (`live_checker`, a live double/take/pass decision stacked on a checker ply — see the base spec's changelog). **GammonView extensions** (this document) add the analysis output of the GammonView engine (bgsage). Almost everything GammonView adds lives in a single self-contained ancillary chunk, **`GVAN`** (GammonView ANalysis), that binds to its `ANAL` block like EVAL/ALTS/CUBE. Base readers skip it by length; the base MHDR/GAME/ANAL/EVAL/ALTS/CUBE chunks stay byte-identical to the reference codec, with one deliberate exception — an `equity_loss` above 1.0, which the reference codec truncates and GammonView does not (see [Fixed-Point Encoding](#fixed-point-encoding)). It fills a field the base format already defines, in the direction where zero means absent, so the reference codec reads either file. Byte-identity is a statement about *encoding*, not about units: the three cube equity fields are a **producer** convention, and GammonView writes normalized cubeful equity in them where the base spec now writes raw MWC for match play — the chunk layout is unchanged and the codec is unaffected, but a base reader's derived `*_norm_eq` values come out wrong. See the JSON spec's [Cube equities are normalized equity, not MWC](OGXM_JSON_SPEC_GAMMONVIEW.md#cube-equities-are-normalized-equity-not-mwc-gammonview-divergence) for what survives the mismatch and why the unit was chosen. (There used to be a second exception: the `probs` of a CUBE `type=2` (missed double), which the reference left zero and GammonView filled. The base spec adopted that in its 2026-08-10 sync — `probs` now "carry the evaluation behind them when the producer supplied one" — so the two writers agree and the CUBE chunk is byte-identical again.) Base spec **1.4** adds the ancillary `VIDO` chunk; gvformat does not decode it, but no longer drops it either (see [Chunk Passthrough](#chunk-passthrough)). The result is that **every GammonView analysis extension is structurally additive** — no version bump, no `min_reader_minor` change, and a base reader can walk the chunk stream. We do not make the stronger claim that a base reader *understands* a GammonView file: the cube equity units alone defeat it. See [Extension Compatibility](#extension-compatibility).

**GammonView's `cube_decision` (correct/non-error live cube) and `missed_double` JSON sub-objects both map onto the base `CUBE` chunk** — `type=4`/`type=2` respectively, as the reference `ogxm_json.cpp` writes them (with one filled-in field on `type=2`, below). There is no GammonView-only "no-double section" in `GVAN` for this; an earlier draft of this document proposed one before the maintainer added `CUBE type=4` upstream, and that proposal is obsolete. `GVAN` now carries only the parts the base format has no field for: `luck`, `decision`/`illegal_move` flags, and per-alt/per-cube `eval_level`. It carries no per-decision MWC anchors either (GVAN v2) — MWC is a compute-on-read quantity everywhere, derived from `(equity, score, cube)` via the one MET shipped in `gvformat/met.py` (Kazaross-XG2), not stored in the format at all.

---

## Design Principles

1. **Sequential access**: Stream-friendly, no random access needed
2. **Moves, not positions**: Store compact ply records; reconstruct positions by replay
3. **Analysis is ancillary**: ANAL/EVAL/ALTS/CUBE/GVAN chunks can be stripped without breaking match data
4. **Fixed-point compact**: Probabilities as uint16, equities as int16 — half the storage of float32
5. **Single C++ implementation**: Used from JS (WASM) and Python (ctypes) via JSON conversion
6. **Rewrite-on-update**: Files are small (< 1 MB); reanalysis rewrites the entire file
7. **DB-friendly**: Stored as PostgreSQL `bytea`; searchable metadata kept in separate DB columns

---

## File Structure

```
OGXM file:
  ├── File Header (20 bytes)
  ├── MHDR — Match header (players, length, date)              CRITICAL
  ├── GAME — Game 0 (compact ply records)                      CRITICAL
  ├── GAME — Game 1                                            CRITICAL
  ├── ...
  ├── ANAL — Analysis 0 header (ply, model, timestamp)         ANCILLARY
  ├── EVAL — Checker evaluations (analysis 0)                  ANCILLARY
  ├── ALTS — Alternative moves (analysis 0)                    ANCILLARY
  ├── CUBE — Cube analyses (analysis 0)                        ANCILLARY
  ├── GVAN — GammonView analysis extensions (analysis 0)       ANCILLARY  [GammonView]
  ├── SIGN — Signature (analysis 0)                            ANCILLARY
  ├── ANAL — Analysis 1 header (a second model)                ANCILLARY
  ├── ...   — EVAL/ALTS/CUBE/GVAN/SIGN for analysis 1          ANCILLARY
  ├── CLCK — Clock data (delta-encoded timestamps)             ANCILLARY
  ├── CSUM — Checksum                                          CRITICAL
  └── End Marker (8 bytes)
```

**Ordering rules:**
1. File Header always first
2. MHDR must be first chunk
3. GAME chunks in game order (0, 1, 2, ...)
4. **Zero or more analysis blocks**, each an ANAL followed by its EVAL, ALTS, CUBE, GVAN, SIGN (each ancillary chunk may be omitted if empty). Each EVAL/ALTS/CUBE/GVAN/SIGN binds to the most recent ANAL. Order is significant: analysis 0 is the primary. Bounded by `MAX_ANALYSES` (16). `GVAN` (a GammonView extension) is placed after CUBE so its positional arrays align with the EVAL/ALTS/CUBE entries it annotates.
5. CLCK chunk (if present) after analysis chunks
6. CSUM last chunk before End Marker

A file carrying **more than one** analysis sets `min_reader_minor = 3`: pre-1.3 readers reject the second ANAL outright, so they must fail as a clean version mismatch. A single analysis - even a signed one - stays readable by older readers.

The check runs in both directions. `read_gvab` / `readGvab` refuse a file whose
`min_reader_major`/`min_reader_minor` exceeds the version they implement
(currently 1.3), raising `GvabError` before any chunk is walked -- the same test
the reference codec applies (`ogxm_io.cpp`, `VERSION_MISMATCH`). Writing the
field but ignoring it on read is the failure mode it exists to prevent: a later
layout would otherwise be parsed optimistically and yield silent garbage rather
than an error.

---

## Extension Compatibility

**Tiering here is about chunk structure only.** Every GammonView extension is Tier 1 in that sense — it costs no version bump, and a reader that does not know it can still walk the chunk stream. That is a narrower statement than "compatible", and it is the only one this section makes: it says nothing about whether another implementation *interprets* the file correctly. One difference is outside its scope entirely — the cube equity fields carry normalized equity where the base spec now specifies raw MWC, a producer convention no structural rule can catch (see the JSON spec's [Cube equities are normalized equity, not MWC](OGXM_JSON_SPEC_GAMMONVIEW.md#cube-equities-are-normalized-equity-not-mwc-gammonview-divergence)). Structural Tier 1 is achieved by two rules:

1. **Match metadata** reuses fields the base spec already reserved, or appends into a chunk's already-tolerated trailing region. These describe the match itself (present even without analysis), so they stay inline:
   - MHDR flags byte: add `beaver` (bit 2) and `raccoon` (bit 3) — old readers see reserved bits; no harm
   - MHDR reserved uint8[8]: carve `cube_limit` from bytes 0–1 — old readers treat as reserved
   - MHDR variable part: append `event`, then `site`, after `player_black` — old readers stop after names; forward extension in the variable part is safe (a reader that knows only `event` likewise stops before `site`)
   - GAME chunk: **no** GammonView bytes — it is byte-identical to the reference codec. (Equity↔MWC conversion is compute-on-read via the shipped MET, gvformat/met.py — it needs no per-game or per-decision anchor at all, so GAME carries no trailing bytes.)

2. **All analysis extensions the base format has no field for** live in the `GVAN` ancillary chunk — never in base EVAL/ALTS/CUBE/ANAL. Old readers skip the unknown chunk by its length (see base spec: "a single analysis stays readable by older readers, because SIGN is ancillary and simply skipped"). This covers `luck`, per-decision `decision`/`illegal_move` flags, and per-alt/per-cube `eval_level`. The base EVAL (20 B, with a per-decision `ply`), ALTS (17 B), CUBE (28 B, with `type=4` for a correct/non-error live cube and `type=2` for a missed double), and ANAL (`ply` = plain depth) chunks are **byte-identical to the reference codec**, except that EVAL/CUBE carry an unclamped `equity_loss` above 1.0 where the reference codec would truncate to `10000` (see [Fixed-Point Encoding](#fixed-point-encoding)). (A CUBE `type=2`'s missed-double `probs` used to be a second such divergence; the base spec adopted it in its 2026-08-10 sync, so it no longer is.) GammonView writes no GVAN-only cube record for either case; both round-trip through base CUBE.

There are **no Tier 2 (structurally breaking) changes.** No file carrying GammonView analysis needs `min_reader_minor` above what the base match data already requires. A file with GammonView analysis is a plain OGXM 1.3 file with one extra ancillary chunk per analysis block. Concretely: every sample in `samples/gv/` declares version 1.3 and stamps `min_reader 1.0` while carrying a GVAN chunk, because GVAN is written with the critical bit clear and the floor is raised only by base match content (set-position ply -> 1.1, its illegal-play dice -> 1.2, a second analysis block -> 1.3). A reader that skips GVAN loses exactly what GVAN holds -- `luck`, the `decision`/`illegal_move` flags, and per-alt/per-cube `eval_level` -- and reads the match, moves, evaluations, alternatives and cube records normally from the base chunks. Note this is a floor on *structural* readability only: our files lean heavily on base `CUBE` `type=4`, which the base spec added after 1.3 as an additive value with no version bump (1,058 of the 1,219 cube entries in `samples/gv/` are `type=4`). A 1.0-1.3 reader unpacks those 28-byte entries without error and has no meaning to attach to the type. The stamp says what the file *demands*, not what a reader needs to understand it.

---

## Shared Infrastructure (reuse from OGXF)

### File Header (20 bytes) — same struct as OGXF, different magic

| Offset | Size | Type   | Description |
|--------|------|--------|-------------|
| 0      | 4    | uint32 | Magic: `"OGXM"` (0x4D58474F LE) |
| 4      | 2    | uint16 | Version major (1) |
| 6      | 2    | uint16 | Version minor (3) |
| 8      | 2    | uint16 | Min reader version major (1) |
| 10     | 2    | uint16 | Min reader version minor (0; 1 with set-position ply, 2 if one carries dice, 3 if >1 analysis). GammonView analysis adds no requirement here — the `GVAN` chunk is ancillary. |
| 12     | 4    | uint32 | Total file size |
| 16     | 4    | uint32 | Flags: bit 0 = HAS_ANALYSIS |

### Chunk Header (12 bytes) — identical to OGXF

| Offset | Size | Type   | Description |
|--------|------|--------|-------------|
| 0      | 4    | uint32 | Chunk type code |
| 4      | 4    | uint32 | Data length (excludes this 12-byte header) |
| 8      | 2    | uint16 | Flags: bit 0 = CRITICAL |
| 10     | 2    | uint16 | Reserved (0) |

### End Marker (8 bytes) — identical to OGXF

| Offset | Size | Type   | Description |
|--------|------|--------|-------------|
| 0      | 4    | uint32 | `"END!"` (0x21444E45 LE) |
| 4      | 4    | uint32 | File size (must match header) |

### CSUM — identical to OGXF

Reuse chunk type `0x4D555343`, same CRC32/SHA-256 algorithms, same coverage (bytes 0 to CSUM chunk header start).

---

## Fixed-Point Encoding

| Type | Range | Encoding | Precision |
|------|-------|----------|-----------|
| **Probability** (uint16) | [0.0, 1.0] | `round(p * 10000)` → [0, 10000] | 0.0001 |
| **Equity** (int16) | [-3.0, +3.0] | `round(eq * 10000)` → [-30000, +30000] | 0.0001 |
| **Equity loss** (uint16) **[GammonView extension]** | [0.0, 6.5535] | `min(65535, round(loss * 10000))` → [0, 65535] | 0.0001 (clamped) |
| **MWC** (uint16) [bgsage] | [0.0, 1.0] | `round(p * 10000)` → [0, 10000] | 0.0001 |

### Equity loss: a wider bound than the base spec **[GammonView extension]**

The base spec clamps `equity_loss` at **1.0**, and its reference codec
(`encode_equity_loss` in `ogxm_format.hpp`) still does. GammonView's writers
clamp at **6.5535** instead — the largest value the `uint16` field can hold at
1e-4 resolution. We do not control the reference codec, so this is a deliberate
divergence on our side, not a change to the base format.

The 1.0 bound truncated any blunder past a point of equity, which a wrong take
of a large double clears easily — a real match had one costing 1.8096 stored as
1.0, dropping that player's match error from 3.85 to 3.05. Worse than a wrong
statistic: `played_equity` is *derived* (`best_equity - equity_loss`), so a
truncated loss silently moved the played equity too, leaving a record that
disagreed with its own alternatives, which encode via **Equity** and were never
clamped so tightly.

**This stays Tier 1.** Decoding is a plain divide on every implementation —
`decode_equity_loss` in the reference codec, `_q()` in gvformat-js — and none of
them range-checks, so the reference codec and any older reader read our wider
values correctly. No version bump and no `min_reader_minor` change.

The one consequence to be aware of: for a decision whose `equity_loss` exceeds
1.0, our EVAL/CUBE bytes **differ from what the reference codec would have
written** for the same analysis (it truncates to `10000`, we do not). Below 1.0
— which is the overwhelming majority of decisions — the bytes are identical.

Files written before this change cannot be identified by inspection (a stored
`10000` is either a genuine 1.0 or a truncation), but the true loss survives
elsewhere in both record types and can be recovered without re-analysis: on EVAL
entries as `best_equity - alternatives[is_played].equity`, and on CUBE entries
from the unclamped `no_double_equity` / `double_take_equity` /
`double_pass_equity`.

---

## Chunk Type Codes

| Chunk | Chars  | uint32 LE      | Critical |
|-------|--------|----------------|----------|
| MHDR  | "MHDR" | `0x5244484D`   | Yes      |
| GAME  | "GAME" | `0x454D4147`   | Yes      |
| ANAL  | "ANAL" | `0x4C414E41`   | No       |
| EVAL  | "EVAL" | `0x4C415645`   | No       |
| ALTS  | "ALTS" | `0x53544C41`   | No       |
| CUBE  | "CUBE" | `0x45425543`   | No       |
| GVAN  | "GVAN" | `0x4E415647`   | No       |
| SIGN  | "SIGN" | `0x4E474953`   | No       |
| CLCK  | "CLCK" | `0x4B434C43`   | No       |
| CSUM  | "CSUM" | `0x4D555343`   | Yes      |

---

## MHDR — Match Header

**Chunk type:** `0x5244484D`, Critical

**Fixed part (24 bytes):**

| Offset | Size | Type     | Field |
|--------|------|----------|-------|
| 0      | 2    | uint16   | match_length (0 = money game) |
| 2      | 1    | uint8    | num_games |
| 3      | 1    | uint8    | flags (see below) |
| 4      | 2    | uint16   | white_score_final |
| 6      | 2    | uint16   | black_score_final |
| 8      | 1    | uint8    | result: 0=incomplete, 1=white_won, 2=black_won |
| 9      | 1    | uint8    | source: 0=play, 1=mat_import, 2=xg_import, 3=transcribed, 4=bgblitz_import |
| 10     | 4    | uint32   | timestamp (unix seconds) |
| 14     | 2    | uint16   | total_plies (across all games, for size hints) |
| 16     | 8    | uint8[8] | reserved / bgsage extensions (see below) |

**Flags byte (offset 3):**

| Bit | Field | Notes |
|-----|-------|-------|
| 0 | crawford | Crawford rule in effect |
| 1 | jacoby | Jacoby rule in effect |
| 2 | beaver | **[GammonView extension]** Beaver rule in effect |
| 3 | raccoon | **[GammonView extension]** Raccoon rule in effect (optional; often implied by beaver) |
| 4–7 | reserved | Must be 0 for now |

*Compatibility: bits 2–3 were reserved (0) in pre-extension files. Old readers see unknown reserved bits; they must not reject files with non-zero reserved bits in this byte.*

**Reserved bytes (offset 16–23) — bgsage extension usage:**

| Bytes | Size | Type   | Field |
|-------|------|--------|-------|
| 16–17 | 2    | uint16 | cube_limit — **[GammonView extension]** maximum cube value (0 = no limit, e.g. 64, 1024) |
| 18–23 | 6    | uint8[6] | reserved (0) |

*Compatibility: reserved bytes were 0 in original MHDR. Old readers do not read these bytes; the variable part is located by reading player name lengths, not by jumping to a fixed offset past reserved bytes.*

**Variable part:**

| Field        | Encoding |
|--------------|----------|
| player_white | uint8 length + UTF-8 bytes |
| player_black | uint8 length + UTF-8 bytes |
| event        | **[GammonView extension]** uint8 length + UTF-8 bytes (0-length = absent) |
| site         | **[GammonView extension]** uint8 length + UTF-8 bytes (0-length = absent) |

*Compatibility: old readers consume player_white and player_black, then stop. The additional `event` and `site` fields are appended after and are not read by old readers — this is safe because old readers parse by length-prefixed fields, not by fixed offset. Each field is self-delimiting. By the same argument a reader that knows `event` but not `site` stops one field earlier and is equally unaffected, so appending `site` needed no version bump.*

*Max event/site string: 255 bytes each (uint8 length prefix); 0-length means absent.*

*Legacy: `event` and `site` were once folded into a single `event` string joined by `" • "`, with no `site` field written at all. A reader that finds an `event` and no `site` splits it on the first separator (`gvformat.place.split_place` / `place.js`'s `splitPlace`); a string with no separator is all event. Old files therefore decode to the same pair as new ones.*

Typical size: ~35 bytes without event/site; ~70 bytes with typical strings.

---

## GAME — Game Data (Compact Ply Encoding)

One chunk per game. Stores an initial board position and a sequence of variable-length **ply records**.

**Chunk type:** `0x454D4147`, Critical

### Game Header (8 bytes)

| Offset | Size | Type   | Field |
|--------|------|--------|-------|
| 0      | 1    | uint8  | game_index (0-based) |
| 1      | 2    | uint16 | num_plies |
| 3      | 1    | uint8  | winner: 0=white, 1=black, 0xFF=incomplete |
| 4      | 1    | uint8  | points_won (base × cube — the game's **full** value; see below) |
| 5      | 1    | uint8  | flags: bit 0 = is_crawford, bit 1 = is_lastgame |
| 6      | 1    | uint8  | first_to_move: 0=white, 1=black — who won the opening roll |
| 7      | 1    | uint8  | reserved (0) |

*Note: offset 6 is `first_to_move` (confirmed against the compiled reference `GameHeader` struct in `ogxm_format.hpp` and an empirical probe of `ogxm_json_to_binary()`'s output); only offset 7 (1 byte) is truly reserved.*

**`points_won` and the match length.** The field stores what the game was *worth*
(base × cube), not what its winner banked. A match ends the instant somebody
reaches the target, so a 4-point gammon won at 12-away of 13 makes the match
13-7, not 16-7 — but the full `4` is what is stored, because it is also the only
record of the win *type* (`points_won / cube` is how `reconstruct_mat` recovers
"with gammon"; capping it would make a match-ending gammon indistinguishable
from a single). The reference codec's writer stores it uncapped for the same
reason, and its `match_score.hpp` notes that "files written by other engines
record the game's full value".

The cap therefore belongs to **accumulation, not storage**: every consumer that
sums `points_won` into a running score runs each game through
`gvformat.binary.cap_points_won` (JS: `capPointsWon`) first, and the two
already-summed totals in MHDR go through `clamp_match_score` /
`clampMatchScore`. Money play (`match_length = 0`) has no target and is never
capped. Base spec 1.4 states the same rule from the writer's side.

*The GAME chunk carries **no** GammonView trailing bytes: equity↔MWC conversion is compute-on-read via the shipped MET (`gvformat/met.py`), needing no per-game or per-decision anchor at all -- replacing the earlier per-game trailing `met_value`. GAME is therefore byte-for-byte identical to the reference codec's output.*

### Initial Board (variable: 2 or 27 bytes)

**Default position (2 bytes):**
```
[0xFF][0xFF]
```

**Custom position (27 bytes):**
```
[point_0][point_1]...[point_25][0x00]
```
Each byte is a signed int8: positive = white checkers, negative = black checkers. Point 0 = white bar, point 25 = black bar, points 1-24 = board.

### Ply Records (variable-length, 1-5 bytes each)

*(Unchanged from base OGXM spec; see base spec for full encoding details.)*

**First byte:**

| Bits | Width | Field |
|------|-------|-------|
| 0-4  | 5     | action_id (see Action ID Table) |
| 5    | 1     | color: 1=White, 0=Black |
| 6-7  | 2     | (part of first move byte, if checker move) |

**Action ID Table (5 bits, values 0-31):**

| ID | Meaning | Move Bytes |
|----|---------|------------|
| 0–20 | Dice combinations | 2 or 4 |
| 21 | Double | 0 |
| 22 | Take | 0 |
| 23 | Drop | 0 |
| 24 | Game over | 0 |
| 25 | Match over | 0 |
| 26 | Final | 0 |
| 27 | Resign game | 0 |
| 28 | Resign match | 0 |
| 29 | Force-forfeit | 0 |
| 30 | NULL (no dice) | 0 |
| 31 | Set position | 26 or 27 |

**Move step encoding (each byte):**

| Bits | Width | Field |
|------|-------|-------|
| 0-4  | 5     | start_point (0-25 absolute) |
| 5-7  | 3     | pips_moved (1-6; 0 = no move / padding) |

---

## ANAL — Analysis Header

**Chunk type:** `0x4C414E41`, Ancillary

**Fixed part (24 bytes):**

| Offset | Size | Type     | Field |
|--------|------|----------|-------|
| 0      | 1    | uint8    | ply (base analysis depth — plain integer, unchanged from base spec) |
| 1      | 1    | uint8    | reserved (0) |
| 2      | 2    | uint16   | num_checker_evals (entries in following EVAL) |
| 4      | 2    | uint16   | num_cube_evals (entries in following CUBE) |
| 6      | 4    | uint32   | timestamp (unix seconds, when analysis ran) |
| 10     | 4    | uint32   | duration_ms (wall-clock time) |
| 14     | 2    | uint16   | reserved (0) |
| 16     | 8    | uint8[8] | reserved (0) |

**Variable part:**

| Field    | Encoding |
|----------|----------|
| model_id | uint8 length + UTF-8 bytes (e.g. `"gv-bgsage/2.0.20260907"`, `"xg"`, `"ensemble_v4.ogxf"`) |

`ply` is a **plain depth integer**, exactly as in the base spec — a base reader interprets it directly. GammonView analysis modes that a plain integer cannot express (truncated rollout, full rollout, database) are carried in the `GVAN` chunk's `base_eval_level` field, which is authoritative for GammonView readers. When the mode has no natural depth (e.g. rollout), a GammonView writer stores a best-effort nominal depth in `ply` (0 if none applies) and the true mode in `GVAN`.

---

## EVAL — Checker Evaluations

**Chunk type:** `0x4C415645`, Ancillary

Entry count from preceding ANAL's `num_checker_evals`.

**Unchanged from the reference codec (20 bytes).** GammonView adds no fields here; its per-decision data (`decision`, `illegal_move`, `has_luck`, `luck`) lives in the `GVAN` chunk's checker section, positionally aligned to these entries. `played_equity` and `classification` are **derived, not stored** — `played_equity = best_equity - equity_loss`; `classification` is a reader-owned threshold bucket over `equity_loss` (see `OGXM_COMPUTED_FIELDS.md`).

| Offset | Size | Type   | Field |
|--------|------|--------|-------|
| 0      | 1    | uint8  | game_index |
| 1      | 2    | uint16 | ply_index |
| 3      | 2    | uint16 | win [0-10000] |
| 5      | 2    | uint16 | gammon_win [0-10000] |
| 7      | 2    | uint16 | bg_win [0-10000] |
| 9      | 2    | uint16 | gammon_loss [0-10000] |
| 11     | 2    | uint16 | bg_loss [0-10000] |
| 13     | 2    | int16  | best_equity (/10000) |
| 15     | 2    | uint16 | equity_loss [0-65535] |
| 17     | 1    | uint8  | num_alts |
| 18     | 1    | uint8  | ply — **per-decision analysis depth**; 0 = use the ANAL header `ply`. Populated from the JSON `analysis.ply` field. |
| 19     | 1    | uint8  | flags: bit 0 = has_missed_double |

250 decisions × 20 bytes = **5.0 KB**.

---

## ALTS — Alternative Moves

**Chunk type:** `0x53544C41`, Ancillary

Entries are **fixed size**. A reader should validate `chunk_len == (Σ num_alts) × 17`.

**Unchanged from base spec (17 bytes).** GammonView's per-alternative `eval_level` lives in the `GVAN` chunk's alt section, positionally aligned to these entries (same `Σ num_alts` count, same order).

| Offset | Size | Type     | Field |
|--------|------|----------|-------|
| 0      | 4    | uint8[4] | move: up to 4 packed checker steps, 0-padded |
| 4      | 2    | int16    | equity (/10000) |
| 6      | 2    | uint16   | win [0-10000] |
| 8      | 2    | uint16   | gammon_win [0-10000] |
| 10     | 2    | uint16   | bg_win [0-10000] |
| 12     | 2    | uint16   | gammon_loss [0-10000] |
| 14     | 2    | uint16   | bg_loss [0-10000] |
| 16     | 1    | uint8    | flags: bit 0 = is_played_move |

**Move step packing** (unchanged): `byte = (start & 0x1F) | ((pips & 0x07) << 5)`. Zero byte = absent step.

250 decisions × 5 alts × 17 bytes = **~21 KB**.

---

## CUBE — Cube Decision Analyses

**Chunk type:** `0x45425543`, Ancillary

Entry count from preceding ANAL's `num_cube_evals`.

**Unchanged from the reference codec (28 bytes).** GammonView adds no fields and no new types here — it uses the base `type=4` the maintainer added upstream. Its per-cube data (`decision`, `eval_level`) lives in the `GVAN` chunk's cube section, positionally aligned to **every** CUBE entry including type=2/type=4 ones. `classification` is derived, not stored (same rule as EVAL).

| Offset | Size | Type     | Field |
|--------|------|----------|-------|
| 0      | 1    | uint8    | game_index |
| 1      | 2    | uint16   | ply_index |
| 3      | 1    | uint8    | type: 0=double_decision, 1=take_pass, 2=missed_double, 3=resign, 4=live_checker |
| 4      | 2    | int16    | no_double_equity (/10000) |
| 6      | 2    | int16    | double_take_equity (/10000) |
| 8      | 2    | int16    | double_pass_equity (/10000) |
| 10     | 2    | uint16   | win [0-10000] |
| 12     | 2    | uint16   | gammon_win [0-10000] |
| 14     | 2    | uint16   | bg_win [0-10000] |
| 16     | 2    | uint16   | gammon_loss [0-10000] |
| 18     | 2    | uint16   | bg_loss [0-10000] |
| 20     | 2    | uint16   | equity_loss [0-65535] |
| 22     | 1    | uint8    | correct_action: 0=no_double, 1=double, 2=take, 3=pass |
| 23     | 1    | uint8    | played_action (same encoding) |
| 24     | 1    | uint8    | ply — **per-decision analysis depth**; 0 = use the ANAL header `ply` |
| 25     | 3    | uint8[3] | reserved (0) |

**CUBE type=2 (missed_double):** References the checker-move ply where the player should have doubled. Built from the checker ply's `analysis.missed_double` sub-object; `played_action` is always `no_double`, and `ply` reuses the checker decision's own per-decision `ply` (not a value read from `missed_double` itself). `probs` carries the cube decision's **pre-roll** probabilities from `missed_double.eval`, exactly as `type=4` carries them from `cube_decision.eval`. Without these the only probabilities on the ply are the checker play's *post-roll* ones, which answer a different question and are actively misleading in a cube view. This used to be the one place GammonView wrote bytes the reference codec left zero (its `missed_double` JSON shape had no `eval`, so `ogxm_json.cpp` had nothing to put there); the base spec's 2026-08-10 sync adopted it — `probs` "carry the evaluation behind them when the producer supplied one" — so the two writers now agree and `tests/test_gvab_writer.py` asserts byte equality rather than tolerating a gap. All-zero still means "not recorded".

Note the base spec has since gone further: it now derives a `cube_decision` block from a `type=2` entry as well, on the grounds that a checker ply holds one cube entry and a missed double *is* that ply's live cube decision plus an error. gvformat still treats the two JSON sub-objects as mutually exclusive on read. The binary is identical either way — this is a JSON-shape difference only, and it costs nothing until a consumer wants `cube_decision` present on every cube-live ply.

**CUBE type=3 (resign):** `no_double_equity` reuses the slot for the raw signed resign error; `double_take_equity` for the raw take-resign error.

**`ply` (offset 24).** Base OGXM's own per-decision field — "analysis ply for this decision; 0 = use ANAL header ply". GammonView populates it for `type=0`/`type=1` from that cube decision's own eval level, so a two-pass preset that upgrades a close cube off the screening depth reports the depth it was actually judged at; without it a reader with no GVAN concludes every cube was decided at the header depth. Only the *depth* fits here — the mode (truncated/rollout) stays in GVAN's `eval_level`. For `type=2`/`type=4` the field keeps its base meaning, the checker decision's ply, as the base spec prescribes.

**CUBE type=4 (live_checker):** A correct/non-error live cube — attached to a **checker** ply rather than a cube ply, so an analysis view can show the live double/take/pass decision above the moves even though doubling wasn't the play. Built from the checker ply's `analysis.cube_decision` sub-object (mutually exclusive with `missed_double` — a checker ply carries at most one of the two). `correct_action` is `double` when `cube_decision.should_double` is true, else `no_double` (this sub-object never encodes take/pass — the opponent's response is a display-only label derived from the equities, not stored). `played_action` is always `no_double`. **`equity_loss` is hardcoded `0`** — it is not read from the JSON's `equity_loss`, because the checker move (not the cube) is where any error for this ply is scored. `ply` reuses the checker decision's own per-decision `ply`, same as `missed_double`. Readers that predate this type ignore the entry (additive value, no version bump).

---

## GVAN — GammonView Analysis Extensions [GammonView]

**Chunk type:** `0x4E415647`, Ancillary

One GVAN chunk per analysis block, placed after that block's CUBE and binding to the most recent ANAL. Its sections correlate **positionally** to that block's EVAL, ALTS, and CUBE entries — same counts (`num_checker_evals`, `Σ num_alts`, `num_cube_evals`), same order — so no per-record keys are needed. Base readers skip the whole chunk by its length. This one chunk carries everything GammonView adds to an analysis that the base format has no field for.

`GVAN` no longer carries a no-double section. Correct/non-error live-cube positions (`cube_decision` in the JSON) round-trip through the base `CUBE` chunk as `type=4` (`live_checker`), the same way `missed_double` round-trips as `type=2` — both are ordinary entries in the CUBE chunk's array, so the GVAN cube section (below) is aligned 1:1 to **all** `num_cube_evals` CUBE entries, including type=2/type=4 ones, not just type=0/1.

**Header (4 bytes):**

| Offset | Size | Type  | Field |
|--------|------|-------|-------|
| 0 | 1 | uint8 | gvan_version (GammonView GVAN format version; **3**) |
| 1 | 1 | uint8 | base_eval_level — authoritative analysis eval level, bit-flag encoded (below) |
| 2 | 1 | uint8 | section_flags: bit 0 = checker, bit 1 = alt, bit 2 = cube |
| 3 | 1 | uint8 | luck_eval_level — depth the luck equities were computed at, bit-flag encoded (same encoding as `base_eval_level`). `0x01` (1-ply) today; will become configurable. |

Sections follow the header in order (checker, alt, cube), each present only if its `section_flags` bit is set. `gvan_version` lets the GammonView chunk evolve **without touching the OGXM version**.

**Eval-level bit-flag encoding** (used by `base_eval_level` and the per-entry `eval_level` fields):

| Bits | Width | Meaning |
|------|-------|---------|
| 0–3 | 4 | Eval depth (0–15) |
| 4 | 1 | Truncated rollout at this depth |
| 5 | 1 | Full rollout |
| 6 | 1 | Database / engine-specific named mode |
| 7 | 1 | Reserved (0) |

Examples: `3ply` = `0x03`, `truncated3` = `0x13`, `rollout` = `0x20`, `database` = `0x40`. In the **per-entry** `eval_level` fields, value `0x00` means "same as `base_eval_level`" (the common case — the cheap-screener depth), so only decisions upgraded to a different level cost a non-zero byte.

**Version history.** v1 carried per-decision `mwc_on_win`/`mwc_on_loss` anchors on
every checker and cube record. v2 blanked them when MWC became compute-on-read but
kept the width. **v3 removes them**, taking the checker record 7 → 3 bytes and the
cube record 6 → 2 bytes (≈37% of a typical GVAN chunk, all of it zeros). Record
width is the only difference between v2 and v3, so both decode to the same shape
and the reader accepts either; **v1 is not supported** (its slots hold live
anchors, not zeros). A reader MUST dispatch on `gvan_version` and reject an
unknown one — the sections are positional and fixed-width, so guessing the width
silently yields plausible garbage rather than an error.

**Checker section** — `num_checker_evals` records (3 bytes each), aligned 1:1 to EVAL entries:

| Offset | Size | Type  | Field |
|--------|------|-------|-------|
| 0 | 1 | uint8  | flags: bit 0 = decision, bit 1 = has_luck, bit 2 = illegal_move |
| 1 | 2 | int16  | luck (/10000; valid only when flags bit 1 = 1, else 0) |

- `decision` — the move counted as a PR decision.
- `has_luck` — `luck` below is valid.
- `illegal_move` — the played move could not be matched to a legal position (transcription error / illegal play).
- `luck` — `postroll - preroll` equity at the **luck eval level** (1-ply; see header `luck_eval_level`), NOT the full eval level; preset-independent. The two equities behind it are not themselves stored.

MWC is compute-on-read everywhere, via the one MET shipped in `gvformat/met.py` (Kazaross-XG2): `mwc = met.eq2mwc(equity, away1, away2, cube_value, is_crawford)`, with `(away1, away2, cube_value, is_crawford)` recovered from the ply's own position/score fields (OGID) rather than a stored anchor. That is what made the v1 anchors redundant.

(A checker ply's live cube decision — whether an error, `missed_double`/type=2, or correct, `cube_decision`/type=4 — is discoverable by scanning the CUBE chunk for an entry whose `(game_index, ply_index)` matches this checker ply; there is no separate flag or pointer for it anymore, since it's a first-class CUBE entry.)

**Alt section** — `Σ num_alts` bytes (1 each), aligned 1:1 to ALTS entries:

| Size | Type | Field |
|------|------|-------|
| 1 | uint8 | eval_level (bit-flag; 0 = same as base_eval_level) |

**Cube section** — `num_cube_evals` records (2 bytes each), aligned 1:1 to **every** CUBE entry (types 0/1/2/3/4, in the same order they appear in the CUBE chunk):

| Offset | Size | Type | Field |
|--------|------|------|-------|
| 0 | 1 | uint8  | flags: bit 0 = decision |
| 1 | 1 | uint8  | eval_level (bit-flag; 0 = same as base_eval_level) — the cube result actually used; distinguishes cheap screener from an upgraded pass |

**What stays here and why.** The `eval_level` byte is a *superset* of base `CUBE.ply`: bits 0–3 are the depth, so a plain N-ply level is bit-identical to a ply number, but bits 4–6 add truncated/rollout/database, which a plain int cannot express. The depth alone now also travels in base `CUBE.ply` (see below), so a reader with no GVAN gets the right depth; GVAN remains authoritative for the mode. `decision` stays because the base format has no field for it and it is not derivable — it records the *producer's* PR-counting policy, which differs by source (XG infers it from a trivial-cube test, BGF reads the file's own flags, bgsage uses `counted`).

For a `type=2` (missed_double) or `type=4` (live_checker) entry, `decision` is `true` for missed_double (a missed double is always a real decision) and reflects the JSON `cube_decision.decision` flag for live_checker; `eval_level` reflects the sub-object's own `eval_level` for both (`missed_double.eval_level` / `cube_decision.eval_level`), and is `0` where the sub-object carries none.

**Length validation:**

```
chunk_len == 4
  + (checker ? 3 × num_checker_evals : 0)      # v2: 7
  + (alt     ? 1 × Σ num_alts        : 0)
  + (cube    ? 2 × num_cube_evals    : 0)      # v2: 6
```

Typical per analysis: 250×3 + 1250×1 + 20×2 + 4 ≈ **2.0 KB** (v2 was ≈3.2 KB).

---

## SIGN — Analysis Signature

*(Unchanged from base spec.)*

**Chunk type:** `0x4E474953`, Ancillary

Optional per-analysis cryptographic signature. See the base spec for full details.
gvformat does not generate or verify one; it carries it through — see
[Chunk Passthrough](#chunk-passthrough).

---

## CLCK — Clock Data

*(Unchanged from base spec.)*

**Chunk type:** `0x4B434C43`, Ancillary

Per-ply timestamps with delta compression. See the base spec for full details.
Carried through, not decoded — see [Chunk Passthrough](#chunk-passthrough).

---

## Chunk Passthrough

gvformat decodes MHDR, GAME, ANAL, EVAL, ALTS, CUBE, GVAN and CSUM. The base
format has more than that — `SIGN`, `CLCK`, and (since base spec **1.4**)
`VIDO`, the video-source chunk that anchors plies to positions in a recording —
and will grow more.

Skipping an unknown ancillary chunk on read is correct and always was. The
problem is the *rewrite*: this pipeline's whole shape is read → append an
analysis → write, so a chunk that is merely skipped is a chunk **deleted** from
the file. A match with video marks that went through `gvan-match` used to come
back without them.

So the reader captures every chunk it does not decode and hands it to the writer
verbatim, on the OGXM object as `_unknown_chunks`:

```json
"_unknown_chunks": [
  {"type": 1329875286, "name": "VIDO", "flags": 0, "anal_index": 0, "data": "<base64>"}
]
```

`data` is base64 so the list survives the `.gva` JSON form as well as the binary
one. `anal_index` is the analysis block the chunk followed in the source file:
`SIGN` binds to its `ANAL`, so it is re-emitted **inside** that block, while
everything else is re-emitted after all blocks and before `CSUM` — the order the
base spec fixes for `CLCK`/`VIDO`. A chunk whose block no longer exists falls
back to the trailing group. Chunk order is preserved exactly on a round trip.

Two caveats worth stating plainly:

- **An unknown chunk marked CRITICAL is rejected**, not carried. The flag means
  a reader that cannot interpret the chunk cannot claim to have read the file,
  and rewriting it would be a lie. `GvabError` names the chunk.
- **A preserved `SIGN` is not re-verified.** It signs the GAME bodies and its
  analysis block's content; appending a *new* block leaves those untouched, so a
  valid signature stays valid — but gvformat has no key material and does not
  check. Carrying it lets a verifier decide; dropping it would destroy the
  provenance outright.

---

## In-Memory Representation (C++) — GammonView Extensions

Match metadata reuses reserved base fields, so it extends the base struct
(`OgxmGame` is unchanged — the per-game `met_value` is gone):

```cpp
struct OgxmMatchGV : OgxmMatch {
    bool beaver = false;
    bool raccoon = false;
    uint16_t cube_limit = 0;   // 0 = no limit
    std::string event;         // empty = absent
    std::string site;          // empty = absent
};
```

All analysis extensions are one decoded `GVAN` struct per analysis block, whose per-entry vectors are read alongside (and index-aligned to) the base EVAL/ALTS/CUBE vectors:

```cpp
struct GVANCheckerExt {
    bool decision = false;
    bool has_luck = false;
    bool illegal_move = false;
    int16_t luck = 0;              // /10000; valid only when has_luck
};                                 // v1/v2 also carried two uint16 anchor
                                   // slots here; v3 dropped them (7 -> 3 B)

struct GVANCubeExt {               // aligned to EVERY CUBE entry (types 0-4)
    bool decision = false;
    uint8_t eval_level_flags = 0;  // bit-flag; 0 = same as base_eval_level
};                                 // likewise: v3 dropped two uint16 (6 -> 2 B)

struct GVAN {                      // one per ANAL block
    uint8_t gvan_version = 3;
    uint8_t base_eval_level = 0;   // bit-flag encoded; authoritative level
    uint8_t luck_eval_level = 0x01; // bit-flag encoded; depth of the luck equities (1-ply today)
    std::vector<GVANCheckerExt> checker;   // size == num_checker_evals
    std::vector<uint8_t> alt_eval_level;   // size == Σ num_alts; 0 = base
    std::vector<GVANCubeExt> cube;         // size == num_cube_evals (incl. type=2/4)
};
```

MWC is compute-on-read: `mwc(e) = met.eq2mwc(e, away1, away2, cube_value, is_crawford)` via the one shipped MET (`gvformat/met.py`, Kazaross-XG2), with the score/cube frame recovered from the ply's own position data rather than a stored anchor.

Correct/non-error live-cube positions and missed doubles are `OgxmCubeEval` entries with `type=4`/`type=2` in the base `cube_evals` vector (see the base spec's `OgxmCubeEval`/`OgxmAnalysis` structs) — there is no GammonView-only struct for them.

---

## Size Budget — With GammonView Extensions (typical 7-point match, 300 plies)

Base chunks are byte-identical to the reference codec (bar an `equity_loss` above 1.0, which we do not truncate, and a missed double's `probs`, which we fill and it leaves zero); GammonView adds only the metadata deltas (inline) and one `GVAN` chunk per analysis block.

| Component | Base | With GammonView |
|-----------|------|-----------------|
| File Header + End Marker | 28 bytes | 28 bytes |
| MHDR | ~47 bytes | ~80 bytes (+ event/site strings) |
| GAME (5 games, 300 plies) | ~950 bytes | ~950 bytes (byte-identical — no `met_value`) |
| ANAL header | ~45 bytes | ~45 bytes (unchanged) |
| EVAL (250 decisions) | 5.0 KB | 5.0 KB (unchanged, 20 B/entry incl. per-decision `ply`) |
| ALTS (1250 entries) | ~21 KB | ~21 KB (unchanged, 17 B/entry) |
| CUBE (20 entries, incl. type=2/4) | 560 bytes | 560 bytes (unchanged) |
| **GVAN (analysis extensions)** | — | **~3.2 KB** (7 B/checker + 6 B/cube; cube's 2 reserved bytes and checker's 2 reserved bytes are the blanked v1 MWC-anchor slots) |
| CLCK (300 plies) | ~520 bytes | ~520 bytes |
| CSUM | 48 bytes | 48 bytes |
| **Total** | **~42 KB** | **~45 KB** |

---

## Security Limits

*(Unchanged from base spec.)*

| Limit | Value |
|-------|-------|
| Max file size | 10 MB |
| Max chunk data | 5 MB |
| Max chunks | 200 |
| Max games | 100 |
| Max plies per game | 1500 |
| Max alts per decision | 50 |
| Max analyses (ANAL blocks) | 16 |
| Max player name | 255 bytes |
| Max event string | 255 bytes [GammonView extension] |
| Max site string | 255 bytes [GammonView extension] |

---

## Extension Summary

All extensions are structurally Tier 1 — no version bump, and the reference codec can walk the chunk stream (which is not the same as reading every value the way we meant it; see the cube equity units above). Metadata reuses reserved base fields; correct/non-error live cubes and missed doubles round-trip through base `CUBE` (`type=4`/`type=2`); **everything else GammonView adds is in the `GVAN` ancillary chunk**, leaving base MHDR(metadata aside)/GAME/ANAL/EVAL/ALTS/CUBE byte-identical to the reference codec, apart from the wider `equity_loss` bound (GAME now has no GammonView bytes at all).

| Location | Field | Size | Tier | Notes |
|----------|-------|------|------|-------|
| MHDR flags | `beaver` (bit 2) | — | 1 | Reserved bit; old readers unaffected |
| MHDR flags | `raccoon` (bit 3) | — | 1 | Reserved bit; old readers unaffected |
| MHDR reserved | `cube_limit` (bytes 16–17) | +2 B | 1 | Was reserved; old readers skip |
| MHDR variable | `event` string | +N B | 1 | Appended after player names; old readers stop earlier |
| MHDR variable | `site` string | +N B | 1 | Appended after `event`; readers that know only `event` stop earlier |
| **base CUBE** | `type=4` (`cube_decision`), `type=2` (`missed_double`), `probs` on `type=2` | — | 1 | Additive value; older readers skip an unrecognized `type` byte, and read zero `probs` as "not recorded" — no chunk change |
| **GVAN chunk** | remaining analysis extensions | +chunk | 1 | New ancillary chunk; old readers skip by length |
| GVAN header | `base_eval_level`, `gvan_version` (3) | in GVAN | 1 | Mode-aware eval level; independent GammonView versioning |
| GVAN checker | `decision`, `illegal_move`, `has_luck`, `luck` (+ 2 reserved bytes) | in GVAN | 1 | Aligned 1:1 to EVAL entries; 7 B/entry |
| GVAN alt | `eval_level` | in GVAN | 1 | Aligned 1:1 to ALTS entries |
| GVAN cube | `decision`, `eval_level` (+ 2 reserved bytes) | in GVAN | 1 | Aligned 1:1 to **every** CUBE entry (incl. type=2/4); 6 B/entry |
