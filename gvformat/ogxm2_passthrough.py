# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Keeping what someone else wrote in an OGXM v2 file (spec I7).

Mirrors ``gvformat-js/src/ogxm2_passthrough.js``; keep the two in step, byte
for byte. ``docs/OGXM_V2_PROFILE.md`` (section 5) is the account for readers.

Our document models a match and its analyses, so reading a foreign v2 file and
writing it back used to drop everything else: the clock, the video, the
signatures, other producers' annotations, sections and fields this version does
not know. ``read_ogxm2`` now attaches ``_ogxm2_passthrough`` to the document,
a JSON-safe record of the source's own bytes, and ``ogxm2_writer`` consults it.

**Fingerprints, not trust.** A signature digests the bytes as stored, so the
only way to keep one valid is to write back the stored bytes -- and the only
safe time to do that is when the document still says what they say. The reader
therefore stamps each part (``MTCH``, each ``GAME``, each analysis block) with
the SHA-256 of *our writer's canonical encoding of that part*, computed from
the document it has just built. The writer encodes the document again; a part
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
* a ``GAME`` changed (a move edit): plies are renumbered or reinterpreted, so
  also ``CLCK``, ``VIDO``, foreign blocks and every ply-addressed annotation go.

A part is dropped only when keeping it would write a file that lies.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import uuid

KEY = "_ogxm2_passthrough"
VERSION = 1

#: Document keys each group of ``MTCH`` fields is read into (see ``_v1_match``);
#: a group whose keys still equal what the source stated is the source's.
MTCH_DOC_KEYS = ("player_white", "player_black", "crawford", "jacoby", "beaver", "raccoon",
                 "cube_limit", "result", "white_score", "black_score", "source", "timestamp",
                 "event", "site")
_OWNED = (((0,), ("player_white",)), ((1,), ("player_black",)),
          ((2,), ("crawford", "jacoby", "beaver", "raccoon")), ((3,), ("cube_limit",)),
          ((6,), ("result", "white_score", "black_score")), ((7,), ("source",)),
          ((8, 14), ("timestamp",)), ((12, 13), ("event",)))

# How each MTCH field (spec 4) is laid out: s string, v varint, w varint64,
# p a pair of varints, f flag, r nested record.
_MTCH_KINDS = "ssvvppvvwwvfsvvsvssssssrrf"      # bits 0-25

KNOWN_SCOPES = (0, 1, 2, 3, 4)


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


def _str_of(field: bytes) -> str:
    cur = _cursor(field)
    return cur.str()


def _mtch_record(mandatory: bytes, fields: dict[int, bytes], unknown_mask: int, tail: bytes) -> bytes:
    from .ogxm2_writer import _varint
    mask = unknown_mask
    body = bytearray(mandatory)
    for bit in sorted(fields):
        mask |= 1 << bit
        body += fields[bit]
    inner = _varint(mask) + bytes(body) + tail
    return _varint(len(inner)) + inner


def merge_mtch(match, doc: dict, pt_mtch: dict) -> bytes:
    """``MTCH`` after an edit: ours for what the document says differently from
    when it was read, the source's for the rest -- including every field the
    document cannot hold (clock-style context, ``player_seat``, the unknown
    tail). ``match`` is the writer's ``_Match``."""
    mandatory, orig, unk_mask, tail = split_mtch(b64d(pt_mtch["payload"]))
    snap = pt_mtch.get("doc") or {}
    ours = match.mtch_fields
    fields = {b: v for b, v in orig.items() if not any(b in bits for bits, _k in _OWNED)}
    for bits, keys in _OWNED:
        if all(doc.get(k) == snap.get(k) for k in keys):
            fields.update({b: orig[b] for b in bits if b in orig})
        else:
            fields.update({b: ours[b] for b in bits if b in ours})
    return _mtch_record(match.mtch_mandatory, fields, unk_mask, tail)


# ---------------------------------------------------------------------------
# The clock and the video (8.2, 8.3), for v1 files
# ---------------------------------------------------------------------------

_MAX_TS = 0xFFFFFFFF
_MAX_VIDEO_URL = 512
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
    """``(header, timestamps)`` of a valid ``CLCK`` payload (8.2), else None."""
    if len(payload) < 29:
        return None
    reserve, delay, incr, start, flags = struct.unpack_from("<IIIIB", payload, 0)
    length, small_bits, precision = struct.unpack_from("<IBI", payload, 20)
    header = (reserve, delay, incr, start, flags)
    if length == 0:
        return (header, [0]) if len(payload) == 29 else None
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
    return header, ts


def encode_clock(header, ts: list[int], ply_count: int) -> bytes | None:
    """The canonical ``CLCK`` payload for these timestamps (8.2, Writing), or
    None where the section cannot be written."""
    if ts and ts[0] != 0 or len(ts) > ply_count:
        return None
    rounded = []
    for i, t in enumerate(ts):
        if i and t < ts[i - 1]:
            return None
        rounded.append((t + 5) // 10)
        if rounded[-1] * 10 > _MAX_TS:
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
    out = bytearray(struct.pack("<IIIIB3xIBI", *header, length, best, 10))
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


def _url_storable(url: bytes, kind: int) -> bool:
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
    if url_len > _MAX_VIDEO_URL or count > 1 << 20 or len(payload) != 22 + url_len + 14 * count:
        return None
    url = payload[22:22 + url_len]
    if not _url_storable(url, kind):
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
    url = url if _url_storable(url, kind) else b""
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

def _anno_is_ours(r: dict) -> bool:
    """Whether ``read_ogxm2`` consumes this annotation into the document (and
    so regenerates it on write)."""
    from . import ogxm2 as R
    base = (r.get("key") or "").partition("~")[0]
    if r["scope"] == R.SCOPE_MATCH:
        return base in (R.GV_KEY_SITE, R.GV_KEY_EVENT, R.GV_KEY_SCORE) or base.startswith(
            R.GV_KEY_ANALYSIS)
    if r["scope"] == R.SCOPE_PLY:
        return base == R.GV_KEY_ILLEGAL_PLY or base.startswith(R.GV_KEY_DECISIONS)
    return False


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
    """
    from . import ogxm2 as R
    from .ogxm2_writer import _assemble, _encode

    foreign_annos = [r for r in annos if not _anno_is_ours(r)]
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
    # `site` is v2's `city` or `site` field when the file states one and has no
    # annotation of ours saying otherwise; then writing it again as an
    # annotation would add a record to a file that already says it.
    _m, fields, _u, _t = split_mtch(mtch)
    stated = next((_str_of(fields[b]) for b in (18, 21) if b in fields), None)
    pt["mtch"] = {"payload": b64e(mtch), "fp": fingerprint(parts.mtch),
                  "doc": {k: ogxm.get(k) for k in MTCH_DOC_KEYS},
                  "site_stated": stated is not None and stated == ogxm.get("site")}
    pt["games"] = [{"payload": b64e(p), "fp": fingerprint(c)} for p, c in zip(games, parts.match.games)]

    by_id = {b.aid_str: b for b in parts.blocks}
    pt_blocks: dict = {}
    anchors: list[tuple[bytes, bytes, dict]] = []
    cur = None
    gi = mi = 0
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
            pt[t.decode().lower()] = b64e(p)
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
    pt["anno"] = [{
        "scope": r["scope"], "ref": r["ref"], "analysis": r.get("analysis"),
        "kind": r.get("kind"), "alt": r.get("alt_index"), "key": r.get("key"),
        "lang": r.get("lang"), "raw": b64e(r["raw"]),
    } for r in foreign_annos]
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
        self.clck: bytes | None = None
        self.vido: bytes | None = None
        self.msig: list[bytes] = []
        self.foreign_annos: list[dict] = []
        self.unknown: list[tuple[bytes, bytes, dict]] = []
        self.minor_floor = self.min_minor_floor = 0
        self.verbatim_ids: set[str] = set()
        self.games_same = self.mtch_same = False
        self.skip_site = False

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
            self.mtch = merge_mtch(match, doc, pt["mtch"])
        self.skip_site = bool(pt["mtch"].get("site_stated")
                              and doc.get("site") == (pt["mtch"].get("doc") or {}).get("site"))
        self.minor_floor = int(pt.get("version_minor") or 0)
        self.min_minor_floor = int(pt.get("min_reader_minor") or 0)

        digest = None if self.mtch_same else match_digest(self.mtch, self.games)
        for k, b in enumerate(parts.blocks):
            pb = (pt.get("blocks") or {}).get(b.aid_str)
            if (pb is None or not self.games_same
                    or fingerprint(b.anal, b.decs, b.anno_bytes()) != pb["fp"]):
                continue
            anal = b64d(pb["anal"])
            if digest is not None:
                anal = _patch_digest(anal, digest)
            sign = b64d(pb["sign"]) if pb.get("sign") and self.mtch_same else None
            self.blocks[k] = (anal, b64d(pb["decs"]), sign, [] if not pb["ours"] else b.annos)
            self.verbatim_ids.add(b.aid_str)

        if self.games_same:
            self.clck = b64d(pt["clck"]) if pt.get("clck") else None
            self.vido = b64d(pt["vido"]) if pt.get("vido") else None
        if self.games_same and self.mtch_same:
            self.msig = [b64d(m) for m in pt.get("msig") or []]
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

    # -- a v1 source ---------------------------------------------------------

    def _v1_chunks(self, parts, doc: dict) -> None:
        from .binary import CHUNK_CLCK, CHUNK_SIGN, CHUNK_VIDO
        match = parts.match
        counts = [0] * len(match.games)
        for key, _p in match.ply_at:
            counts[key[0]] += 1
        for c in doc.get("_unknown_chunks") or []:
            body = c.get("data") or b""
            body = b64d(body) if isinstance(body, str) else body
            t = int(c.get("type", 0) or 0)
            if t == CHUNK_SIGN:
                k = c.get("anal_index")
                sign = v1_sign_to_v2(body)
                if isinstance(k, int) and 0 <= k < len(self.blocks) and sign is not None:
                    a, d, s, n = self.blocks[k]
                    if s is None:
                        self.blocks[k] = (a, d, sign, n)
            elif t == CHUNK_CLCK and self.clck is None:
                clock = decode_clock(body, len(match.ply_at))
                if clock is not None:
                    self.clck = encode_clock(clock[0], clock[1], len(match.ply_at))
            elif t == CHUNK_VIDO and self.vido is None:
                video = decode_video(body, [n + s for n, s in zip(counts, match.game_start)])
                if video is not None:
                    header_, marks = video
                    shifted = [(g, p - match.game_start[g], *rest) for g, p, *rest in marks
                               if p - match.game_start[g] >= 0]
                    self.vido = encode_video(header_, shifted)


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
