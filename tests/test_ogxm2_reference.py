# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Where our files stand against the reference OGXM v2 implementation.

This is the ratchet for the v2 work. It takes the goldens -- the pipeline's own
output, pinned byte for byte -- and runs each through the reference
``ogxm_v1_to_v2``, which is the frozen v1 reader, its JSON projection and the v2
writer in composition (v2 spec 13.8). A file that converts is a file whose
*content* already satisfies the v2 rules, whatever its framing; a file that is
refused names the rule to deal with.

The expectations below are pinned deliberately, never edited to make a run pass.
Each refusal is a v2 item with a reason, and a conversion that starts failing is
a regression in what we write.

One golden is expected to be refused, and a second shape is known but not pinned
here -- both are the same subject: how an illegal play is recorded. See
``samples/README.md`` for the three distinct shapes the corpus keeps.

* ``5nqfGw9bWG3deTaU`` is the one that is pinned, and it spends too many
  die-moves. For a play the engine cannot enumerate, ``game_eval`` appends a
  synthetic alternative describing what was actually done, flagged ``played``
  and ``illegal_move``, scored by ``post_move_analytics`` and put at the end of
  the list. Its equity comes from a different estimator than the ranked plays,
  so it routinely lands above all of them, which breaks A1 (equal-level
  alternatives run in equity order) and A4/A5 (the played entry must be one of
  the legal alternatives). v2 states this case outright instead: the ply carries
  an ``illegal`` flag, no alternative is ``is_played``, and ``equity_loss`` is
  written rather than derived.
* ``rK7pXm4TqLb9NzWd`` plays one hop of ten pips, which ``pips`` cannot hold in
  three bits, so the ply is recorded as the position it left (``action_id`` 31)
  with the roll still on it. The reference replay reads a set-position ply mid
  game as a turn-order break. It has **no golden**, so this test does not cover
  it -- the refusal was found by analysing every ``samples/mat/*.mat`` and
  converting the results, and it stays unpinned until the writer reaches it or a
  golden is added for that match.

Needs the reference library; skips without one. See ``tests/ogxm2_oracle.py``.

Run directly:
    uv run python tests/test_ogxm2_reference.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import ogxm2_oracle as oracle  # noqa: E402

_GOLDEN = _REPO_ROOT / "tests" / "golden"

#: match stem -> the rule the reference names, or "ok". Pinned; see the module
#: docstring before changing a row.
_EXPECTED = {
    "3WNK_g1Z-PLsh_HyvQ5j4a": "ok",
    "5nqfGw9bWG3deTaU": "alternatives",
    "B4_SrGcsKAQmoTyHlgJCbM": "ok",
    "MYdvw1qGuyaRUH__": "ok",
    "baR-U643iUDpvzTC": "ok",
}

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"FAIL  {label}")
    else:
        print(f"OK    {label}")


def main() -> int:
    try:
        path = oracle.library_path()
        version = oracle.version()
    except oracle.OracleUnavailable as exc:
        print(f"SKIP  {exc}")
        return 0

    # Record it in every run: a rule that moved upstream and a bug on our side
    # look the same in a bare failure.
    print(f"      reference libogxm {version} at {path}")

    goldens = sorted(_GOLDEN.glob("*.gvab"))
    check(len(goldens) > 0, f"goldens are present ({len(goldens)} files)")
    check({p.name.split(".")[0] for p in goldens} == set(_EXPECTED),
          "every golden has a pinned expectation")

    for golden in goldens:
        stem = golden.name.split(".")[0]
        expected = _EXPECTED.get(stem)
        if expected is None:
            continue
        v1 = golden.read_bytes()
        v2, rule = oracle.v1_to_v2(v1)
        check(rule == expected, f"{stem}: v1 -> v2 is {expected!r} (got {rule!r})")
        if rule != "ok" or v2 is None:
            continue

        # A conversion the reference produced must satisfy its own rules, and
        # the JSON projection must be lossless -- that projection is the pivot
        # our own writer will be checked against, so a gap here would make
        # every later comparison meaningless.
        doc = oracle.binary_to_json(v2)
        check(oracle.check_json(doc) == "ok", f"{stem}: the converted document passes the v2 rules")
        check(oracle.json_to_binary(doc) == v2, f"{stem}: binary -> JSON -> binary is byte-identical")
        check(doc.get("ogxm", {}).get("major") == 2, f"{stem}: projects as OGXM major 2")
        print(f"      {stem}: {len(v1)} v1 bytes -> {len(v2)} v2 bytes "
              f"({100 * (len(v2) - len(v1)) / len(v1):+.1f}%)")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
