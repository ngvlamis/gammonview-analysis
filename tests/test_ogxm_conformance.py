# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Conformance: what HedgeHog's own codec sees when it reads a file we wrote.

``test_gvab_writer.py`` already asserts *byte* parity -- run libogxm's writer
over the same document and the base chunks come out identical to ours. That is
a strong check and it has one blind spot, which is the whole reason this file
exists: two codecs that faithfully serialize whatever they are handed will agree
on the bytes no matter how far apart the *meanings* have drifted. A unit
divergence in a field both sides encode as ``int16 / 10000`` is invisible to it.

So this reads the same golden ``.gvab`` **twice** -- once with our reader, once
with the reference C++ codec through ctypes -- and compares the two documents
field by field. Same bytes, two implementations, one question: does the
reference see what we meant?

Every difference must fall into one of three classes, and the test fails on
anything that does not:

  1. **Derived on output.** ``notation``, ``diff``, ``classification``,
     ``*_norm_eq``, ``is_default_board``, ``replay_complete`` -- computed on
     read by the reference's JSON layer, never stored, and not something we owe
     it.
  2. **[GV] extensions.** ``decision``, ``luck``, ``eval_level`` and the MHDR
     metadata we write into reserved space. These ride in GVAN, which the
     reference skips as an unrecognized ancillary chunk -- exactly as intended.
  3. **The two known divergences**, pinned below by name and by magnitude, so
     that a change in either direction fails this test rather than passing
     unnoticed.

Requires HedgeHog -- the reference C++ codec and the format's origin -- checked
out as a sibling of this repository (``~/projects/hedgehog-public``) and built
with ``make libogxm``, so that ``build/libogxm.so`` and ``examples/ogxm_ctypes.py``
exist. Clone it from https://gitlab.com/eranlambooij/hedgehog-public. This is an optional
cross-check: it skips cleanly without it, because a fresh clone has neither, and
the suite is green either way. Point ``_HEDGEHOG_EXAMPLES`` elsewhere if you keep
it somewhere else.

Run: uv run python tests/test_ogxm_conformance.py   (exit 0 = all passed)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_HEDGEHOG_EXAMPLES = Path("~/projects/hedgehog-public/examples").expanduser()
sys.path.insert(0, str(_HEDGEHOG_EXAMPLES))

from gvformat import read_gvab

GOLDEN = _ROOT / "tests" / "golden"

#: Fields the reference derives on output. The binary stores none of them, so
#: their absence from our document is not a gap -- `notation` is a rendering of
#: `move`, `diff` of two equities, `classification` a bucket over `equity_loss`.
REF_DERIVED = {
    "notation", "diff", "classification", "is_default_board", "replay_complete",
    "no_double_norm_eq", "double_take_norm_eq", "double_pass_norm_eq",
    # Emitted as 0 where we omit a falsy value.
    "duration_ms",
}

#: Ours alone: the [GV] fields, carried in GVAN where the reference cannot see
#: them, plus the MHDR metadata written into the reference's reserved bytes.
GV_ONLY = {
    "decision", "luck", "eval_level", "luck_eval_level", "illegal_move",
    "beaver", "raccoon", "cube_limit", "event", "site",
}

#: Internal to our reader, never part of the format.
OURS_INTERNAL = {"_unknown_chunks", "_base_analyses", "analyses", "analyses_info"}

_passed = 0
_failed = 0


def check(cond: bool, msg: str) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"OK    {msg}")
    else:
        _failed += 1
        print(f"FAIL  {msg}")


# ---------------------------------------------------------------------------
# Tree walk
# ---------------------------------------------------------------------------

def _close(a, b) -> bool:
    """Equal, or equal to within the format's own quantum.

    Everything numeric in a `.gvab` is fixed-point at 1/10000, and the two
    implementations round the last digit independently, so anything inside that
    step is the same stored number rather than a disagreement.
    """
    try:
        return abs(float(a) - float(b)) <= 1e-4
    except (TypeError, ValueError):
        return a == b


def walk(path, ref, ours, out):
    """Collect (kind, key, path, value) for every difference between the trees."""
    if isinstance(ref, dict) and isinstance(ours, dict):
        for key in set(ref) | set(ours):
            here = f"{path}.{key}" if path else key
            if key not in ours:
                out.append(("ref_only", key, here, ref[key]))
            elif key not in ref:
                out.append(("ours_only", key, here, ours[key]))
            else:
                walk(here, ref[key], ours[key], out)
    elif isinstance(ref, list) and isinstance(ours, list):
        if len(ref) != len(ours):
            out.append(("length", path.rsplit(".", 1)[-1],
                        f"{path} ({len(ref)} vs {len(ours)})", None))
        for i, (a, b) in enumerate(zip(ref, ours)):
            walk(f"{path}[{i}]", a, b, out)
    elif not _close(ref, ours):
        out.append(("value", path.rsplit(".", 1)[-1].split("[")[0],
                    f"{path}: {ref!r} vs {ours!r}", (ref, ours)))


def _all_zero_eval(value) -> bool:
    """An `Eval` with nothing in it.

    The format's convention throughout is that all-zero probabilities mean *not
    recorded* rather than 0% to win -- the binary spec says so for cube records,
    and `_build_missed_double` acts on it. We omit the field; the reference emits
    the zeros. Same information, and the difference is worth allowing by this
    rule rather than by the field's name, so a probability block that genuinely
    went missing still fails.
    """
    return isinstance(value, dict) and not any(
        v for k, v in value.items() if k != "equity")


# ---------------------------------------------------------------------------
# The two known divergences
# ---------------------------------------------------------------------------

def pin_cube_units(ref_doc, our_doc, name) -> tuple[int, float]:
    """**Divergence 1 — the three match-play cube values.**

    Base OGXM stores them as raw match-winning chance and offers normalized
    equity as a derived view; we store the normalized view itself. The bytes are
    a valid `int16` either way, so nothing errors and no byte comparison can see
    it. What *does* see it is the reference's own derived field: normalizing an
    already-normalized value, `double_pass_norm_eq` comes out far from the
    `+1.000` it is `+1.000 by construction` for a conformant file.

    Returns (payloads seen, worst |norm_eq - 1|), and is deliberately an
    assertion that the divergence is *present*: if it ever disappears, someone
    changed the producer and this test should say so.
    """
    seen, worst = 0, 0.0
    if (ref_doc.get("match_length") or 0) <= 0:
        return 0, 0.0                     # money play: both conventions agree
    for game in ref_doc.get("games") or ():
        for ply in game.get("plies") or ():
            a = ply.get("analysis") or {}
            for payload in (a, a.get("cube_decision"), a.get("missed_double")):
                if not isinstance(payload, dict):
                    continue
                if payload.get("double_pass_norm_eq") is None:
                    continue
                seen += 1
                worst = max(worst, abs(payload["double_pass_norm_eq"] - 1.0))
    return seen, worst


def _is_action_spelling(diff) -> bool:
    """**Divergence 2 — the response label is spelled with an underscore.**

    The reference writes `double/take` and `double/pass`; we write `double_take`
    and `double_pass`, which is what our own `.gva` specification documents. It
    is a *derived display label* -- not stored, not computed on by anything on
    either side -- so this is a spelling difference and not a disagreement about
    the cube.

    It was invisible until the block below started being emitted: the label only
    differs when `should_double` is true, and a `live_checker` record is written
    only when the player was right to hold, so every `cube_decision` we had ever
    produced said `no_double`, which both sides spell the same way.
    """
    _kind, key, _where, pair = diff
    if key != "action" or not isinstance(pair, tuple):
        return False
    return set(pair) in ({"double/take", "double_take"}, {"double/pass", "double_pass"})


def check_missed_double_shape(ref_doc, our_doc) -> tuple[int, int, int]:
    """A `missed_double` ply carries the decision block too, as the spec says.

    One cube entry per checker ply, so a type=2 record stands in for the ply's
    live cube as well as its error, and a reader emits both blocks from it --
    "a consumer that renders the cube panel reads only `cube_decision`".

    Returns (plies, matching equities, blocks carrying no `decision`). The last
    is the one that matters: `CubeDecision` is a display projection, given no
    `equity_loss` and no `classification` by the base spec because the decision
    is not itself an error. Our `decision` flag *is* accounting, so a derived
    block must not carry one -- `stats.py` accumulates a `cube_decision` on its
    own flag and the `missed_double` beside it, and would count one cube twice.
    """
    plies = agreed = flagless = 0
    for rg, og in zip(ref_doc.get("games") or (), our_doc.get("games") or ()):
        for rp, op in zip(rg.get("plies") or (), og.get("plies") or ()):
            ra = rp.get("analysis") or {}
            oa = op.get("analysis") or {}
            if "missed_double" not in oa:
                continue
            plies += 1
            rc, oc = ra.get("cube_decision"), oa.get("cube_decision")
            if isinstance(rc, dict) and isinstance(oc, dict) and all(
                    _close(rc.get(k), oc.get(k)) for k in
                    ("no_double_equity", "double_take_equity", "double_pass_equity")):
                agreed += 1
            if isinstance(oc, dict) and "decision" not in oc:
                flagless += 1
    return plies, agreed, flagless


# ---------------------------------------------------------------------------

def main() -> int:
    try:
        import ogxm_ctypes
    except SystemExit:
        # ogxm_ctypes exits rather than raising when libogxm.so is missing.
        print(f"SKIP: libogxm not built under {_HEDGEHOG_EXAMPLES.parent}")
        return 0
    except ImportError:
        print(f"SKIP: {_HEDGEHOG_EXAMPLES} not found (hedgehog-public not checked out)")
        return 0

    goldens = sorted(GOLDEN.glob("*.fast.gvab"))
    if not goldens:
        print(f"SKIP: no golden files in {GOLDEN}")
        return 0

    total_cube_payloads = 0
    total_md_plies = 0
    total_spellings = 0
    worst_norm_eq = 0.0

    for path in goldens:
        print(f"\n--- {path.name} ---")
        raw = path.read_bytes()

        try:
            ref_doc = json.loads(ogxm_ctypes.binary_to_json(raw))
        except Exception as exc:  # noqa: BLE001
            check(False, f"the reference codec reads our file ({exc})")
            continue
        check(True, "the reference codec reads our file")

        our_doc = {k: v for k, v in read_gvab(raw).items() if k not in OURS_INTERNAL}

        diffs: list[tuple[str, str, str]] = []
        walk("", ref_doc, our_doc, diffs)

        values = [d for d in diffs if d[0] == "value" and not _is_action_spelling(d)]
        spellings = [d for d in diffs if d[0] == "value" and _is_action_spelling(d)]
        total_spellings += len(spellings)
        lengths = [d for d in diffs if d[0] == "length"]
        check(not values,
              "every field both sides carry holds the same value"
              + ("" if not values else f" ({len(values)} differ, e.g. {values[0][2][:90]})"))
        check(not lengths,
              "and every list is the same length"
              + ("" if not lengths else f" ({lengths[0][2]})"))

        # A match containing an illegal play sits outside the base spec's
        # model. The reference replays with the rules enforced, cannot make the
        # play, and says so: `replay_complete: false` plus `replay_failed_game`
        # naming the game. It then derives no OGIDs *for the whole match*, so
        # ours become ours-only for this file.
        #
        # That is the right answer on both sides, not a conformance gap. Our
        # OGIDs are correct -- the recorded steps replay to the recorded board,
        # which the corpus audit and test_ogxm_pipeline both check -- and every
        # field the two sides share still agrees, which is what conformance is
        # about. The exception is scoped to files the reference actually flagged
        # so OGID agreement keeps being checked everywhere else.
        replay_failed = ref_doc.get("replay_complete") is False
        ref_derived = REF_DERIVED | ({"replay_failed_game"} if replay_failed else set())
        gv_only = GV_ONLY | ({"ogid_before", "ogid_after"} if replay_failed else set())
        if replay_failed:
            # Never take a replay failure on trust: it is only explainable when
            # the file really does hold an illegal play.
            check(any((p.get("analysis") or {}).get("illegal_move")
                      for g in read_gvab(raw)["games"] for p in g["plies"]),
                  "the reference's replay fails only on a file with an illegal play")

        # Structure: each side-only field must be explainable.
        unexplained_ref = sorted({d[1] for d in diffs
                                  if d[0] == "ref_only" and d[1] not in ref_derived
                                  and d[1] != "cube_decision" and d[1] != "eval"})
        unexplained_ours = sorted({d[1] for d in diffs
                                   if d[0] == "ours_only" and d[1] not in gv_only
                                   and d[1] != "eval"})
        check(not unexplained_ref,
              "the reference reports nothing we do not, beyond what it derives"
              + ("" if not unexplained_ref else f" (unexplained: {unexplained_ref})"))
        check(not unexplained_ours,
              "and we report nothing beyond the [GV] extensions"
              + ("" if not unexplained_ours else f" (unexplained: {unexplained_ours})"))

        # A side-only `eval` is allowed for exactly two reasons, and each is
        # checked on the value rather than on the field name: it is the
        # placement divergence below, or it is an evaluation nobody recorded.
        unexplained_eval = []
        for kind, key, where, value in diffs:
            if key != "eval" or kind not in ("ref_only", "ours_only"):
                continue
            if kind == "ours_only" and where.endswith(".missed_double.eval"):
                continue                       # divergence 2, checked below
            if kind == "ref_only" and _all_zero_eval(value):
                continue                       # not recorded; we omit, they zero
            unexplained_eval.append(where)
        check(not unexplained_eval,
              "every probability block is on both sides, or explained by placement "
              "or by being unrecorded"
              + ("" if not unexplained_eval else f" ({unexplained_eval[:2]})"))

        seen, worst = pin_cube_units(ref_doc, our_doc, path.name)
        total_cube_payloads += seen
        worst_norm_eq = max(worst_norm_eq, worst)

        md_plies, md_agreed, md_flagless = check_missed_double_shape(ref_doc, our_doc)
        total_md_plies += md_plies
        if md_plies:
            check(md_agreed == md_plies,
                  f"all {md_plies} missed doubles carry the decision block too, with the "
                  f"reference's equities ({md_agreed} matched)")
            check(md_flagless == md_plies,
                  f"and none of them carries a `decision` of its own ({md_flagless}/{md_plies})")

    print("\n--- the two known divergences, across every golden ---")

    # Divergence 1. Asserted present, not absent: this is a pin, and a producer
    # change that fixed it should fail here and be acknowledged deliberately.
    check(total_cube_payloads > 0,
          f"divergence 1: the reference derived norm_eq on {total_cube_payloads} "
          f"match-play cube payloads")
    check(worst_norm_eq > 0.5,
          f"divergence 1: a pass reads as {1.0 + worst_norm_eq:.2f} on the reference's "
          f"normalized scale, not the +1.000 a conformant file gives "
          f"(worst deviation {worst_norm_eq:.2f})")

    check(total_spellings > 0,
          f"divergence 2: the response label differs in spelling alone, on "
          f"{total_spellings} blocks")
    check(total_md_plies > 0,
          f"and the shape divergence is gone: {total_md_plies} missed doubles now "
          f"carry their decision block")

    print(f"\n{_passed}/{_passed + _failed} checks passed.")
    if _failed:
        print("\nA failure here means the set of differences changed. Either something "
              "diverged that had not, or one of the two known divergences was fixed -- "
              "both are worth a deliberate look rather than a green tick.")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
