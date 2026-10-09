# The GammonView document — OGXM JSON, as of 2.0

This page is the reference for **the document**: the Python dict that
`gvformat.read_gvab` returns and `write_gvab` takes, the JSON a `.gva` file
holds, and the object the JavaScript `read_gvab` / `write_gvab` pair exchange. A
match is `match → games → plies → analysis`, with the clock, the video and the
annotations on the objects they belong to.

**What it is not.**

- It is **our own JSON**. It is not HedgeHog's JSON projection of OGXM v2
  (Appendix A of the [v2 spec](https://gitlab.com/eranlambooij/hedgehog-public)
  says that projection is not an interchange format, and ours is a different
  shape: normalized equities, `color` rather than `seat`, flat per-ply keys,
  documents keyed the way GammonView draws them). Do not hand this JSON to
  another OGXM program, and do not expect to read theirs here.
- It is **not the file**. A `.gvab` is OGXM v2 binary; how this document is
  written into one, what travels in `x-gammonview-…` annotations, and every way
  reading a file back differs from what was written are in
  [`OGXM_V2_PROFILE.md`](OGXM_V2_PROFILE.md). This page says what the keys
  *mean*; that one says how they are *stored*. The v1 binary format written
  before 2.0.0 is frozen in
  [`v1/OGXM_FORMAT_SPEC_GAMMONVIEW.md`](v1/OGXM_FORMAT_SPEC_GAMMONVIEW.md); see
  [Reading v1 files](#reading-v1-files) below.
- It stores **no statistics**. PR, decision counts, luck totals, MWC and
  classification are derived on read ([Derived values](#derived-values-are-not-stored)).

Every key of every OGXM v2 field and section has a place in the document, so a
v2 file loses nothing on its way in. The *v2 field* column below names where it
goes. Where a v2 file holds something the document cannot model (signatures,
unknown sections, presence-bit tails a later spec adds), it rides along in
[`_ogxm2_passthrough`](#_ogxm2_passthrough).

---

## Conventions

- **Absent is not zero.** A key is omitted when the file has no value for it.
  Where this page says *default-omitted*, the key is absent when its value equals
  v2's default (a `variant` of 0, an `initial_cube_value` of 1, a false flag, an
  empty string), and a writer treats absent and default the same.
- **White = Player 1, Black = Player 2.** `color` is 1 for White and 0 for
  Black. (v2's `seat` is the reverse, 0 White; the reader and writer swap it.)
  Which player an `analysis` belongs to is its ply's `color`.
- **Points** are absolute: 0 is White's bar and 25 is Black's, 1–24 the board
  from White's 1-point. White moves from high to low; a step `{"from": 13,
  "pips": 3}` is a checker leaving point 13 for point 10. A board is
  `int[26]`, signed, `+` White and `−` Black, index 0 White's bar … 25 Black's
  bar.
- **Equity is normalized.** Every `*equity*`, `equity_loss`, `luck` and `*_error`
  in the document is a **normalized cubeful equity** in the mover's frame — the
  scale on which dropping the cube is −1 and cashing it is +1 — in match play as
  well as money play, and whatever unit the file stores it in (MWC in a v2 match
  block). See [Cube equities are normalized equity, not MWC
  (GammonView divergence)](#cube-equities-are-normalized-equity-not-mwc-gammonview-divergence).
  A document value carries at most four places.
- **Probabilities** are floats in [0, 1], four places.
- **Units.** Times named `…_ms` are **milliseconds**. `timestamp`,
  `analysis_info.timestamp` and `clock_info.start_timestamp` are Unix
  **seconds**. `completed_at` (match and block), `wall_ms` and annotation `at` are
  Unix **milliseconds**. The two counts are never mixed in one key.
- **Enums are integers**, v2's (spec §9), except the cube verdict labels
  (`"no_double"`, `"double"`, `"take"`, `"pass"`), the eval-level labels and the
  `played_action` labels, which are strings. An enum value this page does not
  name is kept as it came.
- **Output only** keys are produced by a reader (or a converter) for display and
  ignored by the writer, which derives them again: `ogid_before`, `ogid_after`,
  `notation`, `diff`, `played_equity`, `eval.equity`, `game_index`,
  `is_crawford`, `first_to_move`, `initial_board`, `action` (on a
  `cube_decision`) and `analysis_info.preset`.

---

## Top-level object

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
  "crawford": true,
  "jacoby": false,
  "beaver": false,
  "raccoon": false,
  "cube_limit": 64,
  "event": "World Championship",
  "site": "Monte Carlo",
  "analysis_info": {},
  "games": []
}
```

The fifteen keys from `match_length` to `site` are present on every document a
reader returns. The rest are present when the file states them.

### Match

| Key | Type | Meaning | v2 field |
|---|---|---|---|
| `match_length` | uint | Points to win; **0 is a money game** | `match_length` |
| `variant` | uint | 0 backgammon (absent), 1 nackgammon, 2 hypergammon, 3 longgammon. `gvanalysis` refuses a non-zero variant | `variant` |
| `player_white`, `player_black` | string | Player names; `""` when unnamed | `white_name`, `black_name` |
| `white_score`, `black_score` | uint | Final score. v2 stores no score — it derives it from the games (M8) — so a read gives the games' sum (each deciding game capped, see [Game](#game)), or the file's stated score when another producer states one | none; a stated score the games do not add up to travels in `x-gammonview-score` |
| `score_start` | `[white, black]` | The score before the first recorded game (a match joined mid-way). Absent = `[0, 0]`. It feeds every score, the Crawford game and every OGID | `score_start` |
| `result` | uint | 0 incomplete, 1 White won, 2 Black won, 3 abandoned. Stored only where the score does not decide it, so 3 is always stored and always read back as 3 | `result` |
| `source` | uint | 0 play, 1 `.mat` import, 2 `.xg` import, 3 transcribed, 4 `.bgf` import, 5 `.sgf` import. Absent in a read = 0 | `source` |
| `timestamp` | uint | Match start, Unix **seconds**; 0 = unknown | `started_at` (ms) |
| `completed_at` | uint | Match end, Unix **milliseconds** | `completed_at` |
| `date_precision` | uint | 0 the day, 1 the month, 2 the year: `timestamp` is that period's first instant. Absent = an exact instant | `date_precision` |
| `crawford`, `jacoby`, `beaver`, `raccoon` | bool | The rules in force | `rules` bits 0–3 |
| `auto_doubles` | bool | Automatic doubles in force. Default-omitted | `rules` bit 4 |
| `rules_other` | uint | `rules` bits 5 and up, which the spec has not named, kept whole. Absent = 0 | `rules` |
| `crawford_before_start` | bool | The Crawford game was already over when the first recorded game began. Default-omitted | `crawford_before_start` |
| `cube_limit` | uint | Largest cube allowed (a power of two); **0 = no limit**. A source's "no limit" that is not a power of two reads back as 0 | `cube_limit` |
| `player_seat` | uint | Which seat the recording player held: 0 White, 1 Black, 2 neither | `player_seat` |
| `event` | string \| null | Event name. Never inferred from `site`, nor the reverse | `event` (≤120 bytes; longer in `x-gammonview-event`) |
| `event_year` | uint | The event's year; needs an `event` | `event_year` |
| `stage` | string | e.g. `"Final"` | `stage` |
| `round` | uint | Round number, 1–99 | `round` |
| `table` | string | Table or board label | `table` |
| `city` | string | | `city` |
| `country` | string | Two upper-case letters | `country` |
| `event_url` | string | An `https` URL | `event_url` |
| `platform` | string | The platform's lower-case host name, e.g. `"opengammon.com"`. **Not** `site`: this key names v2's `site` | `site` |
| `match_ref` | string | The match's id on that platform; needs a `platform` | `match_ref` |
| `rated` | bool | A rated match. Default-omitted | `rated` |
| `site` | string \| null | Where the match was played: free text from `[Site]`, XG's location, BGF's `site`. Sometimes a city, sometimes a platform, and nothing says which. A read with none stored gives `city`, else `platform`, else null | `x-gammonview-site`, unless it equals what a reader would derive anyway |
| `white_profile`, `black_profile` | [Profile](#profile) | A player's platform identity | `player` records |
| `clock_info` | [ClockInfo](#clock_info) | The player clock | `CLCK` |
| `video_info` | [VideoInfo](#video_info) | The recording the match was transcribed from | `VIDO` |
| `annotations` | [Annotation](#annotations)[] | Notes on the match | `ANNO`, match scope |
| `analysis_info` | [AnalysisInfo](#analysisinfo) | The primary analysis block's metadata. Absent when the match is unanalyzed | `ANAL` |
| `analyses_info` | AnalysisInfo[] | **Every** block, primary first; present only when there is more than one ([Multiple analyses](#multiple-analyses)) | `ANAL` × n |
| `games` | [Game](#game)[] | The games in play order. Required | `GAME` × n |
| `_ogxm2_passthrough` | object | Opaque, private. See [below](#_ogxm2_passthrough) | — |
| `_unknown_chunks` | object[] | Opaque, private; v1 files only. See [Reading v1 files](#reading-v1-files) | — |
| `_base_analyses` | int[] | Private; see [below](#_base_analyses) | — |

A value v2 cannot hold (a string past its cap, a `round` of 100, a `country` in
lower case, a `user_id` with no `platform`) is **not** refused: it travels in an
`x-gammonview-<key>` annotation and reads back as it was
([`OGXM_V2_PROFILE.md`](OGXM_V2_PROFILE.md) §1). Only a document that breaks a
rule no annotation can stand in for is refused, with a `ValueError`.

### Profile

A player as a platform knows them. Every key is optional.

| Key | Type | Meaning |
|---|---|---|
| `user_id` | string | The platform's id for the player; needs the match's `platform` |
| `rating` | float | To 0.01 |
| `rating_system` | string | Lower case, e.g. `"opengammon"`; needs `rating` |
| `country` | string | Two upper-case letters |
| `kind` | uint | 0 human, 1 bot, 2 engine. Absent = not stated |

### `clock_info`

The clock's settings. The elapsed-time reading of each ply is `timestamp_ms` on
the ply. Present only when the file has a valid clock.

```json
{ "reserve_ms": 120000, "delay_ms": 12000, "increment_ms": 5000,
  "start_timestamp": 1790380800, "flags": 3, "precision": 100 }
```

| Key | Type | Meaning | v2 field |
|---|---|---|---|
| `reserve_ms` | uint32 | Initial reserve per player | `CLCK.reserve_ms` |
| `delay_ms` | uint32 | Delay per move | `CLCK.delay_ms` |
| `increment_ms` | uint32 | Increment per decision | `CLCK.increment_ms` |
| `start_timestamp` | uint32 | Clock start, Unix **seconds** | `CLCK.start_timestamp` |
| `flags` | uint8 | The stored flags byte, as it is: bit 0 White berserk, bit 1 Black berserk, bits 2–7 unassigned and kept whole. **Omitted when 0** | `CLCK.flags` |
| `precision` | uint | The step, in ms, the readings are held to. **Omitted when it is the canonical 10** | `CLCK.precision` |

A clock the writer cannot write — a reading on a ply after one without a
reading, a first reading that is not 0, readings that run backwards — is not
written, and its readings go with it; the video and the annotations stay. A
negative or non-integer value is a `ValueError`.

### `video_info`

```json
{ "kind": 2, "is_live": true, "offset_ms": -250, "url": "https://twitch.tv/videos/123" }
```

| Key | Type | Meaning | v2 field |
|---|---|---|---|
| `kind` | uint8 | 0 unknown, 1 YouTube, 2 Twitch, 3 file (`url` is then the file's basename) | `VIDO.kind` |
| `is_live` | bool | Recorded live. Default-omitted | `VIDO.is_live` |
| `offset_ms` | int32 | A sync correction added to every mark's `video_ms`. Default-omitted | `VIDO.offset_ms` |
| `url` | string | Where it is. A URL v2 would drop (not `https`, over 512 bytes) travels in `x-gammonview-video.url` and is put back | `VIDO.url` |

The marks are on the plies: `video_ms`, `wall_ms`, `behind_live_ms` and
`video_hand_anchored` ([Ply](#ply)). `wall_ms` is the mark's Unix time in
milliseconds; `behind_live_ms` is held in whole seconds and at most 65 534 000.

---

## AnalysisInfo

The metadata of one analysis block. The decisions themselves are on the plies.
One block is the common case: `analysis_info` plus an `analysis` on each analyzed
ply. See [Multiple analyses](#multiple-analyses).

```json
{
  "ply": 2,
  "eval_level": "3ply",
  "luck_eval_level": "1ply",
  "model_id": "gv-bgsage/2.0.20260907",
  "met_id": "kazaross-xg2",
  "timestamp": 1768141380,
  "analysis_id": "ea7ae02e-7c4a-8759-861b-eec0502be9b7",
  "complete": true,
  "engine_build": "2.0.20260907"
}
```

| Key | Type | Meaning | v2 field |
|---|---|---|---|
| `analysis_id` | string | The block's identifier (a hyphenated UUID). Always present on a read, for every block, not only ours: a signature and an annotation name the block by it. Optional on a write; a block without one gets a deterministic id | `ANAL.analysis_id` |
| `ply` | uint | The block's base search depth. In a two-pass scheme the first-pass (screen) depth; a decision deepened beyond it says so in its own `ply`. Always present on a read (0 when the file states none) | the block level's `checker_ply` |
| `eval_level` | string | Our label for the block's level: `1ply`…`4ply`, `truncated1`…`truncated3`, `rollout`, `database`. A label, not a vocabulary a reader may assume closed — another producer's block reads with a label derived from its depth (`"3ply"`) or `"rollout"` | the block level's `preset`; when it differs from the label most decisions share, it travels in the block's annotation |
| `luck_eval_level` | string | The depth `luck` was computed at (`"1ply"` today), independent of `eval_level`. Present when the block has luck | the luck records' level; the label travels in the block's annotation |
| `model_id` | string | Engine identifier, free-form: a label, not a key. Our analyses write `gv-bgsage/<bgsage version>`; the converters write the foreign engine's bare name (`"xg"`, `"bgblitz"`, `"gnubg"`); another producer's is its own (HedgeHog's is a UUID) | `model_id` |
| `model_name` | string | A display name. Show `model_name ?? model_id` | `model_name` |
| `model_digest` | string | 64 lower-case hex digits | `model_digest` |
| `engine_build` | string | The build that ran, e.g. the bgsage version | `engine_build` |
| `producer` | uint | 0 OGX, 1 eXtreme Gammon, 2 external engine, 3 BGBlitz, 4 human annotation | `producer` |
| `complete` | bool | The block analyzed the whole match. Absent = unstated | `complete` |
| `coverage` | `[game_index, ply_index]`[] | The plies the block attempted, which can be more than it holds a decision for. Ascending, no repeats; always includes every ply the block holds a decision for | `coverage` |
| `timestamp` | uint | When the analysis ran, Unix **seconds**; 0 if unknown | `started_at` (ms) |
| `completed_at` | uint | When it finished, Unix **milliseconds** | `completed_at` |
| `duration_ms` | uint | Wall-clock analysis time. Absent when 0 | `duration_ms` |
| `met_id` | string | The match equity table the block's MWCs come from: `"kazaross-xg2"` on a match block of ours, `"bgblitz"` on a `.bgf` import (its own table — see [`mwc_frame`](#mwc_frame)). Absent on a money block | `met_id` |
| `currency` | uint | 0 cubeless, 1 cubeful money, 2 cubeful match. **Omitted when it is the default** — cubeful match in a match, cubeful money in a money game. A document's equities are normalized whatever the currency; this names the unit the *file* holds | `currency` |
| `cube_efficiency` | float | Cube efficiency assumed, to four places | `cube_efficiency` |
| `tables` | string | The bearoff or other tables used | `tables` |
| `dials` | [Dials](#dials) | The engine settings that affect results | `dials` |
| `sources` | string[] | Identifiers (UUIDs) of the blocks this one draws on; a decision's `producer_ref` indexes it | `sources` |
| `level` | [Level](#level) | The block's level, when it is more than `ply` and `eval_level` say | `ANAL.level` |
| `preset` | string | **Output only, JSON only.** The name of the analysis preset (`"world_class"`, `"fast"`). A label, not a specification: preset definitions change between releases, so the levels a block was judged at are read from `ply`, `eval_level` and the decisions' own levels, never inferred from this name. Not carried by `.gvab`, so a round trip through one drops it | — |

`signature` is **not** a key of the document. A block's signature (`SIGN`, and a
match's `MSIG`) is kept as bytes in [`_ogxm2_passthrough`](#_ogxm2_passthrough),
so a rewrite can keep it valid; the document neither exposes nor verifies it.

### Level

A level states the search effort behind a result, at up to three tiers — the
block (`analysis_info.level`), a decision (`level` on a checker, cube, resign
or luck record) and an alternative — each overriding only what differs from the
tier above (v2 §6.4). **The reader keeps a `level` only where the true level is
something the labels (`eval_level`, `ply`) do not already give**, and resolves it
against the tier above, so a stated `level` lists every field it inherits. A
rollout's presence, a preset a producer named, a cube depth: those need it.

| Key | Type | Meaning |
|---|---|---|
| `preset` | string | The preset, as the producer called it (`"3ply"`, `"truncated2"`, `"++"`). Free |
| `checker_ply` | uint | Checker-play depth |
| `cube_ply` | uint | Cube-decision depth |
| `rollout` | object | Present when the result is a rollout |
| `no_rollout` | bool | Clears the rollout inherited from the tier above. Only a rollout can be cleared |

`rollout`:

| Key | Type | Meaning |
|---|---|---|
| `trials` | uint | Trials, at least 1 |
| `truncation_depth` | uint | Plies before truncation |
| `move_ply` | uint | Depth of the rollout's own play |
| `variance_reduction` | uint | 0 none, 1 standard, 2 extended |
| `seed` | **string** | The 64-bit seed as a decimal string (it does not fit in a JSON number's 53 bits). A number is accepted on write |
| `budget_ms` | uint | Time budget |
| `match_policy` | uint | 0 money, 1 match-aware |

### Dials

The resolved engine settings (v2 §6.3). Flags are `true` or absent.

| Key | Type | Meaning |
|---|---|---|
| `jacoby_resolved` | flag | The Jacoby rule was in force |
| `jacoby_mode` | uint | 0 auto, 1 forced on, 2 forced off |
| `cube_limit_resolved` | uint | The cube limit in effect |
| `cube_limit_mode` | uint | As `jacoby_mode` |
| `exact_bearoff` | flag | |
| `race_order` | flag | The one-sided bearoff overlay was enabled |
| `top_deep` | flag | |
| `top_deep_threshold` | float | An equity-loss gap, ≥ 0 |
| `rollout_budget_on` | flag | |
| `cube_rule` | uint | 0 unavailable, 1 Janowski, 2 scalar equity |
| `top_deep_keep` | uint | The most plays searched deep |
| `top_deep_accept` | uint | The plays searched deep whatever their gap |

A name v2 does not define, and a false flag, are not kept.

### Multiple analyses

A match can carry up to 64 analysis blocks in a v2 file (`gvformat.append_analysis`
stops at 64: `merge.MAX_ANALYSES`; a v1 file holds 16, and `write_gvab_v1` refuses more). Each block is an element of
`analyses_info`; its index is the **analysis_index**.

- **One block (the common case):** `analysis_info` and an inline `analysis` on
  each analyzed ply. No `analyses_info`.
- **Several:** `analyses_info` lists them all and `analysis_info` mirrors
  `analyses_info[0]`, the **primary**. Every analyzed ply carries the primary's
  `analysis` (so a reader of one analysis keeps working) and an `analyses` array
  with one entry per block that has a decision there, each a full analysis
  object plus an `analysis_index` into `analyses_info`.

On a write, the presence of `analyses_info` selects several-block mode — the
per-ply `analyses` arrays are read and `analysis` is ignored. Appending an
analysis to a document that already has one promotes it to this form
(`gvformat.append_analysis`); the existing blocks are kept, ours is last.

---

## Game

```json
{ "game_index": 0, "winner": 0, "points_won": 2, "is_crawford": false,
  "is_lastgame": false, "first_to_move": 0, "plies": [] }
```

| Key | Type | Meaning | v2 field |
|---|---|---|---|
| `game_index` | uint | 0-based. **Output only**: the writer numbers games by position | — |
| `winner` | uint | 0 White, 1 Black, **255 incomplete** (no winner). A winner is written with `points_won` | `GAME.winner` |
| `points_won` | uint | The game's **full** value (base points × cube), not capped to what the match still needed — see below. 0 when incomplete | `GAME.points_won` |
| `is_crawford` | bool | This is the Crawford game. **Output only**: derived as the first game at whose start one side is 1-away, when the match has the Crawford rule and `crawford_before_start` is not set | — |
| `is_lastgame` | bool | The match's final game | `GAME.is_last_game` |
| `first_to_move` | uint | 0 White, 1 Black — the first dice or resignation ply's `color`; 0 for a game with none. **Output only** | — |
| `termination` | uint | How it ended: 0 played out, 1 dropped, 2 resigned, 3 settled, 4 forfeited. Absent = not stated | `GAME.termination` |
| `initial_cube_value` | uint | The cube's value when the game opens (a power of two, within `cube_limit`). Default-omitted at 1 | `GAME.initial_cube_value` |
| `initial_cube_owner` | uint | 0 White, 1 Black, 2 centred. Default-omitted at 2 | `GAME.initial_cube_owner` |
| `auto_doubles` | uint | Automatic doubles this game opened with. The cube's real starting value is `initial_cube_value · 2^auto_doubles`. Default-omitted at 0 | `GAME.auto_doubles` |
| `initial_board` | int[26] | **Output only.** The board of a game that starts from a set-up position, as a read also states it as the game's first ply | `GAME.initial_board` |
| `annotations` | Annotation[] | Notes on the game | `ANNO`, game scope |
| `plies` | [Ply](#ply)[] | The game's actions in order | |

A game that starts from a set-up position has, as its first ply, a
`set_position` ply with **no dice**, by the side to move. The opening position
itself is never written that way: v2 never stores a default initial board, so a
set-position ply stating the opening position stays an ordinary ply.

**`points_won` is the game's full value, and the match length is applied on
accumulation.** A match ends the instant somebody reaches the target, so the
deciding game *banks* only what its winner still needed — a 4-point gammon won
at 12-away of 13 makes the match 13-7, not 16-7. The stored value stays `4`,
because it is also the only record of the win *type*: `points_won / cube` is how
a `.mat` reconstruction recovers "with gammon". Every consumer that sums
`points_won` into a running score **caps each game first**
(`gvformat.binary.cap_points_won`, JS `capPointsWon`); `white_score` and
`black_score` go through `clamp_match_score` / `clampMatchScore`. Money play
(`match_length = 0`) has no target and is never capped.

---

## Ply

One action: a checker play, a cube action, a resignation, or a game marker. The
analysis, the clock reading, the video mark and the annotations are on it.

```json
{
  "color": 1, "action_id": 14, "d1": 3, "d2": 6,
  "moves": [{"from": 13, "pips": 3}, {"from": 8, "pips": 6}],
  "ogid_before": "...", "ogid_after": "...",
  "timestamp_ms": 5230,
  "analysis": {}
}
```

| Key | Type | Meaning | v2 field |
|---|---|---|---|
| `color` | uint | 1 White, 0 Black — the actor. A game marker (24, 25, 26, 30) has no actor; a reader stamps it with the game's winner. A set-position ply *with* dice is stamped with the **player who made the play** (v2 seats it on the side to move next) | the ply's seat bit |
| `action_id` | uint | See [Action IDs](#action-ids). Absent is read as 30 (null) | the ply's action |
| `d1`, `d2` | uint | The dice, 1–6. The pair is unordered: a read gives `d1 ≤ d2`. On action 0–20 and on an illegal play restated as a position (31) | the dice action; `extras.dice` on 31 |
| `moves` | [Move](#move)[] | The checker steps of a dice play (0–20). Empty for a dance or a forced pass | the play's step bytes |
| `set_position` | int[26] | The board a set-position ply states (action 31) | `extras.board` |
| `settle_value` | float | A settlement's value, in points (action 34), to a millionth | `extras.settle_value` |
| `cube_value`, `cube_owner` | uint | A cube set by hand (action 36): its value (a power of two) and its owner, 0 White / 1 Black, **absent = centred** | `extras.cube_value`, `extras.cube_owner` |
| `resign_value` | uint | A resignation's factor: 1 single, 2 gammon, 3 backgammon (action 27, 28). Kept **only where it differs** from `points_won / cube`, which is what a reader and the writer derive otherwise | `extras.resign_value` |
| `extras_raw` | string | An action id this version assigns no meaning to (38 and up): the ply's extras record, base64, kept whole | the ply's extras |
| `ogid_before`, `ogid_after` | string | The position before and after the ply as an OGID. **Output only**: derived by replaying the game, so a rewrite never stores them. Note the OGID's colour field (field 5) is the player who *acted*, so the player on roll is its complement | — |
| `timestamp_ms` | uint32 | Milliseconds since the match started (the clock's elapsed-time reading). The first ply a clock covers reads 0. A series may end before the last ply, but never has a gap | `CLCK` series |
| `video_ms` | uint32 | A video mark: the position in the recording, before `video_info.offset_ms` | `VIDO` mark |
| `wall_ms` | uint64 | The mark's Unix time, ms. Absent = unknown | `VIDO` mark |
| `behind_live_ms` | uint32 | How far behind live the mark was, in whole seconds, as ms. Absent = unknown | `VIDO` mark |
| `video_hand_anchored` | bool | The mark was placed by hand, not interpolated. Default-omitted | `VIDO` mark |
| `analysis` | [Analysis](#analysis-objects) | The primary block's analysis of this ply. Absent when it has none | `DECS` |
| `analyses` | Analysis[] | In several-block mode: one entry per block with a decision here, each carrying `analysis_index` | `DECS` |
| `annotations` | Annotation[] | Notes on the ply | `ANNO`, ply scope |

A mark on a ply v2 has no record of — a game's set-up position, a game past the
256th, a ply past 65 535 — is not written.

A play that is not legal for its roll, or that any analysis block flags
`illegal_move`, is written as v2 records one: a set-position ply carrying the
dice, the `illegal` flag and the board the play produced. The document may hold
it either way; a read of a file we wrote gives back the form it was written in.
A **restated play** (action 31 *with* dice) is a checker ply for every purpose
that follows: it takes a checker analysis, its `luck` counts, its `illegal_move`
counts, and it is never a PR decision.

### Action IDs

| ID | Meaning | ID | Meaning |
|----|---------|----|---------|
| 0 | Dice 11 | 20 | Dice 66 |
| 1 | Dice 12 | 21 | Double |
| 2 | Dice 13 | 22 | Take |
| 3 | Dice 14 | 23 | Drop (pass) |
| 4 | Dice 15 | 24 | Game over |
| 5 | Dice 16 | 25 | Match over |
| 6 | Dice 22 | 26 | Final |
| 7 | Dice 23 | 27 | Resign game |
| 8 | Dice 24 | 28 | Resign match |
| 9 | Dice 25 | 29 | Forfeit |
| 10 | Dice 26 | 30 | Null (no dice) |
| 11 | Dice 33 | 31 | Set position |
| 12 | Dice 34 | 32 | Beaver |
| 13 | Dice 35 | 33 | Raccoon |
| 14 | Dice 36 | 34 | Settle |
| 15 | Dice 44 | 35 | Reserved — never valid |
| 16 | Dice 45 | 36 | Cube set |
| 17 | Dice 46 | 37 | Pass (a turn whose roll was not recorded) |
| 18 | Dice 55 | 38–62 | Unassigned; kept whole as `extras_raw` |
| 19 | Dice 56 | 63 / 64+ | An escaped id, kept whole as `extras_raw` |

For dice `a ≤ b` the id is `offset[a] + (b − a)` with `offset = [0, 6, 11, 15,
18, 20]`. A pass (37) is a turn with a dice-less play: no `d1`/`d2`, no `moves`.

What each ply holds: a dice play (0–20) takes a checker analysis; a double (21),
a take (22), a drop (23), a beaver (32) and a raccoon (33) can take a cube
analysis; a resignation (27, 28) a resign analysis; everything else none. A
**beaver is analyzed as the take it answers**, at the cube before the double,
and counts as a decision; the raccoon after it carries none (the engine has no
such decision).

### Move

| Key | Type | Meaning |
|---|---|---|
| `from` | uint | Start point, 0–25 (0 White's bar, 25 Black's) |
| `pips` | uint | Pips moved, 1–6 for a legal hop |

A dice ply holds at most the hops its roll allows — two for a non-double, four
for a double — because that is the room v2's ply record has. A legal play never
needs more. An *illegal* one can (13/9 with a 3-1, then 12/11, is three
die-moves), so a converter records each of its checkers in one step at that
checker's own pip distance: `pips` is then a distance the dice do not explain,
which is exactly what makes the play illegal. Where even that does not fit, the
play is written as a set-position ply (action 31) stating the resulting board.
Either way the ply replays to the position the source recorded — a truncated
play would leave every later board in the game wrong.

Two shapes of hop have no step at all and go straight to the set-position
ply. One runs *backwards*: `pips` is an unsigned forward distance, so no step
can hold it (a 6-5 played `14/8 15/10 6/8` is a real example). The other is
longer than the step byte holds — 6, in v2 — which only an illegal play reaches:
a 3-3 played `13/3 7/4` is ten pips in one hop. A restated play still carries
its `analysis`, and it is a checker analysis like any other: the alternatives
name the plays that were available, `luck` is the roll's, and `illegal_move`
keeps the play out of PR and decision counting. Only the *steps* were lost, and
nothing in the analysis needs them.

The order of a play's steps may change on a round trip: v2 replays them in stored
order and requires every intermediate position legal, so a bar entry is written
before any other move and a bear-off after the last checker comes home. The play
is the same (the positions compare equal); only its spelling moves.

---

## Analysis objects

An `analysis` is the evaluation of the decision(s) a ply poses, in the **mover's
frame** (and, for a cube analysis, the **doubler's**; see below). Its shape
follows the ply's action. Every kind has `decision` (does it count toward PR),
`equity_loss`, and, optionally, an `annotations` list and the v2 per-decision
fields in [Per-decision v2 fields](#per-decision-v2-fields).

### Eval

Five-output probabilities for a position, with the equity they imply.

```json
{ "win": 0.52, "gammon_win": 0.12, "bg_win": 0.01,
  "gammon_loss": 0.10, "bg_loss": 0.005, "equity": 0.445 }
```

`equity` is `win + gammon_win + bg_win − gammon_loss − bg_loss`, computed on
output and ignored on input. An `eval` whose probabilities are all zero is read
as "not recorded" and left out.

### Checker analysis

On a dice play (0–20) or a restated play (31 with dice).

```json
{
  "eval": { },
  "best_equity": 0.15,
  "played_equity": 0.12,
  "equity_loss": 0.03,
  "decision": true,
  "ply": 3,
  "luck": 0.02,
  "illegal_move": true,
  "alternatives": [],
  "missed_double": { },
  "cube_decision": { },
  "annotations": []
}
```

| Key | Type | Meaning | v2 field |
|---|---|---|---|
| `alternatives` | [Alt](#alternative)[] | The candidate plays, best first, with the played one flagged. Absent on a ply that holds only `luck` or a cube record (a dance) | `CHECKER.alternatives` |
| `eval` | Eval | The probabilities of the best play's position (the first alternative's) | derived |
| `best_equity` | float | The best play's equity | derived: the first alternative's; stated only when there is no alternative |
| `played_equity` | float | `best_equity − equity_loss`. **Output only** | derived |
| `equity_loss` | float | `best_equity` minus the played play's, ≥ 0 and unclamped above (an error can cost more than a point). Where the played play is listed it is derived from the alternatives | `CHECKER.equity_loss` (stated only when no alternative is played) |
| `decision` | bool | Whether the play counts as a PR decision: false for a forced move, a position whose candidates all score the same, and an illegal play. Our blocks read it back from the rule `basefill` applies to a foreign block, plus the block's recorded exceptions | none — `x-gammonview-analysis` records the exceptions |
| `ply` | uint | The depth this decision was ultimately judged at. Present only when it differs from `analysis_info.ply` (a two-pass scheme deepened it). The alternatives' `eval_level` strings stay authoritative for the mode | the decision level's `checker_ply` |
| `luck` | float | The roll's luck: `postroll − preroll` equity, from the roller's side, at `analysis_info.luck_eval_level` (not at `eval_level`). Only the difference is stored. Present on every rolled ply of a block that ran the luck pass, absent otherwise. A ply that holds only `luck` also has `decision: false` | the `ROLL` record |
| `illegal_move` | bool | The played board matched no legal play: a transcription error in the source, not a rule violation. Excluded from PR and decision counting. Absent (not `false`) otherwise | none — a restated play is illegal by construction; the flag is recorded only where the document disagrees |
| `mwc_frame` | `[mid, half]` | See [`mwc_frame`](#mwc_frame) | block annotation `frame=` |
| `level` | Level | The decision's level, when the labels do not give it | `CHECKER.level` |
| `luck_level` | Level | The luck record's level, when it is not the block's luck level | `ROLL.level` |
| `luck_producer_ref` | uint | The luck record's `producer_ref` (a name distinct from the checker decision's, since both share this object) | `ROLL.producer_ref` |
| `missed_double` | [MissedDouble](#misseddouble) | The cube decision made against, when the mover should have doubled before rolling | `CUBE` on this ply |
| `cube_decision` | [CubeDecision](#cubedecision) | The live cube's decision | `CUBE` on this ply |
| `annotations` | Annotation[] | Notes on the checker decision. A note on the **roll** is in the same list with `"kind": 3` | `ANNO`, decision scope |

Fresh converter output differs from a read in two more ways that `read_gvab`
removes: a `cube_decision` also carries `equity_loss`, and an alternative carries
`notation` and `diff` ([Alternative](#alternative)). A `.gva` written by
`gvan-match` is a read, so it has none of them.

A rewrite is also free to normalize one thing: a **decision that breaks a v2
invariant is not dropped, it moves** to an `x-gammonview-decisions` annotation
(an XG bearoff-database play interleaved with ply-evaluated ones, the played
move listed twice, the played move judged at another level than the best);
`DECS` carries a conforming version where there is one. Reading it back gives
the document as it was. Details: [`OGXM_V2_PROFILE.md`](OGXM_V2_PROFILE.md) §2.

### Alternative

One candidate play. Alternatives are best first; the played play is always
listed with `is_played: true` (more than one is flagged played only in a
document that breaks a v2 invariant, and travels in an annotation).

```json
{ "move": [{"from": 12, "pips": 3}, {"from": 15, "pips": 1}],
  "equity": 0.15, "eval": { }, "is_played": false, "eval_level": "3ply" }
```

| Key | Type | Meaning | v2 field |
|---|---|---|---|
| `move` | Step[] | The play, structured: the source of truth. Empty where no step list can hold the play (an illegal play's own alternative); nothing replays these steps | the alternative's steps |
| `equity` | float | The play's equity (cubeful in a cubeful block) | `equity` |
| `eval` | Eval | The probabilities after the play. Absent when the source recorded none | `probs` |
| `is_played` | bool | This is the play that was made | `is_played` |
| `eval_level` | string | The label of the level this alternative was judged at. Present on our blocks always, and on another producer's when its alternatives were judged at different levels (a screen plus a deeper pass; an XG match mixes `1ply` to `4ply`, `truncated2` and `database`) | the alternative level's `preset` |
| `rollout_se` | float | The standard error of a rolled-out `equity` | `rollout_se` |
| `cubeless_equity` | float | The cubeless equity, when the block is cubeful and the producer also computed it | `cubeless_equity` |
| `level` | Level | The alternative's level, when the labels do not give it | `level` |
| `notation` | string | A display view of `move` (`"13/7 8/5"`, `"bar/20"`). **Output only**, from conversion; absent in a read | — |
| `diff` | float | `equity − best_equity` (≤ 0). **Output only**, from conversion | — |
| `annotations` | Annotation[] | Notes on this alternative | `ANNO`, alternative scope |

An alternative's `eval_level` is how a viewer shows "which depth was this
judged at": it varies by alternative and by cube decision, so read it per entry.

### MissedDouble

On a checker analysis, when the mover should have doubled before rolling and
did not. A read also gives a [`cube_decision`](#cubedecision) beside it for
the same record, so a viewer drawing the cube panel reads one key.

```json
{ "no_double_equity": 0.30, "double_take_equity": 0.55, "double_pass_equity": 1.00,
  "equity_loss": 0.08, "correct_action": "double", "eval": { }, "eval_level": "2ply" }
```

| Key | Type | Meaning |
|---|---|---|
| `no_double_equity` | float | Equity if the mover does not double. Normalized cubeful equity — see [the divergence note](#cube-equities-are-normalized-equity-not-mwc-gammonview-divergence) |
| `double_take_equity` | float | Equity if the mover doubles and the opponent takes |
| `double_pass_equity` | float | Equity if the mover doubles and the opponent passes. Exactly `1.0` at every score |
| `equity_loss` | float | Equity lost by not doubling, ≥ 0 |
| `correct_action` | string | `"double"` for a missed double. A v2 verdict reads as its own label: `"too_good"` is not folded into `"no_double"` |
| `eval` | Eval | The **pre-roll** probabilities for the cube decision. Not the parent's `eval`, which is post-roll and describes the play that was made. A cube panel must read this one |
| `eval_level` | string | The label of that evaluation. Our blocks only |
| `decision` | bool | Whether the missed double counts as a PR decision. **Usually absent**: a source that records its own counted-ness (BGF's `pr.cubeError`) states it; otherwise a reader recomputes it from the three equities ([`OGXM_COMPUTED_FIELDS.md`](OGXM_COMPUTED_FIELDS.md)) |
| `level`, `take_point`, `window_searched`, `is_optional`, `is_free_cube`, `cubeful_take_value`, `currency`, `producer_ref`, `annotations` | | See [Per-decision v2 fields](#per-decision-v2-fields) |

### CubeDecision

On a checker analysis, the live cube's decision at the point the mover chose
not to double (a correct hold, or, beside a `missed_double`, the same record).
A hold with the cube dead has none.

```json
{ "should_double": false, "no_double_equity": 0.25, "double_take_equity": 0.50,
  "double_pass_equity": 0.80, "action": "no_double", "equity_loss": 0.0,
  "eval": { }, "decision": true, "eval_level": "2ply" }
```

| Key | Type | Meaning |
|---|---|---|
| `should_double` | bool | Whether the correct action is to double. False for a correct hold; true beside a `missed_double` |
| `no_double_equity`, `double_take_equity`, `double_pass_equity` | float | As in MissedDouble |
| `action` | string | **Output only.** `"no_double"` when holding is correct; else `"double_take"` (when `double_pass_equity ≥ double_take_equity`) or `"double_pass"` |
| `equity_loss` | float | Fresh converter output only (0.0 for a correct hold); not stored, absent in a read. The error belongs to `missed_double` |
| `eval` | Eval | The pre-roll probabilities |
| `decision` | bool | Whether this cube decision counted as a PR decision (not trivial). **Absent when a `missed_double` sits beside it** in a block of ours or in a v1 read — that record carries the count, so one cube is never counted twice. (A block of another producer completed by `basefill` currently gets one; see [`OGXM_COMPUTED_FIELDS.md`](OGXM_COMPUTED_FIELDS.md)) |
| `eval_level` | string | The label of the cube result used. Our blocks only |
| `level`, `take_point`, `window_searched`, `is_optional`, `is_free_cube`, `cubeful_take_value`, `currency`, `producer_ref`, `annotations` | | See [Per-decision v2 fields](#per-decision-v2-fields) |

The two sub-objects mirror v2's one `CUBE` record, with the verdict deciding
which it is: `verdict = double` with an `equity_loss` is a missed double; a
`verdict` of no double (or too good) is a hold.

### Cube ply analysis

On a double (21), a take (22), a drop (23), a beaver (32) or a raccoon (33).

```json
{ "correct_action": "double", "played_action": "double",
  "no_double_equity": 0.30, "double_take_equity": 0.55, "double_pass_equity": 1.00,
  "eval": { }, "equity_loss": 0.0, "decision": true, "eval_level": "3ply" }
```

| Key | Type | Meaning |
|---|---|---|
| `correct_action` | string | `"no_double"`, `"double"`, `"take"`, `"pass"`, and v2's other three verdicts under their own names: `"too_good"` (an offer), `"beaver"` and `"raccoon"` (responses). A read gives back the label the writer was given. A v1 file has only the first four and holds the others as the action they refine (`"too_good"` as `"no_double"`, `"beaver"` and `"raccoon"` as `"take"`) |
| `played_action` | string | What was played, as the ply's action: `"double"`, `"take"`, `"pass"`, `"beaver"`, `"raccoon"`. A beaver ply says `"beaver"` whether the analysis is fresh or read; its `correct_action` is the response the engine judged (a take or a pass) and the loss is that take's |
| `no_double_equity`, `double_take_equity`, `double_pass_equity` | float | The three cube equities **in the doubler's frame**, on a double and on every response alike: a drop of a good double reads `double_pass_equity = 1.0` and a `double_take_equity` below it, on the *responder's* ply too. A response ply whose double was not analyzed reads `no_double_equity = 0.0` |
| `eval` | Eval | The probabilities of the position the cube was decided in (the doubler's) |
| `equity_loss` | float | Equity the decision lost, ≥ 0: for a double, the doubler's; for a response, the responder's, `actual − optimal` of take and pass in the doubler's frame |
| `decision` | bool | Whether it counted as a PR decision (a trivial cube is excluded) |
| `eval_level` | string | The label of the evaluation actually used. A take or pass carries the same level as the double beside it. Our blocks only |
| `ply` | uint | The depth, when it exceeds `analysis_info.ply` (a two-pass scheme deepened it). `eval_level` stays authoritative for the mode: `"truncated2"` yields 2 |
| `level`, `take_point`, `window_searched`, `is_optional`, `is_free_cube`, `cubeful_take_value`, `currency`, `producer_ref`, `annotations` | | See [Per-decision v2 fields](#per-decision-v2-fields) |

The judged response of a **beaver** is a take: the cube analysis on the beaver
ply is the same record the take would have carried, it counts toward PR on the
same terms, and the raccoon after it holds no analysis
([`OGXM_COMPUTED_FIELDS.md`](OGXM_COMPUTED_FIELDS.md)).

### Resign analysis

On a resignation (27, 28).

```json
{ "resign_error": 0.31, "take_resign_error": -0.02, "equity_loss": 0.31,
  "decision": true, "eval": { }, "correct_value": 1 }
```

| Key | Type | Meaning | v2 field |
|---|---|---|---|
| `resign_error` | float | The signed error made by resigning | `RESIGN.resign_error` |
| `take_resign_error` | float | The signed error made by accepting the resignation | `RESIGN.take_resign_error` |
| `equity_loss` | float | Magnitude of the resignation error, ≥ 0 | `RESIGN.equity_loss` |
| `decision` | bool | Counts as a PR decision. A resignation's does, always | none |
| `eval` | Eval | The final position's probabilities. Absent when unavailable | `RESIGN.probs` |
| `correct_value` | uint | The value that should have been conceded: 0 none, 1 single, 2 gammon, 3 backgammon | `RESIGN.correct_value` |
| `level`, `producer_ref`, `annotations` | | See [Per-decision v2 fields](#per-decision-v2-fields) | |

### Per-decision v2 fields

v2's records carry fields our own analyzer never sets; the document holds each as
a key, on the object named, so another producer's file survives. Each is written
only where v2 does not derive it, and a decision whose fields `DECS` cannot hold
as they are moves to an annotation whole. Every equity among them is normalized
like all others.

| Key | On | Meaning |
|---|---|---|
| `level` | any decision, alternative, sub-object | A [Level](#level), only where the labels do not give it |
| `alternatives_total` | checker | How many legal plays there were before the list was cut. Meaningful only when the list was cut. The writer cuts a list at v2's 1024 alternatives and sets this to the full count (or keeps a larger one the decision already states); the played move stays in the list, taking the last place if it fell past the cut |
| `rollouts_done` | checker | How many alternatives were rolled out. Meaningful only when the list was cut |
| `deep_searched` | checker | How many alternatives were searched at the leading level. Meaningful only when the list was cut |
| `position_tags` | checker | An opaque bitfield (uint32) |
| `source_band` | checker | The producing engine's own skill rating, opaque |
| `producer_ref` | any decision | A 0-based index into the block's `sources`; absent = the block's own `analysis_id`. Must be below `len(sources)` for `DECS` to hold it |
| `take_point` | cube | The take point, a probability |
| `window_searched` | cube | The verdict came from a search, so the take point is a static estimate |
| `is_optional` | cube | Either action is acceptable |
| `is_free_cube` | cube | |
| `cubeful_take_value` | cube | The take value, normalized equity |
| `currency` | cube | The decision's own currency (uint), when it is not the block's. Cubeful match in a money game, and the block's own, are not written |
| `correct_value` | resign | See above |
| `rollout_se`, `cubeless_equity` | alternative | See above |

### `mwc_frame`

`[mid, half]` — a source's own match-equity frame for this ply, in the ply's
perspective: the MWC at normalized equity 0, and the MWC per unit of normalized
equity. `mwc = mid + equity · half`. A `.bgf` import sets it on every analyzed
ply, because BGBlitz's match equity table is not ours (7-away/7-away on a 1-cube
has half-width 0.05954 against our 0.0626) and a `.bgf` carries both the MWC and
the normalized equity of every decision, an exact linear map of each other. A
ply with a frame is written through it instead of our table, the block's `met_id`
is `"bgblitz"`, and the file holds BGBlitz's own MWCs; a read gives them back as
the frame. Absent means our table ([`met.mwc_anchors`](#mwc-conversion)). A
`.gvab` of v1 does not carry it.

### Cube equities are normalized equity, not MWC (GammonView divergence)

`no_double_equity`, `double_take_equity` and `double_pass_equity` — on
[MissedDouble](#misseddouble), [CubeDecision](#cubedecision) and [Cube ply
analysis](#cube-ply-analysis) alike — and every other equity in the document hold
**normalized cubeful equity**, in match play as well as money play. A pass reads
exactly `1.0` at every score.

What the *file* holds is a separate matter:

- **A `.gvab` of 2.0 and later (OGXM v2)** stores a match block in v2's `cubeful
  match` currency — MWC — which is the honest unit any other reader
  understands. Each equity is mapped through its own ply's score frame on the
  way out and back on the way in, so the document stays normalized. v2 holds 1e-6
  where the document holds four places; a normalized equity returns exactly
  except at the most lopsided scores, where it can move by up to 6e-4 (2-away/25-away,
  the narrowest frame). A money block is cubeful money, written as is.
- **A `.gvab` of v1** stores GammonView's normalized equity in those three fields
  where the base spec had begun to put raw MWC. A base reader's derived `*_norm_eq`
  values come out wrong on such a file (our `double_pass_equity = 1.0` renders as
  `+7.99` at 7-away/7-away); `should_double`, `correct_action`, `equity_loss` and
  PR survive, because the equity↔MWC map is affine and increasing and
  `equity_loss` is never converted. The reason for the choice was quantization:
  v1's `int16` at 1e-4 gives ~20 000 steps for normalized equity at *any* score but
  1 252 for raw MWC at 7-away/7-away and **16** at 2-away/25-away.

---

## Annotations

An annotation is a note, a key/value pair, or a drawing on the board. A list of
them sits under `annotations` on the **match**, a **game**, a **ply**, a
**decision's analysis object** (the checker analysis, a cube ply's analysis or a
resign analysis), a **`missed_double` or `cube_decision`** sub-object (the cube
decision of that ply) and an **alternative**. They map onto v2's `ANNO` scopes
0–4. The list is in v2's order (8.4.1): keyed records first by `(key, lang)`,
then prose in the order given.

```json
{ "value": "Ein Kommentar", "key": "x-note", "lang": "de", "author": "Ann",
  "at": 1790380800123, "drawings": [{"shape": 1, "at": 6, "to": 3, "color": 1}] }
```

| Key | Type | Meaning |
|---|---|---|
| `value` | string | The text; `""` for a record that only carries drawings. Always present |
| `key` | string | Present = a key/value pair, absent = prose. At most once per `(key, lang)` on one target. **Not** a v2 field name, and not in our `x-gammonview-` namespace (both are a `ValueError`); `x-…` keys are reserved for producers outside the spec |
| `lang` | string | A BCP 47 tag |
| `author` | string | Display name or opaque producer identity |
| `at` | uint64 | Unix **milliseconds** |
| `drawings` | Drawing[] | At most 256; not on the match (it has no board) |
| `kind` | uint | **Only on a decision's analysis object**, and only where it is not that object's own decision: 3 addresses the **roll** (`luck`) of a checker analysis |

A `Drawing` is `{"shape": 0 highlight | 1 arrow, "at": 0–27, "to": 0–27, "color": 0 green | 1 red | 2 blue | 3 yellow}`. A highlight has no `to`; an arrow needs a head that is not
its tail; `color` absent = the default. A shape or colour this version does not
name is kept as it is.

An annotation `ANNO` cannot address or hold (a decision `DECS` does not carry, a
value past a cap, drawings the rules forbid, a ply v2 has no record of) is not
refused: it travels in `x-gammonview-annotations` and reads back in place. One
that addresses nothing the document holds (an unknown scope or decision kind)
is kept in [`_ogxm2_passthrough`](#_ogxm2_passthrough) and goes when the games
change.

---

## Private keys

Keys that start with an underscore belong to the codecs. Treat them as opaque:
copy them when you copy the document, don't edit them, and expect `write_gvab` to
consume them.

### `_ogxm2_passthrough`

Present when a v2 file from **another producer** holds something the document
cannot model (signatures `SIGN` and `MSIG`, presence-bit tails and sections a
later spec adds, annotations at an unknown scope) or something whose original
bytes are worth keeping. Everything is base64, so it survives `.gva` JSON.
`write_gvab` writes an unedited part back as it came — each part carries a
SHA-256 *fingerprint* of our writer's canonical encoding of it, and a part that
still hashes the same has not been edited, so a signature over its bytes keeps
verifying. A file our writer reproduces byte for byte gets none. The rules, and
what a metadata or a move edit drops, are in [`OGXM_V2_PROFILE.md`](OGXM_V2_PROFILE.md) §5.
`append_analysis` and `analyze_file` carry it like any other key.

### `_base_analyses`

`int[]` — the indices (into `analyses_info`, or `[0]`) of blocks a reader
completed from the base format alone (`basefill`): another producer's block with
none of our extensions, so with no luck. `gvanalysis` uses it to decide which
blocks need the luck pass. Not written.

### `_unknown_chunks`

v1 files only. See [Reading v1 files](#reading-v1-files).

---

## Derived values are not stored

Match, game and player aggregates — **PR, total and average error, decision
counts, luck totals, illegal-move counts** — are not fields of the document. They
are computed from the per-ply `analysis` records on read
(`gvformat.compute_aggregates`; JS `compute_aggregates`), by the formulas of
[`OGXM_COMPUTED_FIELDS.md`](OGXM_COMPUTED_FIELDS.md), so every consumer derives
identical numbers. Do not add a `summary` block. The same goes for `classification`
(a reader-owned threshold policy over `equity_loss`), MWC (computed from a
shipped match equity table, below), a game's result type and a game's starting
score.

### MWC Conversion

Match-winning chance is not a stored key. It is computed on read, engine-free,
from one match-equity table (MET): Kazaross-XG2, `gvformat/met.py` (mirrored
byte-for-byte in `gvformat-js/src/met.js`). For a fixed `(score, cube)`, the map
from equity to MWC is affine, so for a ply's equity `e`:

```
mwc_win, mwc_loss = met.mwc_anchors(away1, away2, cube_value, is_crawford)
mwc(e) = (mwc_win + mwc_loss) / 2 + e * (mwc_win - mwc_loss) / 2
```

`away1`/`away2` are the mover's and the opponent's away scores at that ply (read
from the ply's own `ogid_before`), and a *difference* of two equities (a loss, a
luck) converts through half the win/loss slope alone. MWC is undefined for money
games. A ply that states a source frame ([`mwc_frame`](#mwc_frame)) converts
through it instead of the table.

---

## Reading v1 files

A `.gvab` written before 2.0.0 is OGXM v1 with a `GVAN` chunk. `read_gvab` reads
it for good into the same document; nothing downstream learns there are two
versions. What differs:

- **A v1 file knows nothing of v2's extra fields.** None of the keys named only
  in this page for v2 appear: `variant`, `score_start`, the match-context keys,
  profiles, `termination`, the cube-opening keys, `analysis_id` (and the other
  block fields), `level`, per-decision fields, `annotations`. `analysis_info`
  holds `ply`, `eval_level` (from `GVAN`'s base level), `luck_eval_level`,
  `model_id`, `timestamp` and `duration_ms`; `luck`, `decision` and
  `illegal_move` come from `GVAN`. v1 has no `mwc_frame` field.
- **Another program's v1 block** (no `GVAN`) is completed from what the base
  format carries (`basefill`): the cube values the base spec stores as raw MWC
  are normalized, `decision` is derived (a checker play counts when its
  candidates disagree, a cube when it is not trivial), `illegal_move` is set on a
  restated play, and the block is listed in [`_base_analyses`](#_base_analyses)
  since a reader cannot recover its luck.
- **`_unknown_chunks`**: every v1 chunk the reader does not decode — `SIGN`, `CLCK`,
  `VIDO` and anything a later spec adds — is carried verbatim, so a read, append
  and rewrite cannot delete it. Each entry:

  | Key | Type | Meaning |
  |---|---|---|
  | `type` | uint32 | The chunk's little-endian type code |
  | `name` | string | Its four ASCII characters, or `0xXXXXXXXX` |
  | `flags` | uint16 | The chunk header's flags |
  | `anal_index` | int | The analysis block the chunk followed, or −1 if it preceded them all |
  | `data` | string | The body, base64 |

  An unknown chunk marked critical is rejected (`GvabError`), not carried. A
  carried `SIGN` is not re-verified: gvformat holds no key material. The 2.0 writer
  converts a v1 `SIGN` to a v2 `SIGN` with the same signature, which cannot
  verify there (v1 and v2 sign different payloads) and is reported invalid by a
  verifier, which is true.
- **`CLCK` and `VIDO` are decoded** into `clock_info` / `video_info` and the plies'
  `timestamp_ms` / `video_ms` (and the rest of a mark), as the reference
  converter reads them. The chunks stay in `_unknown_chunks` for a v1 rewrite
  (`write_gvab_v1`). The clock is read at the canonical 10 ms step, as the
  reference does, so a stated `precision` is not kept; a chunk that is not valid
  is dropped.
- **`event` and `site`** are two separate fields since v0.15.0; a file written
  before then held one combined string in the event slot, which a reader splits
  (`gvformat.place.split_place`).
- **Cube equities are normalized equity** in the MWC fields where the base spec
  has MWC ([divergence note](#cube-equities-are-normalized-equity-not-mwc-gammonview-divergence)).
- **A round trip loses `preset`** (the binary has no field for it) and
  `notation` / `diff` (derived).

The v1 byte layout is frozen in
[`v1/OGXM_FORMAT_SPEC_GAMMONVIEW.md`](v1/OGXM_FORMAT_SPEC_GAMMONVIEW.md).

---

## Conventions for the producers in this repo

- `ogid_before` and `ogid_after` are populated on every ply by `gvformat.ogid`
  and re-derived by a replay on every read.
- `gvanalysis` writes `model_id = "gv-bgsage/<engine version>"`, because
  the engine name alone would overstate its share of the answer: the screening in
  `gvanalysis/checker_eval.py` chooses which moves are evaluated at full depth,
  and the preset's tiers choose that depth, so two files both reading `"bgsage"`
  can hold different numbers from the same engine. No gammonview version is
  carried, deliberately — every other field of `analysis_info` is fixed by the match
  and the engine, and that determinism is what lets a byte-exact regression corpus
  exist at all.
- A match is `White = alphabetically first player` unless the document is the
  analysis of an existing one, in which case it keeps the document's White.

## Limits

Those of v2 (spec §10): 1 000 games, 1 500 plies per game, 100 000 plies in a
file, 1 024 alternatives per decision, 64 analysis blocks, 4 096 bytes per
string, 4 096 annotations (256 KiB) and 256 drawings per annotation, a video URL
of 512 bytes. The writer raises `ValueError` past a string or annotation limit and cuts an
alternatives list at 1 024; the game and ply counts are not checked here.

---

## Example: a match with GammonView analysis

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
  "crawford": true,
  "jacoby": false,
  "beaver": false,
  "raccoon": false,
  "cube_limit": 64,
  "event": "BGS Weekly",
  "site": "Backgammon Studio",
  "analysis_info": {
    "ply": 3,
    "eval_level": "3ply",
    "luck_eval_level": "1ply",
    "model_id": "gv-bgsage/2.0.20260907",
    "met_id": "kazaross-xg2",
    "timestamp": 1710500100,
    "duration_ms": 12300,
    "analysis_id": "ea7ae02e-7c4a-8759-861b-eec0502be9b7",
    "complete": true,
    "engine_build": "2.0.20260907"
  },
  "games": [
    {
      "game_index": 0,
      "winner": 0,
      "points_won": 2,
      "is_crawford": false,
      "is_lastgame": false,
      "first_to_move": 1,
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
                "equity": 0.15,
                "eval": {"win": 0.53, "gammon_win": 0.12, "bg_win": 0.01, "gammon_loss": 0.10, "bg_loss": 0.005, "equity": 0.455},
                "is_played": false,
                "eval_level": "3ply"
              },
              {
                "move": [{"from": 8, "pips": 3}, {"from": 6, "pips": 1}],
                "equity": 0.12,
                "eval": {"win": 0.52, "gammon_win": 0.11, "bg_win": 0.01, "gammon_loss": 0.10, "bg_loss": 0.005, "equity": 0.435},
                "is_played": true,
                "eval_level": "3ply"
              }
            ]
          }
        },
        {
          "color": 1,
          "action_id": 14,
          "d1": 3,
          "d2": 6,
          "moves": [{"from": 13, "pips": 3}, {"from": 8, "pips": 6}],
          "analysis": {
            "eval": {"win": 0.65, "gammon_win": 0.10, "bg_win": 0.01, "gammon_loss": 0.05, "bg_loss": 0.002, "equity": 0.708},
            "best_equity": 0.20,
            "played_equity": 0.20,
            "equity_loss": 0.0,
            "decision": true,
            "luck": 0.01,
            "alternatives": [],
            "cube_decision": {
              "should_double": false,
              "no_double_equity": 0.25,
              "double_take_equity": 0.50,
              "double_pass_equity": 0.80,
              "action": "no_double",
              "eval": {},
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

`alternatives` and `eval` are abbreviated in the second ply; every other key is as
written.

## Example: a ply with a missed double

As a read returns it — the `missed_double` and the `cube_decision` for the same
`CUBE` record, the second without a `decision` or an `equity_loss`.

```json
{
  "color": 1,
  "action_id": 14,
  "d1": 3,
  "d2": 6,
  "moves": [{"from": 13, "pips": 3}, {"from": 8, "pips": 6}],
  "analysis": {
    "eval": {"win": 0.65, "gammon_win": 0.10, "bg_win": 0.01, "gammon_loss": 0.05, "bg_loss": 0.002, "equity": 0.708},
    "best_equity": 0.20,
    "played_equity": 0.20,
    "equity_loss": 0.0,
    "decision": true,
    "luck": 0.01,
    "alternatives": [],
    "missed_double": {
      "no_double_equity": 0.30,
      "double_take_equity": 0.55,
      "double_pass_equity": 1.00,
      "equity_loss": 0.08,
      "correct_action": "double",
      "eval": {"win": 0.62, "gammon_win": 0.21, "bg_win": 0.02, "gammon_loss": 0.08, "bg_loss": 0.01, "equity": 0.30},
      "eval_level": "2ply"
    },
    "cube_decision": {
      "should_double": true,
      "no_double_equity": 0.30,
      "double_take_equity": 0.55,
      "double_pass_equity": 1.00,
      "action": "double_pass",
      "eval": {"win": 0.62, "gammon_win": 0.21, "bg_win": 0.02, "gammon_loss": 0.08, "bg_loss": 0.01, "equity": 0.30},
      "eval_level": "2ply"
    }
  }
}
```

## Example: a take, in the doubler's frame

White doubled; Black takes. The three equities are White's, so a take that is
correct at this position reads with `double_take_equity` at or below
`double_pass_equity`.

```json
{
  "color": 0,
  "action_id": 22,
  "analysis": {
    "correct_action": "take",
    "played_action": "take",
    "no_double_equity": 0.62,
    "double_take_equity": 0.84,
    "double_pass_equity": 1.00,
    "eval": {"win": 0.71, "gammon_win": 0.22, "bg_win": 0.02, "gammon_loss": 0.09, "bg_loss": 0.01, "equity": 0.85},
    "equity_loss": 0.0,
    "decision": true,
    "eval_level": "3ply"
  }
}
```

## Example: the clock, a video mark and an annotation

```json
{
  "clock_info": { "reserve_ms": 120000, "delay_ms": 12000, "increment_ms": 5000,
                  "start_timestamp": 1710500000, "flags": 3 },
  "video_info": { "kind": 2, "is_live": true, "offset_ms": -250,
                  "url": "https://twitch.tv/videos/123" },
  "games": [{
    "plies": [
      { "color": 1, "action_id": 2, "d1": 1, "d2": 3, "moves": [],
        "timestamp_ms": 0, "video_ms": 5000, "wall_ms": 1710500005000,
        "behind_live_ms": 3000, "video_hand_anchored": true,
        "annotations": [{ "value": "Standard", "author": "Ann" }] },
      { "color": 0, "action_id": 14, "d1": 3, "d2": 6, "moves": [],
        "timestamp_ms": 7310 }
    ]
  }]
}
```

(`moves` abbreviated; only the keys of the section are shown.)
