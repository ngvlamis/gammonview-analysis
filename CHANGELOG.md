# Changelog

Notable changes to `gvformat`, `gvanalysis` and `@gammonview/gvformat`.

One version number describes all three: a release is tagged `vX.Y.Z` in this
repository, and the npm package takes the same number, skipping releases where
no JavaScript changed. Versions are derived from the tag at build time
(`hatch-vcs`), so nothing here is written by hand.

**Everything below 1.0.0 predates the first public release.** Those entries were
recovered from the annotated tags of the private history, which was squashed
when the repository was opened; they are kept because they record why things
are the way they are — particularly the breaking changes and the measurements
behind several design decisions. Dates are the tag dates.

## 1.0.0 — 2026-09-15

**First public release.** The repository is now open source under MIT, and
`@gammonview/gvformat` publishes to the public npm registry under the same
number. The version is a statement about licence and availability rather than
about the format: nothing in the file layout or the API breaks relative to
0.38.0, and a `.gvab` written by 0.38.0 reads identically here. The entries
below are the changes that landed after it.

**`mat_to_gvab` is now `analyze_match`**

The one-call server path analyzes the match; the old name said neither that nor
the truth about its input, which has never had to be a `.mat` — it takes
anything the codec reads. It sits beside `analyze_file`, which does the same
work and stops one step earlier, at the OGXM document: `analyze_match(p)` is
`write_gvab(analyze_file(p))`. `mat_to_gvab` remains exported and is the same
function object, so existing callers are unaffected.

**`gvan-match --gvab` writes the binary instead of the JSON** *(breaking)*

It used to write both — `match.gva` *and* `match.gvab` — unless an `-o` path was
given, in which case it wrote only the `.gvab`. The flag now means the same
thing in both cases, and the same thing it already meant in `gvan-batch`: one
file out, the compact binary rather than the `.gva`. `--binary` is accepted as
an alias, as it is there.

Nothing is lost by it. `read_gvab` of the `.gvab` *is* the `.gva` — the two
encode one document and the suites check that from both languages — so the old
behaviour wrote the same match twice. Scripts that relied on the `.gva` landing
alongside should drop the flag, or read the binary.

**The analysis names its producer, not just its engine**

`analysis_info.model_id` was the bare string `bgsage` on every file this pipeline wrote. It now reads `gv-bgsage/<engine version>` — `gv-bgsage/2.0.20260907` today.

Two things were wrong with the old label. It named no build, so a `.gvab` could not say which engine produced its numbers even though an engine upgrade moves them. And it credited bgsage alone, which overstates the engine's share: `gvanalysis/checker_eval.py` decides which moves are evaluated at full depth and the preset's tiers decide that depth, so two files both reading `bgsage` can hold different numbers from the same engine.

It deliberately carries **no gammonview version**. Everything else in `analysis_info` is fixed by the match and the engine — `timestamp` is the match's, not the run's, and `duration_ms` is never set — and that determinism is the only reason `tests/golden/` can pin bytes at all. A `hatch-vcs` version would move daily in a dev checkout (the `.dYYYYMMDD` local segment) and go circular at release, reddening the suite the tag was cut from and leaving the regen commit past the tag. bgsage's version costs nothing by comparison: it changes exactly when an engine upgrade invalidates the goldens anyway. `test_ogxm_pipeline` section 5 asserts both halves of that rule, so adding a version here fails loudly.

`gvformat` stays engine-free and cannot ask bgsage anything, so the label arrives through the summary dict; `gvanalysis.match.model_id()` builds it. The JS mirror follows. All five goldens were regenerated — the only change in any of them is the string, +16 bytes each, with no analysis number moved.

## 0.38.0 — 2026-09-10

**The engine moves to bgsage 2.0**

bgsage 2.0.20260907 relicenses from AGPL-3.0 to MPL-2.0, which matters for hosted analysis: MPL is file-level copyleft, so serving analysis no longer carries AGPL §13's obligation to offer source to users. It attaches only to bgsage's own files if modified, which we don't.

The engine itself is Stage 11 — 24 NNs against Stage 9's 19, with the 17 standard nets carried unchanged and new specialists for backgames, containment, massive backgames and the snake. Ordinary play barely moves: match PR within 0.02 on the sample matches, 14 of 567 golden plies shifted (max 0.0120), no best move changed anywhere. Luck moves further, being 1-ply and routed directly. The gains are concentrated in the families the specialists exist for.

Two of the four defects in docs/upstream-bgsage.md are fixed upstream — the stale "1-ply" eval_level label and the promotion loops skipping index 0 — and a third is starved. Issue 1, the uncapped conversion that pays for gvanalysis/checker_eval.py, remains, so the screen stays; with the label fixed it is now a performance measure that happens to be a correctness backstop rather than the other way round.

The upgrade's one real hazard was a contract change rather than a bug. cubeful_probs_and_equity_nply gained root_board, because Stage 11's snake NN is root-pinned — chosen once from the tree's root position and held throughout, unlike every other net. checker_eval._elevate duplicates that call site and so inherited the omission, which neither errors nor warns; it evaluates a different function. On eight snake seeds the best move disagreed with bgsage on five, by up to 0.19 equity, with elevated rows off by as much as 0.70, while every ordinary position in the parity suite stayed exact. Fixed, signature- probed so pre-2.0 engines still work, and asserted in both directions so the test cannot pass by the probe silently failing.

Also: gvan-position gains a rollout progress bar covering bgsage's finalizing phase, which had been invisible and is often most of the run; ProgressBar moves to gvanalysis/progress.py; and tests/regen_golden.py makes the next engine bump a command rather than a chore.

No gvformat changes, so the JS line skips this release at 0.36.0.

## 0.37.0 — 2026-09-10

**Checker plays are screened before they are evaluated**

Match analysis no longer lets bgsage convert every legal move at full depth. gvanalysis screens the move list itself at 1-ply and elevates only the survivors, which fixes the stale "1-ply" eval_level label that made game_eval re-score a played move through a root-Janowski estimate instead of its tree equity — PR shifted 0.28 and 0.12 on two of twelve sample matches.

Faster as a side effect, most where full-width n-ply work dominates: measured over three matches, world_class 1.88x and fast 1.74x, against 1.1x for the rollout-heavy balanced and world_class_fast.

Also carries the XG importer fix from v0.36.0 line: a zero win probability is an evaluation, not a missing one.

## 0.36.0 — 2026-09-09

**A lost position keeps its probabilities**

XG's converter no longer reads a zero win probability as a missing evaluation, so plays in hopelessly lost positions carry their gammon/ backgammon numbers instead of a blank row.

## 0.35.0 — 2026-09-04

**Blending analysis blocks is not the codec's call**

Backs out `convert_ogxm` / `convertOgxm` and its wiring. The fold was right about the file and wrong about the layer: it decided, inside the read, that a producer's two analysis blocks are really one, and rewrote the document to say so before anything downstream got a look.

That is a policy, and it belongs to the view. Which analysis to rate, and at what depth, is the user's choice through the picker; a converter that collapses the choice on the way in removes it for everyone and cannot be undone from above -- the second block is gone by the time the frontend sees the match. Nothing here needed the document rewritten to offer the merged reading, so the cost bought nothing.

The arrangement itself is real and still worth knowing about, so it is recorded in the GammonView frontend's own notes as a view-layer question rather than deleted along with the code. The history has the fold if it is ever wanted behind a control.

Kept: basefill reading a block's depth from the ANAL header when no entry carries one. That is not about blending -- the header's `ply` and a per-decision `ply` are alternatives, and a block spelling it the second way otherwise reads back with no level at all.

Suites green: Python basefill 24/24, read_gvab 65/65, writer 89/89; all JS suites pass.

## 0.34.1 — 2026-09-04

**State the ordering the fold depends on**

`convertOgxm` decides whether a deeper block re-evaluated a record by asking whether its numbers moved, so both sides of that comparison have to be in the same unit. They are, because `basefill` converts a foreign block's cube values out of the base spec's MWC on the way in and runs inside the read -- but nothing in `ogxm2gva` calls it, so the dependency was invisible from either end.

Recorded at both: the fold says it must run after the completion pass and why, and the completion pass says it has a dependent. The failure mode is the reason it is worth six lines -- moving `basefill` out of the reader would not break the fold, it would quietly produce a document half in MWC and half in equity.

Comments only; no behaviour changes, and the suites are unmoved.

## 0.34.0 — 2026-09-04

**Fold another producer's upgrade block into one analysis**

The base spec offers two ways to record that one decision was searched deeper than its block (OGXM_FORMAT_SPEC.md, ANAL): stamp the decision's own `ply` in place, or append a second block holding just the re-run decisions. We write the first. HedgeHog writes the second.

A real file arrives as a 242-decision block at stored depth 2 and a 24-decision block at stored depth 3 -- the second a strict subset of the first, same model, no new decisions. Left alone that is two analyses, and everything downstream has to treat them as rivals: the viewer offers both, and the second rates the match over 24 plies that are precisely the ones somebody doubted (PR 12.07/26.18 against the real 3.46/7.29). Worse, the primary -- what every default reads -- is the *shallower* of the two. On that file it calls five plies blunders that the deeper re-run scores as the best move available.

So `convert_ogxm` folds a block that is a strict subset of another, from the same model, searched deeper. Three conditions, each ruling out a case the others allow; two rival engines must never blend, because a rating half from each describes neither.

Per decision, a sub-record is replaced only when its numbers actually moved, and then it carries the deeper block's `ply`. That is not defensive throat-clearing. HedgeHog's upgrade re-runs the checker play and carries the ply's cube record along beside it: across the 13 cube records in that file, all 39 stored equities and every equity_loss are bit-identical to the shallower block's, and only the probabilities move. Taking that record and stamping the deeper level on it would claim a depth the cube decision never got, for numbers that did not change.

Two smaller things fall out:

* basefill now reads the block's depth from the ANAL header when no entry carries one. The header's `ply` and a per-decision `ply` are alternatives, not a pair -- 0 means "use the header" -- and this file uses both spellings, one per block. Without it the deeper block reads back with no level at all. * The fold drops a per-decision `ply` that only repeats the header. Ours is written only where it exceeds the base; HedgeHog stamps every entry, so a viewer badging depth would badge all 242 plies and single out none.

Depths above are the stored field throughout. HedgeHog's own interface calls those two blocks 3-ply and 4-ply where bgsage and XG would say 2-ply and 3-ply; an offset shared by every block leaves "which is deeper" untouched, so nothing here depends on it.

Mirrored in gvformat-js as ogxm2gva.js, with the same 23 checks both sides. Full suites green: 340 JS, and the codec's Python scripts.

## 0.33.0 — 2026-09-04

**A missed double reads with its decision block**

A minor for the same reason 0.32.0 was: this changes the document a reader produces from bytes it already accepted. A missed-double ply now comes back carrying `cube_decision` as well as `missed_double`, and the response label's tie-break moves to the spec's strict one. Neither changes a stored byte, and a consumer pinning "^0.32.0" would inherit both on the next plain `npm install` with nobody deciding to.

## 0.32.0 — 2026-09-04

**Reading somebody else's OGXM**

A minor rather than a patch, for the reason 0.31.0 was: this changes the document a reader produces from bytes it already accepted. A .gvab written by another producer now comes back with its decision flags set, its cube values on the normalized scale, and a `_base_analyses` key that was not there before -- a consumer pinning "^0.31.0" would inherit all of that on the next plain `npm install` with nobody deciding to. Outside that range, the bump is a decision.

Nothing changes for a file we wrote: those carry a GVAN, and a block with a GVAN is read exactly as it was before.

## 0.31.0 — 2026-09-02

**A resignation belongs to the player who resigned**

Terminal plies 27/28 now carry the resigner's colour and on-roll instead of the winner's, across all six writers (.xg/.bgf/.mat, JS and Python).

## 0.30.0 — 2026-08-29

**Canonical XG eval levels, so a saved match keeps them**

## 0.29.1 — 2026-08-26

**Honour min_reader when reading a .gvab**

## 0.29.0 — 2026-08-26

**Align gvformat with the 2026-08 upstream sync**

## 0.28.0 — 2026-08-21

**Split an alternative's two-die move around a made point**

## 0.26.0 — 2026-08-16

**Keep an illegal play's checkers where the source put them**

XG records plays that broke the rules (invalid_m == 2; sites do let them through), and one of them can use more die-moves than the roll has: 13/9 with a 3-1, then 12/11, is three hops for a two-hop roll. The step expansion followed the dice, so it needed three steps -- and a ply record holds exactly the roll's hops, so write_gvab dropped the third without a word. Every board replayed after that ply was one checker off; six plies later a recorded move lifted a checker off a point that no longer had one, _apply_moves_p1 decremented anyway, and the mover ended up with sixteen checkers. bgsage then indexed off the end of its bearoff table and the worker process died -- which reached the client as "a process in the process pool was terminated abruptly", naming nothing.

Record each checker of such a play in one step at its own pip distance instead. The dice cannot explain those hops anyway, and two steps replay to exactly the board XG recorded. The played alternative gets the same treatment so it still describes the ply's own `moves`. Where even that overflows, the ply is written as a set-position ply -- what action 31 is for, and what the spec's optional dice on it already anticipated.

Three guards behind it, because a truncated play should never have been able to travel this far:

* write_gvab now raises on a ply with more steps than its action id can hold, instead of keeping the first n. * ogxm_reconstructor applies a set-position ply to its running board. It previously skipped action 31 entirely -- no decision *and* no board -- which would have corrupted a game just as thoroughly. * game_eval checks the faced and played boards against legality's new board_problems before handing either to the engine, and fails the run naming the game, player and roll. Skipping the decision instead would leave a match analyzed with plies quietly missing, on boards that are still wrong where they are not outright impossible.

## 0.25.0 — 2026-08-09

**Allow the truncated rollouts as position eval levels**

The single-position allowlist stopped at 4ply, which left the levels worth having off the menu: 2T and 3T are where the strength above a static eval lives (2T beats XG Roller+, 3T is close to Roller++), and they are still interactive -- 0.7s and 2.1s for a checker play against 1.2s for 4ply, measured on an M-series Mac. That is the line this allowlist is drawing, and truncated1/2/3 sit on the answer-it-inline side of it; a full rollout stays a CLI run someone started deliberately.

Nothing else changes: the level is passed to bgsage's analyzer as given and reported back as `eval_level`. Per-alternative levels stay the engine's own account of how each move was evaluated -- "rollout" for the candidates it kept, "1ply" for the ones left on the screen that ranked them -- which is what a reader needs beside a move it is asked to trust.

## 0.24.0 — 2026-08-09

**Single-position analysis as a library call**

## 0.23.0 — 2026-08-09

**A missed redouble keeps its equity loss through a .gvab write**

## 0.22.0 — 2026-08-09

**Split a two-die move around a point the opponent has made**

BGBlitz records only a move's endpoints, so a single checker playing both dice ("18/7" off a 5-6) leaves the intermediate point to be inferred. The converter used the board-blind splitter, whose tie-break is "larger die first" -- which for that ply routes the checker 18/12/7 through a point holding two enemy checkers, where the legal route is 18/13/7.

That is not a cosmetic mis-description. Replaying a step onto a made point reads as a hit, turning the opponent's two checkers into one of ours plus a bar checker, so the side gains a checker and every board replayed from that ply onward is wrong. Far enough wrong that re-analysis died: gvanalysis rebuilds its running board from `moves`, handed bgsage a position with 16 checkers in a home board, and bgsage indexed its bearoff table out of bounds and segfaulted the worker process -- surfacing to the user as "A process in the process pool was terminated abruptly".

`_split_span_nondouble` already knew how to reject a blocked intermediate; it just needs the board, which `_notation_to_steps` did not forward and the BGF caller did not pass. The mover-relative board is already in scope there for the legality check. Unplayed alternatives still have no board -- they are derived from a notation string alone -- and keep the canonical tie-break, unchanged.

Only the played move's steps move; alternatives are untouched. The XG path was never affected: it derives steps from a board-before/after diff, which cannot pick an illegal route.

Tests assert both levels: that the splitter steps around a made point (but not around a blot, which is a legal landing), and the whole-file invariant board.js already documents -- a ply's `moves` replayed onto its `ogid_before` reproduce its `ogid_after`. The invariant holds across all 12 BGF samples; none happened to contain the shape, which is why this went unnoticed until a real match hit it.

## 0.21.0 — 2026-08-09

**Keep the probabilities BGBlitz records for a ply with no move**

A dance record carries two evaluations, not one. `equity` is pre-roll and holds the cube decision -- you can double even when you cannot move, and BGBlitz scores that -- while `dancingEquity` evaluates the position the non-play leaves behind. The converter read the first and dropped the second, because it looked for candidates in `moveAnalysis` and a dance has none.

So an imported dance came out with a cube decision and no play evaluation, and anything asking "what are the chances here?" had only the pre-roll numbers to answer with: the board before the dice, not the one the opponent is now looking at. This is the third place the same field went missing, after the analyser (which computed nothing for these plies) and a missed double's own probabilities.

`dancingEquity` is fed in as a single played option with no move, so it takes the existing path -- equity from `emg` or the money fallback, probs, ply level, the standard alternatives builder -- and comes out in the shape XG writes for a dance and the analyser now writes too. Nothing else moves: `pr.checkerError` is absent on these records, so the ply stays out of PR exactly as before.

Both mirrors, both test suites, 17 checks each.

## 0.20.0 — 2026-08-09

**Evaluate a ply with no legal move instead of skipping it**

A dance was treated as having nothing to analyze -- true of the *play*, since there is none to judge, but not of the position. The ply went out with an all-zero `eval` and an empty alternatives list, so the only probabilities on it were the cube decision's, and those are pre-roll: they describe the board before the dice, not the one the opponent is now looking at. In GammonView that reads as a panel that fails to update -- switching between Move and Cube on a dance showed the same row twice, the cube's.

bgsage already returns the unchanged board as the single legal "move" here, so a dance evaluates exactly like a forced move and needs no branch of its own: dropping the `no_legal` guard routes it through the existing `len(legal) == 1` path, which emits one played option with an empty notation -- the same shape XG writes for these plies, so the exporter and every reader downstream need no special case. It stays out of PR, because that path never sets `analyzed`.

The guard is kept on the multi-move branch alone: if a recorded no-play somehow has real moves available, that is a broken record rather than a decision to score, and it is left alone as before.

Luck is untouched -- the 21-roll sweep already saw the same no-op move, so the post-roll equity it measured against was never the missing one. Goldens are regenerated; the diff is confined to those plies' `eval`, `best_equity`, `played_equity` and their one new alternative, with no change to any summary, PR, or luck figure. The pipeline test now asserts the field off the goldens directly, so a future blind regeneration cannot drop it silently.

## 0.19.0 — 2026-08-09

**Keep a missed double's probabilities instead of dropping them**

A checker ply carries at most one cube record: `cube_decision` when holding was right, `missed_double` when doubling was. Only the first kept its evaluation. Every producer had the numbers in hand and threw them away on the other branch -- xg.py read XG's no-double probs and attached them under an `if key == "cube_decision"`, og2gva and bgf.py did the same by hand -- and the two that survived that were then dropped by binary.py, which wrote `probs: None` for a type=2 entry, and by reader.py, which never read them back.

So a reader asking "what are the chances here?" over a missed double had only the checker ply's own eval to answer with, and that eval is post-roll: it describes the play made instead of doubling, not the cube decision. GammonView showed exactly that -- switching to the Cube panel on a position the player didn't double left the probability row on the checker play's numbers, looking like the panel had failed to update.

The CUBE entry has its five probability fields whatever its type, so a missed double now stores its pre-roll probabilities there like any other cube record, and its eval_level rides along in GVAN beside them. Zero still means "not recorded", so files written before this read exactly as they did.

This is the second place we write bytes the reference codec leaves zero (the first being an equity_loss above 1.0): its own `missed_double` JSON shape has no `eval`, so `ogxm_json.cpp` has nothing to put there. The byte-parity test now allows that one divergence and nothing else, and both format specs name it. Golden files are regenerated; the only change in them is the new field.

## 0.18.0 — 2026-08-09

**Gvformat-js ships as @gammonview/gvformat from a private registry**

## 0.17.0 — 2026-08-09

**OGID decoding (gvformat.parse_ogid / looks_like_ogid), gvan-position accepts XGID or OGID, parse_xgid cube-owner fix**

## 0.16.0 — 2026-08-08

**V0.16.0**

Separate `event` and `site` into two independent top-level OGXM fields.

The header used to fold them into one " • "-joined string even though every source format parses them apart (.mat's [Event]/[Site], XG's event/location, BGF's event/site). They are now two fields in the JSON and, in the binary, a 4th length-prefixed string appended to MHDR's variable part.

Additive in both directions -- the MHDR strings are self-delimiting, so an old reader stops early and ignores the extra bytes; no min_reader_minor bump. A legacy document carrying one combined string in `event` is split on read, so old .gvab files decode to the same pair new ones do; no file migration needed.

Shared semantics live in gvformat/place.py (PLACE_SEPARATOR, clean_place, split_place, join_place), mirrored in gvformat-js/src/place.js and published as gvformat-js v0.15.0.

Note: a match with [Site] and no [Event] (all the OpenGammon samples) used to surface its value as `event`; it now surfaces as `site`.

## 0.15.0 — 2026-08-07

**V0.15.0**

bgsage 0.8.20260611 -> 1.3.20260723; the played move scored at the full level via force_boards; the opening roll's luck measured against the pre-game MWC; auto parallelism oversubscribed to ~3x the cores.

Numbered 0.15.0 rather than 0.13.0 to clear the gvformat-js mirror's tag series (at 0.14.0), which shares this local tag namespace.

## 0.14.0 — 2026-08-07

**Chore: v0.14.0 — export mwc2eq/scoreMwc, pin the MET against the Python copy**

met gains the two conversions the opening-roll luck baseline needs: mwc2eq, the documented inverse of eq2mwc (equity 0 is the midpoint of this game's win/loss anchors, not the current MWC, so an MWC from outside the ply cannot just be subtracted), and scoreMwc, a score's own pre-game MWC — whose Crawford routing is deliberately not mwcAnchors', since that one resolves the score reached *after* the game. Both are re-exported from index.js beside eq2mwc, so this is new public surface and a minor bump.

Nothing links this file to gvformat's met.py at runtime; they are equal only because they were written to be. test/test-met.js now asserts the literals the Python side prints, and tests/test_met.py there asserts the same ones — 79 checks each: scoreMwc in all three regimes, mwc2eq inverting eq2mwc, and the opening baseline, exactly 0.0 at a symmetric score and the published value elsewhere.

Mirrored from bgsage-analysis ab2473e + 0668de4.

## 0.13.5 — 2026-08-06

**Chore: v0.13.5**

## 0.13.4 — 2026-08-06

**Chore: v0.13.4**

## 0.13.3 — 2026-08-05

**Stop truncating equity losses in toOgxmJson too**

Widening the encoder's bound in 0.13.1 was not enough. export.js caps equity_loss at 1.0 in four places of its own, and it runs *before* the encoder: it is what turns a bgsage result into the OGXM-JSON that then gets written. So a match analysed through this path still had its blunders rounded down to a point of equity, and _enc_equity_loss -- now able to hold 6.5535 -- never saw a value large enough to matter.

The four sites are the two cube branches (missed_double and cube_decision) and both checker branches (the engine's own lost_equity, and the derived best - played for an unanalysed ply). All four now keep the floor at zero and drop the ceiling: the only bound left is the encoder's 6.5535, which sits above anything backgammon can produce, since equity runs [-3, +3] and no single decision can cost more than 6.0.

Nothing covered this layer, so test-equity-loss.js grows a section for it, and _cubeSubAnalysis / _checkerAnalysis join the internals this module already exports for testing.

Mirrors the same fix to gvformat/export.py, where the golden matches confirm the change is inert below the old bound: none of them contains a decision losing more than 0.6147, and all four goldens stay byte-identical.

## 0.13.2 — 2026-08-05

**Count PR decisions by the house rule, not every ply**

og2gva marked every analysed ply `decision: true`, so PR came out low: the denominator swept in forced moves and already-decided positions, which every other converter here excludes. On the reference match that was 10 phantom checker decisions for one player and 9 for the other, dropping their PRs from 24.4 to 22.7 and from 7.8 to 7.2 against the 24.13 and 7.78 opengammon.com shows for the same match.

Cube counting was wrong in both directions at once. A ply where doubling was right emitted `cube_decision` *and* `missed_double`, counting one decision twice -- and the two are mutually exclusive by construction: binary.js drops `cube_decision` when both are set, so a saved match and the one in memory disagreed about its own PR. Meanwhile a close cube the player was right to hold counted for nothing, because `decision` was set to `should_double`, which is false precisely when the player got it right. A decision correctly made is still a decision; that is the whole point of the denominator.

So: checker plies now count on a candidate spread of at least CHECKER_SPREAD_EPS, embedded cubes resolve to exactly one record (`missed_double` when the cube should have turned, else `cube_decision` counted unless trivial), and real cube actions count unless the cube was trivial and played right, or the take/pass branches are indistinguishable. All four rules mirror xg2gva.js and gvanalysis.game_eval.

They are deliberately *not* OpenGammon's `count_xg_decisions`. The house rule is calibrated against XG's own displayed PR, and using it here means an OG import and a re-analysis of the same match agree with each other -- which matters more than agreeing with the site. The two rules pick the same checker plies on the reference match and differ on one cube ply (theirs stops counting when both sides are losing at -0.500, ours at -0.900), leaving 24.40 vs 24.13 and 7.80 vs 7.78. The parity section now pins that, alongside the total error it already reproduced to four decimals.

## 0.13.1 — 2026-08-05

**Stop truncating equity losses above 1.0**

equity_loss is a uint16 at 1e-4, so 6.5535 is representable, but the writer clamped it to 1.0 -- 15% of the range. Any blunder past a point of equity was silently truncated. A wrong take of a large double clears that easily: the case that exposed it cost 1.8096 and was stored as 1.0, dropping that player's match error from 3.85 to 3.05.

The clamp did more than spoil a statistic. played_equity is derived rather than stored (played_equity = best_equity - equity_loss), so a truncated loss moved the played equity too, and the record ended up disagreeing with itself: the alternative flagged is_played still carried the true equity, because alternatives go through _enc_equity, which was never clamped so tightly.

Widening the writer needs no version bump and no min-reader bump. Every reader decodes with a plain divide and none range-checks, so readers that predate this change read the wider values correctly.

Files already written cannot be identified by inspection -- a stored 10000 is either a genuine 1.0 or a truncation -- but the true loss survives elsewhere in both record types and can be recovered without re-analysis: on EVAL entries as best_equity - alternatives[is_played].equity, and on CUBE entries from the unclamped no_double_equity / double_take_equity / double_pass_equity.

The same one-line bound exists in two other implementations of this format, mirrored from the same source; both are being changed alongside this: hedgehog-public/src/match/ogxm_format.hpp (canonical) and bgsage-analysis/gvformat/binary.py. The spec's fixed-point table and the EVAL and CUBE field ranges are updated to match.

## 0.13.0 — 2026-08-05

**> 0.13.0**

## 0.12.0 — 2026-08-06

**V0.12.0**

Equity loss is no longer truncated at 1.0 -- neither by the encoder nor by to_ogxm_json, which capped it upstream and so kept the encoder fix inert on the analysis path. Bound is now the field's own 6.5535.

Also: gvformat-js gains OpenGammon import (og2gva) with house-rule PR decision counting, and multi-analysis read/write via appendAnalysis.

## 0.11.2 — 2026-08-01

**Per-alternative eval on BGF checker plies**

buildAlternatives read a non-existent opt.eq, dropping win/gammon/backgammon eval from every BGF checker-play alternative (GammonView's probabilities panel came up empty on .bgf imports). Reuse the option's precomputed probs vector.

## 0.11.1 — 2026-08-01

**Gvformat 0.11.1: read_gvab guarantees GvabError on malformed input**

Bounds-check the ply decoder and the move applier, and convert any stray exception at the public entry point. Previously a corrupt stream could raise a raw IndexError, or -- worse -- an out-of-range 'from' indexed the board list negatively and silently returned a wrong board.

## 0.11.0 — 2026-08-01

**OGXM-native analysis pipeline**

Analyzer accepts .mat/.gva/.ogxm/.gvab; every input becomes OGXM up front and our analysis is appended as a new block, preserving any the input carried (OGXM allows 16). Multi-analysis read/write in the Python codec.

Breaking: the .mat converter moved to the codec layer. gvanalysis.mat_to_ogxm  -> gvformat.mat_to_ogxm / gvformat.convert_mat gvanalysis.mat_parser   -> gvformat.mat_parser gvanalysis.game_reconstructor -> gvformat.game_reconstructor game_eval.evaluate_game now requires `recon` and no longer takes match_length/p1/p2.

## 0.10.0 — 2026-07-28

**Parallel match analysis (--jobs) + git-derived versioning**

## 0.9.1 — 2026-07-27

**Analyze_mat: on_progress(done, total) callback; total known before engine build**

## 0.9.0 — 2026-07-27

**Self-contained gammonview links + --link/--browser**

Encode a whole match as a compact URL-safe string (.gvab -> zlib-deflate -> base64url) so it rides in the URL fragment with no hosted file.

Layering: the payload codec is site-agnostic and lives in gvformat (share.py / gvformat-js share.js, encode_match/decode_match); the gammonview.com URL wrapper is app-level (gvanalysis/share.py, mirroring gammonview's sharelink.js). gvan-match gains --link (print the URL) and --browser (open it; temp HTML redirect for near-cap-length URLs).

Python zlib.compress and JS pako.deflate emit different bytes but inflate identically; decode is built on inflate, verified cross-language.

## 0.8.1 — 2026-07-27

**Share codec: encode_match/decode_match (match <-> URL-safe base64url string); JS mirror of gvformat/share.py**

## 0.8.0 — 2026-07-27

**Judge a play by its resulting position, not sub-move spelling**

BGBlitz records a checker moved with more than one die as a single net (from, to) pair -- 24/15 for a 6-3, or a two-die bear-off as one (f, off) -- while the legality checker enumerated one sub-move per die. Exact per-die matching therefore flagged these legal moves as illegal (a real 5-point .bgf showed 154 false "illegal move" markers).

Match on the mover's resulting checker positions instead: a play is legal iff it lands the checkers where some maximal legal play would. This is agnostic to how a checker's move was spelled, yet still rejects an under-play (leaving a die unused lands where no maximal play can reach).

Ports the fix to both the JS (gvformat-js, bumped 0.7.0 -> 0.8.0) and Python (gvformat) mirrors, with regression tests for combined pairs, forced two-die bear-offs, and genuine under-plays. Corrects an existing bear-off test whose position ({1,3} with 4-1) is in fact legal via 3/2 2/off.

## 0.7.0 — 2026-07-26

**XG checker decision counting (max-min candidate spread, eps 1e-4)**

## 0.6.0 — 2026-07-26

**BGBlitz-faithful BGF cube counting + money-mode checker error**

## 0.5.0 — 2026-07-26

**Mode-aware BGF luck + luck on no-move plies**

## 0.4.0 — 2026-07-26

**Add total_error_mwc to compute_aggregates (MWC analog of total_error, includes embedded cube errors)**

## 0.3.0 — 2026-07-26

**Fix XG/BGF PR decision counting (forced/trivial checker + cube) and BGF luck; add luck to Python xg.py/bgf.py**

## 0.2.1 — 2026-07-25

**Browser-safe CLI guard + luck on XG/BGF import**

Three fixes found while wiring the GammonView frontend onto the codec.

1. The Node-only CLI entry points in xg2gva.js and bgf2gva.js dereferenced `process.argv` at module scope, so merely *importing* either module in a browser threw "Can't find variable: process" and all XG/BGF import failed. Guard on `typeof process !== "undefined"` before touching it. Bundling never caught this: emitting a chunk is not evaluating it.

2. convert_xg dropped luck. It already parsed XG's ErrLuck (offset 2320) and carried it on the record, but never wrote it to `analysis.luck`, so every XG-imported match reported zero luck and no luck rolls. Emit it, guarding on the NOT_ANALYZED sentinel so a genuine luck of 0.0 survives.

3. convert_bgf never read luck at all. BGBlitz stores `luckPlain` (MWC terms) and `luckWeighted` (the same divided by its MET slope, i.e. equity terms); the spec's `luck` is an equity delta, so store the weighted one. `luck_mwc` stays compute-on-read.

Both converters now emit luck on 100% of analysed checker plies. Cross-checked against bgsage's own analysis of the same match over 104 matched positions (paired by ogid_before): r=0.986 for XG, r=0.982 for BGF, confirming both the scale and the sign convention.

## 0.2.0 — 2026-07-25

**Export notation/met from barrel; enable subpath imports**

## 0.1.0 — 2026-07-25

**Initial standalone publish (includes unified move-notation canonicalizer)**

---

## JavaScript-only releases

Five tags from the period when `gvformat-js` was mirrored to a standalone
repository with its own version sequence, before the package moved to a
registry. That mirror is frozen and the sequence is closed; the package now
publishes from this repository under the shared tag line above.

- **0.11.2** (2026-08-01) — Per-alternative eval on BGF checker plies
- **0.11.1** (2026-08-01) — CLI bodies fully out of src/
- **0.11.0** (2026-08-01) — GvabError contract enforced; CLI guards moved out of src/
- **0.10.0** (2026-08-01) — .mat -> OGXM converter (convertMat)
