# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Chunk passthrough, the points_won cap, and the GVAN version boundary.

Two properties this pipeline's read -> append-analysis -> write shape depends
on, neither of which the byte-parity tests can see:

  1. A chunk gvformat does not decode (SIGN, CLCK, VIDO, anything a later spec
     version adds) survives a round trip in its original stream position. Merely
     skipping an unknown chunk on read is correct; skipping it on *rewrite*
     deletes it from the file.
  2. points_won is stored as the game's full value, and the match length caps it
     only where a running score is accumulated -- so a match-ending 4-point
     gammon scores 13-7 while still reading back as a gammon.

Run directly:
    uv run python tests/test_chunk_passthrough.py
"""

from __future__ import annotations

import base64
import struct
import sys
import zlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvformat.binary import (
    CHUNK_CSUM, CHUNK_GVAN, CHUNK_SIGN, CHUNK_VIDO, _chunk,
    cap_points_won, clamp_match_score, write_gvab,
    GVAN_VERSION, GVAN_CHECKER_REC, GVAN_CUBE_REC,
    GVAN_CHECKER_REC_V2, GVAN_CUBE_REC_V2,
)
from gvformat.reader import GvabError, read_gvab, _game_start_scores, _walk_chunks, _chunk_name

SAMPLE = _REPO_ROOT / "samples" / "gv" / "B4_SrGcsKAQmoTyHlgJCbM.gvab"

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        _failures.append(label)


def _splice_before_csum(src: bytes, extra: bytes) -> bytes:
    """Insert `extra` (whole chunks) just before CSUM, fixing file_size + CRC."""
    i = src.find(struct.pack("<I", CHUNK_CSUM))
    assert i > 0, "sample has no CSUM chunk"
    out = bytearray(src[:i] + extra + src[i:])
    struct.pack_into("<I", out, 12, len(out))
    cs = bytes(out).find(struct.pack("<I", CHUNK_CSUM))
    struct.pack_into("<I", out, cs + 16, zlib.crc32(bytes(out[:cs])) & 0xFFFFFFFF)
    return bytes(out)


def _strip_chunk(data: bytes, ctype: int) -> bytes:
    """Drop every chunk of one type, fixing file_size + CRC."""
    out = bytearray(data[:20])
    for t, body, flags in _walk_chunks(data)[0]:
        if t == ctype:
            continue
        out += struct.pack("<IIHH", t, len(body), flags, 0) + body
    out += data[-8:]
    struct.pack_into("<I", out, 12, len(out))
    cs = bytes(out).find(struct.pack("<I", CHUNK_CSUM))
    struct.pack_into("<I", out, cs + 16, zlib.crc32(bytes(out[:cs])) & 0xFFFFFFFF)
    return bytes(out)


def _chunk_names(data: bytes) -> list[str]:
    return [_chunk_name(t) for t, _body, _flags in _walk_chunks(data)[0]]


def main() -> int:
    src = SAMPLE.read_bytes()

    # --- 1. passthrough -----------------------------------------------------
    print("--- 1. unknown ancillary chunks survive a round trip ---")
    sign_body, vido_body = b"signature-payload", b"video-marks"
    withchunks = _splice_before_csum(
        src,
        _chunk(CHUNK_SIGN, sign_body, critical=False)
        + _chunk(CHUNK_VIDO, vido_body, critical=False),
    )

    d = read_gvab(withchunks)
    carried = d.get("_unknown_chunks") or []
    check([c["name"] for c in carried] == ["SIGN", "VIDO"],
          "1. SIGN and VIDO are captured, in stream order")
    check([base64.b64decode(c["data"]) for c in carried] == [sign_body, vido_body],
          "1. their bodies are captured verbatim")
    check(all(c["anal_index"] == 0 for c in carried),
          "1. each records the analysis block it followed")

    out = write_gvab(d)
    check(_chunk_names(out) == _chunk_names(withchunks),
          "1. the rewritten file has the same chunks in the same order")
    again = read_gvab(out).get("_unknown_chunks") or []
    check(again == carried, "1. a second round trip is stable")

    # The .gva JSON form must carry them too, or `.gva == read_gvab(.gvab)`
    # stops holding for a file with any chunk we don't decode.
    import json
    check(json.loads(json.dumps(carried)) == carried,
          "1. the captured chunks survive the .gva JSON form (base64, not bytes)")

    # Dropping the analysis block a SIGN was bound to must not lose the chunk.
    stripped = dict(d)
    stripped.pop("analysis_info", None)
    stripped.pop("analyses_info", None)
    for g in stripped["games"]:
        for ply in g.get("plies") or []:
            ply.pop("analysis", None)
            ply.pop("analyses", None)
    check(_chunk_names(write_gvab(stripped)).count("SIGN") == 1,
          "1. a SIGN whose block is gone is re-emitted in the trailing group")

    print()
    print("--- 2. an unknown CRITICAL chunk is rejected, not carried ---")
    crit = _splice_before_csum(src, struct.pack("<IIHH", 0x5A5A5A5A, 2, 1, 0) + b"hi")
    try:
        read_gvab(crit)
        check(False, "2. unknown critical chunk raises GvabError")
    except GvabError as e:
        check("ZZZZ" in str(e), f"2. unknown critical chunk raises GvabError naming it ({e})")
    anc = _splice_before_csum(src, struct.pack("<IIHH", 0x5A5A5A5A, 2, 0, 0) + b"hi")
    check([c["name"] for c in read_gvab(anc)["_unknown_chunks"]] == ["ZZZZ"],
          "2. the same chunk marked ancillary is carried instead")

    print()
    print("--- 3. points_won vs the match length ---")
    for args, want in [((4, 6, 7), 1), ((4, 0, 7), 4), ((4, 3, 7), 4),
                       ((4, 6, 0), 4), ((6, 0, 5), 5), ((-2, 0, 5), 0)]:
        got = cap_points_won(*args)
        check(got == want, f"3. cap_points_won{args} == {want} (got {got})")
    check(clamp_match_score(16, 13) == 13, "3. clamp_match_score(16, 13) == 13")
    check(clamp_match_score(16, 0) == 16, "3. money play is never clamped")

    # A match decided by a 4-point gammon the winner only needed 1 of.
    ogxm = {
        "match_length": 13, "player_white": "W", "player_black": "B",
        "white_score": 7, "black_score": 16,  # deliberately the uncapped sum
        "games": [
            {"game_index": 0, "winner": 0, "points_won": 7, "plies": []},
            {"game_index": 1, "winner": 1, "points_won": 12, "plies": []},
            {"game_index": 2, "winner": 1, "points_won": 4, "plies": []},
        ],
    }
    back = read_gvab(write_gvab(ogxm))
    check([g["points_won"] for g in back["games"]] == [7, 12, 4],
          "3. points_won is stored uncapped (the win type is recoverable)")
    check((back["white_score"], back["black_score"]) == (7, 13),
          f"3. MHDR final score is clamped to the match length "
          f"({back['white_score']}-{back['black_score']})")
    check(_game_start_scores(back["games"], 13)[-1] == (7, 12),
          "3. the running score stops at the match length")
    check(_game_start_scores(back["games"], 0)[-1] == (7, 12),
          "3. money play accumulates the same games uncapped")

    print()
    print("--- 4. GVAN version boundary ---")
    # A v2 chunk differs from v3 only in record width, so a reader that ignored
    # the version byte would not fail -- it would silently mis-slice the
    # sections into plausible garbage. Both widths must decode alike.
    ogxm = {
        "match_length": 5, "player_white": "W", "player_black": "B",
        "analysis_info": {"ply": 2, "eval_level": "2ply", "model_id": "t", "timestamp": 0},
        "games": [{"game_index": 0, "winner": 1, "points_won": 5, "plies": [
            {"color": 0, "action_id": 6, "d1": 3, "d2": 1,
             "analysis": {"equity_loss": 0.0, "decision": True, "luck": 0.02,
                          "eval": {"win": 0.5}, "alternatives": []}},
            {"color": 1, "action_id": 24},
        ]}],
    }
    v3 = write_gvab(ogxm)
    gvan_v3 = next(b for t, b, _f in _walk_chunks(v3)[0] if t == CHUNK_GVAN)
    check(gvan_v3[0] == GVAN_VERSION, f"4. writer emits GVAN v{GVAN_VERSION}")
    check(GVAN_CHECKER_REC == 3 and GVAN_CUBE_REC == 2,
          "4. v3 records are 3 B (checker) and 2 B (cube)")
    check(GVAN_CHECKER_REC_V2 - GVAN_CHECKER_REC == 4
          and GVAN_CUBE_REC_V2 - GVAN_CUBE_REC == 4,
          "4. v3 drops exactly the two blanked uint16 anchor slots per record")

    got = read_gvab(v3)
    luck_v3 = [(p.get("analysis") or {}).get("luck")
               for g in got["games"] for p in g["plies"] if p.get("analysis")]

    # Rebuild the same chunk in the v2 layout and confirm it decodes identically.
    body = bytearray(gvan_v3[:4])
    body[0] = 2
    pos = 4
    body += gvan_v3[pos:pos + GVAN_CHECKER_REC] + b"\x00" * 4
    check(len(gvan_v3) == 4 + GVAN_CHECKER_REC, "4. sample GVAN is header + one checker record")
    v2 = _splice_before_csum(
        _strip_chunk(v3, CHUNK_GVAN), _chunk(CHUNK_GVAN, bytes(body), critical=False))
    luck_v2 = [(p.get("analysis") or {}).get("luck")
               for g in read_gvab(v2)["games"] for p in g["plies"] if p.get("analysis")]
    check(luck_v2 == luck_v3 and luck_v3 == [0.02],
          f"4. a v2 chunk decodes to the same values as v3 ({luck_v2} == {luck_v3})")

    bad = bytearray(body); bad[0] = 9
    broken = _splice_before_csum(
        _strip_chunk(v3, CHUNK_GVAN), _chunk(CHUNK_GVAN, bytes(bad), critical=False))
    try:
        read_gvab(broken)
        check(False, "4. an unknown GVAN version is rejected")
    except GvabError as e:
        check("GVAN version 9" in str(e),
              f"4. an unknown GVAN version is rejected, not mis-sliced ({e})")

    print()
    print(f"{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
