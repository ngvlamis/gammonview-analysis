# OGXM JSON Specification — GammonView Extended

Based on `OGXM_JSON_SPEC.md` from [HedgeHog](https://gitlab.com/eranlambooij/hedgehog-public) (`docs/`). Additions specific to bgsage analysis output are marked **[GammonView extension]**.

JSON schema for the OGXM match format. This is the interchange format between the C++ engine (via WASM or ctypes) and all consumers (Vue frontend, Python API).

Produced by `ogxm::to_json()`, consumed by `ogxm::from_json()`. The frontend's `analysis.js` generates this JSON from live analysis; the WASM bridge converts it to/from OGXM binary.

**Key design principle:** Analysis and clock data live inline on each ply, producing a natural `match → game → ply → analysis` hierarchy. The binary format is unchanged — only the JSON conversion layer maps between flat binary chunks and hierarchical JSON.

---

## Top-Level Object

```json
{
  "match_length": 7,
  "player_white": "Alice",
  "player_black": "Bob",
  "white_score": 7,
  "black_score": 3,
  "result": 1,
  "source": 1,
  "timestamp": 1710500000,
  "crawford": false,
  "jacoby": false,
  "beaver": false,
  "raccoon": false,
  "cube_limit": 64,
  "event": "World Championship",
  "site": "Monte Carlo",
  "analysis_info": {...},
  "analyses_info": [...],
  "clock_info": {...},
  "games": [...]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `match_length` | uint16 | Points to win (0 = money game) |
| `player_white` | string | White player name (max 255 bytes) |
| `player_black` | string | Black player name (max 255 bytes) |
| `white_score` | uint16 | White's final score |
| `black_score` | uint16 | Black's final score |
| `result` | uint8 | 0=incomplete, 1=white_won, 2=black_won |
| `source` | uint8 | 0=play, 1=mat_import, 2=xg_import, 3=transcribed, 4=bgblitz_import |
| `timestamp` | uint32 | Unix seconds |
| `crawford` | bool | Match has Crawford rule |
| `jacoby` | bool | Match has Jacoby rule |
| `beaver` | bool | **[GammonView extension]** Beaver rule in effect |
| `raccoon` | bool | **[GammonView extension]** Raccoon rule in effect. Stored in the MHDR flags byte (bit `0x08`); round-trips through `.gvab`. |
| `cube_limit` | int | **[GammonView extension]** Maximum cube value allowed (e.g. 64, 1024); 0 = no limit. From mat `[CubeLimit]` header. |
| `event` | string\|null | **[GammonView extension]** Event name — mat `[Event]` header, XG's event, BGF's `event`. Null if absent. |
| `site` | string\|null | **[GammonView extension]** Where it was played — mat `[Site]` header, XG's location, BGF's `site`. Null if absent. Independent of `event`: neither is inferred from the other. |
| `replay_complete` | bool | **Output only** (set by `to_json`, ignored by `from_json`). `false` when the move sequence could not be fully replayed (an illegal/corrupt ply); plies after the failure carry no `ogid_before`/`ogid_after`. Consumers should treat such a match as unanalysable. |
| `replay_failed_game` | uint | **Optional, output only.** 0-based index of the first game that failed to replay; present only when `replay_complete` is `false`. |
| `analysis_info` | AnalysisInfo | **Optional.** Metadata for the primary analysis (omitted if no analysis) |
| `analyses_info` | AnalysisInfo[] | **Optional.** Metadata for **every** analysis; present only when there is more than one (see [Multiple analyses](#multiple-analyses)) |
| `clock_info` | ClockInfo | **Optional.** Clock settings (omitted if no clock data) |
| `_unknown_chunks` | object[] | **Optional.** Binary chunks the reader did not decode, carried verbatim so a read→write cycle cannot drop them (see [Chunk passthrough](#chunk-passthrough)). Absent when the source carried none, and absent entirely for a match that never came from a `.gvab`. |
| `games` | Game[] | **Required.** Array of games in play order |

### Chunk passthrough

`gvformat` decodes MHDR, GAME, ANAL, EVAL, ALTS, CUBE, GVAN and CSUM. The base
format has more — `SIGN`, `CLCK`, and since base spec 1.4 `VIDO` (video source
and per-ply marks) — and will grow more.

Skipping an unknown ancillary chunk on read is correct. The problem is the
*rewrite*: this pipeline reads a match, appends an analysis, and writes it back,
so a chunk that is merely skipped is a chunk **deleted**. `read_gvab` therefore
captures every chunk it does not decode onto `_unknown_chunks`, and `write_gvab`
re-emits them in their original stream positions:

```json
"_unknown_chunks": [
  {"type": 1329875286, "name": "VIDO", "flags": 0, "anal_index": 0, "data": "<base64>"}
]
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | uint32 | The chunk's little-endian type code |
| `name` | string | The four ASCII characters, or `0xXXXXXXXX` if not printable |
| `flags` | uint16 | The chunk header's flags, preserved verbatim |
| `anal_index` | int | Index of the analysis block the chunk followed, or `-1` if it preceded them all. `SIGN` binds to its `ANAL` and is re-emitted **inside** that block; every other chunk is re-emitted after all blocks and before `CSUM`, the order the base spec fixes for `CLCK`/`VIDO`. A chunk whose block no longer exists falls back to the trailing group. |
| `data` | string | The chunk body, base64. Base64 rather than bytes so the list survives the `.gva` JSON form as well as the binary one. |

Two rules worth stating plainly:

- **An unknown chunk marked CRITICAL is rejected**, not carried. The flag means a
  reader that cannot interpret the chunk cannot claim to have read the file, and
  rewriting it would be a lie. `GvabError` names the chunk.
- **A carried `SIGN` is not re-verified.** It signs the GAME bodies and its own
  analysis block's content, so appending a *new* block leaves it valid — but
  `gvformat` holds no key material and does not check. Carrying it lets a
  verifier decide; dropping it would destroy the provenance outright.

`from_json` treats the field as opaque. Producing a match from a non-`.gvab`
source omits it entirely.

### Multiple analyses

A match may be analyzed by several models (e.g. an ensemble plus a GnuBG reference). Each analysis is one element of `analyses_info`; the index into that array is the **analysis_index**.

- **Single analysis (the common case):** the JSON keeps the legacy shape - `analysis_info` (one object) plus an inline `analysis` object on each analyzed ply. No `analyses_info`.
- **Multiple analyses:** `analyses_info` lists all of them. `analysis_info` still mirrors the **primary** (`analyses_info[0]`), and each ply still carries the primary's `analysis` object, so naive single-analysis consumers keep working. In addition, every analyzed ply carries an `analyses` array - one entry per analysis with an eval at that ply, each tagged with its `analysis_index`.

On parse, the presence of `analyses_info` selects multi mode (the per-ply `analyses` arrays are read and `analysis` is ignored); otherwise the single `analysis` object is read.

---

## AnalysisInfo

Metadata about the analysis run. Individual evaluation results are inline on each ply.

```json
{
  "ply": 3,
  "eval_level": "3ply",
  "luck_eval_level": "1ply",
  "preset": "world_class",
  "model_id": "gv-bgsage/2.0.20260907",
  "timestamp": 1710500100,
  "duration_ms": 5000,
  "signature": {
    "algorithm": "ed25519",
    "key_id": "hedgehog-prod-2026",
    "public_key": "<base64>",
    "signature": "<base64>"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `ply` | uint8 | Base analysis depth — plain integer (base OGXM field). In a two-pass scheme this is the first-pass (screen) depth; individual decisions deepened beyond it carry their own `analysis.ply` (see the checker Analysis object). |
| `eval_level` | string | **[GammonView extension]** Named, mode-aware eval level: `"1ply"`, `"2ply"`, `"3ply"`, `"4ply"`, `"truncated1"`, `"truncated2"`, `"truncated3"`, `"rollout"`, `"database"`. This vocabulary is closed — it is exactly what the binary `GVAN` byte can encode, and a name outside it encodes as `0`, which reads back as "same as `base_eval_level`" rather than as itself. Importers name a foreign engine's levels in these terms (XG's three XG Roller settings are short truncated rollouts, its opening book is a `database` lookup) and leave the engine's own wording to the reader, which can recover it from `model_id`. Authoritative — the plain `ply` integer cannot distinguish `"3ply"` from `"truncated3"` or represent rollout modes. In the binary format this comes from the `GVAN` chunk's `base_eval_level` (bit-flag encoded); `ply` stays a plain depth. |
| `luck_eval_level` | string | **[GammonView extension] Optional.** Depth the luck-analyzer computed each ply's `luck` at (e.g. `"1ply"`). Always `"1ply"` today and independent of `eval_level`/`preset`; will become configurable when a settable luck-eval-level option is added. |
| `preset` | string | **[GammonView extension] Optional. JSON only.** Name of the analysis preset used (e.g. `"world_class"`, `"fast"`, `"balanced"`). Informational — a label, not a specification. Preset definitions live in `gvanalysis/presets.py` and change between releases (the sizing tiers of `balanced` and `world_class_fast` have both moved), so the levels a block was actually judged at must be read from `ply`/`eval_level` here and from each decision's own `analysis.ply`, never inferred from this name. **Not carried in the binary**: `.gvab` has no field for it, so a JSON → `.gvab` → JSON round-trip drops it. |
| `model_id` | string | Model/engine identifier (max 255 bytes). Free-form: a reader must treat it as a label, not a key with a closed vocabulary. The importers in this repo write a bare engine name (`"xg"`, `"bgblitz"`, `"gnubg"`) because a foreign file records no version of its own. Analysis produced *here* writes `"gv-bgsage/<engine version>"` — gammonview's bgsage, at that exact build — because the engine name alone would overstate its share of the answer: the screening in `gvanalysis/checker_eval.py` chooses which moves are evaluated at full depth and the preset's tiers choose that depth, so two files both reading `"bgsage"` can hold different numbers from the same engine. No gammonview version is carried, deliberately — every other field of `analysis_info` is fixed by the match and the engine (`timestamp` is the match's, not the run's; `duration_ms` is unset), and that determinism is what lets a byte-exact regression corpus exist at all. |
| `timestamp` | uint32 | Unix seconds when analysis ran |
| `duration_ms` | uint32 | Wall-clock analysis time |
| `signature` | Signature | **Optional.** Cryptographic proof of authorship (omitted if unsigned) |

Always use the `eval_level` string in JSON; it is unambiguous and carries the mode. The binary `GVAN.base_eval_level` bit-flag encoding is documented in the [GVAN section](OGXM_FORMAT_SPEC_GAMMONVIEW.md#gvan--gammonview-analysis-extensions-gammonview) of the binary spec.

### Derived statistics are not stored (compute-on-read)

Match/game/player aggregates — **PR, total/average error, decision counts, luck totals, illegal-move counts** — are **not fields in this format**. They are computed from the per-ply `analysis` records on read, per `OGXM_COMPUTED_FIELDS.md`. There is one shared set of formulas (a Python helper for the CLI/analyzer, a JS helper for the frontend), so every consumer derives identical numbers with no stored redundancy. Do not add a `summary` block — the ad-hoc GVA format's stored aggregates are deliberately dropped.

### Signature

Lets an analyser prove it generated this analysis.

```json
{
  "algorithm": "ed25519",
  "key_id": "hedgehog-prod-2026",
  "public_key": "<base64 of 32 raw bytes>",
  "signature": "<base64 of 64 raw bytes>"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `algorithm` | string\|int | `"ed25519"`, or a numeric id for a future algorithm |
| `key_id` | string | **Optional.** Signer identity or key fingerprint |
| `public_key` | string | **Optional.** Base64 raw public key (omitted if the verifier resolves the key out-of-band) |
| `signature` | string | Base64 raw signature bytes |

---

## ClockInfo

Clock settings for the match. Per-ply timestamps are inline on each ply as `timestamp_ms`.

```json
{
  "reserve_ms": 300000,
  "delay_ms": 0,
  "increment_ms": 15000,
  "start_timestamp": 1710500000
}
```

| Field | Type | Description |
|-------|------|-------------|
| `reserve_ms` | uint32 | Initial reserve time per player (ms) |
| `delay_ms` | uint32 | Delay time per move (ms) |
| `increment_ms` | uint32 | Increment per decision (ms) |
| `start_timestamp` | uint32 | Match start (unix seconds) |

---

## Game

```json
{
  "game_index": 0,
  "winner": 0,
  "points_won": 2,
  "is_crawford": false,
  "is_lastgame": false,
  "first_to_move": 0,
  "plies": [...]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `game_index` | uint8 | 0-based game number |
| `winner` | uint8 | 0=white, 1=black, 255=incomplete |
| `points_won` | uint8 | Base points × cube value at game end — the game's **full** value, *not* capped to the match length (see below) |
| `is_crawford` | bool | This game is the Crawford game |
| `is_lastgame` | bool | **Optional on input.** This is the final game of the match. Reference GameHeader flags bit `0x02` (not a GammonView extension); always present on read. |
| `first_to_move` | uint8 | **Optional on input.** Color to move first this game (0=white, 1=black); `255` = unknown. Reference GameHeader byte at offset 6 (not a GammonView extension); always present on read. On write, defaults to the first ply's `color` (or `255` when the game has no plies). |
| `plies` | Ply[] | Sequence of actions in this game |

> **`points_won` is the game's full value, and the match length is applied on
> accumulation.** A match ends the instant somebody reaches the target, so the
> deciding game *banks* only what its winner still needed — a 4-point gammon won
> at 12-away of 13 makes the match 13-7, not 16-7. The stored value stays `4`,
> because it is also the only record of the win *type*: `points_won / cube` is
> how a `.mat` reconstruction recovers "with gammon", and capping it would make
> a match-ending gammon indistinguishable from a single. The reference codec's
> writer stores it uncapped for the same reason, and its `match_score.hpp` notes
> that "files written by other engines record the game's full value".
>
> So **every consumer that sums `points_won` into a running score caps each
> game first** — `gvformat.binary.cap_points_won` (JS `capPointsWon`) — and the
> two already-summed totals `white_score`/`black_score` go through
> `clamp_match_score` / `clampMatchScore`. Money play (`match_length = 0`) has
> no target and is never capped.

> **Equity↔MWC conversion** is engine-free and compute-on-read: MWC is derived
> from `(equity, score, cube)` via the one MET shipped in `gvformat/met.py`
> (Kazaross-XG2), not stored anywhere in the format. This replaces the old
> per-game `met_value` (a single game-start anchor that could not follow the
> cube through the game) and the later per-decision `mwc_on_win`/`mwc_on_loss`
> anchors (which required no MET but did require storing two floats per
> decision) -- shipping one MET table means the format needs neither.

---

## Ply

Each ply is one action: a checker move, cube action, or game-ending event. Analysis and clock timestamps are inline.

### Checker move ply (action_id 0–20)

```json
{
  "color": 1,
  "action_id": 14,
  "d1": 3,
  "d2": 6,
  "moves": [
    {"from": 13, "pips": 3},
    {"from": 8, "pips": 6}
  ],
  "ogid_before": "...",
  "ogid_after": "...",
  "timestamp_ms": 5230,
  "analysis": {
    "eval": {"win": 0.52, ...},
    "best_equity": 0.15,
    "played_equity": 0.12,
    "equity_loss": 0.03,
    "ply": 3,
    "decision": true,
    "luck": 0.02,
    "alternatives": [...],
    "missed_double": {...},
    "cube_decision": {...}
  }
}
```

### Cube/game action ply (action_id 21–30)

```json
{
  "color": 1,
  "action_id": 21,
  "ogid_before": "...",
  "ogid_after": "...",
  "timestamp_ms": 8710,
  "analysis": {
    "correct_action": "double",
    "played_action": "double",
    "no_double_equity": 0.30,
    "double_take_equity": 0.55,
    "double_pass_equity": 1.00,
    "eval": {"win": 0.62, ...},
    "equity_loss": 0.0,
    "decision": true
  }
}
```

`classification` is **not stored** anywhere in this format — it is a
threshold policy over `equity_loss` that the reader owns (see
`OGXM_COMPUTED_FIELDS.md`); the raw `equity_loss` is always present.

| Field | Type | Description |
|-------|------|-------------|
| `color` | uint8 | 0=black, 1=white |
| `action_id` | uint8 | See Action ID Table below |
| `d1` | uint8 | Die 1 (action_id 0–20; also optional on action_id 31 for an illegal play) |
| `d2` | uint8 | Die 2 (action_id 0–20; also optional on action_id 31 for an illegal play) |
| `moves` | Move[] | Checker steps (only for action_id 0–20, may be empty for forced pass) |
| `set_position` | int[26] | Board override (only for action_id 31); signed, +white/-black, index 0=white bar … 25=black bar |
| `ogid_before` | string | OGID before this ply (reconstructed, output only) |
| `ogid_after` | string | OGID after this ply (reconstructed, output only) |
| `timestamp_ms` | uint32 | **Optional.** Milliseconds from match start (from clock data) |
| `analysis` | Analysis | **Optional.** Inline analysis for this ply (the primary analysis) |
| `analyses` | Analysis[] | **Optional.** Present only in multi-analysis mode: one entry per analysis with an eval here, each carrying an extra `analysis_index` (uint) into `analyses_info` |

### Move

| Field | Type | Description |
|-------|------|-------------|
| `from` | uint8 | Start point (0–25 absolute: 0=white bar, 25=black bar) |
| `pips` | uint8 | Pips moved (1–6) |

A ply holds at most the hops its roll allows — two for a non-double, four for a
double — because that is the room the binary ply record has. A legal play never
needs more. An *illegal* one can (13/9 with a 3-1, then 12/11, is three
die-moves), so a converter records each of its checkers in one step at that
checker's own pip distance: `pips` is then a distance the dice do not explain,
which is exactly what makes the play illegal. Where even that does not fit, the
play is written as a set-position ply (action_id 31) stating the resulting
board. Either way the ply replays to the position the source recorded — a
truncated play would leave every later board in the game wrong.

### Action ID Table

| ID | Meaning | ID | Meaning |
|----|---------|----|---------|
| 0 | Dice 11 | 15 | Dice 44 |
| 1 | Dice 12 | 16 | Dice 45 |
| 2 | Dice 13 | 17 | Dice 46 |
| 3 | Dice 14 | 18 | Dice 55 |
| 4 | Dice 15 | 19 | Dice 56 |
| 5 | Dice 16 | 20 | Dice 66 |
| 6 | Dice 22 | 21 | Double |
| 7 | Dice 23 | 22 | Take |
| 8 | Dice 24 | 23 | Drop |
| 9 | Dice 25 | 24 | Game over |
| 10 | Dice 26 | 25 | Match over |
| 11 | Dice 33 | 26 | Final |
| 12 | Dice 34 | 27 | Resign game |
| 13 | Dice 35 | 28 | Resign match |
| 14 | Dice 36 | 29 | Force-forfeit |
|    |         | 30 | NULL (no dice) |
|    |         | 31 | Set position |

---

## Eval Object

Used throughout for 5-output NN probabilities. All values are floats in [0.0, 1.0].

```json
{
  "win": 0.52,
  "gammon_win": 0.12,
  "bg_win": 0.01,
  "gammon_loss": 0.10,
  "bg_loss": 0.005,
  "equity": 0.445
}
```

| Field | Type | Description |
|-------|------|-------------|
| `win` | float | P(win) |
| `gammon_win` | float | P(gammon win) |
| `bg_win` | float | P(backgammon win) |
| `gammon_loss` | float | P(gammon loss) |
| `bg_loss` | float | P(backgammon loss) |
| `equity` | float | Derived: `win + gammon_win + bg_win - gammon_loss - bg_loss` |

**Note:** `equity` is computed on output. On input, it is ignored (recomputed from the 5 probabilities). The 5 probabilities are for the **best move's resulting position**.

---

## Checker Analysis (inline on checker plies)

The `analysis` object on a checker move ply (action_id 0–20).

```json
{
  "eval": { ... },
  "best_equity": 0.15,
  "played_equity": 0.12,
  "equity_loss": 0.03,
  "ply": 3,
  "decision": true,
  "luck": 0.02,
  "alternatives": [...],
  "missed_double": {...},
  "cube_decision": {...}
}
```

| Field | Type | Description |
|-------|------|-------------|
| `eval` | Eval | NN evaluation of the best move's position |
| `best_equity` | float | Equity of the best move |
| `played_equity` | float | Equity of the move actually played |
| `equity_loss` | float | `best_equity - played_equity` (>= 0, clamped to [0, 1]) |
| `ply` | uint8 | **Optional.** Per-decision analysis depth (base OGXM field): the deepest level this decision was ultimately evaluated at. Present only when it exceeds the base `analysis_info.ply` (i.e. a two-pass scheme deepened this decision beyond the first-pass screen); omitted means "same as the base ply". Derived from the deepest per-alternative `eval_level`; the per-alt `eval_level` strings remain the authoritative, mode-aware record. |
| `decision` | bool | **[GammonView extension]** Whether this move counted as a PR decision. Moves are excluded by triviality rules (forced moves, positions with negligible equity spread, dead cube). Storing this flag allows importers to compute PR without re-implementing the decision filter. |
| `luck` | float | **[GammonView extension] Optional.** `postroll - preroll` equity at the luck eval level (1-ply today, NOT the full `eval_level`/preset — see `analysis_info.luck_eval_level`); preroll is the probability-weighted average across all 21 dice pairs before the actual dice were known, postroll is the actual roll's equity at that same level. Only the difference is stored (a single float), not the two equities. Absent if the luck analyzer did not run. `luck_mwc` (a reader-computed quantity, not stored) converts this to MWC terms: `luck_mwc = luck * (mwc_on_win - mwc_on_loss) / 2`, with `mwc_on_win`/`mwc_on_loss` computed on read via `met.mwc_anchors(away1, away2, cube_value, is_crawford)` -- see [MWC Conversion](#mwc-conversion) below. |
| `illegal_move` | bool | **[GammonView extension] Optional.** `true` when the played board could not be matched to any legal move generated by the analyzer (a transcription error in the source match, not a rules violation). When present and true, this ply is excluded from PR/decision counting; see `OGXM_COMPUTED_FIELDS.md`. Absent (not `false`) when the move matched normally. |
| `alternatives` | Alt[] | Top-N alternative moves, sorted by equity descending |
| `missed_double` | MissedDouble | **Optional.** Present when player should have doubled before rolling (standard OGXM) |
| `cube_decision` | CubeDecision | **Optional.** Present when cube was live and the correct action was no-double (non-error live cube). Mutually exclusive with `missed_double` — a ply has at most one. |

### Classification is compute-on-read

`classification` is **not a stored field** anywhere in this format. It is a
threshold policy over the raw `equity_loss` (always stored), and the reader
owns that policy — different consumers may bucket the same `equity_loss`
differently. The default GammonView thresholds are documented in
`OGXM_COMPUTED_FIELDS.md`; do not persist a computed `classification` in the
JSON (same rationale as the PR/luck aggregates, which are also derived on
read, not stored).

### MWC Conversion

Match-winning chance (MWC) is **not a stored field anywhere in this format**
-- no per-game anchor (the old `met_value`), no per-decision anchor (the
older `mwc_on_win`/`mwc_on_loss` pair). It is computed on read, engine-free,
from a single shipped match-equity table (MET): Kazaross-XG2, in
`gvformat/met.py` (mirrored byte-for-byte in `gvformat-js/src/met.js`). For a
fixed `(score, cube)`, `eq2mwc` is affine in equity, so any decision's
`mwc(e)` is:

```
mwc_win, mwc_loss = met.mwc_anchors(away1, away2, cube_value, is_crawford)
mwc(e) = (mwc_win + mwc_loss) / 2 + e * (mwc_win - mwc_loss) / 2
```

`away1`/`away2` are the mover's/opponent's away score at that ply (derived
from the ply's own position data -- e.g. `ogid_before` -- not a stored
field), `cube_value` is the ply's cube value, and `is_crawford` is whether
this is the Crawford game. `luck_mwc` for a checker ply's `luck` (a *change*
in equity) uses half the win/loss slope: `luck_mwc = luck * (mwc_win -
mwc_loss) / 2`. MWC is undefined (no score/cube frame) for money games.

### Cube equities are normalized equity, not MWC **[GammonView divergence]**

`no_double_equity`, `double_take_equity` and `double_pass_equity` — on
[MissedDouble](#misseddouble-sub-object-on-checker-analysis),
[CubeDecision](#cubedecision-sub-object-on-checker-analysis) and
[Cube Analysis](#cube-analysis-inline-on-cube-plies) alike — hold **normalized
cubeful equity** in GammonView files, in match play as well as money play. A
pass reads exactly `1.0` at every score.

This is the one place GammonView's *values* differ from the base format rather
than adding to it. Since the base spec's 2026-08-10 sync, those three fields
hold **raw MWC** in match play ("because that is the unit the analyser decides
in"), with `no_double_norm_eq` / `double_take_norm_eq` / `double_pass_norm_eq`
derived on read as the normalized view. GammonView stores the normalized value
directly and does not emit the `*_norm_eq` fields; MWC stays compute-on-read via
[MWC Conversion](#mwc-conversion) above.

The reason is quantization. The binary encodes these as `int16` at 1e-4, which
is ~20,000 usable steps for normalized equity at *any* score, but only 1,252
steps for raw MWC at 7-away/7-away and **16** at 2-away/25-away. Storing MWC
would destroy the field at exactly the scores where cube decisions are most
delicate.

**What this costs an interoperating reader.** The binary field is unit-agnostic
and the convention is a *producer* one — the reference `ogxm_json.cpp` stores
these three verbatim and only *adds* `*_norm_eq` on read — so a base reader
ingesting a GammonView match-play file gets wrong numbers **only** in the three
derived `*_norm_eq` values (our `double_pass_equity = 1.0` renders as `+7.99` at
7-away/7-away). `should_double`, `correct_action`, `equity_loss`,
`classification` and PR all survive: the equity↔MWC map is affine and
*increasing*, so every comparison is order-preserving, and `equity_loss` is
never converted — both sides keep errors on the normalized (EMG) scale. Money
play is identical on both sides. Converting a GammonView file for a base reader
therefore means converting three fields per cube record, using the same MET
anchors as above.

The intended fix is a producer-unit flag in ANAL's reserved bytes, which would
let both conventions coexist explicitly; it is proposed upstream and not yet in
the base spec.

---

## Alt (Alternative Move)

```json
{
  "move": [{ "from": 12, "pips": 3 }, { "from": 15, "pips": 1 }],
  "notation": "13/10 10/9",
  "equity": 0.15,
  "eval": { ... },
  "is_played": false,
  "diff": 0.0,
  "eval_level": "3ply"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `move` | Step[] | The checker move, structured (source of truth). Each step is `{ "from": absolute point 0-25, "pips": 1-6 }`. |
| `notation` | string | **Derived** display view of `move` (e.g. `"13/7 8/5"`, `"bar/20"`). Not stored in binary; rendered from `move` + player color on output. |
| `equity` | float | Equity of this move (authoritative; may be cubeful). |
| `eval` | Eval | NN evaluation after this move |
| `is_played` | bool | `true` if this is the move that was actually played |
| `diff` | float | `this_equity - best_equity` (<= 0, output only) |
| `eval_level` | string | **[GammonView extension]** Eval level used for this specific alternative: `"1ply"`, `"2ply"`, `"3ply"`, etc. Necessary when two-level analysis is used (a cheap screener plus a full evaluator) — different alternatives in the same position may have been evaluated at different depths. |

`from_json` reads `move`; `notation` and `diff` are ignored on input. The played move is always present in the alternatives list with `is_played: true`. Alternatives are sorted by equity descending (best first).

---

## MissedDouble (sub-object on checker analysis)

Present when the player should have doubled before rolling. In the binary format, stored as CUBE entries with type=2.

```json
{
  "no_double_equity": 0.30,
  "double_take_equity": 0.55,
  "double_pass_equity": 1.00,
  "equity_loss": 0.08,
  "correct_action": "double",
  "eval": {"win": 0.62, "gammon_win": 0.21, "bg_win": 0.02,
           "gammon_loss": 0.08, "bg_loss": 0.01, "equity": 0.30},
  "eval_level": "2ply"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `no_double_equity` | float | Equity if player doesn't double. **Normalized cubeful equity, not MWC** — see [Cube equities are normalized equity, not MWC](#cube-equities-are-normalized-equity-not-mwc-gammonview-divergence). |
| `double_take_equity` | float | Equity if player doubles and opponent takes |
| `double_pass_equity` | float | Equity if player doubles and opponent passes. Exactly `1.0` at every score, per the note above. |
| `equity_loss` | float | Equity lost by not doubling (>= 0) |
| `correct_action` | string | `"double"` (always, since it's a missed double) |
| `eval` | Eval | **Optional.** Pre-roll probabilities for the cube decision, same as `CubeDecision.eval`. Absent in files written before writers stored it. |
| `eval_level` | string | **Optional.** Depth of that evaluation. |

`eval` is the position **before** the roll, so it is not the parent checker
analysis's `eval` (which is post-roll, describing the play that was made
instead). A view showing the cube must read this one; falling back to the
parent's answers a different question.

`classification` is not stored (compute-on-read from `equity_loss`). Equity↔MWC
conversion is likewise compute-on-read (no stored anchors, on this sub-object
or its parent) -- see [MWC Conversion](#mwc-conversion) below.

---

## CubeDecision (sub-object on checker analysis)

Present on checker plies where the cube was live and the correct action was no-double (player correctly held — a non-error live cube). Mutually exclusive with `missed_double`. In the binary format, stored as a base `CUBE` entry with `type=4` (`live_checker`), correlated to the checker ply by `game_index`/`ply_index` — matching upstream OGXM (no GammonView-specific chunk needed).

This covers the analysis case that `missed_double` does not: when the correct action is to hold, we still want to display the ND/DT/DP equities (standard practice in analysis software) and record whether this no-double counted as a decision. The shape mirrors the maintainer's base OGXM cube model — the same `should_double`/`action` split as `missed_double` (error) vs a non-error live cube.

```json
{
  "should_double": false,
  "no_double_equity": 0.25,
  "double_take_equity": 0.50,
  "double_pass_equity": 0.80,
  "action": "no_double",
  "equity_loss": 0.0,
  "eval": { ... },
  "decision": true,
  "eval_level": "2ply"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `should_double` | bool | Whether the correct action is to double. For a live-cube hold this is `false`. |
| `no_double_equity` | float | Equity if player doesn't double. **Normalized cubeful equity, not MWC** — see [Cube equities are normalized equity, not MWC](#cube-equities-are-normalized-equity-not-mwc-gammonview-divergence). |
| `double_take_equity` | float | Equity if player doubles and opponent takes |
| `double_pass_equity` | float | Equity if player doubles and opponent passes. Exactly `1.0` at every score, per the note above. |
| `action` | string | Derived label: `"no_double"` when not doubling is correct; else `"double_take"` (when `double_pass_equity >= double_take_equity`) or `"double_pass"`. |
| `equity_loss` | float | Equity lost by the decision (>= 0; 0.0 when the correct action was taken) |
| `eval` | Eval | NN evaluation at the cube decision |
| `decision` | bool | **[GammonView extension]** Whether this cube decision counted as a PR decision |
| `eval_level` | string | **[GammonView extension]** Mode-aware eval level of the cube result used |

`classification` is not stored (compute-on-read from `equity_loss`), and so is
MWC -- see [MWC Conversion](#mwc-conversion) below.

---

## Cube Analysis (inline on cube plies)

The `analysis` object on a cube action ply (action_id 21–23). The analysis type is inferred from the ply's `action_id`. Equity↔MWC conversion for these equities is compute-on-read, same as everywhere else -- see [MWC Conversion](#mwc-conversion) below.

### Double decision (action_id 21)

```json
{
  "correct_action": "double",
  "played_action": "double",
  "no_double_equity": 0.30,
  "double_take_equity": 0.55,
  "double_pass_equity": 1.00,
  "eval": {"win": 0.62, ...},
  "equity_loss": 0.0,
  "decision": true,
  "eval_level": "3ply"
}
```

### Take/pass decision (action_id 22–23)

```json
{
  "correct_action": "take",
  "played_action": "take",
  "no_double_equity": -0.30,
  "double_take_equity": -0.55,
  "double_pass_equity": -1.00,
  "eval": {"win": 0.38, ...},
  "equity_loss": 0.0,
  "decision": true,
  "eval_level": "3ply"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `correct_action` | string | `"no_double"`, `"double"`, `"take"`, `"pass"` |
| `played_action` | string | Same values as `correct_action` |
| `no_double_equity` | float | Equity if player doesn't double. **Normalized cubeful equity, not MWC** — see [Cube equities are normalized equity, not MWC](#cube-equities-are-normalized-equity-not-mwc-gammonview-divergence). |
| `double_take_equity` | float | Equity if player doubles and opponent takes |
| `double_pass_equity` | float | Equity if player doubles and opponent passes. Exactly `1.0` at every score, per the note above. |
| `eval` | Eval | NN evaluation of the position |
| `equity_loss` | float | Equity lost by the decision (>= 0) |
| `decision` | bool | **[GammonView extension]** Whether this cube action counted as a PR decision. Cube decisions may be excluded in trivial positions (e.g. dead cube, negligible equity difference). |
| `eval_level` | string | **[GammonView extension]** Eval level of the cube result actually used (cheap screener or upgraded pass) in a two-level scheme: `"1ply"`, `"2ply"`, `"3ply"`, `"truncated2"`, etc. Present on both double and take/pass decisions — a take/pass is judged by the same evaluation as the double beside it, so it carries the same level. In the binary format this is the `GVAN` cube section's per-entry `eval_level`; omit in JSON when it equals `GVAN.base_eval_level`. |
| `ply` | uint8 | **Optional.** Per-decision analysis depth (**base OGXM field**, `CUBE.ply`): the depth this cube decision was actually judged at. Present only when it exceeds `analysis_info.ply` — i.e. a two-pass scheme deepened this decision past the first-pass screen — exactly as on the checker Analysis object; omitted means "same as the base ply". Derived from `eval_level`, which stays authoritative because a plain integer cannot express a *mode* (`"truncated2"` yields `ply = 2`). Emitting it is what lets a reader that ignores `GVAN` still report the right depth. Not emitted on `missed_double`/`cube_decision` sub-objects: there the base format assigns `CUBE.ply` to the parent checker decision's ply. |

---

## Resign Analysis (inline on resign plies)

The `analysis` object on a resign ply (action_id 27 resign game / 28 resign match). Stored in the binary as a CUBE entry with `type=3`.

```json
{
  "played_action": "resign",
  "equity_loss": 0.0,
  "resign_error": 0.0,
  "take_resign_error": 0.0,
  "eval": { ... }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `played_action` | string | Always `"resign"` |
| `equity_loss` | float | Magnitude of the resignation error (>= 0; classification is computed from it on read) |
| `resign_error` | float | Raw signed error made by resigning (clamped to +/-3) |
| `take_resign_error` | float | Raw signed error accepting the resignation (clamped to +/-3) |
| `eval` | Eval | **Optional.** Final-position eval (omitted when unavailable) |

---

## Conventions (GammonView)

- **White = Player 1, Black = Player 2.** Mat files carry no player color information; this is a fixed convention, not stored in the file.
- A ply belongs to white if `color == 1`, to black if `color == 0`.
- `ogid_before` and `ogid_after` are populated on every ply by the `ogid.py` module.

---

## Limits

| Limit | Value |
|-------|-------|
| Max games | 100 |
| Max plies per game | 1500 |
| Max alternatives per decision | 50 |
| Max player name | 255 bytes |

---

## Example: Match with GammonView Analysis

```json
{
  "match_length": 7,
  "player_white": "Filias",
  "player_black": "Opponent",
  "white_score": 7,
  "black_score": 3,
  "result": 1,
  "source": 1,
  "timestamp": 1710500000,
  "crawford": false,
  "jacoby": false,
  "beaver": false,
  "cube_limit": 64,
  "event": "BGS Weekly",
  "site": "Backgammon Studio",
  "analysis_info": {
    "ply": 3,
    "eval_level": "3ply",
    "luck_eval_level": "1ply",
    "model_id": "gv-bgsage/2.0.20260907",
    "timestamp": 1710500100,
    "duration_ms": 12300
  },
  "games": [
    {
      "game_index": 0,
      "winner": 0,
      "points_won": 2,
      "is_crawford": false,
      "plies": [
        {
          "color": 1,
          "action_id": 2,
          "d1": 1,
          "d2": 3,
          "moves": [
            {"from": 8, "pips": 3},
            {"from": 6, "pips": 1}
          ],
          "ogid_before": "11jjjjjhhhccccc:ooddddd88866666:N0N::W:IW:0:0:7:0",
          "ogid_after": "11jjjjjhhe5cccc:ooddddd88866666:N0N:13:B:R:0:0:7:0",
          "analysis": {
            "eval": {
              "win": 0.52, "gammon_win": 0.12, "bg_win": 0.01,
              "gammon_loss": 0.10, "bg_loss": 0.005, "equity": 0.445
            },
            "best_equity": 0.15,
            "played_equity": 0.12,
            "equity_loss": 0.03,
            "decision": true,
            "luck": 0.02,
            "alternatives": [
              {
                "move": [{"from": 13, "pips": 3}, {"from": 6, "pips": 1}],
                "notation": "13/10 6/5",
                "equity": 0.15,
                "eval": {"win": 0.53, "gammon_win": 0.12, "bg_win": 0.01, "gammon_loss": 0.10, "bg_loss": 0.005, "equity": 0.455},
                "is_played": false,
                "diff": 0.0,
                "eval_level": "3ply"
              },
              {
                "move": [{"from": 8, "pips": 3}, {"from": 6, "pips": 1}],
                "notation": "8/5 6/5",
                "equity": 0.12,
                "eval": {"win": 0.52, "gammon_win": 0.11, "bg_win": 0.01, "gammon_loss": 0.10, "bg_loss": 0.005, "equity": 0.435},
                "is_played": true,
                "diff": -0.03,
                "eval_level": "3ply"
              }
            ]
          }
        },
        {
          "color": 1,
          "action_id": 21,
          "ogid_before": "...",
          "ogid_after": "...",
          "analysis": {
            "correct_action": "no_double",
            "played_action": "no_double",
            "no_double_equity": 0.18,
            "double_take_equity": 0.35,
            "double_pass_equity": 1.00,
            "eval": {"win": 0.55, "gammon_win": 0.08, "bg_win": 0.002, "gammon_loss": 0.09, "bg_loss": 0.004, "equity": 0.538},
            "equity_loss": 0.0,
            "decision": true
          }
        },
        {
          "color": 1,
          "action_id": 14,
          "d1": 3,
          "d2": 6,
          "moves": [{"from": 13, "pips": 3}, {"from": 8, "pips": 6}],
          "ogid_before": "...",
          "ogid_after": "...",
          "analysis": {
            "eval": {"win": 0.65, "gammon_win": 0.10, "bg_win": 0.01, "gammon_loss": 0.05, "bg_loss": 0.002, "equity": 0.708},
            "best_equity": 0.20,
            "played_equity": 0.20,
            "equity_loss": 0.0,
            "decision": true,
            "luck": 0.01,
            "alternatives": [...],
            "cube_decision": {
              "should_double": false,
              "no_double_equity": 0.25,
              "double_take_equity": 0.50,
              "double_pass_equity": 0.80,
              "action": "no_double",
              "equity_loss": 0.0,
              "eval": { ... },
              "decision": true,
              "eval_level": "3ply"
            }
          }
        }
      ]
    }
  ]
}
```

## Example: Ply with Missed Double

```json
{
  "color": 1,
  "action_id": 14,
  "d1": 3,
  "d2": 6,
  "moves": [{"from": 13, "pips": 3}, {"from": 8, "pips": 6}],
  "ogid_before": "...",
  "ogid_after": "...",
  "analysis": {
    "eval": {"win": 0.65, "gammon_win": 0.10, "bg_win": 0.01, "gammon_loss": 0.05, "bg_loss": 0.002, "equity": 0.708},
    "best_equity": 0.20,
    "played_equity": 0.20,
    "equity_loss": 0.0,
    "decision": true,
    "luck": 0.01,
    "alternatives": [...],
    "missed_double": {
      "no_double_equity": 0.30,
      "double_take_equity": 0.55,
      "double_pass_equity": 1.00,
      "equity_loss": 0.08,
      "correct_action": "double",
      "eval": {"win": 0.62, "gammon_win": 0.21, "bg_win": 0.02, "gammon_loss": 0.08, "bg_loss": 0.01, "equity": 0.30},
      "eval_level": "2ply"
    }
  }
}
```
