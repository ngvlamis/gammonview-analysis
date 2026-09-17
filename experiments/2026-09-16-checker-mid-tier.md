# Checker mid tier: a borderline play is never re-escalated

Measured 16 Sep 2026 on an **M3 MacBook Air (4P+4E, 8 GB)**, bgsage 2.0.20260907.
Started as a preset speed study; turned into a correctness finding. The speed
numbers are secondary and machine-specific — **the defect is not**.

Full report (tables, charts): https://claude.ai/code/artifact/e5f1f686-da46-484f-bfb7-6a7ef7e9dc85

---

## 1. The defect

`_eval_checker_decision` (`gvanalysis/game_eval.py`) branches:

```python
if borderline (top2_gap <= close_threshold):   -> mid tier,  checker_upgraded = True
elif played != screen best:                    -> second_pass (rollout)
else:                                          -> keep screen
```

Borderline is tested **first**, and once the mid branch fires nothing downstream
reconsiders — the only follow-up is the `post_move_analytics` fallback, which
runs at `tier_analyzer`, i.e. the *mid* analyzer. So a decision classified
borderline is capped at the mid tier **however large the played move's error
turns out to be**.

`top2_gap` is best-vs-second. It says nothing about how far down the list the
player actually went — the top two can be a dead heat while the player played
the eighth move.

**The cube path already does this correctly.** `_eval_cube_decision` computes
`real_error = (doubler_wrong and not marginal(...)) or (resp_wrong and not
marginal(...))` and tests *that* first; only a non-error cube falls through to
the `close` branch. `presets.py` documents the intended rule in exactly those
terms — "errors bigger than `close_threshold` still go to `second_pass`" — which
the checker path does not implement.

### Evidence (2-match corpus, `balanced`, threshold 0.04)

- 92 of 164 checker decisions were borderline
- **12 of those 92** had the played move lose more than 0.04; **7** lost more than 0.08
- 10 of the 12 were settled at 3-ply and never rolled out

| top-2 gap | error played | tier assigned |
|---|---|---|
| 0.0004 | **0.1761** | 3-ply |
| 0.0231 | 0.1448 | 3-ply |
| 0.0036 | 0.1209 | 3-ply |
| 0.0330 | 0.0889 | 3-ply |

Row 1 is the clean statement: top two moves 0.0004 apart, player played neither
and lost 0.176, and the preset concluded "near-tie, no rollout needed." That
error's **size** is what feeds PR.

---

## 2. The prototype (in this branch, default OFF)

`gvanalysis/game_eval.py`, +32 lines, gated on `_MID_ESCALATE` /
`GVAN_MID_ESCALATE=1`. After the mid tier evaluates a borderline play, escalate
to `second_pass` if the played move's error exceeds `close_threshold` — the cube
path's rule applied to checker plays.

Gated so the goldens and shipping presets are untouched while it is evaluated.
**The real fix should be unconditional**; the env var exists only to A/B it.

```bash
GVAN_MID_ESCALATE=1 uv run gvan-match match.gvab --preset balanced
```

Verified: with the flag on, every borderline decision whose error exceeds the
threshold is labelled `2T`, and error sizes shift (0.1761 -> 0.1749) because they
are now rollout-measured rather than 3-ply-measured.

Full suite passes with the flag **off**: 29 passed, 2 skipped, goldens
byte-identical.

---

## 3. What correcting it costs and buys

2-match corpus (336 decisions, 230 scored), serial, 25 s cooldown before every
run. PR gap = mean absolute distance from a `world_class` run over the same
plies, 4 player-match ratings.

| `close_threshold` | shipped time | rollouts | PR gap | corrected time | rollouts | PR gap |
|---|---|---|---|---|---|---|
| 0.02 | 90.8 s | 25 | 0.792 | 148.5 s | 38 | 0.645 |
| **0.04 (ships)** | 43.6 s | 15 | 0.899 | **105.2 s** | 27 | **0.725** |
| 0.08 | 27.1 s | 7 | 0.476 | 65.3 s | 16 | 0.469 |
| 0.16 | 13.3 s | 0 | 0.739 | 24.7 s | 4 | 0.461 |

- **PR moves toward `world_class` at all four widths.** Four ratings cannot size
  that, but 4-for-4 in the expected direction is meaningful, and the mechanism
  is not statistical: these are 0.05–0.20 equity errors previously sized at 3-ply.
- **An escalated rollout costs 5.1 s against 2.1–4.7 s for an ordinary one.**
  Such a decision pays the mid tier *and* the rollout, and a near-tie is the
  dearest rollout there is — when the top moves are near-equal, more candidates
  survive the filter. The defect was suppressing the most expensive rollouts in
  the match, which is why it read as a large speed win.
- **The defect is nearly inert at 0.08** (+0.007 PR) and bites hardest at the
  value that ships (+0.174 at 0.04).

`world_class_fast` is **unaffected** — `mid_pass: {cube: ...}` only, so there is
no checker mid tier and nothing to escalate.

---

## 4. Next steps

1. **Make the escalation unconditional**, drop the env gate, then
   `uv run python tests/regen_golden.py --check` and read the diff before
   rewriting. This is the first change to error *sizing* rather than error
   *detection*, so the diff is worth reading line by line.
2. **Re-tune `close_threshold` against the corrected branch** on the 493-match
   rig behind `docs/PRESET_ACCURACY.md`, not the 4 ratings here. 0.08 looks good
   in both worlds (1.61x faster than 0.04 after correction, PR gap 0.469 vs
   0.725) but the corrected curve is the one that matters, and it is much
   flatter in accuracy than the shipped one.
3. **Fix the prose regardless of the tuning outcome.** `presets.py`'s
   "errors bigger than close_threshold still go to second_pass" describes the
   cube path only; the `CLAUDE.md` `close_threshold` paragraph inherits the same
   claim.
4. Consider whether the escalation test should be `error > close_threshold`
   (what this prototype does, mirroring the cube path) or simply
   `played != mid-tier best`. The former leaves small errors sized at the mid
   tier, which is the documented intent and keeps the cost bounded.

---

## 5. WARNING before regenerating goldens on another machine

`tests/golden/PROVENANCE.json` currently reads **Darwin / arm64, generated
2026-09-13**, and on this M3 Air the two exact comparisons (`test_ogxm_pipeline`
bytes, `test_luck_fill` floats) **ran and passed** — they are not skipping here.

Per `CLAUDE.md`, regenerating on a second machine restamps provenance and
silently moves the gate there, turning this machine's run into skips. Also note
the gate is `system` + `machine` only: **another arm64 Mac matches the stamp**,
so the exact comparisons will run there and may fail on bgsage's
non-bit-reproducibility across chips without the provenance mismatch warning you
would get from a different architecture.

Decide deliberately which machine owns the goldens before running
`regen_golden.py` anywhere.

---

## 6. Reproducing the measurements

`experiments/bench.py` — times `analyze_ogxm` over a corpus and histograms which
tier each decision landed on:

```bash
uv run python experiments/bench.py match1.gvab match2.gvab \
    --preset balanced --jobs 1 --tag mytag --out results.jsonl
```

Corpus used here: XG files from `~/Downloads/xg` converted with
`gvformat.convert_xg` + `write_gvab`. The 2-match timing corpus was
`1U1Le7Mzle5Df3Fe` (5pt, 214 decisions) and `ypWZBXeRPrpq7sTN` (5pt, 122), both
from `OpenGammon/`. The 1-match corpus for the `world_class_fast` sweep was
`ypWZBXeRPrpq7sTN` alone.

**Pace every run.** This Air throttles: the identical serial `balanced` run
repeated back-to-back goes 42.6 → 46.0 → 48.7 → 48.1 → 47.6 s, a +13 % sustained
plateau reached by the third match and fully reset by ~1 min idle. The first
(unpaced) sweep of this study was invalid because of it. A 25 s cooldown before
each run was enough here; on a machine with a fan this may not be needed, but
verify rather than assume.

---

## 7. Machine-specific speed findings (this Air only — do not generalize)

Preset cost, 2-match corpus, serial, as shipped:

| preset | wall clock | ms/decision |
|---|---|---|
| `very_quick` | 5.8 s | 25.3 |
| `fast` | 9.3 s | 40.7 |
| `deep` | 14.3 s | 62.1 |
| `balanced` | 45.2 s | 196.4 |
| `world_class_fast` | 233.5 s | 1015.2 |
| `world_class` | 452.1 s | 1965.7 |

- **`world_class` costs 1.94x `world_class_fast`** and returned the *identical*
  rating on the one match where both were scored against a common reference.
- **Parallelism is preset-dependent here.** `--jobs 4 --threads 2` against
  serial: `very_quick` 2.42x, `fast` 1.70x, `deep` 1.44x, `balanced` 1.02x,
  `world_class_fast` 1.01x. The 1-ply luck sweep cannot use engine threads, so
  worker processes fill that idle time — but its share shrinks as the preset
  gets dearer, and by `balanced` the rollouts already saturate all 8 cores.
  Pass `--jobs 1` for `balanced` and above: same wall clock, ~1.6 GB less RSS.
- The 24-core `jobs = cpu/2` x `threads = cpu/4` tuning multiplies to 3x cores
  only at 24 cores; at 8 it gives 4 x 2 = 1x, so the intended oversubscription
  does not happen here at all.
- **`wcf_midchk` (restoring the 4-ply checker mid tier) ran 12 % *faster* here**
  (ABBA-verified, 67.4/70.3 s vs 60.6/60.8 s) where `presets.py` records it as
  48 % slower — same 65 % rollout-diversion rate, different price. Its accuracy
  still goes the documented way (0.148 from `world_class` vs 0.000). The
  comment's conclusion holds; its cost rationale is hardware-specific. Worth
  re-checking on the other machine.
