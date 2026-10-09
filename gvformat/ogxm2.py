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
are two versions. ``ogxm2_writer`` is the other half, and since 2.0.0 what
``write_gvab`` writes; ``docs/OGXM_V2_PROFILE.md`` is the profile the two
share.

**Two kinds of block.** A block our writer made is marked by its
``x-gammonview-analysis`` annotation and read exactly: the ``x-`` annotations
put back what v2 has no field for (an illegal play's analysis and steps, the
PR-counting flags, level labels, ``site``), and the reader derives nothing it
was told. Any other producer's block is read as below, and ``basefill``
completes it as it completes any foreign v1 block. HedgeHog's own ``to_v1``
(``src/match/ogxm2_v1.cpp``) is the model for that, with two departures:

* Signatures, unknown sections and fields, and annotations that address nothing
  the document holds are not carried by the document (they travel in
  ``_ogxm2_passthrough``, ``ogxm2_passthrough``). The clock, the video and every
  other annotation are: ``clock_info`` / ``timestamp_ms``, ``video_info`` / ``video_ms``,
  ``annotations`` on the match, a game, a ply, a decision's analysis object and
  an alternative. Everything that does change the
  board or the score is: a starting score, a variant, a cube that a game opens
  with or one set by hand, automatic doubles, a beaver, a raccoon, a settlement.
  The match context (event year, stage, city, platform, ...), the two player
  profiles and each game's termination are document keys too; a value v2 cannot
  hold travels in an ``x-gammonview-<field>`` annotation and is put back.

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
import copy
import json
import re
import struct
import uuid
import zlib
from urllib.parse import unquote

from .basefill import (
    _checker_is_decision, _cube_ply_is_decision, _trivial_cube, complete_base_block,
    frame_perspective_is_white, mwc_frame,
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
ACTION_BEAVER = 32
ACTION_RACCOON = 33
ACTION_SETTLE = 34
ACTION_RESERVED = 35
ACTION_CUBE_SET = 36
ACTION_PASS = 37
ACTION_ESCAPE = 63
#: The last action id this reader assigns a meaning to; above it a ply is kept
#: whole (``extras_raw``), since only the producer knows what it says (P1, P2).
LAST_KNOWN_ACTION = ACTION_PASS

#: The plies a cube record can sit on besides a dice action (7.4), and what each
#: is called in a document's `played_action`.
CUBE_ACTIONS = (ACTION_DOUBLE, ACTION_TAKE, ACTION_DROP, ACTION_BEAVER, ACTION_RACCOON)
PLAYED_ACTION = {ACTION_DOUBLE: "double", ACTION_TAKE: "take", ACTION_DROP: "pass",
                 ACTION_BEAVER: "beaver", ACTION_RACCOON: "raccoon"}

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
#: Annotations of the document that v2's ``ANNO`` cannot address or hold (a
#: decision the ``DECS`` stream does not carry, a value past a cap): a JSON
#: list, base64, chunked (profile section 4).
GV_KEY_ANNOTATIONS = "x-gammonview-annotations"
#: A video URL v2 cannot store (8.3: it is dropped there).
GV_KEY_VIDEO_URL = "x-gammonview-video.url"
GV_PREFIX = "x-gammonview-"

#: Every name v2 gives a field (spec 8.4.2): an ``ANNO`` key may not be one. It
#: is the reference codec's list, taken from its schema.
V2_FIELD_NAMES = frozenset((
    "action action_ext algorithm alt_index alternatives alternatives_total analysis analysis_id "
    "at author auto_doubles best_equity black_name black_profile board budget_ms checker_ply city "
    "color complete completed_at correct_value country coverage covers crawford_before_start "
    "cube_efficiency cube_limit cube_limit_mode cube_limit_resolved cube_owner cube_ply cube_rule "
    "cube_value cubeful_take_value cubeless_equity currency date_precision deep_searched dials dice "
    "digest double_pass_equity double_take_equity drawings duration_ms engine_build equity "
    "equity_loss event event_url event_year exact_bearoff illegal initial_board initial_cube_owner "
    "initial_cube_value is_free_cube is_last_game is_optional is_played jacoby_mode jacoby_resolved "
    "key key_id kind lang level luck match_digest match_length match_policy match_ref met_id "
    "model_digest model_id model_name move_ply no_double_equity no_rollout player_seat ply_ref "
    "points_won position_tags preset probs producer producer_ref public_key race_order rated "
    "rating rating_system ref resign_error resign_value result rollout rollout_budget_on "
    "rollout_se rollouts_done round rules scope score_final score_start seat seed settle_value "
    "shape signature signed_at site source source_band sources stage started_at steps table "
    "tables take_point take_resign_error termination to top_deep top_deep_accept top_deep_keep "
    "top_deep_threshold trials truncation_depth user_id value variance_reduction variant verdict "
    "white_name white_profile window_searched winner").split())

#: Match fields a value v2 cannot hold travels under ``x-gammonview-<field>``
#: at match scope (``x-gammonview-<side>_profile.<field>`` for a player). Each
#: maps to how its text spells it: ``s`` as is, ``i`` an integer, ``f`` a number.
GV_MATCH_FIELDS = {
    "stage": "s", "round": "i", "table": "s", "city": "s", "country": "s", "event_url": "s",
    "platform": "s", "match_ref": "s", "event_year": "i", "date_precision": "i",
    "player_seat": "i", "rules_other": "i",
}
GV_PROFILE_FIELDS = {"user_id": "s", "rating": "f", "rating_system": "s", "country": "s",
                     "kind": "i"}
#: The same for a game, at game scope.
GV_GAME_FIELDS = {"initial_cube_value": "i", "initial_cube_owner": "i", "auto_doubles": "i",
                  "termination": "i"}

SCOPE_MATCH = 0
SCOPE_GAME = 1
SCOPE_PLY = 2
SCOPE_DECISION = 3
SCOPE_ALTERNATIVE = 4
MARKER_ACTIONS = (24, 25, 26, 30)

#: verdict -> the document's label, one for one (writer: ``_VERDICT``). The
#: three beyond the v1 set keep their names: a reader that wants the plain
#: no-double or take they refine can fold them itself.
VERDICT_NAMES = {
    0: "no_double", 1: "double", 2: "take", 3: "pass", 4: "too_good", 5: "beaver", 6: "raccoon",
}

PRODUCER_OGX = 0

#: ``dials`` (6.3): bit -> name, in bit order; ``DIAL_FLAGS`` hold no payload,
#: ``top_deep_threshold`` is an equity loss, the rest are varints.
DIAL_FLAGS_AND_VARINTS = (
    (0, "jacoby_resolved"), (1, "jacoby_mode"), (2, "cube_limit_resolved"),
    (3, "cube_limit_mode"), (4, "exact_bearoff"), (5, "race_order"), (6, "top_deep"),
    (7, "top_deep_threshold"), (8, "rollout_budget_on"), (9, "cube_rule"),
    (10, "top_deep_keep"), (11, "top_deep_accept"),
)
DIAL_FLAGS = ("jacoby_resolved", "exact_bearoff", "race_order", "top_deep", "rollout_budget_on")


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

    def u64(self) -> int:
        return self._unpack("<Q", 8)

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
        r["seed"] = str(body.u64())                    # a decimal string, as the reference's JSON has it (J6)
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


_PLY_LABEL = re.compile(r"(\d+)ply")


_DEPTH_PRESET = re.compile(r"\d+ply")


def tier_level(parent: dict, preset=None, checker_ply=None, cube_ply=None, follow: bool = True) -> dict:
    """The level a tier has when its labels (``eval_level``, ``ply``) are all
    that says so: the tier above, with each label that is set laid over it.
    Shared by the reader, which keeps a ``level`` dict only where the true level
    is something else, and the writer, which derives the level from the labels
    when the document keeps none.

    A preset that is only a depth (``2ply``) follows a depth the labels change:
    a tier at ``ply`` 3 under a ``2ply`` block is ``3ply``, not a 3-ply search
    called ``2ply``. ``follow=False`` leaves it, which is how a luck record states
    its depth (``luck_labels``)."""
    out = dict(parent)
    for key, v in (("preset", preset), ("checker_ply", checker_ply), ("cube_ply", cube_ply)):
        if v:
            out[key] = v
    if follow and not preset and _DEPTH_PRESET.fullmatch(out.get("preset") or ""):
        depth = checker_ply or cube_ply
        if depth:
            out["preset"] = f"{depth}ply"
    return out


def common_label(objs) -> str | None:
    """The level most of a block's decisions were judged at -- what v2 means by
    a block's level (6.4), and what lets most of them state none of their own."""
    counts: dict = {}
    for a in objs:
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


def block_level_of(info: dict, objs) -> dict:
    """The block level a block's labels give: the label most decisions share,
    and the base depth. A block whose level is more than that keeps it as
    ``level``."""
    return tier_level({}, preset=common_label(objs) or info.get("eval_level"),
                       checker_ply=int(info["ply"]) if info.get("ply") else None)


def luck_labels(label: str | None) -> dict:
    """What ``luck_eval_level`` says of a luck record's level. A depth is stated
    as the depth (a reader outside GammonView can use it, and it costs a byte
    where a label costs five); anything else as a preset."""
    m = _PLY_LABEL.fullmatch(label or "")
    return {"checker_ply": int(m.group(1))} if m else {"preset": label}


def luck_level_of(block_level: dict, label: str | None) -> dict:
    """The level a luck record has when ``luck_eval_level`` is all that says so."""
    return tier_level(block_level, follow=False, **luck_labels(label))


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _decode_profile(cur: _Cursor) -> dict:
    """A ``player`` record (4.1)."""
    body, has = cur.record()
    pr: dict = {}
    if has(0):
        pr["user_id"] = body.str()
    if has(1):
        pr["rating"] = body.varint() / 100
    if has(2):
        pr["rating_system"] = body.str()
    if has(3):
        pr["country"] = body.str()
    if has(4):
        pr["kind"] = body.varint()
    return pr


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
        m["completed_at"] = body.varint64()
    if has(10):
        m["player_seat"] = body.varint()
    if has(11):
        m["crawford_before_start"] = True
    if has(12):
        m["event"] = body.str()
    if has(13):
        m["event_year"] = body.varint()
    if has(14):
        m["date_precision"] = body.varint()
    if has(15):
        m["stage"] = body.str()
    if has(16):
        m["round"] = body.varint()
    if has(17):
        m["table"] = body.str()
    if has(18):
        m["city"] = body.str()
    if has(19):
        m["country"] = body.str()
    if has(20):
        m["event_url"] = body.str()
    if has(21):
        m["site"] = body.str()
    if has(22):
        m["match_ref"] = body.str()
    if has(23):
        m["white_profile"] = _decode_profile(body)
    if has(24):
        m["black_profile"] = _decode_profile(body)
    if has(25):
        m["rated"] = True
    # Any later bits are the unknown run; the record length steps over them.
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
        first = cur.pos
        body, has = cur.record()
        x: dict = {}
        # An action id nothing here assigns (38-62) carries a payload only its
        # producer can read, so none of the fields below is its; an escape
        # (63) has to be read as far as its real id.
        known = ply["action"] <= LAST_KNOWN_ACTION or ply["action"] == ACTION_ESCAPE
        if known and has(0):
            x["dice"] = [body.u8(), body.u8()]
        if known and has(1):
            x["resign_value"] = body.varint()
        if known and has(2):
            x["cube_value"] = body.varint()
        if known and has(3):
            x["illegal"] = True
        if known and has(4):
            x["settle_value"] = body.equity()
        if known and has(5):
            x["steps"] = _counted_steps(body)
        if known and has(6):
            x["board"] = body.board()
        if known and has(7):
            x["action_ext"] = body.varint()
        if known and has(8):
            x["cube_owner"] = body.varint()
        ply["extras"] = x
        ply["extras_raw"] = cur.data[first:cur.pos]      # the record, length prefix and all
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


def _decode_dials(cur: _Cursor) -> dict:
    """A ``dials`` record (6.3): flags as ``True``, the threshold a number."""
    body, has = cur.record()
    d: dict = {}
    for bit, name in DIAL_FLAGS_AND_VARINTS:
        if not has(bit):
            continue
        if name in DIAL_FLAGS:
            d[name] = True
        elif name == "top_deep_threshold":
            d[name] = body.loss()
        else:
            d[name] = body.varint()
    return d


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
        refs: list = []
        for i in range(body.varint()):
            refs.append(body.varint() + (refs[-1] + 1 if refs else 0))
        a["coverage"] = refs
    if has(5):
        a["model_id"] = body.str()
    if has(6):
        a["model_name"] = body.str()
    if has(7):
        a["model_digest"] = body.bytes(32).hex()
    if has(8):
        a["engine_build"] = body.str()
    if has(9):
        a["currency"] = body.varint()
    if has(10):
        a["cube_efficiency"] = body.prob()
    if has(11):
        a["met_id"] = body.str()
    if has(12):
        a["tables"] = body.str()
    if has(13):
        a["dials"] = _decode_dials(body)
    if has(14):
        a["started_at"] = body.varint64()
    if has(15):
        a["completed_at"] = body.varint64()
    if has(16):
        a["duration_ms"] = body.varint()
    if has(17):
        a["sources"] = [str(uuid.UUID(bytes=body.bytes(16))) for _ in range(body.varint())]
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
    if has(3):
        alt["rollout_se"] = body.equity()
    if has(4):
        alt["cubeless_equity"] = body.equity()
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
                d["alternatives_total"] = body.varint()
            if has(2):
                d["best_equity"] = body.equity()
            if has(3):
                d["equity_loss"] = body.loss()
            if has(4):
                d["level"] = _level(body)
            if has(5):
                d["rollouts_done"] = body.varint()
            if has(6):
                d["deep_searched"] = body.varint()
            if has(7):
                d["position_tags"] = body.varint()
            if has(8):
                d["producer_ref"] = body.varint()
            if has(9):
                d["source_band"] = body.varint()
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
                d["take_point"] = body.prob()
            if has(6):
                d["window_searched"] = True
            if has(7):
                d["level"] = _level(body)
            if has(8):
                d["is_optional"] = True
            if has(9):
                d["is_free_cube"] = True
            if has(10):
                d["cubeful_take_value"] = body.equity()
            if has(11):
                d["currency"] = body.varint()
            if has(12):
                d["producer_ref"] = body.varint()
        elif kind == KIND_RESIGN:
            if has(0):
                d["correct_value"] = body.varint()
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
            if has(6):
                d["producer_ref"] = body.varint()
        elif kind == KIND_ROLL:
            d["luck"] = body.equity()
            if has(0):
                d["level"] = _level(body)
            if has(1):
                d["producer_ref"] = body.varint()
        else:
            continue                                   # a kind this reader does not know
        out.append(d)
    return out


def uuid_of(anal: bytes) -> str:
    """The ``analysis_id`` of an ``ANAL`` payload, hyphenated."""
    cur = _Cursor(anal, 0, len(anal))
    length = cur.varint()
    body = _Cursor(anal, cur.pos, cur.pos + length)
    body.varint64()
    return str(uuid.UUID(bytes=body.bytes(16)))


def _decode_drawing(cur: _Cursor) -> dict:
    """A drawing (8.4.3). A shape or colour nothing here knows is kept as is."""
    body, has = cur.record()
    d: dict = {"shape": body.varint(), "at": body.u8()}
    if has(0):
        d["to"] = body.u8()
    if has(1):
        d["color"] = body.varint()
    return d


def _decode_anno(payload: bytes) -> list[dict]:
    """ANNO records, in file order (8.4). Each carries its own bytes as
    ``raw``, for ``ogxm2_passthrough``."""
    cur = _Cursor(payload, 0, len(payload))
    out = []
    while cur.pos < cur.end:
        first = cur.pos
        body, has = cur.record()
        r: dict = {"scope": body.varint(), "ref": body.varint(), "value": body.str(),
                   "raw": payload[first:cur.pos]}
        if has(0):
            r["key"] = body.str()
        if has(1):
            r["kind"] = body.varint()
        if has(2):
            r["alt_index"] = body.varint()
        if has(3):
            r["lang"] = body.str()
        if has(4):
            r["author"] = body.str()
        if has(5):
            r["at"] = body.varint64()
        if has(6):
            r["drawings"] = [_decode_drawing(body) for _ in range(body.varint())]
        if has(7):
            r["analysis"] = str(uuid.UUID(bytes=body.bytes(16)))
        out.append(r)
    return out


def anno_doc(r: dict) -> dict:
    """An annotation as the document holds it: ``value``, then whichever of
    ``key``, ``lang``, ``author``, ``at`` and ``drawings`` it states. An empty
    string is no string, and no drawings are none."""
    out: dict = {"value": r["value"]}
    for k in ("key", "lang", "author"):
        if r.get(k):
            out[k] = r[k]
    if r.get("at") is not None:
        out["at"] = r["at"]
    if r.get("drawings"):
        out["drawings"] = [dict(d) for d in r["drawings"]]
    return out


def anno_is_ours(r: dict) -> bool:
    """Whether ``read_ogxm2`` consumes this annotation into document keys it
    has (and so regenerates it on write), rather than into ``annotations``."""
    base = (r.get("key") or "").partition("~")[0]
    name = base[len(GV_PREFIX):] if base.startswith(GV_PREFIX) else None
    if r["scope"] == SCOPE_MATCH:
        side, _dot, field = (name or "").partition(".")
        return (base in (GV_KEY_SITE, GV_KEY_EVENT, GV_KEY_SCORE, GV_KEY_ANNOTATIONS,
                         GV_KEY_VIDEO_URL)
                or base.startswith(GV_KEY_ANALYSIS) or name in GV_MATCH_FIELDS
                or (side in ("white_profile", "black_profile") and field in GV_PROFILE_FIELDS))
    if r["scope"] == SCOPE_GAME:
        return name in GV_GAME_FIELDS
    if r["scope"] == SCOPE_PLY:
        return base == GV_KEY_ILLEGAL_PLY or base.startswith(GV_KEY_DECISIONS)
    return False


def natural_kind(ply: dict):
    """The decision kind an analysis object of this ply is, by its action: a
    dice play is a checker decision, a cube action a cube decision, a
    resignation a resign decision; nothing else is analysed."""
    action = ply["action_id"]
    if _plays_dice(ply):
        return KIND_CHECKER
    if action in CUBE_ACTIONS:
        return KIND_CUBE
    if action in (ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH):
        return KIND_RESIGN
    return None


def decision_holder(ply: dict, obj: dict, kind):
    """``(dict, explicit)``: the part of an analysis object that stands for the
    decision ``kind`` of ``ply``, or None where the object holds no such
    decision. The object itself is the ply's natural decision; a cube decision
    on a dice play is its ``missed_double`` (else ``cube_decision``); a roll is
    the object too, and then ``explicit`` says its records must name ``kind``."""
    nat = natural_kind(ply)
    if kind == KIND_ROLL:
        return (obj, True) if nat == KIND_CHECKER and "luck" in obj else None
    if kind == nat:
        if kind == KIND_CHECKER and "alternatives" not in obj:
            return None
        return obj, False
    if kind == KIND_CUBE and nat == KIND_CHECKER:
        for name in ("missed_double", "cube_decision"):
            if isinstance(obj.get(name), dict):
                return obj[name], False
    return None


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


def _alt_map(main_alts: list, exact_alts: list) -> list:
    """Where each alternative of the ``DECS`` record sits in the exact list an
    annotation of ours stands in for it: the same alternative, by content (two
    that are identical are interchangeable)."""
    used: set = set()
    out: list = []
    for m in main_alts:
        for i, e in enumerate(exact_alts):
            if i not in used and e == m:
                used.add(i)
                out.append(i)
                break
        else:
            out.append(None)
    return out


def _holder_of(ply: dict, obj, kind, alt, amap):
    """``(dict, extra)`` an annotation addressed to ``kind`` (and, where given,
    alternative ``alt`` of the record) belongs on, or None: ``extra`` is what
    the document's record must state besides the annotation itself."""
    if not isinstance(obj, dict):
        return None
    got = decision_holder(ply, obj, kind)
    if got is None:
        return None
    holder, explicit = got
    extra = {"kind": kind} if explicit else {}
    if alt is None:
        return holder, extra
    if kind != KIND_CHECKER:
        return None
    if amap is not None:
        alt = amap[alt] if alt < len(amap) else None
    alts = holder.get("alternatives")
    if alt is None or not isinstance(alts, list) or alt >= len(alts):
        return None
    return alts[alt], {}


def _place_annotations(ogxm: dict, games: list, ply_at: list, decoded: list, annos: list,
                       alt_maps: dict, fallback: list) -> None:
    """Hang every annotation that is not one of ours on what it addresses --
    the match, a game, a ply, a decision's analysis object or one of its
    alternatives -- and mark it ``placed``. One that addresses nothing the
    document holds (an unknown scope or decision kind, a decision whose block
    could not be read) is left for the passthrough record. ``fallback`` is the
    list of annotations ours carried for what ``ANNO`` could not address
    (``x-gammonview-annotations``); they follow the native ones."""
    objs = {info["analysis_id"]: obj for info, obj in decoded}

    def hang(holder: dict, rec: dict) -> None:
        holder.setdefault("annotations", []).append(rec)

    for r in annos:
        if anno_is_ours(r):
            continue
        scope, ref = r["scope"], r["ref"]
        rec = anno_doc(r)
        target = None
        if scope == SCOPE_MATCH:
            target = ogxm
        elif scope == SCOPE_GAME and ref < len(games):
            target = games[ref]
        elif scope == SCOPE_PLY and ref < len(ply_at):
            target = ply_at[ref][1]
        elif (scope in (SCOPE_DECISION, SCOPE_ALTERNATIVE) and ref < len(ply_at)
              and r.get("analysis") in objs and "kind" in r
              and (scope == SCOPE_DECISION or "alt_index" in r)):
            key, ply = ply_at[ref]
            got = _holder_of(ply, objs[r["analysis"]].get(key), r["kind"],
                             r.get("alt_index") if scope == SCOPE_ALTERNATIVE else None,
                             alt_maps.get((r["analysis"], ref)))
            if got is not None:
                target = got[0]
                rec.update(got[1])
        if target is not None:
            hang(target, rec)
            r["placed"] = True

    by_key = {(g["game_index"], i): p for g in games for i, p in enumerate(g["plies"])}
    for e in fallback:
        try:
            scope, rec = e["s"], dict(e["v"])
            if scope == SCOPE_MATCH:
                target = ogxm
            elif scope == SCOPE_GAME:
                target = games[e["g"]]
            elif scope == SCOPE_PLY:
                target = by_key[(e["g"], e["p"])]
            else:
                key = (e["g"], e["p"])
                got = _holder_of(by_key[key], objs[e["a"]].get(key), e["k"],
                                 e["i"] if scope == SCOPE_ALTERNATIVE else None, None)
                target = got[0]
                rec.update(got[1])
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise GvabError("an annotation of ours addresses nothing in the match") from exc
        hang(target, rec)


def _doc_clock(payload: bytes, ply_at: list, ogxm: dict) -> None:
    """The ``CLCK`` section as ``clock_info`` and each ply's ``timestamp_ms``; an invalid
    section is dropped (8.2) and kept only by the passthrough."""
    from .ogxm2_passthrough import clock_doc, decode_clock
    got = decode_clock(payload, len(ply_at))
    if got is None:
        return
    header, ts, precision = got
    ogxm["clock_info"] = clock_doc(header, precision)
    for i, t in enumerate(ts):
        ply_at[i][1]["timestamp_ms"] = t


def _doc_video(payload: bytes, v2games: list, games: list, offsets: list, ogxm: dict) -> None:
    """The ``VIDO`` section as ``video_info`` and its marks on the plies they mark."""
    from .ogxm2_passthrough import decode_video, video_doc, video_mark_doc
    got = decode_video(payload, [len(g["plies"]) for g in v2games])
    if got is None:
        return
    header, marks = got
    ogxm["video_info"] = video_doc(header)
    for gi, pi, hand, video_ms, wall, behind in marks:
        games[gi]["plies"][pi + offsets[gi]].update(video_mark_doc(hand, video_ms, wall, behind))


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

def _dice_action_id(d1: int, d2: int) -> int:
    a, b = sorted((d1, d2))
    return (0, 6, 11, 15, 18, 20)[a - 1] + (b - a)


def _color(seat: int) -> int:
    """v1's colour polarity is the Seat enum's reverse: 1 is White."""
    return 1 if seat == 0 else 0


def _v1_ply(p: dict) -> dict:
    x = p.get("extras") or {}
    action = p["action"]
    if action == ACTION_RESERVED:
        raise GvabError("OGXM v2 uses the reserved action 35")
    assigned = action <= LAST_KNOWN_ACTION
    # Each extras field belongs to the actions that name it (5.1).
    allowed = {ACTION_SET_POSITION: ("dice", "illegal", "board"),
               ACTION_SETTLE: ("settle_value",),
               ACTION_CUBE_SET: ("cube_value", "cube_owner"),
               ACTION_RESIGN_GAME: ("resign_value",),
               ACTION_RESIGN_MATCH: ("resign_value",)}.get(action, ())
    if assigned and any(k not in allowed and k not in ("action_ext",) for k in x):
        raise GvabError(f"OGXM v2 action {action} carries extras that do not belong to it")
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
    elif action == ACTION_SETTLE:
        if "settle_value" not in x:
            raise GvabError("OGXM v2 settle ply without its value")
        ply["settle_value"] = x["settle_value"]
    elif action == ACTION_CUBE_SET:
        if "cube_value" not in x:
            raise GvabError("OGXM v2 cube-set ply without its value")
        ply["cube_value"] = x["cube_value"]
        if "cube_owner" in x:
            ply["cube_owner"] = x["cube_owner"]
    elif action > LAST_KNOWN_ACTION and "extras_raw" in p:
        ply["extras_raw"] = base64.b64encode(p["extras_raw"]).decode("ascii")
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


def derived_resign_value(points_won: int, cube: int) -> int:
    """The factor a resignation is worth, as the writer derives it when the
    document states none: ``points_won`` over the cube, or 1."""
    if cube > 0 and points_won % cube == 0 and 1 <= points_won // cube <= 3:
        return points_won // cube
    return 1


def _cube_after(cube: int, action: int, ply: dict) -> int:
    """The cube's value after a ply (M6). A double changes nothing until it is
    answered; a drop ends the game."""
    if action == ACTION_TAKE:
        return cube * 2
    if action == ACTION_BEAVER:
        return cube * 4
    if action == ACTION_RACCOON:
        return cube * 2
    if action == ACTION_CUBE_SET:
        return int(ply.get("cube_value") or cube)
    return cube


def _v1_match(mtch: dict, v2games: list[dict]):
    starts, final = _score_walk(mtch, v2games)
    crawford = _crawford_games(mtch, starts)
    white_score, black_score = mtch.get("score_final") or final
    result = mtch.get("result")
    length = mtch["match_length"]
    if result is None:   # 3 (abandoned) is stored, and read back as stored
        result = (1 if length > 0 and white_score >= length
                  else 2 if length > 0 and black_score >= length else 0)
    rules = mtch.get("rules", 0)

    games = []
    offsets = []
    for gi, g in enumerate(v2games):
        plies = [_v1_ply(p) for p in g["plies"]]
        # A resignation's factor is derived from the points and the cube; one
        # that is not what the writer would derive is kept on its ply.
        cube = int(g.get("initial_cube_value", 1) or 1) * 2 ** int(g.get("auto_doubles", 0) or 0)
        for pi, ply in enumerate(plies):
            if ply["action_id"] in (ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH):
                stated = (g["plies"][pi].get("extras") or {}).get("resign_value")
                if stated is not None and stated != derived_resign_value(
                        int(g.get("points_won", 0) or 0), cube):
                    ply["resign_value"] = stated
            cube = _cube_after(cube, ply["action_id"], ply)
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
        for key in GV_GAME_FIELDS:
            if key in g:
                game[key] = g[key]
        games.append(game)

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
        "event": mtch.get("event") or None,
        # Our `site` is where the match was played: v2's `city`, else the
        # platform it was played on (`read_ogxm2` settles it, an annotation
        # of ours winning over both).
        "site": None,
    }
    if mtch["variant"]:
        top["variant"] = mtch["variant"]
    if rules & 0x10:
        top["auto_doubles"] = True
    if rules & ~0x1F:
        top["rules_other"] = rules & ~0x1F
    if "score_start" in mtch:
        top["score_start"] = list(mtch["score_start"])
    for key in ("completed_at", "player_seat", "crawford_before_start", "event_year",
                "date_precision", "stage", "round", "table", "city", "country", "event_url",
                "match_ref", "white_profile", "black_profile", "rated"):
        if key in mtch:
            top[key] = mtch[key]
    if "site" in mtch:
        top["platform"] = mtch["site"]          # v2's `site` is the platform's host name
    return top, games, offsets


def _gv_text(kind: str, value) -> str:
    """How a value v2 cannot hold is spelled in an annotation of ours."""
    if kind == "s":
        return str(value)
    if kind == "i":
        return str(int(value))
    return number_text(value)


def number_text(x) -> str:
    """A number as text, the same in both languages: integers without a
    fraction, otherwise the shortest form that reads back as the same double."""
    x = float(x)
    return str(int(x)) if x == int(x) and abs(x) < 1e15 else repr(x)


def _gv_parse(kind: str, text: str, what: str):
    try:
        if kind == "s":
            return text
        if kind == "i":
            return int(text)
        return float(text)
    except ValueError as exc:
        raise GvabError(f"annotation {what} is malformed") from exc


def _block_items(items: dict) -> dict:
    """The block fields an ``x-gammonview-analysis`` annotation carries because
    v2 could not hold them: ``<field>=<value>``, ``dials.<name>=<value>``, and a
    list as its elements, each quoted, joined by commas."""
    out: dict = {}
    for name, kind in GV_BLOCK_FIELDS.items():
        if name in items:
            out[name] = ([unquote(e) for e in items[name].split(",")] if kind == "l"
                         else _gv_parse(kind, unquote(items[name]), name))
    dials = {}
    for name, kind in GV_DIAL_FIELDS.items():
        if "dials." + name in items:
            text = unquote(items["dials." + name])
            dials[name] = text == "1" if kind == "b" else _gv_parse(kind, text, name)
    if dials:
        out["dials"] = dials
    return out


def _restore_fields(target: dict, gv: dict, ref: int, fields: dict, prefix: str = "") -> None:
    """Put back, from ``x-gammonview-<prefix><field>`` annotations at ``ref``,
    the values the writer could not store in v2's own fields."""
    for name, kind in fields.items():
        key = GV_PREFIX + prefix + name
        if (ref, key) in gv:
            target[name] = _gv_parse(kind, gv[(ref, key)], key)


# ---------------------------------------------------------------------------
# Analysis, in v1's shape
# ---------------------------------------------------------------------------

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


#: A level marker the pass in ``_levels`` reads and removes.
_LV = "\0level"
_LV_LUCK = "\0luck"
#: Decision keys that are v2 fields copied as they are, by kind.
CHECKER_KEYS = ("alternatives_total", "rollouts_done", "deep_searched", "position_tags",
                "producer_ref", "source_band")
CUBE_KEYS = ("take_point", "window_searched", "is_optional", "is_free_cube", "producer_ref")
RESIGN_KEYS = ("correct_value", "producer_ref")


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
        if "rollout_se" in a:
            o["rollout_se"] = to_delta(a["rollout_se"])
        if "cubeless_equity" in a:
            o["cubeless_equity"] = to_eq(a["cubeless_equity"])
        o[_LV] = lv                                    # the true level, for `_levels`
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
    for k in CHECKER_KEYS:
        if k in d:
            analysis[k] = d[k]
    analysis[_LV] = (KIND_CHECKER, level)
    return analysis


def _cube_triple(d: dict, frame) -> dict:
    to_eq = frame[0]
    return {
        "no_double_equity": to_eq(d.get("no_double_equity", 0.0)),
        "double_take_equity": to_eq(d.get("double_take_equity", 0.0)),
        "double_pass_equity": to_eq(d.get("double_pass_equity", 0.0)),
    }


def _cube_extras(d: dict, frame, target: dict, level: dict) -> None:
    """The cube record's own fields beyond the triple, on ``target``."""
    for k in CUBE_KEYS:
        if k in d:
            target[k] = d[k]
    if "cubeful_take_value" in d:
        target["cubeful_take_value"] = frame[0](d["cubeful_take_value"])
    if "currency" in d:
        target["currency"] = d["currency"]
    target[_LV] = (KIND_CUBE, level)


def _live_cube(d: dict, frame, analysis: dict, label: str | None = None,
               level: dict | None = None) -> None:
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
        _cube_extras(d, frame, missed, level)
        analysis["missed_double"] = missed
    else:
        live["decision"] = False
    _cube_extras(d, frame, live, level)
    analysis["cube_decision"] = live


def _cube_ply(d: dict, action: int, block_level: dict, frame, ours: bool = False) -> dict:
    level = _resolve(block_level, d.get("level"))
    analysis: dict = {
        "correct_action": VERDICT_NAMES.get(d["verdict"], "no_double"),
        "played_action": PLAYED_ACTION.get(action, "pass"),
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
    _cube_extras(d, frame, analysis, level)
    return analysis


def _resign(d: dict, frame, level: dict) -> dict:
    to_delta = frame[1]
    analysis: dict = {
        "resign_error": to_delta(d.get("resign_error", 0.0)),
        "take_resign_error": to_delta(d.get("take_resign_error", 0.0)),
        "equity_loss": max(0.0, to_delta(d.get("equity_loss", 0.0))),
        "decision": False,
    }
    if "probs" in d:
        analysis["eval"] = _eval_from_probs(*d["probs"])
    for k in RESIGN_KEYS:
        if k in d:
            analysis[k] = d[k]
    analysis[_LV] = (KIND_RESIGN, level)
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
    elif action in CUBE_ACTIONS:
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


def frame_from_wire(mid_white: str, half: str, white: bool) -> list[float]:
    """A source ``[mid, half]`` from the strings ``x-gammonview-analysis``'s
    ``frame=`` item holds, in the perspective of a ply whose frame owner is
    White (or not). Rounded to the eight places the item carries, so a
    document that states a frame and one read back from it agree exactly."""
    mid = float(mid_white)
    return [round(mid if white else 1 - mid, 8), round(float(half), 8)]


def _parse_frames(value: str) -> list[tuple[int, tuple[str, str] | None]]:
    """``frame=``: ``<ply_ref>:<mid_white>:<half>`` entries, each in force from
    its ply until the next; ``<ply_ref>:`` ends a run with no source frame."""
    out = []
    for entry in value.split(","):
        if not entry:
            continue
        ref, _, rest = entry.partition(":")
        mid, _, half = rest.partition(":")
        out.append((int(ref), (mid, half) if mid else None))
    return sorted(out, key=lambda e: e[0])


def _frame_in_force(frames: list, ref: int):
    """The ``(mid_white, half)`` in force at ``ref``, or None."""
    state = None
    for at, st in frames:
        if at > ref:
            break
        state = st
    return state


def _v1_block(anal: dict, decisions: list[dict], ply_at: list, match_length: int,
              ours: dict | None = None):
    """One v2 analysis block in v1's shape. ``ours`` is set for a block our
    writer made: ``{"extra": {ply_ref: [records]}, "exceptions": {tokens},
    "illegal": {ply_refs}, "frames": [...]}`` -- the annotation's decision
    records, which stand in for ``DECS``'s at their ply and kind, the flags to
    flip, and the source frames its values were converted through."""
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
        # rather than shown in the wrong unit. A block that states the source's
        # own frame for the ply converts through that, not through our table.
        wire = _frame_in_force(ours["frames"], d["ply_ref"]) if mine and mwc else None
        source = (frame_from_wire(*wire, frame_perspective_is_white(ply))
                  if wire is not None else None)
        frame = mwc_frame(ply, source) if mwc else _IDENTITY
        if frame is None:
            continue

        action = ply["action_id"]
        dice = _plays_dice(ply)
        if kind == KIND_CHECKER and dice:
            prior = block_obj.get(key) or {}
            block_obj[key] = {**_checker(d, block_level, frame, mine), **prior}
        elif kind == KIND_CUBE and dice:
            lv = _resolve(block_level, d.get("level"))
            _live_cube(d, frame, block_obj.setdefault(key, {}),
                       _label(lv, True) if mine else None, lv)
        elif kind == KIND_CUBE and action in CUBE_ACTIONS:
            block_obj[key] = _cube_ply(d, action, block_level, frame, mine)
        elif kind == KIND_ROLL and dice:
            # From the roller's side, in the block's currency -- a difference
            # of two MWCs, so it converts by the frame's slope alone.
            obj = block_obj.setdefault(key, {"decision": False})
            obj["luck"] = frame[1](d["luck"])
            if "producer_ref" in d:
                obj["luck_producer_ref"] = d["producer_ref"]
            lv = _resolve(block_level, d.get("level"))
            obj[_LV_LUCK] = lv
            luck_levels.add(_label(lv, mine))
        elif kind == KIND_RESIGN and action in (ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH):
            block_obj[key] = _resign(d, frame, _resolve(block_level, d.get("level")))

    if mine:
        ref_of = {k: i for i, (k, _p) in enumerate(ply_at)}
        flips: dict = {}
        for token in ours["exceptions"]:
            flips.setdefault(int(token[:-1]), set()).add(token[-1])
        for key, obj in block_obj.items():
            ref = ref_of[key]
            default_flags(ply_at[ref][1], obj, ref in ours["illegal"])
            _apply_exceptions(obj, flips.get(ref, set()))
            wire = _frame_in_force(ours["frames"], ref) if block_mwc else None
            if wire is not None:
                obj["mwc_frame"] = frame_from_wire(*wire, frame_perspective_is_white(ply_at[ref][1]))

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
    if "model_id" in anal:
        info["model_id"] = anal["model_id"]
    if anal.get("met_id"):
        info["met_id"] = anal["met_id"]
    info["timestamp"] = anal["started_at"] // 1000 if "started_at" in anal else 0
    if anal.get("duration_ms"):
        info["duration_ms"] = anal["duration_ms"]
    if len(luck_levels) == 1:
        label = next(iter(luck_levels))
        if label is not None:
            info["luck_eval_level"] = label
    # Every block states its identifier, not only ours: a signature, and an
    # annotation addressed to a decision, name the block by it.
    info["analysis_id"] = anal["analysis_id"]
    info.update(_block_fields(anal, ours, ply_at, match_length))
    if mine and "model_id" in ours.get("fields", {}):
        info["model_id"] = ours["fields"]["model_id"]       # a value v2 could not hold
    info[_LV] = block_level                      # for `finish_levels`, once the block is complete
    return info, block_obj, bool(luck_levels)


#: The block fields that are v2's own, in the order a document lists them.
BLOCK_KEYS = ("producer", "complete", "coverage", "model_name", "model_digest", "engine_build",
              "currency", "cube_efficiency", "tables", "dials", "completed_at", "sources")
#: How each is spelled in an ``x-gammonview-analysis`` item when v2 cannot hold
#: it: ``s`` as is, ``i`` an integer, ``f`` a number, ``l`` a list of strings.
GV_BLOCK_FIELDS = {"producer": "i", "model_id": "s", "model_name": "s", "model_digest": "s", "engine_build": "s",
                   "tables": "s", "completed_at": "i", "sources": "l"}
GV_DIAL_FIELDS = {name: ("b" if name in DIAL_FLAGS else "f" if name == "top_deep_threshold" else "i")
                  for _bit, name in DIAL_FLAGS_AND_VARINTS}


def default_currency(match_length: int) -> int:
    """The currency a block is written in unless its document says another:
    a match's equities are match winning chances, a money game's cubeful money."""
    return CURRENCY_CUBEFUL_MATCH if match_length > 0 else CURRENCY_CUBEFUL_MONEY


def _block_fields(anal: dict, ours: dict | None, ply_at: list, match_length: int) -> dict:
    """The keys v2's block record adds to ``analysis_info``: the file's, with
    an annotation of ours standing in for a value v2 could not hold."""
    have = {k: anal[k] for k in BLOCK_KEYS if k in anal}
    if "coverage" in have:
        for ref in have["coverage"]:
            if ref >= len(ply_at):
                raise GvabError(f"OGXM v2 coverage names ply {ref}, past the end of the match")
        have["coverage"] = [list(ply_at[ref][0]) for ref in have["coverage"]]
    if have.get("currency") == default_currency(match_length):
        del have["currency"]                    # the default is not a key (P3)
    if ours is not None:
        for k, v in ours.get("fields", {}).items():
            if k == "dials":
                have["dials"] = {**have.get("dials", {}), **v}
            elif k in BLOCK_KEYS:
                have[k] = v
    return {k: have[k] for k in BLOCK_KEYS if k in have}


def _norm_level(level: dict) -> dict:
    """A level without a preset that is only a depth: ``2ply`` says nothing the
    depth next to it does not."""
    out = dict(level)
    preset = out.get("preset")
    if preset and _DEPTH_PRESET.fullmatch(preset):
        del out["preset"]
    return out


def same_level(a: dict, b: dict) -> bool:
    return _norm_level(a) == _norm_level(b)


def finish_levels(info: dict, block_obj: dict) -> None:
    """Settle which tiers keep a ``level`` dict, and remove the markers.

    A document states a level by its labels (``eval_level``, ``ply``), which is
    all its own analyses need. A tier whose true level is something else -- a
    rollout, a preset the producer named, a depth its labels do not reproduce --
    keeps the true level as ``level``, resolved against the tier above, so the
    key appears exactly where the labels would give a different level. A preset
    that only restates the depth is no difference (``same_level``): producers
    name a plain 3-ply search ``3ply``, and a document would otherwise carry that
    on every decision.

    Runs once the block is complete -- ``basefill`` settles ``ply`` and
    ``eval_level`` on a foreign block, and the labels are what the writer will
    derive from."""
    bt = info.pop(_LV)
    objs = list(block_obj.values())
    bd = block_level_of(info, objs)
    if same_level(bd, bt):
        parent = bd
    else:
        info["level"] = copy.deepcopy(bt)
        parent = bt
    luck_label = info.get("luck_eval_level") or "1ply"

    def settle(true: dict, derived: dict, target: dict, key: str = "level") -> dict:
        """The level the writer will have for this tier."""
        if same_level(true, derived):
            return derived
        target[key] = copy.deepcopy(true)
        return true

    for obj in objs:
        marker = obj.pop(_LV, None)
        luck = obj.pop(_LV_LUCK, None)
        if marker is not None:
            kind, level = marker
            if kind == KIND_CHECKER:
                alts = obj.get("alternatives") or []
                derived = tier_level(parent, preset=alts[0].get("eval_level") if alts else None,
                                     checker_ply=obj.get("ply"))
                eff = settle(level, derived, obj)
                for alt in alts:
                    lv = alt.pop(_LV, None)
                    if lv is not None:
                        settle(lv, tier_level(eff, preset=alt.get("eval_level")), alt)
            elif kind == KIND_CUBE:
                settle(level, tier_level(parent, preset=obj.get("eval_level"),
                                         cube_ply=obj.get("ply")), obj)
            else:
                settle(level, parent, obj)
        for sub in (obj.get("cube_decision"), obj.get("missed_double")):
            if isinstance(sub, dict) and _LV in sub:
                _kind, level = sub.pop(_LV)
                settle(level, tier_level(parent, preset=sub.get("eval_level")), sub)
        if luck is not None:
            settle(luck, luck_level_of(parent, luck_label), obj, "luck_level")


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
    clck = vido = None
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
        elif stype == b"CLCK":
            clck = payload
        elif stype == b"VIDO":
            vido = payload
    if mtch is None:
        raise GvabError(
            "This file holds an analysis without its match, so there is nothing to show.")

    ogxm, games, offsets = _v1_match(mtch, v2games)
    gv_match = _gv_values(annos, SCOPE_MATCH)
    gv_game = _gv_values(annos, SCOPE_GAME)
    gv_ply = _gv_values(annos, SCOPE_PLY)
    if (0, GV_KEY_EVENT) in gv_match:
        ogxm["event"] = gv_match[(0, GV_KEY_EVENT)]
    _restore_fields(ogxm, gv_match, 0, GV_MATCH_FIELDS)
    for side in ("white", "black"):
        name = f"{side}_profile"
        restored: dict = {}
        _restore_fields(restored, gv_match, 0, GV_PROFILE_FIELDS, prefix=name + ".")
        if restored:
            ogxm[name] = {**ogxm.get(name, {}), **restored}
    for gi, game in enumerate(games):
        _restore_fields(game, gv_game, gi, GV_GAME_FIELDS)
    # `site` is where the match was played: our annotation, else v2's `city`,
    # else the platform's host name.
    ogxm["site"] = (gv_match[(0, GV_KEY_SITE)] if (0, GV_KEY_SITE) in gv_match
                    else ogxm.get("city") or ogxm.get("platform") or None)
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

    if clck is not None:
        _doc_clock(clck, ply_at, ogxm)
    if vido is not None:
        _doc_video(vido, v2games, games, offsets, ogxm)
        if ogxm.get("video_info") is not None and (0, GV_KEY_VIDEO_URL) in gv_match:
            ogxm["video_info"]["url"] = gv_match[(0, GV_KEY_VIDEO_URL)]

    if derive_ogids:
        _derive_ogids({"match_length": ogxm["match_length"], "games": games,
                       "variant": ogxm.get("variant", 0), "score_start": ogxm.get("score_start")})

    decoded = []
    base_blocks = []
    ours_ids: set = set()
    alt_maps: dict = {}
    for blk in blocks:
        aid = blk["anal"]["analysis_id"]
        ours = None
        if (0, GV_KEY_ANALYSIS + aid) in gv_match:
            ours_ids.add(aid)
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
                    # An annotation addresses the record `DECS` holds, whose
                    # alternatives may be fewer, or ordered otherwise, than the
                    # exact ones ours keeps.
                    exact = next((d for d in recs if d["kind"] == KIND_CHECKER), None)
                    main = next((d for d in blk["decisions"]
                                 if d["ply_ref"] == ref and d["kind"] == KIND_CHECKER), None)
                    if exact is not None and main is not None:
                        alt_maps[(aid, ref)] = _alt_map(main.get("alternatives") or [],
                                                        exact.get("alternatives") or [])
            ours = {"extra": extra, "illegal": illegal_refs,
                    "exceptions": {t for t in items.get("pr", "").split(",") if t}}
            if "level" in items:
                ours["level"] = unquote(items["level"]) or None
            if "luck" in items:
                ours["luck"] = unquote(items["luck"])
            ours["frames"] = _parse_frames(items.get("frame", ""))
            ours["fields"] = _block_items(items)
        info, block_obj, has_luck = _v1_block(
            blk["anal"], blk["decisions"], ply_at, ogxm["match_length"], ours)
        if block_obj and ours is None:
            if derive_ogids:
                complete_base_block(block_obj, ply_by_key, info, convert_units=False)
            if not has_luck:
                base_blocks.append(len(decoded))
        finish_levels(info, block_obj)
        decoded.append((info, block_obj))

    fallback: list = []
    if (0, GV_KEY_ANNOTATIONS) in gv_match:
        value = gv_match[(0, GV_KEY_ANNOTATIONS)]
        try:
            fallback = json.loads(base64.b64decode(value, validate=True).decode("utf-8"))
        except ValueError as exc:
            raise GvabError("the annotations of ours are malformed") from exc
    _place_annotations(ogxm, games, ply_at, decoded, annos, alt_maps, fallback)
    _attach_blocks(ogxm, games, decoded, ply_by_key)
    ogxm["games"] = games
    if base_blocks:
        ogxm["_base_analyses"] = base_blocks
    # What the file holds that this document cannot: kept for the writer.
    from .ogxm2_passthrough import attach
    header = (struct.unpack_from("<H", data, 6)[0], struct.unpack_from("<H", data, 10)[0])
    attach(ogxm, data, header, sections, annos, ours_ids)
    return ogxm
