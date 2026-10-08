# Handoff to GammonView: `.gvab` becomes OGXM v2

*Draft, started 2026-10-08, on `feat/ogxm-v2` (commits `c70ea12`, `7e9c6e4`).
Ships as gammonview / `@gammonview/gvformat` **1.6.0**, not yet tagged.*

This is what the GammonView repo needs to know and do when it takes 1.6.0. The
format itself is specified in [`OGXM_V2_PROFILE.md`](OGXM_V2_PROFILE.md); this
note covers only what touches the site, the analysis service and stored data.

## What changes

`write_gvab` writes **OGXM v2** (HedgeHog's current format) where it wrote v1
plus a `GVAN` chunk. That holds in Python and in JS, and the two writers agree
byte for byte. `read_gvab` / `readGvab` read v1 and v2 both, and always will.
The function names, import paths and document shape are otherwise unchanged:
`readGvab`, `write_gvab`, `appendAnalysis`, `compute_aggregates` are called
exactly as before. `write_gvab_v1` is new, for comparison only. GammonView
should not need it.

Nothing GammonView stores is lost. What v2 has no field for goes in `ANNO`
records keyed `x-gammonview-…`, which other v2 readers keep and ignore:

- illegal plays and their analysis;
- PR-counting exceptions;
- `site`;
- an event over 120 bytes;
- a stated score that the games don't add up to.

A 1.6 reader restores all of it.

## Rollout order: this is the part that can lose data

Both sides of GammonView write `.gvab`:

- the browser: `GammonView.vue`, `MyMatches.vue` (`replace_match`, metadata
  edits) and `DownloadMatchDialog.vue`;
- the service: `server/gvserver/app.py`, which returns `write_gvab(ogxm)`.

1.5.x already *reads* v2, but as a foreign reader. On a file written by 1.6 it
drops the `x-gammonview-…` data: `site`, the analysis of illegal plays, and the
stored PR-counting exceptions (it re-derives counting instead). If a 1.5 side
then writes the document back, the loss is saved.

So both sides should move to 1.6.0 **in the same deploy**:

- `server/pyproject.toml`: `gammonview[engine]==1.5.1` → `==1.6.0`, then
  `uv lock`.
- `package.json`: `"@gammonview/gvformat": "^1.5.0"` → `"^1.6.0"`, then
  `npm install`.
  - Watch out: the caret range already admits 1.6.0, so any `npm install` or
    `npm update` after the publish pulls it into the lockfile without the server
    moving with it.
  - Either pin `1.5.x` until deploy day, or bump both in one commit.

## Stored data

- **Existing v1 files remain readable** with no migration. A stored file becomes
  v2 the next time GammonView rewrites it (save, metadata edit, re-analysis).
  There is no need to batch-convert.
- **File hashes change on rewrite.** `match_originals` dedupes by
  `UNIQUE(match_id, sha256)`, so the same match saved again as v2 becomes a new
  original row. That is benign but visible.
- **The match key should not move.** `SavedMatches.md` hashes the sequence of
  positions, not steps, which is exactly what survives the change: v2 can reorder
  a ply's steps (it requires every intermediate position to be legal) but never
  changes the resulting position. **This needs a test, not just the argument:**
  compute the key for `read(v1 file)` and for `read(write_gvab(read(v1 file)))`
  over `bgsage-analysis/samples/gv/*.gvab`, and assert they are equal.

## Share links

v1 links keep working. New links carry v2. A v2 file is about 7% larger after
deflate, so new links are about 7% longer. That should only matter if a link
was already near a length limit (the `--browser` temp-redirect path exists for
exactly that).

## What reads back differently

These differences are all in the profile's §4. The ones a viewer could notice:

- **`analysis.eval` can be absent.** v1 always wrote a zero placeholder; v2
  writes nothing when there is nothing. The three reads I found already
  tolerate this:
  - `view.js:118`: `?? null`;
  - `view.js:249`: `?? null`;
  - `positiondoc.js:757`: `if (analysis.eval)`.

  Worth one pass through the analysis table and the cube panel.
- **Move steps may be listed in another order** within a ply (a bar entry first,
  for example). It is the same move and the same resulting position. Anything
  that displays steps verbatim will show the new order.
- **Equity loss can move by 1e-4** on about 5% of decisions in a document that
  already went through v1, once. Fresh analysis is exact.
- **Match-play equities** are stored as MWC and converted back. They are exact
  everywhere except the most lopsided scores, where the worst case is ±6e-4
  (2-away/25-away).

## Converter changes a viewer may notice

- **A `.bgf`'s analysis now carries `mwc_frame`** on each analysed ply and
  `analysis_info.met_id = "bgblitz"` on its block, so a v2 file stores
  BGBlitz's own MWCs rather than ours. Normalized equities and PR are
  unchanged. Code that walks analysis keys strictly should allow the two new
  fields; `met_id` also appears on blocks read from any v2 file that names one.
- **A BGBlitz money session's last game can gain points.** Where the source
  stored 0 but the session's final score includes the game, the game now
  shows the difference (6 and 1 points in two of the samples). Per-game score
  displays and anything summing `points_won` will move for such sessions.

## Things GammonView gains

- Every `.gvab` it writes is a valid v2 file that HedgeHog's own tools load and
  replay.
- Analysis is kept at 1e-6, where v1 kept 4 decimal places, and values v1
  clamped are now stored unclamped.

## Rewriting someone else's v2 file

In 1.6 appending analysis to a file another producer wrote keeps its clock,
video, signatures, annotations, unknown fields and sections (spec I7); an
unedited part is written back byte for byte, so a signature stays valid exactly
when it was. Editing the match drops the signatures that cover it, and editing a
move also drops the clock, video and ply-addressed annotations. The document
carries a private `_ogxm2_passthrough` key for this (also in `.gva`); leave it
alone. `docs/OGXM_V2_PROFILE.md` §5 has the rules.

## Not in 1.6 (known gaps)

- **`.gva` is still our own JSON.** Switching it to v2's JSON projection is the
  planned second step. GammonView's `.gva` handling will need its own handoff
  then.

## Checklist for the GammonView session

1. Wait for the 1.6.0 tag and npm publish.
2. Bump both pins in one change (see the rollout order above).
3. Add the match-key stability test over `samples/gv`.
4. Run the full jest suite. The suites that write and re-read
   (`upgrademoves`, `bulkedit`, `gva-load`, `matchstats`, `gva-view`) are
   where a shape difference would surface.
5. Check the analysis table and the cube panel on a match with no recorded
   probabilities (the absent `eval` case).
6. Deploy the client and the server together.
