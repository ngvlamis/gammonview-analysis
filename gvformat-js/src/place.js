// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

//
// Where a match was played: the `event` / `site` pair. Mirrors gvformat/place.py.
//
// OGXM carries these as two independent top-level strings, matching every source
// format we read (a .mat's [Event]/[Site] headers, XG's event/location, BGF's
// event/site) and matching GammonView's two database columns.
//
// They were once folded into a single `event` string joined by PLACE_SEPARATOR,
// so files written before that changed carry the combined form with no `site` at
// all. `splitPlace` recovers the pair from such a string and is applied on read
// when a document has an `event` but no `site` -- old files self-heal, and
// nothing downstream has to know they were ever folded. `joinPlace` is its
// inverse, for writing a source format that only has one slot for the two.

// Joins the two halves in a legacy combined string. Whitespace-padded: it has to
// survive round-tripping through formats that trim their header values.
export const PLACE_SEPARATOR = ' • ';

/** Trim; an empty or blank string becomes null, never "". */
export function cleanPlace(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  return text || null;
}

/**
 * Split a legacy "Event • Site" string into { event, site }.
 *
 * Only the *first* separator splits, so a site that contains one of its own
 * keeps it. A string with no separator is all event -- guessing "site" would
 * move data between two columns that mean different things, and every source
 * format we read writes the event when it has only one of the two.
 */
export function splitPlace(combined) {
  const text = cleanPlace(combined);
  if (text === null) return { event: null, site: null };
  const at = text.indexOf(PLACE_SEPARATOR);
  if (at < 0) return { event: text, site: null };
  return {
    event: cleanPlace(text.slice(0, at)),
    site: cleanPlace(text.slice(at + PLACE_SEPARATOR.length)),
  };
}

/** Inverse of splitPlace: (event, site) -> one string. */
export function joinPlace(event, site) {
  const e = cleanPlace(event);
  const s = cleanPlace(site);
  if (e && s) return `${e}${PLACE_SEPARATOR}${s}`;
  return e || s || null;
}
