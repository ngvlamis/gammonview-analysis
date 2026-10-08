# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Writing OGXM v2 -- HedgeHog's current match format -- from our document.

The inverse of ``ogxm2.read_ogxm2`` for the documents this package produces:
``read_ogxm2(write_ogxm2(D))`` is ``D`` again, up to the normalizations listed
in ``docs/OGXM_V2_PROFILE.md``. Mirrors ``gvformat-js/src/ogxm2_writer.js``.

Everything is written as plain v2 that any conforming reader loads. What v2
has no field for travels in ``ANNO`` records whose keys start ``x-`` -- the
namespace the spec reserves for producers outside it (N6), which a conforming
reader keeps and never interprets:

``x-gammonview-analysis/<analysis_id>`` (match scope, one per block)
    Marks the block as ours and lists the decisions whose PR-counting flag
    differs from the rule the reader derives it by (``ogxm2.default_flags``).
    It also carries ``frame=`` when a source normalized by a match equity table
    that is not ours (BGBlitz's): ``<ply_ref>:<mid_white>:<half>`` entries, each
    in force from its ply until the next, stating the MWC frame (``mwc_frame``
    on the document's analyses) so the file holds the source's own MWCs.
``x-gammonview-decisions/<analysis_id>`` (ply scope)
    Decision records the block holds for a ply that the ``DECS`` stream cannot
    take as they are: everything on an illegal play (v2 allows no decision on
    the ``set position`` ply that records one), and a checker decision that
    breaks a v2 invariant (A1/A3/A5, or an equity loss the played move's
    equity does not give). Our reader prefers these to ``DECS``.
``x-gammonview-illegal-ply`` (ply scope)
    The steps of an illegal play that our document holds as a dice ply.
``x-gammonview-site``, ``x-gammonview-event`` (match scope)
    A ``site`` (v2's is a host name) and an over-long ``event``.

Every value begins with a format version, ``1:``.

Units. A match block is written in v2's ``cubeful match`` currency -- MWC --
converted through each ply's own score frame (``basefill.mwc_frame_inverse``),
which is how the reader converts it back. A money block is cubeful money,
written as is.
"""

from __future__ import annotations

import base64
import copy
import math
import re
import struct
import uuid
import zlib
from urllib.parse import quote

from .basefill import frame_key, frame_perspective_is_white, mwc_frame_inverse
from .binary import DICE_TABLE, _round_c
from .export import _STARTING_BOARD_P1, _flip_board, _p1_to_absolute
from .legality import is_play_legal
from .reader import _absolute_to_p1, _apply_moves_p1, _derive_ogids
from . import ogxm2 as R
from . import ogxm2_passthrough as P

MAX_STRING = 4096
MAX_ALTS = 1024
MAX_EVENT = 120

SCOPE_MATCH = 0
SCOPE_PLY = 2

_MARKERS = (24, 25, 26, 30)
_RESIGNS = (R.ACTION_RESIGN_GAME, R.ACTION_RESIGN_MATCH)
_FORFEIT = 29

#: Characters a label keeps unescaped in an annotation value; ``;`` and ``=``
#: delimit its items.
_SAFE = " +-_./()"

_VERDICT = {"no_double": 0, "double": 1, "take": 2, "pass": 3, "too_good": 4,
            "beaver": 5, "raccoon": 6}
_OFFER_VERDICTS = (0, 1, 4)
_RESPONSE_VERDICTS = (2, 3, 5, 6)

#: An equity loss that the played move's own equity does not give, past this,
#: is a different estimate of the play (not rounding), so the decision is kept
#: exactly in the annotation. Two 1e-4 roundings apart is still the same number.
_LOSS_TOLERANCE = 1.5e-4


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def _varint(n: int) -> bytes:
    if n < 0:
        raise ValueError(f"OGXM v2 varint cannot hold {n}")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _record(mandatory: bytes, fields: dict[int, bytes]) -> bytes:
    """A length-prefixed record (3.2): mask, mandatory fields, then the present
    optional fields in ascending bit order."""
    mask = 0
    body = bytearray(mandatory)
    for bit in sorted(fields):
        mask |= 1 << bit
        body += fields[bit]
    inner = _varint(mask) + bytes(body)
    return _varint(len(inner)) + inner


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) > MAX_STRING:
        raise ValueError(f"string of {len(b)} bytes exceeds OGXM v2's {MAX_STRING}")
    return _varint(len(b)) + b


def _equity(v: float) -> bytes:
    n = _round_c(float(v) * 1e6)
    return struct.pack("<i", max(-0x80000000, min(0x7FFFFFFF, n)))


def _loss(v: float) -> bytes:
    n = _round_c(max(0.0, float(v)) * 1e6)
    return struct.pack("<I", min(0xFFFFFFFF, n))


def _prob(p: float) -> bytes:
    p = 0.0 if p < 0.0 else (1.0 if p > 1.0 else p)
    return struct.pack("<H", _round_c(p * 10000.0))


def _probs(ev: dict) -> bytes:
    return b"".join(_prob(float(ev.get(k, 0.0) or 0.0))
                    for k in ("win", "gammon_win", "bg_win", "gammon_loss", "bg_loss"))


def _board(b) -> bytes:
    return struct.pack("<26b", *[int(v) for v in b])


def _step_byte(m: dict) -> int | None:
    frm, pips = int(m.get("from", -1)), int(m.get("pips", 0))
    if not (0 <= frm <= 25 and 1 <= pips <= 6):
        return None
    return (pips << 5) | frm


def _section(stype: bytes, payload: bytes, critical: bool) -> bytes:
    return stype + struct.pack("<IB", len(payload), 1 if critical else 0) + payload


def _seat(color) -> int:
    """v1's colour polarity is the Seat enum's reverse: colour 1 is seat 0."""
    return 0 if color else 1


def _uuid_str(b: bytes) -> str:
    return str(uuid.UUID(bytes=b))


# ---------------------------------------------------------------------------
# Levels (6.4)
# ---------------------------------------------------------------------------

def _level_bytes(lv: dict) -> bytes:
    fields: dict[int, bytes] = {}
    if "preset" in lv:
        fields[0] = _str(lv["preset"])
    if "checker_ply" in lv:
        fields[1] = _varint(lv["checker_ply"])
    if "cube_ply" in lv:
        fields[2] = _varint(lv["cube_ply"])
    return _record(b"", fields)


def _override(parent: dict, want: dict) -> dict:
    """The fields of ``want`` that differ from the inherited level (L1)."""
    return {k: v for k, v in want.items() if v is not None and parent.get(k) != v}


# ---------------------------------------------------------------------------
# The match
# ---------------------------------------------------------------------------

def _mover_frame(board_p1: list[int], mover_is_white: bool):
    """``(mine, opp)`` in the mover's own numbering, as ``legality`` wants."""
    mb = list(board_p1) if mover_is_white else _flip_board(board_p1)
    mine = [0] * 26
    opp = [0] * 26
    for p in range(1, 25):
        if mb[p] > 0:
            mine[p] = mb[p]
        elif mb[p] < 0:
            opp[p] = -mb[p]
    mine[25] = max(0, mb[25])
    return mine, opp


def _play_is_legal(board_p1, mover_is_white, action, moves) -> bool:
    n = DICE_TABLE[action][2]
    if len(moves) > n or any(_step_byte(m) is None for m in moves):
        return False
    d1, d2 = DICE_TABLE[action][:2]
    mine, opp = _mover_frame(board_p1, mover_is_white)
    pairs = []
    for m in moves:
        frm = int(m["from"])
        src = 25 - frm if mover_is_white else frm
        dest = src - int(m["pips"])
        pairs.append((src, dest if dest > 0 else 0))
    return is_play_legal(mine, opp, d1, d2, pairs)


def _step_ok(mb: list[int], src: int, pips: int) -> bool:
    """Can the mover (own checkers positive, own bar 25) move one checker from
    ``src`` by ``pips`` here?"""
    if not 1 <= src <= 25 or mb[src] <= 0:
        return False
    if mb[25] > 0 and src != 25:
        return False                              # the bar enters first
    dest = src - pips
    if dest >= 1:
        return mb[dest] >= -1                     # not onto a made point
    if any(mb[p] > 0 for p in range(7, 26)):
        return False                              # bear off only from home
    return dest == 0 or not any(mb[p] > 0 for p in range(src + 1, 7))


def _legal_order(board_p1: list[int], mover_is_white: bool, moves: list[dict]) -> list[dict]:
    """``moves`` in an order whose every intermediate position is legal (M3),
    keeping the stored order when it already is. The play itself is unchanged
    (M5 compares positions), so only the spelling moves."""
    from itertools import permutations
    mb0 = list(board_p1) if mover_is_white else _flip_board(board_p1)

    def replays(order) -> bool:
        mb = list(mb0)
        for m in order:
            src = 25 - int(m["from"]) if mover_is_white else int(m["from"])
            pips = int(m["pips"])
            if not _step_ok(mb, src, pips):
                return False
            mb[src] -= 1
            dest = src - pips
            if dest >= 1:
                if mb[dest] == -1:
                    mb[dest] = 0
                    mb[0] += 1
                mb[dest] += 1
        return True

    if replays(moves):
        return moves
    for order in permutations(moves):
        if replays(order):
            return list(order)
    return moves


def _resign_value(points_won: int, cube: int) -> int:
    if cube > 0 and points_won % cube == 0 and 1 <= points_won // cube <= 3:
        return points_won // cube
    return 1


class _Match:
    """The match half of the file, and what the analysis half needs from it."""

    def __init__(self, doc: dict, flagged_illegal: set):
        self.doc = doc
        self.match_length = int(doc.get("match_length") or 0)
        self.games: list[bytes] = []
        self.ply_at: list[tuple[tuple[int, int], dict]] = []   # ply_ref order
        self.ref_of: dict[tuple[int, int], int] = {}
        self.illegal: set[int] = set()          # ply_refs written as set position + illegal
        self.position_before: dict[int, tuple[list[int], bool]] = {}   # ref -> (board_p1, mover is White)
        self.annos: list[tuple[int, int, str, str]] = []
        self.pending_double_end = False
        #: Per game, how many leading document plies v2 states as the game's
        #: initial board instead (0 or 1).
        self.game_start: list[int] = []
        self.mtch_mandatory = b""
        self.mtch_fields: dict[int, bytes] = {}
        self._encode_games(flagged_illegal)

    def _encode_games(self, flagged_illegal: set) -> None:
        opening_abs = _p1_to_absolute(_STARTING_BOARD_P1)
        for gi, g in enumerate(self.doc.get("games") or []):
            plies = g.get("plies") or []
            board = list(_STARTING_BOARD_P1)
            fields: dict[int, bytes] = {}
            start = 0
            first = plies[0] if plies else None
            if (first is not None and first.get("action_id") == R.ACTION_SET_POSITION
                    and not first.get("d1")
                    and [int(v) for v in first.get("set_position") or [0] * 26] != opening_abs):
                # A leading dice-less set position is a game that starts from a
                # set-up board: v2 states that as the game's initial board. One
                # stating the opening position itself stays a ply, since an
                # initial board equal to the default is never written (3.2).
                start = 1
                board_abs = [int(v) for v in first["set_position"]]
                board = _absolute_to_p1(board_abs)
                fields[3] = _board(board_abs)

            self.game_start.append(start)
            winner = g.get("winner")
            if winner in (0, 1):
                fields[0] = _varint(winner)
                fields[1] = _varint(max(0, int(g.get("points_won") or 0)))
            if g.get("is_lastgame"):
                fields[2] = b""

            out = bytearray(_record(b"", fields))
            cube = 1
            double_pending = False
            for pi in range(start, len(plies)):
                p = plies[pi]
                key = (gi, pi)
                ref = len(self.ply_at)
                self.ref_of[key] = ref
                self.ply_at.append((key, p))
                raw = p.get("action_id")
                action = int(raw) if raw is not None else 30
                color = 1 if p.get("color") else 0

                if 0 <= action <= 20 or (action == R.ACTION_SET_POSITION and p.get("d1")):
                    self.position_before[ref] = (list(board), color == 1)
                if 0 <= action <= 20:
                    moves = p.get("moves") or []
                    nxt = plies[pi + 1].get("action_id") if pi + 1 < len(plies) else None
                    unplayed = not moves and nxt in (*_RESIGNS, _FORFEIT)
                    if not unplayed and (key in flagged_illegal or not _play_is_legal(
                            board, color == 1, action, moves)):
                        after = _apply_moves_p1(board, moves, color == 1)
                        d1, d2 = DICE_TABLE[action][:2]
                        out += self._set_position(1 - color, (d1, d2), _p1_to_absolute(after))
                        self.illegal.add(ref)
                        self.annos.append((SCOPE_PLY, ref, R.GV_KEY_ILLEGAL_PLY, R.GV_FORMAT + ",".join(
                            f"{int(m['from'])}/{int(m['pips'])}" for m in moves)))
                        board = after
                    else:
                        out.append(action | (_seat(color) << 6))
                        steps = [_step_byte(m) for m in _legal_order(board, color == 1, moves)]
                        out += bytes(steps)
                        if len(steps) < DICE_TABLE[action][2]:
                            out.append(0)
                        board = _apply_moves_p1(board, moves, color == 1)
                elif action == R.ACTION_SET_POSITION:
                    board_abs = [int(v) for v in p.get("set_position") or [0] * 26]
                    if p.get("d1") and p.get("d2"):
                        # An illegal play restated as its board: the side on
                        # roll is the mover's opponent (M2).
                        out += self._set_position(1 - color, (p["d1"], p["d2"]), board_abs)
                        self.illegal.add(ref)
                    else:
                        out += self._set_position(color, None, board_abs)
                    board = _absolute_to_p1(board_abs)
                elif action in _RESIGNS:
                    out.append(action | (_seat(color) << 6) | 0x80)
                    out += _record(b"", {1: _varint(_resign_value(int(g.get("points_won") or 0), cube))})
                    self.pending_double_end |= double_pending
                elif action in _MARKERS:
                    out.append(action)
                elif action in (R.ACTION_DOUBLE, R.ACTION_TAKE, R.ACTION_DROP, _FORFEIT):
                    out.append(action | (_seat(color) << 6))
                    if action == R.ACTION_DOUBLE:
                        double_pending = True
                    elif action == R.ACTION_TAKE:
                        double_pending = False
                        cube *= 2
                    elif action == R.ACTION_DROP:
                        double_pending = False
                    else:
                        self.pending_double_end |= double_pending
                else:
                    raise ValueError(f"game {gi} ply {pi}: action {action} has no OGXM v2 form here")
            self.games.append(bytes(out))

    @staticmethod
    def _set_position(color: int, dice, board_abs: list[int]) -> bytes:
        fields: dict[int, bytes] = {6: _board(board_abs)}
        if dice is not None:
            fields[0] = bytes([int(dice[0]), int(dice[1])])
            fields[3] = b""
        return bytes([R.ACTION_SET_POSITION | (_seat(color) << 6) | 0x80]) + _record(b"", fields)

    def mtch(self) -> tuple[bytes, bool]:
        """The ``MTCH`` payload, and whether it uses a 2.1 field (4 bits 12-25)."""
        doc = self.doc
        length = self.match_length
        fields: dict[int, bytes] = {}
        if doc.get("player_white"):
            fields[0] = _str(doc["player_white"])
        if doc.get("player_black"):
            fields[1] = _str(doc["player_black"])
        rules = ((1 if doc.get("crawford") else 0) | (2 if doc.get("jacoby") else 0)
                 | (4 if doc.get("beaver") else 0) | (8 if doc.get("raccoon") else 0))
        if rules:
            fields[2] = _varint(rules)
        cube_limit = int(doc.get("cube_limit") or 0)
        if cube_limit > 0 and cube_limit & (cube_limit - 1) == 0:
            fields[3] = _varint(cube_limit)        # anything else is a source's "no limit"
        # The score is derived (M8) and the result follows from it; a result is
        # stored only where the score leaves it open.
        _starts, (w, b) = R._score_walk({"match_length": length}, [
            {k: v for k, v in (("winner", g.get("winner")), ("points_won", g.get("points_won")))
             if v is not None and (k != "winner" or v in (0, 1))}
            for g in doc.get("games") or []])
        derived = 1 if length > 0 and w >= length else 2 if length > 0 and b >= length else 0
        # A score the games do not add up to (a source's header that disagrees
        # with its own games) cannot be stored: score_final is verified (11).
        stated = (int(doc.get("white_score") or 0), int(doc.get("black_score") or 0))
        if length > 0:
            stated = tuple(min(s, length) for s in stated)
        if stated != (w, b):
            self.annos.append((SCOPE_MATCH, 0, R.GV_KEY_SCORE, f"{R.GV_FORMAT}{stated[0]},{stated[1]}"))
        result = int(doc.get("result") or 0)
        if result and result != derived:
            fields[6] = _varint(result)
        if doc.get("source"):
            fields[7] = _varint(int(doc["source"]))
        if doc.get("timestamp"):
            fields[8] = _varint(int(doc["timestamp"]) * 1000)
        event = doc.get("event") or ""
        if event:
            if len(event.encode("utf-8")) <= MAX_EVENT:
                fields[12] = _str(event)
            else:
                self.annos.append((SCOPE_MATCH, 0, R.GV_KEY_EVENT, R.GV_FORMAT + event))
        if doc.get("site"):
            self.annos.append((SCOPE_MATCH, 0, R.GV_KEY_SITE, R.GV_FORMAT + doc["site"]))
        self.mtch_mandatory = _varint(length) + _varint(0)
        self.mtch_fields = fields
        payload = _record(self.mtch_mandatory, fields)
        return payload, 12 in fields


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _blocks(doc: dict) -> list[tuple[dict, object]]:
    """``[(analysis_info, select), ...]``, primary first, as ``binary`` resolves
    them."""
    infos = doc.get("analyses_info")
    if isinstance(infos, list) and infos:
        out = []
        for k, info in enumerate(infos):
            def select(ply, k=k):
                for a in ply.get("analyses") or []:
                    if a.get("analysis_index") == k:
                        return a
                return None
            out.append((info or {}, select))
        return out
    has_any = any(isinstance(p.get("analysis"), dict)
                  for g in doc.get("games") or [] for p in g.get("plies") or [])
    info = doc.get("analysis_info")
    if isinstance(info, dict) or has_any:
        return [(info if isinstance(info, dict) else {}, lambda ply: ply.get("analysis"))]
    return []


def _analysis_id(index: int, info: dict, match_bytes: bytes) -> bytes:
    given = info.get("analysis_id")
    if given:
        return uuid.UUID(str(given)).bytes
    # Deterministic, so the same document always writes the same bytes: four
    # CRC32s over the match and the block's identity, stamped as a UUIDv8.
    def part(v) -> str:
        # Spelled out rather than str(), so the JavaScript mirror derives the
        # same id: strings as they are, numbers as integers, nothing as empty.
        return "" if v is None else v if isinstance(v, str) else str(int(v))

    seed = (b"gammonview-analysis\0" + struct.pack("<I", index)
            + "\0".join(part(info.get(k)) for k in
                        ("model_id", "timestamp", "duration_ms", "eval_level", "ply")).encode("utf-8")
            + b"\0" + match_bytes)
    raw = bytearray(b"".join(struct.pack("<I", zlib.crc32(seed + bytes([i])) & 0xFFFFFFFF)
                             for i in range(4)))
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return bytes(raw)


def _fmt8(x: float) -> str:
    """Eight places, trailing zeros dropped (``0.5``, ``1``): the form a
    ``frame=`` number takes, spelled so the JavaScript mirror matches."""
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def _source_frame(ply: dict, analysis: dict):
    """``(mid_white, half)`` as ``frame=`` spells them for this ply's analysis,
    or None when it states no usable source frame."""
    f = analysis.get("mwc_frame")
    if not isinstance(f, (list, tuple)) or len(f) != 2 or frame_key(ply) is None:
        return None
    try:
        mid, half = float(f[0]), float(f[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(mid) and math.isfinite(half) and half > 0):
        return None
    mid, half = round(mid, 8), round(half, 8)
    return _fmt8(mid if frame_perspective_is_white(ply) else 1 - mid), _fmt8(half)


class _Converter:
    """Normalized equity -> the block's currency, in one ply's frame."""

    def __init__(self, ply: dict, mwc: bool, source=None):
        if mwc:
            inv = mwc_frame_inverse(ply, None if source is None else R.frame_from_wire(
                *source, frame_perspective_is_white(ply)))
            if inv is None:
                raise ValueError("a match-play decision with no score frame cannot be written as MWC")
            self.eq, self.delta = inv
        else:
            self.eq = self.delta = float


def _alt_record(alt: dict, lv_override: dict, played: bool, conv: _Converter) -> bytes:
    steps = [_step_byte(m) for m in alt.get("move") or []]
    if any(s is None for s in steps):
        steps = []          # a hop no step byte holds: draw nothing rather than something wrong
    fields: dict[int, bytes] = {}
    if "eval" in alt:
        fields[0] = _probs(alt["eval"])
    if lv_override:
        fields[1] = _level_bytes(lv_override)
    if played:
        fields[2] = b""
    return _record(_varint(len(steps)) + bytes(steps) + _equity(conv.eq(alt.get("equity", 0.0))), fields)


def _position_key(position, moves) -> tuple | None:
    """The position ``moves`` produce, for comparing two plays (M5)."""
    if position is None:
        return None
    board, mover_is_white = position
    try:
        return tuple(_apply_moves_p1(board, moves or [], mover_is_white))
    except Exception:
        return None


def _checker_records(a: dict, ref: int, block_level: dict, conv: _Converter, unplayed: bool,
                     position=None, played_moves=None):
    """``(main, exact)``: the record ``DECS`` can carry (or None), and the exact
    one for the annotation when that differs (or None). ``position`` is the
    board before a legal dice ply and ``played_moves`` its play, for A4."""
    alts = (a.get("alternatives") or [])[:MAX_ALTS]
    best = float(a.get("best_equity", 0.0) or 0.0)
    loss = float(a.get("equity_loss", 0.0) or 0.0)
    if not alts and not best and not loss:
        return None, None

    want = {"preset": alts[0].get("eval_level") if alts else None,
            "checker_ply": a.get("ply") or None}
    dec_override = _override(block_level, want)
    dec_level = {**block_level, **dec_override}
    labels = [alt.get("eval_level") or dec_level.get("preset") for alt in alts]
    played = [i for i, alt in enumerate(alts) if alt.get("is_played")]

    def build(order, explicit: bool) -> bytes:
        fields: dict[int, bytes] = {}
        if order:
            fields[0] = _varint(len(order)) + b"".join(
                _alt_record(alts[i], _override(dec_level, {"preset": alts[i].get("eval_level")}),
                            i in played, conv) for i in order)
        if explicit or not order:
            fields[2] = _equity(conv.eq(best))
        if explicit or not played:
            fields[3] = _loss(conv.delta(loss))
        if dec_override:
            fields[4] = _level_bytes(dec_override)
        return _record(_varint(ref) + _varint(R.KIND_CHECKER), fields)

    exact = build(list(range(len(alts))), explicit=True)
    if unplayed or len(played) > 1:
        return None, exact
    if alts and abs(best - float(alts[0].get("equity", 0.0))) > 1e-9:
        return None, exact
    if played:
        p = played[0]
        if labels[p] != labels[0]:
            return None, exact                                     # A5
        if abs((best - float(alts[p].get("equity", 0.0))) - loss) > _LOSS_TOLERANCE:
            return None, exact

    # A4: the played move, wherever it appears in the list, is the flagged
    # alternative. Another copy of it (a notation split two ways) is dropped
    # from the record v2 holds; the exact list stays in the annotation.
    keep = list(range(len(alts)))
    played_key = _position_key(position, played_moves) if position is not None else None
    if played_key is not None:
        same = {i for i in keep if _position_key(position, alts[i].get("move")) == played_key}
        if played and played[0] not in same:
            return None, exact
        keep = [i for i in keep if i not in same or (played and i == played[0])]

    # A1: alternatives of one level adjacent, equity non-increasing within each.
    order: list[int] = []
    groups: dict = {}
    for i in keep:
        groups.setdefault(labels[i], []).append(i)
    for idxs in groups.values():
        order.extend(idxs)
    enc = [_round_c(conv.eq(float(alts[i].get("equity", 0.0))) * 1e6) for i in range(len(alts))]
    for idxs in groups.values():
        if any(enc[idxs[j + 1]] > enc[idxs[j]] for j in range(len(idxs) - 1)):
            return None, exact
    if order != list(range(len(alts))):
        if not order or order[0] != 0:
            return None, exact
        return build(order, explicit=False), exact
    return build(order, explicit=False), None


def _cube_record(sub: dict, ref: int, verdict: int, loss, block_level: dict,
                 want: dict, conv: _Converter) -> bytes:
    fields: dict[int, bytes] = {
        0: _equity(conv.eq(sub.get("no_double_equity", 0.0))),
        1: _equity(conv.eq(sub.get("double_take_equity", 0.0))),
        2: _equity(conv.eq(sub.get("double_pass_equity", 0.0))),
    }
    if "eval" in sub:
        fields[3] = _probs(sub["eval"])
    if loss is not None:
        fields[4] = _loss(conv.delta(loss))
    lv = _override(block_level, want)
    if lv:
        fields[7] = _level_bytes(lv)
    return _record(_varint(ref) + _varint(R.KIND_CUBE) + _varint(verdict), fields)


_PLY_LABEL = re.compile(r"(\d+)ply")


def _luck_override(block_level: dict, label: str | None) -> dict:
    """The level a luck record states. The label itself travels once per block
    (``x-gammonview-analysis``); each record states the depth, which is what a
    reader outside GammonView can use, and costs a byte where a label costs
    five."""
    m = _PLY_LABEL.fullmatch(label or "")
    if m:
        return _override(block_level, {"checker_ply": int(m.group(1))})
    return _override(block_level, {"preset": label})


def _common_label(match: "_Match", select) -> str | None:
    """The level most of a block's decisions were judged at -- what v2 means by
    a block's level (6.4), and what lets most of them state none of their own."""
    counts: dict = {}
    for _key, p in match.ply_at:
        a = select(p)
        if not isinstance(a, dict):
            continue
        alts = a.get("alternatives") or []
        labels = [alts[0].get("eval_level") if alts else None, a.get("eval_level")]
        labels += [sub.get("eval_level") for sub in (a.get("cube_decision"), a.get("missed_double"))
                   if isinstance(sub, dict)]
        for lbl in labels:
            if lbl:
                counts[lbl] = counts.get(lbl, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _block_records(match: _Match, select, block_level: dict, luck_label: str | None):
    """``(decs, extra, frames)``: the records ``DECS`` holds, sorted, the
    per-ply records only the annotation can hold, and the ``frame=`` entries
    for plies whose source frame is not ours."""
    mwc = match.match_length > 0
    decs: list[tuple[int, int, bytes]] = []
    extra: dict[int, list[tuple[int, bytes]]] = {}
    frames: list[str] = []
    in_force = None
    plies = match.ply_at
    for idx, (key, p) in enumerate(plies):
        a = select(p)
        if not isinstance(a, dict):
            continue
        ref = match.ref_of[key]
        action = int(p.get("action_id") if p.get("action_id") is not None else 30)
        source = _source_frame(p, a) if mwc else None
        if source != in_force:
            frames.append(f"{ref}:" if source is None else f"{ref}:{source[0]}:{source[1]}")
            in_force = source
        conv = _Converter(p, mwc, source)
        illegal = ref in match.illegal
        recs: list[tuple[int, bytes, bool]] = []      # (kind, bytes, main-capable)

        if 0 <= action <= 20 or illegal:
            nxt = plies[idx + 1][1].get("action_id") if idx + 1 < len(plies) else None
            unplayed = (0 <= action <= 20 and not (p.get("moves") or [])
                        and nxt in (*_RESIGNS, _FORFEIT) and plies[idx + 1][0][0] == key[0])
            legal_dice = 0 <= action <= 20 and not illegal
            main, exact = _checker_records(
                a, ref, block_level, conv, unplayed,
                match.position_before.get(ref) if legal_dice else None,
                p.get("moves") or [])
            if main is not None:
                recs.append((R.KIND_CHECKER, main, True))
            if exact is not None:
                extra.setdefault(ref, []).append((R.KIND_CHECKER, exact))
            md, cd = a.get("missed_double"), a.get("cube_decision")
            if isinstance(md, dict):
                verdict = _VERDICT.get(md.get("correct_action") or "double", 1)
                recs.append((R.KIND_CUBE, _cube_record(
                    md, ref, verdict, float(md.get("equity_loss", 0.0) or 0.0), block_level,
                    {"preset": md.get("eval_level")}, conv), verdict in _OFFER_VERDICTS))
            elif isinstance(cd, dict):
                recs.append((R.KIND_CUBE, _cube_record(
                    cd, ref, 1 if cd.get("should_double") else 0, None, block_level,
                    {"preset": cd.get("eval_level")}, conv), True))
            if "luck" in a:
                lv = _luck_override(block_level, luck_label)
                recs.append((R.KIND_ROLL, _record(
                    _varint(ref) + _varint(R.KIND_ROLL) + _equity(conv.delta(a["luck"])),
                    {0: _level_bytes(lv)} if lv else {}), True))
        elif action in (R.ACTION_DOUBLE, R.ACTION_TAKE, R.ACTION_DROP):
            verdict = _VERDICT.get(a.get("correct_action") or "no_double", 0)
            allowed = _OFFER_VERDICTS if action == R.ACTION_DOUBLE else _RESPONSE_VERDICTS
            loss = float(a.get("equity_loss", 0.0) or 0.0)
            recs.append((R.KIND_CUBE, _cube_record(
                a, ref, verdict, loss if loss else None, block_level,
                {"preset": a.get("eval_level"), "cube_ply": a.get("ply") or None}, conv),
                verdict in allowed))
        elif action in _RESIGNS:
            fields: dict[int, bytes] = {
                1: _equity(conv.delta(a.get("resign_error", 0.0) or 0.0)),
                2: _equity(conv.delta(a.get("take_resign_error", 0.0) or 0.0)),
                4: _loss(conv.delta(a.get("equity_loss", 0.0) or 0.0)),
            }
            if "eval" in a:
                fields[3] = _probs(a["eval"])
            recs.append((R.KIND_RESIGN, _record(_varint(ref) + _varint(R.KIND_RESIGN), fields), True))

        for kind, rec, ok in recs:
            if illegal or not ok:
                extra.setdefault(ref, []).append((kind, rec))
            else:
                decs.append((ref, kind, rec))
    decs.sort(key=lambda t: (t[0], t[1]))
    for ref in extra:
        extra[ref].sort(key=lambda t: t[0])
    return decs, extra, frames


def _flag_exceptions(doc_obj_by_key: dict, decoded_by_key: dict) -> list[str]:
    """Tokens for every flag where the document disagrees with the reader's
    derivation (``ogxm2.default_flags`` already applied to ``decoded``)."""
    tokens = []
    for (ref, key), want in sorted(doc_obj_by_key.items()):
        got = decoded_by_key.get(key) or {}
        if bool(want.get("decision")) != bool(got.get("decision")):
            tokens.append(f"{ref}c")
        wl, gl = want.get("cube_decision"), got.get("cube_decision")
        if isinstance(wl, dict) and "decision" in wl and isinstance(gl, dict):
            if bool(wl["decision"]) != bool(gl.get("decision")):
                tokens.append(f"{ref}l")
        if bool(want.get("illegal_move")) != bool(got.get("illegal_move")):
            tokens.append(f"{ref}i")
    return tokens


def _chunked(key: str, value: str) -> list[tuple[str, str]]:
    """Split an ASCII value over ``key``, ``key~1``, ... to stay under
    ``MAX_STRING``; the reader concatenates them in suffix order."""
    room = MAX_STRING - 16
    if len(value) <= room:
        return [(key, value)]
    parts = [value[i:i + room] for i in range(0, len(value), room)]
    return [(key if i == 0 else f"{key}~{i}", part) for i, part in enumerate(parts)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _anno_record(scope: int, ref: int, key: str, value: str) -> bytes:
    return _record(_varint(scope) + _varint(ref) + _str(value), {0: _str(key)})


class _Block:
    """One analysis block as the document canonically encodes it."""

    def __init__(self, aid: bytes, anal: bytes, decs: bytes, annos: list):
        self.aid = aid
        self.aid_str = _uuid_str(aid)
        self.anal = anal
        self.decs = decs
        self.annos = annos        # (scope, ref, key, value) this block adds to ANNO

    def anno_bytes(self) -> bytes:
        return b"".join(_anno_record(*a) for a in sorted(
            self.annos, key=lambda t: (t[0], t[1], t[2].encode("utf-8"))))


class _Parts:
    """The document, encoded part by part: what ``ogxm2_passthrough``
    fingerprints and what ``_assemble`` puts in a file."""

    def __init__(self, doc: dict, match: _Match, mtch: bytes, uses_21: bool, blocks: list):
        self.doc = doc
        self.match = match
        self.mtch = mtch
        self.uses_21 = uses_21
        self.blocks = blocks


def _encode(ogxm: dict) -> _Parts:
    doc = ogxm
    if any(not p.get("ogid_before") for g in doc.get("games") or [] for p in g.get("plies") or []
           if p.get("action_id") is not None and p.get("action_id") not in _MARKERS):
        doc = copy.deepcopy(ogxm)
        _derive_ogids(doc)

    blocks = _blocks(doc)
    flagged = {(gi, pi) for gi, g in enumerate(doc.get("games") or [])
               for pi, p in enumerate(g.get("plies") or [])
               for _info, select in blocks
               if isinstance(select(p), dict) and select(p).get("illegal_move")}
    match = _Match(doc, flagged)
    mtch, uses_21 = match.mtch()
    match_bytes = mtch + b"".join(match.games)

    out: list[_Block] = []
    used_ids: set[bytes] = set()
    for k, (info, select) in enumerate(blocks):
        aid = _analysis_id(k, info, match_bytes)
        if aid in used_ids:
            raise ValueError(f"analysis block {k} repeats an analysis_id")
        used_ids.add(aid)
        aid_str = _uuid_str(aid)
        block_level = {}
        common = _common_label(match, select) or info.get("eval_level")
        if common:
            block_level["preset"] = common
        if info.get("ply"):
            block_level["checker_ply"] = int(info["ply"])
        luck_label = info.get("luck_eval_level") or "1ply"
        decs, extra, frames = _block_records(match, select, block_level, luck_label)

        anal_fields: dict[int, bytes] = {}
        if block_level:
            anal_fields[2] = _level_bytes(block_level)
        if info.get("model_id"):
            anal_fields[5] = _str(info["model_id"])
        anal_fields[9] = _varint(R.CURRENCY_CUBEFUL_MATCH if match.match_length > 0
                                 else R.CURRENCY_CUBEFUL_MONEY)
        met_id = info.get("met_id") or ("kazaross-xg2" if match.match_length > 0 else None)
        if met_id:
            anal_fields[11] = _str(met_id)
        if info.get("timestamp"):
            anal_fields[14] = _varint(int(info["timestamp"]) * 1000)
        if info.get("duration_ms"):
            anal_fields[16] = _varint(int(info["duration_ms"]))
        anal = _record(aid, anal_fields)
        decs_payload = b"".join(rec for _r, _k, rec in decs)

        # The flags v2 has no field for: derive them the way the reader will,
        # from the records just written, and keep only where the document
        # disagrees.
        anal_dec = R._decode_anal(anal)
        extra_dec = {ref: R._decode_decs(b"".join(rec for _k, rec in recs))
                     for ref, recs in extra.items()}
        _info, decoded, _luck = R._v1_block(
            anal_dec, R._decode_decs(decs_payload), match.ply_at, match.match_length,
            ours={"extra": extra_dec, "exceptions": set(), "illegal": match.illegal,
                  "frames": R._parse_frames(",".join(frames))})
        want = {(match.ref_of[key], key): select(p) for key, p in match.ply_at
                if isinstance(select(p), dict)}
        tokens = _flag_exceptions(want, decoded)

        items = []
        if info.get("eval_level") != block_level.get("preset"):
            items.append("level=" + quote(info.get("eval_level") or "", safe=_SAFE))
        if any(k == R.KIND_ROLL for _r, k, _b in decs) or any(
                k == R.KIND_ROLL for recs in extra.values() for k, _b in recs):
            items.append("luck=" + quote(luck_label, safe=_SAFE))
        items.append("pr=" + ",".join(tokens))
        if frames:
            items.append("frame=" + ",".join(frames))
        annos: list[tuple[int, int, str, str]] = []
        for key_, value in _chunked(R.GV_KEY_ANALYSIS + aid_str, R.GV_FORMAT + ";".join(items)):
            annos.append((SCOPE_MATCH, 0, key_, value))
        for ref, recs in extra.items():
            value = R.GV_FORMAT + base64.b64encode(b"".join(rec for _k, rec in recs)).decode("ascii")
            for key_, part in _chunked(R.GV_KEY_DECISIONS + aid_str, value):
                annos.append((SCOPE_PLY, ref, key_, part))
        out.append(_Block(aid, anal, decs_payload, annos))
    return _Parts(doc, match, mtch, uses_21, out)


def _assemble(parts: _Parts, plan: P.Plan) -> bytes:
    """The file: the plan's parts in the order 2.4 requires, and ``CSUM``."""
    match = parts.match
    secs: list[tuple[str, bytes]] = []        # (anchor key, section)
    secs.append(("MTCH", _section(b"MTCH", plan.mtch, True)))
    for i, g in enumerate(plan.games):
        secs.append((f"GAME:{i}", _section(b"GAME", g, True)))
    for b, (anal, decs, sign, _annos) in zip(parts.blocks, plan.blocks):
        key = f"BLOCK:{b.aid_str}"
        secs.append((key, _section(b"ANAL", anal, False)))
        secs.append((key, _section(b"DECS", decs, False)))
        if sign is not None:
            secs.append((key, _section(b"SIGN", sign, False)))
    if plan.clck is not None:
        secs.append(("CLCK", _section(b"CLCK", plan.clck, False)))
    if plan.vido is not None:
        secs.append(("VIDO", _section(b"VIDO", plan.vido, False)))

    entries: list[tuple[tuple, bytes]] = []
    for r in plan.foreign_annos:
        entries.append((P.anno_sort_key(r["scope"], r["ref"], r["analysis"], r["kind"], r["alt"],
                                        r["key"], r["lang"], len(entries)), P.b64d(r["raw"])))
    ours = list(match.annos) + [a for _an, _d, _s, annos in plan.blocks for a in annos]
    for scope, ref, key, value in ours:
        if plan.skip_site and key == R.GV_KEY_SITE and scope == SCOPE_MATCH:
            continue
        entries.append((P.anno_sort_key(scope, ref, None, None, None, key, None, len(entries)),
                        _anno_record(scope, ref, key, value)))
    if entries:
        entries.sort(key=lambda e: e[0])
        secs.append(("ANNO", _section(b"ANNO", b"".join(rec for _k, rec in entries), False)))
    n_other = len(secs)
    for i, m in enumerate(plan.msig):
        secs.append((f"MSIG:{i}", _section(b"MSIG", m, False)))

    # Unknown sections go back after the section they followed; where that one
    # is gone, at the end of the body (before the signatures).
    if plan.unknown:
        keys = [k for k, _s in secs]
        n_games = len(plan.games)
        inserts: dict[int, list[bytes]] = {}
        for stype, payload, after in plan.unknown:
            k = after["k"]
            want = ("head" if k == "head" else f"{k}:{after['i']}" if k in ("GAME", "MSIG")
                    else f"BLOCK:{after['id']}" if k == "BLOCK" else k)
            if k == "GAME" and after["i"] >= n_games:
                want = f"GAME:{n_games - 1}" if n_games else "MTCH"
            if want == "head":
                at = 0
            elif want in keys:
                at = len(keys) - keys[::-1].index(want)
            else:
                at = n_other
            inserts.setdefault(at, []).append(_section(stype, payload, False))
        merged: list[bytes] = []
        for i in range(len(secs) + 1):
            merged += inserts.get(i, [])
            if i < len(secs):
                merged.append(secs[i][1])
        body_sections = merged
    else:
        body_sections = [s for _k, s in secs]

    minor = max(2 if match.pending_double_end else (1 if parts.uses_21 else 0), plan.minor_floor)
    min_minor = max(2 if match.pending_double_end else 0, plan.min_minor_floor)
    body = bytearray(b"OGXM" + struct.pack("<HHHHI", 2, minor, 2, min_minor, 0))
    for s in body_sections:
        body += s
    csum_at = len(body)
    csum_payload_len = len(_record(_varint(0) + _varint(4) + b"\0\0\0\0", {}))
    total = csum_at + 9 + csum_payload_len + 4
    struct.pack_into("<I", body, 12, total)
    crc = zlib.crc32(bytes(body)) & 0xFFFFFFFF
    body += _section(b"CSUM", _record(_varint(0) + _varint(4) + struct.pack("<I", crc), {}), True)
    body += b"END!"
    assert len(body) == total
    return bytes(body)


def write_ogxm2(ogxm: dict) -> bytes:
    """Serialize our document to OGXM v2 bytes. Pure stdlib, no engine.

    A document read from another producer's v2 file carries
    ``_ogxm2_passthrough``, and what it did not edit is written back as it
    came (``ogxm2_passthrough``)."""
    parts = _encode(ogxm)
    return _assemble(parts, P.Plan(parts, parts.doc))
