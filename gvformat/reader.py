# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Pure-Python OGXM binary READER: ``.gvab`` bytes -> OGXM-JSON dict.

``read_gvab(data)`` is the inverse of ``gvab.write_gvab``: it parses the
compact OGXM binary back into a dict shaped like
``ogxm_export.to_ogxm_json(...)`` produces. Pure stdlib -- no bgsage/engine
calls, no C++ extension, no libogxm.

Design contract
---------------
The reader is defined as the byte-level inverse of the writer:

    write_gvab(read_gvab(b)) == b

for any ``b`` that ``write_gvab`` produced. That round-trip is the correctness
property the test harness checks, and it's what makes this reader trustworthy
without needing the reference C++ codec on hand.

On a file from *another* producer the equality does not hold, and should not be
expected to: a rewrite **canonicalizes**. It reaches a fixed point after one
pass (``write_gvab(read_gvab(x))`` is byte-stable thereafter) and preserves the
decoded content exactly, but it compacts any slack the foreign writer left --
e.g. a corpus ``.gvab`` here loses 823 bytes on first rewrite while decoding to
an identical dict. Compare decoded dicts, not bytes, when the input is not ours.

Per-ply ``ogid_before`` / ``ogid_after`` position strings are *derived*, not
stored (same as in ``ogxm_export``). ``read_gvab`` reconstructs them by
replaying the game: it rebuilds each board by applying the compact move steps
from the standard opening position, reconstructs per-game start scores by
accumulating ``points_won``, and runs the same OGID turn-phase state machine
(``ogxm_export._TurnState`` / ``_ogid``) the exporter uses -- yielding OGIDs
byte-identical to ``to_ogxm_json`` (and thus to ``libogxm``). Pass
``derive_ogids=False`` to skip this pass.

What the binary does NOT carry (so the reader cannot reproduce it):

  - ``analysis_info.preset`` (not serialized).
  - Alternative ``notation`` / ``diff`` (derived display fields).
  - The exact ``eval``/``probs`` *presence* on an entry whose probabilities
    all round to zero (the binary stores a zero-filled prob block either way);
    the reader treats an all-zero prob block on an alternative or cube record
    as "no eval". A checker decision always gets an ``eval`` block, matching
    ``to_ogxm_json`` (a real position never has an exactly-zero win prob).

Everything the writer reads back out of the dict *is* reconstructed, so those
omissions don't break the round-trip: the writer re-derives ``ogid_*`` from
board state (not from this dict), recomputes ``analysis_info`` bookkeeping,
ignores ``notation``/``diff``, and re-emits a zero prob block for an absent
``eval``.

The on-wire layout is documented in ``OGXM_FORMAT_SPEC_GAMMONVIEW.md`` and,
authoritatively, in ``gvab.py`` (whose encoders this module mirrors field for
field). Constants and the dice/action tables are imported from ``gvab`` so the
two stay in lockstep.
"""

from __future__ import annotations

import base64
import struct
import zlib

from .binary import (
    write_gvab,
    OGXM_MAGIC, END_MAGIC, VERSION_MAJOR, VERSION_MINOR,
    CHUNK_MHDR, CHUNK_GAME, CHUNK_ANAL, CHUNK_EVAL, CHUNK_ALTS, CHUNK_CUBE,
    CHUNK_GVAN, CHUNK_CSUM, CHUNK_FLAG_CRITICAL,
    ACTION_SET_POSITION, SET_POSITION_DICE_FLAG,
    CUBE_TYPE_MISSED_DOUBLE,
    CUBE_TYPE_RESIGN, CUBE_TYPE_LIVE_CHECKER,
    CUBE_ACTION_DOUBLE,
    DICE_TABLE, DEFAULT_BOARD_SENTINEL,
    cap_points_won,
)
from .basefill import complete_base_block
from .place import split_place

# Chunks this reader decodes into the OGXM dict. Everything else is carried
# through verbatim so a read/write cycle does not drop it (see _unknown_chunks).
_DECODED_CHUNKS = frozenset({
    CHUNK_MHDR, CHUNK_GAME, CHUNK_ANAL, CHUNK_EVAL, CHUNK_ALTS, CHUNK_CUBE,
    CHUNK_GVAN, CHUNK_CSUM,
})


def _chunk_name(type_code: int) -> str:
    """The chunk's four ASCII characters, for messages; hex if not printable."""
    raw = struct.pack("<I", type_code & 0xFFFFFFFF)
    if all(0x20 <= b < 0x7F for b in raw):
        return raw.decode("ascii")
    return f"0x{type_code:08X}"


# action_code (0-3) -> OGXM cube-action label; inverse of gvab._CUBE_ACTION_CODES.
_ACTION_NAMES = {0: "no_double", 1: "double", 2: "take", 3: "pass"}

_EVAL_ENTRY_FMT = "<BHHHHHHhHBBB"
_EVAL_ENTRY_SIZE = struct.calcsize(_EVAL_ENTRY_FMT)   # 20
_ALT_ENTRY_FMT = "<4sh5HB"
_ALT_ENTRY_SIZE = struct.calcsize(_ALT_ENTRY_FMT)     # 17
_CUBE_ENTRY_FMT = "<BHBhhhHHHHHHBBB3s"
_CUBE_ENTRY_SIZE = struct.calcsize(_CUBE_ENTRY_FMT)   # 28


class GvabError(ValueError):
    """Raised when the input is not a well-formed ``.gvab`` stream."""


# Terminal action ids (game/match end, resign, forfeit, null) -- plies that
# carry no move and leave the turn state untouched.
_TERMINAL_ACTIONS = frozenset({24, 25, 26, 27, 28, 29, 30})


# ---------------------------------------------------------------------------
# Small decoders (inverses of gvab's fixed-point / bit-flag encoders)
# ---------------------------------------------------------------------------

def _decode_eval_level(b: int) -> str | None:
    """Inverse of ``gvab._encode_eval_level``. 0 -> None (== "same as base")."""
    if b == 0:
        return None
    if b & 0x20:
        return "rollout"
    if b & 0x40:
        return "database"
    if b & 0x10:
        return f"truncated{b & 0x0F}"
    return f"{b & 0x0F}ply"


def _eval_from_probs(win, gwin, bgwin, gloss, bgloss) -> dict:
    """flat probs -> named Eval object (matches ogxm_export._probs_to_eval)."""
    equity = win + gwin + bgwin - gloss - bgloss
    return {
        "win": win, "gammon_win": gwin, "bg_win": bgwin,
        "gammon_loss": gloss, "bg_loss": bgloss, "equity": round(equity, 4),
    }


def _cube_action_label(should_double: bool, double_take_equity: float,
                       double_pass_equity: float) -> str:
    """Inverse-side reimplementation of ogxm_export._cube_action_label (the
    display-only response label; not stored, derived from the equities).

    Take when the take equity is *strictly* below the pass equity; an exact tie
    is a pass, which is the engine's own rule (``cube_opponent_takes``) and the
    base spec's. The tie is not hypothetical: these equities are quantised to
    1e-4 on the way into the binary, so any pair within 5e-5 arrives as one
    value. The spelling stays ours -- ``double_take``, not the reference's
    ``double/take`` -- because it is a display label nothing computes on, and it
    is what our own ``.gva`` specification documents.
    """
    if not should_double:
        return "no_double"
    return "double_take" if double_take_equity < double_pass_equity else "double_pass"


def _q(raw: int) -> float:
    """Fixed-point (x/10000) -> float."""
    return raw / 10000.0


def _read_pascal(data: bytes, pos: int) -> tuple[str, int]:
    """Read a uint8-length-prefixed UTF-8 string; returns (value, new_pos)."""
    n = data[pos]
    pos += 1
    s = data[pos:pos + n].decode("utf-8", errors="replace")
    return s, pos + n


# ---------------------------------------------------------------------------
# Chunk-stream walker
# ---------------------------------------------------------------------------

def _walk_chunks(data: bytes) -> tuple[list[tuple[int, bytes, int]], int]:
    """Return (chunks, csum_chunk_start), preserving order. ``chunks`` is a
    list of (type_code, chunk_data, flags); ``csum_chunk_start`` is the byte
    offset of the CSUM chunk header (for CRC verification), or -1 if absent."""
    n = len(data)
    if n < 28:
        raise GvabError(f"file too small ({n} bytes) to be a valid .gvab stream")
    pos = 20  # skip the 20-byte file header
    chunks: list[tuple[int, bytes, int]] = []
    csum_start = -1
    while True:
        remaining = n - pos
        if remaining == 8:
            break  # End Marker
        if remaining < 8:
            raise GvabError(f"truncated stream at offset {pos} ({remaining} bytes left)")
        if pos + 12 > n:
            raise GvabError(f"truncated chunk header at offset {pos}")
        ctype, clen, cflags, _res = struct.unpack_from("<IIHH", data, pos)
        if ctype == CHUNK_CSUM:
            csum_start = pos
        body_start = pos + 12
        if body_start + clen > n:
            raise GvabError(f"chunk 0x{ctype:08X} at {pos} claims {clen} bytes, past EOF")
        chunks.append((ctype, data[body_start:body_start + clen], cflags))
        pos = body_start + clen
    return chunks, csum_start


# ---------------------------------------------------------------------------
# MHDR
# ---------------------------------------------------------------------------

def _decode_mhdr(mhdr: bytes) -> dict:
    (match_length, _num_games, flags, white_score, black_score, result, source,
     timestamp, _total_plies) = struct.unpack_from("<HBBHHBBIH", mhdr, 0)
    cube_limit = struct.unpack_from("<H", mhdr, 16)[0]  # reserved[0:2]

    pos = 24
    player_white, pos = _read_pascal(mhdr, pos)
    player_black, pos = _read_pascal(mhdr, pos)
    event = site = None
    if pos < len(mhdr):
        ev, pos = _read_pascal(mhdr, pos)
        event = ev or None
    if pos < len(mhdr):
        st, pos = _read_pascal(mhdr, pos)
        site = st or None
    if event is not None and site is None:
        # Written before event/site were separate fields: one combined string
        # in the `event` slot. Recover the pair so old files read the same as
        # new ones (a string with no separator is all event -- see place.py).
        event, site = split_place(event)

    return {
        "match_length": match_length,
        "player_white": player_white,
        "player_black": player_black,
        "white_score": white_score,
        "black_score": black_score,
        "result": result,
        "source": source,
        "timestamp": timestamp,
        "crawford": bool(flags & 0x01),
        "jacoby": bool(flags & 0x02),
        "beaver": bool(flags & 0x04),
        "raccoon": bool(flags & 0x08),
        "cube_limit": cube_limit,
        "event": event,
        "site": site,
    }


# ---------------------------------------------------------------------------
# GAME (header + initial board + ply records + trailing met_value)
# ---------------------------------------------------------------------------

def _decode_ply(data: bytes, pos: int) -> tuple[dict, int]:
    """Parse one ply record; returns (ply, new_pos). Exact inverse of
    ``gvab._encode_ply``."""
    if pos >= len(data):
        raise GvabError(f"ply record truncated: offset {pos} past end of GAME chunk ({len(data)})")
    first = data[pos]
    action_id = first & 0x1F
    color = (first >> 5) & 0x01

    if action_id == ACTION_SET_POSITION:
        has_dice = bool(first & SET_POSITION_DICE_FLAG)
        pos += 1
        ply: dict = {"color": color, "action_id": action_id}
        if has_dice:
            dice_byte = data[pos]
            pos += 1
            ply["d1"] = dice_byte & 0x0F
            ply["d2"] = (dice_byte >> 4) & 0x0F
        board = list(struct.unpack_from("<26b", data, pos))
        pos += 26
        ply["set_position"] = board
        return ply, pos

    if 0 <= action_id <= 20:
        d1, d2, n_moves = DICE_TABLE[action_id]
        pos += 1
        steps = data[pos:pos + n_moves]
        pos += n_moves
        # A zero step byte is padding (a real move always has pips 1-6, so its
        # byte is never 0); decode only the non-padding steps, in order.
        moves = [{"from": b & 0x1F, "pips": (b >> 5) & 0x07} for b in steps if b != 0]
        return {"color": color, "action_id": action_id, "d1": d1, "d2": d2, "moves": moves}, pos + 0

    # Actions 21-30 (double/take/drop/game-end/resign/null): first byte only.
    return {"color": color, "action_id": action_id}, pos + 1


def _decode_game(game: bytes) -> tuple[dict, int]:
    """Parse a GAME chunk; returns (game_dict, game_index). The game_index is
    also returned separately so callers can key analysis entries by it."""
    (game_index, num_plies, winner, points_won, gflags, first_to_move,
     _reserved) = struct.unpack_from("<BHBBBBB", game, 0)
    pos = 8

    initial_board = None
    if game[pos] == DEFAULT_BOARD_SENTINEL:
        pos += 2  # [0xFF][0xFF] default sentinel
    else:
        initial_board = list(struct.unpack_from("<26b", game, pos))
        pos += 27  # 26 signed points + 1 pad byte

    plies: list[dict] = []
    for _ in range(num_plies):
        ply, pos = _decode_ply(game, pos)
        plies.append(ply)

    # GAME carries no trailing bytes now (met_value was replaced by the
    # per-decision GVAN anchors), so parsing ends exactly at the last ply.
    game_obj: dict = {
        "game_index": game_index,
        "winner": winner,
        "points_won": points_won,
        "is_crawford": bool(gflags & 0x01),
        "is_lastgame": bool(gflags & 0x02),
        "first_to_move": first_to_move,
        "plies": plies,
    }
    if initial_board is not None:
        game_obj["initial_board"] = initial_board
    return game_obj, game_index


# ---------------------------------------------------------------------------
# Analysis chunks (ANAL / EVAL / ALTS / CUBE) + GVAN
# ---------------------------------------------------------------------------

def _decode_anal(anal: bytes) -> dict:
    (ply, _res1, num_checker, num_cube, timestamp, duration_ms,
     _res2, _res8) = struct.unpack_from("<BBHHIIH8s", anal, 0)
    model_id, _ = _read_pascal(anal, struct.calcsize("<BBHHIIH8s"))
    return {
        "ply": ply,
        "num_checker": num_checker,
        "num_cube": num_cube,
        "timestamp": timestamp,
        "duration_ms": duration_ms,
        "model_id": model_id,
    }


def _decode_eval_entries(eval_data: bytes) -> list[dict]:
    out = []
    for off in range(0, len(eval_data) - _EVAL_ENTRY_SIZE + 1, _EVAL_ENTRY_SIZE):
        (gi, pi, win, gwin, bgwin, gloss, bgloss, best_eq, eq_loss,
         num_alts, ply, flags) = struct.unpack_from(_EVAL_ENTRY_FMT, eval_data, off)
        out.append({
            "game_index": gi, "ply_index": pi,
            "probs": (_q(win), _q(gwin), _q(bgwin), _q(gloss), _q(bgloss)),
            "best_equity": _q(best_eq), "equity_loss": _q(eq_loss),
            "num_alts": num_alts, "ply": ply,
            "has_missed_double": bool(flags & 0x01),
        })
    return out


def _decode_alt_entries(alts_data: bytes) -> list[dict]:
    out = []
    for off in range(0, len(alts_data) - _ALT_ENTRY_SIZE + 1, _ALT_ENTRY_SIZE):
        (move_bytes, eq, win, gwin, bgwin, gloss, bgloss,
         flags) = struct.unpack_from(_ALT_ENTRY_FMT, alts_data, off)
        move = [{"from": b & 0x1F, "pips": (b >> 5) & 0x07} for b in move_bytes if b != 0]
        out.append({
            "move": move, "equity": _q(eq),
            "probs": (_q(win), _q(gwin), _q(bgwin), _q(gloss), _q(bgloss)),
            "is_played": bool(flags & 0x01),
        })
    return out


def _decode_cube_entries(cube_data: bytes) -> list[dict]:
    out = []
    for off in range(0, len(cube_data) - _CUBE_ENTRY_SIZE + 1, _CUBE_ENTRY_SIZE):
        (gi, pi, ctype, nd, dt, dp, win, gwin, bgwin, gloss, bgloss,
         eq_loss, corr, played, ply, _res) = struct.unpack_from(_CUBE_ENTRY_FMT, cube_data, off)
        out.append({
            "game_index": gi, "ply_index": pi, "type": ctype,
            "no_double_equity": _q(nd), "double_take_equity": _q(dt),
            "double_pass_equity": _q(dp),
            "probs": (_q(win), _q(gwin), _q(bgwin), _q(gloss), _q(bgloss)),
            "equity_loss": _q(eq_loss),
            "correct_action": corr, "played_action": played, "ply": ply,
        })
    return out


def _decode_gvan(gvan: bytes, num_checker: int, num_alts: int, num_cube: int) -> dict:
    """Parse a GVAN chunk (v3, or v2 for files written before the shrink).

    v3 dropped the two blanked uint16 anchor slots from each checker and cube
    record (7->3 and 6->2 bytes). They held per-decision ``mwc_on_win`` /
    ``mwc_on_loss`` in v1; v2 zeroed them when MWC became compute-on-read but
    kept the width. Record widths are the only difference, so both versions
    decode to the same shape.

    v1 is not supported (its slots hold live anchors, not zeros). An unknown
    version is rejected rather than guessed at: the sections are positional and
    fixed-width, so misreading the width silently yields plausible garbage.
    """
    version, base_level, section_flags, luck_level = struct.unpack_from("<BBBB", gvan, 0)
    if version == 3:
        chk_fmt, chk_w, cube_fmt, cube_w = "<Bh", 3, "<BB", 2
    elif version == 2:
        chk_fmt, chk_w, cube_fmt, cube_w = "<BhHH", 7, "<BBHH", 6
    else:
        raise GvabError(
            f"unsupported GVAN version {version} (this reader handles 2 and 3)")
    pos = 4

    checker: list[dict] = []
    if section_flags & 0x01:
        for _ in range(num_checker):
            flags, luck = struct.unpack_from(chk_fmt, gvan, pos)[:2]
            pos += chk_w
            checker.append({
                "decision": bool(flags & 0x01),
                "has_luck": bool(flags & 0x02),
                "illegal_move": bool(flags & 0x04),
                "luck": _q(luck),
            })

    alt_levels: list[int] = []
    if section_flags & 0x02:
        alt_levels = list(gvan[pos:pos + num_alts])
        pos += num_alts

    cube: list[dict] = []
    if section_flags & 0x04:
        for _ in range(num_cube):
            flags, evlvl = struct.unpack_from(cube_fmt, gvan, pos)[:2]
            pos += cube_w
            cube.append({"decision": bool(flags & 0x01), "eval_level": evlvl})

    return {
        "version": version,
        "base_eval_level": base_level,
        "luck_eval_level": luck_level,
        "checker": checker,
        "alt_levels": alt_levels,
        "cube": cube,
    }


# ---------------------------------------------------------------------------
# Analysis reassembly
# ---------------------------------------------------------------------------

def _build_alt(alt: dict, level_byte: int) -> dict:
    out = {"move": alt["move"], "equity": alt["equity"], "is_played": alt["is_played"]}
    if any(alt["probs"]):
        out["eval"] = _eval_from_probs(*alt["probs"])
    lvl = _decode_eval_level(level_byte)
    if lvl is not None:
        out["eval_level"] = lvl
    return out


def _build_checker_analysis(ev: dict, gv: dict | None,
                            alts: list[dict], alt_levels: list[int]) -> dict:
    best_equity = ev["best_equity"]
    equity_loss = ev["equity_loss"]
    analysis: dict = {
        # EVAL's five probabilities are the *best move's* resulting position,
        # which is also the first alternative's -- so a producer that fills
        # one and not the other has still said it. All-zero means "not
        # recorded" rather than "0% to win" here exactly as it does on a cube
        # record, and the fallback below costs nothing when the field is
        # populated.
        "eval": _eval_from_probs(*ev["probs"]),
        "best_equity": best_equity,
        "played_equity": round(best_equity - equity_loss, 4),
        "equity_loss": equity_loss,
        "decision": bool(gv["decision"]) if gv else False,
        "alternatives": [_build_alt(a, lvl) for a, lvl in zip(alts, alt_levels)],
    }
    if not any(ev["probs"]):
        alternatives = analysis["alternatives"]
        best = alternatives[0] if alternatives else None
        if best and best.get("eval"):
            analysis["eval"] = dict(best["eval"])
    if ev["ply"]:
        analysis["ply"] = ev["ply"]
    if gv and gv["has_luck"]:
        analysis["luck"] = gv["luck"]
    if gv and gv["illegal_move"]:
        analysis["illegal_move"] = True
    return analysis


def _build_missed_double(c: dict, gv: dict) -> dict:
    sub: dict = {
        "no_double_equity": c["no_double_equity"],
        "double_take_equity": c["double_take_equity"],
        "double_pass_equity": c["double_pass_equity"],
        "equity_loss": c["equity_loss"],
        "correct_action": _ACTION_NAMES.get(c["correct_action"], "double"),
    }
    # Pre-roll probabilities for the cube decision, as on any other cube record.
    # Absent in files written before writers stored them, where all-zero probs
    # mean "not recorded" rather than "0% to win".
    if any(c["probs"]):
        sub["eval"] = _eval_from_probs(*c["probs"])
    lvl = _decode_eval_level(gv["eval_level"])
    if lvl is not None:
        sub["eval_level"] = lvl
    return sub


def _build_cube_decision(c: dict, gv: dict, derived: bool = False) -> dict:
    """The live double/take/pass a checker ply posed.

    ``derived`` builds the same block from a *missed double's* record rather
    than from a ``live_checker`` one. The base spec has a reader emit both from
    a type=2 entry -- one cube entry per checker ply, so the error record has to
    stand in for the decision as well -- because a consumer rendering a cube
    panel reads only ``cube_decision``.

    A derived block carries **no ``decision``**, and that is the whole of what
    keeps this safe. ``CubeDecision`` is a display projection: the base spec
    gives it no ``equity_loss`` and no ``classification`` precisely because "the
    decision is not itself an error, and any error made against it is scored on
    ``missed_double``". Our ``decision`` flag is accounting, so it belongs with
    the error too -- put it on both and ``stats.py`` counts one cube twice,
    since it accumulates a ``cube_decision`` on its own flag *and* the
    ``missed_double`` beside it.
    """
    should_double = c["correct_action"] == CUBE_ACTION_DOUBLE
    sub: dict = {
        "should_double": should_double,
        "no_double_equity": c["no_double_equity"],
        "double_take_equity": c["double_take_equity"],
        "double_pass_equity": c["double_pass_equity"],
        "action": _cube_action_label(should_double, c["double_take_equity"],
                                     c["double_pass_equity"]),
    }
    if not derived:
        sub["decision"] = bool(gv["decision"])
    if any(c["probs"]):
        sub["eval"] = _eval_from_probs(*c["probs"])
    lvl = _decode_eval_level(gv["eval_level"])
    if lvl is not None:
        sub["eval_level"] = lvl
    return sub


def _build_standalone_cube(c: dict, gv: dict) -> dict:
    """Analysis for a standalone cube ply: type=0 (double), type=1 (take/drop),
    or type=3 (resign)."""
    if c["type"] == CUBE_TYPE_RESIGN:
        analysis: dict = {
            "resign_error": c["no_double_equity"],
            "take_resign_error": c["double_take_equity"],
            "equity_loss": c["equity_loss"],
            "decision": bool(gv["decision"]),
        }
    else:
        analysis = {
            "correct_action": _ACTION_NAMES.get(c["correct_action"], "no_double"),
            "played_action": _ACTION_NAMES.get(c["played_action"], "no_double"),
            "no_double_equity": c["no_double_equity"],
            "double_take_equity": c["double_take_equity"],
            "double_pass_equity": c["double_pass_equity"],
            "equity_loss": c["equity_loss"],
            "decision": bool(gv["decision"]),
        }
    if any(c["probs"]):
        analysis["eval"] = _eval_from_probs(*c["probs"])
    lvl = _decode_eval_level(gv["eval_level"])
    if lvl is not None:
        analysis["eval_level"] = lvl
    if c["ply"]:
        analysis["ply"] = c["ply"]
    return analysis


# ---------------------------------------------------------------------------
# OGID derivation (replay the game to recompute the per-ply position strings
# the binary doesn't store). Mirrors ogxm_export's per-ply ogid logic and
# reuses its OGID state machine, so the result is byte-identical to
# to_ogxm_json (and thus libogxm).
# ---------------------------------------------------------------------------

def _absolute_to_p1(board_abs: list[int]) -> list[int]:
    """Convert a set_position board (OGXM absolute frame: 0=white bar,
    25=black bar, positive=white) to the P1/White mover-perspective frame the
    OGID encoder and running-board replay use (0=black bar, 25=white bar)."""
    p1 = [0] * 26
    p1[0] = -board_abs[25]      # black bar (abs 25, stored negative) -> non-neg count
    p1[25] = board_abs[0]       # white bar
    for i in range(1, 25):
        p1[i] = board_abs[25 - i]
    return p1


def _apply_moves_p1(board_p1: list[int], moves: list[dict], mover_is_white: bool):
    """Return board_after in P1/White frame after applying ``moves`` (OGXM
    absolute steps). Works in mover perspective (own checkers positive, own
    bar = 25, opponent bar = 0), then flips back for a black mover."""
    from .export import _flip_board
    mb = list(board_p1) if mover_is_white else _flip_board(board_p1)
    for m in moves:
        from_abs, pips = int(m["from"]), int(m["pips"])
        src = (25 - from_abs) if mover_is_white else from_abs
        # Bounds-check before indexing. Python indexes lists from the end on a
        # negative subscript, so an out-of-range `from` (only reachable from a
        # corrupt or hand-edited stream) would otherwise silently decrement some
        # unrelated point instead of failing -- a wrong board, not an error.
        if not 0 <= src <= 25:
            raise GvabError(f"move from point {from_abs} is outside the board")
        mb[src] -= 1  # lift own checker (own bar is index 25)
        dest = src - pips
        if dest >= 1:
            if mb[dest] < 0:      # opponent blot -> hit
                mb[dest] = 1
                mb[0] += 1        # opponent to the bar
            else:
                mb[dest] += 1
        # dest <= 0: borne off, nothing to place
    return mb if mover_is_white else _flip_board(mb)


def _game_start_scores(games: list[dict], match_length: int = 0) -> list[tuple[int, int]]:
    """Per-game (white_start, black_start) match scores, reconstructed by
    accumulating each game's points_won to its winner (match assumed to open
    0-0 -- the binary stores only final scores and per-game points/winner).

    Each game's contribution is capped at what its winner still needed, so a
    file written before that was a writer rule (an uncapped 4-point gammon at
    6-1 of a 7-pointer) still scores 7-1 rather than 10-1."""
    out = []
    w = b = 0
    for g in games:
        out.append((w, b))
        winner = g.get("winner")
        if winner == 0:
            w += cap_points_won(g.get("points_won", 0), w, match_length)
        elif winner == 1:
            b += cap_points_won(g.get("points_won", 0), b, match_length)
    return out


def _derive_ogids(ogxm: dict) -> None:
    """Fill ogid_before/ogid_after on every ply of ``ogxm`` in place."""
    from .export import (
        _TurnState, _ogid, _STARTING_BOARD_P1,
        _OGID_STATE_INITIAL_BOTH, _OGID_STATE_ROLLED, _OGID_STATE_CHECKER_DONE,
        _OGID_STATE_DOUBLE_OFFERED, _OGID_STATE_AFTER_TAKE, _OGID_STATE_GAME_OVER,
        _OGID_ACTION_NONE, _OGID_ACTION_DOUBLE, _OGID_ACTION_TAKE, _OGID_ACTION_PASS,
        _OGID_CUBE_WHITE, _OGID_CUBE_BLACK,
    )

    match_length = int(ogxm.get("match_length", 0) or 0)
    games = ogxm.get("games") or []
    start_scores = _game_start_scores(games, match_length)

    for g, (sw, sb) in zip(games, start_scores):
        crawford = bool(g.get("is_crawford", False))
        turn = _TurnState()
        board = list(_STARTING_BOARD_P1)

        def ogid(brd, *, on_roll, game_state, cube_action, dice=None):
            return _ogid(brd, cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                         cube_action=cube_action, dice=dice, on_roll=on_roll,
                         game_state=game_state, score_white=sw, score_black=sb,
                         match_length=match_length, crawford=crawford,
                         move_id=turn.move_id)

        for ply in g.get("plies") or []:
            aid = ply.get("action_id")
            color = 1 if ply.get("color") else 0
            on_roll = "W" if color == 1 else "B"
            opp = "B" if color == 1 else "W"

            if aid is not None and 0 <= aid <= 20:  # checker
                d1, d2 = ply.get("d1"), ply.get("d2")
                before_state = _OGID_STATE_INITIAL_BOTH if turn.is_first_ply else _OGID_STATE_ROLLED
                ply["ogid_before"] = ogid(board, on_roll=on_roll, game_state=before_state,
                                          cube_action=turn.cube_action, dice=(d1, d2))
                turn.move_id += 1
                turn.is_first_ply = False
                turn.cur_state = _OGID_STATE_CHECKER_DONE
                turn.cube_action = _OGID_ACTION_NONE
                board = _apply_moves_p1(board, ply.get("moves") or [], color == 1)
                ply["ogid_after"] = ogid(board, on_roll=opp, game_state=_OGID_STATE_CHECKER_DONE,
                                         cube_action=_OGID_ACTION_NONE)

            elif aid == 21:  # cube: double offered
                ply["ogid_before"] = ogid(board, on_roll=on_roll, game_state=turn.cur_state,
                                          cube_action=turn.cube_action)
                turn.awaiting_response = True
                turn.cur_state = _OGID_STATE_DOUBLE_OFFERED
                turn.cube_action = _OGID_ACTION_DOUBLE
                ply["ogid_after"] = ogid(board, on_roll=opp, game_state=_OGID_STATE_DOUBLE_OFFERED,
                                         cube_action=_OGID_ACTION_DOUBLE)

            elif aid in (22, 23):  # cube: take (22) / drop (23)
                ply["ogid_before"] = ogid(board, on_roll=on_roll,
                                          game_state=_OGID_STATE_DOUBLE_OFFERED,
                                          cube_action=_OGID_ACTION_DOUBLE)
                turn.awaiting_response = False
                if aid == 22:  # take
                    turn.cube_log2 += 1
                    turn.cube_owner = _OGID_CUBE_WHITE if color == 1 else _OGID_CUBE_BLACK
                    turn.cur_state = _OGID_STATE_AFTER_TAKE
                    turn.cube_action = _OGID_ACTION_TAKE
                else:  # drop
                    turn.cur_state = _OGID_STATE_GAME_OVER
                    turn.cube_action = _OGID_ACTION_PASS
                ply["ogid_after"] = ogid(board, on_roll=opp, game_state=turn.cur_state,
                                         cube_action=turn.cube_action)

            elif aid == ACTION_SET_POSITION:  # 31
                board = _absolute_to_p1(ply.get("set_position") or [0] * 26)
                # No exporter emits set_position, so there is no reference OGID
                # to match; skip rather than invent one.

            elif aid in _TERMINAL_ACTIONS:  # game/match end, resign, forfeit
                ply["ogid_before"] = ogid(board, on_roll=on_roll, game_state=turn.cur_state,
                                          cube_action=turn.cube_action)
                ply["ogid_after"] = ogid(board, on_roll=opp, game_state=turn.cur_state,
                                         cube_action=turn.cube_action)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_gvab(data: bytes, *, verify_crc: bool = True, derive_ogids: bool = True) -> dict:
    """Parse ``.gvab`` binary bytes into an OGXM-JSON dict (the inverse of
    ``gvab.write_gvab``).

    Args:
        data: the raw ``.gvab`` file contents.
        verify_crc: when True (default), validate the CSUM CRC32 and raise
            ``GvabError`` on mismatch. Pass False to accept a corrupt/edited
            checksum.
        derive_ogids: when True (default), replay each game to fill in the
            per-ply ``ogid_before``/``ogid_after`` position strings (which the
            binary doesn't store). Pass False to skip that pass.

    Raises:
        GvabError: the input isn't a well-formed OGXM binary stream. This is a
            guarantee, not a best effort: callers parse untrusted uploads with
            this, so *every* failure on malformed input arrives as ``GvabError``
            and never as a stray ``IndexError`` / ``struct.error`` from some
            decoder's internals.
    """
    try:
        return _read_gvab(data, verify_crc=verify_crc, derive_ogids=derive_ogids)
    except GvabError:
        raise
    except Exception as exc:  # noqa: BLE001 - see the Raises: contract above
        raise GvabError(f"malformed .gvab ({type(exc).__name__}: {exc})") from exc


def _read_gvab(data: bytes, *, verify_crc: bool, derive_ogids: bool) -> dict:
    """``read_gvab``'s body; see it for the documented behaviour. Split out so
    the public entry point can enforce the GvabError contract in one place."""
    if len(data) < 20:
        raise GvabError("input shorter than the 20-byte file header")
    magic, _vmaj, _vmin, rmaj, rmin, file_size, _hflags = struct.unpack_from("<IHHHHII", data, 0)
    if magic != OGXM_MAGIC:
        raise GvabError(f"bad magic 0x{magic:08X} (expected 0x{OGXM_MAGIC:08X})")
    # min_reader_* is the file's own statement of the spec version it needs.
    # Honouring it is the point of the field: a file using a later layout must
    # fail as a clean version mismatch, not be parsed optimistically into
    # silent garbage. Checked before file_size, as the reference codec does.
    if rmaj > VERSION_MAJOR or (rmaj == VERSION_MAJOR and rmin > VERSION_MINOR):
        raise GvabError(
            f"file requires an OGXM {rmaj}.{rmin} reader; this one implements "
            f"{VERSION_MAJOR}.{VERSION_MINOR}")
    if file_size and file_size != len(data):
        raise GvabError(f"file_size header ({file_size}) != actual length ({len(data)})")

    end_magic = struct.unpack_from("<I", data, len(data) - 8)[0]
    if end_magic != END_MAGIC:
        raise GvabError(f"bad end marker 0x{end_magic:08X} (expected 0x{END_MAGIC:08X})")

    chunks, csum_start = _walk_chunks(data)

    if verify_crc and csum_start >= 0:
        want = struct.unpack_from("<I", data, csum_start + 16)[0]
        got = zlib.crc32(data[:csum_start]) & 0xFFFFFFFF
        if want != got:
            raise GvabError(f"CSUM mismatch: stored 0x{want:08X}, computed 0x{got:08X}")

    def first(ctype: int) -> bytes | None:
        for t, body, _flags in chunks:
            if t == ctype:
                return body
        return None

    mhdr = first(CHUNK_MHDR)
    if mhdr is None:
        raise GvabError("no MHDR chunk")
    ogxm = _decode_mhdr(mhdr)

    # Games in stream order; also index each ply by (game_index, ply_index) so
    # analysis entries (keyed by that pair) can be threaded back onto them.
    games: list[dict] = []
    ply_by_key: dict[tuple[int, int], dict] = {}
    for ctype, body, _flags in chunks:
        if ctype != CHUNK_GAME:
            continue
        game_obj, gi = _decode_game(body)
        for pi, ply in enumerate(game_obj["plies"]):
            ply_by_key[(gi, pi)] = ply
        games.append(game_obj)

    # Games, and the positions they replay to, are settled before any
    # analysis is decoded. `basefill.py` needs a ply's own score and cube to
    # read a foreign block's cube values, and derivation depends on nothing
    # an analysis holds -- so the order costs nothing and is what lets that
    # completion happen while each block is still a dict of its own, before
    # the primary is aliased onto `ply["analysis"]``.
    #
    # `_derive_ogids` is handed a throwaway dict rather than `ogxm` itself:
    # it only reads `match_length`/`games` and mutates the ply dicts in
    # place (which `games` already shares by reference), so this reproduces
    # every effect of setting `ogxm["games"]` here without moving that key
    # earlier in `ogxm`'s own insertion order -- which the golden `.gva` JSON
    # comparison in tests/test_ogxm_pipeline.py depends on.
    if derive_ogids:
        _derive_ogids({"match_length": ogxm.get("match_length"), "games": games})

    # Group the analysis chunks by ANAL: each EVAL/ALTS/CUBE/GVAN binds to the
    # most recent ANAL (base spec 1.3 -- zero or more analysis blocks, primary
    # first). One block is the legacy single-analysis case; more than one is
    # multi-analysis (analyses_info + per-ply analyses[]).
    #
    # The same pass captures every chunk we do not decode (SIGN, CLCK, VIDO,
    # anything a later spec version adds) so write_gvab can re-emit it. Each is
    # tagged with the analysis block it followed, which is what SIGN binds to.
    anal_groups: list[dict] = []
    unknown: list[dict] = []
    cur: dict | None = None
    for t, body, cflags in chunks:
        if t == CHUNK_ANAL:
            cur = {"anal": body, "eval": b"", "alts": b"", "cube": b"", "gvan": None}
            anal_groups.append(cur)
        elif cur is not None:
            if t == CHUNK_EVAL:
                cur["eval"] = body
            elif t == CHUNK_ALTS:
                cur["alts"] = body
            elif t == CHUNK_CUBE:
                cur["cube"] = body
            elif t == CHUNK_GVAN:
                cur["gvan"] = body
        if t in _DECODED_CHUNKS:
            continue
        if cflags & CHUNK_FLAG_CRITICAL:
            # A critical chunk we cannot interpret means the file says more
            # than we can read. Carrying it through would be a lie; the base
            # spec's rule is to reject.
            raise GvabError(
                f"unknown critical chunk {_chunk_name(t)} -- this reader cannot "
                "safely read or rewrite the file"
            )
        unknown.append({
            "type": t,
            "name": _chunk_name(t),
            "flags": cflags,
            "anal_index": len(anal_groups) - 1,
            "data": base64.b64encode(body).decode("ascii"),
        })

    # Decode each block to (analysis_info, {(game_index, ply_index): analysis}).
    blocks: list[tuple[dict, dict]] = []
    #: Which of them arrived without our extensions, for a caller that wants
    #: to say so -- a foreign block is missing its luck until an engine
    #: supplies it. Not written back: once a block has been completed and
    #: re-encoded it has a GVAN of its own and is no longer foreign to the
    #: next read.
    base_blocks: list[int] = []
    for grp in anal_groups:
        info = _decode_anal(grp["anal"])
        eval_entries = _decode_eval_entries(grp["eval"])
        alt_entries = _decode_alt_entries(grp["alts"])
        cube_entries = _decode_cube_entries(grp["cube"])
        total_alts = sum(e["num_alts"] for e in eval_entries)

        gvan = (_decode_gvan(grp["gvan"], len(eval_entries), total_alts, len(cube_entries))
                if grp["gvan"] is not None else None)
        gv_checker = gvan["checker"] if gvan else []
        gv_alt_levels = gvan["alt_levels"] if gvan else []
        gv_cube = gvan["cube"] if gvan else []

        block_obj: dict[tuple[int, int], dict] = {}

        # EVAL + ALTS + GVAN-checker -> per-checker-ply analysis.
        alt_cursor = 0
        for i, ev in enumerate(eval_entries):
            n = ev["num_alts"]
            alts = alt_entries[alt_cursor:alt_cursor + n]
            levels = gv_alt_levels[alt_cursor:alt_cursor + n] if gv_alt_levels else [0] * n
            alt_cursor += n
            gv = gv_checker[i] if i < len(gv_checker) else None
            block_obj[(ev["game_index"], ev["ply_index"])] = _build_checker_analysis(ev, gv, alts, levels)

        # CUBE + GVAN-cube -> standalone cube-ply analyses, plus missed_double
        # (type=2) / cube_decision (type=4) sub-objects on their checker ply.
        for i, c in enumerate(cube_entries):
            gv = gv_cube[i] if i < len(gv_cube) else {"decision": False, "eval_level": 0}
            key = (c["game_index"], c["ply_index"])
            if c["type"] == CUBE_TYPE_MISSED_DOUBLE:
                obj = block_obj.setdefault(key, {})
                obj["missed_double"] = _build_missed_double(c, gv)
                # And the decision the error was made against, as the spec has
                # every cube-live checker ply carry -- a missed-double ply
                # included. Derived, so it holds no `decision` of its own; the
                # accounting stays above.
                obj["cube_decision"] = _build_cube_decision(c, gv, derived=True)
            elif c["type"] == CUBE_TYPE_LIVE_CHECKER:
                block_obj.setdefault(key, {})["cube_decision"] = _build_cube_decision(c, gv)
            else:
                block_obj[key] = _build_standalone_cube(c, gv)

        analysis_info: dict = {"ply": info["ply"]}
        if gvan is not None:
            base_lvl = _decode_eval_level(gvan["base_eval_level"])
            if base_lvl is not None:
                analysis_info["eval_level"] = base_lvl
            luck_lvl = _decode_eval_level(gvan["luck_eval_level"])
            if luck_lvl is not None:
                analysis_info["luck_eval_level"] = luck_lvl
        analysis_info["model_id"] = info["model_id"]
        analysis_info["timestamp"] = info["timestamp"]
        if info["duration_ms"]:
            analysis_info["duration_ms"] = info["duration_ms"]

        # No GVAN means no writer of ours: the block is the base format
        # alone, and the fields only our extension carries have to be
        # derived from what is there rather than read as absent. See
        # `basefill.py` -- and note it needs the OGIDs, which is why they are
        # derived above and not at the end.
        if gvan is None and block_obj:
            if derive_ogids:
                complete_base_block(block_obj, ply_by_key, analysis_info)
            base_blocks.append(len(blocks))
        blocks.append((analysis_info, block_obj))

    if len(blocks) == 1:
        info, block_obj = blocks[0]
        for key, obj in block_obj.items():
            ply = ply_by_key.get(key)
            if ply is not None:
                ply["analysis"] = obj
        ogxm["analysis_info"] = info
    elif len(blocks) > 1:
        # Multi-analysis: analyses_info lists every block; each analyzed ply
        # gets one analyses[] entry per block (tagged analysis_index). The
        # primary (index 0) is also mirrored as analysis_info + per-ply
        # analysis so naive single-analysis readers keep working.
        ogxm["analyses_info"] = [info for info, _ in blocks]
        ogxm["analysis_info"] = blocks[0][0]
        for k, (_info, block_obj) in enumerate(blocks):
            for key, obj in block_obj.items():
                ply = ply_by_key.get(key)
                if ply is None:
                    continue
                ply.setdefault("analyses", []).append({**obj, "analysis_index": k})
                if k == 0:
                    ply["analysis"] = obj

    # `ogid_before`/`ogid_after` were derived above, before `analysis`/
    # `analyses` existed on any ply, so a naive assignment would leave them
    # ordered ahead of the fields this pass just added -- dict-key order
    # `to_ogxm_json` never produced and the golden `.gva` regression pins.
    # Re-insert them last on every ply that has them, restoring that order.
    for g in games:
        for ply in g.get("plies") or ():
            for k in ("ogid_before", "ogid_after"):
                if k in ply:
                    ply[k] = ply.pop(k)

    ogxm["games"] = games
    if unknown:
        ogxm["_unknown_chunks"] = unknown
    if base_blocks:
        ogxm["_base_analyses"] = base_blocks

    return ogxm


def canonicalize(ogxm: dict) -> dict:
    """Return the *canonical* form of an OGXM-JSON dict: exactly the dict a
    later ``read_gvab`` of the stored ``.gvab`` will produce.

    The ``.gvab`` binary is a lossy encoding of OGXM-JSON: it quantizes every
    probability/equity to 1/10000, clamps equities to +/-3, stores the dice
    pair unordered, and carries no derived/display fields (``notation``,
    ``diff``, ``preset``, ...). So a freshly generated dict and the dict you
    read back from its ``.gvab`` are *not* equal in general.

    ``canonicalize(J)`` collapses that difference by round-tripping J through
    the binary once (``read_gvab(write_gvab(J))``). Because ``read_gvab``'s
    output is a fixed point of the write/read cycle, the result is idempotent
    and byte-stable:

        canonicalize(canonicalize(J)) == canonicalize(J)
        read_gvab(write_gvab(canonicalize(J))) == canonicalize(J)

    Recommended server pattern -- since you serialize the ``.gvab`` to store
    it anyway, get your working JSON straight from those same bytes so it is,
    by construction, identical to any future read::

        ogxm = to_ogxm_json(analyze_mat(...))
        data = write_gvab(ogxm)      # persist this
        store(data)
        working = read_gvab(data)    # == canonicalize(ogxm); use everywhere

    ``canonicalize(ogxm)`` is the same thing when you don't have the bytes in
    hand. Both leave the original ``ogxm`` untouched.
    """
    return read_gvab(write_gvab(ogxm))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Decode a .gvab (OGXM binary) file to OGXM-JSON.")
    parser.add_argument("path", help="path to a .gvab file")
    parser.add_argument("-o", "--output", help="write JSON here (default: stdout)")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip CSUM CRC verification")
    parser.add_argument("--no-ogids", action="store_true",
                        help="skip deriving per-ply ogid_before/ogid_after")
    parser.add_argument("--compact", action="store_true",
                        help="compact JSON (no indentation)")
    args = parser.parse_args(argv)

    with open(args.path, "rb") as f:
        data = f.read()
    ogxm = read_gvab(data, verify_crc=not args.no_verify,
                     derive_ogids=not args.no_ogids)

    indent = None if args.compact else 2
    text = json.dumps(ogxm, indent=indent, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        try:
            sys.stdout.write(text + "\n")
        except BrokenPipeError:  # e.g. piped into `head`
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
