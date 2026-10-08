# Handoff to GammonView: `.gvab` becomes OGXM v2

*Draft, started 2026-10-08, on `feat/ogxm-v2`.
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
- **A `.bgf`-derived v1 file upgrades with our MWCs, not BGBlitz's.** v1 stores
  only normalized equity, rounded to 4 places, so the per-score map back to
  BGBlitz's MWC (`mwc_frame`) is gone. Rewriting such a file as v2 converts its
  equities through our table and records `met_id` `kazaross-xg2`, where a
  `.bgf` converted directly records `bgblitz` and BGBlitz's own MWCs at 1e-6.
  The result is consistent and correctly labelled, and normalized equities, errors
  and PR read back unchanged, so nothing the viewer shows moves. To get BGBlitz's
  MWCs, re-convert from the original `.bgf` if it is still held; nothing can
  recover them from the v1 file.
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

## The document gained every v2 field

1.6 reads everything v2 defines into the document, under v2's own names, and
writes it back. All of it is optional: a key is absent when the file has no
value, so documents from our own converters look exactly as before, apart from
`complete` and `engine_build` on our analysis blocks. What the site will see
when it opens another producer's file:

- **Match context:** `stage`, `round`, `table`, `city`, `country`,
  `event_url`, `platform`, `match_ref`, `rated`, `completed_at` (ms),
  `date_precision`, `player_seat`, `score_start`, `variant`, and
  `white_profile` / `black_profile` with `rating`, `rating_system`, `country`,
  `user_id`, `kind`.
- **Clock, video and annotations:** `clock` with `clock_ms` per ply, `video`
  with `video_ms` per marked ply, and `annotations` (comments, key/value notes,
  arrows and highlights) on the match, games, plies, decisions and
  alternatives.
- **Analysis detail:** rollout settings in `level` records, `rollout_se`,
  `is_optional`, `take_point`, `cubeless_equity` and more per decision.

**Four changes the display code must handle:**

1. **`event_year` is separate.** v2 files used to read as
   `event: "Nordic Open 2025"`; now `event: "Nordic Open"` plus
   `event_year: 2025`. Show both.
2. **`model_id` is the producer's identifier.** A HedgeHog block's `model_id` is
   now its UUID (1.5 showed `hedgehog/xerxes`), with the name in `model_name`.
   Show `model_name ?? model_id`.
3. **`site` vs `platform`/`city`.** `site` is still filled for display (from our
   own annotation, else `city`, else `platform`), so nothing breaks; a richer
   display can show `city`/`country` and `platform` separately.
4. **Matches that used to fail to load now load:** a match joined part-way
   (`score_start`), a cube already turned at the start, automatic doubles, and
   nackgammon / hypergammon / longgammon. A variant cannot be analyzed (the
   server should expect a refusal), and its board view must not assume 15
   checkers each in the backgammon start.

Editing any of these through the document is supported: the writer validates
each value, and one v2 cannot hold (too long, wrong characters, a `user_id`
without a `platform`) goes to an `x-gammonview-…` annotation instead of being
lost. A document that breaks a rule outright raises — for example an annotation
whose key is a v2 field name, or a clock whose readings run backwards.

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

Appending analysis to a file another producer wrote keeps everything in it (spec
I7). An unedited part is written back byte for byte, so a signature stays valid
exactly when it was; an edit drops only the signatures covering what changed.
Clock readings, video marks and annotations move with their ply, so a move edit
keeps them. What the document cannot model (signatures, unknown sections) rides
in a private `_ogxm2_passthrough` key, also in `.gva`; leave it alone.
`docs/OGXM_V2_PROFILE.md` §5 has the rules.

## Not in 1.6 (known gaps)

- **`.gva` is our document's JSON**, not v2's JSON projection (which the spec
  says is not an interchange format). `.gvab` is the file to exchange.

## Checklist for the GammonView session

1. Wait for the 1.6.0 tag and npm publish.
2. Bump both pins in one change (see the rollout order above).
3. Add the match-key stability test over `samples/gv`.
4. Run the full jest suite. The suites that write and re-read
   (`upgrademoves`, `bulkedit`, `gva-load`, `matchstats`, `gva-view`) are
   where a shape difference would surface.
5. Check the analysis table and the cube panel on a match with no recorded
   probabilities (the absent `eval` case).
6. Handle the four display changes above: `event_year`, `model_name ??
   model_id`, `site`/`platform`/`city`, and the newly loadable matches (in
   particular a variant's board).
7. Deploy the client and the server together.
