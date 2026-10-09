# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Keeping what someone else wrote in an OGXM v2 file (spec I7).

Mirrors ``gvformat-js/src/ogxm2_passthrough.js``; keep the two in step, byte
for byte. ``docs/OGXM_V2_PROFILE.md`` (section 5) is the account for readers.

Our document models a match, its analyses, the clock, the video and every
annotation a player or another tool wrote (``clock_info``, ``video_info``,
``annotations``). What it cannot model -- the signatures, unknown sections,
fields and annotation scopes this version does not know -- would be dropped by
reading a foreign v2 file and writing it back. ``read_ogxm2`` therefore attaches
``_ogxm2_passthrough`` to the document, a JSON-safe record of the source's own
bytes, and ``ogxm2_writer`` consults it.

**Fingerprints, not trust.** A signature digests the bytes as stored, so the
only way to keep one valid is to write back the stored bytes -- and the only
safe time to do that is when the document still says what they say. The reader
therefore stamps each part (``MTCH``, each ``GAME``, each analysis block) with
the SHA-256 of *our writer's canonical encoding of that part*, computed from
the document it has just built. The clock, the video and each annotation are
parts too: they are encoded from the document, and the source's bytes go out
instead only while the document still encodes to what the source decoded to. The writer encodes the document again; a part
whose canonical bytes hash to the stored fingerprint has not been edited, and
its original bytes are emitted instead. An edit changes the canonical bytes, so
the original is not used and nothing stale is ever written. No flag is kept
that an edit could forget to clear.

The edit cases, in order of damage:

* nothing changed, or analysis blocks added or removed: every original part is
  emitted verbatim, with its signatures and everything else;
* ``MTCH`` changed (a metadata edit): ``MTCH`` is re-encoded, keeping every
  field our document does not model; ``MSIG`` and ``SIGN`` go, since they cover
  it; ``match_digest`` inside a kept ``ANAL`` is recomputed;
* a ``GAME`` changed (a move edit): the foreign analysis blocks go, since they
  were made over the old moves, and with them ``SIGN`` and ``MSIG``. The clock,
  the video and the annotations travel with their plies in the document, so
  they are written from it and survive.

A part is dropped only when keeping it would write a file that lies. A clock
that cannot be written for lack of a reading is dropped (profile section 5).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import struct
import uuid

KEY = "_ogxm2_passthrough"
VERSION = 1

#: Document keys each group of ``MTCH`` fields is read into (see ``_v1_match``);
#: a group whose keys still equal what the source stated is the source's.
MTCH_DOC_KEYS = ("player_white", "player_black", "crawford", "jacoby", "beaver", "raccoon",
                 "auto_doubles", "rules_other", "cube_limit", "score_start", "result",
                 "white_score", "black_score", "source", "timestamp", "date_precision",
                 "completed_at", "crawford_before_start", "event", "event_year", "stage",
                 "round", "table", "city", "country", "event_url", "player_seat", "platform",
                 "match_ref", "white_profile", "black_profile", "rated")
#: Groups of ``MTCH`` bits and the document keys they are read into. A group
#: whose keys are as the source stated them keeps the source's bytes; fields
#: that constrain one another (a year needs its event, a seat its profile's
#: kind) share a group, so an edit to one can never leave the other invalid.
_OWNED = (((0,), ("player_white",)), ((1,), ("player_black",)),
          ((2,), ("crawford", "jacoby", "beaver", "raccoon", "auto_doubles", "rules_other")),
          ((3,), ("cube_limit",)),
          ((4, 5, 6), ("score_start", "result", "white_score", "black_score")),
          ((7,), ("source",)), ((8, 14), ("timestamp", "date_precision")),
          ((9,), ("completed_at",)), ((11,), ("crawford_before_start",)),
          ((12, 13), ("event", "event_year")), ((15,), ("stage",)), ((16,), ("round",)),
          ((17,), ("table",)), ((18,), ("city",)), ((19,), ("country",)),
          ((20,), ("event_url",)),
          ((10, 21, 22, 23, 24), ("player_seat", "platform", "match_ref", "white_profile",
                                  "black_profile")),
          ((25,), ("rated",)))

# How each MTCH field (spec 4) is laid out: s string, v varint, w varint64,
# p a pair of varints, f flag, r nested record.
_MTCH_KINDS = "ssvvppvvwwvfsvvsvssssssrrf"      # bits 0-25

KNOWN_SCOPES = (0, 1, 2, 3, 4)
CLOCK_PRECISION = 10


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


def fingerprint(*chunks: bytes) -> str:
    """SHA-256 over length-prefixed chunks: two parts that happen to join into
    the same bytes still differ."""
    h = hashlib.sha256()
    for c in chunks:
        h.update(struct.pack("<I", len(c)))
        h.update(c)
    return h.hexdigest()


def match_digest(mtch: bytes, games: list[bytes]) -> bytes:
    """Spec 6.1: SHA-256 over ``MTCH`` and every ``GAME`` payload, each
    preceded by its length as a uint32."""
    h = hashlib.sha256()
    for p in (mtch, *games):
        h.update(struct.pack("<I", len(p)))
        h.update(p)
    return h.digest()


# ---------------------------------------------------------------------------
# MTCH, field by field
# ---------------------------------------------------------------------------

def _cursor(data: bytes):
    from .ogxm2 import _Cursor
    return _Cursor(data, 0, len(data))


def split_mtch(payload: bytes):
    """``(mandatory, {bit: bytes}, unknown_mask, tail)`` of an ``MTCH`` record:
    the two mandatory fields' bytes, each known optional field's bytes, and the
    unknown run at the record's end (3.2) with its mask bits."""
    cur = _cursor(payload)
    from .ogxm2 import _Cursor
    length = cur.varint()
    start = cur.pos
    body = _Cursor(payload, start, start + length)
    mask = body.varint64()
    m0 = body.pos
    body.varint()
    body.varint()
    mandatory = payload[m0:body.pos]
    fields: dict[int, bytes] = {}
    for bit, kind in enumerate(_MTCH_KINDS):
        if not (mask >> bit) & 1:
            continue
        s = body.pos
        if kind == "s":
            body.str()
        elif kind == "v":
            body.varint()
        elif kind == "w":
            body.varint64()
        elif kind == "p":
            body.varint()
            body.varint()
        elif kind == "r":
            n = body.varint()
            body.skip(n)
        fields[bit] = payload[s:body.pos]
    return mandatory, fields, mask & ~((1 << len(_MTCH_KINDS)) - 1), payload[body.pos:start + length]


def _mtch_record(mandatory: bytes, fields: dict[int, bytes], unknown_mask: int, tail: bytes) -> bytes:
    from .ogxm2_writer import _varint
    mask = unknown_mask
    body = bytearray(mandatory)
    for bit in sorted(fields):
        mask |= 1 << bit
        body += fields[bit]
    inner = _varint(mask) + bytes(body) + tail
    return _varint(len(inner)) + inner


def merge_mtch(match, doc: dict, pt_mtch: dict, games_same: bool = True) -> bytes:
    """``MTCH`` after an edit: ours for what the document says differently from
    when it was read, the source's for the rest -- including the unknown tail.
    ``match`` is the writer's ``_Match``. A stated final score (bit 5) is the
    source's only while its games are."""
    mandatory, orig, unk_mask, tail = split_mtch(b64d(pt_mtch["payload"]))
    snap = pt_mtch.get("doc") or {}
    ours = match.mtch_fields
    fields = {b: v for b, v in orig.items() if not any(b in bits for bits, _k in _OWNED)}
    for bits, keys in _OWNED:
        if all(doc.get(k) == snap.get(k) for k in keys):
            fields.update({b: orig[b] for b in bits if b in orig and (games_same or b != 5)})
        else:
            fields.update({b: ours[b] for b in bits if b in ours})
    # P4: the document counts seconds, so a source that stated milliseconds keeps
    # them while the second is the same -- unless a precision now claims the
    # instant is a period's first, which only the whole second can promise.
    if (8 in orig and 14 not in fields and doc.get("timestamp") == snap.get("timestamp")):
        fields[8] = orig[8]
    return _mtch_record(match.mtch_mandatory, fields, unk_mask, tail)


# ---------------------------------------------------------------------------
# The clock and the video (8.2, 8.3), for v1 files
# ---------------------------------------------------------------------------

_MAX_TS = 0xFFFFFFFF
MAX_VIDEO_URL = 512
_WALL_UNKNOWN = 0xFFFFFFFF
_LAG_ABSENT = 0xFFFF
_LAG_SATURATED = 0xFFFE


def _get_bits(blob: bytes, pos: int, n: int) -> int:
    v = 0
    for i in range(n):
        p = pos + i
        v |= ((blob[p >> 3] >> (p & 7)) & 1) << i
    return v


def decode_clock(payload: bytes, ply_count: int):
    """``(header, timestamps, precision)`` of a valid ``CLCK`` payload (8.2),
    else None. ``precision`` is the canonical 10 when the series is empty."""
    if len(payload) < 29:
        return None
    reserve, delay, incr, start, flags = struct.unpack_from("<IIIIB", payload, 0)
    length, small_bits, precision = struct.unpack_from("<IBI", payload, 20)
    header = (reserve, delay, incr, start, flags)
    if length == 0:
        return (header, [0], CLOCK_PRECISION) if len(payload) == 29 else None
    if not 1 <= small_bits <= 31 or precision == 0 or length + 1 > ply_count:
        return None
    per_word = 64 // small_bits
    words = -(-length // per_word)
    if len(payload) - 29 < words * 8:
        return None
    lsb = payload[29:29 + words * 8]
    low = []
    for w in range(words):
        used = min(per_word, length - w * per_word)
        if used * small_bits < 64 and _get_bits(lsb, w * 64 + used * small_bits,
                                                64 - used * small_bits):
            return None
        for k in range(used):
            low.append(_get_bits(lsb, w * 64 + k * small_bits, small_bits))
    msb = payload[29 + words * 8:]
    bit = 0
    t = 0
    ts = [0]
    for i in range(length):
        m = 0
        while True:
            if bit >= len(msb) * 8:
                return None
            one = (msb[bit >> 3] >> (bit & 7)) & 1
            bit += 1
            if not one:
                break
            m += 1
            if m > _MAX_TS:
                return None
        q = m * (1 << small_bits) + low[i]
        if q > _MAX_TS // precision + 1:
            return None
        t += q * precision
        if t > _MAX_TS:
            return None
        ts.append(t)
    if (bit + 7) // 8 != len(msb):
        return None
    if bit % 8 and (msb[bit >> 3] >> (bit & 7)) != 0:
        return None
    return header, ts, precision


def encode_clock(header, ts: list[int], ply_count: int,
                 precision: int = CLOCK_PRECISION) -> bytes | None:
    """The canonical ``CLCK`` payload for these timestamps (8.2, Writing), or
    None where the section cannot be written."""
    if ts and ts[0] != 0 or len(ts) > ply_count:
        return None
    rounded = []
    for i, t in enumerate(ts):
        if i and t < ts[i - 1]:
            return None
        rounded.append((t + precision // 2) // precision)
        if rounded[-1] * precision > _MAX_TS:
            return None
    length = max(0, len(ts) - 1)
    q = [rounded[i + 1] - rounded[i] for i in range(length)]

    def size(b: int) -> int:
        return 8 * -(-length // (64 // b)) + (sum((x >> b) + 1 for x in q) + 7) // 8

    best = 1
    if length:
        best_size = size(1)
        for b in range(2, 32):
            s = size(b)
            if s < best_size:
                best_size, best = s, b
    out = bytearray(struct.pack("<IIIIB3xIBI", *header, length, best, precision))
    per_word = 64 // best
    for i in range(0, length, per_word):
        word = 0
        for k in range(min(per_word, length - i)):
            word |= (q[i + k] & ((1 << best) - 1)) << (k * best)
        out += struct.pack("<Q", word)
    acc = nbits = 0
    for x in q:
        for bit in [1] * (x >> best) + [0]:
            acc |= bit << nbits
            nbits += 1
            if nbits == 8:
                out.append(acc)
                acc = nbits = 0
    if nbits:
        out.append(acc)
    return bytes(out)


def url_storable(url: bytes, kind: int) -> bool:
    try:
        url.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if any(c < 0x20 or c == 0x7F for c in url):
        return False
    return kind == 3 or not url or url.startswith(b"https://")


def decode_video(payload: bytes, plies_per_game: list[int]):
    """``(header, marks)`` of a valid ``VIDO`` payload (8.3), else None. A mark
    addressing no ply is dropped on its own."""
    if len(payload) < 22 or payload[0] != 1:
        return None
    kind, flags, offset, base_wall, url_len, count = struct.unpack_from("<BHiQHI", payload, 1)
    if url_len > MAX_VIDEO_URL or count > 1 << 20 or len(payload) != 22 + url_len + 14 * count:
        return None
    url = payload[22:22 + url_len]
    if not url_storable(url, kind):
        url = b""
    marks = []
    prev = (-1, -1)
    for i in range(count):
        gi, pi, mflags, video_ms, wall_delta, lag = struct.unpack_from(
            "<BHBIIH", payload, 22 + url_len + 14 * i)
        if (gi, pi) <= prev:
            return None
        prev = (gi, pi)
        wall = base_wall + wall_delta if base_wall and wall_delta != _WALL_UNKNOWN else None
        behind = lag * 1000 if lag != _LAG_ABSENT else None
        if gi >= len(plies_per_game) or pi >= plies_per_game[gi]:
            continue
        marks.append((gi, pi, mflags & 1, video_ms, wall, behind))
    return (kind, flags & 1, offset, url), marks


def clock_doc(header, precision: int) -> dict:
    """The document's ``clock_info`` for a decoded header: its four numbers, the
    ``flags`` byte as stored when it is not 0, and ``precision`` only when it is
    not the canonical 10."""
    reserve, delay, incr, start, flags = header
    out: dict = {"reserve_ms": reserve, "delay_ms": delay, "increment_ms": incr,
                 "start_timestamp": start}
    if flags:
        out["flags"] = flags
    if precision != CLOCK_PRECISION:
        out["precision"] = precision
    return out


def video_doc(header) -> dict:
    """The document's ``video_info`` for a decoded header: its ``kind`` and the other
    fields where they are not the default."""
    kind, live, offset, url = header
    out: dict = {"kind": kind}
    if live:
        out["is_live"] = True
    if offset:
        out["offset_ms"] = offset
    if url:
        out["url"] = url.decode("utf-8")
    return out


def video_mark_doc(hand: int, video_ms: int, wall, behind) -> dict:
    """What a mark puts on its ply: ``video_ms``, and the wall-clock time, the
    lag behind live and the hand-anchored flag where it states them."""
    out: dict = {"video_ms": video_ms}
    if wall is not None:
        out["wall_ms"] = wall
    if behind is not None:
        out["behind_live_ms"] = behind
    if hand:
        out["video_hand_anchored"] = True
    return out


def msig_covers_clock(payload: bytes) -> bool:
    """Whether an ``MSIG`` (8.6) signs the ``CLCK`` too (``covers`` bit 0)."""
    from .ogxm2 import _Cursor
    try:
        body, has = _Cursor(payload, 0, len(payload)).record()
        body.varint()
        body.skip(body.varint())
        if has(0):
            body.str()
        if has(1):
            body.skip(body.varint())
        if has(2):
            body.varint64()
        return bool(has(3) and body.varint() & 1)
    except Exception:  # noqa: BLE001 - a signature that does not parse covers nothing we can tell
        return True


def encode_video(header, marks) -> bytes:
    kind, live, offset, url = header
    marks = sorted(marks, key=lambda m: (m[0], m[1]))
    unique = []
    for m in marks:
        if unique and unique[-1][:2] == m[:2]:
            unique[-1] = m
        else:
            unique.append(m)
    base = 0
    for m in unique:
        if m[4] and (base == 0 or m[4] < base):
            base = m[4]
    url = url if url_storable(url, kind) else b""
    out = bytearray(struct.pack("<BBHiQHI", 1, kind, 1 if live else 0, offset, base,
                                len(url), len(unique)))
    out += url
    for gi, pi, hand, video_ms, wall, behind in unique:
        delta = _WALL_UNKNOWN
        if base and wall and wall - base < _WALL_UNKNOWN:
            delta = wall - base
        lag = _LAG_ABSENT if behind is None else min(behind // 1000, _LAG_SATURATED)
        out += struct.pack("<BHBIIH", gi, pi, 1 if hand else 0, video_ms, delta, lag)
    return bytes(out)


def v1_sign_to_v2(body: bytes) -> bytes | None:
    """A v1 ``SIGN`` chunk as a v2 ``SIGN`` payload, as the reference's
    ``v1_to_v2`` does. It cannot verify there (the signed payload differs, 8.1.1
    against v1), and is carried because dropping it would erase who vouched
    for the analysis; a verifier reports it invalid, which is true."""
    from .ogxm2_writer import _record, _str, _varint
    if len(body) < 4:
        return None
    algo, klen, plen, slen = body[0], body[1], body[2], body[3]
    if algo != 1 or len(body) < 4 + klen + plen + slen:
        return None
    key_id = body[4:4 + klen]
    public = body[4 + klen:4 + klen + plen]
    sig = body[4 + klen + plen:4 + klen + plen + slen]
    fields: dict[int, bytes] = {}
    if key_id:
        fields[0] = _varint(len(key_id)) + key_id
    if public:
        fields[1] = _varint(len(public)) + public
    return _record(_varint(algo) + _varint(len(sig)) + sig, fields)


# ---------------------------------------------------------------------------
# Reading: attach the passthrough record to a document
# ---------------------------------------------------------------------------

def attach(ogxm: dict, data: bytes, header: tuple, sections: list, annos: list,
           ours_ids: set) -> None:
    """Attach ``_ogxm2_passthrough`` to ``ogxm`` when the file holds anything
    the document cannot. ``sections`` is ``[(type, offset, payload)]`` in file
    order, ``annos`` the decoded ``ANNO`` records (each with its ``raw`` bytes)
    and ``ours_ids`` the ``analysis_id`` of every block our writer made.

    A file the writer reproduces byte for byte gets nothing, since there is
    nothing to keep: that is our own files, the common case, and it is decided
    without encoding when every block is marked ours and the file holds no
    clock, video, signature, foreign annotation or unknown section. Anything
    that stops the document being encoded (it could not be written either)
    also gets nothing.

    ``annos`` are the decoded ``ANNO`` records; ``read_ogxm2`` marks the ones it
    hung on the document ``placed``. A placed record is kept by the fingerprint
    of its canonical encoding, so it goes out verbatim while the document still
    says what it said; one that addressed nothing (an unknown scope or decision
    kind) is kept whole.
    """
    from . import ogxm2 as R
    from .ogxm2_writer import _assemble, _encode, anno_canon

    foreign_annos = [r for r in annos if not R.anno_is_ours(r)]
    types = [s[0] for s in sections]
    blocks_in_file = [R.uuid_of(p) for t, _o, p in sections if t == b"ANAL"]
    unknown = [s for s in sections if s[0] not in R.KNOWN_SECTIONS]
    if (blocks_in_file and all(b in ours_ids for b in blocks_in_file) and not foreign_annos
            and not unknown and not any(t in types for t in (b"CLCK", b"VIDO", b"MSIG", b"SIGN"))):
        return
    try:
        parts = _encode(ogxm)
        if _assemble(parts, Plan(parts, ogxm)) == data:
            return
    except Exception:  # noqa: BLE001 - a document that cannot be written has nothing to keep
        return

    games = [p for t, _o, p in sections if t == b"GAME"]
    if len(games) != len(parts.match.games):
        return
    pt: dict = {
        "version": VERSION,
        "version_minor": header[0], "min_reader_minor": header[1],
        "match_length": parts.match.match_length,
    }
    mtch = next(p for t, _o, p in sections if t == b"MTCH")
    pt["mtch"] = {"payload": b64e(mtch), "fp": fingerprint(parts.mtch),
                  "doc": {k: copy.deepcopy(ogxm.get(k)) for k in MTCH_DOC_KEYS}}
    pt["games"] = [{"payload": b64e(p), "fp": fingerprint(c)} for p, c in zip(games, parts.match.games)]

    by_id = {b.aid_str: b for b in parts.blocks}
    pt_blocks: dict = {}
    anchors: list[tuple[bytes, bytes, dict]] = []
    cur = None
    gi = mi = 0
    ancillary: dict[str, bytes] = {}
    anchor: dict = {"k": "head"}
    for t, _o, p in sections:
        if t == b"MTCH":
            anchor = {"k": "MTCH"}
        elif t == b"GAME":
            anchor = {"k": "GAME", "i": gi}
            gi += 1
        elif t == b"ANAL":
            cur = R.uuid_of(p)
            pt_blocks[cur] = {"anal": b64e(p), "ours": cur in ours_ids}
            anchor = {"k": "BLOCK", "id": cur}
        elif t in (b"DECS", b"SIGN") and cur is not None:
            pt_blocks[cur]["decs" if t == b"DECS" else "sign"] = b64e(p)
            anchor = {"k": "BLOCK", "id": cur}
        elif t in (b"CLCK", b"VIDO"):
            ancillary[t.decode().lower()] = p
            anchor = {"k": t.decode()}
        elif t == b"ANNO":
            anchor = {"k": "ANNO"}
        elif t == b"MSIG":
            pt.setdefault("msig", []).append(b64e(p))
            anchor = {"k": "MSIG", "i": mi}
            mi += 1
        elif t not in R.KNOWN_SECTIONS:
            anchors.append((t, p, anchor))
    for aid, blk in pt_blocks.items():
        mine = by_id.get(aid)
        if mine is None or "decs" not in blk:
            return
        blk["fp"] = fingerprint(mine.anal, mine.decs, mine.anno_bytes())
    pt["blocks"] = pt_blocks
    # The clock and the video are in the document; the source's bytes stand for
    # them while the document still encodes to what they decoded to.
    for name, canon in (("clck", parts.clck), ("vido", parts.vido)):
        if name in ancillary:
            pt[name] = {"payload": b64e(ancillary[name]), "fp": fingerprint(canon or b"")}
    pt["anno"] = [{
        "scope": r["scope"], "ref": r["ref"], "analysis": r.get("analysis"),
        "kind": r.get("kind"), "alt": r.get("alt_index"), "key": r.get("key"),
        "lang": r.get("lang"), "raw": b64e(r["raw"]),
    } for r in foreign_annos if not r.get("placed")]
    pt["anno_raw"] = []
    for r in foreign_annos:
        if r.get("placed"):
            try:
                pt["anno_raw"].append({"fp": fingerprint(anno_canon(r)), "raw": b64e(r["raw"])})
            except (ValueError, OverflowError):
                pass
    pt["unknown"] = [{"type": t.decode("latin-1"), "payload": b64e(p), "after": a}
                     for t, p, a in anchors]
    ogxm[KEY] = pt


# ---------------------------------------------------------------------------
# Writing: decide, part by part, what to emit
# ---------------------------------------------------------------------------

class Plan:
    """What ``write_ogxm2`` emits: each part chosen between the document's
    canonical encoding and the source's original bytes."""

    def __init__(self, parts, doc: dict):
        pt = doc.get(KEY)
        if not isinstance(pt, dict) or pt.get("version") != VERSION:
            pt = None
        match = parts.match
        self.mtch = parts.mtch
        self.games = list(match.games)
        self.blocks: list[tuple[bytes, bytes, bytes | None, list]] = []   # anal, decs, sign, annos
        self.clck: bytes | None = parts.clck
        self.vido: bytes | None = parts.vido
        self.msig: list[bytes] = []
        self.anno_raw: dict[str, list[bytes]] = {}
        self.foreign_annos: list[dict] = []
        self.unknown: list[tuple[bytes, bytes, dict]] = []
        self.minor_floor = self.min_minor_floor = 0
        self.verbatim_ids: set[str] = set()
        #: The verbatim blocks that are another producer's: their ``DECS`` is
        #: not ours, so what it carries is only what the source's annotations say.
        self.foreign_verbatim: set[str] = set()
        #: What those blocks' own ``DECS`` holds: ``{analysis_id: {(ply_ref, kind):
        #: alternatives}}``, which says what an annotation can address there.
        self.foreign_decs: dict[str, dict] = {}
        self.games_same = self.mtch_same = False

        for b in parts.blocks:
            self.blocks.append((b.anal, b.decs, None, b.annos))
        if pt is not None:
            try:
                self._passthrough(parts, doc, pt)
            except Exception:  # noqa: BLE001 - a record that does not parse keeps nothing
                self.__init__(parts, {k: v for k, v in doc.items() if k != KEY})
        else:
            self._v1_chunks(parts, doc)

    # -- a v2 source ---------------------------------------------------------

    def _passthrough(self, parts, doc: dict, pt: dict) -> None:
        match = parts.match
        pg = pt.get("games") or []
        self.games_same = (len(pg) == len(match.games)
                           and all(fingerprint(g) == p["fp"] for g, p in zip(match.games, pg))
                           and pt.get("match_length") == match.match_length)
        for i, g in enumerate(match.games):
            if i < len(pg) and fingerprint(g) == pg[i]["fp"]:
                self.games[i] = b64d(pg[i]["payload"])
        self.mtch_same = fingerprint(parts.mtch) == pt["mtch"]["fp"]
        if self.mtch_same:
            self.mtch = b64d(pt["mtch"]["payload"])
        else:
            self.mtch = merge_mtch(match, doc, pt["mtch"], self.games_same)
        self.minor_floor = int(pt.get("version_minor") or 0)
        self.min_minor_floor = int(pt.get("min_reader_minor") or 0)

        digest = None if self.mtch_same else match_digest(self.mtch, self.games)
        for k, b in enumerate(parts.blocks):
            pb = (pt.get("blocks") or {}).get(b.aid_str)
            if (pb is None or not self.games_same
                    or fingerprint(b.anal, b.decs, b.anno_bytes()) != pb["fp"]):
                if pb is not None:
                    self._keep_started(k, b, pb)
                continue
            anal = b64d(pb["anal"])
            if digest is not None:
                anal = _patch_digest(anal, digest)
            sign = b64d(pb["sign"]) if pb.get("sign") and self.mtch_same else None
            self.blocks[k] = (anal, b64d(pb["decs"]), sign, [] if not pb["ours"] else b.annos)
            self.verbatim_ids.add(b.aid_str)
            if not pb["ours"]:
                self.foreign_verbatim.add(b.aid_str)
                from .ogxm2 import _decode_decs
                self.foreign_decs[b.aid_str] = {
                    (d["ply_ref"], d["kind"]): len(d.get("alternatives") or [])
                    for d in _decode_decs(b64d(pb["decs"]))}

        original = {}
        for name, mine in (("clck", parts.clck), ("vido", parts.vido)):
            pc = pt.get(name)
            original[name] = None
            if pc is not None:
                original[name] = b64d(pc["payload"])
                if fingerprint(mine or b"") == pc["fp"]:
                    setattr(self, name, original[name])
        for r in pt.get("anno_raw") or []:
            self.anno_raw.setdefault(r["fp"], []).append(b64d(r["raw"]))
        # A match signature digests the clock when it says so, so it stands only
        # while the clock written is the one it signed.
        if self.games_same and self.mtch_same:
            self.msig = [b64d(m) for m in pt.get("msig") or []
                         if not msig_covers_clock(b64d(m)) or self.clck == original["clck"]]
        emitted = {_uuid_of_record(a) for a, _d, _s, _n in self.blocks}
        n_games = len(match.games)
        for r in pt.get("anno") or []:
            scope = r["scope"]
            if not self.games_same and (scope not in (0, 1) or (scope == 1 and r["ref"] >= n_games)):
                continue
            if scope in (3, 4) and (r["analysis"] is None
                                    or r["analysis"] not in self.verbatim_ids):
                continue
            if r["analysis"] is not None and r["analysis"] not in emitted:
                continue
            self.foreign_annos.append(r)
        self.unknown = [(t["type"].encode("latin-1"), b64d(t["payload"]), t["after"])
                        for t in pt.get("unknown") or []]

    def take_raw(self, canon: bytes) -> bytes | None:
        """The source's bytes for an annotation the document still encodes to
        ``canon`` -- each used once, so duplicates stay duplicates."""
        pool = self.anno_raw.get(fingerprint(canon))
        return pool.pop(0) if pool else None

    def _keep_started(self, k: int, b, pb: dict) -> None:
        """P4 for a block re-encoded from the document: a source that stated
        milliseconds for ``started_at`` keeps them while the document's second
        (``timestamp``) is the same."""
        from .ogxm2 import _decode_anal
        try:
            src = _decode_anal(b64d(pb["anal"])).get("started_at")
            mine = _decode_anal(b.anal).get("started_at")
        except Exception:  # noqa: BLE001
            return
        if src is not None and mine is not None and src != mine and src // 1000 == mine // 1000:
            self.blocks[k] = (b.anal_started(src), b.decs, None, b.annos)

    # -- a v1 source ---------------------------------------------------------

    def _v1_chunks(self, parts, doc: dict) -> None:
        """A v1 file's ``SIGN`` chunks. Its clock and video are in the document
        (``decode_v1_chunks``), and are written from there."""
        from .binary import CHUNK_SIGN
        for c in doc.get("_unknown_chunks") or []:
            if int(c.get("type", 0) or 0) != CHUNK_SIGN:
                continue
            body = c.get("data") or b""
            body = b64d(body) if isinstance(body, str) else body
            k = c.get("anal_index")
            sign = v1_sign_to_v2(body)
            if isinstance(k, int) and 0 <= k < len(self.blocks) and sign is not None:
                a, d, s, n = self.blocks[k]
                if s is None:
                    self.blocks[k] = (a, d, sign, n)


def decode_v1_chunks(doc: dict) -> None:
    """Read a v1 file's ``CLCK`` and ``VIDO`` chunks into ``clock_info``, ``video_info``
    and the plies' ``timestamp_ms`` / ``video_ms``, as the reference's ``v1_to_v2``
    reads them. The chunks stay in ``_unknown_chunks``, so a v1 rewrite is
    unchanged. A chunk that is not valid (8.2, 8.3) is dropped, as it is there."""
    from .binary import CHUNK_CLCK, CHUNK_VIDO
    from .ogxm2_writer import ply_layout
    chunks = [c for c in doc.get("_unknown_chunks") or []
              if int(c.get("type", 0) or 0) in (CHUNK_CLCK, CHUNK_VIDO)]
    if not chunks:
        return
    keys, _starts = ply_layout(doc)
    games = doc["games"]
    done: set = set()
    for c in chunks:
        t = int(c["type"])
        if t in done:
            continue
        body = c.get("data") or b""
        body = b64d(body) if isinstance(body, str) else body
        if t == CHUNK_CLCK:
            got = decode_clock(body, len(keys))
            if got is not None:
                # The reference rewrites a v1 clock at the canonical step, so
                # does this: the stated precision is not kept.
                header, ts, _precision = got
                doc["clock_info"] = clock_doc(header, CLOCK_PRECISION)
                for i, v in enumerate(ts):
                    gi, pi = keys[i]
                    games[gi]["plies"][pi]["timestamp_ms"] = v
                done.add(t)
        else:
            # v1 addresses a mark by the ply's place in the game as the document
            # lists it, a game's set-up position included.
            got = decode_video(body, [len(g["plies"]) for g in games])
            if got is not None:
                header, marks = got
                doc["video_info"] = video_doc(header)
                for gi, pi, hand, video_ms, wall, behind in marks:
                    games[gi]["plies"][pi].update(video_mark_doc(hand, video_ms, wall, behind))
                done.add(t)


def _uuid_of_record(anal: bytes) -> str:
    from .ogxm2 import uuid_of
    return uuid_of(anal)


def _patch_digest(anal: bytes, digest: bytes) -> bytes:
    """Replace ``ANAL`` bit 0 (``match_digest``) if present: it directly follows
    the mandatory 16-byte identifier, so its offset is fixed."""
    from .ogxm2 import _Cursor
    cur = _Cursor(anal, 0, len(anal))
    length = cur.varint()
    body = _Cursor(anal, cur.pos, cur.pos + length)
    mask = body.varint64()
    if not mask & 1:
        return anal
    at = body.pos + 16
    return anal[:at] + digest + anal[at + 32:]


def anno_sort_key(scope: int, ref: int, analysis: str | None, kind, alt, key, lang, seq: int):
    """Spec 8.4.1 (N1-N3): groups by target, keyed records first by ``(key,
    lang)``, prose in the order given."""
    a = uuid.UUID(analysis).bytes if analysis else b""
    kb = key.encode("utf-8") if key is not None else b""
    lb = lang.encode("utf-8") if lang is not None else b""
    return (scope, ref, analysis is not None, a, kind is not None, kind or 0,
            alt is not None, alt or 0, key is None, kb, lang is not None, lb, seq)
