# Analysis Methodology

This document records the analytical choices made in `gvanalysis/match.py` (the
`gvan-match` CLI).

## Performance Rating (PR)

PR measures average error per decision, scaled to match the XG convention:

```
PR = (sum of equity errors) / (number of counted decisions) × 500
```

A PR of 0 is perfect play. Higher is worse. The ×500 factor is the XG standard.

Equity errors are always non-negative: the difference between the optimal equity and the equity of the action actually taken, floored at zero.

## Checker Play Decisions

### What counts

A checker play is counted toward PR only when **both** conditions hold:

1. There are **≥ 2 legal moves** (forced plays carry no decision).
2. The **top-10 equity spread** (best move equity − 10th-best move equity) is **≥ 0.001**. Positions where all reasonable moves are essentially equal are excluded.

Forced moves (exactly 1 legal move) are evaluated and recorded in the JSON for reference, but do not count toward PR.

### Error measurement

Error = `best_equity − played_equity`, where both are evaluated at the configured eval level. If the played move is not in Sage's move list (board mismatch after notation parsing), the position is silently skipped.

### Two-level upgrade path (`--base-level`)

When `--base-level` is supplied, all positions are first screened cheaply. The full eval level is used only when the cheap screener's top move differs from what was played. This cuts runtime significantly without changing results for obvious plays.

For checker play, upgrade triggers when `cheap_top_move_board ≠ played_board`.

## Cube Decisions

### Doubler

The doubler's error is:

```
error = optimal − actual_equity
optimal = max(ND, min(DT, DP))
actual  = min(DT, DP)  if doubled,  else ND
```

A no-double decision is **not counted** when the position is trivially obvious AND the error is < 0.001. Trivially obvious means any of:

- `|ND − min(DT, DP)| < 0.001` — ND and double are essentially equal.
- `ND − DT > 0.200` — doubling and taking is much worse than not doubling.
- `ND − DP > 0.200` — doubling and passing is much worse than not doubling.
- `ND < −0.900 and DT < −0.900` — position is nearly hopeless for both.

Actual doubles are always counted, regardless of triviality.

The **dead cube** is also excluded pre-filter: if `cube_value ≥ away1` the mover can already win the match at the current stake and the cube has no value, so the entire pre-roll cube decision is skipped.

### Responder

The responder's error is:

```
error = actual_equity − min(DT, DP)
actual = DT  if took,  else DP
```

A take/pass decision is **not counted** when `|DT − DP| < 0.001` (correct response is ambiguous).

### Two-level upgrade path

For cube decisions, upgrade triggers when the cheap screener's optimal action (no-double vs. double, or take vs. pass) differs from what was played.

### No-double before the opening roll

The first player to roll has no cube access yet. The `any_move_made` guard ensures no pre-roll no-double decision is recorded before the first checker move of the game.

## Luck

Luck quantifies how much above (or below) average a player's actual dice were, in equity terms.

### Definition

For each checker roll `(d1, d2)`:

```
preroll_equity  = Σ weight(d,d') × best_equity(d,d') / 36   [over all 21 dice pairs]
postroll_equity = best_equity(d1, d2)
luck            = postroll_equity − preroll_equity
```

Positive luck means the actual dice were above the pre-roll expected value; negative means below.

### Implementation

- All 21 dice pairs are evaluated using a **dedicated 1-ply analyzer**, regardless of the main eval level. Luck does not need deep search — 1-ply captures the structure of which rolls are good and bad.
- Luck is computed for every checker play that has at least one legal move.
- Forced moves (1 legal move) still get a luck value; the roll's luck is real even if there was no decision.

### Convention: own rolls only

Each player's reported luck is the sum of luck values on **their own rolls only**. This gives two independent numbers — P1's luck on P1's turns and P2's luck on P2's turns — which can both be positive, both negative, or any combination.

This differs from XG's convention, where one player's total luck is the negative of the other's (because XG accumulates every roll from both players' perspectives simultaneously). We prefer the own-rolls convention because it communicates strictly more information: two independent quantities rather than one.

## Match Winning Chance (MWC)

For match play, equity values are converted to match winning probability using `bgbot_cpp.eq2mwc`, which implements the standard match equity table lookup. MWC is reported alongside equity on move options and lost-equity entries. For money games (match length 0), MWC fields are omitted.
