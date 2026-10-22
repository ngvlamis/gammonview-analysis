# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""A preset label names one method, and nothing can change that quietly.

An analysis records ``Preset.label`` -- ``world_class@2`` for a built-in,
``custom/<name>@<digest>`` for a user preset -- so that two analyses carrying the
same label (and the same engine build) can be compared as like with like: a
player comparing their matches, a title-granting body requiring one method. That
promise holds only if:

  * a built-in's definition never changes under an existing revision -- the
    digest of what decides its results is pinned per revision in
    ``presets._REVISIONS``, and the first check here fails until a changed
    definition gets a new revision (append a row; never edit one);
  * a built-in cannot be overridden from ``presets.yaml``, not even one field;
  * a custom preset's label follows its definition, not its name or display;
  * an environment override (``GVAN_STAGED``, ``GVAN_NO_SCREEN``) shows in the
    label instead of running under the real revision's name;
  * a pinned name (``world_class@2``) runs that revision or nothing;
  * a definition held in memory (``custom_preset``) obeys the file's rules and
    gets the file's label, so the two cannot name one method two ways.

Run: uv run python tests/test_presets.py   (exit 0 = all passed)
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# The pinned digests describe the default environment.
for _var in ("GVAN_STAGED", "GVAN_NO_SCREEN", "GVAN_ROLLOUT_SELECT"):
    os.environ.pop(_var, None)

from gvanalysis import presets as P  # noqa: E402

failures = 0


def check(ok: bool, what: str, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}" + (f"\n        {detail}" if detail and not ok else ""))


@contextlib.contextmanager
def env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update(kv)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def config(yaml_text: str):
    """(presets, aliases, default, stderr) from one presets.yaml."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "presets.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = P._build_config([path])
        return (*out, err.getvalue())


def check_revisions_pinned() -> None:
    for key, spec in P._BUILTIN_SPECS.items():
        p = P.PRESETS[key]
        history = P._REVISIONS.get(key, {})
        rev = spec.get("revision")
        d = p.digest()[:12]
        check(rev is not None and rev == max(history, default=None),
              f"{key}: runs its latest pinned revision (@{rev})",
              f"_REVISIONS[{key!r}] = {history}")
        check(history.get(rev) == d,
              f"{key}@{rev}: definition matches its pinned digest",
              f"digest is now {d}, pinned {history.get(rev)}. The definition changed: "
              f"append a revision {rev + 1 if rev else 1}: {d!r} to _REVISIONS and bump "
              f"'revision' in _BUILTIN_SPECS -- never edit an existing row.")
        check(p.label == f"{key}@{rev}", f"{key}: label is {p.label}")


def check_digest_scope() -> None:
    wc = P.PRESETS["world_class"]
    renamed = dataclasses.replace(wc, key="other", display="Other", revision=None)
    check(renamed.digest() == wc.digest(), "name and display are not part of the definition")
    tweaked = dataclasses.replace(wc, error_threshold_checker=0.0)
    check(tweaked.digest() != wc.digest() and tweaked.label.startswith("world_class@2+"),
          f"a changed threshold changes the digest and marks the label ({tweaked.label})")
    with env(GVAN_STAGED="0"):
        flat = {k: p.label for k, p in P.PRESETS.items()}
    check(flat["very_quick"] == "very_quick@1",
          "very_quick never reaches the staged screen, so turning it off leaves it alone")
    check(all("+" in v for k, v in flat.items() if k != "very_quick"),
          f"GVAN_STAGED=0 shows in every other label: {sorted(flat.values())}")
    with env(GVAN_NO_SCREEN="1"):
        check("+" in P.PRESETS["deep"].label, "GVAN_NO_SCREEN shows in the label")
    with env(GVAN_ROLLOUT_SELECT="0"):
        ro = {k: p.label for k, p in P.PRESETS.items()}
    check(all(("+" in v) == k.startswith("world_class") for k, v in ro.items()),
          f"GVAN_ROLLOUT_SELECT=0 shows in exactly the rollout presets' labels: {sorted(ro.values())}")


def check_builtins_locked() -> None:
    presets, _aliases, _default, err = config(
        "presets:\n"
        "  world_class: {error_threshold: 0.0}\n"
        "  d: {first_pass: 2ply}\n"
        "  mine: {first_pass: 3ply, second_pass: truncated2}\n")
    check(presets["world_class"].error_threshold_checker == 0.02
          and presets["deep"].first_pass == "3ply",
          "a built-in (by name or alias) cannot be overridden")
    check("world_class" in err and "'d'" in err, "each skipped override is warned about", err)
    check("mine" in presets, "the file's other presets still load")
    check(presets["mine"].label.startswith("custom/mine@") and presets["mine"].revision is None,
          f"a custom preset is labelled by its definition ({presets['mine'].label})")
    same, *_ = config("presets:\n  theirs: {first_pass: 3ply, second_pass: truncated2}\n")
    check(same["theirs"].digest() == presets["mine"].digest(),
          "the same definition under another name has the same digest")

    for text, what in (
        ("presets:\n  'My Preset': {first_pass: 3ply}\n", "a name the label cannot carry"),
        ("presets:\n  mine: {first_pass: 3ply, revision: 3}\n", "a custom preset claiming a revision"),
        ("presets:\n  mine: {first_pass: 3ply, aliases: [wc]}\n", "an alias that is a built-in name"),
    ):
        got, *_rest, err = config(text)
        check("mine" not in got and "my preset" not in got and "ignoring" in err,
              f"refused: {what}", err)


def check_first_pass_split() -> None:
    """A per-kind first pass (a custom preset's: no built-in splits it), and
    one method has one digest however it is spelled."""
    check(all(p.first_pass_cube is None for p in P.PRESETS.values()),
          "no built-in splits its first pass")
    check(P.PRESETS["deep"].first_pass_cube is None
          and P.PRESETS["deep"].cube_first_pass == "3ply",
          "an unsplit first pass serves both kinds")
    got, *_rest, err = config(
        "presets:\n"
        "  bare: {first_pass: 3ply, second_pass: truncated2}\n"
        "  same: {first_pass: {checker: 3ply, cube: 3ply}, second_pass: truncated2}\n"
        "  split: {first_pass: {checker: 3ply, cube: 4ply}, second_pass: truncated2}\n")
    check(got["bare"].digest() == got["same"].digest(),
          "a mapping naming one level for both is the bare level's digest")
    check(got["split"].digest() != got["bare"].digest()
          and got["split"].cube_first_pass == "4ply",
          "a split first pass is its own definition")
    for text, what in (
        ("presets:\n  mine: {first_pass: {cube: 4ply}}\n", "a first pass with no checker level"),
        ("presets:\n  mine: {first_pass: {checker: 3ply, cube: 5ply}}\n", "an invalid cube level"),
        ("presets:\n  mine: {first_pass: {checker: 3ply, move: 4ply}}\n", "an unknown kind"),
    ):
        got, *_rest, err = config(text)
        check("mine" not in got and "ignoring" in err, f"refused: {what}", err)


def check_second_pass_split() -> None:
    """A per-kind second pass, spelled and digested the way the first is."""
    check(all(p.second_pass_cube is None for p in P.PRESETS.values()),
          "no built-in splits its second pass")
    check(P.PRESETS["world_class"].cube_second_pass == "truncated2"
          and P.PRESETS["deep"].cube_second_pass is None,
          "an unsplit second pass serves both kinds, and a single pass has none")
    check(P.PRESETS["world_class"].definition()["second_pass"] == "truncated2",
          "an unsplit second pass stays a bare level in the definition")
    got, *_rest, err = config(
        "presets:\n"
        "  bare: {first_pass: 3ply, second_pass: truncated2}\n"
        "  same: {first_pass: 3ply, second_pass: {checker: truncated2, cube: truncated2}}\n"
        "  only: {first_pass: 3ply, second_pass: {checker: truncated2}}\n"
        "  split: {first_pass: 3ply, second_pass: {checker: 4ply, cube: truncated3}}\n")
    check(got["bare"].digest() == got["same"].digest() == got["only"].digest(),
          "a mapping naming one level for both, or no cube level, is the bare level's digest", err)
    check(got["split"].digest() != got["bare"].digest()
          and got["split"].second_pass == "4ply"
          and got["split"].cube_second_pass == "truncated3"
          and got["split"].definition()["second_pass"] == ["4ply", "truncated3"],
          "a split second pass is its own definition")
    mine = P.custom_preset("mine", {"first_pass": "3ply",
                                    "second_pass": {"checker": "4ply", "cube": "truncated3"}})
    check(mine.label == P.custom_preset("mine", {
              "first_pass": "3ply", "second_pass": {"checker": "4ply", "cube": "truncated3"}}).label
          and mine.digest() == got["split"].digest(),
          "the same split from data and from a file is the same definition")
    for text, what in (
        ("presets:\n  mine: {first_pass: 3ply, second_pass: {cube: truncated2}}\n",
         "a second pass with no checker level"),
        ("presets:\n  mine: {first_pass: 3ply, second_pass: {checker: 3ply, cube: 5ply}}\n",
         "an invalid cube level"),
        ("presets:\n  mine: {first_pass: 3ply, second_pass: {checker: 3ply, move: 4ply}}\n",
         "an unknown kind"),
    ):
        got, *_rest, err = config(text)
        check("mine" not in got and "ignoring" in err, f"refused: {what}", err)


def check_resign_option() -> None:
    """Resign errors are an option, never part of a preset: off in every
    built-in, named in the label when on, and outside what a pin names."""
    check(not any(p.resign_errors for p in P.PRESETS.values()),
          "no built-in judges resignations")
    wc = P.PRESETS["world_class"]
    on = wc.with_resign_errors()
    check(on.label == "world_class@2+resign" and on.method_label == wc.label
          and on.digest() == wc.digest(),
          f"the option shows in the label, not the definition ({on.label})")
    got, *_rest, err = config("presets:\n  mine: {first_pass: 3ply, resign_errors: true}\n")
    check("mine" not in got and "analysis option" in err,
          "a preset cannot turn resign errors on", err)


def check_custom_from_data() -> None:
    """`custom_preset` is one presets.yaml entry, passed as data."""
    spec = {"first_pass": "3ply", "mid_pass": {"checker": "4ply"}, "second_pass": "truncated2",
            "close_threshold": 0.08, "error_threshold": 0.02, "display": "Mine"}
    mine = P.custom_preset("Mine", spec)
    from_file, *_rest, err = config(
        "presets:\n"
        "  mine: {first_pass: 3ply, mid_pass: {checker: 4ply}, second_pass: truncated2,\n"
        "         close_threshold: 0.08, error_threshold: 0.02}\n")
    check(mine.key == "mine" and mine.display == "Mine" and mine.revision is None,
          "the name is normalised the way a file's key is")
    check(mine.label == from_file["mine"].label and mine.label.startswith("custom/mine@"),
          f"the same definition is labelled the same from data and from a file ({mine.label})", err)
    check(P.custom_preset("other", {**spec, "display": "Something else"}).digest() == mine.digest(),
          "neither the name nor the display is part of the definition")
    check(P.resolve_preset(mine) is mine, "resolve_preset hands a Preset back unchanged")
    check(P.custom_preset("x", {"first_pass": "2ply"}).second_pass is None,
          "a single-pass definition is accepted")
    for name, bad, what in (
        ("world_class", {"first_pass": "3ply"}, "a built-in name"),
        ("wc", {"first_pass": "3ply"}, "a built-in alias"),
        ("balanced", {"first_pass": "3ply"}, "a retired name"),
        ("My Preset", {"first_pass": "3ply"}, "a name the label cannot carry"),
        ("mine", {"first_pass": "3ply", "revision": 1}, "a revision"),
        ("mine", {"first_pass": "3ply", "resign_errors": True}, "resign errors"),
        ("mine", {"first_pass": "5ply"}, "an invalid level"),
        ("mine", {"first_pass": "3ply", "mid_pass": "4ply"}, "a middle pass with no second pass"),
        ("mine", {"first_pass": "3ply", "mid_pass": "4ply", "second_pass": "truncated2"},
         "a middle pass with no close threshold"),
        ("mine", ["3ply"], "a definition that is not a mapping"),
    ):
        try:
            P.custom_preset(name, bad)
            check(False, f"custom_preset refuses {what}")
        except P.PresetConfigError as e:
            check(True, f"custom_preset refuses {what}: {e}")
    import gvanalysis
    check(gvanalysis.custom_preset is P.custom_preset
          and gvanalysis.PresetConfigError is P.PresetConfigError,
          "the package exports custom_preset and PresetConfigError")


def check_pins() -> None:
    check(P.resolve_preset("world_class@2").key == "world_class", "world_class@2 resolves")
    check(P.resolve_preset("WC@2").key == "world_class", "a pin works through an alias")
    check(P.resolve_preset("very_quick@1").key == "very_quick", "very_quick@1 resolves")
    for name, frag in (("world_class@3", "newer revision"),
                       ("world_class@1", "earlier revision"),
                       ("world_class@0", "not a revision"),
                       ("world_class@x", "revision number")):
        try:
            P.resolve_preset(name)
            check(False, f"{name} refused")
        except ValueError as e:
            check(frag in str(e), f"{name} refused: {e}")
    for name in ("world_class_fast", "wcf", "wcf@2", "balanced"):
        try:
            P.resolve_preset(name)
            check(False, f"retired {name} refused")
        except ValueError as e:
            check("retired" in str(e) and "Unknown" not in str(e), f"retired {name} refused: {e}")
    got, *_rest, err = config("presets:\n  wcf: {first_pass: 3ply}\n")
    check("wcf" not in got and "wcf" in err, "a retired name cannot be reused for a custom preset", err)
    with env(GVAN_STAGED="0"):
        try:
            P.resolve_preset("world_class@2")
            check(False, "a modified built-in refuses its pin")
        except ValueError as e:
            check("modified" in str(e), f"a modified built-in refuses its pin: {e}")


def main() -> int:
    check_revisions_pinned()
    check_digest_scope()
    check_builtins_locked()
    check_first_pass_split()
    check_resign_option()
    check_pins()
    check_custom_from_data()
    check_second_pass_split()
    print(f"\n{'all passed' if not failures else str(failures) + ' failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
