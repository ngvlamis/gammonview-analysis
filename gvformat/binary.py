# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis
#
# Portions of this file are ported from HedgeHog's C++ codec
# (MIT, Copyright (c) 2026 Eran Lambooij). See THIRD-PARTY-NOTICES.md,
# whose notices must be preserved in copies of this file.

"""Pure-Python OGXM binary WRITER: OGXM-JSON dict -> ``.gvab`` bytes.

``write_gvab(ogxm)`` serializes a dict shaped like ``ogxm_export.to_ogxm_json(...)``
produces into the compact OGXM binary format. Pure stdlib -- no bgsage/engine
calls, no C++ extension.

This module writes only. The inverse -- parsing ``.gvab`` bytes back into an
OGXM-JSON dict -- lives in ``read_gvab.py`` (``read_gvab(data)``), defined as
this writer's byte-level inverse (``write_gvab(read_gvab(b)) == b``).

HedgeHog is the reference implementation and the format's origin:
https://gitlab.com/eranlambooij/hedgehog-public

Two specs describe the on-wire format:

  - ``OGXM_FORMAT_SPEC_GAMMONVIEW.md`` (this repo) -- file header, chunk
    framing, GammonView MHDR/GAME metadata, and the GVAN ancillary chunk
    (checker/alt/cube sections, eval-level bit-flag encoding).
  - HedgeHog's own ``docs/OGXM_FORMAT_SPEC.md`` -- the base MHDR/
    GAME/ANAL/EVAL/ALTS/CUBE/CSUM chunk layouts (v1.3 + the source=4/
    CUBE type=4 additive extensions).

That markdown document describes a design that is slightly AHEAD of the
actually-compiled reference codec (HedgeHog's ``build/libogxm.so``,
built from ``src/match/ogxm_io.cpp`` + ``ogxm_format.hpp`` + ``ogxm_json.cpp``):
the compiled library does not yet implement the GammonView MHDR metadata
extensions (``event``, ``site``, ``cube_limit``, ``beaver``, ``raccoon``). This module
writes those into MHDR's reserved/trailing regions anyway (the reference
codec's reader is provably tolerant: ``read_mhdr`` tracks how many bytes it
actually parsed and ``skip()``s any remainder up to the chunk's declared
length, so extra trailing bytes -- and non-zero "reserved" struct bytes it
never reads into anything -- are silently ignored, not rejected). Consequently
MHDR is **no longer** byte-for-byte identical to ``ogxm_json_to_binary()``'s
output on this dict: it differs in exactly the GammonView metadata bytes
documented below. GAME and ANAL/EVAL/ALTS/CUBE remain byte-for-byte identical
to the compiled C++ writer, mirroring its field layout and semantics verified
by reading ``ogxm_io.cpp``/``ogxm_format.hpp``/``ogxm_json.cpp`` directly.

GammonView MHDR metadata placement (see ``OGXM_FORMAT_SPEC_GAMMONVIEW.md``
for the full writeup):

  - ``beaver`` -> MHDR flags byte (offset 3) bit 2; ``raccoon`` -> bit 3.
    (Base bits: 0=crawford, 1=jacoby.)
  - ``cube_limit`` -> uint16 LE in the first 2 bytes (offset 16-17) of MHDR's
    8-byte ``reserved[8]`` region (offset 16-23); the other 6 bytes stay 0.
  - ``event``, then ``site`` -> appended to MHDR's variable part after
    ``player_black``, each a length-prefixed string (uint8 length + UTF-8
    bytes, 0-length = absent). Old readers stop after the two player names and
    never look past them; a reader that knows ``event`` but not ``site`` stops
    one field earlier and is equally unaffected.

Equity<->MWC conversion is engine-free on read via a single shipped MET
(Kazaross-XG2, ``gvformat.met``): ``met.mwc_anchors(away1, away2, cube_value,
is_crawford)`` derives the mover's match-winning chance on a win/loss of the
current cubeful game from ``(score, cube)`` alone, so the format no longer
needs to carry per-decision ``mwc_on_win``/``mwc_on_loss`` anchors at all --
they were removed from GVAN (v2; the two former anchor slots in each checker/
cube record are now reserved zero bytes). This replaces the older per-game
``met_value`` (which only anchored cube 1 at equity 0 and could not follow
the cube through the game), so GAME carries no trailing bytes either.

The JSON `cube_decision` sub-object (a correct/non-error live cube on a
checker ply) and `missed_double` sub-object both map to base ``CUBE``
entries -- ``type=4`` (``CUBE_TYPE_LIVE_CHECKER``) and ``type=2``
(``CUBE_TYPE_MISSED_DOUBLE``) respectively -- exactly as
``ogxm_json.cpp``'s ``parse_ply_analysis_json`` does. There is no separate
GammonView "no-double section" for this; it was folded into the base CUBE
chunk when the maintainer added ``CUBE type=4`` upstream. All remaining
GammonView *analysis* extensions (luck equities, decision flags, per-alt/
per-cube eval levels) live entirely in the ancillary ``GVAN`` chunk, which is
new -- the reference codec doesn't write or read it, but also doesn't need
to: base readers skip unknown ancillary chunks by length.

CRC32 for the CSUM chunk uses the same algorithm as ``zlib.crc32`` (ISO-hdlc /
"zip" CRC-32: reflected, poly 0xEDB88320, init/final XOR 0xFFFFFFFF) -- the
same table-driven CRC32 the reference's ``src/lib/checksum.hpp`` implements.
"""

from __future__ import annotations

import base64
import math
import re
import struct
import zlib

# ---------------------------------------------------------------------------
# Constants (mirrors ogxm_format.hpp)
# ---------------------------------------------------------------------------

OGXM_MAGIC = 0x4D58474F
VERSION_MAJOR = 1
VERSION_MINOR = 3

CHUNK_MHDR = 0x5244484D
CHUNK_GAME = 0x454D4147
CHUNK_ANAL = 0x4C414E41
CHUNK_EVAL = 0x4C415645
CHUNK_ALTS = 0x53544C41
CHUNK_CUBE = 0x45425543
CHUNK_GVAN = 0x4E415647
CHUNK_CSUM = 0x4D555343
# Base-spec chunks gvformat does not decode. They are carried through a
# read/write cycle verbatim (see ``_unknown_chunks``): SIGN binds to its
# analysis block, CLCK/VIDO trail every block.
CHUNK_SIGN = 0x4E474953
CHUNK_CLCK = 0x4B434C43
CHUNK_VIDO = 0x4F444956
END_MAGIC = 0x21444E45

CHUNK_FLAG_CRITICAL = 0x0001
HEADER_FLAG_HAS_ANALYSIS = 0x0001

# GVAN chunk version (GammonView's own, independent of the OGXM version).
#   v1: per-decision mwc_on_win/mwc_on_loss anchors
#   v2: anchors blanked (MWC became compute-on-read) but the bytes kept
#   v3: the blanked bytes dropped -- checker record 7->3 B, cube record 6->2 B
GVAN_VERSION = 3
GVAN_CHECKER_REC = 3
GVAN_CUBE_REC = 2
GVAN_CHECKER_REC_V2 = 7
GVAN_CUBE_REC_V2 = 6

MAX_ALTS_PER_DECISION = 50
MAX_PLAYER_NAME = 255
DEFAULT_BOARD_SENTINEL = 0xFF
WINNER_WHITE = 0
WINNER_BLACK = 1
WINNER_INCOMPLETE = 0xFF

ACTION_DOUBLE = 21
ACTION_TAKE = 22
ACTION_DROP = 23
ACTION_RESIGN_GAME = 27
ACTION_RESIGN_MATCH = 28
ACTION_SET_POSITION = 31

# The terminal actions that are *acts*. Every other end-of-game marker (24 game
# over, 26 final, 29 forfeit) has no actor, so writers stamp it with the winner;
# a resignation has one, and it is the player who lost. Readers use this to
# decide whose row a terminal ply belongs to.
RESIGN_ACTIONS = frozenset({ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH})
SET_POSITION_DICE_FLAG = 0x40

CUBE_TYPE_DOUBLE_DECISION = 0
CUBE_TYPE_TAKE_PASS = 1
CUBE_TYPE_MISSED_DOUBLE = 2
CUBE_TYPE_RESIGN = 3
CUBE_TYPE_LIVE_CHECKER = 4

CUBE_ACTION_NO_DOUBLE = 0
CUBE_ACTION_DOUBLE = 1
CUBE_ACTION_TAKE = 2
CUBE_ACTION_PASS = 3

_CUBE_ACTION_CODES = {
    "no_double": CUBE_ACTION_NO_DOUBLE,
    "double": CUBE_ACTION_DOUBLE,
    "take": CUBE_ACTION_TAKE,
    "pass": CUBE_ACTION_PASS,
}

# action_id -> (d1, d2, num_move_bytes); index = action_id (0-20).
DICE_TABLE = [
    (1, 1, 4), (1, 2, 2), (1, 3, 2), (1, 4, 2), (1, 5, 2), (1, 6, 2),
    (2, 2, 4), (2, 3, 2), (2, 4, 2), (2, 5, 2), (2, 6, 2),
    (3, 3, 4), (3, 4, 2), (3, 5, 2), (3, 6, 2),
    (4, 4, 4), (4, 5, 2), (4, 6, 2),
    (5, 5, 4), (5, 6, 2),
    (6, 6, 4),
]


# ---------------------------------------------------------------------------
# Fixed-point encoding (mirrors ogxm_format.hpp's encode_probability/
# encode_equity/encode_equity_loss -- including round-half-away-from-zero,
# matching C's roundf() rather than Python's round-half-to-even).
#
# One deliberate exception: _enc_equity_loss uses a wider bound than the
# reference codec. See its comment below -- that divergence is ours, and
# ogxm_format.hpp is not ours to change.
# ---------------------------------------------------------------------------

def _round_c(x: float) -> int:
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


def _enc_prob(p: float) -> int:
    p = 0.0 if p < 0.0 else (1.0 if p > 1.0 else p)
    return _round_c(p * 10000.0)


def _enc_equity(eq: float) -> int:
    eq = -3.0 if eq < -3.0 else (3.0 if eq > 3.0 else eq)
    return _round_c(eq * 10000.0)


# uint16 at 1e-4, so 6.5535 is the largest representable loss. The base format
# clamps this at 1.0 and its reference codec still does; we deliberately do not,
# because that truncated any blunder past a point of equity -- and since
# played_equity is derived (best_equity - equity_loss), a truncated loss moved
# the played equity too, leaving records disagreeing with their own alternatives.
#
# Still Tier 1: readers decode with a plain divide and never range-check, so the
# reference codec reads our wider values correctly. No version bump. The clamp
# stays at the top of the range -- without it, 6.6 would overflow the uint16 and
# wrap to 0.0464. See OGXM_FORMAT_SPEC_GAMMONVIEW.md, "Fixed-Point Encoding".
MAX_EQUITY_LOSS = 6.5535


def _enc_equity_loss(loss: float) -> int:
    loss = 0.0 if loss < 0.0 else (MAX_EQUITY_LOSS if loss > MAX_EQUITY_LOSS else loss)
    return _round_c(loss * 10000.0)


def _pack_move_step(start: int, pips: int) -> int:
    return (start & 0x1F) | ((pips & 0x07) << 5)


def _pack_string(s) -> bytes:
    b = (s or "").encode("utf-8", errors="replace")
    b = b[:MAX_PLAYER_NAME]
    return bytes([len(b)]) + b


def _chunk(type_code: int, data: bytes, critical: bool) -> bytes:
    flags = CHUNK_FLAG_CRITICAL if critical else 0
    return struct.pack("<IIHH", type_code, len(data), flags, 0) + data


def cap_points_won(points: int, winner_score_before: int, match_length: int) -> int:
    """Clamp a game's ``points_won`` to what the winner could actually bank.

    A match ends the instant somebody reaches the target, so a 4-point gammon
    won at 6-1 of a 7-point match banks ``1``, not ``4``, and the match ends
    7-1. Readers accumulate the match score by summing this field, so storing
    the uncapped value pushes the running score past ``match_length``.

    Money play (``match_length`` 0/absent) has no target and is never capped.
    """
    points = max(0, int(points or 0))
    match_length = int(match_length or 0)
    if match_length <= 0:
        return points
    return min(points, max(0, match_length - int(winner_score_before or 0)))


def clamp_match_score(score: int, match_length: int) -> int:
    """Clamp one side's recorded total to the match length (money play is
    never clamped). The companion to :func:`cap_points_won` for the two
    already-summed totals MHDR stores."""
    score = int(score or 0)
    match_length = int(match_length or 0)
    if match_length > 0 and score > match_length:
        return match_length
    return score


def _encode_passthrough(chunks) -> bytes:
    """Re-emit chunks carried verbatim from the input (see ``_unknown_chunks``)."""
    out = bytearray()
    for c in chunks or []:
        body = c.get("data") or b""
        if isinstance(body, str):
            body = base64.b64decode(body)
        out += struct.pack(
            "<IIHH", int(c["type"]) & 0xFFFFFFFF, len(body),
            int(c.get("flags", 0) or 0) & 0xFFFF, 0,
        ) + body
    return bytes(out)


def _split_passthrough(ogxm: dict, num_blocks: int):
    """Partition ``_unknown_chunks`` into (per-block lists, trailing list).

    A chunk records the index of the analysis block it followed in the source
    file. SIGN binds to its ANAL and must stay inside that block; everything
    else (CLCK, VIDO, any future ancillary chunk) trails all blocks, which is
    also where a chunk from a block we no longer emit is parked.
    """
    per_block: list[list[dict]] = [[] for _ in range(num_blocks)]
    trailing: list[dict] = []
    for c in ogxm.get("_unknown_chunks") or []:
        idx = c.get("anal_index")
        if (int(c.get("type", 0) or 0) == CHUNK_SIGN
                and isinstance(idx, int) and 0 <= idx < num_blocks):
            per_block[idx].append(c)
        else:
            trailing.append(c)
    return per_block, trailing


def _probs_from_eval(ev) -> list[float]:
    if not isinstance(ev, dict):
        return [0.0, 0.0, 0.0, 0.0, 0.0]
    return [
        float(ev.get("win", 0.0) or 0.0),
        float(ev.get("gammon_win", 0.0) or 0.0),
        float(ev.get("bg_win", 0.0) or 0.0),
        float(ev.get("gammon_loss", 0.0) or 0.0),
        float(ev.get("bg_loss", 0.0) or 0.0),
    ]


def _cube_action_code(s) -> int:
    return _CUBE_ACTION_CODES.get(s, CUBE_ACTION_NO_DOUBLE)


# ---------------------------------------------------------------------------
# Ply encoding (mirrors ogxm_io.cpp's encode_ply)
# ---------------------------------------------------------------------------

def _encode_ply(ply: dict, action_id: int) -> bytes:
    color = 1 if ply.get("color") else 0
    first_byte = (action_id & 0x1F) | (color << 5)

    if action_id == ACTION_SET_POSITION:
        board = ply.get("set_position") or [0] * 26
        board = [int(v) for v in board]
        if len(board) != 26:
            board = (board + [0] * 26)[:26]
        d1 = ply.get("d1")
        d2 = ply.get("d2")
        has_dice = (
            isinstance(d1, int) and isinstance(d2, int)
            and 1 <= d1 <= 6 and 1 <= d2 <= 6
        )
        board_bytes = struct.pack("<26b", *board)
        if has_dice:
            return bytes([
                first_byte | SET_POSITION_DICE_FLAG,
                (d1 & 0x0F) | ((d2 & 0x0F) << 4),
            ]) + board_bytes
        return bytes([first_byte]) + board_bytes

    if not (0 <= action_id <= 20):
        return bytes([first_byte])

    _, _, n_moves = DICE_TABLE[action_id]
    moves = ply.get("moves") or []
    if len(moves) > n_moves:
        # The record holds exactly the roll's hops. Silently keeping the first
        # `n_moves` would write a ply that replays to the wrong board, and the
        # error only surfaces plies later as an impossible position -- so say it
        # here. A play with extra hops is an illegal one (see gvformat.xg); a
        # converter must reshape it before writing, not hand it over as-is.
        raise ValueError(
            f"ply has {len(moves)} move steps but action_id {action_id} has room "
            f"for {n_moves}; an illegal play must be written as a set-position ply"
        )
    step_bytes = []
    for i in range(n_moves):
        if i < len(moves):
            m = moves[i]
            step_bytes.append(_pack_move_step(int(m.get("from", 0)), int(m.get("pips", 0))))
        else:
            step_bytes.append(0)
    move0 = step_bytes[0] if step_bytes else 0
    first_byte |= (move0 & 0x03) << 6
    return bytes([first_byte] + step_bytes)


# ---------------------------------------------------------------------------
# Analysis-record builders: reshape a ply's inline `analysis` dict (OGXM-JSON
# shape, see OGXM_JSON_SPEC_GAMMONVIEW.md) into the flat records the base
# EVAL/ALTS/CUBE writers and the GVAN writer both consume. These mirror
# ogxm_json.cpp's parse_ply_analysis_json() field-for-field (including its
# defaults) so the base chunk bytes match the reference codec exactly.
# ---------------------------------------------------------------------------

def _build_checker_eval(game_index: int, ply_index: int, analysis: dict) -> dict:
    alts_raw = analysis.get("alternatives") or []
    alts = []
    for a in alts_raw[:MAX_ALTS_PER_DECISION]:
        alts.append({
            "move": a.get("move") or [],
            "equity": float(a.get("equity", 0.0) or 0.0),
            "probs": _probs_from_eval(a.get("eval")) if "eval" in a else None,
            "is_played": bool(a.get("is_played", False)),
            "eval_level": a.get("eval_level"),
        })
    has_luck = "luck" in analysis
    return {
        "game_index": game_index,
        "ply_index": ply_index,
        "probs": _probs_from_eval(analysis.get("eval")) if "eval" in analysis else None,
        "best_equity": float(analysis.get("best_equity", 0.0) or 0.0),
        "equity_loss": float(analysis.get("equity_loss", 0.0) or 0.0),
        "ply": int(analysis.get("ply", 0) or 0),
        "has_missed_double": isinstance(analysis.get("missed_double"), dict),
        "alternatives": alts,
        "decision": bool(analysis.get("decision", False)),
        "has_luck": has_luck,
        "illegal_move": bool(analysis.get("illegal_move", False)),
        "luck": float(analysis.get("luck", 0.0) or 0.0) if has_luck else 0.0,
    }


def _build_cube_eval_decision(game_index: int, ply_index: int, analysis: dict, ctype: int) -> dict:
    return {
        "game_index": game_index,
        "ply_index": ply_index,
        "type": ctype,
        "correct_action": _cube_action_code(analysis.get("correct_action")),
        "played_action": _cube_action_code(analysis.get("played_action")),
        "no_double_eq": float(analysis.get("no_double_equity", 0.0) or 0.0),
        "double_take_eq": float(analysis.get("double_take_equity", 0.0) or 0.0),
        "double_pass_eq": float(analysis.get("double_pass_equity", 0.0) or 0.0),
        "probs": _probs_from_eval(analysis.get("eval")) if "eval" in analysis else None,
        "equity_loss": float(analysis.get("equity_loss", 0.0) or 0.0),
        "ply": int(analysis.get("ply", 0) or 0),
        "decision": bool(analysis.get("decision", False)),
        "eval_level": analysis.get("eval_level"),
    }


def _trivial_cube(nd: float, dt: float, dp: float) -> bool:
    """Cube decision so clear it should not count toward PR.

    Mirrors gvanalysis.game_eval._trivial_cube / gvformat.xg.trivialCube /
    stats.py's _trivial_cube.
    """
    return (
        abs(nd - min(dt, dp)) < 0.001
        or (nd - dt) > 0.200
        or (nd - dp) > 0.200
        or (nd < -0.900 and dt < -0.900)
    )


def _missed_double_counts(nd: float, dt: float, dp: float) -> bool:
    """Whether a missed double counts toward PR, from its own three equities.

    Derived here rather than taken from the document, and derived rather than
    assumed. A CUBE type=2 entry has one decision bit and no way to spell "the
    source said nothing", so nothing sets ``missed_double["decision"]`` -- see
    ``bgf.py``/``bgf2gva.js``, which drop BGBlitz's own answer for exactly that
    reason, and ``stats.py``'s ``_missed_double_counts``, which every reader
    falls back to. This writes the bit those readers will compute, so the file
    agrees with them.

    It used to be hardcoded ``True``. Nothing read it back, so nothing broke --
    but a missed double by 0.0002 on a cube nobody had to think about does not
    count, and a stored ``True`` is a claim about that ply which every reader of
    the file disagrees with. One is in the sample corpus.
    """
    doubler_err = max(0.0, min(dt, dp) - nd)
    return not (_trivial_cube(nd, dt, dp) and doubler_err < 0.001)


def _build_cube_eval_missed_double(game_index: int, ply_index: int, md: dict, ev_ply) -> dict:
    return {
        "game_index": game_index,
        "ply_index": ply_index,
        "type": CUBE_TYPE_MISSED_DOUBLE,
        "correct_action": _cube_action_code(md.get("correct_action", "double")),
        "played_action": CUBE_ACTION_NO_DOUBLE,
        "no_double_eq": float(md.get("no_double_equity", 0.0) or 0.0),
        "double_take_eq": float(md.get("double_take_equity", 0.0) or 0.0),
        "double_pass_eq": float(md.get("double_pass_equity", 0.0) or 0.0),
        # The CUBE entry carries its five probability fields whatever its type,
        # so a missed double stores the pre-roll probabilities like any other
        # cube record. They are the only probabilities that describe the cube
        # decision -- the checker ply's own eval is post-roll -- and a reader
        # with nowhere else to turn will otherwise show the checker play's.
        # All-zero still means absent, so files written before this are read
        # exactly as they were.
        "probs": _probs_from_eval(md["eval"]) if "eval" in md else None,
        "equity_loss": float(md.get("equity_loss", 0.0) or 0.0),
        "ply": int(ev_ply or 0),
        "decision": _missed_double_counts(
            float(md.get("no_double_equity", 0.0) or 0.0),
            float(md.get("double_take_equity", 0.0) or 0.0),
            float(md.get("double_pass_equity", 0.0) or 0.0),
        ),
        "eval_level": md.get("eval_level"),
    }


def _build_cube_eval_resign(game_index: int, ply_index: int, analysis: dict) -> dict:
    return {
        "game_index": game_index,
        "ply_index": ply_index,
        "type": CUBE_TYPE_RESIGN,
        "correct_action": CUBE_ACTION_NO_DOUBLE,
        "played_action": CUBE_ACTION_NO_DOUBLE,
        "no_double_eq": float(analysis.get("resign_error", 0.0) or 0.0),
        "double_take_eq": float(analysis.get("take_resign_error", 0.0) or 0.0),
        "double_pass_eq": 0.0,
        "probs": _probs_from_eval(analysis.get("eval")) if "eval" in analysis else None,
        "equity_loss": float(analysis.get("equity_loss", 0.0) or 0.0),
        "ply": int(analysis.get("ply", 0) or 0),
        "decision": bool(analysis.get("decision", True)),
        "eval_level": None,
    }


def _build_cube_eval_live_checker(game_index: int, ply_index: int, cd: dict, ev_ply) -> dict:
    """A correct/non-error live cube on a checker ply (JSON ``cube_decision``
    sub-object) -> base ``CUBE type=4`` (``CUBE_TYPE_LIVE_CHECKER``).

    Mirrors ``ogxm_json.cpp``'s ``parse_ply_analysis_json`` field-for-field:
    ``correct_action`` encodes ``should_double`` (double/no_double only --
    ``cube_decision`` never carries take/pass), ``played_action`` is always
    ``no_double`` (a live decision wasn't "played" either way), and
    ``equity_loss`` is hardcoded 0 (the checker move, not the cube, is where
    any error is scored) -- NOT read from the JSON's own ``equity_loss``.
    ``ply`` reuses the checker eval's own per-decision ``ply`` (``ev_ply``),
    same as ``missed_double``.
    """
    return {
        "game_index": game_index,
        "ply_index": ply_index,
        "type": CUBE_TYPE_LIVE_CHECKER,
        "correct_action": CUBE_ACTION_DOUBLE if cd.get("should_double") else CUBE_ACTION_NO_DOUBLE,
        "played_action": CUBE_ACTION_NO_DOUBLE,
        "no_double_eq": float(cd.get("no_double_equity", 0.0) or 0.0),
        "double_take_eq": float(cd.get("double_take_equity", 0.0) or 0.0),
        "double_pass_eq": float(cd.get("double_pass_equity", 0.0) or 0.0),
        "probs": _probs_from_eval(cd.get("eval")) if "eval" in cd else None,
        "equity_loss": 0.0,
        "ply": int(ev_ply or 0),
        # GammonView GVAN extensions (not read by the reference codec).
        "decision": bool(cd.get("decision", False)),
        "eval_level": cd.get("eval_level"),
    }


# ---------------------------------------------------------------------------
# Base chunk encoders (mirrors ogxm_io.cpp's write_mhdr/write_game/write_anal/
# write_eval/write_alts/write_cube -- struct layouts from ogxm_format.hpp)
# ---------------------------------------------------------------------------

def _encode_mhdr(ogxm: dict, games: list[dict]) -> bytes:
    total_plies = min(sum(len(g.get("plies") or []) for g in games), 0xFFFF)
    # Bits 0-1 (crawford/jacoby) are the base format's; bits 2-3 (beaver/
    # raccoon) are GammonView extensions in what was a reserved bit range --
    # old readers copy the whole byte through unexamined for unknown bits.
    flags = (
        (0x01 if ogxm.get("crawford") else 0)
        | (0x02 if ogxm.get("jacoby") else 0)
        | (0x04 if ogxm.get("beaver") else 0)
        | (0x08 if ogxm.get("raccoon") else 0)
    )
    # GammonView extension: cube_limit packed into the first 2 of MHDR's 8
    # reserved bytes (offset 16-17); the remaining 6 stay 0.
    cube_limit = int(ogxm.get("cube_limit", 0) or 0) & 0xFFFF
    reserved8 = struct.pack("<H", cube_limit) + b"\x00" * 6
    # The two final scores stop at the match length, for the same reason
    # points_won does: a match ends when somebody reaches the target.
    match_length = int(ogxm.get("match_length", 0) or 0)
    hdr = struct.pack(
        "<HBBHHBBIH8s",
        match_length & 0xFFFF,
        len(games) & 0xFF,
        flags & 0xFF,
        clamp_match_score(ogxm.get("white_score", 0), match_length) & 0xFFFF,
        clamp_match_score(ogxm.get("black_score", 0), match_length) & 0xFFFF,
        int(ogxm.get("result", 0) or 0) & 0xFF,
        int(ogxm.get("source", 0) or 0) & 0xFF,
        int(ogxm.get("timestamp", 0) or 0) & 0xFFFFFFFF,
        total_plies,
        reserved8,
    )
    # GammonView extension: `event` and `site` appended after player_black as
    # their own length-prefixed strings. Old readers read the two player-name
    # strings by their own length prefixes and stop; these trailing fields are
    # simply never reached by them (and read_mhdr's own skip-remainder logic
    # consumes them harmlessly for readers that do keep going, i.e. old readers
    # don't even need the skip -- they just never advance this far). A reader
    # that knows `event` but not `site` stops one field earlier, by the same
    # argument, so appending `site` needs no version bump.
    return (
        hdr
        + _pack_string(ogxm.get("player_white", ""))
        + _pack_string(ogxm.get("player_black", ""))
        + _pack_string(ogxm.get("event"))
        + _pack_string(ogxm.get("site"))
    )


def _encode_anal(ply: int, num_checker: int, num_cube: int, timestamp: int,
                  duration_ms: int, model_id: str) -> bytes:
    hdr = struct.pack(
        "<BBHHIIH8s",
        ply & 0xFF, 0,
        num_checker & 0xFFFF, num_cube & 0xFFFF,
        timestamp & 0xFFFFFFFF, duration_ms & 0xFFFFFFFF,
        0, b"\x00" * 8,
    )
    return hdr + _pack_string(model_id)


def _encode_eval_entry(ce: dict) -> bytes:
    probs = ce["probs"] if ce["probs"] is not None else [0.0, 0.0, 0.0, 0.0, 0.0]
    return struct.pack(
        "<BHHHHHHhHBBB",
        ce["game_index"] & 0xFF,
        ce["ply_index"] & 0xFFFF,
        _enc_prob(probs[0]), _enc_prob(probs[1]), _enc_prob(probs[2]),
        _enc_prob(probs[3]), _enc_prob(probs[4]),
        _enc_equity(ce["best_equity"]),
        _enc_equity_loss(ce["equity_loss"]),
        min(len(ce["alternatives"]), MAX_ALTS_PER_DECISION) & 0xFF,
        ce["ply"] & 0xFF,
        (0x01 if ce["has_missed_double"] else 0) & 0xFF,
    )


def _encode_alt_entry(alt: dict) -> bytes:
    move_bytes = bytearray(4)
    steps = alt.get("move") or []
    for i in range(min(4, len(steps))):
        s = steps[i]
        move_bytes[i] = _pack_move_step(int(s.get("from", 0)), int(s.get("pips", 0)))
    probs = alt["probs"] if alt["probs"] is not None else [0.0, 0.0, 0.0, 0.0, 0.0]
    return struct.pack(
        "<4sh5HB",
        bytes(move_bytes),
        _enc_equity(alt["equity"]),
        _enc_prob(probs[0]), _enc_prob(probs[1]), _enc_prob(probs[2]),
        _enc_prob(probs[3]), _enc_prob(probs[4]),
        0x01 if alt["is_played"] else 0,
    )


def _encode_cube_entry(c: dict) -> bytes:
    probs = c["probs"] if c["probs"] is not None else [0.0, 0.0, 0.0, 0.0, 0.0]
    return struct.pack(
        "<BHBhhhHHHHHHBBB3s",
        c["game_index"] & 0xFF,
        c["ply_index"] & 0xFFFF,
        c["type"] & 0xFF,
        _enc_equity(c["no_double_eq"]),
        _enc_equity(c["double_take_eq"]),
        _enc_equity(c["double_pass_eq"]),
        _enc_prob(probs[0]), _enc_prob(probs[1]), _enc_prob(probs[2]),
        _enc_prob(probs[3]), _enc_prob(probs[4]),
        _enc_equity_loss(c["equity_loss"]),
        c["correct_action"] & 0xFF,
        c["played_action"] & 0xFF,
        c["ply"] & 0xFF,
        b"\x00" * 3,
    )


# ---------------------------------------------------------------------------
# GVAN chunk encoder (mirrors OGXM_FORMAT_SPEC_GAMMONVIEW.md's GVAN section --
# a GammonView-only addition; the reference codec neither writes nor reads it).
# GVAN carries only luck + decision + eval_level now: correct-no-doubles and
# missed-doubles both live in base CUBE (type=4/type=2) since the maintainer
# added CUBE type=4 upstream, so there is no separate no-double section here.
# ---------------------------------------------------------------------------

_EVAL_LEVEL_PLY_RE = re.compile(r"^(\d+)ply$")
_EVAL_LEVEL_TRUNC_RE = re.compile(r"^truncated(\d+)$")


def _encode_eval_level(level) -> int:
    if not level:
        return 0
    m = _EVAL_LEVEL_PLY_RE.match(level)
    if m:
        return int(m.group(1)) & 0x0F
    m = _EVAL_LEVEL_TRUNC_RE.match(level)
    if m:
        return (int(m.group(1)) & 0x0F) | 0x10
    if level == "rollout":
        return 0x20
    if level == "database":
        return 0x40
    return 0


def _encode_gvan(analysis_info: dict, checker_evals: list[dict],
                  cube_evals: list[dict]) -> bytes:
    ai = analysis_info or {}
    base_level = _encode_eval_level(ai.get("eval_level"))
    luck_level_raw = ai.get("luck_eval_level")
    luck_level = _encode_eval_level(luck_level_raw) if luck_level_raw else 0x01

    total_alts = sum(len(ce["alternatives"]) for ce in checker_evals)

    section_flags = 0
    if checker_evals:
        section_flags |= 0x01
    if total_alts:
        section_flags |= 0x02
    if cube_evals:
        section_flags |= 0x04

    out = bytearray()
    out += struct.pack("<BBBB", GVAN_VERSION, base_level & 0xFF,
                       section_flags & 0xFF, luck_level & 0xFF)

    if checker_evals:
        for ce in checker_evals:
            flags = 0
            if ce["decision"]:
                flags |= 0x01
            if ce["has_luck"]:
                flags |= 0x02
            if ce["illegal_move"]:
                flags |= 0x04
            luck = _enc_equity(ce["luck"]) if ce["has_luck"] else 0
            out += struct.pack("<Bh", flags & 0xFF, luck)

    if total_alts:
        for ce in checker_evals:
            for alt in ce["alternatives"]:
                out += struct.pack("<B", _encode_eval_level(alt.get("eval_level")) & 0xFF)

    if cube_evals:
        for c in cube_evals:
            flags = 0x01 if c["decision"] else 0
            out += struct.pack("<BB", flags & 0xFF,
                               _encode_eval_level(c.get("eval_level")) & 0xFF)

    return bytes(out)


# ---------------------------------------------------------------------------
# Analysis blocks (one ANAL group per analysis; the format allows up to
# MAX_ANALYSES). `select(ply)` picks which analysis object to serialize for a
# ply -- the single `analysis` for one block, or the matching `analyses[]` entry
# for a given block in the multi-analysis case.
# ---------------------------------------------------------------------------

MAX_ANALYSES = 16


def _gather_evals(games: list[dict], select) -> tuple[list[dict], list[dict]]:
    """Build (checker_evals, cube_evals) for one analysis block by walking every
    ply in game/ply order and reading its analysis object via ``select``."""
    checker_evals: list[dict] = []
    cube_evals: list[dict] = []
    for g in games:
        game_index = int(g.get("game_index", 0) or 0)
        for pi, ply in enumerate(g.get("plies") or []):
            _raw = ply.get("action_id")
            action_id = int(_raw) if _raw is not None else 30
            analysis = select(ply)
            if not isinstance(analysis, dict):
                continue
            if 0 <= action_id <= 20:
                checker_evals.append(_build_checker_eval(game_index, pi, analysis))
                # missed_double and cube_decision are mutually exclusive per ply
                # (mirrors ogxm_json.cpp's if/else): both map to base CUBE
                # entries (type=2 / type=4 respectively).
                md = analysis.get("missed_double")
                cd = analysis.get("cube_decision")
                if isinstance(md, dict):
                    cube_evals.append(_build_cube_eval_missed_double(
                        game_index, pi, md, analysis.get("ply", 0)))
                elif isinstance(cd, dict):
                    cube_evals.append(_build_cube_eval_live_checker(
                        game_index, pi, cd, analysis.get("ply", 0)))
            elif action_id in (ACTION_DOUBLE, ACTION_TAKE, ACTION_DROP):
                ctype = CUBE_TYPE_DOUBLE_DECISION if action_id == ACTION_DOUBLE else CUBE_TYPE_TAKE_PASS
                cube_evals.append(_build_cube_eval_decision(game_index, pi, analysis, ctype))
            elif action_id in (ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH):
                cube_evals.append(_build_cube_eval_resign(game_index, pi, analysis))
    return checker_evals, cube_evals


def _encode_analysis_block(info: dict, checker_evals: list[dict],
                           cube_evals: list[dict]) -> bytes:
    """Serialize one analysis block: ANAL -> [EVAL] -> [ALTS] -> [CUBE] -> GVAN
    (each ancillary chunk omitted when empty, as the reference codec does)."""
    ai = info or {}
    anal_ply = int(ai.get("ply", 0) or 0)
    model_id = ai.get("model_id", "") or ""
    timestamp = int(ai.get("timestamp", 0) or 0)
    duration_ms = int(ai.get("duration_ms", 0) or 0)

    out = bytearray()
    out += _chunk(
        CHUNK_ANAL,
        _encode_anal(anal_ply, len(checker_evals), len(cube_evals), timestamp, duration_ms, model_id),
        critical=False,
    )
    if checker_evals:
        eval_data = b"".join(_encode_eval_entry(ce) for ce in checker_evals)
        out += _chunk(CHUNK_EVAL, eval_data, critical=False)
    if any(ce["alternatives"] for ce in checker_evals):
        alts_data = bytearray()
        for ce in checker_evals:
            for alt in ce["alternatives"]:
                alts_data += _encode_alt_entry(alt)
        out += _chunk(CHUNK_ALTS, bytes(alts_data), critical=False)
    if cube_evals:
        cube_data = b"".join(_encode_cube_entry(c) for c in cube_evals)
        out += _chunk(CHUNK_CUBE, cube_data, critical=False)
    out += _chunk(CHUNK_GVAN, _encode_gvan(ai, checker_evals, cube_evals), critical=False)
    return bytes(out)


def _analysis_blocks(ogxm: dict, games: list[dict]) -> list[tuple[dict, list[dict], list[dict]]]:
    """Resolve the analysis blocks to serialize as ``[(info, checker_evals,
    cube_evals), ...]``, primary first.

    Multi-analysis (``analyses_info`` present): one block per entry, each
    reading its ply ``analyses[]`` entry by ``analysis_index``. Otherwise the
    legacy single block from ``analysis_info`` + per-ply ``analysis``. Empty
    when the match carries no analysis at all.
    """
    analyses_info = ogxm.get("analyses_info")
    if isinstance(analyses_info, list) and analyses_info:
        blocks = []
        for k, info in enumerate(analyses_info):
            def select(ply, k=k):
                for a in ply.get("analyses") or []:
                    if a.get("analysis_index") == k:
                        return a
                return None
            ce, cu = _gather_evals(games, select)
            blocks.append((info or {}, ce, cu))
        return blocks

    ce, cu = _gather_evals(games, lambda ply: ply.get("analysis"))
    analysis_info = ogxm.get("analysis_info")
    if isinstance(analysis_info, dict) or ce or cu:
        return [(analysis_info if isinstance(analysis_info, dict) else {}, ce, cu)]
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_gvab(ogxm: dict) -> bytes:
    """Serialize an OGXM-JSON dict (``ogxm_export.to_ogxm_json(...)`` shape)
    to ``.gvab`` binary bytes.

    Emits, in order: File Header -> MHDR -> GAME (per game) -> one
    [ANAL -> EVAL -> ALTS -> CUBE -> GVAN] group **per analysis block** (0..N,
    primary first; the format allows up to ``MAX_ANALYSES``) -> CSUM -> End
    Marker. A file with more than one block sets ``min_reader_minor = 3`` (pre-
    1.3 readers reject the second ANAL). Pure stdlib; no engine/bgsage calls.
    """
    games = ogxm.get("games") or []
    match_length = int(ogxm.get("match_length", 0) or 0)

    game_chunks: list[bytes] = []

    has_set_position = False
    has_set_position_dice = False

    for g in games:
        plies = g.get("plies") or []
        game_index = int(g.get("game_index", 0) or 0)

        first_to_move = g.get("first_to_move")
        if first_to_move is None:
            first_to_move = int(plies[0].get("color", 0) or 0) if plies else 0xFF

        gflags = (0x01 if g.get("is_crawford") else 0) | (0x02 if g.get("is_lastgame") else 0)
        winner = int(g.get("winner", WINNER_INCOMPLETE) if g.get("winner") is not None else WINNER_INCOMPLETE)
        # Stored verbatim, like the reference writer: this is the game's full
        # value, and capping it to the match length here would destroy the win
        # type (a match-ending 4-point gammon would become indistinguishable
        # from a single). Every consumer that sums the field into a running
        # score caps it there instead -- see cap_points_won.
        points_won = max(0, int(g.get("points_won", 0) or 0)) & 0xFF

        hdr = struct.pack(
            "<BHBBBBB",
            game_index & 0xFF, len(plies) & 0xFFFF, winner & 0xFF,
            points_won, gflags & 0xFF, int(first_to_move) & 0xFF, 0,
        )

        ib = g.get("initial_board")
        if isinstance(ib, list) and len(ib) == 26:
            board_bytes = struct.pack("<26b", *[int(v) for v in ib]) + b"\x00"
        else:
            board_bytes = bytes([DEFAULT_BOARD_SENTINEL, DEFAULT_BOARD_SENTINEL])

        ply_bytes = bytearray()
        for pi, ply in enumerate(plies):
            # NOTE: action_id 0 (Dice 11) is a legitimate value, not "missing" --
            # must NOT be coalesced via `or` (0 is falsy in Python).
            _raw_action_id = ply.get("action_id")
            action_id = int(_raw_action_id) if _raw_action_id is not None else 30
            ply_bytes += _encode_ply(ply, action_id)

            if action_id == ACTION_SET_POSITION:
                has_set_position = True
                d1, d2 = ply.get("d1"), ply.get("d2")
                if isinstance(d1, int) and isinstance(d2, int) and 1 <= d1 <= 6 and 1 <= d2 <= 6:
                    has_set_position_dice = True

        # GAME carries no trailing bytes -- equity<->MWC conversion is
        # compute-on-read (gvformat.met) and needs no per-game/per-decision
        # anchor at all, so the GAME chunk is byte-for-byte identical to the
        # reference codec's own output.
        game_data = hdr + board_bytes + bytes(ply_bytes)
        game_chunks.append(_chunk(CHUNK_GAME, game_data, critical=True))

    blocks = _analysis_blocks(ogxm, games)
    has_analysis = bool(blocks)

    min_reader_minor = 0
    if has_set_position_dice:
        min_reader_minor = 2
    elif has_set_position:
        min_reader_minor = 1
    if len(blocks) > 1:
        # More than one analysis block: a pre-1.3 reader rejects the second
        # ANAL, so it must fail as a clean version mismatch (base spec 1.3).
        min_reader_minor = max(min_reader_minor, 3)

    header_flags = HEADER_FLAG_HAS_ANALYSIS if has_analysis else 0

    out = bytearray()
    out += struct.pack(
        "<IHHHHII",
        OGXM_MAGIC, VERSION_MAJOR, VERSION_MINOR,
        VERSION_MAJOR, min_reader_minor & 0xFFFF,
        0,  # file_size placeholder, patched below
        header_flags & 0xFFFFFFFF,
    )

    out += _chunk(CHUNK_MHDR, _encode_mhdr(ogxm, games), critical=True)
    for gc in game_chunks:
        out += gc

    per_block_extra, trailing_extra = _split_passthrough(ogxm, len(blocks))
    for i, (info, checker_evals, cube_evals) in enumerate(blocks):
        out += _encode_analysis_block(info, checker_evals, cube_evals)
        out += _encode_passthrough(per_block_extra[i])
    # CLCK/VIDO and any other chunk we do not decode: after every analysis
    # block, before CSUM, which is the order the base spec fixes for them.
    out += _encode_passthrough(trailing_extra)

    csum_chunk_start = len(out)
    csum_data = struct.pack("<I", 0) + b"\x00" * 4  # algorithm=CRC32(0) + placeholder value
    out += _chunk(CHUNK_CSUM, csum_data, critical=True)

    total_size = len(out) + 8  # + 8-byte End Marker
    file_size = total_size
    out += struct.pack("<II", END_MAGIC, file_size)

    struct.pack_into("<I", out, 12, file_size)  # patch FileHeader.file_size

    crc = zlib.crc32(bytes(out[:csum_chunk_start])) & 0xFFFFFFFF
    crc_value_offset = csum_chunk_start + 12 + 4  # chunk header (12) + CsumChunkHeader.algorithm (4)
    struct.pack_into("<I", out, crc_value_offset, crc)

    return bytes(out)
