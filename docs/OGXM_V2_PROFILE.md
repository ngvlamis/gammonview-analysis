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
| `match_length` | `MTCH.match_length`; `variant` is always 0 (backgammon) |
| `player_white`, `player_black` | `white_name`, `black_name` (omitted when empty) |
| `crawford`, `jacoby`, `beaver`, `raccoon` | `rules` bits 0-3 |
| `cube_limit` | `cube_limit`, when a power of two; anything else is a source's "no limit" and is not written |
| `white_score`, `black_score` | not stored: v2 derives the score from the games (M8). A stated score the games do not add up to goes in `x-gammonview-score` |
| `result` | stored only when the score does not decide it |
| `source` | `source` (same enum), omitted when 0 |
| `timestamp` (seconds) | `started_at` (milliseconds), omitted when 0 |
| `event` | `event` when at most 120 bytes; longer, `x-gammonview-event` |
| `site` | `x-gammonview-site`, always. v2's `site` is the platform's host name, and ours is whatever the source called it |
| `games[].winner`, `points_won`, `is_lastgame` | `GAME` bits 0-2; `points_won` is written exactly when `winner` is |
| `games[].first_to_move`, `is_crawford`, `game_index` | derived, not stored |

**Plies.** A dice ply is v2's dice action, with its steps stored in an order
that replays legally (M3): a bar entry before any other move, a bear-off after
the last checker comes home. The play is the same (M5 compares positions); only
its spelling can change. A cube action, a resignation and a game-end marker map
one to one. A resignation's `resign_value` is `points_won / cube`, or 1.

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
| `analysis_info.model_id`, `timestamp`, `duration_ms` | `model_id`, `started_at`, `duration_ms` |
| `analysis_info.ply` | the block level's `checker_ply` |
| `analysis_info.eval_level` | see Levels |
| (currency) | `cubeful match` (MWC) in a match, `cubeful money` in a money session |

**Units.** A match block is written in MWC. Each equity is mapped through its
own ply's score frame — the mover's MWC on a win and a loss of the cube in play,
from the one MET we ship — and a difference (a loss, a luck) through the
frame's span alone. The reader maps back the same way. v2 stores 1e-6, so a
normalized equity returns exactly except at the most lopsided scores, where it
can move by up to 6e-4 (2-away/25-away, the narrowest frame).

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

**Cube decisions.** A live cube on a dice ply is a `CUBE` record on that ply
(`verdict` no double / double); a missed double is the same with its
`equity_loss`. A cube action ply carries its own, the verdict being the correct
action, and is moved to the annotation if that action is not one v2 allows
there. **Resignations** are `RESIGN` records. **Luck** is a `ROLL` record on
every dice ply that has it.

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
| `x-gammonview-analysis/<analysis_id>` | match | `;`-separated items: `level=<label>` (the block's own level, when it is not the block preset; empty for none), `luck=<label>` (when the block has luck), `pr=<tokens>` (always present; it marks the block as ours) |
| `x-gammonview-decisions/<analysis_id>` | ply | base64 of `DECS` records for this ply, in kind order. They stand in for `DECS`'s records of the same kind at that ply |
| `x-gammonview-illegal-ply` | ply | the steps of the illegal play, `from/pips` joined by `,` (empty for a dance) |
| `x-gammonview-site` | match | `site` |
| `x-gammonview-event` | match | an `event` longer than 120 bytes |
| `x-gammonview-score` | match | `white,black`, a stated score the games do not add up to |

Labels in `level=` and `luck=` are percent-encoded except for letters, digits
and ` +-_./()`.

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

Each is a rule in `tests/test_ogxm2_writer.py`, which fails on any difference
none of them explains.

## 5. What is not carried yet

A v2 file from another producer is read into our document, which does not hold
everything v2 can say: its `CLCK`, `VIDO`, `SIGN`, `MSIG` and other producers'
`ANNO` records, and `MTCH` fields beyond those in §1 (the event's stage and
round, the players' profiles, ...). Rewriting such a file — appending our
analysis to it — drops them. Likewise a v1 file's `SIGN`, `CLCK` and `VIDO`
chunks, which `write_gvab_v1` carries through, are not carried into v2.
