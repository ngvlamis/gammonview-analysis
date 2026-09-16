# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Validation harness for gvab.write_gvab against Eran's reference OGXM codec.

Runs gvan_match.analyze_mat on a real match file, converts the result with
ogxm_export.to_ogxm_json, and checks gvab.write_gvab's output three ways:

  1. libogxm (the reference C++ codec, via ctypes) can read our bytes back,
     and the decoded base match (match_length, player names, game/ply
     counts, sampled EVAL/CUBE values) matches the input OGXM-JSON. libogxm
     doesn't know about GVAN -- it just skips it as an unrecognized
     ancillary chunk, which is exactly the point.
  2. Base-chunk byte parity: libogxm's own writer (`ogxm_json_to_binary`) is
     run on the *same* OGXM-JSON dict, and the ANAL/EVAL/ALTS/CUBE chunks
     from our bytes and the reference's bytes are parsed out of both TLV
     streams and asserted byte-identical. GAME is byte-identical to the
     reference (equity<->MWC conversion is compute-on-read via gvformat.met,
     so the format carries no per-game or per-decision anchor at all). MHDR
     is asserted identical *except* the documented GammonView metadata bytes
     gvab.py writes into its reserved/trailing regions (beaver/raccoon flag
     bits, cube_limit, the trailing `event`/`site` strings) -- see
     `_mhdr_base_equal`/`_game_base_equal` below. Only those MHDR bytes plus
     GVAN/CSUM/total-size/END legitimately differ (GVAN is a GammonView-only
     addition the reference doesn't write). CUBE parity is a key check: the
     maintainer's base codec maps the JSON `cube_decision` (correct/non-error
     live cube) sub-object to `type=4` (live_checker) and `missed_double` to
     `type=2` -- both must appear as ordinary CUBE entries and byte-match, not
     be diverted into a GammonView-only section.
  3. GVAN self-check: the GVAN chunk is manually parsed out of our bytes
     (v2 layout: checker records carry `luck` + two reserved zero slots
     where the removed mwc_on_win/mwc_on_loss anchors used to live; cube
     records carry two reserved zero slots too) and its header
     (luck_eval_level, base_eval_level, gvan_version==2) and per-section
     records (checker decision/has_luck/illegal_move flags + luck, alt
     eval_level bytes, cube decision/eval_level) are compared against
     independently-derived expectations read straight off `ogxm`'s per-ply
     `analysis` objects (not off gvab's own internals).
  4. GammonView match metadata round-trip: small decode helpers here pull
     `beaver`/`raccoon`/`cube_limit`/`event`/`site` back out of the raw MHDR
     bytes and assert they equal `ogxm`'s values -- for the real match (which
     exercises cube_limit and event/site organically) and for a small synthetic
     match that exercises beaver and raccoon. The synthetic case also
     re-confirms libogxm tolerates non-zero reserved bytes and extra trailing
     bytes in MHDR rather than rejecting them -- the critical compatibility
     check for this whole placement strategy.

Run directly:
    uv run python tests/test_gvab_writer.py
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Optional cross-check against HedgeHog's reference codec; clone from
# https://gitlab.com/eranlambooij/hedgehog-public and `make libogxm`. Absent -> skip.
_HEDGEHOG_EXAMPLES = Path("~/projects/hedgehog-public/examples").expanduser()
sys.path.insert(0, str(_HEDGEHOG_EXAMPLES))

from gvanalysis import analyze_mat
from gvformat.export import to_ogxm_json
from gvformat.binary import (
    write_gvab, _encode_eval_level, _enc_equity, _missed_double_counts,
    CHUNK_MHDR, CHUNK_GAME, CHUNK_ANAL, CHUNK_EVAL, CHUNK_ALTS, CHUNK_CUBE, CHUNK_GVAN,
    CUBE_TYPE_DOUBLE_DECISION, CUBE_TYPE_TAKE_PASS, CUBE_TYPE_MISSED_DOUBLE,
    CUBE_TYPE_RESIGN, CUBE_TYPE_LIVE_CHECKER,
    GVAN_VERSION, GVAN_CHECKER_REC, GVAN_CUBE_REC,
)

try:
    import ogxm_ctypes  # ctypes wrappers over hedgehog-public/build/libogxm.so
except SystemExit:
    # ogxm_ctypes exits rather than raising when libogxm.so is missing.
    ogxm_ctypes = None
    _OGXM_SKIP = f"SKIP: libogxm not built under {_HEDGEHOG_EXAMPLES.parent}"
except ImportError:
    ogxm_ctypes = None
    _OGXM_SKIP = f"SKIP: {_HEDGEHOG_EXAMPLES} not found (hedgehog-public not checked out)"
else:
    _OGXM_SKIP = None

from fixtures import sample_mat, missing_mat_message  # noqa: E402

MAT_PATH = sample_mat()

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"FAIL  {label}")
    else:
        print(f"OK    {label}")


# ---------------------------------------------------------------------------
# Minimal TLV chunk-stream parser (independent of gvab.py's writer) -- used to
# pull individual chunks back out of both our bytes and the reference's bytes
# for the byte-parity comparison.
# ---------------------------------------------------------------------------

def _parse_chunks(data: bytes) -> dict[int, list[bytes]]:
    chunks: dict[int, list[bytes]] = {}
    pos = 20  # skip the 20-byte file header
    n = len(data)
    while True:
        remaining = n - pos
        if remaining == 8:
            break  # End Marker
        assert remaining > 8, f"truncated OGXM stream at offset {pos} ({remaining} bytes left)"
        ctype, clen, _cflags, _res = struct.unpack_from("<IIHH", data, pos)
        pos += 12
        cdata = data[pos:pos + clen]
        pos += clen
        chunks.setdefault(ctype, []).append(cdata)
    return chunks


def _first_diff(a: bytes, b: bytes) -> int | None:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return None if len(a) == len(b) else n


def _report_mismatch(label: str, ours: bytes, ref: bytes) -> None:
    idx = _first_diff(ours, ref)
    print(f"      {label} mismatch: len(ours)={len(ours)} len(ref)={len(ref)} first diff @ byte {idx}")
    if idx is not None:
        lo, hi = max(0, idx - 4), idx + 12
        print(f"      ours: {ours[lo:hi].hex()}")
        print(f"      ref : {ref[lo:hi].hex()}")


# CubeEntry layout (28 bytes), matching gvab._encode_cube_entry / the base
# ogxm_format.hpp struct -- used to decode individual CUBE records for a
# type-aware diff when the raw CUBE chunk bytes mismatch.
_CUBE_ENTRY_FMT = "<BHBhhhHHHHHHBBB3s"
_CUBE_ENTRY_SIZE = struct.calcsize(_CUBE_ENTRY_FMT)
assert _CUBE_ENTRY_SIZE == 28

_CUBE_TYPE_NAMES = {
    CUBE_TYPE_DOUBLE_DECISION: "double_decision",
    CUBE_TYPE_TAKE_PASS: "take_pass",
    CUBE_TYPE_MISSED_DOUBLE: "missed_double",
    CUBE_TYPE_RESIGN: "resign",
    CUBE_TYPE_LIVE_CHECKER: "live_checker",
}


def _decode_cube_entries(data: bytes) -> list[tuple]:
    n = len(data) // _CUBE_ENTRY_SIZE
    return [struct.unpack_from(_CUBE_ENTRY_FMT, data, i * _CUBE_ENTRY_SIZE) for i in range(n)]


def _cube_type_counts(data: bytes) -> dict[int, int]:
    counts: dict[int, int] = {}
    for entry in _decode_cube_entries(data):
        t = entry[2]
        counts[t] = counts.get(t, 0) + 1
    return counts


def _diff_cube_entries(ours: bytes, ref: bytes) -> None:
    """Diagnostic for a CUBE byte mismatch: decode entry-by-entry and print
    the first structurally differing record (with its type), per the task
    of diffing type=4/type=2 packing against libogxm's."""
    ours_entries = _decode_cube_entries(ours)
    ref_entries = _decode_cube_entries(ref)
    if len(ours_entries) != len(ref_entries):
        print(f"      CUBE entry count differs: ours={len(ours_entries)} ref={len(ref_entries)}")
    for i, (oe, re) in enumerate(zip(ours_entries, ref_entries)):
        if oe != re:
            tname = _CUBE_TYPE_NAMES.get(re[2], f"type={re[2]}")
            fields = ("game_index", "ply_index", "type", "no_double_eq", "double_take_eq",
                      "double_pass_eq", "win", "gwin", "bgwin", "gloss", "bgloss",
                      "equity_loss", "correct_action", "played_action", "ply", "reserved")
            print(f"      CUBE[{i}] ({tname}) differs:")
            print(f"        ours: {dict(zip(fields, oe))}")
            print(f"        ref : {dict(zip(fields, re))}")
            return
    print("      CUBE entries all decode equal (mismatch must be in surrounding padding/length)")


# ---------------------------------------------------------------------------
# GammonView metadata decode helpers -- independent of gvab.py's internals,
# these pull beaver/raccoon/cube_limit/event/site back out of raw MHDR bytes and
# met_value out of raw GAME bytes, per the placement documented in
# OGXM_FORMAT_SPEC_GAMMONVIEW.md and gvab.py's module docstring:
#   - MHDR flags byte (offset 3): bit 2 = beaver, bit 3 = raccoon
#   - MHDR reserved[8] (offset 16-23): cube_limit as uint16 LE at 16-17
#   - MHDR variable part: event then site, each uint8-length-prefixed UTF-8,
#     appended after player_white/player_black
# The old per-game trailing `met_value` is gone -- equity<->MWC anchors are
# now per-decision in GVAN, so GAME is byte-identical to the reference codec.
# ---------------------------------------------------------------------------

def _decode_mhdr_gv(mhdr: bytes) -> dict:
    flags = mhdr[3]
    cube_limit = struct.unpack_from("<H", mhdr, 16)[0]
    pos = 24  # end of MhdrFixed
    plen = mhdr[pos]
    pos += 1 + plen
    blen = mhdr[pos]
    pos += 1 + blen
    event = site = None
    if pos < len(mhdr):
        elen = mhdr[pos]
        pos += 1
        if elen:
            event = mhdr[pos:pos + elen].decode("utf-8")
        pos += elen
    if pos < len(mhdr):
        slen = mhdr[pos]
        pos += 1
        if slen:
            site = mhdr[pos:pos + slen].decode("utf-8")
    return {
        "beaver": bool(flags & 0x04),
        "raccoon": bool(flags & 0x08),
        "cube_limit": cube_limit,
        "event": event,
        "site": site,
    }


def _mhdr_base_equal(ours: bytes, ref: bytes) -> bool:
    """True if `ours` and `ref` MHDR chunk bytes agree on everything the base
    codec knows: the fixed 24-byte header except flags bits 2-3 (beaver/
    raccoon) and the cube_limit uint16 at offset 16-17, plus the
    player_white/player_black variable-part strings (identical -- only the
    trailing `event`/`site` strings, which `ref` never writes at all,
    differ)."""
    if len(ours) < 24 or len(ref) < 24:
        return False
    o_fixed, r_fixed = bytearray(ours[:24]), bytearray(ref[:24])
    o_fixed[3] &= ~0x0C
    r_fixed[3] &= ~0x0C
    o_fixed[16:18] = b"\x00\x00"
    r_fixed[16:18] = b"\x00\x00"
    if bytes(o_fixed) != bytes(r_fixed):
        return False
    pos = 24
    plen = ref[pos]
    pos += 1 + plen
    blen = ref[pos]
    pos += 1 + blen
    return ours[24:pos] == ref[24:pos]


def _cube_base_equal(ours: bytes, ref: bytes) -> bool:
    """True if `ours` and `ref` CUBE chunk bytes are byte-identical.

    This used to tolerate one documented divergence: the `type=2`
    (missed_double) probability fields, which GammonView filled with the cube
    decision's pre-roll probabilities -- the only probabilities that describe
    the decision, since the checker ply's own eval is post-roll -- while the
    reference writer left them zero. Upstream adopted that in the 2026-08-10
    sync ("`probs` carry the evaluation behind them when the producer supplied
    one"), so the two writers now agree and the tolerance is gone. Both the
    values and the byte layout are checked, so a regression on either side
    fails here rather than silently reopening the gap.
    """
    if len(ours) != len(ref):
        return False
    if ours == ref:
        return True
    # Not identical: say where, in entry terms, for the failure report.
    return _decode_cube_entries(ours) == _decode_cube_entries(ref)


def _game_base_equal(ours: bytes, ref: bytes) -> bool:
    """True if `ours` GAME chunk bytes are byte-identical to `ref`'s. With the
    per-game trailing met_value gone (anchors moved to GVAN), GAME once again
    matches the reference codec exactly."""
    return ours == ref


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if ogxm_ctypes is None:
        print(_OGXM_SKIP)
        return 0

    if MAT_PATH is None:
        print(missing_mat_message())
        return 0

    print(f"Analyzing {MAT_PATH} (preset=very_quick)...")
    result = analyze_mat(MAT_PATH, preset="very_quick", quiet=True)
    ogxm = to_ogxm_json(result)
    print(f"{len(ogxm['games'])} games, "
          f"{sum(len(g['plies']) for g in ogxm['games'])} plies total.\n")

    our_bytes = write_gvab(ogxm)
    print(f"write_gvab() -> {len(our_bytes)} bytes\n")

    # =======================================================================
    # 1. libogxm reads our bytes back
    # =======================================================================
    print("--- 1. libogxm reads write_gvab() output ---")
    decoded = None
    try:
        decoded = json.loads(ogxm_ctypes.binary_to_json(our_bytes))
        check(True, "1. libogxm.ogxm_binary_to_json accepted write_gvab() output")
    except Exception as e:  # noqa: BLE001
        check(False, f"1. libogxm.ogxm_binary_to_json accepted write_gvab() output ({e})")

    if decoded is not None:
        check(decoded.get("match_length") == ogxm.get("match_length"), "1b. match_length round-trips")
        check(decoded.get("player_white") == ogxm.get("player_white"), "1c. player_white round-trips")
        check(decoded.get("player_black") == ogxm.get("player_black"), "1d. player_black round-trips")
        check(len(decoded.get("games", [])) == len(ogxm.get("games", [])), "1e. game count round-trips")

        d_games = decoded.get("games", [])
        o_games = ogxm.get("games", [])
        all_ply_counts_ok = len(d_games) == len(o_games) and all(
            len(dg.get("plies", [])) == len(og.get("plies", [])) for dg, og in zip(d_games, o_games)
        )
        check(all_ply_counts_ok, "1f. per-game ply counts round-trip")

        # Sample EVAL/ALTS/CUBE values across the match.
        sampled = 0
        for og, dg in zip(o_games, d_games):
            for op, dp in zip(og.get("plies", []), dg.get("plies", [])):
                oa, da = op.get("analysis"), dp.get("analysis")
                if not isinstance(oa, dict) or not isinstance(da, dict):
                    continue
                if "best_equity" in oa:
                    check(abs(da.get("best_equity", 1e9) - oa["best_equity"]) < 0.0005,
                          f"1g. EVAL best_equity round-trips (~{oa['best_equity']:.4f})")
                    if oa.get("alternatives") and da.get("alternatives"):
                        check(abs(da["alternatives"][0].get("equity", 1e9)
                                  - oa["alternatives"][0]["equity"]) < 0.0005,
                              "1h. ALTS[0] equity round-trips")
                elif "correct_action" in oa:
                    check(da.get("correct_action") == oa["correct_action"],
                          f"1i. CUBE correct_action round-trips ({oa['correct_action']!r})")
                    check(abs(da.get("no_double_equity", 1e9) - oa.get("no_double_equity", 0.0)) < 0.0005,
                          "1j. CUBE no_double_equity round-trips")
                sampled += 1
                if sampled >= 25:
                    break
            if sampled >= 25:
                break
        check(sampled > 0, "1k. sampled at least one EVAL/CUBE decision")
        print(f"   sampled {sampled} decisions")
    print()

    # =======================================================================
    # 2. Base-chunk byte parity vs libogxm's own writer
    # =======================================================================
    print("--- 2. Base-chunk byte parity vs ogxm_json_to_binary ---")
    ref_bytes = ogxm_ctypes.json_to_binary(json.dumps(ogxm))
    print(f"   reference (libogxm) -> {len(ref_bytes)} bytes")

    ours_chunks = _parse_chunks(our_bytes)
    ref_chunks = _parse_chunks(ref_bytes)

    for label, ctype in (("ANAL", CHUNK_ANAL), ("EVAL", CHUNK_EVAL), ("ALTS", CHUNK_ALTS)):
        ours = ours_chunks.get(ctype, [])
        ref = ref_chunks.get(ctype, [])
        ok = ours == ref
        check(ok, f"2. {label} chunk byte-identical to libogxm ({len(ours)} instance(s))")
        if not ok:
            for i, (o, r) in enumerate(zip(ours, ref)):
                if o != r:
                    _report_mismatch(f"{label}[{i}]", o, r)
            if len(ours) != len(ref):
                print(f"      {label}: {len(ours)} chunks (ours) vs {len(ref)} chunks (ref)")

    # CUBE: identical except the missed-double probabilities the reference
    # writer has no JSON field to fill (see _cube_base_equal).
    ours_cube = ours_chunks.get(CHUNK_CUBE, [])
    ref_cube = ref_chunks.get(CHUNK_CUBE, [])
    cube_ok = (len(ours_cube) == len(ref_cube)
               and all(_cube_base_equal(o, r) for o, r in zip(ours_cube, ref_cube)))
    check(cube_ok, "2. CUBE chunk byte-identical to libogxm, missed-double probs "
                   f"included ({len(ours_cube)} instance(s))")
    if not cube_ok:
        for i, (o, r) in enumerate(zip(ours_cube, ref_cube)):
            if o != r:
                _report_mismatch(f"CUBE[{i}]", o, r)
                _diff_cube_entries(o, r)
        if len(ours_cube) != len(ref_cube):
            print(f"      CUBE: {len(ours_cube)} chunks (ours) vs {len(ref_cube)} chunks (ref)")

    # MHDR: no longer byte-identical -- ours carries the documented
    # GammonView metadata (flags bits 2-3, cube_limit, trailing event).
    ours_mhdr_list = ours_chunks.get(CHUNK_MHDR, [])
    ref_mhdr_list = ref_chunks.get(CHUNK_MHDR, [])
    mhdr_ok = (
        len(ours_mhdr_list) == len(ref_mhdr_list) == 1
        and _mhdr_base_equal(ours_mhdr_list[0], ref_mhdr_list[0])
    )
    check(mhdr_ok, "2. MHDR chunk identical to libogxm except documented GammonView metadata bytes")
    if not mhdr_ok and ours_mhdr_list and ref_mhdr_list:
        _report_mismatch("MHDR", ours_mhdr_list[0], ref_mhdr_list[0])

    # CUBE type=4 (live_checker) / type=2 (missed_double) are the key new
    # check: they must round-trip as ordinary CUBE entries (not be diverted
    # into a GammonView-only GVAN section). Cross-check the per-type entry
    # counts against what ogxm's own missed_double/cube_decision sub-objects
    # imply, independent of gvab's internals.
    ours_cube_blob = b"".join(ours_chunks.get(CHUNK_CUBE, []))
    ref_cube_blob = b"".join(ref_chunks.get(CHUNK_CUBE, []))
    ours_type_counts = _cube_type_counts(ours_cube_blob)
    ref_type_counts = _cube_type_counts(ref_cube_blob)
    check(ours_type_counts == ref_type_counts,
          f"2. CUBE per-type entry counts match libogxm ({ours_type_counts})")

    expected_md_count = expected_lc_count = 0
    for g in ogxm.get("games", []):
        for ply in g.get("plies", []):
            a = ply.get("analysis")
            if isinstance(a, dict):
                if isinstance(a.get("missed_double"), dict):
                    expected_md_count += 1
                elif isinstance(a.get("cube_decision"), dict):
                    expected_lc_count += 1
    check(ours_type_counts.get(CUBE_TYPE_MISSED_DOUBLE, 0) == expected_md_count,
          f"2. CUBE type=2 (missed_double) count matches ogxm ({expected_md_count})")
    check(ours_type_counts.get(CUBE_TYPE_LIVE_CHECKER, 0) == expected_lc_count,
          f"2. CUBE type=4 (live_checker) count matches ogxm ({expected_lc_count})")
    check(expected_md_count > 0 and expected_lc_count > 0,
          f"2. sample match exercises both CUBE type=2 and type=4 "
          f"(md={expected_md_count}, lc={expected_lc_count})")

    # GAME: byte-identical to libogxm again -- the per-game trailing met_value
    # is gone (equity<->MWC anchors moved to per-decision GVAN records).
    ours_games = ours_chunks.get(CHUNK_GAME, [])
    ref_games = ref_chunks.get(CHUNK_GAME, [])
    check(len(ours_games) == len(ref_games), f"2. GAME chunk count matches ({len(ours_games)})")
    all_games_ok = True
    for gi, (og, rg) in enumerate(zip(ours_games, ref_games)):
        ok = _game_base_equal(og, rg)
        all_games_ok &= ok
        if not ok:
            _report_mismatch(f"GAME[{gi}]", og, rg)
    check(all_games_ok,
          f"2. all {len(ours_games)} GAME chunks byte-identical to libogxm")
    print()

    # =======================================================================
    # 3. GVAN self-check
    # =======================================================================
    print("--- 3. GVAN self-check ---")
    gvan_list = ours_chunks.get(CHUNK_GVAN, [])
    check(len(gvan_list) == 1, "3. exactly one GVAN chunk present")

    if gvan_list:
        gvan = gvan_list[0]
        gvan_version, base_level, section_flags, luck_level = struct.unpack_from("<BBBB", gvan, 0)
        check(gvan_version == GVAN_VERSION, f"3. gvan_version == {GVAN_VERSION}")

        ai = ogxm.get("analysis_info", {}) or {}
        expected_base = _encode_eval_level(ai.get("eval_level"))
        check(base_level == expected_base,
              f"3. base_eval_level == {expected_base} (eval_level={ai.get('eval_level')!r}); got {base_level}")
        expected_luck = _encode_eval_level(ai.get("luck_eval_level")) if ai.get("luck_eval_level") else 0x01
        check(luck_level == expected_luck,
              f"3. luck_eval_level == {expected_luck} (luck_eval_level={ai.get('luck_eval_level')!r}); got {luck_level}")

        # Independently re-derive expectations straight from ogxm's per-ply
        # `analysis` objects (not from gvab's internal builders). Cube-eval
        # entries are appended in the same ply-traversal order write_gvab
        # uses, so this list lines up 1:1 with the base CUBE chunk -- which
        # now includes type=2 (missed_double) and type=4 (cube_decision)
        # entries interleaved with the standalone type=0/1/3 ones.
        expected_checker = []   # (decision, has_luck, illegal_move, luck)
        expected_alt_levels = []
        expected_cube = []      # (decision, eval_level)

        for g in ogxm.get("games", []):
            for ply in g.get("plies", []):
                action_id = ply.get("action_id")
                analysis = ply.get("analysis")
                if action_id is not None and 0 <= action_id <= 20 and isinstance(analysis, dict):
                    has_luck = "luck" in analysis
                    expected_checker.append((
                        bool(analysis.get("decision", False)),
                        has_luck,
                        bool(analysis.get("illegal_move", False)),
                        analysis.get("luck", 0.0) if has_luck else 0.0,
                    ))
                    for alt in (analysis.get("alternatives") or [])[:50]:
                        expected_alt_levels.append(alt.get("eval_level"))
                    md = analysis.get("missed_double")
                    cd = analysis.get("cube_decision")
                    if isinstance(md, dict):
                        # Not `True`. A missed double is very nearly always a
                        # real decision -- but not when the cube was trivial and
                        # the margin under 0.001, which is a ply the player is
                        # right to have skipped past. Nothing sets
                        # `missed_double["decision"]` (see bgf.py's note), so the
                        # writer derives the bit every reader will compute; this
                        # asserts it wrote that one and not an assumption.
                        expected_cube.append((
                            _missed_double_counts(
                                float(md.get("no_double_equity", 0.0) or 0.0),
                                float(md.get("double_take_equity", 0.0) or 0.0),
                                float(md.get("double_pass_equity", 0.0) or 0.0),
                            ),
                            md.get("eval_level"),
                        ))
                    elif isinstance(cd, dict):
                        expected_cube.append((bool(cd.get("decision", False)), cd.get("eval_level")))
                elif action_id in (21, 22, 23) and isinstance(analysis, dict):
                    expected_cube.append((bool(analysis.get("decision", False)), analysis.get("eval_level")))
                elif action_id in (27, 28) and isinstance(analysis, dict):
                    expected_cube.append((bool(analysis.get("decision", True)), None))

        pos = 4
        checker_ok = True
        for idx, (dec, hluck, illegal, luck) in enumerate(expected_checker):
            flags, luck_raw = struct.unpack_from("<Bh", gvan, pos)
            pos += GVAN_CHECKER_REC
            ok = (bool(flags & 0x01) == dec
                  and bool(flags & 0x02) == hluck and bool(flags & 0x04) == illegal)
            if hluck:
                ok = ok and luck_raw == _enc_equity(luck)
            checker_ok &= ok
            if not ok and idx < 5:
                print(f"      checker[{idx}] flags=0x{flags:02x} expected dec={dec} "
                      f"hluck={hluck} illegal={illegal}; luck_raw={luck_raw} vs {_enc_equity(luck)}")
        check(checker_ok, f"3. checker section ({len(expected_checker)} v3 records) matches ogxm")

        alt_ok = True
        for idx, lvl in enumerate(expected_alt_levels):
            (b,) = struct.unpack_from("<B", gvan, pos)
            pos += 1
            expected_b = _encode_eval_level(lvl)
            alt_ok &= (b == expected_b)
        check(alt_ok, f"3. alt eval_level section ({len(expected_alt_levels)} bytes) matches ogxm analysis")

        cube_ok = True
        for idx, (dec, lvl) in enumerate(expected_cube):
            flags, evlvl = struct.unpack_from("<BB", gvan, pos)
            pos += GVAN_CUBE_REC
            ok = bool(flags & 0x01) == dec and evlvl == _encode_eval_level(lvl)
            cube_ok &= ok
            if not ok and idx < 5:
                print(f"      cube[{idx}] flags=0x{flags:02x} expected dec={dec}; "
                      f"eval_level={evlvl} vs {_encode_eval_level(lvl)}")
        check(cube_ok, f"3. cube section ({len(expected_cube)} v3 records) matches ogxm")

        # Cube-eval count sanity: expected_cube must equal num_cube_evals,
        # i.e. every type=0/1/2/3/4 CUBE entry got exactly one GVAN
        # cube record -- confirms the section really is aligned to *all*
        # CUBE entries now, not just the pre-reconciliation type=0/1 subset.
        ours_cube_blob = ours_chunks.get(CHUNK_CUBE, [b""])[0] if ours_chunks.get(CHUNK_CUBE) else b""
        num_cube_entries = len(ours_cube_blob) // 28
        check(len(expected_cube) == num_cube_entries,
              f"3. GVAN cube section count ({len(expected_cube)}) == CUBE entry count ({num_cube_entries})")

        # No no-double section any more -- section_flags bit 3 (formerly
        # "nodouble") no longer exists; confirm the chunk is fully consumed
        # right after the cube section.
        check(pos == len(gvan), f"3. GVAN chunk fully consumed ({pos} == {len(gvan)} bytes)")
    print()

    # =======================================================================
    # 4. GammonView match/game metadata round-trip
    # =======================================================================
    print("--- 4. GammonView metadata round-trip (real match) ---")
    decoded_mhdr_gv = _decode_mhdr_gv(ours_chunks[CHUNK_MHDR][0])
    check(decoded_mhdr_gv["beaver"] == bool(ogxm.get("beaver")),
          f"4. beaver round-trips ({decoded_mhdr_gv['beaver']!r})")
    check(decoded_mhdr_gv["cube_limit"] == (int(ogxm.get("cube_limit", 0) or 0) & 0xFFFF),
          f"4. cube_limit round-trips ({decoded_mhdr_gv['cube_limit']})")
    check(decoded_mhdr_gv["event"] == ogxm.get("event"),
          f"4. event round-trips ({decoded_mhdr_gv['event']!r})")
    check(decoded_mhdr_gv["site"] == ogxm.get("site"),
          f"4. site round-trips ({decoded_mhdr_gv['site']!r})")
    print()

    print("--- 4b. Synthetic metadata (beaver/raccoon/cube_limit/event/site) ---")
    synthetic = {
        "match_length": 5,
        "player_white": "Alice",
        "player_black": "Bob",
        "crawford": True,
        "jacoby": False,
        "beaver": True,
        "raccoon": True,
        "cube_limit": 64,
        "event": "Synthetic Test Event",
        "site": "Somewhere",
        "white_score": 1,
        "black_score": 2,
        "result": 0,
        "source": 0,
        "timestamp": 1234567890,
        "games": [
            {
                "game_index": 0, "points_won": 0, "is_crawford": False,
                "is_lastgame": False, "first_to_move": 0,
                "plies": [],
            },
            {
                "game_index": 1, "points_won": 0, "is_crawford": False,
                "is_lastgame": True, "first_to_move": 1,
                "plies": [],
            },
        ],
    }
    synth_bytes = write_gvab(synthetic)

    try:
        json.loads(ogxm_ctypes.binary_to_json(synth_bytes))
        check(True, "4b. libogxm accepts write_gvab() output with non-zero "
                     "beaver/raccoon/cube_limit/event/site bytes (critical tolerance check)")
    except Exception as e:  # noqa: BLE001
        check(False, f"4b. libogxm accepts write_gvab() output with non-zero "
                      f"beaver/raccoon/cube_limit/event/site bytes (critical tolerance check) ({e})")

    synth_chunks = _parse_chunks(synth_bytes)
    synth_mhdr_gv = _decode_mhdr_gv(synth_chunks[CHUNK_MHDR][0])
    check(synth_mhdr_gv["beaver"] is True, "4b. synthetic beaver round-trips")
    check(synth_mhdr_gv["raccoon"] is True, "4b. synthetic raccoon round-trips")
    check(synth_mhdr_gv["cube_limit"] == 64,
          f"4b. synthetic cube_limit round-trips ({synth_mhdr_gv['cube_limit']})")
    check(synth_mhdr_gv["event"] == synthetic["event"],
          f"4b. synthetic event round-trips ({synth_mhdr_gv['event']!r})")
    check(synth_mhdr_gv["site"] == synthetic["site"],
          f"4b. synthetic site round-trips ({synth_mhdr_gv['site']!r})")

    synth_games = synth_chunks.get(CHUNK_GAME, [])
    synth_ref_bytes = ogxm_ctypes.json_to_binary(json.dumps(synthetic))
    synth_ref_chunks = _parse_chunks(synth_ref_bytes)
    check(_mhdr_base_equal(synth_chunks[CHUNK_MHDR][0], synth_ref_chunks[CHUNK_MHDR][0]),
          "4b. synthetic MHDR identical to libogxm except documented GammonView metadata bytes")
    synth_ref_games = synth_ref_chunks.get(CHUNK_GAME, [])
    synth_games_ok = len(synth_games) == len(synth_ref_games) == 2 and all(
        _game_base_equal(o, r) for o, r in zip(synth_games, synth_ref_games)
    )
    check(synth_games_ok,
          "4b. synthetic GAME chunks byte-identical to libogxm")
    print()

    print(f"{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
