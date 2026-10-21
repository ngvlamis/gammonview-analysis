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
  * a pinned name (``world_class@2``) runs that revision or nothing.

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
        "  wcf: {first_pass: 2ply}\n"
        "  mine: {first_pass: 3ply, second_pass: truncated2}\n")
    check(presets["world_class"].error_threshold_checker == 0.02
          and presets["world_class_fast"].first_pass == "3ply",
          "a built-in (by name or alias) cannot be overridden")
    check("world_class" in err and "wcf" in err, "each skipped override is warned about", err)
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
    print(f"\n{'all passed' if not failures else str(failures) + ' failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
