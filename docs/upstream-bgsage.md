# Upstream bgsage issues

Defects found in `bgsage` while working on this repo, kept here because bgsage
is a third-party dependency we don't patch. Each entry records what we
measured, what we did about it locally, and what an upstream fix would look
like.

Originally observed against **bgsage 1.3.20260723**; line numbers are
`site-packages/bgsage/analyzer.py` at that version. Re-checked against
**2.0.20260907**, which is what we now depend on.

| # | Issue | Status in 2.0 |
|---|---|---|
| 1 | The cube-aware conversion is uncapped | **Open** — and now deliberate |
| 2 | Non-survivors keep a `1-ply` label | **Fixed upstream** |
| 3 | Both promotion loops skip index 0 | **Fixed upstream** |
| 4 | `_nply_eval` regresses probabilities | **Moot upstream** |

Issue 1 is the one that pays for `gvanalysis/checker_eval.py`, and it is still
there, so the local mitigation stays. Fixed entries are kept rather than
deleted: they are the record of *why* the mitigation looks the way it does, and
`tests/test_screened_checker.py` asserts each fix still holds, so a regression
upstream is caught rather than silently re-absorbed.

Upgrading also turned up an issue of the opposite kind — not a defect, but a
signature change we had to follow. See "Root routing" at the end.

---

## 1. The cube-aware conversion is uncapped

**Severity: performance, large.**

`_MultiPlyAnalyzer.checker_play_analytics` (`:300-331`) does the right thing:
it screens every legal move at 1-ply, keeps a small survivor set (the TINY
filter — `FILTER_THRESHOLD = 0.08`, `FILTER_MAX_MOVES = 5`), and runs the N-ply
tree only on survivors. Non-survivors are appended carrying their 1-ply numbers
and `is_1ply_only: True`.

`_CubefulAnalyzer.checker_play_analytics` (`:850-896`) then discards that
saving. When the inner analyzer is multi-ply, cubeful, and has a cube owner —
i.e. **always**, for 2/3/4-ply cubeful checker play, since `resolve_owner`
never returns None — it maps `_convert_move` over `results`, which is *every*
candidate, not the survivors:

```python
with ThreadPoolExecutor(max_workers=total_threads) as pool:
    converted = list(pool.map(_convert_move, results))
```

Each `_convert_move` is a full `bgbot_cpp.cubeful_probs_and_equity_nply`
traversal. So a position with 393 legal moves runs 393 N-ply searches where 5
were intended.

Measured:

| position | legal moves | survivors | N-ply searches | time (3-ply) |
|---|---|---|---|---|
| hand-built spread, 2-2 | 393 | 5 | 393 | 7.24s |

Across 12 sample matches (1567 checker decisions, 31286 legal moves between
them) bgsage performs 31286 N-ply evaluations. Capping at the survivor set
would be 4477.

The comment above the branch explains *why* the conversion exists — the
cubeless tree lets the opponent pick gammon-greedy moves in match play, so the
displayed probabilities need to come from the cube-aware tree. That reasoning
is sound and applies to moves anyone will read. It does not require converting
moves the filter has already discarded.

**Fix:** restrict the `pool.map` to entries without `is_1ply_only`, or drop the
flagged entries' probabilities rather than recomputing them.

---

## 2. Non-survivors keep a `1-ply` label after being re-evaluated at N-ply

**Severity: correctness, silent. FIXED UPSTREAM in bgsage 2.0.**

The conversion loop now assigns `eval_level` and drops `is_1ply_only`
alongside the numbers — the fix suggested below, near enough verbatim.
`test_screened_checker` asserts bgsage mislabels **0** rows; that check used to
assert the opposite.

Issue 1's conversion overwrites `probs`, `cubeless_equity` and `equity` on
every row, but never `eval_level` (it isn't in the assignment list at `:893`).
A non-survivor therefore ends up carrying genuine N-ply numbers under the
string `"1-ply"`.

This is not a mislabel-only cosmetic problem — the numbers really are full
depth. Verified directly: for the opening 6-5 at 3-ply, every row's five
probabilities were compared against explicit `n_plies=1`, `2` and `3` runs.

```
label    |Δ| vs n=1   |Δ| vs n=2   |Δ| vs n=3
3-ply     0.003730    0.002596    0.000000
1-ply     0.002035    0.001231    0.000000
1-ply     0.008970    0.001618    0.000000
1-ply     0.010625    0.001228    0.000000
```

Every row matches `n=3` exactly; none matches `n=1`. Across three analyzed
matches, 1447 of 2441 displayed alternatives (59%) carried the stale label.

The label is load-bearing for consumers. Our `game_eval` compares the played
move's `eval_level` against the best move's to decide whether the played move
needs re-scoring at the same depth:

```python
if checker_err is not None and played_m_eval_level != moves[0].eval_level:
    post = tier_analyzer.post_move_analytics(...)   # weaker estimator
    checker_err = max(0.0, best_eq - played_eq)
```

When the best move is a fossil-labelled non-survivor and the played move is a
real survivor, the labels differ, the check fires, and a correct error is
replaced by a `post_move_analytics` estimate. Measured on one 262-decision
match: 3 rows carried the fossil label, 1 tripped this branch, and it moved
that decision's error from 0.0169 to 0.0900 — 0.28 PR on the match.

**Fix:** assign `eval_level` alongside the other fields in the conversion loop
(or, with issue 1 fixed, leave non-survivors genuinely at 1-ply, which is what
the label already claims).

---

## 3. Both promotion loops skip index 0

**Severity: correctness, small. FIXED UPSTREAM in bgsage 2.0.**

Both loops now go through `_CubelessBase._first_stale_top_two`, which scans
`results[:2]` — index 0 included. Upstream reached it from the other side, on
their snake benchmark: 312 of 795 cubeless 2-ply picks were moves the filter
had pruned and the search had never evaluated, carrying 55 of that level's 68
PR points.

The rollout promotion loop (`:805`) and the multi-ply one (`:933`) are both
written as:

```python
while len(results) >= 2 and results[1].get("is_1ply_only"):
```

They test `results[1]`, so an `is_1ply_only` entry that sorts to **index 0** is
never promoted — the one position where being under-evaluated matters most,
since it is the move the caller will report as best. The loop also re-sorts
after every promotion and keeps going, so it is not the bounded "check the
runner-up" operation the shape suggests: on the 393-move position it promoted
147 rows.

**Fix:** test `results[0]` as well, or promote by scanning for any flagged
entry ranked above the last unflagged one.

---

## 4. `_nply_eval` regresses probabilities it was called to improve

**Severity: consistency, small. MOOT UPSTREAM in bgsage 2.0.**

Not fixed so much as starved: the conversion loop now re-scores *every*
candidate through the cube-aware tree and clears `is_1ply_only`, so by the time
the promotion loop runs there is nothing stale left for it to promote. bgsage's
own comment calls it a no-op. The cubeless estimator is still in there, so this
entry stays as a note against issue 1 ever being capped without revisiting it.

`_nply_eval` (`:917`) promotes a row using `inner._strategy_nply.evaluate_board`
— the **cubeless** tree — while the rest of the list has just been given
cube-aware probabilities by issue 1's conversion. A promoted row therefore ends
up with worse probabilities than the rows around it, in exactly the dimension
the conversion exists to fix.

Cubeful equity is unaffected: `_cubeful_equity` ignores the `probs` argument
when `_cubeful_ply > 1` and calls `bgbot_cpp.cubeful_equity_nply` instead,
which returns bit-identical values to `cubeful_probs_and_equity_nply["equity"]`
(verified to 7 decimal places across six candidate moves). Only the displayed
probability vector and `cubeless_equity` regress.

**Fix:** promote via `cubeful_probs_and_equity_nply` so promotion and
conversion use one estimator.

---

## Local mitigation

`gvanalysis/checker_eval.py` sidesteps 1–4 for the 2/3/4-ply cubeful checker
path by screening at 1-ply ourselves and elevating only a chosen set, using the
same `cubeful_probs_and_equity_nply` call — so elevated moves match bgsage's
numbers exactly while the tail keeps honest 1-ply numbers and an honest label.
Rollout levels and any bgsage whose internals have moved fall back to
`analyzer.checker_play` unchanged.

End-to-end effect, same matches and preset either side, `GVAN_NO_SCREEN=1`
selecting the baseline arm (idle machine — an early set of these numbers was
taken under CPU contention and understated the gain badly):

| preset | matches | baseline | screened | speed | PR changed |
|---|---|---|---|---|---|
| `fast` | 12 | 106.7s | 75.3s | 1.42x | 2 of 24 player results |
| `deep` | 3 | 31.9s / 32.6s | 24.5s / 24.4s | 1.30x / 1.34x | none |
| `world_class` | 3 | 480.1s | 379.7s | 1.26x | none |

Both PR changes are issue 2 being corrected: −0.283 and +0.124, each a decision
where the fossil label had diverted the played move's equity through
`post_move_analytics`. Every other player result is bit-identical.

Speed is the smaller half of the case. Wall-clock gains are bounded by work this
does not touch — luck alone spends 21 1-ply `checker_play` calls per move — so
the ratios above sit well under the 1.8x reduction in N-ply evaluations.

The measurements above were taken against bgsage 1.3, where issue 2 was live.
Against 2.0 the *speed* case is unchanged — issue 1 is still there, so we still
evaluate ~1.8x fewer moves than bgsage — but the PR case is gone: with the
fossil label fixed upstream, screened and unscreened runs now agree on every
player result we have measured. The mitigation is now a performance measure
that happens to also be a correctness backstop, rather than the other way
round.

---

## Root routing: a signature change, not a defect

**bgsage 2.0.** `cubeful_probs_and_equity_nply` gained a `root_board`
parameter, and `_CubefulAnalyzer._convert_move` passes the decision's pre-move
board to it.

It exists because Stage 11's snake NN is **root-pinned**: the net is chosen
once from the position the search tree is rooted at and held for the whole
tree, where every other net is picked per node. Omitting the argument does not
error and does not warn — it evaluates a different function.

`checker_eval._elevate` mirrors `_convert_move` call-for-call, so it inherited
the omission. Measured on eight seeds from bgsage's own snake benchmark at
3-ply, before the fix:

| | |
|---|---|
| best move disagreed with `analyzer.checker_play` | 5 of 8 |
| worst best-move equity gap | 0.19 |
| worst elevated-row delta | 0.70 |

Containment and massive-backgame seeds were unaffected (both per-node routed),
and so were all five of the ordinary positions in `test_screened_checker` —
which is precisely the hazard: the parity suite passed clean while the screen
was wrong on a whole position family.

**What we do:** pass `root_board=board`, guarded by `_has_root_routing()` — a
one-shot probe of the pybind signature, so a pre-2.0 bgsage (which has no
root-pinned net and would reject the kwarg) still takes the screened path.
`test_screened_checker.check_root_routing` asserts both directions: parity is
exact with routing on, and *not* exact with it suppressed, so the test cannot
pass by the probe quietly returning False forever.

**The general lesson for this file.** Issues 1–4 are bugs in code we call.
This was a change in the *contract* of code we deliberately duplicate. Copying
a call site buys exact parity and costs exactly this: parity holds only while
the signature does, and nothing tells you when it stops. Any bgsage upgrade
should re-read `_CubefulAnalyzer._convert_move` against `_elevate` line by line
before trusting the test suite's green.
