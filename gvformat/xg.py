# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Pure-stdlib converter: eXtreme Gammon ``.xg`` match files → OGXM JSON.

Reads an ``.xg`` file (fixed-size header + JPEG thumbnail + zlib-compressed
record stream), decodes the record stream, and produces a dict conforming to
``OGXM_JSON_SPEC_GAMMONVIEW.md``.

No third-party dependencies — all binary parsing is pure stdlib.

Public API::

    from gvformat.xg import convert_xg

    ogxm = convert_xg(Path("match.xg"))

CLI entry point ``xg2gva`` (added in ``pyproject.toml``).
"""

from __future__ import annotations

import gzip
import json
import struct
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .export import (
    _OGID_ACTION_DOUBLE,
    _OGID_ACTION_NONE,
    _OGID_ACTION_PASS,
    _OGID_ACTION_TAKE,
    _OGID_CUBE_BLACK,
    _OGID_CUBE_WHITE,
    _OGID_STATE_AFTER_TAKE,
    _OGID_STATE_CHECKER_DONE,
    _OGID_STATE_DOUBLE_OFFERED,
    _OGID_STATE_GAME_OVER,
    _OGID_STATE_INITIAL_BOTH,
    _OGID_STATE_ROLLED,
    _STARTING_BOARD_P1,
    _TurnState,
    _canonical_orientation,
    _dice_action_id,
    _flip_board,
    _game_end_action_id,
    _notation_to_steps,
    _notation_to_steps_unsplit,
    _ogid,
    _p1_to_absolute,
    _probs_to_eval,
    _steps_per_roll,
    fit_move_steps,
    set_position_ply,
)
from .binary import RESIGN_ACTIONS
from .notation import canonical_notation, from_xg_p1_frame
from .place import clean_place

# ---------------------------------------------------------------------------
# XG binary format constants
# ---------------------------------------------------------------------------

_MAGIC = 0x484D4752  # "RGMH" in little-endian
_RICH_GAME_HEADER_SIZE = 8232
_SAVE_REC_SIZE = 2560
_NOT_ANALYZED = -1000.0

# Record type enum
_TS_HEADER_MATCH = 0
_TS_HEADER_GAME = 1
_TS_CUBE = 2
_TS_MOVE = 3
_TS_FOOTER_GAME = 4
_TS_FOOTER_MATCH = 5

# Delphi TDateTime epoch
_DELPHI_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Delphi TDateTime → unix timestamp
# ---------------------------------------------------------------------------

def _delphi_datetime_to_unix(dt: float) -> int:
    """Convert Delphi TDateTime to unix timestamp."""
    if dt <= 0:
        return 0
    try:
        from datetime import timedelta
        days = int(dt)
        secs = int((dt - days) * 86400)
        return int((_DELPHI_EPOCH + timedelta(days=days, seconds=secs)).timestamp())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# XG binary file reader
# ---------------------------------------------------------------------------

def _read_pascal_ansi(data: bytes, offset: int) -> str:
    """Read a Pascal-style length-prefixed ANSI string."""
    n = data[offset]
    return data[offset + 1 : offset + 1 + n].decode("latin-1", errors="replace")


def _read_tshort_unicode(data: bytes, offset: int, length: int = 129) -> str:
    """Read a Delphi TShortUnicodeString (129 UTF-16LE code units)."""
    chars = []
    for i in range(length):
        code = struct.unpack_from("<H", data, offset + i * 2)[0]
        if code == 0:
            break
        chars.append(chr(code))
    return "".join(chars)


def _parse_header_match(data: bytes) -> dict:
    """Parse the tsHeaderMatch record (2560 bytes)."""
    player1_u = _read_tshort_unicode(data, 880)
    player2_u = _read_tshort_unicode(data, 1138)
    player1_a = _read_pascal_ansi(data, 9)
    player2_a = _read_pascal_ansi(data, 50)
    match_length = struct.unpack_from("<i", data, 92)[0]
    crawford = bool(data[100])
    jacoby = bool(data[101])
    beaver = bool(data[102])
    elo1 = struct.unpack_from("<d", data, 104)[0]
    elo2 = struct.unpack_from("<d", data, 112)[0]
    date_val = struct.unpack_from("<d", data, 128)[0]
    event_u = _read_tshort_unicode(data, 622)
    event_a = _read_pascal_ansi(data, 136)
    location_u = _read_tshort_unicode(data, 1396)
    cube_limit_code = struct.unpack_from("<i", data, 612)[0]
    return {
        "player1": player1_u or player1_a,
        "player2": player2_u or player2_a,
        "match_length": match_length,
        "crawford": crawford,
        "jacoby": jacoby,
        "beaver": beaver,
        "elo1": elo1,
        "elo2": elo2,
        "date": date_val,
        "event": event_u or event_a,
        "location": location_u,
        "cube_limit_code": cube_limit_code,
    }


def _parse_header_game(data: bytes) -> dict:
    """Parse the tsHeaderGame record."""
    score1 = struct.unpack_from("<i", data, 12)[0]
    score2 = struct.unpack_from("<i", data, 16)[0]
    is_crawford = bool(data[20])
    game_number = struct.unpack_from("<i", data, 48)[0]
    return {
        "score1": score1,
        "score2": score2,
        "is_crawford": is_crawford,
        "game_number": game_number,
    }


def _parse_cube_record(data: bytes) -> dict:
    """Parse a tsCube record."""
    actif = struct.unpack_from("<i", data, 12)[0]      # +1=P1, -1=P2
    doubled = struct.unpack_from("<i", data, 16)[0]     # 0/1/-2
    take = struct.unpack_from("<i", data, 20)[0]         # 0=pass,1=take,2=beaver,-1=N/A
    cube_b = struct.unpack_from("<i", data, 32)[0]       # cube code (0=centered, sign=owner)

    # EngineStructDoubleAction at +64
    dd = 64
    level = struct.unpack_from("<i", data, dd + 28)[0]
    equ_b = struct.unpack_from("<f", data, dd + 88)[0]      # no-double equity
    equ_double = struct.unpack_from("<f", data, dd + 92)[0]  # double-take equity
    equ_drop = struct.unpack_from("<f", data, dd + 96)[0]    # double-pass equity
    # (double_choice at dd+102 is intentionally not read: cube actions are
    # derived from equities, never from XG's unreliable double_choice flag.)

    eval_nd = struct.unpack_from("<7f", data, dd + 60)
    eval_dt = struct.unpack_from("<7f", data, dd + 104)

    err_cube = struct.unpack_from("<d", data, 200)[0]
    err_take = struct.unpack_from("<d", data, 216)[0]

    return {
        "actif": actif,
        "doubled": doubled,
        "take": take,
        "cube_b": cube_b,
        "level": level,
        "equ_b": equ_b,
        "equ_double": equ_double,
        "equ_drop": equ_drop,
        "eval_nd": list(eval_nd),
        "eval_dt": list(eval_dt),
        "err_cube": err_cube,
        "err_take": err_take,
    }


def _parse_move_record(data: bytes) -> dict:
    """Parse a tsMove record."""
    # Position before and after (P1's fixed frame)
    pos_before = list(struct.unpack_from("<26b", data, 9))
    pos_after = list(struct.unpack_from("<26b", data, 35))

    actifp = struct.unpack_from("<i", data, 64)[0]
    moves_raw = list(struct.unpack_from("<8b", data, 68))
    dice = [struct.unpack_from("<i", data, 100 + i * 4)[0] for i in range(2)]
    cube_a = struct.unpack_from("<i", data, 108)[0]
    nmoves = struct.unpack_from("<i", data, 120)[0]
    err_move = struct.unpack_from("<d", data, 2312)[0]
    err_luck = struct.unpack_from("<d", data, 2320)[0]
    comp_choice = struct.unpack_from("<i", data, 2328)[0]
    init_eq = struct.unpack_from("<d", data, 2336)[0]
    analyze_m = struct.unpack_from("<i", data, 2472)[0]
    invalid_m = struct.unpack_from("<i", data, 2480)[0]

    # Parse candidate moves from DataMoves structure
    # PosPlayed: +192, 32 entries of 26 bytes each
    # Moves:     +1024, 32 entries of 8 bytes each
    # EvalLevel: +1280, 32 entries of 4 bytes each
    # Eval:      +1408, 32 entries of 28 bytes each
    candidates = []
    for i in range(min(nmoves, 32)):
        pos_i = list(struct.unpack_from("<26b", data, 192 + i * 26))
        mov_i = list(struct.unpack_from("<8b", data, 1024 + i * 8))
        level_i = struct.unpack_from("<h", data, 1280 + i * 4)[0]
        eval_i = struct.unpack_from("<7f", data, 1408 + i * 28)

        # Convert eval from XG order [loseBG, loseG, loseS, winS, winG, winBG, equity]
        # to standard [winS, winG, winBG, loseG, loseBG]
        probs = [
            eval_i[3],  # winS
            eval_i[4],  # winG
            eval_i[5],  # winBG
            eval_i[1],  # loseG
            eval_i[0],  # loseBG
        ]
        equity = eval_i[6]

        # Find played move index by comparing pos_after with pos_i
        is_played = pos_i == pos_after

        candidates.append({
            "pos": pos_i,
            "moves": mov_i,
            "level": level_i,
            "probs": probs,
            "equity": equity,
            "is_played": is_played,
        })

    # Parse the played move into from/to pairs
    played_from = []
    played_to = []
    i = 0
    while i < len(moves_raw):
        if moves_raw[i] == -1:
            break
        played_from.append(moves_raw[i])
        if i + 1 < len(moves_raw):
            played_to.append(moves_raw[i + 1])
        i += 2

    return {
        "actifp": actifp,
        "pos_before": pos_before,
        "pos_after": pos_after,
        "played_from": played_from,
        "played_to": played_to,
        "dice": dice,
        "cube_a": cube_a,
        "nmoves": nmoves,
        "candidates": candidates,
        "err_move": err_move,
        "err_luck": err_luck,
        "comp_choice": comp_choice,
        "init_eq": init_eq,
        "level": analyze_m,
        "invalid_m": invalid_m,
    }


def _parse_footer_game(data: bytes) -> dict:
    """Parse the tsFooterGame record."""
    score1 = struct.unpack_from("<i", data, 12)[0]
    score2 = struct.unpack_from("<i", data, 16)[0]
    winner = struct.unpack_from("<i", data, 24)[0]
    points = struct.unpack_from("<i", data, 28)[0]
    termination = struct.unpack_from("<i", data, 32)[0]
    return {
        "score1": score1,
        "score2": score2,
        "winner": winner,
        "points": points,
        "termination": termination,
    }


def _parse_footer_match(data: bytes) -> dict:
    """Parse the tsFooterMatch record."""
    score1 = struct.unpack_from("<i", data, 12)[0]
    score2 = struct.unpack_from("<i", data, 16)[0]
    match_result = struct.unpack_from("<i", data, 20)[0]
    return {
        "score1": score1,
        "score2": score2,
        "match_result": match_result,
    }


def read_xg(path: Path) -> list[dict]:
    """Read an XG file and return a list of parsed records."""
    raw = path.read_bytes()

    # Validate magic
    magic = struct.unpack_from("<I", raw, 0)[0]
    if magic != _MAGIC:
        raise ValueError(f"Not an XG file (magic: 0x{magic:08X})")

    # Rich-game header layout: offset 8 = header size, 12 = thumbnail offset
    # (both fixed by _RICH_GAME_HEADER_SIZE, so neither is read here), 20 =
    # thumbnail size, which shifts where the compressed record stream starts.
    thumb_size = struct.unpack_from("<I", raw, 20)[0]

    record_start = _RICH_GAME_HEADER_SIZE + thumb_size
    compressed = raw[record_start:]

    try:
        decompressed = zlib.decompress(compressed)
    except zlib.error:
        decompressed = compressed

    nrecs = len(decompressed) // _SAVE_REC_SIZE
    records = []
    for i in range(nrecs):
        chunk = decompressed[i * _SAVE_REC_SIZE : (i + 1) * _SAVE_REC_SIZE]
        rec_type = chunk[8]
        if rec_type == _TS_HEADER_MATCH:
            records.append(("header_match", _parse_header_match(chunk)))
        elif rec_type == _TS_HEADER_GAME:
            records.append(("header_game", _parse_header_game(chunk)))
        elif rec_type == _TS_CUBE:
            records.append(("cube", _parse_cube_record(chunk)))
        elif rec_type == _TS_MOVE:
            records.append(("move", _parse_move_record(chunk)))
        elif rec_type == _TS_FOOTER_GAME:
            records.append(("footer_game", _parse_footer_game(chunk)))
        elif rec_type == _TS_FOOTER_MATCH:
            records.append(("footer_match", _parse_footer_match(chunk)))
    return records


# ---------------------------------------------------------------------------
# XG eval level name
# ---------------------------------------------------------------------------

# XG's own level codes, mapped onto the canonical OGXM eval levels rather than
# kept under XG's names. The names are not decoration: an eval level has to
# survive ``.gvab``, and GVAN encodes a level as one byte -- a 4-bit depth plus
# the truncated/rollout/database flags -- so anything outside that vocabulary
# encodes as 0, which means "same as the header level" on read. An "xgroller+"
# alternative therefore came back from a saved match claiming the match's plain
# ply depth, silently, while the same file read straight from XG showed the real
# level.
#
# The canonical names fit because they describe what XG actually does. The three
# XG Roller settings *are* short truncated rollouts (1000/1001/1002 ->
# truncated1/2/3), and the two opening-book codes (998/999) are a lookup, not a
# search, which is what ``database`` names. Readers that want XG's own wording
# back can key it off ``analysis_info["model_id"] == "xg"``; nothing is lost that
# the file does not already say.
_LEVEL_NAMES = {
    0: "1ply", 1: "2ply", 2: "3ply", 3: "4ply", 4: "5ply", 5: "6ply", 6: "7ply",
    12: "3ply", 100: "rollout", 998: "database", 999: "database",
    1000: "truncated1", 1001: "truncated2", 1002: "truncated3",
}


def _eval_level_name(code: int) -> str | None:
    # None, not a made-up name, for a code we do not recognise. The vocabulary
    # above is the whole of what a ``.gvab`` can store, so a "level_7" would go
    # the way "xgroller+" used to: encoded as 0, read back as the header level,
    # with the file claiming a depth XG never reported. Saying nothing is the
    # honest answer and the one every caller here already handles -- each
    # assigns ``eval_level`` only if this returns something.
    return _LEVEL_NAMES.get(code)


# Minimum spread (max - min) across a ply's candidate equities for the checker
# play to count as a PR decision. This is not a triviality threshold: XG counts
# essentially every play with a real choice, and only drops the ones where the
# candidates are indistinguishable at its own equity display precision (1e-4).
# In practice that means already-decided positions -- equity saturated at a
# certain single/gammon win or loss, where every legal move scores identically.
#
# Empirically identified against XG's own displayed PR on six player-sides
# (samples/xg: UL-CV6F9j Dunes_04 20.57 / Jade1 8.94, MYdvw1qG Jade1 12.25 /
# Kestrelcove1 8.02, 3WNK_g1Z Iris09 7.41 / Jade1 6.51). Each side pins a unique
# decision count, and the six feasible intervals for this constant intersect in
# (5.0e-5, 1.33e-4] -- 1e-4 sits inside and reproduces all six PRs exactly. The
# previous rule (first-vs-last candidate, >= 0.001) was an order of magnitude
# too aggressive and ran PR up to 1.14 high. Note the candidate list is NOT
# sorted by equity (XG evaluates the tail at a shallower level), so max-min over
# the whole list is the well-defined form; first-vs-last only approximates it.
#
# Unlike the cube thresholds below (whose implied windows are mutually
# inconsistent, hence still approximations), this one is determined by the data.
_CHECKER_SPREAD_EPS = 1e-4


def _has_eval(probs: list[float], equity: float) -> bool:
    """Did XG actually write an evaluation into this record?

    XG leaves a record's probability block all-zero when it has nothing to say
    about the position. A zero *win* probability is not that: a play that is a
    certain loss reads win = 0 with a real gammon_loss beside it, and the last
    few plies of a lost game are full of them. Guarding on ``probs[0] > 0``
    threw those evaluations away, so the top play on such a ply showed
    equities with no probabilities under them.

    An unwritten block is five zeros *and* a zero equity. No genuine
    evaluation is: equity 0 means a roughly even game, which cannot sit beside
    a zero win probability. So the pair separates the two cleanly, and the
    blank-record protection the old guard was really there for survives.

    Mirrors ``_hasEval`` in xg2gva.js.
    """
    return any(p != 0 for p in probs) or equity != 0


def _trivial_cube(nd: float, dt: float, dp: float) -> bool:
    """Cube decision so clear it should not count toward PR. Mirrors
    gvanalysis.game_eval._trivial_cube (kept in-sync; gvformat is zero-dep and
    cannot import the analysis layer)."""
    return (
        abs(nd - min(dt, dp)) < 0.001
        or (nd - dt) > 0.200
        or (nd - dp) > 0.200
        or (nd < -0.900 and dt < -0.900)
    )


def _trivial_take_pass(dt: float, dp: float) -> bool:
    """Take/pass response so clear it should not count toward PR. Mirrors
    gvanalysis.game_eval._trivial_take_pass."""
    return abs(dt - dp) < 0.001


def _xg_embedded_cube(nd: float, dt: float, dp: float) -> tuple[str, dict] | None:
    """Classify a no-double cube decision from its (rounded) equities.

    Returns ``(key, sub_analysis)`` where ``key`` is ``"missed_double"`` (the
    should-have-doubled error) or ``"cube_decision"`` (a correct no-double), or
    ``None`` for an unanalyzed row. The optimal action is derived from the
    equities (double iff ``min(dt, dp) > nd``) to match gvanalysis.game_eval and
    gvformat.export -- NOT XG's ``double_choice`` flag, which is an unreliable
    sentinel. The caller attaches any ``eval`` field, to either key.
    """
    # Unanalyzed rows carry all-zero equities; real cube data normalizes the
    # drop equity to 1.0.
    if dp == 0.0:
        return None
    best = min(dt, dp)
    if best > nd:
        return "missed_double", {
            "no_double_equity": nd,
            "double_take_equity": dt,
            "double_pass_equity": dp,
            "equity_loss": round(best - nd, 4),
            "correct_action": "double",
        }
    return "cube_decision", {
        "should_double": False,
        "no_double_equity": nd,
        "double_take_equity": dt,
        "double_pass_equity": dp,
        "action": "no_double",
        "equity_loss": 0.0,
        "decision": not _trivial_cube(nd, dt, dp),
    }


# ---------------------------------------------------------------------------
# Board: XG P1 frame → absolute (for board_to_ogid with mover_is_white=True)
# ---------------------------------------------------------------------------

def _xg_to_absolute(pos: list[int]) -> list[int]:
    """Convert XG 26-byte P1-frame position to absolute board.

    Absolute convention (for board_to_ogid with mover_is_white=True):
      index 0 = White's bar, 25 = Black's bar, 1-24 signed count
      (positive = White, negative = Black).

    XG encoding:
      pos[0] = P1 off-bar (positive = P1 checkers on bar)
      pos[1..24] = points 1-24 (positive = P1, negative = P2)
      pos[25] = P2 on-bar (negative = P2 checkers on bar)
    """
    board = [0] * 26
    board[25] = pos[0]       # White's bar (P1 on-bar)
    for i in range(1, 25):
        board[i] = pos[i]    # P1 points are positive, P2 are negative
    board[0] = -pos[25]      # Black's bar (P2 on-bar, store as negative)
    return board


# ---------------------------------------------------------------------------
# Move formatting helpers
# ---------------------------------------------------------------------------

def _fmt_xg_move(
    from_pts: list[int],
    to_pts: list[int],
    board: list[int] | None = None,
    mover_is_p1: bool = True,
) -> tuple[str, list[int] | None]:
    """Format XG move as a notation string, annotating hits with ``*``.

    ``from_pts``/``to_pts`` are 0-indexed points in the *mover's* own
    numbering (point n = index n-1, bar = 24, off = -1). ``board``, when
    given, is the raw XG P1-frame position (``pos_before``: index 1-24 signed,
    +P1/-P2) as it stood before this move; hops that land on a lone opponent
    checker get a trailing ``*``. A working copy is advanced hop-by-hop so
    multi-leg plays annotate each hit correctly.

    Returns ``(notation, work)``: ``work`` is the resulting raw P1-frame
    board after every hop (same 26-slot convention as ``board``), or
    ``None`` when ``board`` was ``None``. Callers that need an authoritative
    post-move board (XG's own ``pos_after`` field is documented as
    unreliable -- it "can roll forward across a turn") should use this
    instead of re-deriving one from ``pos_after``.
    """
    work = list(board) if board is not None else None
    # A mover-point n sits at P1-frame index n for P1, else 25-n for P2.
    def p1_index(mover_point: int) -> int:
        return mover_point if mover_is_p1 else 25 - mover_point
    mover_sign = 1 if mover_is_p1 else -1

    parts = []
    for f, t in zip(from_pts, to_pts):
        # -1 is XG's explicit terminator; an all-zero (f==t==0) pair is unused
        # padding or a "no move" (dance) sub-move — a zero-pip 1/1 is never a
        # real play, so stop rather than emit a bogus "1/1".
        if f == -1 or (f == 0 and t == 0):
            break
        f_str = "bar" if f == 24 else str(f + 1)
        # A bear-off destination is any point off the board. XG encodes the
        # exact bear-off as -1, but an overage roll (e.g. bearing a checker
        # off the 1-point with a 5) lands at a more-negative index; clamp all
        # of them to "off" rather than emitting a negative point (1/-4).
        t_str = "off" if t < 0 else str(t + 1)

        hit = ""
        if work is not None:
            # Advance the working board: pull the mover's checker off its
            # source -- the bar (f==24) lives at raw index 0 (P1's own bar,
            # positive count) or 25 (P2's own bar, stored negative); a normal
            # point uses ``p1_index``. The same ``-= mover_sign`` works for
            # both bar slots too (P1: decrements a positive count; P2:
            # increments -- i.e. moves toward zero -- a negative one).
            src_idx = (0 if mover_is_p1 else 25) if f == 24 else p1_index(f + 1)
            work[src_idx] -= mover_sign
            # ...and, for a non-bear-off, land it on the destination, hitting
            # a lone opponent blot (sign == -mover_sign, count 1) if present.
            if t >= 0:
                di = p1_index(t + 1)
                if work[di] == -mover_sign:
                    hit = "*"
                    work[di] = 0
                    # Send the hit opponent checker to *its own* bar: P1's
                    # bar is raw index 0 (positive count), P2's is 25
                    # (stored negative) -- the opposite slot from the
                    # mover's own bar above. Only matters for an
                    # authoritative post-move ``work`` board (the notation
                    # string itself doesn't encode bar counts), but leaving
                    # it out silently underreports the opponent's bar count
                    # on every hit.
                    work[25 if mover_is_p1 else 0] -= mover_sign
                work[di] += mover_sign
        parts.append(f"{f_str}/{t_str}{hit}")
    return " ".join(parts), work


# ---------------------------------------------------------------------------
# Cube-decision emission (shared by the in-move and game-ending double paths)
# ---------------------------------------------------------------------------

def _emit_double_response(
    cd: dict,
    board_before: list[int],
    turn: _TurnState,
    score_white: int,
    score_black: int,
    match_length: int,
    is_crawford: bool,
    eval_level: str | None,
    flip: bool,
) -> tuple[dict, dict, bool]:
    """Build the analyzed doubler ply (``action_id=21``) and its response ply
    (``action_id=22`` take / ``23`` pass) for a real double (``cd["doubled"]
    == 1``).

    ``board_before`` is the board as it stood when the double was offered
    (pre-roll), already in canonical-white's frame (the caller flips it, if
    ``flip``, at the point it's read from ``_xg_to_absolute``). For an
    in-move double this is the following move record's ``pos_before``; for a
    game-ending double/pass (no following move) it is the last-seen
    ``board_after`` (the board doesn't change between the last checker move
    and the double). ``flip`` is whether canonical white is XG's player-2
    (``cd["actif"]`` is XG's raw +1=P1/-1=P2 mover flag, so
    ``dbl_is_white = (cd["actif"] == 1) != flip``). Mutates ``turn``'s OGID
    phase-state fields exactly as the reference replay does (offer ->
    ``_OGID_STATE_DOUBLE_OFFERED``, then ``_OGID_STATE_AFTER_TAKE`` on a take
    or ``_OGID_STATE_GAME_OVER`` on a pass, advancing ``cube_log2``/
    ``cube_owner`` on a take).

    Returns ``(doubler_ply, response_ply, has_take)``; the caller appends
    both plies and, on a take, updates its own ``cube_value``/
    ``cube_owner_bgf`` bookkeeping.
    """
    dbl_is_p1 = cd["actif"] == 1
    dbl_is_white = dbl_is_p1 != flip

    # Doubler ply
    on_roll = "W" if dbl_is_white else "B"
    ogid_before = _ogid(
        board_before, cube_value=turn.cube_value,
        cube_owner=turn.cube_owner, cube_action=turn.cube_action,
        dice=None, on_roll=on_roll, game_state=turn.cur_state,
        score_white=score_white, score_black=score_black,
        match_length=match_length, crawford=is_crawford,
        move_id=turn.move_id,
    )
    turn.awaiting_response = True
    turn.cur_state = _OGID_STATE_DOUBLE_OFFERED
    turn.cube_action = _OGID_ACTION_DOUBLE
    ogid_after = _ogid(
        board_before, cube_value=turn.cube_value,
        cube_owner=turn.cube_owner, cube_action=turn.cube_action,
        dice=None, on_roll="B" if dbl_is_white else "W",
        game_state=turn.cur_state, score_white=score_white,
        score_black=score_black, match_length=match_length,
        crawford=is_crawford, move_id=turn.move_id,
    )

    # Build cube decision analysis
    probs_nd = [
        cd["eval_nd"][3], cd["eval_nd"][4], cd["eval_nd"][5],
        cd["eval_nd"][1], cd["eval_nd"][0],
    ]
    probs_dt = [
        cd["eval_dt"][3], cd["eval_dt"][4], cd["eval_dt"][5],
        cd["eval_dt"][1], cd["eval_dt"][0],
    ]
    # Optimal action from equities (double iff realized double value beats
    # no-double), matching game_eval.
    optimal_action = "double" if min(cd["equ_double"], cd["equ_drop"]) > cd["equ_b"] else "no_double"
    player_action = "double"
    lost_equity = 0.0 if optimal_action == player_action else round(abs(cd["equ_b"] - min(cd["equ_double"], cd["equ_drop"])), 4)

    dbl_analysis = {
        "correct_action": optimal_action,
        "played_action": player_action,
        "no_double_equity": round(cd["equ_b"], 4),
        "double_take_equity": round(cd["equ_double"], 4),
        "double_pass_equity": round(cd["equ_drop"], 4),
        "equity_loss": lost_equity,
        # A double counts toward PR unless the cube is trivial AND the doubler
        # made no error -- mirrors game_eval._eval_cube_decision (doubler_counts)
        # and stats._missed_double_counts.
        "decision": not (_trivial_cube(cd["equ_b"], cd["equ_double"], cd["equ_drop"]) and lost_equity < 0.001),
        "eval": _probs_to_eval(probs_nd) if _has_eval(probs_nd, cd["eval_nd"][6]) else {},
    }
    if eval_level:
        dbl_analysis["eval_level"] = eval_level

    doubler_ply = {
        "color": 1 if dbl_is_white else 0,
        "action_id": 21,
        "ogid_before": ogid_before,
        "ogid_after": ogid_after,
        "analysis": dbl_analysis,
    }

    pending_nd_equity = dbl_analysis.get("no_double_equity")

    # Response ply
    resp_is_white = not dbl_is_white
    has_take = cd["take"] == 1
    resp_action = "take" if has_take else "pass"

    on_roll_r = "W" if resp_is_white else "B"
    ogid_before_r = _ogid(
        board_before, cube_value=turn.cube_value,
        cube_owner=turn.cube_owner, cube_action=_OGID_ACTION_DOUBLE,
        dice=None, on_roll=on_roll_r,
        game_state=_OGID_STATE_DOUBLE_OFFERED,
        score_white=score_white, score_black=score_black,
        match_length=match_length, crawford=is_crawford,
        move_id=turn.move_id,
    )

    turn.awaiting_response = False
    if has_take:
        turn.cube_log2 += 1
        turn.cube_owner = _OGID_CUBE_WHITE if resp_is_white else _OGID_CUBE_BLACK
        turn.cur_state = _OGID_STATE_AFTER_TAKE
        turn.cube_action = _OGID_ACTION_TAKE
    else:
        turn.cur_state = _OGID_STATE_GAME_OVER
        turn.cube_action = _OGID_ACTION_PASS

    ogid_after_r = _ogid(
        board_before, cube_value=turn.cube_value,
        cube_owner=turn.cube_owner, cube_action=turn.cube_action,
        dice=None, on_roll="B" if resp_is_white else "W",
        game_state=turn.cur_state, score_white=score_white,
        score_black=score_black, match_length=match_length,
        crawford=is_crawford, move_id=turn.move_id,
    )

    # Optimal response from equities: take iff the taken equity is no worse
    # for the responder than passing.
    optimal_resp = "take" if cd["equ_double"] <= cd["equ_drop"] else "pass"
    resp_lost = 0.0 if optimal_resp == resp_action else round(abs(cd["equ_double"] - cd["equ_drop"]), 4)

    resp_analysis = {
        "correct_action": optimal_resp,
        "played_action": resp_action,
        "double_take_equity": round(cd["equ_double"], 4),
        "double_pass_equity": round(cd["equ_drop"], 4),
        "equity_loss": resp_lost,
        # A take/pass counts toward PR unless it is trivial (take and pass
        # equities within 0.001) -- mirrors game_eval (resp_counts).
        "decision": not _trivial_take_pass(cd["equ_double"], cd["equ_drop"]),
    }
    if pending_nd_equity is not None:
        resp_analysis["no_double_equity"] = pending_nd_equity
    probs_resp = probs_dt
    if _has_eval(probs_resp, cd["eval_dt"][6]):
        resp_analysis["eval"] = _probs_to_eval(probs_resp)

    response_ply = {
        "color": 1 if resp_is_white else 0,
        "action_id": 22 if has_take else 23,
        "ogid_before": ogid_before_r,
        "ogid_after": ogid_after_r,
        "analysis": resp_analysis,
    }

    return doubler_ply, response_ply, has_take


# ---------------------------------------------------------------------------
# Main converter
# ---------------------------------------------------------------------------

def convert_xg(xg_path: Path) -> dict:
    """Convert an eXtreme Gammon ``.xg`` file to OGXM JSON.

    Returns a dict conforming to ``OGXM_JSON_SPEC_GAMMONVIEW.md``.
    """
    records = read_xg(xg_path)

    header_match = next((r[1] for r in records if r[0] == "header_match"), None)
    if header_match is None:
        raise ValueError("No header_match record found")

    name_p1 = header_match["player1"]
    name_p2 = header_match["player2"]

    # Canonical orientation: XG stores the account player as P1, so hardcoding
    # white=P1 disagrees with other sources' P1. `flip` is whether canonical
    # white is XG's P2 -- every P1-frame board must then be mirrored
    # (`_flip_board`) before it reaches `_ogid`, every P1-relative mover flag
    # must be corrected to `is_white = is_p1 != flip` for white/black-facing
    # fields (color, on_roll, cube_owner), and the two scores are swapped.
    # Candidate/played moves in mover-own numbering are untouched; only the
    # `_notation_to_steps` call (mover-own -> absolute white numbering) needs
    # `is_white`, NOT the `_fmt_xg_move` hit-detection call (which stays
    # `is_p1`, a frame-internal fact about the raw P1-frame board it's diffing).
    player_white, player_black, p1_is_white = _canonical_orientation(name_p1, name_p2)
    flip = not p1_is_white

    match_length = header_match["match_length"]
    is_money_game = match_length == 0 or match_length == 99999
    if is_money_game:
        match_length = 0
    crawford = header_match["crawford"]
    jacoby = header_match["jacoby"]
    beaver = header_match["beaver"]
    cube_limit_code = header_match["cube_limit_code"]
    cube_limit = 1 << cube_limit_code if cube_limit_code >= 0 else 64
    timestamp = _delphi_datetime_to_unix(header_match["date"])

    # XG's `location` is OGXM's `site`; the two stay separate fields.
    event_str = clean_place(header_match["event"])
    site_str = clean_place(header_match["location"])

    # Iterate records to build games
    games_out: list[dict] = []
    gi = 0
    score_p1 = 0
    score_p2 = 0

    idx = 0
    # Tracks the last-seen post-move board across the whole file (mirrors
    # gvformat-js's `lastBoard`). Used as the pre-double board when a game
    # ends on a double with no following move record -- the board doesn't
    # change between the last checker move and the double offer.
    board_after: list[int] = [0] * 26
    while idx < len(records):
        rtype, rdata = records[idx]

        if rtype == "header_game":
            score_p1 = rdata["score1"]
            score_p2 = rdata["score2"]
            score_white = score_p2 if flip else score_p1
            score_black = score_p1 if flip else score_p2
            is_crawford = rdata["is_crawford"]
            game_index = gi

            # Cube value/owner live on `turn` (cube_log2 / cube_owner); it is
            # the single tracker every ply emits from.
            turn = _TurnState()
            plies: list[dict] = []

            # Persistent RAW P1-frame board tracker (XG's own ``pos`` array
            # convention: index 0 = P1's bar (positive), 1-24 signed
            # (+P1/-P2), 25 = P2's bar (negative)), reset every game to the
            # standard starting position. XG's per-record ``pos_before``/
            # ``pos_after`` are both individually unreliable (see the
            # comment at the played-move reconstruction below), so every
            # checker ply reads its "before" board from this tracker
            # (carried forward from the previous ply's authoritative
            # ``played_work`` outcome) instead of trusting either raw field.
            # ``_STARTING_BOARD_P1``'s 1-24 entries are numerically the same
            # array XG's raw pos-frame uses at the start of a game (both are
            # "positive = P1"); bar slots are 0 either way at game start, so
            # the sign-convention difference at index 25 doesn't matter yet.
            raw_board = list(_STARTING_BOARD_P1)

            idx += 1
            pending_cube = None

            while idx < len(records):
                rtype2, rdata2 = records[idx]

                if rtype2 == "footer_game":
                    # A game-ending double/pass has no following move record
                    # -- the game is over, so the loop reaches footer_game
                    # directly with the analyzed double still pending. A
                    # taken double never reaches here (the game continues, so
                    # a move record follows and the in-move path above
                    # already consumed pending_cube, clearing it to None).
                    if pending_cube is not None and pending_cube["doubled"] == 1:
                        cd = pending_cube
                        pending_cube = None
                        eval_level = _eval_level_name(cd["level"])
                        doubler_ply, response_ply, has_take = _emit_double_response(
                            cd, board_after, turn, score_white, score_black,
                            match_length, is_crawford, eval_level, flip,
                        )
                        plies.append(doubler_ply)
                        plies.append(response_ply)
                    idx += 1
                    break

                if rtype2 == "cube":
                    pending_cube = rdata2
                    idx += 1
                    continue

                if rtype2 == "move":
                    m = rdata2
                    d1, d2 = m["dice"]
                    actifp = m["actifp"]
                    is_p1 = actifp == 1
                    is_white = is_p1 != flip

                    # Board in absolute coordinates, from the persistent
                    # RAW P1-frame tracker (NOT ``m["pos_before"]`` -- see
                    # ``raw_board``'s comment above), mirrored into canonical
                    # white's frame when white is XG's P2. ``pre_move_raw``
                    # keeps the pre-move RAW frame around for the
                    # alternatives loop below, which runs *after*
                    # ``raw_board`` has already been advanced to the played
                    # move's outcome.
                    pre_move_raw = raw_board
                    board_before = _xg_to_absolute(raw_board)
                    if flip:
                        board_before = _flip_board(board_before)

                    # Eval level
                    eval_level = _eval_level_name(m["level"])

                    # ── Handle cube pair (tsCube + tsMove) ──────────────
                    if pending_cube is not None:
                        cd = pending_cube

                        if cd["doubled"] == 1:
                            pending_cube = None
                            # Actual double: emit doubler + response plies
                            doubler_ply, response_ply, has_take = _emit_double_response(
                                cd, board_before, turn, score_white, score_black,
                                match_length, is_crawford, eval_level, flip,
                            )
                            plies.append(doubler_ply)
                            plies.append(response_ply)

                            if not has_take:
                                # Game ended by pass — skip the checker move
                                idx += 1
                                break

                        elif cd["doubled"] == 0:
                            # No double — leave pending_cube set so the embedded
                            # block below attaches it to this checker move.
                            pass

                        else:
                            # Passive/other cube record — discard.
                            pending_cube = None

                    # ── Checker move ──────────────────────────────────────
                    on_roll = "W" if is_white else "B"
                    before_state = _OGID_STATE_INITIAL_BOTH if turn.is_first_ply else _OGID_STATE_ROLLED

                    ogid_before = _ogid(
                        board_before, cube_value=turn.cube_value,
                        cube_owner=turn.cube_owner, cube_action=turn.cube_action,
                        dice=tuple(sorted(m["dice"], reverse=True)) if m["dice"][0] else None,
                        on_roll=on_roll, game_state=before_state,
                        score_white=score_white, score_black=score_black,
                        match_length=match_length, crawford=is_crawford,
                        move_id=turn.move_id,
                    )

                    turn.move_id += 1
                    turn.is_first_ply = False
                    turn.cur_state = _OGID_STATE_CHECKER_DONE
                    turn.cube_action = _OGID_ACTION_NONE

                    # Reconstruct the played move from the *played candidate's*
                    # from/to pairs, NOT a board diff and NOT the +68
                    # ``moves_raw`` field -- both misencode the play here. XG's
                    # ``pos_before``/``pos_after`` pair is not a clean single-ply
                    # transition (its stored position can roll forward across a
                    # turn) and its ``actifp`` mover flag is unreliable (``-1``
                    # occurs), so a board diff fabricates impossible plays (>4
                    # hops, phantom bear-offs); ``moves_raw`` at +68 is in a
                    # different encoding again. The candidate DataMoves entry
                    # (``c["moves"]``) is authoritative -- render the played one
                    # through the same notation path the alternatives use
                    # (``_fmt_xg_move`` -> ``_notation_to_steps``) so the
                    # top-level ``moves`` matches the played alternative exactly.
                    # A dance has no distinct played candidate -> emit nothing.
                    d1, d2 = m["dice"]
                    _played_cand = next(
                        (c for c in m["candidates"] if c["is_played"]), None,
                    )
                    if _played_cand is not None:
                        # ``_fmt_xg_move`` renders a no-move (dance) candidate as
                        # an empty string, so a dance yields no steps here.
                        # ``mover_is_p1=is_p1`` here is deliberate (NOT flipped):
                        # it's used only for hit-detection against the raw
                        # P1-frame ``raw_board``, a frame-internal fact.
                        # ``_notation_to_steps``, below, maps mover-own numbering
                        # to ABSOLUTE white numbering, so it needs the
                        # flip-corrected ``is_white``.
                        played_notation, played_work = _fmt_xg_move(
                            _played_cand["moves"][:8:2], _played_cand["moves"][1:8:2],
                            board=raw_board, mover_is_p1=is_p1,
                        )
                        # Hand the splitter the pre-move board in the mover's
                        # own numbering. XG stores a one-checker two-die play as
                        # a single span ("24/18" off a 5-1), leaving the
                        # intermediate point to be inferred, and only one of the
                        # two routes may be open. Without the board the
                        # tie-break takes the larger die first and can route the
                        # checker through a point the opponent has *made*, which
                        # replays as a hit: their five checkers become one of
                        # yours plus a bar checker, and every board after this
                        # ply is wrong.
                        move_steps = _notation_to_steps(
                            played_notation, is_white, d1, d2,
                            board=(board_before if is_white
                                   else _flip_board(board_before)),
                        )
                        # An illegal play (``invalid_m == 2``: a rules violation
                        # the site let through) can use more die-moves than the
                        # roll has -- 13/9 with a 3-1, then 12/11, is three hops
                        # for a two-hop roll. A ply record holds exactly the
                        # roll's hops, so the extra step would be dropped on
                        # write and every board replayed after this ply would be
                        # one checker off; a later ply then lifts a checker off
                        # an empty point, which mints checkers until the position
                        # is impossible. Record each span at its own pip distance
                        # instead: the dice cannot explain these hops anyway, and
                        # it replays to the board XG recorded.
                        fitted = fit_move_steps(
                            move_steps, played_notation, is_white, d1, d2)
                        if fitted is not None:
                            move_steps = fitted
                        else:
                            # Still overflowing at one step per span. Keep the
                            # collapsed form so the check below sees it is too
                            # long and demotes the ply to a set position.
                            move_steps = _notation_to_steps_unsplit(
                                played_notation, is_white, d1, d2)
                        # XG's own ``pos_after`` "can roll forward across a
                        # turn" (see comment above) -- rederive board_after
                        # from the authoritative played move instead, and
                        # carry it forward as the tracker for the next ply.
                        raw_board = played_work
                        board_after = _xg_to_absolute(raw_board)
                        if flip:
                            board_after = _flip_board(board_after)
                    else:
                        # A true dance: no checkers moved, so the board is
                        # unchanged (raw_board stays as-is for the next ply).
                        move_steps = []
                        board_after = board_before

                    ogid_after = _ogid(
                        board_after, cube_value=turn.cube_value,
                        cube_owner=turn.cube_owner, cube_action=turn.cube_action,
                        dice=None, on_roll="B" if is_white else "W",
                        game_state=turn.cur_state, score_white=score_white,
                        score_black=score_black, match_length=match_length,
                        crawford=is_crawford, move_id=turn.move_id,
                    )

                    action_id = _dice_action_id(d1, d2) if d1 and d2 else 30

                    # Build checker analysis
                    analysis = None
                    if m["candidates"]:
                        # XG flags an illegal play (a rules violation the player
                        # actually made) with invalid_m == 2 -- mirror the JS
                        # (xg2gva.js). The played candidate is the illegal move;
                        # size its error against the best *legal* alternative
                        # (excluding the played one), and flag the ply so PR /
                        # decision counting excludes it (see gvformat.stats).
                        is_illegal = m["invalid_m"] == 2
                        played_idx = next(
                            (i for i, c in enumerate(m["candidates"]) if c["is_played"]),
                            None,
                        )
                        if is_illegal and played_idx is not None and len(m["candidates"]) > 1:
                            legal_eqs = [
                                c["equity"] for i, c in enumerate(m["candidates"])
                                if i != played_idx
                            ]
                            best_eq = max(legal_eqs) if legal_eqs else m["candidates"][0]["equity"]
                        else:
                            best_eq = m["candidates"][0]["equity"]

                        played_eq = best_eq
                        for c in m["candidates"]:
                            if c["is_played"]:
                                played_eq = c["equity"]
                                break

                        equity_loss = round(max(0.0, best_eq - played_eq), 4)
                        err_move = m["err_move"]
                        is_analyzed = err_move > _NOT_ANALYZED + 1
                        # A checker play counts toward PR only if XG analyzed it
                        # AND there was a genuine choice: candidate equities
                        # spanning at least _CHECKER_SPREAD_EPS. Forced moves (a
                        # single candidate) and already-decided positions (every
                        # option scores the same) are excluded, as are illegal
                        # plies.
                        cand_eqs = [c["equity"] for c in m["candidates"]]
                        spread = (
                            max(cand_eqs) - min(cand_eqs) if len(cand_eqs) >= 2 else 0.0
                        )
                        is_decision = (
                            is_analyzed and not is_illegal
                            and spread >= _CHECKER_SPREAD_EPS
                        )

                        analysis = {
                            "best_equity": round(best_eq, 4),
                            "played_equity": round(played_eq, 4),
                            "equity_loss": equity_loss,
                            "decision": is_decision,
                        }
                        if is_illegal:
                            analysis["illegal_move"] = True

                        # XG's ErrLuck is this roll's `postroll - preroll` equity,
                        # the same quantity the spec's `luck` field carries.
                        # Unanalyzed rolls carry the NOT_ANALYZED sentinel rather
                        # than 0, so guard on it -- a genuine luck of exactly 0.0
                        # is meaningful and must survive (mirrors xg2gva.js).
                        if m["err_luck"] > _NOT_ANALYZED + 1:
                            analysis["luck"] = round(m["err_luck"], 4)

                        # Eval from best candidate
                        best_cand = m["candidates"][0]
                        if _has_eval(best_cand["probs"], best_cand["equity"]):
                            analysis["eval"] = _probs_to_eval(best_cand["probs"])

                        if eval_level:
                            analysis["eval_level"] = eval_level

                        # Alternatives
                        alts = []
                        for c in m["candidates"]:
                            alt: dict = {
                                "equity": round(c["equity"], 4),
                                "is_played": c["is_played"],
                                "diff": round(c["equity"] - best_eq, 4),
                            }
                            if _has_eval(c["probs"], c["equity"]):
                                alt["eval"] = _probs_to_eval(c["probs"])
                            lvl = _eval_level_name(c["level"])
                            if lvl:
                                alt["eval_level"] = lvl
                            # Per the OGXM spec, ``move`` is the structured
                            # source of truth (Step[] of {from, pips} in absolute
                            # coords) and ``notation`` is its derived display
                            # string. Render the per-hop mover-frame string first
                            # (with hit ``*`` markers, and the candidate's own
                            # post-move board ``cand_work``), then parse it into
                            # absolute steps with the same helper the analyze
                            # path uses, so XG-import and analyze output are
                            # byte-identical (and .gvab-able). The *display*
                            # ``notation`` is rendered separately through the
                            # shared board-diff canonicalizer (collapse hops,
                            # combine identical legs) so it matches the analyzer
                            # and BGF paths exactly.
                            notation, cand_work = _fmt_xg_move(
                                c["moves"][:8:2], c["moves"][1:8:2],
                                board=pre_move_raw, mover_is_p1=is_p1,
                            )
                            # Same pre-move board the played move is split
                            # against (see the ``_notation_to_steps`` call
                            # above). Every candidate starts from this ply's
                            # position, and without the board a one-checker
                            # two-die alternative is split larger-die-first and
                            # can be drawn routing through a point the opponent
                            # has made -- the board arrows come straight from
                            # these steps, so it shows a checker landing on a
                            # stack of enemy checkers and moving on.
                            alt["move"] = _notation_to_steps(
                                notation, is_white, d1, d2,
                                board_before if is_white else _flip_board(board_before),
                            )
                            # Same collapse the played move gets above, so the
                            # played alternative still describes the same play
                            # as the ply's own ``moves``. Only an illegal play
                            # can overflow, and only the played candidate is
                            # ever illegal.
                            if len(alt["move"]) > _steps_per_roll(d1, d2):
                                alt["move"] = _notation_to_steps_unsplit(
                                    notation, is_white, d1, d2)
                            alt["notation"] = canonical_notation(
                                from_xg_p1_frame(pre_move_raw, is_p1),
                                from_xg_p1_frame(cand_work, is_p1),
                                d1, d2,
                            )
                            alts.append(alt)
                        analysis["alternatives"] = alts

                    # Embedded cube analysis (no-double decision). Classify by
                    # equity (min(dt,dp) > nd), matching gvanalysis.game_eval and
                    # gvformat.export -- NOT XG's double_choice flag, which is an
                    # unreliable sentinel here (often -1 with real equities, and
                    # sometimes -1 over a genuine missed double).
                    if pending_cube is not None and pending_cube["doubled"] == 0:
                        cd = pending_cube
                        pending_cube = None
                        result = _xg_embedded_cube(
                            round(cd["equ_b"], 4),
                            round(cd["equ_double"], 4),
                            round(cd["equ_drop"], 4),
                        )
                        if result is not None and analysis is not None:
                            key, emb = result
                            # XG's no-double evaluation is the pre-roll read of
                            # the cube decision, so it belongs on either
                            # classification -- a missed double is the same
                            # position, judged the other way.
                            probs_nd = [
                                cd["eval_nd"][3], cd["eval_nd"][4], cd["eval_nd"][5],
                                cd["eval_nd"][1], cd["eval_nd"][0],
                            ]
                            if _has_eval(probs_nd, cd["eval_nd"][6]):
                                emb["eval"] = _probs_to_eval(probs_nd)
                            analysis[key] = emb

                    if (0 <= action_id <= 20
                            and len(move_steps) > _steps_per_roll(d1, d2)):
                        # An illegal play too tangled for even one step per
                        # checker (three checkers moved on a two-hop roll, say).
                        # No checker ply can carry it, and truncating it would
                        # corrupt every board after this one, so state the
                        # resulting position outright -- what action 31 is for
                        # (the spec notes its optional dice are exactly this
                        # case). The play itself is lost; it broke the rules, so
                        # there is no move to score.
                        ply: dict = set_position_ply(
                            is_white, d1, d2, board_after, ogid_before, ogid_after)
                    else:
                        ply = {
                            "color": 1 if is_white else 0,
                            "action_id": action_id,
                            "d1": d1,
                            "d2": d2,
                            "moves": move_steps,
                            "ogid_before": ogid_before,
                            "ogid_after": ogid_after,
                        }
                        if analysis:
                            ply["analysis"] = analysis
                    plies.append(ply)

                    idx += 1
                    continue

                idx += 1

            # ── Game result ──────────────────────────────────────────────
            footer = next(
                (r[1] for r in records[idx - 1 : idx + 5] if r[0] == "footer_game"),
                None,
            )
            if footer:
                won_pts = footer["points"]
                winner_p1 = footer["winner"] == 1
                winner_is_white = winner_p1 != flip

                # Termination type
                term = footer["termination"]
                base = term % 100
                modifier = term - base
                if modifier == 100:
                    result_type = "resign"
                elif base == 2:
                    result_type = "gammon"
                elif base == 3:
                    result_type = "backgammon"
                else:
                    result_type = "normal"

                match_complete = bool(
                    match_length and (
                        (winner_p1 and score_p1 + won_pts >= match_length)
                        or (not winner_p1 and score_p2 + won_pts >= match_length)
                    )
                )

                action_id = _game_end_action_id(result_type, match_complete)
                # A terminal ply names the winner: nobody *does* a game-over, and
                # ogxm_replay.cpp just carries the winner through. Resignation is the
                # one exception -- it is an act, and the player who resigns is the one
                # who lost -- so 27/28 gets the resigner, both as the ply's color
                # (`color` is documented as the player a ply belongs to) and as the
                # player on roll (the resigner is the one facing the roll they chose
                # not to take).
                actor_is_white = (
                    not winner_is_white if action_id in RESIGN_ACTIONS else winner_is_white
                )
                on_roll = "W" if actor_is_white else "B"

                end_state = turn.cur_state
                end_cube_action = turn.cube_action
                ogid_before_end = _ogid(
                    board_after if m else [0]*26, cube_value=turn.cube_value,
                    cube_owner=turn.cube_owner, cube_action=end_cube_action,
                    dice=None, on_roll=on_roll, game_state=end_state,
                    score_white=score_white, score_black=score_black,
                    match_length=match_length, crawford=is_crawford,
                    move_id=turn.move_id,
                )
                ogid_after_end = _ogid(
                    board_after if m else [0]*26, cube_value=turn.cube_value,
                    cube_owner=turn.cube_owner, cube_action=end_cube_action,
                    dice=None, on_roll="B" if actor_is_white else "W",
                    game_state=end_state, score_white=score_white,
                    score_black=score_black, match_length=match_length,
                    crawford=is_crawford, move_id=turn.move_id,
                )
                plies.append({
                    "color": 1 if actor_is_white else 0,
                    "action_id": action_id,
                    "ogid_before": ogid_before_end,
                    "ogid_after": ogid_after_end,
                })

            games_out.append({
                "game_index": game_index,
                "winner": 0 if winner_is_white else 1,
                "points_won": won_pts,
                "is_crawford": is_crawford,
                "plies": plies,
            })
            gi += 1
            continue

        idx += 1

    # ── Match-level output ───────────────────────────────────────────────
    footer_match = next((r[1] for r in records if r[0] == "footer_match"), None)
    final_p1 = footer_match["score1"] if footer_match else score_p1
    final_p2 = footer_match["score2"] if footer_match else score_p2
    final_white = final_p2 if flip else final_p1
    final_black = final_p1 if flip else final_p2

    if match_length and final_white >= match_length:
        match_result = 1
    elif match_length and final_black >= match_length:
        match_result = 2
    else:
        match_result = 0

    # Find max eval level across all games
    max_ply = 0
    for rtype, rdata in records:
        if rtype == "move":
            lvl = rdata.get("level", 0)
            if lvl > max_ply and lvl < 100:
                max_ply = lvl

    ply_depth = max_ply + 1 if max_ply > 0 else 3

    ogxm: dict = {
        "match_length": match_length,
        "player_white": player_white,
        "player_black": player_black,
        "white_score": final_white,
        "black_score": final_black,
        "result": match_result,
        "source": 2,  # xg_import
        "timestamp": timestamp,
        "crawford": crawford,
        "jacoby": jacoby,
        "beaver": beaver,
        "cube_limit": cube_limit,
        "event": event_str,
        "site": site_str,
        "analysis_info": {
            "ply": ply_depth,
            "eval_level": f"{ply_depth}ply",
            "model_id": "xg",
            "timestamp": timestamp,
        },
        "games": games_out,
    }
    return ogxm


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Convert an eXtreme Gammon .xg file to OGXM JSON.")
    parser.add_argument("xg_file", help="Input .xg file")
    parser.add_argument(
        "output",
        nargs="?",
        metavar="OUTPUT",
        help="Output file (default: input name with .gva extension)",
    )
    parser.add_argument(
        "-z", "--compress",
        action="store_true",
        help="Write gzip-compressed output (implied by .gz output filename)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON (default: compact)",
    )
    args = parser.parse_args()

    xg_path = Path(args.xg_file)
    if not xg_path.exists():
        print(f"File not found: {xg_path}", file=sys.stderr)
        sys.exit(1)

    out = convert_xg(xg_path)

    out_path = (
        Path(args.output) if args.output
        else xg_path.with_suffix(".gva.gz" if args.compress else ".gva")
    )
    indent = 2 if args.pretty else None
    seps = None if args.pretty else (",", ":")
    data = json.dumps(out, indent=indent, separators=seps, ensure_ascii=False).encode()
    if args.compress or out_path.suffix == ".gz":
        with gzip.open(out_path, "wb") as f:
            f.write(data)
    else:
        out_path.write_bytes(data)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
