# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Spec I7 -- unknown content is preserved -- for a foreign OGXM v2 file.

Our document models a match and its analyses. Reading another producer's v2 file
and writing it back (after appending our analysis, say) used to drop the clock,
the video, every signature, other tools' annotations, unknown sections and
fields. ``read_ogxm2`` now keeps the source's bytes in ``_ogxm2_passthrough`` and
the writer emits what the document has not edited, verbatim, so a signature
stays valid exactly when it was. See ``docs/OGXM_V2_PROFILE.md`` section 5.

The fixture is ``gvformat-js/test/fixtures/ogxm2/foreign.ogxm``: HedgeHog's own
``two-blocks.ogxm`` with a clock, a video, a ``SIGN`` per block, two ``MSIG``,
annotations at every scope, match context, an unknown tail on ``MTCH`` and on a
decision, a decision of unknown kind and an unknown section added through the
reference codec (``tests/gen_ogxm2_passthrough_fixtures.py``). Signatures are
fake bytes: what is tested is that the *payload they cover* is unchanged, which
is what decides whether a real one verifies.

The reference library (``libogxm``) checks every output; without it those
checks skip, and the byte-level ones still run.

Run directly:
    uv run python tests/test_ogxm2_passthrough.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_oracle as oracle  # noqa: E402
import passthrough_cases as C  # noqa: E402
from gvformat import ogxm2 as R  # noqa: E402
from gvformat import ogxm2_passthrough as P  # noqa: E402
from gvformat import read_gvab, write_gvab  # noqa: E402
from gvformat.ogxm2_writer import _encode  # noqa: E402

FIXTURES = _REPO_ROOT / "gvformat-js" / "test" / "fixtures" / "ogxm2"
FOREIGN = (FIXTURES / "foreign.ogxm").read_bytes()
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


def kinds(data: bytes) -> list[str]:
    return [t for t, _p in sections(data)]


def reference_accepts(label: str, data: bytes) -> dict | None:
    """The reference loads ``data``, passes every v2 rule and re-encodes it to
    the same bytes. Returns its JSON projection (None without the library)."""
    try:
        j = oracle.binary_to_json(data)
    except oracle.OracleUnavailable:
        return None
    check(oracle.check_json(j) == "ok", f"{label}: the reference passes every v2 rule")
    check(oracle.json_to_binary(j) == data, f"{label}: the reference re-encodes it to the same bytes")
    return j


def main() -> int:
    try:
        oracle.library_path()
        have_oracle = True
    except oracle.OracleUnavailable as exc:
        print(f"note: {exc}; reference checks skipped")
        have_oracle = False

    src = sections(FOREIGN)
    src_kinds = [t for t, _p in src]
    foreign_json = oracle.binary_to_json(FOREIGN) if have_oracle else None

    # -- 0. What the reader keeps ---------------------------------------------
    print("--- 0. the reader keeps the source's bytes ---")
    doc = read_gvab(FOREIGN)
    pt = doc.get(P.KEY)
    check(pt is not None and json.loads(json.dumps(pt)) == pt,
          "0. the document carries _ogxm2_passthrough, and it is JSON-safe")
    check(len(pt["games"]) == 2 and len(pt["blocks"]) == 2 and len(pt["msig"]) == 2
          and len(pt["unknown"]) == 1 and pt["unknown"][0]["type"] == "ZZZZ",
          "0. games, blocks, signatures and the unknown section are all recorded")
    check(len(pt["anno"]) == 6, "0. all six foreign annotations are recorded")
    check(list(pt["blocks"]) == [i["analysis_id"] for i in doc["analyses_info"]],
          "0. a foreign block keeps its own analysis_id in the document")
    ours = read_gvab(write_gvab(read_gvab(
        (_REPO_ROOT / "samples" / "gv" / "B4_SrGcsKAQmoTyHlgJCbM.gvab").read_bytes())))
    check(P.KEY not in ours, "0. a file we wrote carries no passthrough (nothing to keep)")
    bare = {k: v for k, v in doc.items() if k != P.KEY}
    check(all(a.aid == b.aid and a.anal == b.anal and a.decs == b.decs
              for a, b in zip(_encode(doc).blocks, _encode(bare).blocks))
          and _encode(doc).mtch == _encode(bare).mtch,
          "0. canonical encodings are not affected by the passthrough record itself")
    stale = copy.deepcopy(doc)
    stale[P.KEY]["version"] = 99
    check(write_gvab(stale) == write_gvab(bare), "0. a passthrough record of another version is ignored")

    broken = copy.deepcopy(doc)
    broken[P.KEY]["mtch"] = {"payload": "!!not base64!!"}
    check(write_gvab(broken) == write_gvab(bare), "0. a record that does not parse keeps nothing, and does not fail")

    # -- 1. Unchanged ----------------------------------------------------------
    print("--- 1. an unedited rewrite is the source, byte for byte ---")
    check(write_gvab(doc) == FOREIGN,
          "1. write(read(F)) == F: sections, signatures, unknown content, all of it")
    check(write_gvab(read_gvab(write_gvab(doc))) == FOREIGN, "1. and again: stable")
    check(write_gvab(C.via_gva(doc)) == FOREIGN,
          "1. the .gva route (document as JSON text and back) writes the same bytes")
    check(write_gvab(read_gvab(FOREIGN, derive_ogids=False)) == FOREIGN,
          "1. reading without OGIDs changes nothing")

    # -- 2. Append -------------------------------------------------------------
    print("--- 2. appending our analysis keeps everything of theirs ---")
    out = C.CASES["append"](FOREIGN)
    out_secs = sections(out)
    for kind in ("MTCH", "GAME", "ZZZZ", "SIGN", "CLCK", "VIDO", "MSIG", "ANAL", "DECS"):
        mine = [p for t, p in out_secs if t == kind]
        theirs = [p for t, p in src if t == kind]
        check(mine[:len(theirs)] == theirs,
              f"2. every original {kind} section is present, byte for byte")
    check(kinds(out).count("ANAL") == 3 and kinds(out).count("SIGN") == 2,
          "2. a third block was added after their two, unsigned")
    out_doc = read_gvab(out)
    check([i.get("model_id") for i in out_doc["analyses_info"]][2] == "test/appended",
          "2. it reads back as the third block")
    check(out_doc[P.KEY] is not None and write_gvab(out_doc) == out, "2. and that file is itself stable")
    j = reference_accepts("2. append", out)
    if j is not None:
        theirs = {json.dumps(a, sort_keys=True) for a in foreign_json["annotations"]}
        mine = {json.dumps(a, sort_keys=True) for a in j["annotations"]}
        check(theirs <= mine, "2. every foreign annotation is present")
        ours_only = [a for a in j["annotations"] if json.dumps(a, sort_keys=True) not in theirs]
        check(ours_only and all(a["key"].startswith("x-gammonview-") for a in ours_only),
              "2. the only additions are ours, in the x-gammonview namespace")
        for i in range(2):
            check(oracle.analysis_signing_payload(j, i) == oracle.analysis_signing_payload(foreign_json, i),
                  f"2. block {i}: the payload its SIGN covers is unchanged (it verifies iff it did)")
        for i in range(2):
            check(oracle.match_signing_payload(j, i) == oracle.match_signing_payload(foreign_json, i),
                  f"2. MSIG {i}: the payload it covers is unchanged")
        check(j["clock_info"] == foreign_json["clock_info"]
              and j["video_info"] == foreign_json["video_info"]
              and j.get("_ext") == foreign_json.get("_ext"),
              "2. clock, video and the MTCH unknown tail are intact")
        check(j["_sections"] == foreign_json["_sections"], "2. the unknown section is where it was")

    # -- 3. A metadata edit ----------------------------------------------------
    print("--- 3. a metadata edit re-encodes MTCH and drops what covers it ---")
    out = C.CASES["metadata_edit"](FOREIGN)
    check("SIGN" not in kinds(out) and "MSIG" not in kinds(out),
          "3. SIGN and MSIG are gone: they cover MTCH, and MTCH changed")
    check(payloads(out, "GAME") == payloads(FOREIGN, "GAME"), "3. the games are verbatim")
    check(payloads(out, "CLCK") == payloads(FOREIGN, "CLCK")
          and payloads(out, "VIDO") == payloads(FOREIGN, "VIDO")
          and payloads(out, "ZZZZ") == payloads(FOREIGN, "ZZZZ"),
          "3. clock, video and the unknown section are kept (ply addressing is intact)")
    check(payloads(out, "DECS") == payloads(FOREIGN, "DECS"), "3. foreign decisions are verbatim")
    new_digest = P.match_digest(payloads(out, "MTCH")[0], payloads(out, "GAME"))
    old_anal, new_anal = payloads(FOREIGN, "ANAL")[0], payloads(out, "ANAL")[0]
    check(old_anal != new_anal and new_digest in new_anal and len(old_anal) == len(new_anal)
          and payloads(FOREIGN, "ANAL")[1] == payloads(out, "ANAL")[1],
          "3. a match_digest inside ANAL is recomputed over the new MTCH; a block without one is verbatim")
    m_old, f_old, u_old, t_old = P.split_mtch(payloads(FOREIGN, "MTCH")[0])
    m_new, f_new, u_new, t_new = P.split_mtch(payloads(out, "MTCH")[0])
    check(u_new == u_old == (1 << 26 | 1 << 27) and t_new == t_old == b"\x05ab",
          "3. MTCH's unknown presence bits and their tail are spliced back")
    check(all(f_new[b] == f_old[b] for b in f_old if b not in (0,)) and f_new[0] != f_old[0]
          and set(f_new) == set(f_old),
          "3. every other field -- completed_at, player_seat, stage, round, city -- is as it was")
    check(payloads(out, "ANNO") == payloads(FOREIGN, "ANNO"), "3. foreign annotations are all kept")
    renamed = read_gvab(out)
    check(renamed["player_white"] == "Alicia", "3. the new name reads back")
    j = reference_accepts("3. metadata edit", out)
    if j is not None:
        check(j["player_white"] == "Alicia" and j["_ext"] == foreign_json["_ext"]
              and j["stage"] == "Final" and j["completed_at"] == foreign_json["completed_at"],
              "3. the reference reads the edited MTCH with its context intact")
        check("match_signatures" not in j and all("signature" not in i for i in j["analyses_info"]),
              "3. and finds no signatures, none stale")

    # -- 4. A move edit --------------------------------------------------------
    print("--- 4. editing a move drops everything addressed by ply ---")
    out = C.CASES["move_edit"](FOREIGN)
    check(not {"SIGN", "MSIG", "CLCK", "VIDO"} & set(kinds(out)),
          "4. SIGN, MSIG, CLCK and VIDO are gone")
    check(kinds(out).count("ZZZZ") == 1, "4. the unknown section stays")
    check(payloads(out, "GAME")[0] == payloads(FOREIGN, "GAME")[0]
          and payloads(out, "GAME")[1] != payloads(FOREIGN, "GAME")[1],
          "4. the untouched game is verbatim, the edited one is re-encoded")
    j = reference_accepts("4. move edit", out)
    if j is not None:
        scopes = sorted(a["scope"] for a in j["annotations"] if not a.get("key", "").startswith("x-gammonview-"))
        check(scopes == ["game", "match"],
              f"4. ply, decision and alternative annotations are dropped; match and game kept ({scopes})")
        check(j["games"][1]["plies"][-1] != foreign_json["games"][1]["plies"][-1]
              and len(j["games"][1]["plies"]) == len(foreign_json["games"][1]["plies"]) - 1,
              "4. the edit is in the file")
        check(j["_ext"] == foreign_json["_ext"], "4. MTCH's unknown tail is still there")

    # -- 5. A block removed ----------------------------------------------------
    print("--- 5. a block gone from the document is gone from the file ---")
    out = C.CASES["block_removed"](FOREIGN)
    check(kinds(out).count("ANAL") == 1 and kinds(out).count("SIGN") == 1
          and payloads(out, "ANAL")[0] == payloads(FOREIGN, "ANAL")[0]
          and payloads(out, "SIGN")[0] == payloads(FOREIGN, "SIGN")[0],
          "5. the first block stays verbatim with its SIGN; the second is not emitted")
    check(payloads(out, "MSIG") == payloads(FOREIGN, "MSIG"),
          "5. match signatures survive (they do not cover analysis)")
    j = reference_accepts("5. block removed", out)
    if j is not None:
        check(all(a["scope"] in ("match", "game", "ply") for a in j["annotations"]
                  if not a.get("key", "").startswith("x-gammonview-")),
              "5. annotations addressed to the removed block are dropped with it")
        for i in range(2):
            check(oracle.match_signing_payload(j, i) == oracle.match_signing_payload(foreign_json, i),
                  f"5. MSIG {i} still covers the same payload")

    # -- 5b. An analysis edit ----------------------------------------------------
    print("--- 5b. editing one block's analysis re-encodes that block only ---")
    out = C.CASES["analysis_edit"](FOREIGN)
    check(payloads(out, "DECS")[0] != payloads(FOREIGN, "DECS")[0]
          and payloads(out, "DECS")[1] == payloads(FOREIGN, "DECS")[1]
          and payloads(out, "SIGN") == payloads(FOREIGN, "SIGN")[1:],
          "5b. the edited block is re-encoded and unsigned; the other keeps its bytes and SIGN")
    check(payloads(out, "MSIG") == payloads(FOREIGN, "MSIG") and payloads(out, "GAME") == payloads(FOREIGN, "GAME")
          and payloads(out, "CLCK") == payloads(FOREIGN, "CLCK"),
          "5b. the match half is untouched, so MSIG, CLCK and the games stand")
    j = reference_accepts("5b. analysis edit", out)
    if j is not None:
        check(oracle.analysis_signing_payload(j, 1) == oracle.analysis_signing_payload(foreign_json, 1),
              "5b. the untouched block's signed payload is unchanged")
        check(any(a["scope"] == "decision" for a in j["annotations"]),
              "5b. its decision annotations stay")

    # -- 6. Both -------------------------------------------------------------
    print("--- 6. append, then rename ---")
    out = C.CASES["append_then_rename"](FOREIGN)
    check("MSIG" not in kinds(out) and kinds(out).count("ANAL") == 3
          and payloads(out, "DECS")[:2] == payloads(FOREIGN, "DECS"),
          "6. the new block is added, the signatures dropped, their decisions verbatim")
    reference_accepts("6. append then rename", out)

    # -- 7. A v1 file ----------------------------------------------------------
    print("--- 7. a v1 file's undecoded chunks, written as v2 ---")
    v1doc = read_gvab(V1_CHUNKS)
    check([c["name"] for c in v1doc["_unknown_chunks"]] == ["SIGN", "CLCK", "VIDO"],
          "7. the v1 reader captured SIGN, CLCK and VIDO")
    out = write_gvab(v1doc)
    check({"SIGN", "CLCK", "VIDO"} <= set(kinds(out)),
          "7. all three are written")
    ref = None
    if have_oracle:
        ref, rule = oracle.v1_to_v2(V1_CHUNKS)
        check(ref is not None, f"7. the reference converts the same file ({rule})")
    if ref is not None:
        for kind in ("SIGN", "CLCK", "VIDO"):
            check(payloads(out, kind) == payloads(ref, kind),
                  f"7. {kind}: byte-identical to the reference's v1_to_v2")
        reference_accepts("7. v1 chunks", out)
        j = oracle.binary_to_json(out)
        check(j["clock_info"]["start_timestamp"] == 1790442000 and len(j["video_info"]["url"]) > 0,
              "7. the non-canonical clock is re-encoded canonically, the video's marks kept")
    again = read_gvab(out)
    check(write_gvab(again) == out, "7. and the v2 file is stable")
    without = {k: v for k, v in v1doc.items() if k != "_unknown_chunks"}
    check(set(kinds(write_gvab(without))) & {"SIGN", "CLCK", "VIDO"} == set(),
          "7. a document without the chunks writes none")

    # -- 8. The pinned bytes (the JavaScript mirror must write the same) ---------
    print("--- 8. pinned ---")
    pinned = json.loads((FIXTURES / "passthrough-sha256.json").read_text())
    for name, fn in {**C.CASES, **C.V1_CASES}.items():
        data = fn(V1_CHUNKS if name in C.V1_CASES else FOREIGN)
        check(pinned[name]["sha256"] == hashlib.sha256(data).hexdigest()
              and pinned[name]["length"] == len(data), f"8. {name}: bytes as pinned")

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
