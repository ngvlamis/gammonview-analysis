# One knob is two knobs: mid-tier escalation, and what `close_threshold` is for

Measured 16-18 Sep 2026 on the **Mac Studio (M2 Ultra, 24 cores, 128 GB)**,
bgsage 2.0.20260907 — the machine that owns `tests/golden/`. Follow-up to
[`2026-09-16-checker-mid-tier.md`](2026-09-16-checker-mid-tier.md), which was
measured on the M3 Air.

Corpus: **101 matches** sampled (seeded) from the same 493-match OpenGammon
record behind `docs/PRESET_ACCURACY.md`, plus the two matches the first study
used. 11,786 scoreable checker decisions, 4,972 cube decisions, 202
player-match ratings. XG's own analysis of the same matches is the referee
throughout.

**Headline: the defect is confirmed on both decision kinds and the fix is worth
shipping. The threshold retune the first study proposed is not — but only
because `close_threshold` is one dial doing two unrelated jobs. Split it and
the retune becomes the best configuration measured.**

---

## 1. The defect, at scale

A borderline checker play — top two moves within `close_threshold` — is capped
at the middle tier and never re-escalated, however large the played move's
error turns out to be. At the shipped `balanced` threshold of 0.04:

| | |
|---|---|
| borderline (capped at the middle tier) | 6,338 of 11,786 — 53.8% |
| of those, played move lost **more** than the threshold | **633 — 10.0%** |
| lost more than twice the threshold | 245 |
| worst error sized at 3-ply | **0.5622** |

~6.3 mis-sized real errors per match. `top2_gap` is best-vs-second and says
nothing about how far down the list the player actually went:
`correlation(top2_gap, played error) = +0.116`. They are independent quantities,
which is the whole of the bug.

**`balanced` is the only shipped preset with a checker middle tier**, so it is
the only one affected on this path. The cube path (§6) is a different story.

## 2. Four variants, not one

The first study's prototype says it applies "the rule the cube path already
applies via `real_error`". It does not. `_eval_cube_decision` tests
`real_error` **first**, off the *screen's* numbers, so a borderline-and-wrong
cube skips the middle tier entirely. Four distinct rules were measured:

| | rule | cost shape |
|---|---|---|
| **POST** (the prototype) | borderline → mid, then rollout if the *mid tier* says error > thr | pays mid + rollout |
| **PRE** (literal cube parity) | *screen* says error > thr → rollout, skip mid | cheapest |
| **BOTH** | PRE's shortcut plus POST's re-check | catches what the screen understates |
| **ANY** | borderline → mid, escalate on *any* error the mid tier finds | no threshold at all |

They are not the same set: at 0.04 over the corpus, PRE catches 576, POST 633,
BOTH 760 — 127 that only PRE finds and 184 that only POST does.

## 3. The arbiter is not neutral — and neither were the repo's existing ones

Scored on the 760 decisions the correction escalates at 0.04, asking whether
3-ply (shipped) or `truncated2` (corrected) sizes the error better:

| referee | `truncated2` closer | mean-gap ratio |
|---|---|---|
| `truncated3` — shares method, seed, trial count with `truncated2` | 77.4% | 2.59x |
| `4ply` — full-width, the 3-ply tier's own family | 43.8% | **0.94x** |
| `rollout`, untruncated — no shared truncation horizon | 61.4% | 1.51x |
| **XG — a different engine entirely** | **61.3%** | **1.55x** |

**The verdict flips under a same-family arbiter.** The two independent referees
land within a hair of each other (61.3% / 61.4%) and squarely between the two
kin-biased ones. Scale was checked: mean error 0.0599 XG vs 0.0608 `truncated3`,
r = 0.982. The two independent referees disagree with *each other* by 0.0129 on
average — the same order as the ~0.015 effect — so this is a reliable aggregate
over hundreds of decisions, not a per-decision oracle.

Against XG the escalation improves error sizing by **35–42%**, consistently:
+36.3% at 0.02, +35.5% at 0.04, +37.7% at 0.08, +42.0% at 0.12. The
full-rollout referee independently gives +38.6% and +43.5%.

> **This contaminates two results the repo already relies on.**
> `gvanalysis/presets.py` justifies `truncated2` as the sizing tier over 4-ply
> on "632 borderline cubes scored against an independent `truncated3` arbiter"
> and a 175-checker-error repeat. That arbiter is *not* independent of
> `truncated2` — it shares method, seed and trial count — and it is from the
> opposite family to the 4-ply it was judging. Both comparisons are confounded
> in the direction of their stated conclusion, and `docs/PRESET_ACCURACY.md`
> leans on them in its "Agreement is not accuracy" section. **Re-run both with
> XG or an untruncated rollout as referee.** The conclusion survived here — the
> independent referees still favour `truncated2`, at 1.55x rather than 2.59x —
> but the margin as stated is roughly 1.7x too generous.

## 4. What XG actually does

The corpus is 493 matches analysed at XG World Class, and XG records the
`eval_level` it used for every alternative. Mining 78,661 decisions answers the
design question directly — and says our schema conflates two things.

**Close decisions get more depth.** XG grades depth by the top-2 gap:

| top-2 gap | 4-ply | 3-ply | `truncated2` |
|---|---|---|---|
| < 0.005 | **93.7%** | 1.1% | 3.0% |
| 0.005 – 0.02 | 85.3% | 0.4% | 8.5% |
| 0.02 – 0.05 | 66.7% | 0.3% | 29.5% |
| 0.05 – 0.08 | **64.9%** | 0.2% | 32.3% |
| 0.08 – 0.12 | **15.0%** | 57.5% | 26.4% |
| 0.12 + | 8.3% | 74.2% | 15.6% |

(63,548 checker decisions carrying a top-2 gap; the level XG used for the best
move. The rising `truncated2` column is the *other* variable leaking in — a
wider gap correlates with the played move being wrong — which is precisely why
the two need separating.)

The 4-ply share falls off a cliff between the 0.05–0.08 band and the 0.08–0.12
one: 64.9% → 15.0%. **The crossover is at 0.08**, three times wider than
`balanced`'s 0.04.

**Errors get rolled out, with a cliff at 0.02.** This is a different variable
and a much sharper threshold:

| played move's error | share rolled out (`truncated2`) |
|---|---|
| 0.015 – 0.020 | 22.9% |
| 0.020 – 0.030 | **94.7%** |
| 0.050 – 0.100 | 99.0% |

**The cross-tab is decisive**, because it is exactly the cell our code gets
wrong:

| | played right | played wrong by > 0.02 |
|---|---|---|
| **close** (gap ≤ 0.08) | 3.9% rolled out | **88.7% rolled out** |
| **clear** | 0.1% rolled out | 99.2% rolled out |

XG does **not** exempt a close decision from the rollout tier. `balanced`
exempts it at 100%. That is the defect, independently confirmed from the outside.

And the two variables want opposite values: 0.08 for "look harder", 0.02 for
"measure it properly". One number cannot be both.

## 5. Why the split decouples cost

Because the two tiers are not remotely the same price. Per checker decision,
measured on this corpus (post-`checker_eval.py` screening):

| tier | mean cost |
|---|---|
| 2-ply screen | 0.0057s |
| 3-ply middle tier | 0.0357s |
| `truncated2` sizing tier | **1.208s** |

A **34x** ratio. The middle tier is nearly free, so `close_threshold` can be
generous; the rollout is the entire bill, so `error_threshold` must be earned.
Tying them together forces one compromise on both — and, perversely, *widening*
the shared knob speeds the analysis up, because it diverts borderline decisions
away from the rollout into the cheap tier. That is a cost artifact, not a
quality choice.

Rollouts only repay their cost above ~0.02 of error. Improvement in sizing
accuracy from escalating, by error band, XG as referee:

| played move's error | sizing accuracy gained |
|---|---|
| 0.00 – 0.01 | +3.7% |
| 0.01 – 0.02 | +8.3% |
| 0.02 – 0.04 | **+34.3%** |
| 0.04 – 0.08 | +29.8% |
| 0.08 + | +37.0% |

XG's 0.02 cliff is where the value appears. Independently derived, same number.

Across the corpus, an `error_threshold` of 0.02 exempts **43.3% of all flagged
checker errors** from the rollout tier, and those carry **6.8%** of the total
error equity. That is the trade in one line.

## 6. The same defect on the cube path

The cube branch tests `real_error` *before* `close`, so it cannot skip the
rollout on a decision the screen already knows is wrong. But it computes
`real_error` from the **screen's** numbers and never re-tests after the middle
tier runs — so a cube whose margin the screen *understates* is routed to the mid
tier, discovered there to be a real error, and left sized at 3-ply anyway.

Instrumented over the corpus (98 matches, 7,059 cube decisions; 2-ply screen /
3-ply mid / `truncated2` sizing / `truncated3` arbiter — `balanced`'s shape):

| `close_threshold` | screen → rollout | borderline → mid only | **missed** |
|---|---|---|---|
| 0.02 | 677 (9.6%) | 239 (3.4%) | **24 — 0.34% of cubes, 10.0% of mid-tier cubes** |
| 0.04 | 599 (8.5%) | 417 (5.9%) | **30 — 0.42% of cubes, 7.2% of mid-tier cubes** |
| 0.08 | 449 (6.4%) | 758 (10.7%) | **25 — 0.35% of cubes, 3.3% of mid-tier cubes** |

**Rarer than the checker defect, but the misses are large.** At 0.04 the missed
errors have a median mid-tier margin of 0.0692 and a median *arbiter* margin of
0.0739, topping out at 0.2617. The screen understates by 5–25x in the worst
cases:

| screen margin | 3-ply | `truncated2` | `truncated3` |
|---|---|---|---|
| 0.0039 | 0.0946 | 0.1171 | 0.1073 |
| 0.0163 | 0.0755 | 0.1067 | 0.0999 |
| 0.0184 | 0.0912 | 0.1105 | 0.0811 |

Both rollouts corroborate the mid tier, so this is the screen misjudging the
position, not 3-ply noise.

Sizing these correctly recovers **0.73 equity** of accuracy across the corpus,
concentrated in the ~1 match in 3 that contains one — worth up to **+0.79 PR**
on an affected match (and +0.96 at a 0.02 window), against an overall MAE of
~0.47. The fix escalates 30 cubes at 0.30s each: **~0.09s per match.**
Effectively free, occasionally worth a full PR point. Ship it.

## 7. End to end: what a user actually gets

All eleven arms over 101 matches, PR compared against **XG's own ratings** on
202 player-match ratings, the yardstick `docs/PRESET_ACCURACY.md` uses. (Shipped
`balanced` scores 0.5129 here against that document's 0.540 on the full 493 —
close enough to confirm the harness; the document's number is the authority.)

`cNN` is the mid-tier gate, `eNN` the rollout gate, where they differ.

| arm | MAE vs XG | RMSE | bias | p90 | within 1 PR | wall | rollouts |
|---|---|---|---|---|---|---|---|
| **shipped @ 0.04** | 0.5129 | 0.7102 | −0.063 | 1.151 | 88.1% | 1784s | 1149 |
| `BOTH` @ 0.12 (first study's direction) | 0.5427 | 0.7800 | −0.071 | 1.116 | 86.6% | 1526s | 702 |
| `BOTH` @ ck 0.12 / cube 0.04 | 0.5099 | 0.7037 | −0.047 | 1.078 | 88.1% | **1592s** | 780 |
| `BOTH` @ 0.06 | 0.5054 | 0.7191 | −0.055 | 0.956 | 91.1% | 2294s | 1424 |
| `PRE` @ 0.04 | 0.4793 | 0.6518 | **−0.017** | 0.951 | 91.1% | 2633s | 1725 |
| `BOTH` @ 0.04 (fix only) | 0.4729 | 0.6347 | −0.041 | 0.901 | 91.1% | 2874s | 1909 |
| `ANY` @ 0.02 | 0.5019 | 0.6729 | −0.067 | 0.957 | 90.1% | 5120s | 3993 |
| `ANY` @ 0.04 | 0.4783 | 0.6490 | −0.028 | 0.922 | 90.6% | 5256s | 3983 |
| `ANY` @ 0.12 | 0.4936 | 0.7220 | −0.039 | 1.078 | 89.6% | 5272s | 3913 |
| **`c08 / e04`** | 0.4694 | **0.6310** | −0.021 | **0.903** | **91.6%** | 2913s | 1913 |
| **`c08 / e02`** | **0.4586** | 0.6450 | −0.023 | 0.955 | 91.1% | 3851s | 2712 |

**The fix is validated.** `BOTH` @ 0.04 has cube routing byte-identical to
shipped, so its entire +7.8% comes from sizing checker errors correctly. It
closes ~40% of the distance between `balanced` (0.513 here) and `world_class`
(0.434 in the doc), and lifts within-1-PR from 88.1% to 91.1%.

**The shared retune was wrong, and why.** `close_threshold` governs *both*
decision kinds. Widening it to 0.12 pulls borderline cubes off the rollout tier
— cube rollouts 137 → 98, cubes at 3-ply 65 → 198 — and that costs more PR than
the wider checker window gains. A checker-only sweep cannot see this. Holding
the cube threshold at 0.04 recovers it entirely (0.5427 → 0.5099).

**`ANY` is dominated.** Removing the threshold entirely collapses the policy to
"roll out every checker error", which is 80% more expensive than `BOTH` @ 0.04
and no more accurate (best `ANY` arm 0.4783 at 5256s vs 0.4729 at 2874s). It
also **flattens** the cost curve rather than inverting it — 3,993 / 3,983 /
3,913 rollouts across thresholds 0.02 → 0.12 — because the threshold no longer
gates anything. A prediction that the gradient would invert was wrong.

**The two-knob arms win on every axis but cost.** `c08 / e04` takes the best
RMSE, p90 and within-1-PR simultaneously. `c08 / e02` — XG's own cliff — takes
the best mean at 32% more compute, with a slightly worse tail; on 202 samples
that tail difference is within noise and should not be read as a real effect.

## 8. Recommendations

1. **Make the escalation unconditional, on both decision kinds.** Prefer `BOTH`.
   After the middle tier runs, re-test the error and escalate. Three lines on
   each path; the cube path is missing the same re-check for the same reason.
   Drop the `GVAN_MID_ESCALATE` gating — it was study scaffolding.

2. **Split `close_threshold` into two fields**, both accepting a
   `{checker, cube}` mapping exactly as `mid_pass` already does:

   | field | gates | meaning | cost when it fires |
   |---|---|---|---|
   | `close_threshold` | `mid_pass` | "too close to call at the screen" | ~0.036s |
   | `error_threshold` | `second_pass` | "wrong by enough that the size matters" | ~1.2s |

   `error_threshold` defaults to **0.02** — the standard error cutoff, and
   independently where XG's rollout cliff falls (§4) and where rollouts start
   repaying their cost (§5). `close_threshold` for `balanced` becomes **0.08**,
   XG's own crossover.

   > **Open decision, and a bigger one than it looks.** The checker error
   > branch has *no* threshold today — `close_threshold` gates only the
   > borderline test, and the error test is the bare `played != screen best`.
   > **Every** preset with a `second_pass` therefore rolls out every
   > disagreement of any size, `balanced` and the 2-tier presets alike.
   > Introducing `error_threshold` at 0.02 exempts **43.3%** of all flagged
   > checker errors while giving up **6.8%** of the error equity — a large
   > saving for a small cost, and closer to what XG does (§4). But it is a
   > behaviour change to `fast`, `balanced`, `world_class` and
   > `world_class_fast`, and it moves the goldens. Decide deliberately rather
   > than by inheriting the default.

3. **Do not adopt a shared 0.08/0.12/0.16.** The first study's table pointed
   that way because the defect was suppressing exactly the expensive rollouts a
   wider threshold would have needed, and because a checker-only sweep cannot
   see the cube regression.

4. **Re-run the two arbiter tests in `presets.py`** with a non-kin referee — see
   §3. The conclusion survives; the stated margin does not.

5. **Fix the prose either way.** `presets.py`'s "errors bigger than
   `close_threshold` still go to `second_pass`" describes the cube path only and
   is false for checker plays; the `CLAUDE.md` `close_threshold` paragraph
   inherits the claim. The sentence is arguably what let the defect sit
   unnoticed.

6. **Document what a middle tier is for**, since a user writing their own preset
   currently has no way to know:

   > `mid_pass` is a **second look before judging** — it fires on decisions the
   > screen could not call confidently, and its job is to settle them.
   > `second_pass` is **sizing an error you already believe in** — it fires when
   > a decision is wrong by enough that the magnitude matters. A decision can
   > reach `second_pass` straight from the screen, or via `mid_pass` when the
   > closer look is what revealed the error.

   That last clause is precisely what is broken today, on both paths.

## 9. Method notes

- Per-decision instrumentation (screen / mid / sizing / arbiter in one pass)
  made every threshold sweep a query over a table rather than 24 engine runs.
  Verified deterministic: re-running produced bit-identical tier values.
- Restricting the expensive tiers to escalation candidates (error > 0.015) cut
  instrumentation from 157s to 70s per match with identical output.
- **Serial with full engine threads beat 4-way process parallelism by 1.8x**
  (4 matches: 517s parallel vs ~280s serial). bgsage's internal threading
  already saturates 24 cores at these levels; `--jobs` is counterproductive here.
- The cube instrumentation's `close` test approximates the shipped one: it omits
  the `_trivial_cube` / `_trivial_take_pass` guards, which can only *reduce* the
  mid-tier population, so the missed-case rate is if anything conservative.
- **Four analysis errors caught by sanity checks, all of which had produced
  confident wrong numbers**: charging the middle tier to candidate decisions
  only (made shipped look dominated when it was not); joining match PR to XG
  ratings with Python's `sorted()` when the runs had gone through `bash`'s
  locale-collated glob (MAE 5.24 instead of 0.51); deriving the XG checker
  ordinal from recorded decisions only, and filtering `action_id == 14` when
  checker plies span 0–20 (829 and 74 joined rows instead of 11,786); and a
  cost estimate for the two-knob config that omitted the screen, cube work and
  luck (+27% predicted, +63% actual). Each was caught by checking a result
  against a number known from elsewhere. Keep doing that.
- No goldens were regenerated. `tests/golden/PROVENANCE.json` is untouched.

## 10. Reproducing

Working files (scratch, not in the repo): `instrument.py`, `analyze.py`,
`sweep2.py`, `final_sweep.py`, `xg_join.py`, `xg_score.py`, `xg_tiers.py`,
`robust.py`, `pr_compare.py`, `cube_instr.py`, `cube_analyze.py`, plus
`study.jsonl` (11,786 rows), `xg.jsonl`, `xgtiers.jsonl` (78,661 rows),
`cubes.jsonl`, `arb4ply.jsonl`, `arbroll*.jsonl`, `e2e.jsonl`.

Variants are gated, default off, so the goldens and shipping presets are
untouched:

```bash
GVAN_MID_ESCALATE=1 ...   # POST  (the original prototype)
GVAN_MID_ESCALATE=2 ...   # PRE   (literal cube-path parity)
GVAN_MID_ESCALATE=3 ...   # BOTH  (recommended)
GVAN_MID_ESCALATE=4 ...   # ANY   (no threshold; dominated)
GVAN_CUBE_THRESHOLD=0.04  # hold the cube gate while sweeping the checker one
GVAN_ERROR_THRESHOLD=0.02 # the proposed second knob
```
