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
    A ``site`` that is neither the city nor the platform (v2's own ``site`` is a
    host name), and an over-long ``event``.
``x-gammonview-<field>`` (match scope), ``x-gammonview-<side>_profile.<field>``, and
``x-gammonview-<field>`` at game scope
    A value v2 cannot hold (a string past its cap or outside its byte set, a
    year without an event, a rating that is not a hundredth ...): the reader puts
    it back, so nothing a document says is lost.

``x-gammonview-annotations`` (match scope), ``x-gammonview-video.url``
    The document's annotations ``ANNO`` cannot address or hold (a JSON list in the
    document's own coordinates), and a video URL v2 would drop.

Every value begins with a format version, ``1:``.

Units. A match block is written in v2's ``cubeful match`` currency -- MWC --
converted through each ply's own score frame (``basefill.mwc_frame_inverse``),
which is how the reader converts it back. A money block is cubeful money,
written as is.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import re
import struct
import uuid
import zlib
from datetime import datetime, timezone
from urllib.parse import quote

from .basefill import frame_key, frame_perspective_is_white, mwc_frame_inverse
from .binary import DICE_TABLE, _round_c
from .export import (
    _STARTING_BOARD_P1, _flip_board, _p1_to_absolute, variant_opening_abs, variant_opening_p1,
)
from .legality import is_play_legal
from .reader import _absolute_to_p1, _apply_moves_p1, _derive_ogids
from . import ogxm2 as R
from . import ogxm2_passthrough as P

MAX_STRING = 4096
MAX_ALTS = 1024            # MAX_ALTS_PER_DECISION
MAX_GAMES = 1000
MAX_PLIES_PER_GAME = 1500
MAX_TOTAL_PLIES = 100000
MAX_ANALYSES = 64
MAX_EVENT = 120

SCOPE_MATCH = 0
SCOPE_GAME = 1
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

_ROLLOUT_UINTS = ("trials", "truncation_depth", "move_ply", "variance_reduction", "budget_ms",
                  "match_policy")


def _level_bytes(lv: dict) -> bytes:
    """A ``level`` record from an override (``_level_diff``'s shape)."""
    fields: dict[int, bytes] = {}
    if "preset" in lv:
        fields[0] = _str(lv["preset"])
    if "checker_ply" in lv:
        fields[1] = _varint(lv["checker_ply"])
    if "cube_ply" in lv:
        fields[2] = _varint(lv["cube_ply"])
    if "rollout" in lv:
        r = lv["rollout"]
        rf: dict[int, bytes] = {}
        for bit, key in enumerate(("trials", "truncation_depth", "move_ply",
                                   "variance_reduction", "seed", "budget_ms", "match_policy")):
            if key == "seed" and key in r:
                rf[bit] = struct.pack("<Q", int(r[key]))
            elif key in r:
                rf[bit] = _varint(r[key])
        fields[3] = _record(b"", rf)
    if lv.get("no_rollout"):
        fields[4] = b""
    return _record(b"", fields)


def _check_level(lv) -> dict:
    """A level the document states, checked against what v2 can hold and put in
    the form the rest of this module compares: no empty strings, no absent
    fields, a seed as a decimal string. A level v2 cannot hold is an error, not
    a quiet change (P6)."""
    if not isinstance(lv, dict):
        raise ValueError(f"a level must be an object, not {lv!r}")
    out: dict = {}
    preset = lv.get("preset")
    if preset is not None and preset != "":
        if not isinstance(preset, str) or _nbytes(preset) > MAX_STRING:
            raise ValueError(f"a level's preset {preset!r} is not a string v2 can hold")
        out["preset"] = preset
    for key in ("checker_ply", "cube_ply"):
        v = lv.get(key)
        if v is not None:
            if not _is_uint(v):
                raise ValueError(f"a level's {key} {v!r} is not a count")
            out[key] = v
    r = lv.get("rollout")
    if r:
        if not isinstance(r, dict):
            raise ValueError(f"a level's rollout must be an object, not {r!r}")
        ro: dict = {}
        for key in _ROLLOUT_UINTS:
            v = r.get(key)
            if v is not None:
                if not _is_uint(v) or (key == "trials" and v < 1):
                    raise ValueError(f"a rollout's {key} {v!r} is not a count v2 can hold")
                ro[key] = v
        seed = r.get("seed")
        if seed is not None:
            ok = (isinstance(seed, int) and not isinstance(seed, bool)) or (
                isinstance(seed, str) and seed.isascii() and seed.isdigit())
            if not ok or not 0 <= int(seed) < 1 << 64:
                raise ValueError(f"a rollout's seed {seed!r} is not a 64-bit count")
            ro["seed"] = str(int(seed))
        if ro:
            out["rollout"] = ro
    return out


def _level_diff(parent: dict, want: dict) -> dict:
    """What a tier must state to have the level ``want`` under ``parent`` (L1,
    L2): the fields that differ, a rollout's only as far as it differs, and
    ``no_rollout`` where the tier above has one and this has none."""
    out: dict = {}
    for key in ("preset", "checker_ply", "cube_ply"):
        if key in want and want[key] != parent.get(key):
            out[key] = want[key]
    rollout, above = want.get("rollout"), parent.get("rollout")
    if rollout is None:
        if above is not None:
            out["no_rollout"] = True
    else:
        diff = {k: v for k, v in rollout.items() if (above or {}).get(k) != v}
        if diff:
            out["rollout"] = diff
    return out


def _tier(parent: dict, explicit, follow: bool = True, **labels):
    """``(override, effective)`` for one tier: the level the document states, if
    it does, else the one its labels give. A level the document states is the
    tier's whole level -- except that a tier cannot clear a field the tier above
    has, only its rollout (J9), so a field it leaves out is inherited."""
    if explicit is not None:
        stated = _check_level(explicit)
        want = {**{k: v for k, v in parent.items() if k != "rollout"},
                **{k: v for k, v in stated.items() if k != "rollout"}}
        if "rollout" in stated:
            want["rollout"] = stated["rollout"]
    else:
        want = R.tier_level(parent, follow=follow, **labels)
        # A label that only restates the depth, under a tier that names no
        # preset, says nothing: reading labels a foreign tier by its depth, and
        # writing that back must not turn the depth into an override.
        preset = want.get("preset")
        if (preset and "preset" not in parent and R._DEPTH_PRESET.fullmatch(preset)
                and want.get("checker_ply") == int(preset[:-3])):
            del want["preset"]
    override = _level_diff(parent, want)
    return override, R._resolve(parent, override)


def _level_key(level: dict) -> str:
    """A level as something comparable and hashable (A1 groups by it)."""
    return json.dumps(level, sort_keys=True)


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


_resign_value = R.derived_resign_value


# What v2 holds in a match-context string (4): its cap in bytes and the byte set.
_HOST = re.compile(r"[a-z0-9.\-]+")
_MATCH_REF = re.compile(r"[A-Za-z0-9._\-]+")
_RATING_SYSTEM = re.compile(r"[a-z0-9\-]+")
_COUNTRY = re.compile(r"[A-Z]{2}")
_DAY_MS = 86_400_000


def _is_uint(v, bits: int = 32) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < (1 << bits)


def _is_pow2(v) -> bool:
    return _is_uint(v) and v >= 1 and v & (v - 1) == 0


def _nbytes(v) -> int:
    try:
        return len(v.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError):
        return -1


def _str_ok(v, cap: int, pattern=None) -> bool:
    """A non-empty string within ``cap`` bytes (and, if given, of the byte set
    ``pattern`` -- ASCII, so characters are bytes)."""
    n = _nbytes(v)
    if not (1 <= n <= cap):
        return False
    return pattern is None or pattern.fullmatch(v) is not None


def _url_ok(v) -> bool:
    return (isinstance(v, str) and v.startswith("https://") and len(v) <= 512
            and all(0x20 <= ord(c) <= 0x7E for c in v))


def _on_boundary(ms: int, precision: int) -> bool:
    """Whether ``ms`` is the first instant (UTC) of the day, month or year
    ``precision`` names (9.27); an unknown precision is not checked (I7)."""
    if ms % _DAY_MS:
        return False
    if precision not in (1, 2):
        return True
    d = datetime.fromtimestamp(ms // 1000, timezone.utc)
    return d.day == 1 and (precision == 1 or d.month == 1)


def _rating_hundredths(v):
    """A rating as the hundredths v2 stores, or None where it is not exactly
    that (not a 0.01 step, or out of range)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    n = round(v * 100)
    return n if 0 <= n <= 0xFFFFFFFF and n / 100 == v else None


def _game_start(g: dict, opening_abs: list) -> int:
    """How many leading plies of ``g`` v2 states as the game's initial board
    (0 or 1). A leading dice-less set position is a game that starts from a
    set-up board; one stating the opening position itself stays a ply, since an
    initial board equal to the default is never written (3.2)."""
    plies = g.get("plies") or []
    first = plies[0] if plies else None
    return int(first is not None and first.get("action_id") == R.ACTION_SET_POSITION
               and not first.get("d1")
               and [int(v) for v in first.get("set_position") or [0] * 26] != opening_abs)


def ply_layout(doc: dict):
    """``(keys, starts)``: the ``(game, ply)`` of every ply v2 holds, in
    ``ply_ref`` order, and each game's leading set-up position (0 or 1) that it
    does not."""
    opening_abs = variant_opening_abs(int(doc.get("variant") or 0)) or _p1_to_absolute(_STARTING_BOARD_P1)
    keys, starts = [], []
    for gi, g in enumerate(doc.get("games") or []):
        st = _game_start(g, opening_abs)
        starts.append(st)
        keys += [(gi, pi) for pi in range(st, len(g.get("plies") or []))]
    return keys, starts


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
        limit = doc.get("cube_limit")
        self.cube_limit = limit if _is_pow2(limit) else 0
        self._encode_games(flagged_illegal)

    def _gv(self, scope: int, ref: int, name: str, kind: str, value) -> None:
        """Carry a value v2 cannot hold in an annotation of ours (P6)."""
        self.annos.append((scope, ref, R.GV_PREFIX + name, R.GV_FORMAT + R._gv_text(kind, value)))

    def _game_fields(self, gi: int, g: dict, fields: dict) -> int:
        """Bits 4-7 of a ``GAME`` record (the game's cube and how it ended);
        returns the cube the game opens with (5.3)."""
        value, doubles = 1, 0
        v = g.get("initial_cube_value")
        if v is not None and v != 1:
            if _is_pow2(v) and (not self.cube_limit or v <= self.cube_limit):
                fields[4] = _varint(v)
                value = v
            else:
                self._gv(SCOPE_GAME, gi, "initial_cube_value", "i", v)
        owner = g.get("initial_cube_owner")
        if owner is not None and owner != 2:
            if owner in (0, 1) and not isinstance(owner, bool):
                fields[5] = _varint(owner)
            else:
                self._gv(SCOPE_GAME, gi, "initial_cube_owner", "i", owner)
        a = g.get("auto_doubles")
        if a is not None and a != 0:
            if _is_uint(a):
                fields[6] = _varint(a)
                doubles = a
            else:
                self._gv(SCOPE_GAME, gi, "auto_doubles", "i", a)
        t = g.get("termination")
        if t is not None:
            if _is_uint(t):
                fields[7] = _varint(t)
            else:
                self._gv(SCOPE_GAME, gi, "termination", "i", t)
        return value * 2 ** doubles

    def _encode_games(self, flagged_illegal: set) -> None:
        variant = int(self.doc.get("variant") or 0)
        opening_abs = variant_opening_abs(variant) or _p1_to_absolute(_STARTING_BOARD_P1)
        opening_p1 = variant_opening_p1(variant) or list(_STARTING_BOARD_P1)
        games = self.doc.get("games") or []
        if len(games) > MAX_GAMES:
            raise ValueError(f"too many games for an OGXM v2 file: {len(games)} (v2 holds {MAX_GAMES})")
        for gi, g in enumerate(games):
            plies = g.get("plies") or []
            if len(plies) > MAX_PLIES_PER_GAME:
                raise ValueError(f"game {gi} has too many plies for an OGXM v2 file: "
                                 f"{len(plies)} (v2 holds {MAX_PLIES_PER_GAME} per game)")
            board = list(opening_p1)
            fields: dict[int, bytes] = {}
            start = _game_start(g, opening_abs)
            if start:
                first = plies[0]
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
            cube = self._game_fields(gi, g, fields)

            out = bytearray(_record(b"", fields))
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
                    rv = p.get("resign_value")
                    if not _is_uint(rv):
                        rv = _resign_value(int(g.get("points_won") or 0), cube)
                    out += _record(b"", {1: _varint(rv)})
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
                elif action in (R.ACTION_BEAVER, R.ACTION_RACCOON, R.ACTION_PASS):
                    out.append(action | (_seat(color) << 6))
                    if action != R.ACTION_PASS:
                        double_pending = False
                        cube *= 4 if action == R.ACTION_BEAVER else 2
                elif action == R.ACTION_SETTLE:
                    out.append(action | (_seat(color) << 6) | 0x80)
                    out += _record(b"", {4: _equity(p.get("settle_value") or 0.0)})
                elif action == R.ACTION_CUBE_SET:
                    value, owner = p.get("cube_value"), p.get("cube_owner")
                    if not _is_pow2(value):
                        raise ValueError(f"game {gi} ply {pi}: a cube set to {value!r}")
                    f = {2: _varint(value)}
                    if owner in (0, 1):
                        f[8] = _varint(owner)
                    out.append(action | (_seat(color) << 6) | 0x80)
                    out += _record(b"", f)
                    cube = value
                elif action > R.LAST_KNOWN_ACTION and action != R.ACTION_ESCAPE:
                    # An action id nothing here assigns a meaning to: its extras
                    # are the producer's, kept as they came.
                    extras = base64.b64decode(p["extras_raw"]) if p.get("extras_raw") else b""
                    if action >= 64 and not extras:
                        extras = _record(b"", {7: _varint(action)})
                    out.append((action if action < 64 else R.ACTION_ESCAPE)
                               | (_seat(color) << 6) | (0x80 if extras else 0))
                    out += extras
                else:
                    raise ValueError(f"game {gi} ply {pi}: action {action} has no OGXM v2 form here")
            self.games.append(bytes(out))
        if len(self.ply_at) > MAX_TOTAL_PLIES:
            raise ValueError(f"too many plies for an OGXM v2 file: {len(self.ply_at)} "
                             f"(v2 holds {MAX_TOTAL_PLIES})")

    @staticmethod
    def _set_position(color: int, dice, board_abs: list[int]) -> bytes:
        fields: dict[int, bytes] = {6: _board(board_abs)}
        if dice is not None:
            fields[0] = bytes([int(dice[0]), int(dice[1])])
            fields[3] = b""
        return bytes([R.ACTION_SET_POSITION | (_seat(color) << 6) | 0x80]) + _record(b"", fields)

    def _put_str(self, fields: dict, bit: int, key: str, ok: bool) -> bool:
        """Store the string ``key`` at ``bit`` when v2 can hold it, else in an
        annotation; returns whether it is in the record."""
        v = self.doc.get(key)
        if v is None or v == "":
            return False
        if ok:
            fields[bit] = _str(v)
            return True
        self._gv(SCOPE_MATCH, 0, key, "s", v)
        return False

    def _profile(self, side: str, bit: int, fields: dict, has_platform: bool):
        """A ``player`` record (4.1) from ``<side>_profile``; what v2 cannot
        hold goes in ``x-gammonview-<side>_profile.<field>``. Returns the
        written ``kind`` (for ``player_seat``'s check)."""
        name = f"{side}_profile"
        pr = self.doc.get(name)
        if not isinstance(pr, dict) or not pr:
            return None
        out: dict[int, bytes] = {}

        def carry(field: str, kind: str) -> None:
            self.annos.append((SCOPE_MATCH, 0, f"{R.GV_PREFIX}{name}.{field}",
                               R.GV_FORMAT + R._gv_text(kind, pr[field])))

        uid = pr.get("user_id")
        if uid is not None and uid != "":
            if has_platform and _str_ok(uid, 64):
                out[0] = _str(uid)
            else:
                carry("user_id", "s")
        rating, system = pr.get("rating"), pr.get("rating_system")
        if rating is not None or system is not None:
            n = _rating_hundredths(rating) if rating is not None else None
            if n is not None and system is not None and _str_ok(system, 32, _RATING_SYSTEM):
                out[1] = _varint(n)
                out[2] = _str(system)
            else:
                # The two stand or fall together: a rating without its system
                # (or the reverse) is not a v2 record.
                if rating is not None:
                    carry("rating", "f")
                if system is not None:
                    carry("rating_system", "s")
        country = pr.get("country")
        if country is not None and country != "":
            if _COUNTRY.fullmatch(country) if isinstance(country, str) else False:
                out[3] = _str(country)
            else:
                carry("country", "s")
        kind = pr.get("kind")
        written_kind = None
        if kind is not None:
            if _is_uint(kind):
                out[4] = _varint(kind)
                written_kind = kind
            else:
                carry("kind", "i")
        if out:
            fields[bit] = _record(b"", out)
        return written_kind

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
                 | (4 if doc.get("beaver") else 0) | (8 if doc.get("raccoon") else 0)
                 | (16 if doc.get("auto_doubles") else 0))
        other = doc.get("rules_other")
        if other:
            if _is_uint(other) and not other & 0x1F:
                rules |= other
            else:
                self._gv(SCOPE_MATCH, 0, "rules_other", "i", other)
        if rules:
            fields[2] = _varint(rules)
        cube_limit = int(doc.get("cube_limit") or 0)
        if cube_limit > 0 and cube_limit & (cube_limit - 1) == 0:
            fields[3] = _varint(cube_limit)        # anything else is a source's "no limit"
        start = doc.get("score_start") or [0, 0]
        start = (int(start[0] or 0), int(start[1] or 0))
        if start != (0, 0):
            fields[4] = _varint(start[0]) + _varint(start[1])
        # The score is derived (M8) and the result follows from it; a result is
        # stored only where the score leaves it open.
        _starts, (w, b) = R._score_walk({"match_length": length, "score_start": start}, [
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
        started = int(doc["timestamp"]) * 1000 if doc.get("timestamp") else 0
        if started:
            fields[8] = _varint(started)
        completed = doc.get("completed_at")
        if completed is not None:
            if _is_uint(completed, 64):
                fields[9] = _varint(completed)
            else:
                self._gv(SCOPE_MATCH, 0, "completed_at", "i", completed)
        event = doc.get("event") or ""
        has_event = False
        if event:
            if len(event.encode("utf-8")) <= MAX_EVENT:
                fields[12] = _str(event)
                has_event = True
            else:
                self.annos.append((SCOPE_MATCH, 0, R.GV_KEY_EVENT, R.GV_FORMAT + event))
        year = doc.get("event_year")
        if year is not None:
            if _is_uint(year) and has_event:
                fields[13] = _varint(year)
            else:
                self._gv(SCOPE_MATCH, 0, "event_year", "i", year)
        precision = doc.get("date_precision")
        if precision is not None:
            if _is_uint(precision) and started and _on_boundary(started, precision):
                fields[14] = _varint(precision)
            else:
                self._gv(SCOPE_MATCH, 0, "date_precision", "i", precision)
        self._put_str(fields, 15, "stage", _str_ok(doc.get("stage"), 60))
        rnd = doc.get("round")
        if rnd is not None:
            if _is_uint(rnd) and 1 <= rnd <= 99:
                fields[16] = _varint(rnd)
            else:
                self._gv(SCOPE_MATCH, 0, "round", "i", rnd)
        self._put_str(fields, 17, "table", _str_ok(doc.get("table"), 24))
        self._put_str(fields, 18, "city", _str_ok(doc.get("city"), 80))
        country = doc.get("country")
        self._put_str(fields, 19, "country",
                      isinstance(country, str) and _COUNTRY.fullmatch(country) is not None)
        self._put_str(fields, 20, "event_url", _url_ok(doc.get("event_url")))
        has_platform = self._put_str(fields, 21, "platform",
                                     _str_ok(doc.get("platform"), 253, _HOST))
        ref = doc.get("match_ref")
        self._put_str(fields, 22, "match_ref",
                      has_platform and _str_ok(ref, 64, _MATCH_REF) and ref not in (".", ".."))
        kinds = {"white": self._profile("white", 23, fields, has_platform),
                 "black": self._profile("black", 24, fields, has_platform)}
        seat = doc.get("player_seat")
        if seat is not None:
            human = seat not in (0, 1) or kinds["white" if seat == 0 else "black"] in (None, 0)
            if _is_uint(seat) and human:
                fields[10] = _varint(seat)
            else:
                self._gv(SCOPE_MATCH, 0, "player_seat", "i", seat)
        if doc.get("crawford_before_start"):
            fields[11] = b""
        if doc.get("rated"):
            fields[25] = b""
        # Our `site` is where the match was played, free text; v2 states the
        # same in `city` (or names the platform). Only a `site` that is
        # neither needs its own record.
        site = doc.get("site")
        if site and site != (doc.get("city") or doc.get("platform")):
            self.annos.append((SCOPE_MATCH, 0, R.GV_KEY_SITE, R.GV_FORMAT + site))
        variant = doc.get("variant") or 0
        if not _is_uint(variant):
            raise ValueError(f"variant {variant!r} is not an OGXM v2 variant number")
        self.mtch_mandatory = _varint(length) + _varint(variant)
        self.mtch_fields = fields
        payload = _record(self.mtch_mandatory, fields)
        return payload, any(bit >= 12 for bit in fields)


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
    if alt.get("rollout_se") is not None:
        fields[3] = _equity(conv.delta(_number(alt["rollout_se"], "rollout_se")))
    if alt.get("cubeless_equity") is not None:
        fields[4] = _equity(conv.eq(_number(alt["cubeless_equity"], "cubeless_equity")))
    return _record(_varint(len(steps)) + bytes(steps) + _equity(conv.eq(alt.get("equity", 0.0))), fields)


def _number(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{what} {v!r} is not a number")
    return float(v)


def _uint_field(v, what: str, bits: int = 32) -> int:
    if not _is_uint(v, bits):
        raise ValueError(f"{what} {v!r} is not a count v2 can hold")
    return v


def _position_key(position, moves) -> tuple | None:
    """The position ``moves`` produce, for comparing two plays (M5)."""
    if position is None:
        return None
    board, mover_is_white = position
    try:
        return tuple(_apply_moves_p1(board, moves or [], mover_is_white))
    except Exception:
        return None


def _checker_extras(a: dict, count: int, nsources: int, total=None):
    """``(fields, conformant)``: the checker record's fields beyond the list and
    the level, from the document, and whether ``DECS`` can hold them as they
    are. The list's size decides two of them (7.1): the total only means
    something where the list was cut, and so do the counts of what was searched.
    A value ``DECS`` would reject is still written, for the annotation that
    keeps the decision exactly (P6)."""
    f: dict[int, bytes] = {}
    ok = True
    truncated = False
    if total is None:
        total = a.get("alternatives_total")
    if total is not None:
        _uint_field(total, "alternatives_total")
        f[1] = _varint(total)
        truncated = total > count
        ok &= truncated
    for bit, key in ((5, "rollouts_done"), (6, "deep_searched")):
        if a.get(key) is not None:
            f[bit] = _varint(_uint_field(a[key], key))
            ok &= truncated
    for bit, key in ((7, "position_tags"), (9, "source_band")):
        if a.get(key) is not None:
            f[bit] = _varint(_uint_field(a[key], key))
    if a.get("producer_ref") is not None:
        f[8] = _varint(_uint_field(a["producer_ref"], "producer_ref"))
        ok &= a["producer_ref"] < nsources
    return f, ok


def _checker_records(a: dict, ref: int, block_eff: dict, conv: _Converter, unplayed: bool,
                     position=None, played_moves=None, nsources: int = 0):
    """``(main, exact, places)``: the record ``DECS`` can carry (or None), the
    exact one for the annotation when that differs (or None), and where each
    document alternative sits in ``main`` (None for one it leaves out; the list
    is None without a ``main``). ``position`` is the board before a legal dice
    ply and ``played_moves`` its play, for A4."""
    full = a.get("alternatives") or []
    kept = list(range(len(full)))
    total = None
    if len(full) > MAX_ALTS:
        # v2 holds MAX_ALTS_PER_DECISION alternatives and lets a list be cut only
        # where `alternatives_total` states the full count (7.1). The played
        # move must stay in the list (A4), so it takes the last place if it
        # would fall past the cut.
        kept = kept[:MAX_ALTS]
        at = next((i for i, x in enumerate(full) if x.get("is_played")), None)
        if at is not None and at >= MAX_ALTS:
            kept[-1] = at
        total = max(len(full), int(a.get("alternatives_total") or 0))
    alts = [full[i] for i in kept]

    def lift(places):
        """``places`` over the kept list, over the document's own."""
        if places is None or len(kept) == len(full):
            return places
        out = [None] * len(full)
        for pos, where in enumerate(places):
            out[kept[pos]] = where
        return out

    best = float(a.get("best_equity", 0.0) or 0.0)
    loss = float(a.get("equity_loss", 0.0) or 0.0)
    has_extras = any(a.get(k) is not None for k in R.CHECKER_KEYS)
    if not alts and not best and not loss and not has_extras and a.get("level") is None:
        return None, None, None

    dec_override, dec_eff = _tier(
        block_eff, a.get("level"), preset=alts[0].get("eval_level") if alts else None,
        checker_ply=a.get("ply"))
    tiers = [_tier(dec_eff, alt.get("level"), preset=alt.get("eval_level")) for alt in alts]
    keys = [_level_key(eff) for _ov, eff in tiers]
    played = [i for i, alt in enumerate(alts) if alt.get("is_played")]

    def build(order, explicit: bool):
        fields: dict[int, bytes] = {}
        if order:
            fields[0] = _varint(len(order)) + b"".join(
                _alt_record(alts[i], tiers[i][0], i in played, conv) for i in order)
        extra, conformant = _checker_extras(a, len(order), nsources, total)
        fields.update(extra)
        if explicit or not order:
            fields[2] = _equity(conv.eq(best))
        if explicit or not played:
            fields[3] = _loss(conv.delta(loss))
        if dec_override:
            fields[4] = _level_bytes(dec_override)
        return _record(_varint(ref) + _varint(R.KIND_CHECKER), fields), conformant

    exact, _ = build(list(range(len(alts))), explicit=True)
    if unplayed or len(played) > 1:
        return None, exact, None
    if alts and abs(best - float(alts[0].get("equity", 0.0))) > 1e-9:
        return None, exact, None
    if played:
        p = played[0]
        if keys[p] != keys[0]:
            return None, exact, None                               # A5
        if abs((best - float(alts[p].get("equity", 0.0))) - loss) > _LOSS_TOLERANCE:
            return None, exact, None

    # A4: the played move, wherever it appears in the list, is the flagged
    # alternative. Another copy of it (a notation split two ways) is dropped
    # from the record v2 holds; the exact list stays in the annotation.
    keep = list(range(len(alts)))
    played_key = _position_key(position, played_moves) if position is not None else None
    if played_key is not None:
        same = {i for i in keep if _position_key(position, alts[i].get("move")) == played_key}
        if played and played[0] not in same:
            return None, exact, None
        keep = [i for i in keep if i not in same or (played and i == played[0])]

    # A1: alternatives of one level adjacent, equity non-increasing within each.
    order: list[int] = []
    groups: dict = {}
    for i in keep:
        groups.setdefault(keys[i], []).append(i)
    for idxs in groups.values():
        order.extend(idxs)
    enc = [_round_c(conv.eq(float(alts[i].get("equity", 0.0))) * 1e6) for i in range(len(alts))]
    for idxs in groups.values():
        if any(enc[idxs[j + 1]] > enc[idxs[j]] for j in range(len(idxs) - 1)):
            return None, exact, None
    if order != list(range(len(alts))):
        if not order or order[0] != 0:
            return None, exact, None
        main, conformant = build(order, explicit=False)
        places = [order.index(i) if i in order else None for i in range(len(alts))]
        return (main if conformant else None), exact, (lift(places) if conformant else None)
    main, conformant = build(order, explicit=False)
    return ((main, None, lift(list(range(len(alts))))) if conformant else (None, exact, None))


class _Ctx:
    """What the records of one block need to know about the block."""

    def __init__(self, match: "_Match", block_eff: dict, written_currency: int, nsources: int,
                 luck_label: str):
        self.match = match
        self.block_eff = block_eff
        self.currency = written_currency
        self.nsources = nsources
        self.luck_label = luck_label

    @property
    def mwc(self) -> bool:
        return self.currency == R.CURRENCY_CUBEFUL_MATCH

    def currency_override(self, sub: dict):
        """A cube decision's own currency, where it states one that is not the
        block's and that can be written; else None. A match currency in a
        money game has no frame to convert through."""
        c = sub.get("currency")
        if c is None:
            return None
        _uint_field(c, "currency")
        if c == self.currency or (c == R.CURRENCY_CUBEFUL_MATCH and self.match.match_length <= 0):
            return None
        return c


def _cube_record(sub: dict, ref: int, verdict: int, loss, ctx: _Ctx, explicit_level, conv: _Converter,
                 conv_for, **labels):
    """``(record, conformant)``. ``conv_for(currency)`` makes the converter for a
    currency the decision states itself."""
    own = ctx.currency_override(sub)
    if own is not None:
        conv = conv_for(own)
    fields: dict[int, bytes] = {
        0: _equity(conv.eq(sub.get("no_double_equity", 0.0))),
        1: _equity(conv.eq(sub.get("double_take_equity", 0.0))),
        2: _equity(conv.eq(sub.get("double_pass_equity", 0.0))),
    }
    if "eval" in sub:
        fields[3] = _probs(sub["eval"])
    if loss is not None:
        fields[4] = _loss(conv.delta(loss))
    if sub.get("take_point") is not None:
        fields[5] = _prob(_number(sub["take_point"], "take_point"))
    if sub.get("window_searched"):
        fields[6] = b""
    override, _eff = _tier(ctx.block_eff, explicit_level, **labels)
    if override:
        fields[7] = _level_bytes(override)
    if sub.get("is_optional"):
        fields[8] = b""
    if sub.get("is_free_cube"):
        fields[9] = b""
    if sub.get("cubeful_take_value") is not None:
        fields[10] = _equity(conv.eq(_number(sub["cubeful_take_value"], "cubeful_take_value")))
    if own is not None:
        fields[11] = _varint(own)
    ok = True
    if sub.get("producer_ref") is not None:
        fields[12] = _varint(_uint_field(sub["producer_ref"], "producer_ref"))
        ok = sub["producer_ref"] < ctx.nsources
    return _record(_varint(ref) + _varint(R.KIND_CUBE) + _varint(verdict), fields), ok


def _luck_record(a: dict, ref: int, ctx: _Ctx, conv: _Converter):
    """``(record, conformant)``."""
    override, _eff = _tier(ctx.block_eff, a.get("luck_level"), follow=False,
                         **R.luck_labels(ctx.luck_label))
    fields: dict[int, bytes] = {}
    if override:
        fields[0] = _level_bytes(override)
    ok = True
    if a.get("luck_producer_ref") is not None:
        fields[1] = _varint(_uint_field(a["luck_producer_ref"], "luck_producer_ref"))
        ok = a["luck_producer_ref"] < ctx.nsources
    return _record(_varint(ref) + _varint(R.KIND_ROLL) + _equity(conv.delta(a["luck"])), fields), ok


def _block_records(match: _Match, select, ctx: _Ctx):
    """``(decs, extra, frames, places)``: the records ``DECS`` holds, sorted,
    the per-ply records only the annotation can hold, the ``frame=`` entries
    for plies whose source frame is not ours, and where a checker decision's
    document alternatives sit in the record ``DECS`` holds for it."""
    mwc = ctx.mwc
    decs: list[tuple[int, int, bytes]] = []
    extra: dict[int, list[tuple[int, bytes]]] = {}
    frames: list[str] = []
    places: dict[int, list] = {}
    in_force = None
    plies = match.ply_at

    def conv_for(p, currency):
        return _Converter(p, currency == R.CURRENCY_CUBEFUL_MATCH, None)

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

        def cube(sub, verdict, loss, *, ply_label=False):
            labels = {"preset": sub.get("eval_level")}
            if ply_label:
                labels["cube_ply"] = sub.get("ply")
            return _cube_record(sub, ref, verdict, loss, ctx, sub.get("level"), conv,
                                lambda c: conv_for(p, c), **labels)

        if 0 <= action <= 20 or illegal:
            nxt = plies[idx + 1][1].get("action_id") if idx + 1 < len(plies) else None
            unplayed = (0 <= action <= 20 and not (p.get("moves") or [])
                        and nxt in (*_RESIGNS, _FORFEIT) and plies[idx + 1][0][0] == key[0])
            legal_dice = 0 <= action <= 20 and not illegal
            main, exact, placed = _checker_records(
                a, ref, ctx.block_eff, conv, unplayed,
                match.position_before.get(ref) if legal_dice else None,
                p.get("moves") or [], ctx.nsources)
            if main is not None:
                recs.append((R.KIND_CHECKER, main, True))
                places[ref] = placed
            if exact is not None:
                extra.setdefault(ref, []).append((R.KIND_CHECKER, exact))
            md, cd = a.get("missed_double"), a.get("cube_decision")
            if isinstance(md, dict):
                verdict = _VERDICT.get(md.get("correct_action") or "double", 1)
                rec, ok = cube(md, verdict, float(md.get("equity_loss", 0.0) or 0.0))
                recs.append((R.KIND_CUBE, rec, ok and verdict in _OFFER_VERDICTS))
            elif isinstance(cd, dict):
                rec, ok = cube(cd, 1 if cd.get("should_double") else 0, None)
                recs.append((R.KIND_CUBE, rec, ok))
            if "luck" in a:
                rec, ok = _luck_record(a, ref, ctx, conv)
                recs.append((R.KIND_ROLL, rec, ok))
        elif action in R.CUBE_ACTIONS:
            verdict = _VERDICT.get(a.get("correct_action") or "no_double", 0)
            allowed = _OFFER_VERDICTS if action == R.ACTION_DOUBLE else _RESPONSE_VERDICTS
            loss = float(a.get("equity_loss", 0.0) or 0.0)
            rec, ok = cube(a, verdict, loss if loss else None, ply_label=True)
            recs.append((R.KIND_CUBE, rec, ok and verdict in allowed))
        elif action in _RESIGNS:
            fields: dict[int, bytes] = {
                1: _equity(conv.delta(a.get("resign_error", 0.0) or 0.0)),
                2: _equity(conv.delta(a.get("take_resign_error", 0.0) or 0.0)),
                4: _loss(conv.delta(a.get("equity_loss", 0.0) or 0.0)),
            }
            if a.get("correct_value") is not None:
                fields[0] = _varint(_uint_field(a["correct_value"], "correct_value"))
            if "eval" in a:
                fields[3] = _probs(a["eval"])
            override, _eff = _tier(ctx.block_eff, a.get("level"))
            if override:
                fields[5] = _level_bytes(override)
            ok = True
            if a.get("producer_ref") is not None:
                fields[6] = _varint(_uint_field(a["producer_ref"], "producer_ref"))
                ok = a["producer_ref"] < ctx.nsources
            recs.append((R.KIND_RESIGN, _record(_varint(ref) + _varint(R.KIND_RESIGN), fields), ok))

        for kind, rec, ok in recs:
            if illegal or not ok:
                extra.setdefault(ref, []).append((kind, rec))
            else:
                decs.append((ref, kind, rec))
    decs.sort(key=lambda t: (t[0], t[1]))
    for ref in extra:
        extra[ref].sort(key=lambda t: t[0])
    return decs, extra, frames, places


_HEX64 = re.compile(r"[0-9a-f]{64}")


def _uuid_ok(v) -> bool:
    """Whether ``v`` is the hyphenated lower-case form this module reads back."""
    if not isinstance(v, str):
        return False
    try:
        return str(uuid.UUID(v)) == v
    except ValueError:
        return False


def _block_fields(info: dict, match: "_Match"):
    """``(fields, items, nsources, currency)`` for a block's ``ANAL`` record: the
    optional fields by bit (not the level or coverage), the
    ``x-gammonview-analysis`` items for values v2 cannot hold (P6), how many
    sources the record states, and the currency it is written in.

    A block is written in its stated currency when that can be converted to:
    match winning chances need a match to be a chance of winning, every other
    currency is the document's numbers as they are. Otherwise it is written in
    the default for the match, and reads back so (profile section 4)."""
    fields: dict[int, bytes] = {}
    items: list[str] = []

    def item(name: str, text: str) -> None:
        items.append(f"{name}=" + quote(text, safe=_SAFE))

    def string(bit: int, key: str, cap: int = MAX_STRING, pattern=None) -> bool:
        v = info.get(key)
        if v is None or v == "":
            return False
        if _str_ok(v, cap, pattern):
            fields[bit] = _str(v)
            return True
        item(key, v)
        return False

    producer = info.get("producer")
    if producer is not None:
        if _is_uint(producer):
            fields[1] = _varint(producer)
        else:
            item("producer", R._gv_text("i", producer))
    if info.get("complete"):
        fields[3] = b""

    string(5, "model_id")
    string(6, "model_name")
    digest = info.get("model_digest")
    if digest:
        if isinstance(digest, str) and _HEX64.fullmatch(digest):
            fields[7] = bytes.fromhex(digest)
        else:
            item("model_digest", str(digest))
    string(8, "engine_build")

    default = R.default_currency(match.match_length)
    stated = info.get("currency")
    if stated is not None:
        _uint_field(stated, "currency")
    currency = default if stated is None or (
        stated == R.CURRENCY_CUBEFUL_MATCH and match.match_length <= 0) else stated
    fields[9] = _varint(currency)

    if info.get("cube_efficiency") is not None:
        fields[10] = _prob(_number(info["cube_efficiency"], "cube_efficiency"))
    met_id = info.get("met_id") or ("kazaross-xg2" if match.match_length > 0 else None)
    if met_id:
        fields[11] = _str(met_id)
    string(12, "tables")

    dials = info.get("dials")
    if isinstance(dials, dict) and dials:
        df: dict[int, bytes] = {}
        for bit, key in R.DIAL_FLAGS_AND_VARINTS:
            v = dials.get(key)
            if v is None:
                continue
            kind = R.GV_DIAL_FIELDS[key]
            if kind == "b":
                if v:
                    df[bit] = b""
            elif kind == "f":
                num = _number(v, "dials." + key)
                if num >= 0:
                    df[bit] = _loss(num)
                else:
                    item("dials." + key, R.number_text(num))
            elif _is_uint(v):
                df[bit] = _varint(v)
            elif isinstance(v, int) and not isinstance(v, bool):
                item("dials." + key, str(v))
            else:
                raise ValueError(f"dials.{key} {v!r} is not a count")
        if df:
            fields[13] = _record(b"", df)

    if info.get("timestamp"):
        fields[14] = _varint(int(info["timestamp"]) * 1000)
    completed = info.get("completed_at")
    if completed is not None:
        if _is_uint(completed, 64):
            fields[15] = _varint(completed)
        else:
            item("completed_at", R._gv_text("i", completed))
    if info.get("duration_ms"):
        fields[16] = _varint(int(info["duration_ms"]))

    sources = info.get("sources")
    nsources = 0
    if isinstance(sources, list) and sources:
        if all(_uuid_ok(x) for x in sources):
            fields[17] = _varint(len(sources)) + b"".join(uuid.UUID(x).bytes for x in sources)
            nsources = len(sources)
        else:
            items.append("sources=" + ",".join(quote(str(x), safe=_SAFE) for x in sources))
    return fields, items, nsources, currency


def _coverage(info: dict, match: "_Match", decs) -> list[int]:
    """The ``ply_ref``s a block's ``coverage`` names, ascending: the plies the
    document lists that exist, and every ply the block holds a decision for (6.5
    makes a decision outside its coverage an error)."""
    listed = info.get("coverage")
    if not listed:
        return []
    refs = set()
    for entry in listed:
        try:
            ref = match.ref_of.get((int(entry[0]), int(entry[1])))
        except (TypeError, ValueError, IndexError):
            ref = None
        if ref is not None:
            refs.add(ref)
    if not refs:
        return []
    refs |= {ref for ref, _k, _b in decs}
    return sorted(refs)


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
# Annotations, the clock and the video (8.2 - 8.4)
# ---------------------------------------------------------------------------

MAX_DRAWINGS = 256
MAX_ANNOTATIONS = 4096
MAX_ANNO_TOTAL = 256 * 1024


def _opt_uint(d: dict, key: str, what: str, bits: int = 32) -> int:
    v = d.get(key)
    if v is None:
        return 0
    if not _is_uint(v, bits):
        raise ValueError(f"{what} {key} {v!r} is not a count v2 can hold")
    return v


def anno_problem(a: dict) -> str | None:
    """Why v2's ``ANNO`` cannot hold the annotation ``a`` (``anno_canon``'s
    shape), or None. Such a record travels in ``x-gammonview-annotations``."""
    if not _is_uint(a["ref"]):
        return "its target is out of range"
    for name in ("value", "key", "lang", "author"):
        v = a.get(name)
        if v is not None and (not isinstance(v, str) or not 0 <= _nbytes(v) <= MAX_STRING):
            return f"its {name} is not a string v2 can hold"
    if a.get("at") is not None and not _is_uint(a["at"], 64):
        return "its time is not an instant"
    scope = a["scope"]
    if scope >= R.SCOPE_DECISION and not _is_uint(a.get("kind")):
        return "its decision kind is not a count"
    if scope == R.SCOPE_ALTERNATIVE and not _is_uint(a.get("alt_index")):
        return "its alternative is not a count"
    drawings = a.get("drawings") or []
    if not isinstance(drawings, list) or len(drawings) > MAX_DRAWINGS:
        return "it has more drawings than v2 allows"
    if drawings and scope == R.SCOPE_MATCH:
        return "a match has no board to draw on (D3)"
    for d in drawings:
        if not isinstance(d, dict) or not _is_uint(d.get("shape")):
            return "a drawing has no shape"
        at, to = d.get("at"), d.get("to")
        if not _is_uint(at, 8) or (to is not None and not _is_uint(to, 8)):
            return "a drawing's point is not a point"
        if not 0 <= at <= 27 or (to is not None and not 0 <= to <= 27):
            return "a drawing's point is off the board (D2)"
        if d["shape"] == 1 and (to is None or to == at):
            return "an arrow needs a head that is not its tail (D1)"
        if d["shape"] == 0 and to is not None:
            return "a highlight has no head (D1)"
        if d.get("color") is not None and not _is_uint(d["color"]):
            return "a drawing's colour is not a colour"
    return None


def anno_canon(a: dict) -> bytes:
    """The canonical ``ANNO`` record (8.4) of ``a``: ``scope``, ``ref``,
    ``value`` and whichever of ``key``, ``kind``, ``alt_index``, ``lang``,
    ``author``, ``at``, ``drawings`` and ``analysis`` (a hyphenated
    identifier) it states. An empty string is no string."""
    fields: dict[int, bytes] = {}
    if a.get("key"):
        fields[0] = _str(a["key"])
    if a.get("kind") is not None:
        fields[1] = _varint(a["kind"])
    if a.get("alt_index") is not None:
        fields[2] = _varint(a["alt_index"])
    if a.get("lang"):
        fields[3] = _str(a["lang"])
    if a.get("author"):
        fields[4] = _str(a["author"])
    if a.get("at") is not None:
        fields[5] = _varint(a["at"])
    if a.get("drawings"):
        out = bytearray(_varint(len(a["drawings"])))
        for d in a["drawings"]:
            df: dict[int, bytes] = {}
            if d.get("to") is not None:
                df[0] = bytes([d["to"]])
            if d.get("color") is not None:
                df[1] = _varint(d["color"])
            out += _record(_varint(d["shape"]) + bytes([d["at"]]), df)
        fields[6] = bytes(out)
    if a.get("analysis"):
        fields[7] = uuid.UUID(a["analysis"]).bytes
    return _record(_varint(a["scope"]) + _varint(a["ref"]) + _str(a.get("value") or ""), fields)


class _Anno:
    """One annotation of the document, placed. ``native`` says whether the file
    being written holds what it addresses, so ``ANNO`` can state it; if not (or
    if v2 cannot hold the record) it travels in ``x-gammonview-annotations``,
    which names the target in the document's own terms (``coords``)."""

    def __init__(self, scope: int, ref, rec: dict, coords: dict, analysis=None, kind=None,
                 alt=None, native: bool = True):
        self.coords = coords
        self.rec = rec
        self.analysis = analysis
        self.group = json.dumps(coords, sort_keys=True)
        self.a = {"scope": scope, "ref": ref if ref is not None else 0, "value": rec.get("value") or "",
                  "key": rec.get("key"), "lang": rec.get("lang"), "author": rec.get("author"),
                  "at": rec.get("at"), "drawings": rec.get("drawings"), "analysis": analysis,
                  "kind": kind, "alt_index": alt}
        self.problem = None if ref is not None else "its target is not in the file"
        if self.problem is None:
            self.problem = anno_problem(self.a)
        self.native = native and self.problem is None
        self.bytes = anno_canon(self.a) if self.problem is None else None

    def as_document_index(self) -> bytes | None:
        """The record with an alternative addressed by its place in the document's
        list, which is where another producer's ``DECS`` keeps it."""
        if self.problem is not None or self.a["scope"] != R.SCOPE_ALTERNATIVE:
            return self.bytes
        return anno_canon({**self.a, "alt_index": self.coords["i"]})

    def sort_key(self, seq: int, document_index: bool = False):
        a = self.a
        alt = self.coords["i"] if document_index and a["scope"] == R.SCOPE_ALTERNATIVE else a["alt_index"]
        return P.anno_sort_key(a["scope"], a["ref"], a["analysis"], a["kind"], alt,
                               a["key"] or None, a["lang"] or None, seq)

    def fallback_item(self) -> dict:
        return {**self.coords, "v": self.rec}


def _records(holder: dict, what: str) -> list[dict]:
    """The annotations a document object states, each a record."""
    got = holder.get("annotations")
    if got is None:
        return []
    if not isinstance(got, list) or not all(isinstance(r, dict) for r in got):
        raise ValueError(f"{what}: annotations must be a list of objects")
    return got


def _own(rec: dict, scope: int) -> dict:
    """``rec`` without the keys that name its target (``kind``), checked: a key
    may not be a v2 field name (8.4.2), and one in our namespace would be read
    as a value of ours."""
    key = rec.get("key")
    if key:
        if key in R.V2_FIELD_NAMES:
            raise ValueError(f"annotation key {key!r} is the name of a v2 field (8.4.2)")
        if key.startswith(R.GV_PREFIX) and scope <= R.SCOPE_PLY:
            raise ValueError(f"annotation key {key!r} is in the x-gammonview namespace, which is ours")
    out = {k: v for k, v in rec.items() if k != "kind"}
    if out.get("value") is None:
        out["value"] = ""
    return out


def document_annos(doc: dict, match: _Match) -> list[_Anno]:
    """The annotations at match, game and ply scope, in document order."""
    out: list[_Anno] = []
    for rec in _records(doc, "the match"):
        out.append(_Anno(R.SCOPE_MATCH, 0, _own(rec, 0), {"s": 0}))
    for gi, g in enumerate(doc.get("games") or []):
        for rec in _records(g, f"game {gi}"):
            out.append(_Anno(R.SCOPE_GAME, gi, _own(rec, 1), {"s": 1, "g": gi}))
        for pi, p in enumerate(g.get("plies") or []):
            for rec in _records(p, f"game {gi} ply {pi}"):
                out.append(_Anno(R.SCOPE_PLY, match.ref_of.get((gi, pi)), _own(rec, 2),
                                 {"s": 2, "g": gi, "p": pi}))
    return out


def block_annos(match: _Match, select, aid: str, decs: list, places: dict) -> list[_Anno]:
    """The annotations at decision and alternative scope of one block, in
    ``ply_ref`` order. A decision ``DECS`` does not carry (an illegal play's,
    one kept exactly in an annotation of ours) cannot be addressed by ``ANNO``."""
    held = {(ref, kind) for ref, kind, _rec in decs}
    out: list[_Anno] = []

    def holder_ok(p: dict, a: dict, kind, what: str) -> None:
        if not _is_uint(kind) or R.decision_holder(p, a, kind) is None:
            raise ValueError(f"{what}: annotation names a decision (kind {kind!r}) the analysis lacks")

    for key, p in match.ply_at:
        a = select(p)
        if not isinstance(a, dict):
            continue
        gi, pi = key
        ref = match.ref_of[key]
        what = f"game {gi} ply {pi}"
        nat = R.natural_kind(p)

        def decision(rec: dict, kind: int) -> None:
            out.append(_Anno(R.SCOPE_DECISION, ref, _own(rec, 3),
                             {"s": 3, "g": gi, "p": pi, "a": aid, "k": kind}, aid, kind, None,
                             (ref, kind) in held))

        for rec in _records(a, what):
            kind = rec.get("kind", nat)
            holder_ok(p, a, kind, what)
            decision(rec, kind)
        if nat == R.KIND_CHECKER:
            for name in ("missed_double", "cube_decision"):
                sub = a.get(name)
                if isinstance(sub, dict):
                    for rec in _records(sub, what):
                        holder_ok(p, a, R.KIND_CUBE, what)
                        decision(rec, R.KIND_CUBE)
            for i, alt in enumerate(a.get("alternatives") or []):
                recs = _records(alt, what)
                if recs:
                    holder_ok(p, a, R.KIND_CHECKER, what)
                where = places.get(ref) if (ref, R.KIND_CHECKER) in held else None
                place = where[i] if where is not None and i < len(where) else None
                for rec in recs:
                    out.append(_Anno(R.SCOPE_ALTERNATIVE, ref, _own(rec, 4),
                                     {"s": 4, "g": gi, "p": pi, "a": aid, "k": R.KIND_CHECKER, "i": i},
                                     aid, R.KIND_CHECKER, place if place is not None else 0,
                                     place is not None))
    return out


def check_annos(annos: list[_Anno]) -> None:
    """Spec N2: within one target, a ``(key, lang)`` pair is used once."""
    seen: set = set()
    for a in annos:
        if a.a.get("key"):
            ident = (a.group, a.a["key"], a.a.get("lang") or None)
            if ident in seen:
                raise ValueError(f"annotation key {a.a['key']!r} is used twice on one target (N2)")
            seen.add(ident)


def _clock_section(doc: dict, match: _Match) -> bytes | None:
    """``CLCK`` (8.2) from ``clock_info`` and each ply's ``timestamp_ms``, or None where
    the document has no clock or the series cannot be written: a reading after a
    ply without one, a first reading that is not 0, one that runs backwards."""
    clock = doc.get("clock_info")
    if not isinstance(clock, dict):
        return None
    flags = _opt_uint(clock, "flags", "clock_info", 8)
    header = tuple(_opt_uint(clock, k, "clock_info") for k in
                   ("reserve_ms", "delay_ms", "increment_ms", "start_timestamp")) + (flags,)
    precision = clock.get("precision")
    if precision is None:
        precision = P.CLOCK_PRECISION
    if not _is_uint(precision) or precision < 1:
        raise ValueError(f"clock precision {precision!r} is not a step")
    ts: list[int] = []
    gap = False
    for _key, p in match.ply_at:
        v = p.get("timestamp_ms")
        if v is None:
            gap = True
            continue
        if gap:
            return None
        if not _is_uint(v):
            raise ValueError(f"timestamp_ms {v!r} is not a time v2 can hold")
        ts.append(v)
    return P.encode_clock(header, ts, len(match.ply_at), precision)


def _video_section(doc: dict, match: _Match) -> bytes | None:
    """``VIDO`` (8.3) from ``video_info`` and the marks on the plies. A mark on a ply
    v2 has no record of (a game's set-up position, a game past the 256th) is
    dropped by itself."""
    video = doc.get("video_info")
    if not isinstance(video, dict):
        return None
    kind = _opt_uint(video, "kind", "video_info", 8)
    offset = video.get("offset_ms") or 0
    if not isinstance(offset, int) or isinstance(offset, bool) or not -(1 << 31) <= offset < 1 << 31:
        raise ValueError(f"video_info offset_ms {offset!r} is not an offset v2 can hold")
    url = video.get("url") or ""
    if not isinstance(url, str):
        raise ValueError("video_info url must be a string")
    raw = url.encode("utf-8") if url else b""
    if len(raw) > P.MAX_VIDEO_URL or not P.url_storable(raw, kind):
        if url:
            match.annos.append((SCOPE_MATCH, 0, R.GV_KEY_VIDEO_URL, R.GV_FORMAT + url))
        raw = b""
    marks = []
    for (gi, pi), p in match.ply_at:
        if p.get("video_ms") is None:
            continue
        v2pi = pi - match.game_start[gi]
        if gi > 255 or v2pi > 0xFFFF:
            continue
        wall, behind = p.get("wall_ms"), p.get("behind_live_ms")
        for name, v, bits in (("video_ms", p["video_ms"], 32), ("wall_ms", wall, 64),
                              ("behind_live_ms", behind, 32)):
            if v is not None and not _is_uint(v, bits):
                raise ValueError(f"{name} {v!r} is not a time v2 can hold")
        marks.append((gi, v2pi, bool(p.get("video_hand_anchored")), p["video_ms"], wall, behind))
    return P.encode_video((kind, bool(video.get("is_live")), offset, raw), marks)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _anno_record(scope: int, ref: int, key: str, value: str) -> bytes:
    return _record(_varint(scope) + _varint(ref) + _str(value), {0: _str(key)})


class _Block:
    """One analysis block as the document canonically encodes it."""

    def __init__(self, aid: bytes, anal: bytes, decs: bytes, annos: list, fields: dict):
        self.aid = aid
        self.fields = fields      # the ANAL record's optional fields, by bit
        self.aid_str = _uuid_str(aid)
        self.anal = anal
        self.decs = decs
        self.annos = annos        # (scope, ref, key, value) this block adds to ANNO

    def anno_bytes(self) -> bytes:
        return b"".join(_anno_record(*a) for a in sorted(
            self.annos, key=lambda t: (t[0], t[1], t[2].encode("utf-8"))))

    def anal_started(self, ms: int) -> bytes:
        """The ``ANAL`` record with ``started_at`` the exact instant ``ms``: a
        source that stated milliseconds keeps them while the document's second
        is the same (P4)."""
        return _record(self.aid, {**self.fields, 14: _varint(ms)})


class _Parts:
    """The document, encoded part by part: what ``ogxm2_passthrough``
    fingerprints and what ``_assemble`` puts in a file."""

    def __init__(self, doc: dict, match: _Match, mtch: bytes, uses_21: bool, blocks: list,
                 annos: list, clck: bytes | None, vido: bytes | None):
        self.doc = doc
        self.match = match
        self.mtch = mtch
        self.uses_21 = uses_21
        self.blocks = blocks
        self.annos = annos        # the document's annotations (``_Anno``), in document order
        self.clck = clck          # the canonical CLCK / VIDO payloads, or None
        self.vido = vido


def _encode(ogxm: dict) -> _Parts:
    doc = ogxm
    if any(not p.get("ogid_before") for g in doc.get("games") or [] for p in g.get("plies") or []
           if p.get("action_id") is not None and p.get("action_id") not in _MARKERS):
        doc = copy.deepcopy(ogxm)
        _derive_ogids(doc)

    blocks = _blocks(doc)
    if len(blocks) > MAX_ANALYSES:
        raise ValueError(f"too many analysis blocks for an OGXM v2 file: {len(blocks)} "
                         f"(v2 holds {MAX_ANALYSES})")
    flagged = {(gi, pi) for gi, g in enumerate(doc.get("games") or [])
               for pi, p in enumerate(g.get("plies") or [])
               for _info, select in blocks
               if isinstance(select(p), dict) and select(p).get("illegal_move")}
    match = _Match(doc, flagged)
    mtch, uses_21 = match.mtch()
    match_bytes = mtch + b"".join(match.games)

    out: list[_Block] = []
    annos = document_annos(doc, match)
    used_ids: set[bytes] = set()
    for k, (info, select) in enumerate(blocks):
        aid = _analysis_id(k, info, match_bytes)
        if aid in used_ids:
            raise ValueError(f"analysis block {k} repeats an analysis_id")
        used_ids.add(aid)
        aid_str = _uuid_str(aid)
        objs = [select(p) for _k, p in match.ply_at]
        stated = info.get("level")
        block_eff = _check_level(stated) if stated is not None else R.block_level_of(info, objs)
        luck_label = info.get("luck_eval_level") or "1ply"
        fields, items, nsources, written_currency = _block_fields(info, match)
        ctx = _Ctx(match, block_eff, written_currency, nsources, luck_label)
        decs, extra, frames, places = _block_records(match, select, ctx)

        anal_fields: dict[int, bytes] = fields
        if block_eff:
            anal_fields[2] = _level_bytes(block_eff)
        coverage = _coverage(info, match, decs)
        if coverage:
            anal_fields[4] = _varint(len(coverage)) + b"".join(
                _varint(r - (coverage[i - 1] + 1 if i else 0)) for i, r in enumerate(coverage))
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

        if info.get("eval_level") != block_eff.get("preset"):
            items.append("level=" + quote(info.get("eval_level") or "", safe=_SAFE))
        if any(k == R.KIND_ROLL for _r, k, _b in decs) or any(
                k == R.KIND_ROLL for recs in extra.values() for k, _b in recs):
            items.append("luck=" + quote(luck_label, safe=_SAFE))
        items.append("pr=" + ",".join(tokens))
        if frames:
            items.append("frame=" + ",".join(frames))
        gv_annos: list[tuple[int, int, str, str]] = []
        for key_, value in _chunked(R.GV_KEY_ANALYSIS + aid_str, R.GV_FORMAT + ";".join(items)):
            gv_annos.append((SCOPE_MATCH, 0, key_, value))
        for ref, recs in extra.items():
            value = R.GV_FORMAT + base64.b64encode(b"".join(rec for _k, rec in recs)).decode("ascii")
            for key_, part in _chunked(R.GV_KEY_DECISIONS + aid_str, value):
                gv_annos.append((SCOPE_PLY, ref, key_, part))
        out.append(_Block(aid, anal, decs_payload, gv_annos, anal_fields))
        annos += block_annos(match, select, aid_str, decs, places)
    check_annos(annos)
    clck = _clock_section(doc, match)
    vido = _video_section(doc, match)
    return _Parts(doc, match, mtch, uses_21, out, annos, clck, vido)


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
    # The document's own annotations: the source's bytes while it still encodes
    # to what the source held, else v2's record where the file holds what it
    # addresses, else a record of ours.
    ours = list(match.annos) + [a for _an, _d, _s, annos in plan.blocks for a in annos]
    carried: list[dict] = []
    for a in parts.annos:
        foreign = a.analysis in plan.foreign_verbatim
        canon = a.as_document_index() if foreign else a.bytes
        raw = plan.take_raw(canon) if canon is not None and (
            a.analysis is None or a.analysis in plan.verbatim_ids) else None
        if raw is not None:
            entries.append((a.sort_key(len(entries)), raw))
        elif foreign:
            # Another producer's DECS is as it came, so the annotation can address
            # what it holds: a decision of that kind, an alternative in its list.
            held = plan.foreign_decs.get(a.analysis, {}).get((a.a["ref"], a.a["kind"]))
            if canon is not None and held is not None and (
                    a.a["scope"] == R.SCOPE_DECISION or a.coords["i"] < held):
                entries.append((a.sort_key(len(entries), document_index=True), canon))
            else:
                carried.append(a.fallback_item())
        elif a.native:
            entries.append((a.sort_key(len(entries)), a.bytes))
        else:
            carried.append(a.fallback_item())
    if carried:
        text = base64.b64encode(json.dumps(carried, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False).encode("utf-8")).decode("ascii")
        ours += [(SCOPE_MATCH, 0, key, part)
                 for key, part in _chunked(R.GV_KEY_ANNOTATIONS, R.GV_FORMAT + text)]
    for scope, ref, key, value in ours:
        entries.append((P.anno_sort_key(scope, ref, None, None, None, key, None, len(entries)),
                        _anno_record(scope, ref, key, value)))
    if entries:
        entries.sort(key=lambda e: e[0])
        payload = b"".join(rec for _k, rec in entries)
        # Spec section 10: never write a file past the limits, nor drop a record.
        if len(entries) > MAX_ANNOTATIONS:
            raise ValueError(f"{len(entries)} annotations (ours included) exceed OGXM v2's {MAX_ANNOTATIONS}")
        if len(payload) > MAX_ANNO_TOTAL:
            raise ValueError(f"the annotations (ours included) are {len(payload)} bytes, past OGXM v2's {MAX_ANNO_TOTAL}")
        secs.append(("ANNO", _section(b"ANNO", payload, False)))
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
