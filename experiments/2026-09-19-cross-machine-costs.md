# Everything cost-based was measured on the fastest machine in the house

Measured 19 Sep 2026 on **four machines**, all running bgsage 2.0.20260907:

| host | chip | cores | OS |
|---|---|---|---|
| `mac_studio` | Apple M2 Ultra | 24 (16P + 8E) | macOS — owns `tests/golden/` |
| `macbook` | Apple M3 | 8 (4P + 4E) | macOS 26.5 — MacBook Air |
| `applepi` | Intel i5-8500B | 6, no HT | Ubuntu (t2linux) — thermally capped at 2.7 GHz |
| `imac` | Intel i5-7600 | 4, no HT | Ubuntu 24.04 — Kaby Lake, 3.5/4.1 GHz, 8 GB |

`imac` was added last and **replaces `applepi` as the weak-machine
reference** (§8). It is the slowest sound machine here and the only one with
fewer than six cores, so it is where a cost ratio tuned on the Studio is most
likely to break.

It is also, on most work, *faster* than the 6-core `applepi` despite having
four cores — 3-ply screen 0.150 s against 0.173 s, 4-ply 3.63 s against 5.30 s,
`fast` 75.5 ms/ply against 90.3 — which is the clearest single statement of what
applepi's thermal state costs it. The one row where applepi wins is
`very_quick` (34.5 ms/ply against 40.8), and that reversal is **unexplained**.
The obvious candidate, per-match engine construction dominating a cheap preset,
does not survive measurement: analyzer construction plus first eval is 0.50 s
for four workers on the M3 (§5.3), against a `very_quick` match of several
seconds. It is left as an anomaly rather than explained away.

Also worth noting for the table below: `imac` has **8 GB of RAM**, about 3 GB of
it free, which is the tightest memory of the four. Each worker process loads
bgsage's nets, so memory is a real constraint on how far `--jobs` can be pushed
on a machine like this — a limit the Studio never surfaces.

Corpus: **6 matches** from the tournament record (`~/Desktop/xg`: BCGMN,
Montclair Monthly, USBGF Championship) spanning 5, 7, 9 and 13-point lengths —
84 to 459 analyzed moves, 1,429 in all. Deliberately *not* the OpenGammon
sample the preset studies use, and deliberately long: a first pass of this
benchmark ran on 41–64 move matches and produced a qualitatively wrong answer
(see §7).

**Headline: every structural decision in `presets.py` survives cross-machine
testing. The cost ratios that justify them do not — the published "relative to
`fast`" column is outside the observed range on every machine measured,
including the Studio it was taken on.**

Two things changed during the study and are worth stating up front, because
both are corrections to it rather than findings from it:

- **`applepi` was disqualified mid-study** (§8). Run twice on the same day on
  identical work, it differed by 41%. No conclusion here depended on it, which
  is luck; the three-machine cross-check is what made that recoverable.
- **A claim about the auto jobs/threads split was withdrawn** (§5). A full
  18-point sweep said the shipped default was 38% slower than serial on an
  8-core laptop; re-run on a longer match from the same corpus, the same points
  reverse and the default wins. The sweep's unit — one match — was the error.
  Over the whole corpus the entire grid spans **2.1%**, so the default stands
  and the section's surviving findings are about documentation, not speed.

---

## 1. What transfers

Three claims in `presets.py` rest on cost measured only on the Studio. Two of
them hold everywhere.

**4-ply is cheaper than `truncated2` on a checker move list.** This is the
claim that pays for `checker_eval.py`. Paired over the same 40 decisions on
each machine (fixed seed, same sample), alternating order per decision to
cancel drift:

| machine | median ratio | geomean | total time | 4-ply faster on |
|---|---|---|---|---|
| `mac_studio` | 1.63x | 1.96x | 1.40x | 25/40 |
| `macbook` (M3) | **2.56x** | 2.96x | 2.10x | 33/40 |
| `imac` | 1.79x | 2.32x | 1.53x | 29/40 |

Not merely preserved — *stronger* on every weaker machine. The mechanism is in
the code: 4-ply's elevations run serially (`ctx.threads` resolves to 1 from the
default `parallel_threads=0`), while `truncated2` is a rollout level, so
`_screen_context` returns `None` and bgsage's own pool takes `n_threads=0` =
hardware concurrency. Dropping from 24 cores to 8 or 4 costs the rollout most
of its parallelism and costs 4-ply only the per-core penalty. Core count was
never propping this finding up; it was working against it — which is why the
4-core `imac` and the 8-core `macbook` both show it more strongly than the
Studio does.

**The 4-ply-vs-screen ratio is stable.** This is what justified dropping
`world_class_fast`'s checker middle tier. Median per-decision cost against the
3-ply screen, 40 decisions from the wide corpus, serial:

| machine | 3-ply screen | 4-ply | `truncated2` |
|---|---|---|---|
| `mac_studio` | 0.036 s — 1.0x | 0.775 s — **17.1x** | 0.945 s — **23.4x** |
| `macbook` | 0.082 s — 1.0x | 1.832 s — **21.6x** | 3.980 s — **39.7x** |
| `imac` | 0.150 s — 1.0x | 3.634 s — **20.7x** | 4.987 s — **31.4x** |
| ~~`applepi`~~ | 0.173 / 0.144 s | 5.303 / 4.378 s — 24.4 / 24.0x | 7.184 / 5.354 s — 37.8 / **32.3x** |

17 → 22 across a 6x range of machines, on the three that are thermally sound.
The decision stands.

applepi is struck through throughout and shows **two** figures per cell: the
same measurement run twice on the same day, 41% apart end to end. See §8 for
why that disqualifies it, and note that its `truncated2` ratio alone moves
37.8x → 32.3x between the two — a swing larger than the entire spread across
the three sound machines.

## 2. What does not transfer

The same table, other column. **The rollout ratio drifts 23x → 40x**, and the
M3 is the worst of the three sound machines even though it sits between the
others in core count — its 3-ply screen is fast enough that the denominator
stops flattering the rollout. Note this is the opposite ordering from the
4-ply column, where the M3 is also the extreme: the two levels do not degrade
together, which is the whole point of the section.

**The Studio is the most rollout-friendly machine tested, by a wide margin.**
Every "this error is worth rolling out" decision in `presets.py` was priced on
the friendliest possible hardware. Two consequences:

- `error_threshold` rationing rollouts is worth *more* off the Studio, not
  less. The 0.02 gate added on 18 Sep cut `world_class_fast`'s checker rollouts
  44.7%; that is worth roughly twice as much to an M3 user as it was here.
- Anything that sends *more* work to the rollout tier should be re-priced
  before shipping. The Studio will always make it look cheap.

## 3. What a user actually waits through

End to end, auto jobs (`--jobs 0`), the wide corpus, all six presets. The
figure is **milliseconds per ply**, with the slowdown against the Studio; the
Studio column also carries seconds per match for scale.

| preset | `mac_studio` | `macbook` (M3, 8c) | `imac` (4c) | ~~`applepi`~~ (6c) |
|---|---|---|---|---|
| `very_quick` | 5.8 ms — 1.4 s | 11.6 ms (2.0x) | 40.8 ms (7.0x) | 34.5 ms (5.9x) |
| `fast` | 9.6 ms — 2.3 s | 31.4 ms (3.3x) | 75.5 ms (7.9x) | 90.3 ms (9.4x) |
| `deep` | 18.0 ms — 4.3 s | 65.5 ms (3.6x) | 143.0 ms (7.9x) | 149.4 ms (8.3x) |
| `balanced` | 170.4 ms — 40.6 s | 751.8 ms (4.4x) | 1029.8 ms (6.0x) | 1390.6 ms (8.2x) |
| `world_class_fast` | 165.6 ms — 39.4 s | 611.0 ms (3.7x) | **1015.8 ms** (6.1x) | 1313.4 ms (7.9x) |
| `world_class` | 291.3 ms — 69.4 s | 1370.6 ms (4.7x) | **2048.0 ms** (7.0x) | 4156.4 ms (14.3x) |

In wall-clock terms a user can feel: on the 4-core `imac`, `world_class_fast`
is **242 s per match** and `world_class` **488 s** — eight minutes for a single
match, against 69 s on the Studio.

**The degraded machine is slower than a machine with two fewer cores, at every
preset that does real work** — and by 2.0x at `world_class`, the preset that
sustains load longest. That single comparison is §8's argument in one line.

**`docs/CLI.md` and `README.md` publish a single "relative to `fast`" column
(24x for `world_class_fast`, 45x for `world_class`). That number does not
exist**, and it does not drift monotonically with how weak the machine is:

| relative to `fast` | Studio | M3 | `imac` | ~~applepi~~ |
|---|---|---|---|---|
| `deep` | 1.9x | 2.1x | 1.9x | 1.7x |
| `balanced` | 17.8x | 23.9x | 13.6x | 15.4x |
| `world_class_fast` | 17.2x | 19.5x | **13.5x** | 14.5x |
| `world_class` | 30.3x | 43.6x | **27.1x** | 46.0x |

The weakest sound machine has the *lowest* multiple in both strong rows, not
the highest, because the denominator moves too: the `imac` is 7.9x the Studio
at `fast` but only 6.1x at `world_class_fast`, so the quotient falls even as
every absolute number rises. The published 24x for `world_class_fast` is
outside the observed range on **every** machine (13.5–19.5x); the published 45x
for `world_class` matches only the M3.

A ratio against a preset that is itself machine-dependent cannot be published
as a constant. The column should be replaced by per-ply absolutes on named
hardware, which at least degrade predictably — `deep` is the only row stable
enough (1.7–2.1x) to survive as a ratio.

## 4. `balanced` is dominated on every machine — but on accuracy, not cost

| | Studio | M3 | `imac` | ~~applepi~~ | MAE vs XG (200 matches) |
|---|---|---|---|---|---|
| `balanced` | 170.4 ms | 751.8 ms | 1029.8 ms | 1390.6 ms | 0.4474 |
| `world_class_fast` | **165.6 ms** | **611.0 ms** | **1015.8 ms** | **1313.4 ms** | **0.4208** |
| `balanced` is slower by | +2.9% | **+23.0%** | +1.4% | +5.9% | |

`world_class_fast` is faster *and* closer to XG on every machine tested, so the
direction of the conclusion is not in doubt. **But the cost margin is honestly
thin on three of the four**: +2.9% and +1.4% are within what a repeat run
moves. The M3's +23% is the outlier, not the pattern, and an earlier draft of
this note leaned on it harder than the data supports.

So the case against `balanced` rests on **accuracy** — 0.4474 against 0.4208
MAE — with cost as a tie-breaker that only one machine makes emphatic. That is
still a dominated preset: paying the same time for a worse answer is not a
trade-off. But "slower *and* worse" overstates a 1.4% difference, and the
recommendation should be argued from the MAE.

`balanced`'s stated purpose — "the same 3-tier machinery tuned for quality/speed
rather than XG parity" — is not what it delivers at 0.08/0.02: it is priced like
`world_class_fast` without matching it. Either retune it genuinely cheap (back
toward 0.04, keeping escalation) or retire it. **Note the MAE figures predate
the 19 Sep retune of both presets** and need re-running before a final decision.

## 5. The auto jobs/threads split: one defect stands, one claim withdrawn

> **Withdrawn.** An earlier draft of this section led with "the auto split
> starves its own dominant axis," on the strength of a full 18-point sweep of
> the M3 in which serial beat **every** parallel configuration and the shipped
> `4 x 2` was 38% slower than doing nothing. That sweep analyzed *one*
> 140-move match. Re-run on a 459-move match, the same three points reverse
> and the shipped default comes out **fastest**. The claim was an artifact of
> the benchmark's unit and is retracted. What follows separates what survives
> from what does not.

### 5.1 The structural defect (stands)

`analyze_ogxm` sizes its pool as `max(1, cpu // 2)` worker processes x
`max(2, cpu // 4)` engine threads. The comment at `match.py:359` says this
"DELIBERATELY OVERSUBSCRIBES, to ~3x the core count," and argues for it at
length. The product:

| cpu | split | slots | vs cpu |
|---|---|---|---|
| 24 | 12 x 6 | 72 | **3.0x** |
| 8 | 4 x 2 | 8 | **1.0x** |
| 6 | 3 x 2 | 6 | **1.0x** |
| 4 | 2 x 2 | 4 | **1.0x** |

**The 3x overcommit is unreachable at or below 8 cores.** The `max(2, ...)`
floor binds — at exactly 8 cores `8 // 4` is already 2 — so the product
collapses to `(cpu // 2) * 2 = cpu`. A laptop gets none of the behaviour the
comment argues for. The code half-admits this ("Only the 24-core point is
measured... degrade to roughly the old commitment on small machines"), but that
reads like a ratio shifting when the feature actually switches off. This is a
documentation-vs-behaviour gap regardless of which split turns out to be
fastest, and it stands.

> **And tune it on a preset with a rollout tier.** `match.py:369` records that
> 12 x 6 was chosen against `fast` (1.22x) and `deep` (1.12x) — both
> single-pass and rollout-free, so Axis A had almost nothing to do in either.
> Engine threads are exactly what a rollout tier uses. The shipped default was
> tuned on workloads that cannot exercise the axis it is choosing.

### 5.2 The Studio grid (stands, with a caveat)

`world_class_fast`, one 140-move match, best of two passes, each point in its
own process:

```
  jobs   1 x threads 0  serial                      39.47s
  jobs   6 x threads 1    6 slots ( 0.2x cpu)      101.45s
  jobs   6 x threads 6   36 slots ( 1.5x cpu)       31.05s
  jobs  12 x threads 1   12 slots ( 0.5x cpu)       69.72s
  jobs  12 x threads 6   72 slots ( 3.0x cpu)       30.80s  <-- shipped auto
  jobs  24 x threads 1   24 slots ( 1.0x cpu)       50.45s
  jobs  72 x threads 1   72 slots ( 3.0x cpu)       48.53s
```

**Read "serial" carefully: it is not single-core.** `threads=0` resolves to
hardware concurrency, so the serial row is one process using *all 24 cores* via
Axis A — and it is the single most efficient per-decision setting available
(§5.4).

**Threads dominate jobs at every fixed slot count**, by up to 1.6x:

| total slots | threads-heavy | middle | jobs-heavy |
|---|---|---|---|
| 24 | 6 x 4 — **34.0 s** | 12 x 2 — 41.2 s | 24 x 1 — 50.5 s |
| 48 | 12 x 4 — **32.8 s** | 24 x 2 — 36.1 s | 48 x 1 — 48.2 s |
| 72 | 12 x 6 — **30.8 s** | 24 x 3 — 33.4 s | 72 x 1 — 48.5 s |

And `jobs` saturates early: 6 x 6 (31.05 s) is within 1% of 12 x 6 (30.80 s).
Past ~6 workers extra processes buy nothing while extra threads still do.

This ordering is *among parallel points* and is reproduced on the M3 grid
(`2 x 4` and `2 x 6` beat `4 x 2`, `4 x 4` and `8 x 1`), so it is the part
least likely to be a one-match artifact. **The caveat:** it too was measured on
a single match, and the serial baseline row inside it is exactly the comparison
§5.3 shows to be match-dependent. Treat "threads beat jobs" as well-supported
and any absolute serial-vs-auto number from this grid as provisional.

### 5.3 Why one match cannot set the default

The M3, `world_class_fast`, best of two passes, same three points on two
matches from the same corpus:

| | serial (1 x 8) | `2 x 4` | `4 x 2` (shipped auto) |
|---|---|---|---|
| 140-move match | **92.96 s** | 118.38 s (+27%) | 127.92 s (+38%) |
| 459-move match | 331.77 s | 326.33 s (−1.6%) | **320.43 s (−3.4%)** |

The shipped default is the worst of the three on one match and the best on the
other. Two candidate explanations were tested and **both refuted**:

- **Per-worker startup.** Refuted by direct measurement: four workers reach
  ready in **0.50 s** total (import 0.01 s, analyzer construction 0.15–0.22 s,
  first eval 0.22–0.28 s each). The fixed-overhead model that would explain the
  sign flip needs roughly 50 s. It is not startup.
- **Chunk imbalance.** Refuted by reading the code: `match.py:519` submits one
  future *per decision* and collects with `as_completed`. Dispatch is already
  dynamic at the finest granularity available.

What remains is the mechanism `match.py:283` already names — **Axis B cannot
split a single decision.** Cost concentration, measured by timestamping the
per-decision `on_progress` callback:

| | 140-move match | 459-move match |
|---|---|---|
| decisions | 203 | 733 |
| max single decision | 8.75 s = **8.6%** of total | 11.00 s = **3.3%** |
| top 10 decisions | **54.0%** of total | **26.6%** |
| top 25 decisions | 87.8% | 51.0% |
| decisions over 1 s | 11.3%, carrying 86.2% | 11.6%, carrying 87.0% |

Both matches put ~87% of their cost in ~11% of their decisions — the shape is
the same — but the short match concentrates it twice as tightly. Four workers
can balance 85 heavy items; they cannot balance 23, and every one they cannot
overlap is a decision running on 2 engine threads instead of 8.

**This is consistent, not proven.** An LPT bound from these costs puts an ideal
4-worker time at `max(total/4, max_decision)` = 25.4 s for the short match,
against 127.92 s measured — both matches sit far from their bound, so
concentration is a contributing factor and not the whole account. Settling it
needs per-decision costs measured *under* each configuration, which has not
been done.

### 5.4 What `threads` actually controls, and why `4 x 0` cannot be asked for

**`threads=0` means "all of them", not "none".** bgsage resolves it at
construction to hardware concurrency: an analyzer built with
`parallel_threads=0` reads back `ctx.threads == 24` on the Studio. Every
"serial" row in this note is therefore one process using the whole machine.

Measured per decision on one 40-move checker play, Studio, cores used =
CPU time / wall time:

| level | `threads=0` | `threads=1` | `threads=2` | `threads=8` |
|---|---|---|---|---|
| `3ply` | **0.096 s** (7.3 cores) | 0.352 s (1.9) | 0.353 s (1.9) | 0.117 s (5.9) |
| `4ply` | **1.680 s** (10.5) | 8.656 s (2.0) | 8.641 s (2.0) | 2.800 s (6.2) |
| `truncated2` | **1.090 s** (17.2) | 14.605 s (1.0) | 7.506 s (2.0) | 2.214 s (6.9) |

`threads=0` is **3.7x faster than `threads=2` at 3-ply, 5.1x at 4-ply and 6.9x
at `truncated2`**, and still beats an explicit `threads=8` because on 24 cores
it resolves to 24.

The mechanism is in `checker_eval`: each candidate is elevated with a
**hardcoded `n_threads=1`**, and parallelism comes from running candidates
concurrently — `ThreadPoolExecutor(max_workers=min(ctx.threads, len(chosen)))`.
So `ctx.threads` decides how many of the ~14 elevations overlap, and nothing
else. `truncated2` never reaches this path (`_screen_context` returns `None` at
a rollout level) and gets bgsage's own pool instead, which is why it alone
scales past 17 cores.

**This is what §5.3 is about.** The auto formula's `max(2, cpu // 4)` sets each
worker to the *worst* point on that curve. On an 8-core laptop `4 x 2` gives
each worker two concurrent elevations — 3.7x slower per decision than one
worker with the whole machine — and then runs four of them. The two effects
very nearly cancel, which is exactly what §5.3 and the corpus sweep observe.

**And the best-looking setting cannot be requested.** `match.py:404` is:

```python
worker_threads = threads if threads > 0 else max(2, cpu // 4)
```

`0` is the sentinel for "auto", so `--jobs 4 --threads 0` silently becomes
`4 x 2`. There is no way to say "four workers, each using the whole machine."
On an 8-core box the value `8` happens to be equivalent, so `N x 0` is testable
there by accident; on the Studio it is not expressible at all. Whether it is
*worth* expressing is a separate question — the `jobs=4` family was flat from
`4 x 2` (127.92 s) to `4 x 6` (128.49 s), which argues it would not help — but
a sentinel that quietly redirects to the opposite end of a 3.7x curve is worth
a comment at minimum.

### 5.4a The stated reason Axis B exists does not hold for this call

`analyze_ogxm`'s docstring justifies worker *processes* like this:

> Processes, not threads, because bgsage's evaluation is a C++ extension that
> holds the GIL -- threads plateau at ~2.8x, separate processes scale past it.

Measured directly: 32 single-threaded 3-ply `cubeful_probs_and_equity_nply`
calls — the exact call `checker_eval._elevate` makes — through a plain Python
`ThreadPoolExecutor` on the Studio:

| pool threads | wall | speedup | cores used |
|---|---|---|---|
| 1 | 0.999 s | 1.00x | 1.00 |
| 2 | 0.520 s | 1.92x | 1.99 |
| 4 | 0.304 s | 3.29x | 3.47 |
| 8 | 0.194 s | **5.14x** | 5.43 |
| 16 | 0.140 s | 7.15x | 7.65 |
| 24 | 0.129 s | **7.77x** | 9.47 |

**`bgbot_cpp` releases the GIL.** Threads reach 5.14x at 8 and 7.77x at 24, not
2.8x. (The tail is flattened by batch granularity — 32 items across 24 threads
is two uneven waves — so the 24-thread point understates the ceiling if
anything.)

**Scope the refutation honestly.** This contradicts the docstring *for this call
on bgsage 2.0*. The original ~2.8x may have been measured on a different path:
the docstring elsewhere reports 1-ply evaluation at 0.98–1.00x from 1 to 24
threads, and the per-roll luck sweep is all 1-ply and ~29% of a `fast` analysis.
A claim true of the luck sweep was likely generalised to evaluation as a whole.

It matters because it is the stated premise for the entire two-axis design. But
threads scaling 5x does **not** make Axis B unnecessary, and §5.6 shows why: the
inner pool is capped by the candidate count (~15 moves) and draws ~7.3 effective
cores, so one process saturates 8 cores and leaves 24 two-thirds idle. Threads
and processes are not substitutes — threads fill a decision, processes fill a
machine. The docstring's *reasoning* is wrong; its *conclusion* survives on
large machines and is unnecessary on small ones.

### 5.5 What the default should be: leave it alone

The corpus sweep — the experiment the single-match sweeps could not substitute
for. M3, `world_class_fast`, **all 6 matches**, one pass each:

| point | wall | vs serial |
|---|---|---|
| `1 x 0` serial | 1072.84 s | — |
| `2 x 4` | 1074.14 s | +0.1% |
| `4 x 2` **(shipped auto)** | 1058.97 s | −1.3% |
| `2 x 6` | **1051.75 s** | **−2.0%** |

**A 2.1% spread across the entire grid.** The same four-way comparison on one
140-move match spanned 38%, and on one 459-move match spanned 3.4% in the
opposite direction. Averaged over a realistic mix they cancel to nearly
nothing.

`2 x 6` wins, in the threads-heavy direction §5.2 and §5.4 both predict, and
the shipped `4 x 2` is 1.3% off it. **That is not a margin worth changing a
default for**, and it retires the question: the auto formula is not costing
users anything measurable, whatever its docstring says.

What remains actionable from this section is documentation and expressiveness,
not performance:

- the `max(2, ...)` floor makes the docstring's "~3x the core count" false at
  or below 8 cores (§5.1) — fix the comment;
- `0` as the auto sentinel makes the best per-decision setting unrequestable
  (§5.4) — a different sentinel costs one line;
- the GIL justification for Axis B does not hold for the `nply` call (§5.4a) —
  fix the reasoning, whether or not the design changes.

The design itself is vindicated by being unimportant: with threads scaling ~5x
inside one process and decisions spreading across workers, there are two routes
to the same throughput, and picking badly between them costs ~2%.

## 5.6 A worker draws ~6-7 cores, and everything follows from that

`checker_eval` elevates ~15 candidates single-threaded and overlaps them, so a
worker at `threads=0` draws ~7.3 effective cores on an isolated decision and
~6 under contention. That per-decision ceiling explains every result in §5.

Whole corpus, `world_class_fast`. `N x 0` means each worker gets the whole
machine (`threads=24` on the Studio, `8` on the M3 — the values `0` resolves
to). **The M3 column is drift-corrected; see below.**

| Studio (24c, mean of 2) | wall | vs best | | M3 (8c, corrected) | wall | vs best |
|---|---|---|---|---|---|---|
| **`4 x 0`** | **234.97 s** | **best** | | `2 x 6` | **1029.8 s** | best |
| `12 x 6` (shipped) | 237.04 s | +0.9% | | **`2 x 0`** | **1034.0 s** | **+0.4%** |
| `6 x 6` | 237.68 s | +1.2% | | `4 x 0` | 1041.7 s | +1.2% |
| `3 x 0` | 240.39 s | +2.3% | | `4 x 2` (shipped) | 1043.2 s | +1.3% |
| `2 x 0` | 264.03 s | +12.4% | | `2 x 4` | 1064.7 s | +3.4% |
| `1 x 0` serial | 338.26 s | **+44.0%** | | `1 x 0` serial | 1069.7 s | **+3.9%** |

Read the serial rows together: **3.9% off optimal on 8 cores, 44% off on 24.**
One decision fills an 8-core machine and leaves a 24-core machine two-thirds
idle. The `N x 0` columns trace the ceiling from both sides — the Studio needs
4 workers and is flat from 4 to 12; the M3 peaks at 2 and is already worse at 4.

**Threads and processes are not substitutes.** Threads fill a *decision* and cap
out at the candidate count; processes fill a *machine*. That is the correct
statement of the two-axis design, and not what `match.py`'s docstring says
(§5.4a).

#### Calibration: the M3 drifts 2%/hour, the Studio does not

The M3 Air is fanless, and every point above was a single pass run
back-to-back over ~2.5 hours. Repeating two points at the end exposed it:

| point | first | repeat | raw change |
|---|---|---|---|
| `1 x 0` | 1072.8 s | 1108.9 s | +3.4% |
| `4 x 2` | 1059.0 s | 1092.0 s | +3.1% |

Both pairs imply the same rate — 0.337 and 0.369 s/min — so the slowdown is
**linear and monotonic, not random**. Correcting every point to a common t=0 at
0.353 s/min (2.0%/hour) reconciles the two repeat pairs to **0.16% and 0.14%**,
which is what makes the corrected column above trustworthy.

Two consequences worth stating plainly:

- **Measurement precision here is ~0.15%, not ~3%.** An earlier draft called
  the 0.4-1.3% margins "a tie, decide on tiebreakers." That was wrong: the raw
  spread was contamination, not noise, and the margins are resolvable.
- **The drift ran against the winner.** `2 x 6` was measured last in the sweep,
  under the worst thermal conditions, and won anyway.

The Studio, fan-cooled, shows no such effect — repeats of 236.39 → 237.69 and
235.51 → 234.42, ±0.5% in opposite directions. Its two pairs also do not
overlap: both `4 x 0` runs fall below both `12 x 6` runs.

**This is the general lesson of the day, twice over.** applepi was disqualified
for a 41% swing (§8); the M3 needed correction for 2%/hour. A sequential sweep
on a thermally unstable machine has a systematic bias toward whatever ran
first, and only a repeat point can reveal it. Every future sweep should end by
re-running its first point.

### The formula this implies

```python
n_jobs = max(2, (cpu + 6) // 7)    # ceil(cpu / 7), floor of 2
threads = 0
```

2 workers at 1-14 cores, 3 at 15-21, 4 at 22-28. The ceiling is
deliberate: a worker does not cleanly occupy its ~7 cores, because the ~15
elevations finish unevenly and the pool drains to a tail at the end of every
decision. Rounding up lets the next worker's decision fill that tail. This is
the argument `match.py`'s existing comment gestures at ("overcommitting fills
that gap") but makes about 1-ply luck sweeps rather than about elevation tails.

**The floor of 2 is measured, not defensive.** An earlier version of this
formula had no floor and picked a single worker below 8 cores. On the 4-core
`imac`, whole corpus:

| point | run 1 | run 2 | vs best |
|---|---|---|---|
| `2 x 0` (`2 x 4`) | **1429.67 s** | **1429.62 s** | — |
| `2 x 2` | 1449.88 s | — | +1.4% |
| `1 x 0` serial | 1491.25 s | 1542.74 s | **+4.1% / +7.9%** |

Two workers beat one by 4.1%, with no overlap between the pairs. The tail
argument generalises: ~15 elevations of uneven cost never drain evenly, so a
second decision in flight is worth having *at any core count* — even on 4
cores, where two workers oversubscribe 3x.

**And oversubscription buys stability, not just throughput.** The `2 x 0`
repeat reproduced to **45 ms on 1430 s (0.003%)** while serial varied 3.45%
between runs. The `imac` runs a live desktop — Brave, Spotify, ~960 MB
swapped — so cores are intermittently stolen. With more runnable threads than
cores the analysis absorbs that; with one process the interference lands on the
elevation tail, which is the critical path. A user's machine is never idle, so
this is the realistic case, and it argues for the floor independently of the
4.1%.

**It lands on or next to the optimum at every point measured** — `4 x 0` is the best point
tested on the Studio; `2 x 0` is +0.4% on the M3 and the best of its `N x 0`
family. Alternatives considered and rejected: `round(cpu/6)` picks 1 worker at
8 cores (+3.9%); `round(cpu/4)` picks 6 at 24 (+1.2%).

**Against the shipped formula it wins ~0.9% at both ends** — 234.97 vs 237.04 s
on the Studio, 1034.0 vs 1043.2 s on the M3 (corrected). Replicated on two
machines, same direction, same magnitude, above a ~0.15-0.5% noise floor. That
is a small but real win, not the tie an earlier draft claimed.

The speed case is thin on its own; the supporting arguments are what make it
worth doing:

- **memory** — it spawns 2 workers where the shipped formula spawns 12 on a
  24-core box; each loads bgsage's nets. (Below 8 cores both spawn 2, so there
  is no memory argument at the small end — an earlier draft claimed one, on the
  strength of a `1 x 0` default the `imac` then refuted.)
- **stability under real load** — the oversubscribed configuration reproduced
  to 0.003% on a machine with a browser running, against serial's 3.45%;
- **it has a physical referent** — `cpu / 7` is the measured width of a
  decision. `max(2, cpu // 4)` is not derived from anything, and its `max(2,
  ...)` floor is what makes the docstring false below 8 cores (§5.1).

**Measured at 4, 8 and 24 cores.** Nothing between 9 and 21 cores has been
tested, and the 15-core step from 2 workers to 3 is interpolation.

**Not yet measured:** the 4-core end (`imac`, where the formula picks `1 x 0`)
and any machine between 9 and 21 cores.

## 6. The lever that matters is not parallelism

On an M3 Air, per ply over the whole corpus:

| lever | worth |
|---|---|
| jobs/threads tuning | **≤ ~1.4x**, and match-dependent in sign (§5.3) |
| `world_class_fast` instead of `world_class` | **2.24x** |
| `fast` instead of `world_class_fast` | **19.5x** |

The preset tiers are one to two orders of magnitude more consequential than the
parallelism knobs, and unlike the knobs their effect does not change sign with
the match. If laptop wall clock is the goal, that is where to spend the effort
— and it is why §5 being unresolved does not block anything.

The bound on the first row is deliberately loose: the widest spread seen
between any two configurations on a single match was 1.38x, and on the corpus
it is expected to be smaller. It is an upper bound on the prize, not an
estimate of it.

## 7. Method notes

**Short matches produce a qualitatively wrong answer.** The first pass of this
benchmark used 41–64 move matches and showed `fast` as essentially
machine-independent (2.21 s Studio vs 2.11 s M3). That was net-loading
amortisation: a fixed per-match cost spread over few decisions pushes every
machine's ms/decision toward a common floor. On the wide corpus the same
comparison is 9.6 ms vs 31.4 ms — a 3.3x spread. **Any per-decision cost
measured on short matches is suspect.**

**`--jobs` and `analyze_ogxm(jobs=)` have different defaults.** The CLI flag
defaults to `0` (auto); the function signature defaults to `1` (serial). A
benchmark that calls the function without passing `jobs` measures the serial
path while believing it measures the user path. This invalidated a full run
here before it was caught.

**`jobs != 1` requires a `__main__` guard in the caller.** Worker processes are
spawned, so each child re-imports the calling module; without the guard every
child re-runs the caller's top level and the failure surfaces as
`BrokenProcessPool`, naming nothing relevant. Now documented in the
`analyze_ogxm` docstring.

**Sweep each grid point in its own process.** Running 18 points in one
interpreter broke the fifth pool outright, and would have let earlier points'
memory pressure contaminate later points' timings either way.

> **Possible library defect, unconfirmed.** That broken pool arrived after ~5
> sequential `analyze_ogxm` calls in one process, each building a fresh
> `ProcessPoolExecutor` whose workers load bgsage's nets (`match.py:180`: "this
> process holds hundreds of megabytes"). A long-lived server calling
> `analyze_ogxm` repeatedly walks the same path. Mechanism not established —
> worth investigating on its own.

**The `analyze_ogxm` docstring was stale**, describing the old 8x3 default long
after 12 x 6 replaced it. Fixed alongside the guard note.

## 8. `applepi` is disqualified as a benchmark machine

It appears struck through above and its numbers are retained only as an example
of *degraded* hardware. It is not a valid measurement of an i5-8500B.

**The direct evidence.** The level sweep was run on applepi twice on 19 Sep —
same script, same fixed seed, therefore the same 40 decisions, same engine,
same machine, nothing else running either time. Run A came at the tail of a
long sustained sweep; run B started after ~30 minutes idle:

| | run A (heat-soaked) | run B (from idle) | swing |
|---|---|---|---|
| 3-ply, median | 0.1727 s | 0.1439 s | 1.20x |
| 4-ply, median | 5.3028 s | 4.3784 s | 1.21x |
| `truncated2`, median | 7.1842 s | 5.3544 s | **1.34x** |
| **whole sample, total** | **581.1 s** | **411.3 s** | **1.41x** |

**Identical work, 41% apart.** No code changed between them. That is the whole
argument: a slow machine is a perfectly good benchmark, an *unrepeatable* one
is not.

**It corrupts ratios too, not just absolutes.** The defence would be that
per-decision *ratios* survive because both levels scale with the clock. Mostly
they do — and then they don't:

| | run A | run B | move |
|---|---|---|---|
| 4-ply vs screen, median ratio | 24.4x | 24.0x | −1.6% |
| `truncated2` vs screen, median ratio | 37.8x | 32.3x | **−14.8%** |
| 4-ply vs screen, total ratio | 25.2x | 26.2x | +4.0% |
| `truncated2` vs screen, total ratio | 38.6x | 40.6x | +5.1% |

The rollout is the level that degrades unevenly, because it is the one that
holds every core busy long enough to heat-soak the package. A 15% swing in the
exact quantity §1 and §2 are *about* is not a usable measurement.

**The mechanism**, sampled live during run B with turbo on (`no_turbo=0`) and
`max_perf_pct=100`, so nothing but heat is limiting it:

```
t+1: mean 2500 MHz   pkg 100 C
t+2: mean 2461 MHz   pkg 100 C
t+3: mean 2584 MHz   pkg 100 C
```

against **2700–2706 MHz at 100 C** recorded on 10 Sep under the same load. Base
clock is 3.0 GHz and rated turbo 4.1, so it runs below base, and the amount by
which it runs below base moves both within a session (heat soak, above) and
across weeks (~8% in nine days). The root cause is settled and is not software
— it hits Tjmax in six seconds with the fan already at its rated maximum and
cool intake air, and dropping RAPL PL1 from 100 W to 65 W changed nothing,
proving it never drew near its limit.

For scale, the effects this study set out to resolve are 1.5% (the
`world_class_fast` cube retune), the 1.28x-vs-1.13x difference in auto's gain
between machines, and a 3–23% `balanced`/`world_class_fast` cost gap. Run A
versus run B is larger than all of them.

**What survives without it.** Every conclusion was re-checked against
`mac_studio` / `macbook` / `imac` alone:

| section | claim | survives? |
|---|---|---|
| §1 | 4-ply cheaper than `truncated2` | yes — 25/40, 33/40, 29/40, and applepi was never the strongest case |
| §1 | 4-ply-vs-screen ratio stable | yes, and *tighter*: 17.1–21.6x instead of 17.1–24.4x |
| §2 | rollout ratio drifts 23x → 40x | yes — the M3's 39.7x is the extreme, not applepi's |
| §3 | published "relative to `fast`" column is fictional | yes — 17.3x vs 19.5x on two sound machines alone |
| §4 | `balanced` is dominated | yes on Studio and M3; `imac` pending |
| §5 | auto split starves its dominant axis | yes — applepi was never used for it |

applepi contributed no load-bearing conclusion, which is the only reason this
note did not have to be withdrawn and re-run. Its remaining value is the A/B
above: a clean measurement of what hosting analysis on thermally marginal
hardware actually costs, and of how badly it misleads if you benchmark on it.

## 9. Open

- **The corpus-wide jobs/threads sweep (M3)** — the one number that would let
  the auto formula be changed. Running at time of writing; §5.4 states what is
  and is not justified until it lands. Single-match sweeps are now known to be
  unable to answer this.
- **Attributing §5.3 properly.** Startup and chunk imbalance are refuted;
  cost concentration is consistent but does not close the gap to the scheduling
  bound. Needs per-decision costs measured *under* each configuration, not just
  serially.
- **The `very_quick` anomaly** — `imac` beats `applepi` at `very_quick` and
  loses everywhere else, and the setup-cost explanation is refuted (0.50 s for
  four workers). Small, but it is an unexplained reversal sitting in §3.
- **Re-run the preset MAE figures.** The 0.4474 / 0.4208 numbers in §4 predate
  the 19 Sep retune of both presets, so the accuracy half of the `balanced`
  argument — now the *load-bearing* half — rests on superseded arms.
- **~~iMac (Ubuntu, Intel)~~ — done.** The host-key mismatch was a stale
  `imac-xubuntu` entry; the machine answers as `nick@imac` with a clean key. It
  is now the weak-machine reference and `applepi` is retired from timing (§8).
- **`world_class` old-vs-new on a weak machine.** The 3-ply + 4-ply-middle-tier
  redesign was justified partly on a 1.88x speedup measured on the Studio over
  three matches. It holds there, but the new shape leans on 4-ply and its
  premium over `world_class_fast` runs 1.76x (Studio) → 2.24x (M3) → **3.16x**
  (applepi). It is now the most machine-sensitive preset of the four. The design
  argument — that it models XG's actual routing rather than a flat depth — is
  unaffected; the cost argument is weaker than `presets.py` currently states.

## 10. Reproducing

Scripts live in the session scratchpad, not the repo. Benchmark hosts and their
isolated environments are recorded in the author's local notes; note all
three remotes default to **fish**, so inline `ssh host '...'` with `$(...)` fails —
pipe a script instead (`ssh host 'bash -s' < script.sh`).

| script | what |
|---|---|
| `levelcost.py <n> <threads> <levels> <files>` | per-decision cost of each eval level on the same sampled decisions |
| `usercost.py <jobs> <presets> <files>` | end-to-end seconds per match; `jobs` explicit, never defaulted |
| `jobsweep.py <preset> <passes> <files>` | jobs x threads grid, one subprocess per point, serial in the grid as baseline |
| `sweeppoint.py` | one grid point; separated so a broken pool cannot end the sweep |
| `startupcost.py <n_workers>` | import + analyzer build + first eval, per worker — the direct test that refuted the startup hypothesis |
| `decdist.py <preset> <files>` | per-decision cost distribution, by timestamping the `on_progress` callback |

Verify engine parity before trusting any cross-machine number:
`importlib.metadata.version("bgsage")` and
`gvanalysis.checker_eval._has_root_routing()`. The macbook's own checkout
carries bgsage **1.3**, a different engine (Stage 9's 19 nets, no `root_board`
routing); the bench environment pins 2.0.20260907 precisely to avoid comparing
across it.
