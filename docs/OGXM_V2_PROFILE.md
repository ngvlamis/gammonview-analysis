# OGXM v2 — the GammonView profile

Since 1.6.0 `write_gvab` writes **OGXM v2**, HedgeHog's current match format,
specified in `docs/OGXM_FORMAT_SPEC.md` of the
[HedgeHog repository](https://gitlab.com/eranlambooij/hedgehog-public). Every
file we write is plain v2: any conforming reader loads it, replays every game,
and finds every decision v2 has a field for. This document says how our
document maps onto v2, what travels outside v2's fields, and where reading a
file back gives something other than what was written.

`read_gvab` reads v1 and v2 alike. v1 stays readable for good — files and share
links written before 1.6.0 are v1 — and `write_gvab_v1` keeps the old writer for
tests and comparison. The GVAN chunk and the rest of
`OGXM_FORMAT_SPEC_GAMMONVIEW.md` describe that v1 format only.

The Python implementation is `gvformat/ogxm2_writer.py` and `gvformat/ogxm2.py`;
`gvformat-js/src/` mirrors both. `tests/test_ogxm2_writer.py` checks the rules
below over the whole sample corpus and against the reference codec.

---

## 1. The match

| Our document | v2 |
|---|---|
| `match_length`, `variant` | `match_length`, `variant` (9.1; absent = 0, backgammon) |
| `player_white`, `player_black` | `white_name`, `black_name` (omitted when empty) |
| `crawford`, `jacoby`, `beaver`, `raccoon`, `auto_doubles` | `rules` bits 0-4; bits the spec has not named are `rules_other` |
| `cube_limit` | `cube_limit`, when a power of two; anything else is a source's "no limit" and is not written |
| `score_start` (`[white, black]`) | `score_start`: the score before the first recorded game. It feeds the score walk, Crawford detection and every OGID |
| `white_score`, `black_score` | not stored: v2 derives the score from the games (M8). A stated score the games do not add up to goes in `x-gammonview-score` |
| `result` | stored only when the score does not decide it |
| `source` | `source` (same enum), omitted when 0 |
| `timestamp` (seconds) | `started_at` (milliseconds), omitted when 0 |
| `completed_at` (ms), `date_precision`, `player_seat`, `crawford_before_start`, `rated` | the same fields |
| `event`, `event_year` | `event` (at most 120 bytes) and `event_year`, separately: a reader no longer appends the year to the event |
| `stage`, `round`, `table`, `city`, `country`, `event_url`, `match_ref` | the same fields |
| `platform` | v2's `site`: the platform's host name. Renamed because our `site` predates it |
| `white_profile`, `black_profile` | the `player` records (4.1): `user_id`, `rating` (a float, to 0.01), `rating_system`, `country`, `kind` |
| `site` | `x-gammonview-site`, unless it is what the reader would derive anyway: `city`, else `platform`. Ours is whatever the source called the place, sometimes a city and sometimes a platform, and nothing tells which |
| `games[].winner`, `points_won`, `is_lastgame` | `GAME` bits 0-2; `points_won` is written exactly when `winner` is |
| `games[].termination`, `initial_cube_value`, `initial_cube_owner`, `auto_doubles` | the same `GAME` fields. The game's cube starts at `initial_cube_value · 2^auto_doubles`, owned by `initial_cube_owner` (5.3), in the OGIDs and in `gvanalysis` |
| `games[].first_to_move`, `is_crawford`, `game_index` | derived, not stored |

Enums and seats are v2's integers. A key is absent when the file has no value,
and a value equal to v2's default is not written (*default-omitted*, §4).

**A value v2 cannot hold** — past a length cap, outside a field's character
set, a `user_id` or `match_ref` with no `platform`, an `event_year` with no
`event` — goes in an `x-gammonview-<field>` record at its scope
(`x-gammonview-white_profile.<field>` for a profile) and comes back on read, so
nothing is dropped and no file we write breaks a rule. A document that breaks a
rule no annotation can stand in for (an annotation keyed with a v2 field name,
a clock that runs backwards) is refused.

**Nothing valid is refused on read.** A mid-match start, a pre-turned cube,
auto-doubles and the variants all load, with OGIDs from the variant's own
opening position (a hypergammon OGID states its checker count, `:3`, which the
reference's leaves out). `gvanalysis` refuses a variant, since the engine plays
backgammon only.

**Plies.** A dice ply is v2's dice action, with its steps stored in an order
that replays legally (M3): a bar entry before any other move, a bear-off after
the last checker comes home. The play is the same (M5 compares positions); only
its spelling can change. A cube action, a resignation and a game-end marker map
one to one, and so do a beaver, a raccoon, a settlement (`settle_value`), a
cube set (`cube_value`, `cube_owner`) and a pass. A resignation's `resign_value`
is kept only where it differs from `points_won / cube`. An action id the spec
has not assigned is kept whole, as `extras_raw`.

A game that starts from a set-up board (a leading set-position ply with no
dice) stores it as `GAME.initial_board` — unless the board is the opening
position, which v2 never writes as an initial board, and then it stays a ply.

**Illegal plays.** A play that is not legal (M4) for its roll — or that any
analysis block flags `illegal_move` — is written the way v2 records one: a
`set position` ply carrying the dice, the `illegal` flag and the board the play
produced, seated on the side to move next (M2). Our document may hold it as a
dice ply with steps; then the steps go in `x-gammonview-illegal-ply` and come
back on read. A ply our document already holds as a set position with dice is
written as one, flagged `illegal`.

## 2. Analysis

One `ANAL` + `DECS` per analysis block, primary first.

| Our document | v2 |
|---|---|
| `analysis_info.analysis_id` | `analysis_id`. A block without one gets a deterministic id: four CRC32s over the match and the block's identity, stamped as a UUIDv8 |
| `analysis_info.model_id`, `model_name` | `model_id`, `model_name`, each exactly as stated: HedgeHog's `model_id` is a UUID, ours `gv-bgsage/<version>`. Show `model_name ?? model_id` |
| `analysis_info.timestamp`, `duration_ms`, `completed_at` (ms) | `started_at`, `duration_ms`, `completed_at` |
| `analysis_info.producer`, `complete`, `model_digest` (hex), `engine_build`, `cube_efficiency`, `tables`, `dials`, `sources` | the same fields. Ours states `engine_build` (the bgsage version) and `complete` |
| `analysis_info.coverage` | `coverage`, as `[game_index, ply_index]` pairs in the document's own plies |
| `analysis_info.currency` | `currency`, when it is not the match's default |
| `analysis_info.ply` | the block level's `checker_ply` |
| `analysis_info.eval_level` | see Levels |
| `analysis_info.met_id` | `met_id`. A match block without one is written as `kazaross-xg2`, the table we convert with |
| (no `currency` key) | `cubeful match` (MWC) in a match, `cubeful money` in a money session |
| `level` (block, decision, alternative) | the full level record (6.4) — preset, depths, the rollout's trials, truncation, seed (a decimal string), ... — kept only where the labels below do not already give it |

**Units.** A match block is written in MWC. Each equity is mapped through its
own ply's score frame — the mover's MWC on a win and a loss of the cube in play,
from the one MET we ship — and a difference (a loss, a luck) through the
frame's span alone. The reader maps back the same way. v2 stores 1e-6, so a
normalized equity returns exactly except at the most lopsided scores, where it
can move by up to 6e-4 (2-away/25-away, the narrowest frame).

**A source's own table.** BGBlitz normalizes with a match equity table that is
not ours (7-away/7-away on a 1-cube: half-width 0.05954 against our 0.0626), so
converting its equities through ours would store MWCs BGBlitz never computed.
A `.bgf` carries both numbers for every decision — the MWC and its normalized
form, an exact linear map of each other — so the converter measures BGBlitz's
frame per score and cube and puts it on each analysed ply as
`analysis.mwc_frame = [mid, half]` (the ply's own perspective: the MWC at
normalized 0, and the MWC per unit of equity). A ply with a frame is converted
through it instead of our table, the block's `met_id` is `bgblitz`, and the
frames travel in the block annotation's `frame=` item. The file then holds
BGBlitz's own MWCs, to within the document's 4-place equities (2.5e-5 at
worst over the samples), and our reader gives back BGBlitz's normalized
equities exactly. XG needs nothing of this: it stores normalized equities only,
and its default table is ours.

**Levels.** v2 states a level at three tiers — block, decision, alternative —
each overriding only what differs (L1). Our labels (`2ply`, `truncated2`,
`rollout`, `database`, ...) are v2's `preset`, which is a free label (L3). The
block's preset is the label most of its decisions share, which is what v2
means by a block level; the block's own `eval_level`, when that is different,
travels in its annotation. A decision's preset is its best move's label, an
alternative overrides it only where its own differs, and a decision's or cube's
`ply` is `checker_ply` / `cube_ply`. A luck record states its depth as
`checker_ply`; the luck label travels once, in the block's annotation.

**Checker decisions** carry the alternatives in their order, each with its
steps, equity, probabilities when recorded, and `is_played`. v2 then derives
the best equity (the first alternative's) and, when the played move is listed,
the equity loss (best minus played). Where a decision breaks a v2 invariant it
is not dropped, it moves: the exact record goes in the ply's
`x-gammonview-decisions` annotation, and `DECS` carries a conforming version
when there is one.

| Case | `DECS` | Annotation |
|---|---|---|
| XG lists bearoff-database plays among ply-evaluated ones (A1) | the list regrouped by level | the list as XG ordered it |
| the played move appears twice — a notation split two ways (A3/A4) | the copy dropped | the list as stored |
| the played move was judged at another level than the best (A5) | — | the decision |
| the loss differs from best minus played by more than rounding | — | the decision |
| more than one alternative is flagged played | — | the decision |
| an unplayed roll before a resignation (no checker decision allowed) | — | the decision |

**Decision fields.** A checker decision also carries `alternatives_total`,
`rollouts_done`, `deep_searched`, `position_tags`, `producer_ref` and
`source_band`; an alternative `rollout_se` and `cubeless_equity`; a cube
decision `take_point`, `window_searched`, `is_optional`, `is_free_cube`,
`cubeful_take_value`, `currency` and `producer_ref`; a resignation
`correct_value`; a luck record `producer_ref` and `level`, as `luck_producer_ref`
and `luck_level` since it shares its analysis object with the checker play.
Equities among them are normalized like every other. Each is written only where
v2 does not derive it, and a decision whose fields `DECS` cannot hold as they
are moves to `x-gammonview-decisions` whole.

**Cube decisions.** A live cube on a dice ply is a `CUBE` record on that ply
(`verdict` no double / double); a missed double is the same with its
`equity_loss`. A cube action ply carries its own, the verdict being the correct
action, and is moved to the annotation if that action is not one v2 allows
there. **Resignations** are `RESIGN` records. **Luck** is a `ROLL` record on
every dice ply that has it. A beaver is analyzed as the take it answers, at the
cube before the double; the raccoon after it carries no decision.

**Clock, video and annotations.** `CLCK` is `clock` (its settings) with
`clock_ms` on each ply; `VIDO` is `video` with `video_ms`, `wall_ms`,
`behind_live_ms` and `video_hand_anchored` on each marked ply (the spec's
Appendix A.3 names); `ANNO` records other than ours are `annotations` lists —
`{value, key?, lang?, author?, at?, drawings?}` — on the match, a game, a ply,
a decision's analysis object and an alternative. A decision's records sit on
the ply's natural decision (the checker play on a dice ply, `missed_double` or
`cube_decision` for a cube record there, the action's own on a cube or resign
ply), and a roll's carry `"kind": 3`. An annotation `ANNO` cannot address or
hold goes in `x-gammonview-annotations`.

**On an illegal play** v2 allows no decision at all — the ply is a set position.
Every record the block holds for it (checker, cube, luck) goes in the
annotation, and the played play's own evaluation with them.

## 3. The annotations

`ANNO` records with keys beginning `x-`, the namespace v2 reserves for producers
outside the spec (N6): a conforming reader keeps them and interprets none.
Every value starts with a format version, `1:`. A value longer than a string
holds is split over `key`, `key~1`, `key~2`, ... and joined in that order.

| Key | Scope | Value after `1:` |
|---|---|---|
| `x-gammonview-analysis/<analysis_id>` | match | `;`-separated items: `level=<label>` (the block's own level, when it is not the block preset; empty for none), `luck=<label>` (when the block has luck), `pr=<tokens>` (always present; it marks the block as ours), `frame=<entries>` (when plies carry a source frame) |
| `x-gammonview-decisions/<analysis_id>` | ply | base64 of `DECS` records for this ply, in kind order. They stand in for `DECS`'s records of the same kind at that ply |
| `x-gammonview-illegal-ply` | ply | the steps of the illegal play, `from/pips` joined by `,` (empty for a dance) |
| `x-gammonview-site` | match | `site` |
| `x-gammonview-event` | match | an `event` longer than 120 bytes |
| `x-gammonview-score` | match | `white,black`, a stated score the games do not add up to |
| `x-gammonview-<field>` | match or game | a value of that document field v2 cannot hold (§1); `x-gammonview-white_profile.<field>` / `black_profile.<field>` for a profile's |
| `x-gammonview-annotations` | match | base64 of a JSON list of the document's `annotations` that `ANNO` cannot address or hold, each `{s: scope, g: game, p: ply, a: analysis_id, k: kind, i: alternative, v: the record}` in the document's own coordinates: a decision `DECS` does not carry, a value past a cap, drawings the rules forbid, a ply v2 has no record of |
| `x-gammonview-video.url` | match | a video `url` v2 would drop (not `https`, or over 512 bytes) |

Labels in `level=` and `luck=` are percent-encoded except for letters, digits
and ` +-_./()`.

**Frames.** `frame=` is `,`-separated entries `<ply_ref>:<mid>:<half>`, where
`mid` is from White's perspective (so it holds still while the score and cube
do), each number to 8 decimals with trailing zeros dropped. An entry holds for
its ply and every later ply of the block until the next one; an entry with
nothing after the `ply_ref` (`<ply_ref>:`) means our table from there on.

**PR counting.** Whether a decision counts toward PR has no v2 field. On read,
a block of ours derives it the way `basefill` derives it for a foreign block —
a checker play with at least two alternatives that differ, a cube that is not
trivial, every resignation, never an illegal play — and `pr=` lists the
decisions where the document says otherwise, each as its `ply_ref` and a letter:
`c` the decision's own flag, `l` a live cube's, `i` `illegal_move`.

## 4. Reading it back

`read_gvab(write_gvab(D))` gives `D` back, except:

| Rule | What changes |
|---|---|
| analysis-id | each block gains `analysis_id` |
| derived-loss | where the played move is listed, `equity_loss` is best minus played. A document that went through v1 (4-place values) can move by 1e-4 |
| step-order | a play's steps may come back reordered (§1) |
| zero-probs | probabilities recorded as all zero are kept; v1 read them as "not recorded" |
| unclamped | equities beyond ±3 are kept; v1 clamped them |
| level-filled | a decision with no level of its own reads the block's |
| no-limit | a `cube_limit` that is not a power of two reads as no limit |
| no-first-move | a game with no play has no first mover to derive |
| luck-level | a block with no luck has no luck level |
| met-id | a match block names its table: `kazaross-xg2` when the document named none |
| mwc-frame | a source's frame (`mwc_frame`, §2) is carried by v2 only; v1 loses it |
| default-omitted | a value equal to v2's default is not written, so it reads back absent: `variant` 0, a 0-0 `score_start`, `initial_cube_value` 1, `initial_cube_owner` 2 (centred), `auto_doubles` 0 or false, `rules_other` 0, a false flag, an empty string |
| site-follows-place | a document with a `city` (else a `platform`) and no `site` reads back with that place as its `site` |
| resign-derived | a `resign_value` equal to what the game's points and the cube give is not kept; only a different one is |
| settle-millionths | a `settle_value` is held to a millionth of a point |
| block-provenance | what v2's block record states (producer, completeness, coverage, model name, identifier and digest, engine build, currency, cube efficiency, tables, dials, completion time, sources, a level beyond the labels) is in the v2 read-back only; v1 has no field for it |
| block-currency | a block's `currency` equal to the match's default (match winning chances in a match, cubeful money in a money game) is not a key; match winning chances stated for a money game are written, and read back, as cubeful money |
| cube-currency | a cube decision's `currency` equal to its block's, or match winning chances in a money game, is not written and reads back absent |
| coverage | `coverage` reads back ascending and without repeats, naming only plies that exist, plus every ply the block holds a decision for (6.5 makes a decision outside its coverage an error); a list naming no ply is no key |
| level-resolved | a `level` reads back resolved against the tier above, every field it inherits stated; a tier cannot clear a field the tier above states except its rollout (J9), so a field it leaves out is inherited |
| level-depth | a `level` that is what the labels (`eval_level`, `ply`) give, but for a preset that only names the depth (`3ply`), is not kept |
| foreign-labels | a block read from another producer and rewritten (so no longer its source's bytes) reads back with its presets as its labels, where the first read labelled it by depth |
| seed-string | a rollout's `seed` reads back as a decimal string, given as one or as a number |
| dials-known | `dials` names v2 does not define, and a false flag, are not kept |
| annotation-order | a target's `annotations` read back in v2's order (8.4.1): keyed records first by `(key, lang)`, then prose in the order given; one carried in `x-gammonview-annotations` reads back after the target's others |
| clock-first-reading | a clock reads back with a reading of 0 on the first ply (`t[0]`, which v2 does not store), and readings are held to the clock's step: 10 ms, or the `precision` the document states |
| clock-dropped | a clock whose readings have a gap (a reading after a ply without one), do not start at 0, or run backwards is not written (8.2); its readings go with it, the video and annotations stay |
| video-unaddressed | a mark on a ply v2 has no record of (a game's set-up position, a game past the 256th, a ply past 65535) is not written; `behind_live_ms` reads back in whole seconds, at most 65534000 |
| probability-places | `cube_efficiency` and `take_point` are held to a ten-thousandth, `rollout_se`, `cubeless_equity` and `cubeful_take_value` to the document's four places; an empty string, list or object is no key |

Each is a rule in `tests/test_ogxm2_writer.py`, which fails on any difference
none of them explains.

## 5. Another producer's file: what is kept (spec I7)

Our document holds every field and section v2 defines (§1-§3). What it cannot
hold is what it cannot understand: `SIGN` and `MSIG`, presence-bit tails a later
spec adds, decisions of unknown kind, annotations at an unknown scope, and
unknown ancillary sections. Rewriting another producer's file keeps all of it,
and keeps the bytes of what the document did not change.

**How.** `read_ogxm2` attaches `_ogxm2_passthrough` to the document: the source
header's minor versions, the original `MTCH`, `GAME`, `ANAL`, `DECS` and `SIGN`
payloads, every `MSIG`, `CLCK` and `VIDO` and each `ANNO` record the document
holds as a fingerprint with the source's bytes, each `ANNO` record that
addresses nothing the document holds, and each unknown section with the known
section it followed. Bytes are base64, so the record survives `.gva`
JSON, and `append_analysis` and `analyze_file` carry it like any other key. A
file the writer reproduces exactly gets none, so our own files are unaffected.

**Fingerprints.** Each stored part carries the SHA-256 of *our writer's
canonical encoding of that part*, taken from the document when it was read. The
writer encodes the document again; a part whose canonical bytes still hash to
the stored value has not been edited, and the original bytes are written instead.
This matters because `SIGN` and `MSIG` digest the bytes as stored (8.1.1, 8.6.1):
re-encoding an unedited part can differ from the source in bytes alone, and a
signature over it would stop verifying for no reason. Comparing fingerprints
rather than keeping an "edited" flag means no code path has to remember to
clear one.

**Three cases**, by what the document changed:

| Edit | Kept verbatim | Dropped | Why |
|---|---|---|---|
| none, or blocks added or removed | everything, including `SIGN` and `MSIG` | removed blocks (and annotations addressed to them) | neither signature covers analysis blocks beyond their own |
| `MTCH` (a name, the event, ...) | games, foreign blocks (`match_digest` recomputed), `CLCK`, `VIDO`, annotations, unknown sections; `MTCH`'s unknown tail | `MSIG`, `SIGN` | both digest `MTCH` |
| a `GAME` (a move) | unchanged games, unknown sections | `SIGN`, `MSIG`, foreign blocks' bytes (re-encoded from the document, unsigned) | signatures cover the moves; the analysis was made over the old ones |

The clock, the video and every annotation are document keys (`clock` and
`clock_ms`, `video` and `video_ms`, `annotations`), so they move with their plies
and are written from the document in every case: a move edit renumbers them
rather than dropping them. The source's bytes stand for each (the fingerprint
rule above, per record for annotations) while the document still encodes to what
they decoded to, and an edit re-encodes only what it touched. Two things drop a
section: a clock the readings of which no longer form a series (8.2; see
*clock-dropped*), and an `MSIG` that covers a clock whose bytes changed. An
annotation on a decision of another producer's block is written as `ANNO` while
that block's `DECS` holds the decision (and the alternative); otherwise it is
carried in `x-gammonview-annotations`. Only annotations that address nothing the
document holds (an unknown scope, or a decision kind) are kept as bytes beside
it, and those go when the games change.

An edited analysis block is re-encoded and loses its `SIGN`; its siblings keep
theirs.

**Details.** The header's minors are never lower than the source's. `ANNO` is
one section holding the foreign records and ours, merged in the order 8.4.1
requires. A foreign block written verbatim gets no `x-gammonview-analysis`
annotation, so it reads back as foreign, as it first did; `site` is not
re-annotated when the source's `MTCH` already states it. Every block now states
its `analysis_id` in the document, not only ours, since a signature and an
annotation name the block by it. An unknown *critical* section is still a hard
error.

**A v1 file's chunks** (`SIGN`, `CLCK`, `VIDO`, carried as `_unknown_chunks`) are
written as v2 the way the reference's `v1_to_v2` converts them, byte for byte
(tested against it). `CLCK` and `VIDO` are decoded into `clock` and `video` (the chunks stay in
`_unknown_chunks` for a v1 rewrite) and re-encoded canonically (8.2, 8.3), and
dropped if invalid. `SIGN` becomes a v2 `SIGN` with the same
signature; it cannot verify (v1 and v2 sign different payloads), so a verifier
reports it invalid, which is true. Other unknown v1 chunks are dropped.

**Not covered.** A document whose passthrough record cannot be parsed is written
as if it had none. A field a later spec adds cannot be edited, only carried.
