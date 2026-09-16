# Computed fields

Quantities a reader is expected to **derive** rather than read. They are not
stored anywhere in OGXM — not in the JSON, not in the binary — so every consumer
computes them from the ply records, and two consumers that disagree about them
disagree about the numbers a user sees.

`gvformat.compute_aggregates` (and its JS mirror `computeAggregates`) is the
reference implementation of everything below; this document is the specification
it implements.

---

## Conventions

- **White = Player 1, Black = Player 2** (mat-file convention; no color info in the source format)
- A ply belongs to white if `color == 1`, black if `color == 0`
- `analysis` is present on a ply only when the position was evaluated

---

## MWC Conversion

MWC (match-winning chance) is **not stored anywhere** — no per-game anchor
(the old `met_value`), no per-decision anchor (a later, now also removed,
`mwc_on_win`/`mwc_on_loss` pair). It is computed on read from a single shipped
match-equity table (MET): **Kazaross-XG2**, shipped in `gvformat/met.py`
(mirrored byte-for-byte in `gvformat-js/src/met.js`), validated to match
bgsage's own `eq2mwc` exactly. For a fixed `(score, cube)`, `eq2mwc` is affine
in equity, so any equity `e` on a decision converts with no per-decision
anchor and no bgsage call — just the mover's away scores, the ply's cube
value, and whether it's the Crawford game:

```
mwc_win, mwc_loss = met.mwc_anchors(away1, away2, cube_value, is_crawford)
mwc(e) = (mwc_win + mwc_loss) / 2 + e * (mwc_win − mwc_loss) / 2
```

`away1` is the mover's away score, `away2` the opponent's — both decodable
from the ply's own `ogid_before` (fields 7–8 are white/black scores, field 9
is match length; see "Score at Game Start" below for the field layout).
`mwc(e)` is in the mover's frame; White's MWC on a black-to-move ply is
`1 − mwc(e)`. MWC is undefined for money games (no score/cube frame to anchor
it to) — same as the old GVA output, which omitted all MWC fields there.

For a `missed_double` / `cube_decision` sub-object, use the **parent checker
ply's** `(away1, away2, cube_value, is_crawford)` (same score/cube frame,
recovered the same way from the parent's `ogid_before`).

---

## Luck

`luck` (a single field, `postroll − preroll`) is stored on the checker ply's
`analysis`, computed at the luck-analyzer's level (`analysis_info.luck_eval_level`,
1-ply today) — NOT at `analysis_info.eval_level`. Because it's always at that
same fixed level, `luck` reproduces exactly (preset-independent), unlike a
formula that mixed a full-eval-level `best_equity` with a 1-ply preroll. The
two equities behind it (`preroll`/`postroll`) are not themselves stored.

| GVA field | How to compute |
|---|---|
| `moves[].luck` | Read directly: `analysis.luck` (stored) |
| `moves[].luck_mwc` | `luck * (mwc_win − mwc_loss) / 2`, using this ply's `(away1, away2, cube_value, is_crawford)` via [MWC Conversion](#mwc-conversion) — engine-free (a *change* in equity converts to a change in MWC via half the win/loss slope, since `eq2mwc` is affine). Implemented in `gvformat/stats.py` (`_eq_delta_to_mwc`), whose `total_luck_mwc` reproduces the analyzer's engine-computed totals to 1/10000 quantization. |
| `summary.player1_total_luck` | Sum `analysis.luck` over checker plies (`action_id` 0–20) where `color==1` and `luck` is present |
| `summary.player2_total_luck` | Same for `color==0` |
| `summary.player1_total_luck_mwc` | Sum `luck_mwc` over same plies (money games: no anchors, so this total is absent) |
| `summary.player1_luck_rolls` | Count checker plies where `color==1` and `luck` is present |
| *(game-scoped equivalents)* | Same formulas scoped to plies within each game |

---

## Lost MWC and Cube MWC

Every MWC (match winning chance) field is **computed from the stored equities**, never stored directly. Each conversion applies [MWC Conversion](#mwc-conversion) to a stored equity, using that decision's own `(away1, away2, cube_value, is_crawford)` (from its `ogid_before`). **All MWC fields are absent for money games** (no score/cube frame to anchor MWC to), matching GVA output.

**Checker plies:**

| GVA field | How to compute |
|---|---|
| `moves[].lost_mwc` | `mwc(best_equity) − mwc(played_equity)`; equivalently derived from `equity_loss` via the same MWC conversion |

**Cube decision plies** (doubler's action — read equities from the ply's CUBE entry):

| GVA field | How to compute |
|---|---|
| `cube_decision.mwc_no_double` | `mwc(no_double_equity)` |
| `cube_decision.mwc_double_take` | `mwc(double_take_equity)` |
| `cube_decision.mwc_double_pass` | `mwc(double_pass_equity)` |
| `cube_decision.lost_mwc` | MWC form of `equity_loss` (optimal-action MWC − played-action MWC) |

**Cube response plies** (responder's take/pass — CUBE type=1 entry):

| GVA field | How to compute |
|---|---|
| `cube_response.mwc_take` | `mwc(equity_take)` |
| `cube_response.mwc_pass` | `mwc(equity_pass)` |
| `cube_response.lost_mwc` | MWC form of `equity_loss` |

---

## Performance Rating (PR)

PR = `(sum of equity losses) / (decision count) × 500`

A ply's checker-move `analysis` (and each standalone cube ply's `analysis`) carries a `decision` bool flag — this is the OGXM JSON field name; the internal/pre-export GVA shape calls the same flag `counted` (renamed on export, see `gvformat/export.py`). Read `decision`, not `counted`, from real OGXM JSON.

| GVA field | How to compute |
|---|---|
| `summary.player1_pr` | Sum `analysis.equity_loss` over all plies where `color==1` and the ply counts as a decision (see "What counts as a decision" below), divide by count, multiply by 500 |
| `summary.player2_pr` | Same for `color==0` |
| `summary.player1_total_decisions` | Count plies where `color==1` and the ply counts as a decision |
| `summary.player2_total_decisions` | Same for `color==0` |
| `summary.player1_cube_decisions` | Count cube decisions (see below) where `color==1` and the ply counts as a decision |
| `summary.player2_cube_decisions` | Same for `color==0` |
| `summary.player1_total_error` | Sum `analysis.equity_loss` over plies where `color==1` and the ply counts as a decision |
| `summary.player2_total_error` | Same for `color==0` |
| `summary.player1_total_error_mwc` | MWC analog of `total_error`: sum each counted decision's `equity_loss` converted via [MWC Conversion](#mwc-conversion) (that decision's own anchors — the same slope as `luck_mwc`). Absent for money games. Includes embedded `cube_decision` / `missed_double` errors, so it (like `total_error`) covers **all** counted decisions — do not recompute it by summing only checker + standalone-cube rows. |
| `summary.player2_total_error_mwc` | Same for `color==0` |
| `games[i].player1_pr` | Same formulas scoped to plies within `games[i]` |
| *(and all player2/game equivalents)* | |

### What counts as a decision

- **Checker-move ply** (`action_id` 0–20): counts iff `analysis.decision == true`.
- **Cube decision**: a *standalone* cube ply (`action_id` 21 = double, 22 = take, 23 = pass) counts iff `analysis.decision == true`. This is not the only source of cube decisions — a player who *holds* correctly (doesn't double, and not doubling was optimal) never gets a standalone action_id-21 ply; instead the resulting cube analysis is embedded as `cube_decision` on the checker ply that follows (mutually exclusive with `missed_double` — see the Checker Analysis section of `OGXM_JSON_SPEC_GAMMONVIEW.md`). Cube decisions are the union of:
  1. Standalone `action_id` 21/22/23 plies where `analysis.decision == true`.
  2. Embedded `cube_decision` sub-objects (on a checker ply's `analysis`) where `cube_decision.decision == true`.
  3. Embedded `missed_double` sub-objects — these carry **no** `decision` flag of their own (per the MissedDouble object), so whether one counts must be recomputed from its three stored equities (`no_double_equity`, `double_take_equity`, `double_pass_equity`) using the same triviality rule the analyzer applies to the doubler: trivial iff `abs(nd - min(dt, dp)) < 0.001 or (nd - dt) > 0.200 or (nd - dp) > 0.200 or (nd < -0.900 and dt < -0.900)`; the missed double counts iff **not** (trivial and `min(dt, dp) - nd < 0.001`).

  This matches what `gvformat/stats.py` (`_accumulate_ply` / `_missed_double_counts`) implements — treat that module as the reference implementation if in doubt.

---

## Classification

`classification` (the `"inaccuracy"` / `"error"` / `"blunder"` / none bucket for a decision) is **not stored** in `.ogxm.json`. It is a threshold policy over the raw `equity_loss` (which *is* stored on every analysis and sub-object), and the **reader owns that policy** — a consumer may bucket the same `equity_loss` however it likes. This mirrors the PR/luck aggregates, which are likewise derived on read rather than persisted.

The **default** GammonView policy (buckets over `equity_loss`, in equity):

| Classification | Equity loss |
|---|---|
| none / `null` | `< 0.02` |
| `"inaccuracy"` | `>= 0.02` |
| `"error"` | `>= 0.04` |
| `"blunder"` | `>= 0.08` |

Apply the same buckets to any `equity_loss`: checker `analysis`, standalone cube/resign analyses, and the embedded `missed_double` / `cube_decision` sub-objects. A reader that prefers different thresholds (or a different bucket set) simply substitutes its own — nothing in the stored format depends on this choice.

---

## Illegal Moves

| GVA field | How to compute |
|---|---|
| `summary.illegal_moves` | Count checker plies where `analysis.illegal_move == true` (across all games) |

---

## Game Result Type

`games[i].result.type` (`"normal"`, `"gammon"`, `"backgammon"`, `"resign"`, `"forfeit"`) is not stored. Infer from the final ply of the game:

| Final ply `action_id` | Result type |
|---|---|
| 24 (Game Over) or 25 (Match Over) | Divide `points_won` by cube value at game end: 1 → `"normal"`, 2 → `"gammon"`, 3 → `"backgammon"`. This works *because* `points_won` stores the game's full value uncapped — see [Score at Game Start](#score-at-game-start) |
| 26 (Final) | `"normal"` (pass/drop endings) |
| 27 (Resign Game) or 28 (Resign Match) | `"resign"` |
| 29 (Force Forfeit) | `"forfeit"` |

**Cube value at game end**: track through the game's ply sequence — each Double (action_id 21) followed by Take (22) doubles the cube; start from 1.

---

## Score at Game Start

`games[i].score_start` is not stored. Two options:

1. Decode from `ogid_before` on the first ply of the game (fields 7–8 of the OGID are white/black scores)
2. Accumulate from previous games — **capping each game at what its winner still
   needed**, because `points_won` stores the game's *full* value:

   ```python
   from gvformat.binary import cap_points_won   # JS: capPointsWon

   white = black = 0
   for g in games[:i]:
       if g["winner"] == 0:
           white += cap_points_won(g["points_won"], white, match_length)
       elif g["winner"] == 1:
           black += cap_points_won(g["points_won"], black, match_length)
   ```

   A match ends the instant somebody reaches the target, so the deciding game
   banks only the remainder: a 4-point gammon won at 12-away of 13 makes the
   match 13-7, not 16-7. Summing raw overshoots. The stored value stays
   uncapped because it is also the only record of the win *type* (the
   `points_won / cube` rule above). Money play (`match_length = 0`) is never
   capped.

---

## Eval Level / Base Level

| GVA field | How to compute |
|---|---|
| `summary.eval_level` | Read from `analysis_info.eval_level` (stored as a bgsage extension on `analysis_info`) |
| `summary.base_level` | Infer: if any alternative in the match has a different `eval_level` than the primary, the cheaper one is the base level. Or simply omit — it does not affect correctness. **Only sound for a two-level run** — see below. |

The `base_level` rule assumes the analysis was a screen-and-deepen pass: one
cheap level over every candidate, a dearer one over the few that stayed close.
That is what bgsage's presets do, and there "the cheaper level" names something
real.

An imported match need not be one. XG picks a level per decision, so a single
XG match carries `1ply` through `4ply`, `truncated2`, `truncated3` and
`database` on alternatives of the same document — seven levels, no screen
among them (`samples/xg/issue.xg` is exactly this). Inferring a `base_level`
there invents a structure the run did not have; omit the field instead.

The levels are not totally ordered by cost either, so "cheaper" cannot be
decided from the name alone: `database` is a lookup and cheaper than anything,
and a truncated rollout against a plain ply depth depends on the trial count,
which is not stored. Compare only within a family (`1ply` < `2ply`,
`truncated1` < `truncated2`), and treat a match that mixes families as having
no base level.

---

## Engine Level Names

`eval_level` is stored in one closed vocabulary — `1ply`…`4ply`, `truncated1`…
`truncated3`, `rollout`, `database` — because that is exactly what the binary
`GVAN` byte encodes (a 4-bit depth plus the truncated/rollout/database flags).
A name outside it encodes as `0`, which reads back as "same as
`base_eval_level`" rather than as itself, so an importer that kept a foreign
engine's own wording would lose the level on write and not be able to tell
afterwards. The converters therefore map into the canonical names, and the
engine's wording is **recovered on read** from the level plus the engine.

| GVA field | How to compute |
|---|---|
| an entry's level *as the engine named it* | From that entry's `eval_level` and the `model_id` of **the analysis block the entry belongs to**. For `model_id == "xg"`, apply the table below; for any other engine, the canonical name already is the engine's name. |

| stored `eval_level` | + `model_id == "xg"` |
|---|---|
| `truncated1` | XG Roller |
| `truncated2` | XG Roller+ |
| `truncated3` | XG Roller++ |
| `database` | XG opening book |
| `1ply`…`4ply`, `rollout` | unchanged — XG calls these the same thing |

This is the inverse of `_LEVEL_NAMES` in `gvformat/xg.py` (and `LEVEL_NAMES` in
`gvformat-js/src/xg2gva.js`), which is where XG's numeric level codes are
mapped in. The three XG Roller settings are short truncated rollouts and the
opening book is a lookup rather than a search, so the canonical names describe
what XG did — the rename is presentational, not a reinterpretation.

**Per entry, not per match.** `eval_level` varies by alternative and by cube
decision — XG picks a level per decision, and bgsage's presets deepen only the
close ones — so this is computed for each alternative and each cube record, not
once for the document. GammonView's match viewer does exactly this in its `Lvl`
column, abbreviating to `XGR+` with the full name on the cell's tooltip.

**Take `model_id` from the right block.** It lives on `analysis_info`, or on
`analyses_info[i]` for a merged document. A match holding both an XG block and
a bgsage one has two engines and two vocabularies over the same plies: the
bgsage block's `truncated2` is its own 2-ply truncated rollout and must not be
relabelled. Keying on the document's primary block instead of the entry's own
would mislabel every entry in the other one.

**One thing does not come back.** XG has two opening-book codes (998 `ob_v2`,
999 `ob_v1`) and both map to `database`, so the book *version* is not
recoverable — "XG opening book" is as precise as the stored file can be. The
format has no field for it, and it does not affect any equity.
