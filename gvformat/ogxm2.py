# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Reading OGXM v2 -- HedgeHog's current match format -- into the document
``read_gvab`` returns for a v1 file.

Mirrors ``gvformat-js/src/ogxm2.js``; keep the two in step. The reasoning lives
there in full and is summarised here.

v2 (HedgeHog's ``docs/OGXM_FORMAT_SPEC.md``, 2.2 at the time of writing) keeps
v1's magic and changes nearly every byte: a 16-byte header, sections with a
9-byte header, varint-framed records with a presence mask, plies addressed by
one match-wide ordinal (``ply_ref``), and one ``DECS`` section holding every
decision of an analysis block.

**A reader into our document's shape.** The document that comes out is the one
a v1 file holding the same match would give, so nothing downstream learns there
are two versions. ``ogxm2_writer`` is the other half, and since 1.6.0 what
``write_gvab`` writes; ``docs/OGXM_V2_PROFILE.md`` is the profile the two
share.

**Two kinds of block.** A block our writer made is marked by its
``x-gammonview-analysis`` annotation and read exactly: the ``x-`` annotations
put back what v2 has no field for (an illegal play's analysis and steps, the
PR-counting flags, level labels, ``site``), and the reader derives nothing it
was told. Any other producer's block is read as below, and ``basefill``
completes it as it completes any foreign v1 block. HedgeHog's own ``to_v1``
(``src/match/ogxm2_v1.cpp``) is the model for that, with two departures:

* Content that does not change the board -- signatures, annotations, the clock,
  the video, a game's ``termination`` -- is not carried rather than refused.
  What *would* change the board or the score and that the v1 shape cannot say
  (a beaver, a raccoon, a cube set by hand, a settlement, a starting score, a
  variant) is refused with a message for the player.

* Units. A v2 block names its currency, and HedgeHog's match analyses are
  ``cubeful_match``: every equity in them is an MWC. Each value is mapped onto
  the normalized scale through its own ply's frame (``basefill.mwc_frame``), and
  basefill's own unit inference is switched off for these blocks.

**Luck is read**, from the ``ROLL`` decisions, as it is from an ``.xg`` or a
``.bgf``. A block with no rolls is the one listed in ``_base_analyses``, which
is what sends a match to the luck pass.

Validation is the framing's: every record must end where its length says, every
varint must fit, every ``ply_ref`` must name a ply.
"""

from __future__ import annotations

import base64
import struct
import uuid
import zlib
from urllib.parse import unquote

from .basefill import (
    _checker_is_decision, _cube_ply_is_decision, _trivial_cube, complete_base_block, mwc_frame,
)
from .binary import DICE_TABLE
from .reader import (
    GvabError, _attach_blocks, _cube_action_label, _derive_ogids, _eval_from_probs,
)

HEADER_SIZE = 16
SECTION_HEADER_SIZE = 9
END_MARKER = b"END!"
READER_MAJOR = 2
READER_MINOR = 2
MAX_FILE_SIZE = 64 * 1024 * 1024

KNOWN_SECTIONS = {
    b"MTCH", b"GAME", b"ANAL", b"DECS", b"SIGN", b"CLCK", b"VIDO", b"ANNO", b"MSIG", b"CSUM",
}

ACTION_DOUBLE = 21
ACTION_TAKE = 22
ACTION_DROP = 23
ACTION_RESIGN_GAME = 27
ACTION_RESIGN_MATCH = 28
ACTION_SET_POSITION = 31
ACTION_ESCAPE = 63
UNSUPPORTED_ACTIONS = {
    32: "a beaver",
    33: "a raccoon",
    34: "a settlement",
    35: "a reserved move code",
    36: "a cube value set by hand",
    37: "a turn with no recorded roll",
}

KIND_CHECKER = 0
KIND_CUBE = 1
KIND_RESIGN = 2
KIND_ROLL = 3

CURRENCY_CUBEFUL_MONEY = 1
CURRENCY_CUBEFUL_MATCH = 2

# What v2 has no field for, carried in ANNO under the `x-` namespace the spec
# reserves for producers outside it (N6). See ogxm2_writer.py for what each
# holds; every value starts with GV_FORMAT.
GV_FORMAT = "1:"
GV_KEY_ANALYSIS = "x-gammonview-analysis/"
GV_KEY_DECISIONS = "x-gammonview-decisions/"
GV_KEY_ILLEGAL_PLY = "x-gammonview-illegal-ply"
GV_KEY_SITE = "x-gammonview-site"
GV_KEY_EVENT = "x-gammonview-event"
GV_KEY_SCORE = "x-gammonview-score"

SCOPE_MATCH = 0
SCOPE_PLY = 2
MARKER_ACTIONS = (24, 25, 26, 30)

#: verdict -> the v1 label set. "Too good" is a no-double; a beaver or raccoon
#: verdict is a take that does better than a take.
VERDICT_NAMES = {
    0: "no_double", 1: "double", 2: "take", 3: "pass", 4: "no_double", 5: "take", 6: "take",
}

PRODUCER_OGX = 0


# ---------------------------------------------------------------------------
# Byte cursor
# ---------------------------------------------------------------------------

class _Cursor:
    __slots__ = ("data", "pos", "end")

    def __init__(self, data: bytes, pos: int, end: int):
        self.data = data
        self.pos = pos
        self.end = end

    def need(self, n: int) -> None:
        if self.pos + n > self.end:
            raise GvabError(f"OGXM v2 record truncated at offset {self.pos}")

    def u8(self) -> int:
        self.need(1)
        v = self.data[self.pos]
        self.pos += 1
        return v

    def _unpack(self, fmt: str, n: int):
        self.need(n)
        v = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += n
        return v

    def u16(self) -> int:
        return self._unpack("<H", 2)

    def i32(self) -> int:
        return self._unpack("<i", 4)

    def u32(self) -> int:
        return self._unpack("<I", 4)

    def skip(self, n: int) -> None:
        self.need(n)
        self.pos += n

    def bytes(self, n: int) -> bytes:
        self.need(n)
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def _leb(self, bits: int) -> int:
        v = 0
        shift = 0
        for i in range((bits + 6) // 7):
            b = self.u8()
            v |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                if b == 0 and i > 0:
                    raise GvabError("OGXM v2 overlong varint")
                if v >> bits:
                    raise GvabError("OGXM v2 varint exceeds its width")
                return v
        raise GvabError("OGXM v2 varint too long")

    def varint(self) -> int:
        return self._leb(32)

    def varint64(self) -> int:
        return self._leb(64)

    def str(self) -> str:
        n = self.varint()
        try:
            return self.bytes(n).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GvabError("OGXM v2 string is not valid UTF-8") from exc

    def equity(self) -> float:
        return self.i32() / 1e6

    def loss(self) -> float:
        return self.u32() / 1e6

    def prob(self) -> float:
        return self.u16() / 10000

    def probs(self) -> list[float]:
        return [self.prob() for _ in range(5)]

    def board(self) -> list[int]:
        self.need(26)
        out = list(struct.unpack_from("<26b", self.data, self.pos))
        self.pos += 26
        return out

    def record(self):
        """A length-prefixed record: a cursor over its body and a bit test."""
        length = self.varint()
        start = self.pos
        if start + length > self.end:
            raise GvabError(f"OGXM v2 record at {start} claims {length} bytes past its parent")
        self.pos = start + length
        body = _Cursor(self.data, start, start + length)
        mask = body.varint64()
        return body, (lambda bit: (mask >> bit) & 1 == 1)


def _step(b: int) -> dict:
    frm = b & 0x1F
    pips = (b >> 5) & 0x07
    if frm > 25 or pips in (0, 7):
        raise GvabError(f"OGXM v2 step byte 0x{b:02x} is not a move")
    return {"from": frm, "pips": pips}


def _counted_steps(cur: _Cursor) -> list[dict]:
    return [_step(cur.u8()) for _ in range(cur.varint())]


# ---------------------------------------------------------------------------
# Nested records
# ---------------------------------------------------------------------------

def _rollout(cur: _Cursor) -> dict:
    body, has = cur.record()
    r: dict = {}
    if has(0):
        r["trials"] = body.varint()
    if has(1):
        r["truncation_depth"] = body.varint()
    if has(2):
        r["move_ply"] = body.varint()
    if has(3):
        r["variance_reduction"] = body.varint()
    if has(4):
        body.skip(8)                                   # seed: provenance only
    if has(5):
        r["budget_ms"] = body.varint()
    if has(6):
        r["match_policy"] = body.varint()
    return r


def _level(cur: _Cursor) -> dict:
    body, has = cur.record()
    lv: dict = {}
    if has(0):
        lv["preset"] = body.str()
    if has(1):
        lv["checker_ply"] = body.varint()
    if has(2):
        lv["cube_ply"] = body.varint()
    if has(3):
        lv["rollout"] = _rollout(body)
    if has(4):
        lv["no_rollout"] = True
    return lv


def _resolve(parent: dict, override: dict | None) -> dict:
    """The effective level: each field from the innermost tier carrying it."""
    if not override:
        return parent
    out = dict(parent)
    for k in ("preset", "checker_ply", "cube_ply"):
        if k in override:
            out[k] = override[k]
    if override.get("no_rollout"):
        out.pop("rollout", None)
    if "rollout" in override:
        inherited = {} if override.get("no_rollout") else (parent.get("rollout") or {})
        out["rollout"] = {**inherited, **override["rollout"]}
    return out


def _level_label(level: dict) -> str | None:
    if level.get("rollout") is not None:
        return "rollout"
    return f"{level['checker_ply']}ply" if "checker_ply" in level else None


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _decode_mtch(payload: bytes) -> dict:
    body, has = _Cursor(payload, 0, len(payload)).record()
    m: dict = {"match_length": body.varint(), "variant": body.varint()}
    if has(0):
        m["white_name"] = body.str()
    if has(1):
        m["black_name"] = body.str()
    if has(2):
        m["rules"] = body.varint()
    if has(3):
        m["cube_limit"] = body.varint()
    if has(4):
        m["score_start"] = [body.varint(), body.varint()]
    if has(5):
        m["score_final"] = [body.varint(), body.varint()]
    if has(6):
        m["result"] = body.varint()
    if has(7):
        m["source"] = body.varint()
    if has(8):
        m["started_at"] = body.varint64()
    if has(9):
        body.varint64()                                # completed_at
    if has(10):
        body.varint()                                  # player_seat
    if has(11):
        m["crawford_before_start"] = True
    if has(12):
        m["event"] = body.str()
    if has(13):
        m["event_year"] = body.varint()
    if has(14):
        body.varint()                                  # date_precision
    if has(15):
        body.str()                                     # stage
    if has(16):
        body.varint()                                  # round
    if has(17):
        body.str()                                     # table
    if has(18):
        m["city"] = body.str()
    if has(19):
        m["country"] = body.str()
    if has(20):
        body.str()                                     # event_url
    if has(21):
        m["site"] = body.str()
    # Bits 22-25 are the last fields and unread; the record length steps over them.
    return m


def _decode_ply(cur: _Cursor) -> dict:
    b0 = cur.u8()
    ply: dict = {"action": b0 & 0x3F, "seat": (b0 >> 6) & 1, "steps": []}
    if ply["action"] <= 20:
        for _ in range(DICE_TABLE[ply["action"]][2]):
            b = cur.u8()
            if b == 0:
                break                                  # zero-terminated within move_bytes
            ply["steps"].append(_step(b))
    if b0 & 0x80:
        body, has = cur.record()
        x: dict = {}
        if has(0):
            x["dice"] = [body.u8(), body.u8()]
        if has(1):
            x["resign_value"] = body.varint()
        if has(2):
            x["cube_value"] = body.varint()
        if has(3):
            x["illegal"] = True
        if has(4):
            x["settle_value"] = body.equity()
        if has(5):
            x["steps"] = _counted_steps(body)
        if has(6):
            x["board"] = body.board()
        if has(7):
            x["action_ext"] = body.varint()
        if has(8):
            x["cube_owner"] = body.varint()
        ply["extras"] = x
        if ply["action"] == ACTION_ESCAPE:
            if "action_ext" not in x:
                raise GvabError("OGXM v2 escaped action without its id")
            ply["action"] = x["action_ext"]
    return ply


def _decode_game(payload: bytes) -> dict:
    cur = _Cursor(payload, 0, len(payload))
    body, has = cur.record()
    g: dict = {}
    if has(0):
        g["winner"] = body.varint()
    if has(1):
        g["points_won"] = body.varint()
    if has(2):
        g["is_last_game"] = True
    if has(3):
        g["initial_board"] = body.board()
    if has(4):
        g["initial_cube_value"] = body.varint()
    if has(5):
        g["initial_cube_owner"] = body.varint()
    if has(6):
        g["auto_doubles"] = body.varint()
    if has(7):
        g["termination"] = body.varint()
    g["plies"] = []
    while cur.pos < cur.end:
        g["plies"].append(_decode_ply(cur))
    return g


def _decode_anal(payload: bytes) -> dict:
    body, has = _Cursor(payload, 0, len(payload)).record()
    a: dict = {"analysis_id": str(uuid.UUID(bytes=body.bytes(16)))}
    if has(0):
        body.skip(32)                                  # match_digest
    if has(1):
        a["producer"] = body.varint()
    if has(2):
        a["level"] = _level(body)
    if has(3):
        a["complete"] = True
    if has(4):
        for _ in range(body.varint()):
            body.varint()                              # coverage
    if has(5):
        a["model_id"] = body.str()
    if has(6):
        a["model_name"] = body.str()
    if has(7):
        body.skip(32)                                  # model_digest
    if has(8):
        body.str()                                     # engine_build
    if has(9):
        a["currency"] = body.varint()
    if has(10):
        body.u16()                                     # cube_efficiency
    if has(11):
        body.str()                                     # met_id
    if has(12):
        body.str()                                     # tables
    if has(13):
        body.record()                                  # dials
    if has(14):
        a["started_at"] = body.varint64()
    if has(15):
        body.varint64()
    if has(16):
        a["duration_ms"] = body.varint()
    return a


def _decode_alternative(cur: _Cursor) -> dict:
    body, has = cur.record()
    steps = [_step(body.u8()) for _ in range(body.varint())]
    alt: dict = {"steps": steps, "equity": body.equity()}
    if has(0):
        alt["probs"] = body.probs()
    if has(1):
        alt["level"] = _level(body)
    if has(2):
        alt["is_played"] = True
    return alt


def _decode_decs(payload: bytes) -> list[dict]:
    cur = _Cursor(payload, 0, len(payload))
    out = []
    while cur.pos < cur.end:
        body, has = cur.record()
        d: dict = {"ply_ref": body.varint(), "kind": body.varint()}
        kind = d["kind"]
        if kind == KIND_CHECKER:
            if has(0):
                d["alternatives"] = [_decode_alternative(body) for _ in range(body.varint())]
            if has(1):
                body.varint()                          # alternatives_total
            if has(2):
                d["best_equity"] = body.equity()
            if has(3):
                d["equity_loss"] = body.loss()
            if has(4):
                d["level"] = _level(body)
        elif kind == KIND_CUBE:
            d["verdict"] = body.varint()
            if has(0):
                d["no_double_equity"] = body.equity()
            if has(1):
                d["double_take_equity"] = body.equity()
            if has(2):
                d["double_pass_equity"] = body.equity()
            if has(3):
                d["probs"] = body.probs()
            if has(4):
                d["equity_loss"] = body.loss()
            if has(5):
                body.u16()                             # take_point
            if has(7):
                d["level"] = _level(body)
            if has(10):
                body.equity()                          # cubeful_take_value
            if has(11):
                d["currency"] = body.varint()
        elif kind == KIND_RESIGN:
            if has(0):
                body.varint()                          # correct_value
            if has(1):
                d["resign_error"] = body.equity()
            if has(2):
                d["take_resign_error"] = body.equity()
            if has(3):
                d["probs"] = body.probs()
            if has(4):
                d["equity_loss"] = body.loss()
            if has(5):
                d["level"] = _level(body)
        elif kind == KIND_ROLL:
            d["luck"] = body.equity()
            if has(0):
                d["level"] = _level(body)
        else:
            continue                                   # a kind this reader does not know
        out.append(d)
    return out


def _decode_anno(payload: bytes) -> list[dict]:
    """ANNO records, in file order (8.4). Drawings are stepped over."""
    cur = _Cursor(payload, 0, len(payload))
    out = []
    while cur.pos < cur.end:
        body, has = cur.record()
        r: dict = {"scope": body.varint(), "ref": body.varint(), "value": body.str()}
        if has(0):
            r["key"] = body.str()
        if has(1):
            r["kind"] = body.varint()
        if has(2):
            r["alt_index"] = body.varint()
        if has(3):
            body.str()                                 # lang
        if has(4):
            body.str()                                 # author
        if has(5):
            body.varint64()                            # at
        if has(6):
            for _ in range(body.varint()):
                body.record()                          # a drawing
        if has(7):
            r["analysis"] = str(uuid.UUID(bytes=body.bytes(16)))
        out.append(r)
    return out


def _gv_values(annos: list[dict], scope: int) -> dict:
    """Our ``x-gammonview`` values at ``scope``, as ``{(ref, key): value}``,
    with a value split over ``key``, ``key~1``, ... joined back and the format
    prefix checked off."""
    parts: dict = {}
    for r in annos:
        key = r.get("key") or ""
        if r["scope"] != scope or not key.startswith("x-gammonview-"):
            continue
        base, _sep, idx = key.partition("~")
        parts.setdefault((r["ref"], base), []).append((int(idx) if idx.isdigit() else 0, r["value"]))
    out = {}
    for k, chunks in parts.items():
        value = "".join(v for _i, v in sorted(chunks))
        if not value.startswith(GV_FORMAT):
            raise GvabError(f"annotation {k[1]!r} is in a format this reader does not know")
        out[k] = value[len(GV_FORMAT):]
    return out


def _verify_csum(data: bytes, payload: bytes, section_start: int) -> None:
    """Verify a CRC32 CSUM; a SHA-256 one is skipped, as the spec allows."""
    body, _has = _Cursor(payload, 0, len(payload)).record()
    algorithm = body.varint()
    digest = body.bytes(body.varint())
    if algorithm != 0:
        return
    if len(digest) != 4:
        raise GvabError("OGXM v2 CRC32 checksum is not 4 bytes")
    want = struct.unpack("<I", digest)[0]
    got = zlib.crc32(data[:section_start]) & 0xFFFFFFFF
    if want != got:
        raise GvabError(f"CSUM mismatch: stored 0x{want:x}, computed 0x{got:x}")


# ---------------------------------------------------------------------------
# The match, in v1's shape
# ---------------------------------------------------------------------------

def _unsupported(what: str) -> GvabError:
    return GvabError(f"This match uses {what}, which GammonView cannot show yet.")


def _dice_action_id(d1: int, d2: int) -> int:
    a, b = sorted((d1, d2))
    return (0, 6, 11, 15, 18, 20)[a - 1] + (b - a)


def _color(seat: int) -> int:
    """v1's colour polarity is the Seat enum's reverse: 1 is White."""
    return 1 if seat == 0 else 0


def _v1_ply(p: dict) -> dict:
    x = p.get("extras") or {}
    action = p["action"]
    if action in UNSUPPORTED_ACTIONS:
        raise _unsupported(UNSUPPORTED_ACTIONS[action])
    if action > ACTION_SET_POSITION:
        raise _unsupported(f"a move type ({action}) this reader does not know")
    if ("cube_value" in x or "settle_value" in x or "cube_owner" in x or x.get("steps")):
        raise _unsupported("a move type this reader does not know")
    ply: dict = {"color": _color(p["seat"]), "action_id": action}
    if action <= 20:
        d1, d2, _n = DICE_TABLE[action]
        ply["d1"] = d1
        ply["d2"] = d2
        ply["moves"] = p["steps"]
    elif action == ACTION_SET_POSITION:
        if "board" not in x:
            raise GvabError("OGXM v2 set-position ply without its board")
        if "dice" in x:
            ply["d1"], ply["d2"] = x["dice"]
            # A set position with dice restates a play (5.4.7). Its seat is the
            # side on roll afterwards (M2); our colour is the player who made it.
            ply["color"] = 1 - ply["color"]
        ply["set_position"] = x["board"]
    return ply


def _score_walk(mtch: dict, games: list[dict]):
    length = mtch["match_length"]
    w, b = mtch.get("score_start") or (0, 0)
    starts = []
    for g in games:
        starts.append((w, b))
        if "winner" not in g or "points_won" not in g:
            continue
        pts = g["points_won"]
        if g["winner"] == 0:
            w += min(pts, length - w) if length > 0 else pts
        elif g["winner"] == 1:
            b += min(pts, length - b) if length > 0 else pts
    return starts, (w, b)


def _crawford_games(mtch: dict, starts: list) -> list[bool]:
    """The Crawford game is derived: the first at whose start one side is 1-away."""
    out = [False] * len(starts)
    length = mtch["match_length"]
    if not length > 0 or not (mtch.get("rules", 0) & 1) or mtch.get("crawford_before_start"):
        return out
    for i, (w, b) in enumerate(starts):
        if w == length - 1 or b == length - 1:
            if max(w, b) < length:
                out[i] = True
            break
    return out


def _v1_match(mtch: dict, v2games: list[dict]):
    if mtch["variant"] != 0:
        raise _unsupported("a backgammon variant")
    start = mtch.get("score_start")
    if start and (start[0] or start[1]):
        raise _unsupported("a match that starts part-way through")
    rules = mtch.get("rules", 0)
    if rules & 0x10:
        raise _unsupported("automatic doubles")

    starts, final = _score_walk(mtch, v2games)
    crawford = _crawford_games(mtch, starts)
    white_score, black_score = mtch.get("score_final") or final
    result = mtch.get("result")
    length = mtch["match_length"]
    if result is None or result == 3:
        result = (1 if length > 0 and white_score >= length
                  else 2 if length > 0 and black_score >= length else 0)

    games = []
    offsets = []
    for gi, g in enumerate(v2games):
        if (g.get("initial_cube_value", 1) != 1 or g.get("initial_cube_owner", 2) != 2):
            raise _unsupported("a game that starts with the cube already turned")
        if g.get("auto_doubles"):
            raise _unsupported("automatic doubles")
        plies = [_v1_ply(p) for p in g["plies"]]
        # A marker has no actor (its seat is always 0); our documents stamp it
        # with the game's winner, as every converter here does.
        if g.get("winner") in (0, 1):
            for p in plies:
                if p["action_id"] in MARKER_ACTIONS:
                    p["color"] = _color(g["winner"])
        first = next((p for p in plies if p["action_id"] < 24
                      or p["action_id"] in (ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH)), None)
        # A set-up position is stated the way our own exports state one: a
        # leading set-position ply with no dice, by the side on roll.
        if "initial_board" in g:
            plies.insert(0, {
                "color": first["color"] if first else 1,
                "action_id": ACTION_SET_POSITION,
                "set_position": g["initial_board"],
            })
        offsets.append(1 if "initial_board" in g else 0)
        game = {
            "game_index": gi,
            "winner": g.get("winner", 255),
            "points_won": g.get("points_won", 0),
            "is_crawford": crawford[gi],
            "is_lastgame": bool(g.get("is_last_game")),
            "first_to_move": first["color"] if first else 0,
            "plies": plies,
        }
        if "initial_board" in g:
            game["initial_board"] = g["initial_board"]
        games.append(game)

    event = mtch.get("event") or None
    if event and mtch.get("event_year"):
        event = f"{event} {mtch['event_year']}"
    top = {
        "match_length": length,
        "player_white": mtch.get("white_name", ""),
        "player_black": mtch.get("black_name", ""),
        "white_score": white_score,
        "black_score": black_score,
        "result": result,
        "source": mtch.get("source", 0),
        "timestamp": mtch["started_at"] // 1000 if "started_at" in mtch else 0,
        "crawford": bool(rules & 0x01),
        "jacoby": bool(rules & 0x02),
        "beaver": bool(rules & 0x04),
        "raccoon": bool(rules & 0x08),
        "cube_limit": mtch.get("cube_limit", 0),
        "event": event,
        # Our `site` is where the match was played; v2 has that as `city`, and
        # its own `site` is the platform's host name, the nearest thing.
        "site": mtch.get("city") or mtch.get("site") or None,
    }
    return top, games, offsets


# ---------------------------------------------------------------------------
# Analysis, in v1's shape
# ---------------------------------------------------------------------------

def _model_id(anal: dict) -> str:
    name = anal.get("model_name")
    if name:
        return f"hedgehog/{name}" if anal.get("producer") == PRODUCER_OGX else name
    return anal.get("model_id", "")


def _r4(v: float) -> float:
    return round(v * 10000) / 10000


_IDENTITY = (_r4, _r4)


def _label(level: dict, ours: bool) -> str | None:
    """A level's display label. Our own blocks name it in ``preset``
    (``3ply``, ``truncated2``, ``rollout``); a foreign block's preset is the
    producer's own vocabulary, so its label comes from the depth instead."""
    if ours and "preset" in level:
        return level["preset"]
    return _level_label(level)


def _checker(d: dict, block_level: dict, frame, ours: bool = False) -> dict:
    to_eq, to_delta = frame
    level = _resolve(block_level, d.get("level"))
    alts = d.get("alternatives") or []
    levels = [_resolve(level, a.get("level")) for a in alts]
    split = ours or any(_level_label(lv) != _level_label(levels[0]) for lv in levels)

    best_mwc = d["best_equity"] if "best_equity" in d else (alts[0]["equity"] if alts else 0.0)
    played = next((a for a in alts if a.get("is_played")), None)
    # A derived loss (7.1) is the difference of the stored values, taken before
    # either is rounded to the four places a document holds.
    loss_mwc = (d["equity_loss"] if "equity_loss" in d
                else (best_mwc - played["equity"] if played else 0.0))

    best = to_eq(best_mwc)
    loss = max(0.0, to_delta(loss_mwc))
    out_alts = []
    for a, lv in zip(alts, levels):
        o: dict = {"move": a["steps"], "equity": to_eq(a["equity"]),
                   "is_played": bool(a.get("is_played"))}
        if "probs" in a:
            o["eval"] = _eval_from_probs(*a["probs"])
        if split:
            label = _label(lv, ours)
            if label is not None:
                o["eval_level"] = label
        out_alts.append(o)
    analysis: dict = {
        "best_equity": best,
        "played_equity": _r4(best - loss),
        "equity_loss": loss,
        "decision": False,
        "alternatives": out_alts,
    }
    if out_alts and "eval" in out_alts[0]:
        analysis["eval"] = dict(out_alts[0]["eval"])
    if "checker_ply" in level and level["checker_ply"] != block_level.get("checker_ply"):
        analysis["ply"] = level["checker_ply"]
    return analysis


def _cube_triple(d: dict, frame) -> dict:
    to_eq = frame[0]
    return {
        "no_double_equity": to_eq(d.get("no_double_equity", 0.0)),
        "double_take_equity": to_eq(d.get("double_take_equity", 0.0)),
        "double_pass_equity": to_eq(d.get("double_pass_equity", 0.0)),
    }


def _live_cube(d: dict, frame, analysis: dict, label: str | None = None) -> None:
    triple = _cube_triple(d, frame)
    eval_obj = _eval_from_probs(*d["probs"]) if "probs" in d else None
    should_double = d["verdict"] == 1
    live: dict = {
        "should_double": should_double,
        **triple,
        "action": _cube_action_label(should_double, triple["double_take_equity"],
                                     triple["double_pass_equity"]),
    }
    if eval_obj is not None:
        live["eval"] = eval_obj
    if label is not None:
        live["eval_level"] = label
    if "equity_loss" in d:
        missed: dict = {
            **triple,
            "equity_loss": max(0.0, frame[1](d["equity_loss"])),
            "correct_action": VERDICT_NAMES.get(d["verdict"], "double"),
        }
        if eval_obj is not None:
            missed["eval"] = dict(eval_obj)
        if label is not None:
            missed["eval_level"] = label
        analysis["missed_double"] = missed
    else:
        live["decision"] = False
    analysis["cube_decision"] = live


def _cube_ply(d: dict, action: int, block_level: dict, frame, ours: bool = False) -> dict:
    level = _resolve(block_level, d.get("level"))
    analysis: dict = {
        "correct_action": VERDICT_NAMES.get(d["verdict"], "no_double"),
        "played_action": ("double" if action == ACTION_DOUBLE
                          else "take" if action == ACTION_TAKE else "pass"),
        **_cube_triple(d, frame),
        "equity_loss": max(0.0, frame[1](d.get("equity_loss", 0.0))),
        "decision": False,
    }
    if "probs" in d:
        analysis["eval"] = _eval_from_probs(*d["probs"])
    if ours and "preset" in level:
        analysis["eval_level"] = level["preset"]
    if "cube_ply" in level and level["cube_ply"] != block_level.get("cube_ply"):
        analysis["ply"] = level["cube_ply"]
    return analysis


def _resign(d: dict, frame) -> dict:
    to_delta = frame[1]
    analysis: dict = {
        "resign_error": to_delta(d.get("resign_error", 0.0)),
        "take_resign_error": to_delta(d.get("take_resign_error", 0.0)),
        "equity_loss": max(0.0, to_delta(d.get("equity_loss", 0.0))),
        "decision": False,
    }
    if "probs" in d:
        analysis["eval"] = _eval_from_probs(*d["probs"])
    return analysis


def _plays_dice(ply: dict) -> bool:
    """A dice action, or an illegal play restated as a set position."""
    action = ply["action_id"]
    return action <= 20 or (action == ACTION_SET_POSITION and bool(ply.get("d1")))


def default_flags(ply: dict, analysis: dict, illegal_play: bool) -> None:
    """Set the flags v2 has no field for to what a block of ours holds unless it
    says otherwise (``x-gammonview-analysis``): ``illegal_move`` on a checker
    analysis of an illegal play, the PR-counting ``decision`` by the rule
    ``basefill`` applies to a foreign block, and a live cube's own
    ``decision`` by triviality. Shared with the writer, which records only
    where its document disagrees."""
    action = ply["action_id"]
    if illegal_play and "alternatives" in analysis:
        analysis["illegal_move"] = True
    if _plays_dice(ply):
        analysis["decision"] = ("alternatives" in analysis and _checker_is_decision(analysis))
    elif action in (ACTION_DOUBLE, ACTION_TAKE, ACTION_DROP):
        analysis["decision"] = _cube_ply_is_decision(ply, analysis)
    elif action in (ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH):
        analysis["decision"] = True
    live = analysis.get("cube_decision")
    if isinstance(live, dict) and "missed_double" not in analysis:
        live["decision"] = not _trivial_cube(
            live["no_double_equity"], live["double_take_equity"], live["double_pass_equity"])


def _apply_exceptions(analysis: dict, letters: set) -> None:
    if "c" in letters:
        analysis["decision"] = not analysis.get("decision")
    if "l" in letters and isinstance(analysis.get("cube_decision"), dict):
        live = analysis["cube_decision"]
        live["decision"] = not live.get("decision")
    if "i" in letters:
        if analysis.get("illegal_move"):
            del analysis["illegal_move"]
        else:
            analysis["illegal_move"] = True


def _v1_block(anal: dict, decisions: list[dict], ply_at: list, match_length: int,
              ours: dict | None = None):
    """One v2 analysis block in v1's shape. ``ours`` is set for a block our
    writer made: ``{"extra": {ply_ref: [records]}, "exceptions": {tokens},
    "illegal": {ply_refs}}`` -- the annotation's decision records, which stand
    in for ``DECS``'s at their ply and kind, and the flags to flip."""
    block_level = _resolve({}, anal.get("level"))
    block_mwc = match_length > 0 and anal.get("currency") == CURRENCY_CUBEFUL_MATCH
    block_obj: dict = {}
    luck_levels: set = set()
    mine = ours is not None

    if mine and ours["extra"]:
        replaced = {(ref, d["kind"]) for ref, recs in ours["extra"].items() for d in recs}
        decisions = sorted(
            [d for d in decisions if (d["ply_ref"], d["kind"]) not in replaced]
            + [d for recs in ours["extra"].values() for d in recs],
            key=lambda d: (d["ply_ref"], d["kind"]))

    for d in decisions:
        if d["ply_ref"] >= len(ply_at):
            raise GvabError(
                f"OGXM v2 decision names ply {d['ply_ref']}, past the end of the match")
        key, ply = ply_at[d["ply_ref"]]
        kind = d["kind"]
        currency = d.get("currency", anal.get("currency"))
        mwc = (match_length > 0 and currency == CURRENCY_CUBEFUL_MATCH
               if kind == KIND_CUBE else block_mwc)
        # A ply with no frame cannot be converted; its decision is dropped
        # rather than shown in the wrong unit.
        frame = mwc_frame(ply) if mwc else _IDENTITY
        if frame is None:
            continue

        action = ply["action_id"]
        dice = _plays_dice(ply)
        if kind == KIND_CHECKER and dice:
            prior = block_obj.get(key) or {}
            block_obj[key] = {**_checker(d, block_level, frame, mine), **prior}
        elif kind == KIND_CUBE and dice:
            label = _label(_resolve(block_level, d.get("level")), True) if mine else None
            _live_cube(d, frame, block_obj.setdefault(key, {}), label)
        elif kind == KIND_CUBE and action in (ACTION_DOUBLE, ACTION_TAKE, ACTION_DROP):
            block_obj[key] = _cube_ply(d, action, block_level, frame, mine)
        elif kind == KIND_ROLL and dice:
            # From the roller's side, in the block's currency -- a difference
            # of two MWCs, so it converts by the frame's slope alone.
            block_obj.setdefault(key, {"decision": False})["luck"] = frame[1](d["luck"])
            luck_levels.add(_label(_resolve(block_level, d.get("level")), mine))
        elif kind == KIND_RESIGN and action in (ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH):
            block_obj[key] = _resign(d, frame)

    if mine:
        ref_of = {k: i for i, (k, _p) in enumerate(ply_at)}
        flips: dict = {}
        for token in ours["exceptions"]:
            flips.setdefault(int(token[:-1]), set()).add(token[-1])
        for key, obj in block_obj.items():
            ref = ref_of[key]
            default_flags(ply_at[ref][1], obj, ref in ours["illegal"])
            _apply_exceptions(obj, flips.get(ref, set()))

    info: dict = {"ply": block_level.get("checker_ply") or 0}
    if mine:
        # The block's own label travels in its annotation when it is not the
        # level most decisions share, which is what v2's block level states.
        label = ours["level"] if "level" in ours else block_level.get("preset")
        if label is not None:
            info["eval_level"] = label
        if luck_levels and ours.get("luck"):
            luck_levels = {ours["luck"]}
    elif block_level.get("rollout") is not None:
        info["eval_level"] = "rollout"
    info["model_id"] = _model_id(anal)
    info["timestamp"] = anal["started_at"] // 1000 if "started_at" in anal else 0
    if anal.get("duration_ms"):
        info["duration_ms"] = anal["duration_ms"]
    if len(luck_levels) == 1:
        label = next(iter(luck_levels))
        if label is not None:
            info["luck_eval_level"] = label
    if mine:
        info["analysis_id"] = anal["analysis_id"]
    return info, block_obj, bool(luck_levels)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _walk_sections(data: bytes, file_size: int) -> list[tuple[bytes, int, bytes]]:
    sections = []
    pos = HEADER_SIZE
    end = file_size - len(END_MARKER)
    while pos < end:
        if pos + SECTION_HEADER_SIZE > end:
            raise GvabError(f"OGXM v2 section header truncated at {pos}")
        stype = data[pos:pos + 4]
        length, flags = struct.unpack_from("<IB", data, pos + 4)
        start = pos + SECTION_HEADER_SIZE
        if start + length > end:
            raise GvabError(f"OGXM v2 section {stype!r} at {pos} runs past the end")
        if stype not in KNOWN_SECTIONS and flags & 1:
            raise GvabError(
                f"unknown critical OGXM v2 section {stype!r} -- this reader cannot "
                "safely read the file")
        sections.append((stype, pos, data[start:start + length]))
        pos = start + length
    return sections


def read_ogxm2(data: bytes, *, verify_crc: bool = True, derive_ogids: bool = True) -> dict:
    """Parse an OGXM v2 file into the document ``read_gvab`` returns for v1.
    Called by ``read_gvab``, which owns the GvabError contract."""
    if len(data) < HEADER_SIZE + len(END_MARKER):
        raise GvabError("input shorter than an OGXM v2 header")
    rmaj, rmin, file_size = struct.unpack_from("<HHI", data, 8)
    if rmaj > READER_MAJOR or (rmaj == READER_MAJOR and rmin > READER_MINOR):
        raise GvabError(
            f"file requires an OGXM {rmaj}.{rmin} reader; this one implements "
            f"{READER_MAJOR}.{READER_MINOR}")
    if file_size > MAX_FILE_SIZE or file_size != len(data):
        raise GvabError(f"file_size header ({file_size}) != actual length ({len(data)})")
    if data[file_size - 4:file_size] != END_MARKER:
        raise GvabError("bad OGXM v2 end marker")

    sections = _walk_sections(data, file_size)
    # The checksum first, so a damaged file says so rather than failing
    # wherever the damage happens to trip the decoder.
    if verify_crc:
        for stype, start, payload in sections:
            if stype == b"CSUM":
                _verify_csum(data, payload, start)

    mtch = None
    v2games: list[dict] = []
    blocks: list[dict] = []
    annos: list[dict] = []
    for stype, _start, payload in sections:
        if stype == b"MTCH":
            mtch = _decode_mtch(payload)
        elif stype == b"GAME":
            v2games.append(_decode_game(payload))
        elif stype == b"ANAL":
            blocks.append({"anal": _decode_anal(payload), "decisions": []})
        elif stype == b"DECS":
            if not blocks:
                raise GvabError("OGXM v2 DECS with no ANAL before it")
            blocks[-1]["decisions"] = _decode_decs(payload)
        elif stype == b"ANNO":
            annos = _decode_anno(payload)
    if mtch is None:
        raise GvabError(
            "This file holds an analysis without its match, so there is nothing to show.")

    ogxm, games, offsets = _v1_match(mtch, v2games)
    gv_match = _gv_values(annos, SCOPE_MATCH)
    gv_ply = _gv_values(annos, SCOPE_PLY)
    if (0, GV_KEY_SITE) in gv_match:
        ogxm["site"] = gv_match[(0, GV_KEY_SITE)]
    if (0, GV_KEY_EVENT) in gv_match:
        ogxm["event"] = gv_match[(0, GV_KEY_EVENT)]
    if (0, GV_KEY_SCORE) in gv_match:
        try:
            ogxm["white_score"], ogxm["black_score"] = (
                int(v) for v in gv_match[(0, GV_KEY_SCORE)].split(","))
        except ValueError as exc:
            raise GvabError("the stated-score annotation is malformed") from exc

    # ply_ref is the ply's ordinal across the whole match, in v2's plies --
    # which a synthetic set-position ply is not one of.
    ply_at: list = []
    ply_by_key: dict = {}
    illegal_refs: set = set()
    for gi, g in enumerate(games):
        for pi, ply in enumerate(g["plies"]):
            if pi >= offsets[gi]:
                v2ply = v2games[gi]["plies"][pi - offsets[gi]]
                if (v2ply.get("extras") or {}).get("illegal"):
                    illegal_refs.add(len(ply_at))
                ply_at.append(((gi, pi), ply))
            ply_by_key[(gi, pi)] = ply

    # An illegal play our document held as a dice ply: v2 states it as the
    # board it produced, and the annotation keeps the steps that produced it.
    for (ref, key), value in gv_ply.items():
        if key != GV_KEY_ILLEGAL_PLY:
            continue
        if ref not in illegal_refs:
            raise GvabError(f"ply {ref} carries an illegal play's steps but is not one")
        ply = ply_at[ref][1]
        action = _dice_action_id(ply["d1"], ply["d2"])
        steps = [s.split("/") for s in value.split(",")] if value else []
        restored = {"color": ply["color"], "action_id": action,
                    "d1": DICE_TABLE[action][0], "d2": DICE_TABLE[action][1],
                    "moves": [{"from": int(f), "pips": int(n)} for f, n in steps]}
        ply.clear()
        ply.update(restored)

    if derive_ogids:
        _derive_ogids({"match_length": ogxm["match_length"], "games": games})

    decoded = []
    base_blocks = []
    for blk in blocks:
        aid = blk["anal"]["analysis_id"]
        ours = None
        if (0, GV_KEY_ANALYSIS + aid) in gv_match:
            items = dict(item.partition("=")[::2]
                         for item in gv_match[(0, GV_KEY_ANALYSIS + aid)].split(";") if item)
            extra = {}
            for (ref, key), value in gv_ply.items():
                if key == GV_KEY_DECISIONS + aid:
                    try:
                        raw = base64.b64decode(value, validate=True)
                    except ValueError as exc:
                        raise GvabError(f"annotation on ply {ref} is not base64") from exc
                    recs = _decode_decs(raw)
                    if any(d["ply_ref"] != ref for d in recs):
                        raise GvabError(f"annotation on ply {ref} holds another ply's decision")
                    extra[ref] = recs
            ours = {"extra": extra, "illegal": illegal_refs,
                    "exceptions": {t for t in items.get("pr", "").split(",") if t}}
            if "level" in items:
                ours["level"] = unquote(items["level"]) or None
            if "luck" in items:
                ours["luck"] = unquote(items["luck"])
        info, block_obj, has_luck = _v1_block(
            blk["anal"], blk["decisions"], ply_at, ogxm["match_length"], ours)
        if block_obj and ours is None:
            if derive_ogids:
                complete_base_block(block_obj, ply_by_key, info, convert_units=False)
            if not has_luck:
                base_blocks.append(len(decoded))
        decoded.append((info, block_obj))

    _attach_blocks(ogxm, games, decoded, ply_by_key)
    ogxm["games"] = games
    if base_blocks:
        ogxm["_base_analyses"] = base_blocks
    return ogxm
