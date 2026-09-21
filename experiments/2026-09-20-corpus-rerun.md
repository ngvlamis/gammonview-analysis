# Corpus re-run after the September retune

**The retune left agreement with XG statistically unchanged on every measure.**
Both presets moved toward XG by about 0.01 PR; neither move is significant, and
the ordering between them is unchanged and still not resolvable.

Run 2026-09-20 on the Studio (24-core M2 Ultra) at `43054d1`, over the same 493
matches (84,008 plies, 986 player-match ratings) the original preset study used.
`world_class_fast` took 3 h 55 m, `world_class` 6 h 25 m.

## Method

Each corpus file already carried four analysis blocks — XG World Class plus the
three pre-retune presets — so the re-run is **paired by construction**: the new
block is appended to a copy of the same document, and old and new are read off
the same plies. Blocks are told apart by `model_id` and `ply`:

| block | `model_id` | `ply` | `eval_level` |
|---|---|---|---|
| XG World Class | `xg` | 4 | `4ply` |
| pre-retune `world_class` | `bgsage` | 4 | `truncated2` |
| pre-retune `world_class_fast` | `bgsage` | 3 | `truncated2` |
| pre-retune `balanced` | `bgsage` | 2 | `truncated2` |
| this run | `gv-bgsage/2.0.20260907` | 3 | `truncated2` |

**The two retuned presets are indistinguishable by metadata** — both are now
`ply: 3, truncated2`, an appended block carries no `preset` field, and
timestamps are rewritten identically on save. So they were written to separate
directories (`gva-rerun/wcf/`, `gva-rerun/wc/`) and attributed by path. Anything
comparing multiple own-analysis blocks in one document needs to know this.

Match PR per block comes from `gvformat.stats.compute_aggregates` over a
single-block projection of the document (each ply's `analyses[]` entry with the
matching `analysis_index` promoted to `analysis`).

## Result

| | mean gap to XG | bias | PR within 1 of XG |
|---|---|---|---|
| `world_class` (new) | **0.4228** | −0.1200 | 91.7% |
| `world_class_fast` (new) | **0.4308** | −0.0722 | 91.3% |
| `world_class` (pre-retune) | 0.4345 | −0.0948 | 91.8% |
| `world_class_fast` (pre-retune) | 0.4411 | −0.1078 | 90.7% |
| `balanced` (retired) | 0.5400 | +0.0358 | 86.7% |

Paired, on |gap to XG| per rating (negative = closer to XG after the retune):

| | change | 95% CI | t | closer / further |
|---|---|---|---|---|
| `world_class` | −0.0117 | [−0.0278, +0.0044] | −1.42 | 508 / 463 |
| `world_class_fast` | −0.0103 | [−0.0224, +0.0019] | −1.66 | 487 / 483 |

Every one of the 493 files changed, so this is not a null result from nothing
happening — the changes cancel. Head to head the two presets are still not
separable: `world_class` is nearer XG by 0.0081, CI [−0.0201, +0.0040], t =
−1.31, winning on 519 of 986 ratings.

**What this licenses:** removing the "pending re-measurement" notices from the
README and `CLI.md`, and updating the two columns re-measured here. **What it
does not:** any claim that the retune improved accuracy. It did not degrade it,
which is what was actually in doubt.

## The measurement was validated before it was trusted

Re-deriving the pre-retune blocks reproduces the published figures to four
decimals — `world_class` **0.4345** (published 0.434), `world_class_fast`
**0.4411** (0.441), `balanced` **0.5400** (0.540), and PR-within-1 of 91.8% /
90.7% / 86.7% against the published 91.8% / 90.7% / 86.7%. Same corpus, same
code path, so the new figures are comparable to the old by construction.

## Unresolved: the checker-play agreement column

**`PRESET_ACCURACY.md`'s "same checker play as XG" figures (90.3% / 90.0% /
89.2%) could not be reproduced, and the gap is not small.** Counting a decision
as agreeing when both blocks' highest-equity alternative is the same move gives
**83.4% / 83.1% / 82.5%** over the 61,460 decisions XG scores.

Two parts of the original method *were* recovered exactly, which is why the
remainder is puzzling: filtering on the XG block's `decision` flag yields
n = 61,460, the published "scoreable by XG", and filtering on more than one
alternative yields n = 63,548, the published count for the routing study. So the
decision set is right and the disagreement is in how a play is compared.

Collapsing chained hops so `13/10 10/4` equals `13/4` recovers 5 points
(78.4% → 83.4%) and is clearly necessary, but does not close the gap. What
remains is likely tie handling — treating moves within some epsilon of the best
as agreement would raise all four figures together.

Measured like-for-like the retune moves this metric by at most 0.6 points
(`world_class` 83.4% → 83.3%, `world_class_fast` 83.1% → 82.5%), so the
published figures are not materially stale even though they are not reproducible
here. They are left standing; the discrepancy is a methodology gap to close, not
a number to correct.

Also not re-measured: the "player who followed the preset every time" PR figures
(0.333 / 0.353), which are a different computation over XG's equities.
