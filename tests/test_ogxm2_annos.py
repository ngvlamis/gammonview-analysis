# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The clock, the video and the annotations of OGXM v2 as document keys.

``clock_info`` (with ``timestamp_ms`` on the plies), ``video_info`` (with ``video_ms`` and its
companions on the marked plies) and ``annotations`` -- lists on the match, a
game, a ply, a decision's analysis object and an alternative -- carry what v2's
``CLCK``, ``VIDO`` and ``ANNO`` hold, so they move with their plies and are
written from the document. ``docs/OGXM_V2_PROFILE.md`` has the rules.

1. the field-name list an ``ANNO`` key is checked against is the reference's;
2. synthetic documents round-trip, byte-stably, and the reference accepts them;
3. what ``ANNO`` cannot address or hold travels in ``x-gammonview-annotations``;
4. a foreign file (``annos.ogxm``, built by the reference codec with every field)
   reads into the expected document and rewrites byte for byte;
5. edits of that document write valid v2 that carries the clock, the video and
   the annotations, and drop a section only where the profile says;
6. a v1 file's clock and video are decoded;
7. the pinned bytes the JavaScript mirror must write.

Run directly:
    uv run python tests/test_ogxm2_annos.py
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_annos_cases as C  # noqa: E402
import ogxm2_blocks_cases as B  # noqa: E402
import ogxm2_oracle as oracle  # noqa: E402
from gvformat import ogxm2 as R  # noqa: E402
from gvformat import ogxm2_passthrough as P  # noqa: E402
from gvformat import read_gvab, write_gvab  # noqa: E402

FIXTURES = C.FIXTURES
FOREIGN = (FIXTURES / "annos.ogxm").read_bytes()
V1_CHUNKS = (FIXTURES / "v1-chunks.gvab").read_bytes()

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


def sections(data: bytes) -> list[tuple[str, bytes]]:
    return [(t.decode("latin-1"), p) for t, _o, p in R._walk_sections(data, len(data))]


def payloads(data: bytes, kind: str) -> list[bytes]:
    return [p for t, p in sections(data) if t == kind]


def annos(data: bytes) -> list[dict]:
    got = payloads(data, "ANNO")
    return R._decode_anno(got[0]) if got else []


def carried(data: bytes) -> list[dict]:
    """The annotations ``x-gammonview-annotations`` carries, as it states them."""
    parts = [(a.get("key", ""), a["value"]) for a in annos(data)
             if (a.get("key") or "").partition("~")[0] == R.GV_KEY_ANNOTATIONS]
    if not parts:
        return []
    parts.sort(key=lambda kv: int(kv[0].partition("~")[2] or 0))
    text = "".join(v for _k, v in parts)[len(R.GV_FORMAT):]
    return json.loads(base64.b64decode(text))


def reference(label: str, data: bytes, same: bool = True) -> dict | None:
    try:
        j = oracle.binary_to_json(data)
    except oracle.OracleUnavailable:
        return None
    check(oracle.check_json(j) == "ok", f"{label}: the reference passes every v2 rule")
    if same:
        check(oracle.json_to_binary(j) == data, f"{label}: the reference re-encodes it to the same bytes")
    return j


def main() -> int:
    try:
        oracle.library_path()
        have_oracle = True
    except oracle.OracleUnavailable as exc:
        print(f"note: {exc}; reference checks skipped")
        have_oracle = False

    # -- 1. Field names ------------------------------------------------------------
    print("--- 1. the names an ANNO key may not take ---")
    if have_oracle:
        check(all(oracle.is_field_name(n) for n in R.V2_FIELD_NAMES),
              "1. every name in V2_FIELD_NAMES is a field name to the reference")
        check(not any(oracle.is_field_name(n) for n in ("x-note", "x-gammonview-site", "note", "timestamp_ms")),
              "1. and a key of ours or a producer's is not")
    else:
        check(len(R.V2_FIELD_NAMES) == 140, "1. the list has the reference schema's 140 names")

    # -- 2. Round trips --------------------------------------------------------------
    print("--- 2. documents round-trip ---")
    cases = C.all_cases()
    read_back = {"video-dropped-mark": C.video_dropped_mark()[1], "clock-gap": C.clock_gap()[1]}
    for name, doc in cases.items():
        data = write_gvab(doc)
        back = read_gvab(data)
        back.pop(P.KEY, None)
        check(back == read_back.get(name, doc), f"2. {name}: read(write(D)) is D")
        check(write_gvab(read_gvab(data)) == data, f"2. {name}: rewriting is byte-stable")
        check(P.KEY not in read_gvab(data), f"2. {name}: a file we wrote carries no passthrough")
        j = reference(f"2. {name}", data, same=name not in ("clock-video", "annotated-fallback",
                                                            "video-dropped-mark"))
        if j is not None and name in ("clock-video", "annotated-fallback", "video-dropped-mark"):
            ref = oracle.json_to_binary(j)
            check([p for t, p in sections(data) if t != "CLCK" and t != "CSUM"]
                  == [p for t, p in sections(ref) if t != "CLCK" and t != "CSUM"],
                  f"2. {name}: the reference writes every section but the clock (its step is 100 ms here) as we do")
            check(j["clock_info"]["reserve_ms"] == 120000, f"2. {name}: and reads the clock")

    cv = cases["clock-video"]
    check(cv["clock_info"] == C.CLOCK and cv["video_info"] == C.VIDEO
          and cv["games"][0]["plies"][3]["video_hand_anchored"] is True
          and cv["games"][0]["plies"][3]["wall_ms"] == 1790380805000
          and cv["games"][1]["plies"][5]["behind_live_ms"] == 65534000,
          "2. the clock and the video carry every header field and every mark field")
    check(sum("timestamp_ms" in p for g in cv["games"] for p in g["plies"]) == 30
          and "timestamp_ms" not in cv["games"][1]["plies"][0],
          "2. a clock reading is on thirty plies, and not on a game's set-up position")
    check("precision" not in cases["clock-canonical"]["clock_info"] and cases["clock-canonical"]["clock_info"]["flags"] == 2,
          "2. a canonical clock has no precision key; its flags are the byte as stored")
    check(cases["clock-empty"]["clock_info"] == {"reserve_ms": 0, "delay_ms": 0, "increment_ms": 0, "start_timestamp": 0}
          and cases["clock-empty"]["games"][0]["plies"][0]["timestamp_ms"] == 0
          and cases["clock-empty"]["video_info"] == {"kind": 0},
          "2. an empty clock reads back with the reading at ply 0 that v2 implies, and a bare video")

    # -- 3. What ANNO cannot hold -------------------------------------------------------
    print("--- 3. annotations of ours carry what ANNO cannot ---")
    data = write_gvab(cases["annotated"])
    check(not carried(data), "3. a fully addressable document carries nothing in x-gammonview-annotations")
    own = [a for a in annos(data) if not R.anno_is_ours(a)]
    check(len(own) == 20, f"3. all twenty annotations are ANNO records ({len(own)})")
    check({a["scope"] for a in own} == {0, 1, 2, 3, 4}, "3. at all five scopes")
    roll = [a for a in own if a["scope"] == 3 and a["kind"] == 3]
    cube = [a for a in own if a["scope"] == 3 and a["kind"] == 1]
    check(len(roll) == 1 and len(cube) == 3 and all(a["analysis"] for a in own if a["scope"] >= 3),
          "3. a roll, and the cube decisions of a dice ply and of a double, name their kind and block")
    draw = next(a for a in own if a.get("drawings"))
    check(len(draw["drawings"]) == 6 and draw["value"] == ""
          and draw["drawings"][-1] == {"shape": 7, "at": 5, "color": 9},
          "3. a record that only draws has an empty value, and a shape nobody knows is kept")

    data = write_gvab(cases["annotated-fallback"])
    got = carried(data)
    check([(e["s"], e.get("g"), e.get("p")) for e in got]
          == [(0, None, None), (2, 0, 1), (2, 0, 2), (2, 0, 3), (1, 1, None), (2, 1, 0)],
          "3. a drawing at the match, a value past the cap, two drawings v2 forbids, a language past the cap "
          "and a note on a set-up position are carried")
    check({a["value"] for a in annos(data) if not R.anno_is_ours(a)} == {"ok", "native"},
          "3. the ones ANNO can hold are written as ANNO")
    data = write_gvab(cases["annotated-decisions-fallback"])
    check([(e["s"], e["p"], e["k"], e.get("i")) for e in carried(data)] == [(3, 7, 0, None), (4, 7, 0, 1)],
          "3. a decision only the annotation holds takes its annotations with it; the cube beside it does not")
    data = write_gvab(cases["video-url-unholdable"])
    check(any(a.get("key") == R.GV_KEY_VIDEO_URL for a in annos(data)),
          "3. a video URL v2 would drop is carried")

    written, read = C.video_dropped_mark()
    check(read_gvab(write_gvab(written)).get("games")[1]["plies"][0].get("video_ms") is None,
          "3. a mark on a set-up position is on no ply v2 holds, so it is not written")
    written, read = C.clock_gap()
    back = read_gvab(write_gvab(written))
    check("clock_info" not in back and "video_info" in back
          and not any("timestamp_ms" in p for g in back["games"] for p in g["plies"]),
          "3. a clock with a gap in its readings is not written, and the video stands")

    many = C.base()
    many["annotations"] = [{"value": "n", "key": f"x-k{i}"} for i in range(4100)]
    big = C.base()
    big["annotations"] = [{"value": "b" * 4000, "key": f"x-k{i}"} for i in range(70)]
    for label, doc in (("more than 4096 annotations", many), ("more than 256 KiB of annotations", big)):
        try:
            write_gvab(doc)
            refused = False
        except ValueError as exc:
            refused = "annotations" in str(exc)
        check(refused, f"3. {label}: the writer raises, and drops nothing")

    for name, doc in C.errors().items():
        try:
            write_gvab(doc)
            refused = False
        except ValueError:
            refused = True
        check(refused, f"3. {name}: the writer refuses")

    # -- 4. A foreign file ------------------------------------------------------------------
    print("--- 4. a foreign file ---")
    foreign_json = oracle.binary_to_json(FOREIGN) if have_oracle else None
    doc = read_gvab(FOREIGN)
    pt = doc.get(P.KEY)
    expected = json.loads((FIXTURES / "annos.expected.json").read_text())
    held = {k: v for k, v in doc.items() if k != P.KEY}
    check(held == expected, "4. read_gvab gives the expected document")
    check(doc["clock_info"]["flags"] == 3 | 16
          and doc["video_info"] == {"kind": 2, "is_live": True, "offset_ms": -300,
                               "url": "https://www.twitch.tv/videos/99"},
          "4. the clock and video headers")
    check(len(pt["anno"]) == 2 and sorted(a["scope"] for a in pt["anno"]) == [3, 9]
          and len(pt["anno_raw"]) == 17 and "clck" in pt and "vido" in pt,
          "4. only the two annotations that address nothing are kept whole; the rest by fingerprint")
    check(not any(k in pt for k in ("clock_info", "video_info")) and json.loads(json.dumps(pt)) == pt,
          "4. and the record is JSON-safe")
    check(write_gvab(doc) == FOREIGN, "4. write(read(F)) is F, byte for byte")
    check(write_gvab(json.loads(json.dumps(doc))) == FOREIGN, "4. the .gva route writes the same bytes")
    check(write_gvab(read_gvab(FOREIGN, derive_ogids=False)) == FOREIGN, "4. reading without OGIDs changes nothing")
    check(all(not R.anno_is_ours(r) or (r.get("key") or "").startswith("x-gammonview-") for r in annos(FOREIGN)),
          "4. the source holds no annotation of ours")
    names = json.dumps(doc)
    check("x-gammonview" not in names, "4. and none of ours surfaces in the document")
    check(doc["games"][0].get("annotations") == [{"value": "Game zero", "key": "x-game"}]
          and doc["games"][1].get("annotations") == [{"value": "Game one"}],
          "4. game annotations")

    # -- 5. Edits -----------------------------------------------------------------------------
    print("--- 5. edits ---")
    src = sections(FOREIGN)

    def edited(name: str) -> bytes:
        return write_gvab(C.EDITS[name](read_gvab(FOREIGN)))

    def same(kinds_: tuple, out: bytes, label: str) -> None:
        check(all(payloads(out, k) == payloads(FOREIGN, k) for k in kinds_), label)

    for name in C.EDITS:
        out = edited(name)
        reference(f"5. {name}", out)
        check(write_gvab(read_gvab(out)) == out, f"5. {name}: the result is stable")

    out = edited("comment_edited")
    same(("MTCH", "GAME", "ANAL", "DECS", "SIGN", "MSIG", "CLCK", "VIDO"), out,
         "5. an edited comment changes ANNO only: signatures, clock and video are the source's bytes")
    check(b"hello, edited" in payloads(out, "ANNO")[0] and b"hello" not in payloads(out, "ANNO")[0].replace(b"hello, edited", b""),
          "5. and the comment is changed")
    old, new = annos(FOREIGN), annos(out)
    check(len(old) == len(new) and sum(a["raw"] != b["raw"] for a, b in zip(old, new)) == 1,
          "5. one annotation record differs; every other is the source's bytes")

    out = edited("annotation_added")
    same(("MTCH", "GAME", "ANAL", "DECS", "SIGN", "MSIG", "CLCK", "VIDO"), out,
         "5. adding annotations keeps every signature valid (ANNO is outside what they cover)")
    j = reference("5. annotation_added", out)
    check(len(annos(out)) == len(annos(FOREIGN)) + 3
          and all(a["raw"] in {x["raw"] for x in annos(out)} for a in annos(FOREIGN)),
          "5. three records are added, the others are untouched")
    check(not carried(out), "5. all three address something the file holds")

    out = edited("annotation_removed")
    same(("GAME", "SIGN", "MSIG", "CLCK", "VIDO"), out, "5. removing an annotation touches ANNO only")
    check(len(annos(out)) == len(annos(FOREIGN)) - 1, "5. and it is gone")

    out = edited("clock_edited")
    check(payloads(out, "CLCK") != payloads(FOREIGN, "CLCK") and payloads(out, "VIDO") == payloads(FOREIGN, "VIDO")
          and payloads(out, "SIGN") == payloads(FOREIGN, "SIGN"),
          "5. an edited reading re-encodes the clock; the video and the block signatures stand")
    check(payloads(out, "MSIG") == payloads(FOREIGN, "MSIG")[:1],
          "5. the match signature that covers the clock is gone; the one that does not stays")
    ts = P.decode_clock(payloads(out, "CLCK")[0], 68)[1]
    check(ts[6] == P.decode_clock(payloads(FOREIGN, "CLCK")[0], 68)[1][6] + 40, "5. the reading is the edited one")

    out = edited("ply_removed")
    check(not {"SIGN", "MSIG"} & {t for t, _p in sections(out)},
          "5. a move edit drops the signatures (they cover the moves)")
    check(payloads(out, "ANAL") and payloads(out, "CLCK") and payloads(out, "VIDO"),
          "5. but not the clock, the video, or the analysis")
    ts = P.decode_clock(payloads(out, "CLCK")[0], 66)[1]
    check(len(ts) == C.FOREIGN_READINGS - 2, f"5. the clock has two readings fewer ({len(ts)})")
    j = reference("5. ply_removed", out)
    if j is not None:
        refs = {(a["scope"], a["ref"]) for a in j["annotations"] if not a.get("key", "").startswith("x-gammonview")}
        check(("decision", 15) in refs and ("ply", 3) in refs and ("ply", 4) in refs
              and ("alternative", 2) in refs,
              "5. annotations moved with their plies: the double is now ply 15, earlier ones stay")
        marks = [(g, i) for g, gg in enumerate(j["games"]) for i, p in enumerate(gg["plies"]) if "video_ms" in p]
        check(marks == [(0, 1), (0, 5), (1, 10)], "5. so did the video marks")

    out = edited("ply_inserted_after_clock")
    check(payloads(out, "CLCK") == payloads(FOREIGN, "CLCK") and payloads(out, "VIDO") == payloads(FOREIGN, "VIDO"),
          "5. a ply after the last reading leaves the clock and the video the source's bytes")
    j = reference("5. ply_inserted_after_clock", out)
    if j is not None:
        check(any(a["scope"] == "decision" and a["ref"] == 18 for a in j["annotations"]),
              "5. and a later decision's annotation follows its ply")

    out = edited("ply_inserted_in_clock")
    check("CLCK" not in {t for t, _p in sections(out)} and payloads(out, "VIDO"),
          "5. a ply among the readings with none of its own: the clock cannot be written and is dropped; the video stands")
    j = reference("5. ply_inserted_in_clock", out)
    if j is not None:
        check("clock_info" not in j and "timestamp_ms" not in json.dumps(j["games"]),
              "5. no clock, no readings")
        marks = [(g, i) for g, gg in enumerate(j["games"]) for i, p in enumerate(gg["plies"]) if "video_ms" in p]
        check(marks == [(0, 1), (0, 6), (1, 10)], "5. the video marks moved with their plies")
        check({(a["scope"], a["ref"]) for a in j["annotations"]} >= {("ply", 4), ("ply", 5), ("decision", 3)},
              "5. and so did the annotations")

    out = edited("block_removed")
    j = reference("5. block_removed", out)
    if j is not None:
        check(not any(a.get("analysis") == foreign_json["analyses_info"][1]["analysis_id"] for a in j["annotations"])
              and any(a["scope"] == "decision" for a in j["annotations"]),
              "5. annotations naming the removed block go with it; the other block's stay")
        check(len(payloads(out, "SIGN")) == 1 and payloads(out, "MSIG") == payloads(FOREIGN, "MSIG"),
              "5. its signature goes; the match signatures stand")

    # -- 6. A v1 file ---------------------------------------------------------------------------
    print("--- 6. a v1 file's clock and video ---")
    v1 = read_gvab(V1_CHUNKS)
    check(v1["clock_info"] == {"reserve_ms": 120000, "delay_ms": 12000, "increment_ms": 0,
                          "start_timestamp": 1790442000}
          and v1["video_info"]["kind"] == 1 and v1["video_info"]["offset_ms"] == -500,
          "6. the chunks decode to clock and video (a v1 clock's step is rewritten to the canonical one)")
    check(sum("timestamp_ms" in p for g in v1["games"] for p in g["plies"]) == 11
          and [c["name"] for c in v1["_unknown_chunks"]] == ["SIGN", "CLCK", "VIDO"],
          "6. eleven readings, and the chunks still stand for a v1 rewrite")
    marked = [(gi, pi, p["video_ms"]) for gi, g in enumerate(v1["games"]) for pi, p in enumerate(g["plies"])
              if "video_ms" in p]
    check(marked == [(0, 2, 7000), (0, 6, 15000)], f"6. and two marks ({marked})")
    out = write_gvab(v1)
    check(write_gvab(read_gvab(out)) == out and read_gvab(out)["clock_info"] == v1["clock_info"],
          "6. the v2 file reads back to the same clock")
    if have_oracle:
        ref, _rule = oracle.v1_to_v2(V1_CHUNKS)
        check(payloads(out, "CLCK") == payloads(ref, "CLCK") and payloads(out, "VIDO") == payloads(ref, "VIDO"),
              "6. byte-identical to the reference's v1_to_v2")

    # -- 7. Pinned ----------------------------------------------------------------------------------
    print("--- 7. pinned ---")
    pinned = json.loads((FIXTURES / "annos-sha256.json").read_text())
    for name, doc in cases.items():
        data = write_gvab(doc)
        check(pinned[name] == {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)},
              f"7. {name}: bytes as pinned")
    pinned = json.loads((FIXTURES / "annos-edits-sha256.json").read_text())
    for name in C.EDITS:
        data = edited(name)
        check(pinned[name] == {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)},
              f"7. {name}: bytes as pinned")

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
