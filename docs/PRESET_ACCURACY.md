# Preset accuracy

What the presets actually cost you in accuracy, measured against eXtreme Gammon.

This document records one experiment. 493 backgammon matches were analysed four
times over — once by XG at World Class, and once each by `world_class`,
`world_class_fast` and `balanced` — and the four readings compared decision by
decision. It answers the question a user asks when picking a preset ("what do I
give up by not running the slow one?") and the question a maintainer asks
("where is the extra compute going?").

It does **not** measure engine strength, and it does not establish that either
engine is right. See [Agreement is not accuracy](#agreement-is-not-accuracy),
which is the most important section here.

`very_quick`, `fast` and `deep` were not included.

## The dataset

| | |
|---|---|
| Matches | 493 (2026 OpenGammon record of a single player) |
| Player-match ratings | 986 |
| Checker plays | 70,972 (61,460 scoreable by XG) |
| Cube nodes | 39,489 (8,334 scoreable by XG) |
| Match lengths | 437 × 5pt, 33 × 7pt, 11 × 3pt, 6 × 1pt, 4 × 9pt, 1 × 11pt, 1 money |

Every match is a single `.gva` document carrying four analysis blocks over the
same plies, so the readings are indexed by construction rather than matched by
heuristic. Match PR comes from `gvformat.stats.compute_aggregates`, run once per
block with that block projected into `ply["analysis"]` — so all four PRs use one
definition and differ only in the evaluations underneath. Moves are compared
after canonicalising hop chains, so `13/11 11/6` and `13/6` compare equal.

`world_class` matters more than the other two here: it searches at the same
tier XG does (4-ply first pass), so **XG ↔ `world_class` is the engine
difference with the preset held still**. Everything else is measured against
that floor.

## Headline: the floor is the engine, not the preset

Mean absolute difference in match PR, over the same 986 player-match ratings:

| Pair | Bias | MAE | RMSE | p90 abs | r | Within 1 PR |
|---|---|---|---|---|---|---|
| XG ↔ `world_class` | +0.095 | **0.434** | 0.626 | 0.919 | 0.9948 | 91.8% |
| XG ↔ `world_class_fast` | +0.108 | **0.441** | 0.641 | 0.979 | 0.9947 | 90.7% |
| XG ↔ `balanced` | −0.036 | 0.540 | 0.742 | 1.151 | 0.9925 | 86.7% |
| `world_class` ↔ `world_class_fast` | +0.013 | **0.136** | 0.254 | 0.327 | 0.9992 | 99.0% |
| `world_class` ↔ `balanced` | −0.131 | 0.406 | 0.571 | 0.889 | 0.9959 | 93.0% |
| `world_class_fast` ↔ `balanced` | −0.144 | 0.403 | 0.583 | 0.869 | 0.9958 | 92.5% |

Pooled PR over all 986 ratings: XG 9.768, `world_class` 9.842,
`world_class_fast` 9.852, `balanced` 9.711.

**`world_class_fast` is already at the floor.** It lands 0.441 from XG where the
deepest preset lands 0.434 — the upgrade is worth 0.007 PR. The two presets sit
0.136 from each other, three times closer than either is to XG.

Decomposing the squared PR error against XG:

| | MSE to XG | engine part (XG ↔ `world_class`) | preset part | RMSE recovered by upgrading |
|---|---|---|---|---|
| `world_class_fast` | 0.411 | 0.392 — **95%** | 0.064 — 16% | +0.015 |
| `balanced` | 0.551 | 0.392 — 71% | 0.326 — 59% | +0.116 |

The shares do not sum to 100% because the two components are correlated: the
same hard positions trouble both. The asymmetry is the useful part —
`balanced` has real headroom, `world_class_fast` has essentially none.

Each engine decides for itself which decisions are scoreable, so the
denominators differ slightly (71,676 / 71,360 / 71,197 / 71,629). That accounts
for most of the small positive bias, not the evaluations.

## Checker play

Agreement with XG on the top move, over the 61,460 decisions XG scores:

| | same top move as XG | same as `world_class` | advice-PR by XG | XG blunders caught | blunders shown as fine |
|---|---|---|---|---|---|
| `world_class` | 90.28% | — | 0.333 | 93.2% | 5 of 4,144 |
| `world_class_fast` | 89.97% | 97.05% | 0.353 | 93.2% | 6 of 4,145 |
| `balanced` | 89.21% | 90.39% | 0.412 | 90.3% | 13 of 4,142 |

*advice-PR by XG* prices each preset's recommended play using XG's own equities:
the PR a player would post by following that preset on every decision. Under
0.42 for all three — below what XG itself calls world-class play.

Severity agreement (cuts at 0.02 and 0.08 equity lost) is 96.19% / 96.19% /
95.31%. The first two are identical to three significant figures on every row of
this table but one.

**The disagreements sit where the plays are equal.** Splitting by how far XG
separates its own best play from its runner-up:

| XG's best-vs-2nd gap | n | `world_class` | `world_class_fast` | `balanced` |
|---|---|---|---|---|
| under 0.005 | 10,704 | 63.7% | 63.2% | 61.0% |
| 0.005 – 0.02 | 11,910 | 87.3% | 86.4% | 85.1% |
| 0.02 – 0.05 | 11,232 | 97.2% | 97.0% | 96.4% |
| 0.05 and up | 27,610 | **99.0%** | **99.0%** | **99.0%** |

On the 45% of decisions XG calls clearly, all three presets agree with it on
99.0% — to the decimal. Depth buys nothing there because there is nothing left
to buy. The median gap between XG's top two plays is 0.0476 where bgsage picks
the same move and 0.0027 where it does not.

What does **not** reproduce is the ordering of near-equal plays: bgsage matches
XG's exact top-two set on 74% of decisions and its top-three set on 71%, though
XG's best play lands inside bgsage's top three 98.0% of the time. Deep in the
list neither engine is reliable — at the eighth-best play XG has fallen to 1-ply
on 35% of moves, where `world_class_fast` is still at 3-ply or better on 66%.

## The cube, and why `world_class` looks better

Cube action agreement with XG, over the 8,334 scoreable cube decisions:

| | same action as XG | same as `world_class` | advice-PR by XG |
|---|---|---|---|
| `world_class` | 96.47% | — | 0.179 |
| `world_class_fast` | 95.22% | 97.48% | 0.251 |
| `balanced` | 94.95% | 96.74% | 0.347 |

This is the only place the presets separate, and the reason is not depth.

Both presets record the `eval_level` they actually used on each cube. Grouping
by that choice:

| Cube decisions, grouped by which preset rolled out | n | `world_class_fast` | `world_class` |
|---|---|---|---|
| Both made the **same** choice | 6,719 | 96.90% | 97.08% |
| **Only `world_class_fast`** rolled out | 1,491 | 90.01% | 96.78% |
| **Only `world_class`** rolled out | 124 | 66.94% | 59.68% |
| All scored cube decisions | 8,334 | 95.22% | 96.47% |

Where both make the same escalation choice — 81% of scored cubes — the 3-ply
screen and the 4-ply screen agree with XG within 0.18 of a point. The entire
1.25-point gap lives in the 1,491 cubes `world_class_fast` sent to a rollout and
`world_class` did not. On the 124 that went the other way, the ordering
reverses.

**Rolling out costs agreement with XG, identically for both presets.**
`world_class` agrees on 98.9% of the cubes it left at 4-ply and 89.4% of those
it rolled out; `world_class_fast` gets 98.5% and 90.7%. Holding XG's own margin
fixed sharpens it: among cubes XG separates by under 0.01, `world_class` agrees
on 90.2% of the ones it left at 4-ply and 60.1% of the ones it rolled out.

`world_class_fast` escalates borderline cubes to a rollout by design (its
`mid_pass`), so it rolls out 41.8% of scoreable cubes against `world_class`'s
25.4%, at a median XG margin of 0.036 against 0.059. It substitutes a different
estimator exactly where the answer is most sensitive. That is the whole effect.

## Agreement is not accuracy

Every headline number in this document measures *similarity to XG*. Where
bgsage substitutes a `truncated2` rollout for a 4-ply search it scores worse by
that yardstick whether the rollout is better or worse.

There is direct evidence the difference is systematic rather than noise. On the
1,990 cubes both presets rolled out, their three equities are **bit-identical**
— maximum difference 0.000000, the rollout seed and trial count being fixed —
yet jointly displaced from XG by −0.0037 on no-double and −0.0036 on
double/take. Two presets, one method, one consistent offset.

Which is right is a separate question, and this experiment has no arbiter that
could answer it. It has been asked separately: `gvanalysis/presets.py` records
632 borderline cubes scored against an independent `truncated3` arbiter, where a
single `truncated2` run beat 4-ply (12% wrong verdicts and a 0.0091 mean gap,
against 14% and 0.0127), and the same test on 175 sizing-tier checker errors
landed the same way. If that holds, `world_class_fast`'s cube numbers are not
worse than `world_class`'s — they are further from XG because they are closer to
the truth.

Two consequences worth stating plainly:

- **Do not tune a preset to raise its XG agreement by rolling out less.** That
  is the one change that would reliably move the number while plausibly lowering
  quality.
- **A deeper cube screen is not the lever it appears to be.** It is worth 0.18
  of a point.

## Choosing a preset

- **`world_class_fast` is the recommended analysis.** It reproduces XG's match
  rating to within 1 PR on 90.7% of player-matches and within 2 on 98.3%, picks
  the same checker play on 90.0% of decisions and 99.0% of the ones XG calls
  clearly, and catches 93.2% of the plays XG calls blunders. It is
  indistinguishable from the deepest preset on checker play and on PR.
- **`world_class` is for reproducing XG specifically.** It is not more accurate
  than `world_class_fast` in any way this experiment demonstrates; it is more
  XG-like on borderline cubes, because it rolls them out less often. Reach for
  it when matching XG's verdict is itself the goal — as in this study.
- **`balanced` is the quality/speed option, and the only one with real
  headroom.** Within 1 PR on 86.7% of matches, 90.3% blunder recall. Upgrading
  it recovers eight times as much RMSE as upgrading `world_class_fast` does.
  Worth knowing: over a long record its near-zero bias makes it the *closest* of
  the three — pooling 100 matches, its 90th-percentile gap to XG is 0.109 PR
  against 0.164 for `world_class_fast` — while being the worst on any single
  match (1.150 against 0.885).

Cost is the other half of that choice and is not measured here; the wall-clock
table is in [`CLI.md`](CLI.md#what-they-cost). Briefly: `world_class` costs
about twice `world_class_fast` for the 0.007 PR above, and `balanced` costs
about a quarter of `world_class_fast` for the 0.1 PR above.

Luck is not a differentiator: it is evaluated at 1-ply in every preset, so all
three return bit-identical luck totals. Against XG the per-player-match
difference averages 0.0001 with a standard deviation of 0.31, on totals ranging
from −5.4 to +6.6.

## Limitations

- **"Matched depth" is close, not exact.** `world_class` is a 4-ply first pass
  with a `truncated2` sizing tier; XG World Class is 4-ply with its own rollout
  conventions. Same tier of effort, not the same algorithm — so the 0.434 PR
  floor is an upper bound on the pure engine difference, not a measurement of
  it.
- **XG's per-decision level is not recorded** in these files, so there is no way
  to check here whether XG escalates a borderline cube the way bgsage does.
- **One player pool, one format.** 493 matches from a single player's online
  record, 437 of them 5-point. Longer matches, money play and a different
  opponent mix are not represented in proportion.
- **Counted sets differ by design** and are reported as a source of difference
  rather than normalised away.
- **Ties inflate the disagreement rate.** 39% of "different top move" cases are
  exact equity ties in already-decided positions.
- **No timings are claimed.** This document measures agreement only. What
  each preset costs in wall clock is measured separately, in
  [`CLI.md`](CLI.md#what-they-cost).
