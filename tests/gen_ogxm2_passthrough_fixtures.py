# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Build the foreign-producer OGXM v2 fixtures the passthrough tests read.

``gvformat-js/test/fixtures/ogxm2/foreign.ogxm`` is HedgeHog's own
``two-blocks.ogxm`` (a real export, players renamed) with everything our
document cannot hold added through the reference codec's JSON projection:

* ``CLCK`` with a flags byte carrying an unassigned bit, and ``VIDO`` with marks;
* a ``SIGN`` on each analysis block and two ``MSIG`` (one covering ``CLCK``) --
  fake Ed25519 bytes, since the tests are about bytes and payloads, not about
  cryptography;
* ``ANNO`` records of every scope our reader does not consume, prose and keyed,
  with drawings;
* match context (event, stage, round, city, ...), ``completed_at`` and
  ``player_seat``, and an unknown tail on ``MTCH`` and on a checker decision;
* a ``DECS`` record of an unknown kind, and an unknown ancillary section.

``v1-chunks.gvab`` is a v1 sample with a ``SIGN``, a non-canonical ``CLCK`` and a
``VIDO`` spliced in, for the v1 case. ``passthrough-sha256.json`` pins what each
case of ``tests/passthrough_cases.py`` writes, so the JavaScript mirror can be
held to the same bytes.

Rerun after a change to either writer, and commit the result with it:
    uv run python tests/gen_ogxm2_passthrough_fixtures.py
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_oracle as oracle  # noqa: E402

FIXTURES = _REPO_ROOT / "gvformat-js" / "test" / "fixtures" / "ogxm2"
V1_SAMPLE = _REPO_ROOT / "samples" / "gv" / "XCu3RJ0UvDg_Tldl.gvab"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def build_foreign() -> bytes:
    base = oracle.binary_to_json((FIXTURES / "two-blocks.ogxm").read_bytes())
    j = copy.deepcopy(base)

    j.update({
        "completed_at": 1790450000000, "player_seat": "white", "event": "Test Open",
        "event_year": 2026, "stage": "Final", "round": 3, "city": "Testville", "country": "NO",
        "rated": True, "white_profile": {"country": "NO", "kind": "human"},
        "_ext": {"mask": "%016x" % (1 << 26 | 1 << 27), "bytes": _b64(b"\x05ab")},
    })

    j["clock_info"] = {"reserve_ms": 120000, "delay_ms": 12000, "increment_ms": 0,
                       "start_timestamp": 1790442000, "flags": 4}
    t = n = 0
    for g in j["games"]:
        for p in g["plies"]:
            if n < 40:
                p["timestamp_ms"] = t
                t += 1000 + 37 * n
            n += 1
    j["video_info"] = {"url": "https://example.com/v/abc", "kind": "youtube",
                       "is_live": False, "offset_ms": 250}
    j["games"][0]["plies"][3].update({"video_ms": 5000, "wall_ms": 1790442005000,
                                      "behind_live_ms": 3000, "video_hand_anchored": True})
    j["games"][1]["plies"][0]["video_ms"] = 9000

    for i, info in enumerate(j["analyses_info"]):
        info["signature"] = {"algorithm": "ed25519", "signature": _b64(bytes(range(i, i + 64))),
                             "key_id": f"test-key-{i}"}
        if i == 0:
            info["signature"]["public_key"] = _b64(bytes(range(32)))
    j["analysis_info"] = j["analyses_info"][0]
    j["match_signatures"] = [
        {"algorithm": "ed25519", "signature": _b64(bytes(range(2, 66))), "key_id": "platform",
         "public_key": _b64(bytes(range(32)))},
        {"algorithm": "ed25519", "signature": _b64(bytes(range(3, 67))), "key_id": "player",
         "covers": ["clck"]},
    ]

    # Decisions of the second block, to address with decision/alternative scope.
    refs = []
    n = 0
    for g in j["games"]:
        for p in g["plies"]:
            for a in p.get("analyses") or []:
                if a["analysis_index"] == 1 and a.get("alternatives") and len(a["alternatives"]) > 1:
                    refs.append(n)
            n += 1
    second_id = j["analyses_info"][1]["analysis_id"]
    j["annotations"] = [
        {"scope": "match", "ref": 0, "value": "A note on the match"},
        {"scope": "game", "ref": 1, "key": "x-example-game", "value": "game one note"},
        {"scope": "ply", "ref": 5, "key": "x-example-tag", "value": "hello"},
        {"scope": "ply", "ref": 7, "value": "",
         "drawings": [{"shape": "arrow", "at": 6, "to": 3, "color": "red"},
                      {"shape": "highlight", "at": 20}]},
        {"scope": "decision", "ref": refs[0], "kind": "checker", "analysis": second_id,
         "value": "a comment on a decision"},
        {"scope": "alternative", "ref": refs[0], "kind": "checker", "alt_index": 1,
         "analysis": second_id, "value": "a comment on an alternative", "lang": "en"},
    ]

    # An unknown tail on a checker decision, and a decision of a kind nobody knows.
    for g in j["games"]:
        for p in g["plies"]:
            a = (p.get("analyses") or [None])[0]
            if a and a.get("alternatives") and a["analysis_index"] == 0:
                a["_ext"] = {"mask": "%016x" % (1 << 10), "bytes": _b64(b"\x01\x02")}
                a["unknown"] = [{"kind": 9, "_ext": {"mask": "%016x" % 3, "bytes": _b64(b"xyz")}}]
                p["analysis"] = a
                break
        else:
            continue
        break
    j["checksum"] = "crc32"
    j["_sections"] = [{"index": 3, "type": "ZZZZ", "bytes": _b64(b"unknown section")}]
    # The first block states its match_digest (6.1), which only the written
    # MTCH and GAME bytes can give; the digest does not depend on what it is in.
    from gvformat import ogxm2 as R
    from gvformat.ogxm2_passthrough import match_digest
    first = oracle.json_to_binary(j)
    secs = R._walk_sections(first, len(first))
    digest = match_digest(next(p for t, _o, p in secs if t == b"MTCH"),
                          [p for t, _o, p in secs if t == b"GAME"])
    j["analyses_info"][0]["match_digest"] = digest.hex()
    j["analysis_info"] = j["analyses_info"][0]
    out = oracle.json_to_binary(j)
    assert oracle.check_json(oracle.binary_to_json(out)) == "ok"
    return out


def build_v1_chunks() -> bytes:
    """A v1 sample with the three chunks gvformat does not decode spliced in
    before ``CSUM``: a ``SIGN`` on its first block, a ``CLCK`` whose series is
    not in the canonical form (precision 100, 4 small bits), and a ``VIDO``."""
    from gvformat.binary import CHUNK_CLCK, CHUNK_CSUM, CHUNK_SIGN, CHUNK_VIDO

    src = V1_SAMPLE.read_bytes()
    key_id, pub, sig = b"test-key", bytes(range(32)), bytes(range(64))
    sign = bytes([1, len(key_id), len(pub), len(sig)]) + key_id + pub + sig

    # 10 deltas of 100 ms, 120 ms, ... in units of the (non-canonical) 100 ms precision.
    q = [1, 1, 2, 1, 3, 1, 1, 2, 1, 1]
    small = 4
    per_word = 64 // small
    clck = struct.pack("<IIIIB3x", 120000, 12000, 0, 1790442000, 0)
    clck += struct.pack("<IBI", len(q), small, 100)
    for i in range(0, len(q), per_word):
        word = 0
        for k, x in enumerate(q[i:i + per_word]):
            word |= (x & 15) << (k * small)
        clck += struct.pack("<Q", word)
    bits = []
    for x in q:
        bits += [1] * (x >> small) + [0]
    msb = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        msb[i >> 3] |= b << (i & 7)
    clck += bytes(msb)

    url = b"https://example.com/watch"
    vido = struct.pack("<BBHiQHI", 1, 1, 0, -500, 1790442000000, len(url), 2) + url
    vido += struct.pack("<BHBIIH", 0, 2, 1, 7000, 2000, 4)
    vido += struct.pack("<BHBIIH", 0, 6, 0, 15000, 10000, 0xFFFF)

    def chunk(t: int, body: bytes) -> bytes:
        return struct.pack("<IIHH", t, len(body), 0, 0) + body

    i = src.find(struct.pack("<I", CHUNK_CSUM))
    extra = chunk(CHUNK_SIGN, sign) + chunk(CHUNK_CLCK, clck) + chunk(CHUNK_VIDO, vido)
    out = bytearray(src[:i] + extra + src[i:])
    struct.pack_into("<I", out, 12, len(out))
    struct.pack_into("<I", out, len(out) - 4, len(out))      # the trailer repeats it
    cs = bytes(out).find(struct.pack("<I", CHUNK_CSUM))
    struct.pack_into("<I", out, cs + 16, zlib.crc32(bytes(out[:cs])) & 0xFFFFFFFF)
    return bytes(out)


def main() -> int:
    (FIXTURES / "foreign.ogxm").write_bytes(build_foreign())
    (FIXTURES / "v1-chunks.gvab").write_bytes(build_v1_chunks())
    print("wrote foreign.ogxm, v1-chunks.gvab")

    from passthrough_cases import CASES, V1_CASES
    foreign = (FIXTURES / "foreign.ogxm").read_bytes()
    v1 = (FIXTURES / "v1-chunks.gvab").read_bytes()
    pinned = {}
    for name, fn in CASES.items():
        data = fn(foreign)
        pinned[name] = {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}
    for name, fn in V1_CASES.items():
        data = fn(v1)
        pinned[name] = {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}
    (FIXTURES / "passthrough-sha256.json").write_text(
        json.dumps(pinned, indent=2, sort_keys=True) + "\n")
    print(f"wrote passthrough-sha256.json ({len(pinned)} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
