# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Every format the analyzer accepts, and the ones it must refuse.

``gvanalysis.loader.load_ogxm`` is the single front door: whatever a caller
drops in becomes one OGXM document, and every entry point above it -- the
``gvan-match``/``gvan-batch`` CLIs, ``analyze_file``, ``analyze_match`` -- takes
exactly what it takes. Two things were wrong before Oct 2026:

1. It read ``.gva``/``.ogxm``/``.gvab``/``.mat`` and nothing else, though
   ``gvformat`` ships ``convert_xg`` and ``convert_bgf`` and neither needs the
   engine. An ``.xg`` is the format most users actually hold.
2. An unrecognized file was *guessed* to be ``.mat`` text. A ``.mat`` parser
   finds no games in an ``.xg``, so handing ``gvan-match`` one produced a
   cheerful empty analysis of a zero-game match instead of an error -- the worst
   of the three possible outcomes.

So dispatch now reads content first and the extension second, and raises when
nothing identifies the file. Both halves are asserted here: the formats that must
load (against the converter each one is supposed to reach), and the inputs that
must raise.

Needs no engine -- the loader is pure ``gvformat``.

Run directly:
    uv run python tests/test_loader_formats.py
"""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from gvanalysis.loader import INPUT_EXTENSIONS, load_ogxm
from gvformat import canonicalize, convert_bgf, convert_xg, mat_to_ogxm, read_gvab

_SAMPLES = _REPO_ROOT / "samples"
#: One match in all four source forms, plus the corpus's one committed .gva.
STEM = "5nqfGw9bWG3deTaU"
GVA_SAMPLE = _SAMPLES / "gv" / "B4_SrGcsKAQmoTyHlgJCbM.gva"

_checks = 0
_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        _failures.append(label)


def raises(fn, label: str) -> None:
    """A ValueError is the contract: `gvan-match` catches it and exits 1."""
    try:
        fn()
    except ValueError as e:
        check(bool(str(e)), f"{label} -- refused: {e}")
    except Exception as e:                                   # noqa: BLE001
        check(False, f"{label} -- raised {type(e).__name__}, not ValueError: {e}")
    else:
        check(False, f"{label} -- loaded instead of raising")


def main() -> int:
    paths = {
        "xg": _SAMPLES / "xg" / f"{STEM}.xg",
        "bgf": _SAMPLES / "bgf" / f"{STEM}.bgf",
        "mat": _SAMPLES / "mat" / f"{STEM}.mat",
        "gvab": _SAMPLES / "gv" / f"{STEM}.gvab",
    }
    missing = [k for k, p in paths.items() if not p.exists()]
    if missing or not GVA_SAMPLE.exists():
        absent = ", ".join(missing) or GVA_SAMPLE.name
        print(f"SKIP: sample corpus incomplete ({absent})")
        return 0

    # --- the formats, each against the converter the loader should reach -------
    #
    # Equality with the dedicated converter is the whole assertion: the loader's
    # job is dispatch, so anything it adds or drops is a bug in it.
    expected = {
        "xg": convert_xg(paths["xg"]),
        "bgf": convert_bgf(paths["bgf"]),
        "mat": mat_to_ogxm(paths["mat"].read_text(encoding="utf-8", errors="replace")),
        "gvab": read_gvab(paths["gvab"].read_bytes()),
    }
    for kind, path in paths.items():
        got = load_ogxm(path)
        check(got == expected[kind], f".{kind} loads as {kind} does: {path.name}")
        check(len(got.get("games") or []) > 0,
              f".{kind} yields a match with games in it")

    gva = load_ogxm(GVA_SAMPLE)
    check(gva == canonicalize(json.loads(GVA_SAMPLE.read_text(encoding="utf-8"))),
          f".gva loads as the canonical form of its own JSON: {GVA_SAMPLE.name}")

    # The reported symptom, stated as what it costs: an .xg carries XG's own
    # analysis, and reading it as .mat text threw the match away along with it.
    xg = expected["xg"]
    check(bool(xg.get("analysis_info")),
          ".xg arrives with XG's analysis block on it, not stripped")
    check(any(p.get("analysis") for g in xg["games"] for p in g["plies"]),
          "and the plies carry the evaluations, which is what ours is appended to")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # --- .gz, for every format -------------------------------------------
        for kind, path in paths.items():
            gz = tmp / f"{kind}{path.suffix}.gz"
            gz.write_bytes(gzip.compress(path.read_bytes()))
            check(load_ogxm(gz) == expected[kind],
                  f".{kind}.gz unwraps to the same match")

        # --- content beats the name ------------------------------------------
        #
        # Three of the five name themselves in their first bytes, so a file
        # saved under the wrong extension is still read as what it is. This is
        # not a nicety: a browser download named .txt, or a .mat extension on an
        # XG export, is exactly how the old guess went wrong.
        for kind in ("xg", "gvab"):
            wrong = tmp / f"mislabeled-{kind}.mat"
            wrong.write_bytes(paths[kind].read_bytes())
            check(load_ogxm(wrong) == expected[kind],
                  f"a .{kind} named .mat is still read as .{kind}")
        bgf_as_gva = tmp / "mislabeled-bgf.gva"
        bgf_as_gva.write_bytes(paths["bgf"].read_bytes())
        check(load_ogxm(bgf_as_gva) == expected["bgf"],
              "a .bgf named .gva is still read as .bgf (both open with '{')")

        # --- and the name is still enough where content cannot say -----------
        for kind in ("mat", "gvab", "xg", "bgf"):
            plain = tmp / f"noext-{kind}"
            plain.write_bytes(paths[kind].read_bytes())
            check(load_ogxm(plain) == expected[kind],
                  f"a .{kind} with no extension at all still loads")

        # --- and what must not load ------------------------------------------
        junk = tmp / "notes.txt"
        junk.write_text("Had a good session tonight, won 3 of 5.\n")
        raises(lambda: load_ogxm(junk), "prose that is not a match")

        empty = tmp / "empty.mat"
        empty.write_bytes(b"")
        raises(lambda: load_ogxm(empty), "an empty file, even named .mat")

        bad_gz = tmp / "mislabeled.mat.gz"
        bad_gz.write_bytes(paths["mat"].read_bytes())      # not gzipped at all
        raises(lambda: load_ogxm(bad_gz), "a .gz that is not gzipped")

        truncated = tmp / "half.xg"
        truncated.write_bytes(paths["xg"].read_bytes()[:200])
        raises(lambda: load_ogxm(truncated), "a truncated .xg")

        check("not any of" in _message(lambda: load_ogxm(junk))
              and all(e in _message(lambda: load_ogxm(junk)) for e in INPUT_EXTENSIONS),
              "and the refusal names every format that would have worked")

        missing_path = tmp / "nope.mat"
        try:
            load_ogxm(missing_path)
            check(False, "a path that does not exist -- loaded instead of raising")
        except FileNotFoundError:
            check(True, "a path that does not exist raises FileNotFoundError")

        # --- the bytes-or-path converters ------------------------------------
        #
        # The loader reaches them with bytes (it has already decompressed a .gz
        # before it knows the format), so both entry shapes must agree.
        check(convert_xg(paths["xg"].read_bytes()) == expected["xg"],
              "convert_xg takes the bytes as well as the path")
        check(convert_bgf(paths["bgf"].read_bytes()) == expected["bgf"],
              "convert_bgf takes the bytes as well as the path")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    for f in _failures:
        print(f"  FAILED: {f}")
    return 1 if _failures else 0


def _message(fn) -> str:
    try:
        fn()
    except Exception as e:                                   # noqa: BLE001
        return str(e)
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
