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

## 1.6.0 — unreleased

**`.gvab` is OGXM v2** *(format change)*

`write_gvab` writes OGXM v2, HedgeHog's current format, in place of v1 with a
`GVAN` chunk. Every file is plain v2: HedgeHog's reference codec loads each one
in the test corpus, finds no rule broken, replays every game to the end and
re-encodes it to the same bytes. `read_gvab` reads v1 and v2 alike, so every
file and share link written before stays readable; `write_gvab_v1` keeps the
old writer.

Nothing GammonView stored is dropped. What v2 has no field for travels in `ANNO`
records keyed `x-gammonview-…`, the namespace v2 reserves for producers outside
the spec, which another reader keeps and ignores: the whole analysis of an
illegal play (v2 records the play as a `set position` and allows no decision on
it), a decision v2's invariants would refuse as stored (XG's interleaved
database/ply lists, a played move listed twice, a played move judged at another
level), the decisions whose PR counting differs from the derived rule, and the
match's `site`. `docs/OGXM_V2_PROFILE.md` is the mapping.

What a reader sees differently, each a named rule in
`tests/test_ogxm2_writer.py`:

- match-play equities are stored as MWC, v2's unit, converted through each
  ply's score frame; they come back exactly except at the most lopsided scores
  (at worst ±6e-4, 2-away/25-away);
- where the played move is listed, `equity_loss` is best minus played, as v2
  derives it — exact for fresh analysis, ±1e-4 for analysis that went through
  v1's four places;
- a play's steps are stored in an order that replays legally (bar entries
  first), since v2 checks every intermediate position;
- all-zero probabilities and equities beyond ±3 are kept, where v1 read the
  first as "not recorded" and clamped the second;
- each analysis block gains an `analysis_id`.

Size: +2.8% raw and +6.9% deflated across the corpus, for strictly more
content (1e-6 precision, the values v1 clamped or dropped).

Not yet carried: rewriting another producer's v2 file drops its `CLCK`, `VIDO`,
signatures, foreign annotations and `MTCH` fields our document does not hold.

## 1.5.1 — 2026-10-05

**Analyzing a document keeps its White** *(fix)*

`analyze_file` appends its analysis onto the document it was given, and
`append_analysis` requires both to name the same White. `to_ogxm_json` always
chose White alphabetically, so a document whose White did not sort first was
analyzed to the last decision and then refused with `orientation mismatch`.
Every converter we own orients alphabetically, which hid it; a HedgeHog
`.ogxm` names White by its own seats, so an unanalyzed one sent to the server
failed at the end of the run. `to_ogxm_json(..., keep_orientation=True)` takes
White from the analyzed document, and `analyze_file` passes it.
`tests/test_analysis_orientation.py`. Python only; no npm release.

## 1.5.0 — 2026-10-05

**`read_gvab` reads OGXM v2** *(feature)*

HedgeHog writes v2 and nothing else since its 2.0 release, so every `.ogxm`
downloaded from hedgehog-bg.com failed a v1 reader's version check. v2 keeps
the magic and changes the container: a 16-byte header, varint-framed records
with presence masks, plies addressed by one match-wide `ply_ref`, one `DECS`
section per analysis block. `read_gvab` / `readGvab` dispatch on the major
version to `gvformat/ogxm2.py` / `gvformat-js/src/ogxm2.js`, which return the
document a v1 file holding the same match would give, so nothing downstream
learns there was a second version, and saving writes v1 `.gvab` as before. The
two ports produce identical documents on every fixture.

What v1 reading did not need:

- **Units.** A v2 block names its currency, and HedgeHog's match analyses are
  `cubeful_match`: every equity in them, each candidate's included, is an MWC.
  Each is mapped onto the normalized scale through its own ply's frame
  (`mwc_frame` / `mwcFrame`, now public in `basefill`, whose unit inference is
  switched off for these blocks so nothing converts twice). On a real 9-point
  match the converted errors sum back to HedgeHog's own MWC totals within 6e-5.
- **Luck.** v2 records it per roll (`ROLL`), and it is read as an `.xg`'s or a
  `.bgf`'s is, with `luck_eval_level` from the rolls' level. Held against
  bgsage's own luck pass on the same match: correlation 0.993, mean difference
  0.016 a roll. Only a block with no rolls is listed in `_base_analyses`.
- **Refusals.** What the v1 document cannot replay (beavers, raccoons, a cube
  set by hand, a settlement, a starting score, a variant) is refused with a
  message for the player rather than shown on the wrong board. A game from a
  set-up position becomes a leading set-position ply, the way our own exports
  state one.

Signatures, annotations, the clock and the video are not carried. The fixtures
(`gvformat-js/test/fixtures/ogxm2/`, shared by both suites) were written by
HedgeHog's own reference writer, and their expected positions are its reader's
replay, not ours.

## 1.4.0 — 2026-10-02

**A restated play keeps its analysis** *(fix)*

The fourth of the set-position ply's loose ends, and the first that was never a
corruption: the play replayed correctly, it just arrived with nothing said about
it. All three converters dropped the ply's `analysis` when the play
had to be stated as the board it produced, so an illegal play showed its error,
its alternatives and its luck when its longest hop happened to fit `pips`' three
bits, and showed a bare row when it did not. In the match this was reported on,
the same player makes three illegal plays: a 6-2 played `13/6` and a 6-1 played
`16/14 13/7` were analyzed on screen, and a 6-1 played `12/4` — eight pips in one
hop — was not.

Nothing in a checker analysis needs the steps. The alternatives name the plays
that *were* available, `luck` belongs to the roll, and the played candidate of an
illegal play already carries no steps of its own (1.3.0's
`fit_alternative_steps`). So `set_position_ply` / `setPositionPly` now take the
analysis and carry it through, and `xg.py`, `bgf.py` and `export.py` — with their
three JavaScript mirrors — hand it over. The play stays out of PR the way it
always did, through `illegal_move` and `decision: false`, not through being
thrown away.

The rest of the format layer follows the same rule: a set-position ply *carrying
dice* is a restated play and reads as a checker ply.

- `binary.py` / `binary.js`: an EVAL entry is keyed by (game, ply) index and
  never looked at the ply's action, so action 31 joins the checker branch and the
  analysis — alternatives, luck, and any embedded cube decision — survives a
  `.gvab` round trip.
- `stats.py` / `stats.js`: its roll's luck reaches the luck totals and its
  `illegal_move` the illegal-move count, so neither total depends on how the play
  had to be encoded. It still counts as no decision.
- `basefill.py` / `basefill.js`: a base-format block has no `illegal_move` flag
  to read, but a set-position ply with dice is one by construction, so the flag is
  derived — which is what keeps a foreign file's restated play out of its decision
  count.

`test_illegal_play_steps` and its JS mirror now assert the analysis is carried,
survives the binary, and reaches the aggregates as luck and an illegal move but
not as a decision. The spec pages gained the rule (`OGXM_JSON_SPEC_GAMMONVIEW.md`
under `set_position`, `OGXM_COMPUTED_FIELDS.md` under what counts as a decision).

**…and our own analyzer judges one too** *(fix)*

The other half: carrying a source's analysis is no use where there is none, and
`gvanalysis` raised no decision for a restated play, so an analyzed `.mat` had a
hole exactly where the imported `.xg` had an evaluation. It is a turn like any
other — the player was on roll, faced the cube and played something — so
`ogxm_reconstructor` now reconstructs it as a checker decision with the stated
board standing in for the play. `game_eval` then finds no legal move that reaches
that board and takes its illegal-play path, which is exactly what the ply records:
the error sized against the best legal play, the alternatives, the roll's luck,
`illegal_move` set, and no decision counted.

**It could not have worked before, and that is the part worth reading.**
`analyze_file` on either corpus match with a restated play raised
`analysis/ply count mismatch` and returned nothing at all. The exporter re-derives
a ply's steps from a board diff, and a diff cannot always be split back into the
hops that made it — `hQ8sVn2LbTdF4wRm`'s 4-4 bear-off matches as a single 11-pip
span, which no step can hold, so a perfectly *legal* play was restated as a set
position and the analysis stopped lining up with the document it was made from.
The reconstructor now hands the ply's own steps over (`move_steps` on the
decision and the log entry, preferred by `_convert_checker_ply` /
`_convertCheckerPly`): they came *out* of a ply record, so they fit one, and they
are the play as recorded rather than a reading of it.

`merge._is_decision` / `_isDecision` pairs a restated play up like any other
decision ply, which it can now that both documents reach one by the same route.

One smaller loss went with it: the played candidate of an illegal play was
written with an empty notation, because only the `.mat` *reader* supplies a
source notation and the OGXM path has none. It now falls back to the play as the
two boards describe it, so the candidate names itself and — where a step list can
hold it — draws itself. That is the one golden that moved
(`5nqfGw9bWG3deTaU`, whose played alternative gained the two steps the ply itself
already carried); the other four are untouched.

New test: `test_restated_play_analysis` (engine-backed, ~30s) — both matches
analyzed from their `.mat`, and one re-analyzed as the converted `.gvab` a server
is handed, where XG's judgement of the play and ours end up side by side on it.

**`gvan-match` takes an `.xg` or a `.bgf`** *(feature, and a fix underneath it)*

Found while reproducing the above: handing the analyzer the very `.xg` the bug was
reported on produced a cheerful empty analysis of a zero-game match. `load_ogxm`
read `.mat`, `.gva`/`.ogxm` and `.gvab`, and *guessed* anything else was `.mat`
text — and a `.mat` parser finds no games in an XG file, so the one input shape a
user is most likely to have was the one that failed silently. `gvformat` has
shipped `convert_xg` and `convert_bgf` all along, neither needs the engine, and
the loader simply never called them.

So it calls them. `gvan-match`, `gvan-batch`, `analyze_file`, `analyze_mat` and
`analyze_match` now take `.xg` and `.bgf` directly, with the source's own analysis
preserved and ours appended — an `.xg` in, XG's judgement and ours side by side
out, with no conversion step in between. `read_xg` / `read_bgf` and
`convert_xg` / `convert_bgf` accept **bytes as well as a path**, which is what the
loader holds (it has already decompressed a `.gz` before it knows the format) and
what a server is handed; the JS mirrors have always been bytes-only for that
reason.

And dispatch no longer guesses. It reads the file's own opening bytes first —
`OGXM`, XG's `RGMH`, a `.bgf`'s one-line JSON header — so a mislabeled or
extension-less file is read as what it is, and falls back to the extension only
where the content cannot say. What nothing identifies now raises, naming every
format that would have worked, rather than being run through the `.mat` parser.
A `.mat` that yields no games raises too: it has no file signature, so "it
parsed" proves nothing, and that is the last point at which a text file that is
not a match can be told apart from one.

`gvan-batch` expands a directory by `loader.INPUT_EXTENSIONS` now, so its list
cannot fall behind the loader's again.

New test: `test_loader_formats` (no engine, instant) — all five formats loading
as the converter each one reaches, gzipped and not, mislabeled and
extension-less, and the four inputs that must raise instead of returning an empty
match.

## 1.3.0 — 2026-09-29

**A hop longer than 7 pips is no longer written as a different move** *(fix)*

The third shape of illegal play, and a near-miss of the same bug 1.2.0 fixed. A
real 3-3 was played `13/3 7/4`: ten pips in one hop, which the dice cannot
explain, so the span splitters keep it whole and hand over a single step of
`pips: 10`. `pips` is three bits, so ten was **written as two** and the play read
back out of the file as `13/11 7/4` — a move nobody made, on a ply whose step
count and replayed board both looked right, which is why nothing downstream could
tell. The wrap only showed once the document had been through a `.gvab`, so an
import viewed in place was fine and the same match saved, shared or analyzed was
not.

`fit_move_steps` / `fitMoveSteps` now check hop *length* as well as hop count, so
a span the field cannot hold reaches the set-position rung alongside a backwards
hop, and the play is stated as the position it produced. The two writers say so
rather than wrap: `binary._encode_ply` raises on an oversized step, as it already
does on an over-long ply, and `_encode_alt_entry` — whose steps are read only for
display, and where refusing would cost a whole file — drops them instead, so an
alternative draws nothing rather than the wrong move. The same shaping is now
applied to every alternative on the way in (`fit_alternative_steps`), which is
where the played candidate of an illegal play gets its steps.

New public API: `fit_alternative_steps` / `fitAlternativeSteps` in `export`, and
`MAX_STEP_PIPS` (`binary.py`, `constants.js`).

The match this was found on joins the sample corpus as `rK7pXm4TqLb9NzWd`, as
both a `.mat` and an `.xg` — the two converters reach the set-position ply by
different routes, as they do for `hQ8sVn2LbTdF4wRm` — and
`test_illegal_play_steps` now names both.

One existing sample moves: `hQ8sVn2LbTdF4wRm`'s 4-4 bear-off, which the diff
matched as an 11-pip span and stored as 3, now splits into the four hops its
notation states. Its `.mat` reference fixture was regenerated to match. The spec
pages gained the ceiling: `pips` is 1–7, a legal hop 1–6.

## 1.2.0 — 2026-09-29

**A checker moved *backwards* is no longer lost on import** *(fix)*

A play that sends a checker the wrong way up the board — a 6-5 played
`14/8 15/10 6/8`, which a site let through and both XG and HedgeHog record
verbatim — used to vanish from the ply that carried it, leaving every board for
the rest of that game one checker out of place. Two separate causes:

- `export.py` / `export.js`: the span splitters answer a non-forward span with no
  hops at all, so the hop left nothing behind for `fit_move_steps`' step count to
  catch. The three-hop play came out as two steps, the count looked right, and the
  hop was simply gone. A backwards hop is now recognised in its own right
  (`notation_has_non_forward_hop`, `board_diff_has_non_forward_hop`) and sends the
  ply straight to the set-position rung: `pips` is an unsigned 3-bit *forward*
  distance, so no step can hold one however much room the record has.
- `xg.py` / `xg2gva.js`: XG's DataMoves list for such a play holds only the
  forward hops, while the played candidate's own stored *position* has all of them.
  Where the two disagree on a play XG flagged illegal (`invalid_m == 2`), the
  position is now believed and the hop list is not.

The ply is written as a set-position ply carrying the roll's dice, which is what
the spec already reserved for a play no checker ply can carry. Reading such a
ply, the reader now derives its OGIDs and advances the turn state, as a checker
ply's would be — a set-position ply with *no* dice still states only where a game
starts (the first ply of an exported saved position) and gets none.

Two consequences worth knowing. The ply keeps no analysis: the format hangs a
checker eval on a dice ply, and an illegal play was excluded from PR and decision
counting anyway, so no rating moves. And a match holding one of these hashes
differently than it did in 1.1.0, because the document genuinely changed; only
matches with this shape are affected.

The match this was found on is now in the sample corpus as `hQ8sVn2LbTdF4wRm`,
as both a `.mat` and an `.xg` — one file per cause — and
`test_illegal_play_steps` checks that the two land on the same board.

## 1.1.0 — 2026-09-20

**Analysis results change in this release.** The first four entries below all
change what a fresh analysis produces — a preset is gone, two were retuned, one
threshold became two, and a borderline decision now escalates. A `.gva` or
`.gvab` written by 1.0.0 still reads identically; it is the *next* analysis of
the same match that differs, so re-analyze rather than compare across versions.

**The `balanced` preset is retired** *(breaking)*

`--preset balanced` (and its `b` alias) now fails. It existed to be the cheap
strong preset — a 2-ply screen with a 3-ply middle tier, where `world_class`
screens at 3-ply and deepens to 4 — and it stopped being one. Because a preset's
cost is dominated by its sizing tier, and `balanced` paid the same `truncated2`
tier as everything above it, re-timing after the September 2026 retune put it at
170.4 / 751.8 / 1030 ms per ply on a 24-core M2 Ultra, an 8-core M3 and a 4-core
i5-7600, against `world_class_fast`'s 165.6 / 611.0 / 1016 — within a few
percent on two machines and 23% *worse* on the laptop. It also agreed with XG
less often (0.540 mean PR gap against 0.441). Neither faster nor better leaves
nothing to recommend.

Use `world_class_fast`, or `deep` if you want something genuinely cheap — the
gap `balanced` was meant to fill is now the gap between those two, about an
order of magnitude.

The name is **not** aliased to a replacement. The preset name is written into
the output document, so a silent redirect would label the analysis as something
it is not; `resolve_preset` raises instead, and the message names the
replacement. A `presets.yaml` may still define a preset called `balanced` — it
is a removed built-in, not a reserved word.

[`docs/PRESET_ACCURACY.md`](docs/PRESET_ACCURACY.md) keeps the 493-match study
that included it, unedited. The `balanced` column there is the evidence for the
removal, not a recommendation.

**`world_class` and `world_class_fast` are retuned to XG's own routing**

Mining XG's World Class decisions — 78,661 of them — for the level it actually
spent puts its depth crossover near a top-2 equity gap of **0.08** and its
rollout cliff at an error of **0.02**. Both presets now use those numbers.

`world_class` was a flat 4-ply first pass, which is not what XG does. It becomes
a **3-ply screen that deepens to 4-ply on near-ties** — XG runs 4-ply on 93.7%
of decisions with a gap under 0.005 and on only 8.3% of those past 0.12. This is
both more faithful and considerably cheaper: measured over three matches, 104.9
→ 55.7 s/match, a 1.88× speedup, as the full-width 4-ply pass falls from 71.1%
of decisions to 49.0% and the rollout share from 28.9% to 13.6%.

`world_class_fast`'s cube middle tier moves from a 0.04 close threshold to 0.08,
so "borderline" means the same thing on both presets. Over 33 holdout matches
this is MAE-neutral (0.4826 → 0.4840, 95% CI [−0.0235, +0.0192]) at 1.5% more
wall clock, with 2 of 33 matches changing at all — adopted as a coherence fix,
not a measured gain, since 489 of 686 cube decisions already reached the rollout
at 0.04.

**`close_threshold` and `error_threshold` are now two separate dials**

They answer different questions and were never the same number.
`close_threshold` gates the *middle tier*: how near a tie before the screen's
verdict stops being trusted. `error_threshold` gates the *second pass*: how much
a decision has to cost before its size is worth a rollout. A middle tier is
about 34× cheaper than a rollout on a checker move list, so the first can afford
to be generous where the second cannot.

`error_threshold` defaults to **0.02** and, like `mid_pass` and
`close_threshold`, takes either a bare number or a `{checker, cube}` mapping.
`fast` sets it to `0`, because its second pass is 3-ply rather than a rollout:
there the second pass costs ~6× the screen instead of ~200×, and cheap depth is
worth spending on every error.

A `presets.yaml` written against 1.0.0 still loads, but a custom preset may
escalate differently, in either direction. A 3-tier preset used to need an error
larger than its `close_threshold` to reach the second pass, so one with a wide
threshold now rolls out **more** often. A 2-tier preset had no error gate at all
and escalated on any disagreement, so it now rolls out **less** often. Setting
`error_threshold` explicitly — to the old `close_threshold` in the first case,
to `0` in the second — restores the previous behaviour exactly.

**A borderline decision that proves to be a real error now escalates**

A checker play whose top two moves were within `close_threshold` went to the
middle tier and was capped there, however large the played move's error turned
out to be. The two are unrelated: `top2_gap` is best-vs-second and says nothing
about how far down the list the player actually went. Over a 2-match corpus at
a 0.04 threshold, 12 of 92 borderline decisions lost more than the threshold and
7 lost more than 0.08 — the worst a 0.176 blunder in a position whose top two
moves were 0.0004 apart, all of them sized at 3-ply by a preset whose second
pass exists to roll out exactly those errors. Borderline cubes were never
re-checked at all (0.4% of cubes, median 0.07, worst 0.22).

Both kinds now escalate to `second_pass`, judged on the middle tier's own
numbers rather than the screen's. PR moves toward the deepest preset at every
threshold width. The escalation is skipped when `mid_pass` names `second_pass`'s
own level, since the analyzers are one object and the second call would buy the
same answer twice — which makes it a no-op for `world_class_fast`'s cube tier by
construction.

**Parallel analysis now fills the machine** *(behaviour change on `--jobs 0`)*

The auto split was `cpu // 2` workers × `cpu // 4` engine threads, measured only
at 24 cores and only against the two presets with no rollout tier. It is now
`ceil(cpu / 7)` workers, each using every core.

The 7 is measured: `checker_eval` elevates ~15 candidates per decision with a
hardcoded `n_threads=1` and overlaps them, so **one decision draws about 7
effective cores and no more**. Everything follows from that ceiling — one
decision fills a laptop but leaves a workstation two-thirds idle, which is why
serial analysis costs 3.9% on 8 cores and 44% on 24. Measured over a 6-match
corpus on a 24-core M2 Ultra, an 8-core M3 and a 4-core i5-7600; 9–21 cores is
interpolation. Output is byte-identical at any setting.

`gvan-match` and `gvan-batch` need no change — both already defaulted to
`--jobs 0`, and simply get faster. Imported callers still default to `jobs=1`
(serial), because a library cannot know whether its caller guarded
`if __name__ == "__main__":` and spawning without that guard fails as a bare
`BrokenProcessPool`. That safety is no longer cheap: **pass `jobs=0` from a
server or batch driver.**

**Preset accuracy re-measured after the retune**

The 493-match XG corpus was re-analyzed with both retuned presets. Mean gap to
XG's match PR moves 0.434 → 0.423 for `world_class` and 0.441 → 0.431 for
`world_class_fast`; neither change is significant (paired 95% intervals span
zero), so the retune is established as harmless rather than as an improvement.
The "pending re-measurement" notices are gone from the README and `CLI.md`.

**Worker processes now exit with their parent**

A `ProcessPoolExecutor` only cleans up when the parent exits normally. If it
dies hard — SIGKILL, force-quit, crash, a laptop shut down rather than logged
out — the workers block forever on a queue nobody will write to again, reparent
to PID 1, and hold their analyzers and network weights until reboot. Under the
`spawn` start method every worker holds its own handle to the call queue's pipe,
so the write end stays open among the survivors and there is no EOF to notice.

Thirty-seven such orphans were found on one machine in September 2026, in three
cohorts days apart, holding roughly 19 GB. On a 16 GB laptop running the helper
this reads to a user as "my computer is broken", with nothing to point at.
Workers now wait on `multiprocessing.parent_process()`, whose sentinel is a pipe
only the parent holds the other end of — one idle thread, no polling, and it
works on macOS and Windows where `PR_SET_PDEATHSIG` does not.

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
