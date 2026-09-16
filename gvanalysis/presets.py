# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""eXtreme Gammon (XG) style analysis presets.

Each preset is a two-pass scheme: a cheap first pass evaluates every decision,
and a stronger second pass runs only on an error -- the played checker move or
cube action disagrees with the first pass (matching XG World Class, which only
rolls out cube decisions on a cube error). A preset with no second pass is a
single-pass scheme that judges everything at the first-pass level.

The canonical presets are defined in code here (`_BUILTIN_SPECS`), so they
are always available and can never be broken by editing. Users customize by
creating an optional `presets.yaml` next to this file: entries there override a
built-in preset by key or add brand-new presets, and the file's `default:` key
overrides the default preset. To restore defaults, just delete presets.yaml; to
start one, run `gvan-match --init-presets`. A malformed presets.yaml is
ignored with a warning (the built-ins still load).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# Valid bgsage eval levels, shallow -> deep. Kept in sync with the eval levels
# documented in CLAUDE.md.
VALID_LEVELS: frozenset[str] = frozenset({
    "1ply", "2ply", "3ply", "4ply",
    "truncated1", "truncated2", "truncated3", "rollout",
})

# Canonical built-in presets (spec dicts, same shape as presets.yaml entries).
# These are the guaranteed baseline. second_pass None => single pass; mid_pass +
# close_threshold add the optional 3-tier scheme (checker plays and cubes).
_BUILTIN_SPECS: dict[str, dict] = {
    "very_quick": {"display": "Very quick", "first_pass": "2ply", "second_pass": None, "aliases": ["vq"]},
    "fast": {"display": "Fast", "first_pass": "2ply", "second_pass": "3ply", "aliases": ["f"]},
    "deep": {"display": "Deep", "first_pass": "3ply", "second_pass": None, "aliases": ["d"]},
    # 3-tier balanced scheme (quality/speed, not an XG analog): cheap 2-ply
    # screen on every decision, 3-ply to resolve near-ties (cheap enough to keep
    # a wide threshold), truncated2 to size genuine errors. The sizing tier was
    # 4-ply until both were scored against an independent truncated3 arbiter on
    # 175 real sizing-tier checker errors: truncated2 landed nearer the arbiter
    # on 123 of them, mean gap 0.0095 against 4-ply's 0.0132 (the cube arbiter
    # test behind world_class_fast found 0.0091 vs 0.0127 -- the same result on
    # the other decision kind). Being the better estimator on exactly the
    # decisions the tier exists for is now the ONLY reason for the choice. It
    # used to be the cheaper one too: before checker_eval.py screened checker
    # plays, full-width 4-ply over a whole move list cost more than a 360-trial
    # truncated rollout, and swapping the rollout in measured slightly faster.
    # Screening made 4-ply 1.6x cheaper and inverted that -- the rollout sizing
    # tier now costs ~13% more wall clock than 4-ply would (38.9s vs 34.3s on
    # three matches). The accuracy is worth the seconds. Fixed seed, fixed trial
    # count, fixed truncation depth, so cost stays bounded and cacheable.
    "balanced": {"display": "Balanced", "first_pass": "2ply", "mid_pass": "3ply",
                 "second_pass": "truncated2", "close_threshold": 0.04, "aliases": ["b"]},
    "world_class": {"display": "World Class", "first_pass": "4ply", "second_pass": "truncated2", "aliases": ["wc", "worldclass"]},
    # 3-tier XG World Class analog: 3-ply screen, truncated2 rollout on error,
    # and a middle tier on cubes only. The checker middle tier was dropped after
    # measuring it: 4-ply on a whole move list costs ~19x the 3-ply screen it
    # would be added to, and most of what it rescued was a decision the rollout
    # above it would have taken anyway. Re-measured once checker_eval.py made
    # 4-ply 1.6x cheaper, it still does not pay: restoring it runs the preset
    # 48% slower (142.7s vs 96.4s on three matches) AND diverts 65 of the 83
    # checker rollouts into 4-ply instead -- the weaker estimator by the arbiter
    # test above. Dearer and shallower at once, so it stays gone. Cubes keep
    # a middle tier, but it is truncated2 rather than 4-ply: a borderline cube is
    # the one place 4-ply looked like a bargain, and against an independent
    # truncated3 arbiter it still lost to the rollout, at a difference in cost
    # too small to buy the accuracy back. So the cube tier is really "borderline
    # OR wrong -> roll it out"; the middle tier survives only to widen what
    # counts as worth rolling out, not to name a shallower level.
    "world_class_fast": {"display": "World Class Fast", "first_pass": "3ply",
                         "mid_pass": {"cube": "truncated2"},
                         "second_pass": "truncated2", "close_threshold": 0.04, "aliases": ["wcf"]},
    # TODO Extensive: needs a full-completion `rollout` second pass plus a tiered
    # outplay-vs-error escalation trigger, so it is not yet defined.
}
_BUILTIN_DEFAULT = "fast"

# Optional user overrides are read from two locations, lowest precedence first:
#   1. a global per-user file (~/.config/bgsage/presets.yaml, or $XDG_CONFIG_HOME)
#   2. a project-local file next to this module (./presets.yaml)
# Both are layered on top of the built-ins; the project-local file wins on
# conflicts, so it can override the global file which overrides the built-ins.
def _global_presets_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "bgsage" / "presets.yaml"


GLOBAL_PRESETS_FILE = _global_presets_file()
PROJECT_PRESETS_FILE = Path(__file__).resolve().parent / "presets.yaml"
PRESET_FILES: list[Path] = [GLOBAL_PRESETS_FILE, PROJECT_PRESETS_FILE]  # low -> high

# Starter file written by --init-presets. Documents the schema; the built-ins
# above always apply even when this file is absent, so it starts empty.
TEMPLATE = """\
# Custom analysis presets for gvan-match (optional).
#
# The built-in presets always apply, so this file is only for OVERRIDING a
# built-in (reuse its key) or ADDING your own. Delete this file to restore
# defaults. A malformed file is ignored with a warning.
#
# This file is read from two locations (project-local wins over global):
#   - global:  ~/.config/bgsage/presets.yaml
#   - project: ./presets.yaml (next to the scripts)
#
# Built-in presets: very_quick (vq), fast (f), deep (d), balanced (b),
#                   world_class (wc), world_class_fast (wcf). Default: fast.
#
# Each preset is a two-pass scheme: a cheap first_pass screens every decision,
# a stronger second_pass runs only on an error (the played checker move or cube
# action disagrees with the first pass). Omit second_pass (null) for single-pass.
#
# Optional 3-tier: add mid_pass + close_threshold to deepen a borderline
# decision to mid_pass instead of leaving it at the screen. Borderline means the
# top two checker moves are within close_threshold, or a cube is that close to
# its double point / take point. mid_pass is either a level (both decision kinds
# get that middle tier, as in balanced) or a {checker, cube} mapping naming one
# per kind, since the same level is not equally cheap on a move list and on a
# single cube -- see world_class_fast, which gives cubes a middle tier and
# leaves checker plays 2-tier. An omitted kind stays plain 2-tier. Naming the
# same level as second_pass is allowed and costs nothing extra (one analyzer is
# built and shared); it means "borderline as well as wrong earns the top tier".
#
# Valid levels (shallow -> deep):
#   1ply, 2ply, 3ply, 4ply, truncated1, truncated2, truncated3, rollout

# Uncomment to change which preset runs when --preset is omitted:
# default: world_class

presets: {}
  # my_preset:
  #   display: My Preset
  #   first_pass: 3ply
  #   second_pass: truncated2   # omit or null for a single-pass preset
  #   aliases: [mp]
"""


@dataclass(frozen=True)
class Preset:
    key: str                   # canonical, e.g. "world_class"
    display: str               # "World Class"
    first_pass: str            # bgsage eval_level for pass 1 (the screen)
    second_pass: str | None    # None => single pass; else the on-error tier
    # Optional 3-tier scheme (XG World Class style): a decision the first pass
    # calls borderline -- top-2 moves within close_threshold, or a cube within it
    # of its double/take point -- is deepened to that kind's mid pass instead of
    # the rollout; errors bigger than close_threshold still go to second_pass.
    # Set per decision kind: a checker play and a cube have opposite cost
    # profiles at the same level. A middle tier is added on top of the screen
    # -- it fires on decisions the screen would otherwise have settled -- so
    # what decides whether it pays is its cost against the SCREEN BELOW, not
    # against the tier above. Measured per decision off a 3-ply screen: 4-ply
    # is 19x the screen on a move list (25ms -> 467ms) but only 4x on one
    # pre-roll cube (24ms -> 100ms). So each kind names its own middle tier;
    # either may be None, leaving that kind plain 2-tier.
    mid_pass_checker: str | None = None    # e.g. "4ply"; None => 2-tier checker
    mid_pass_cube: str | None = None       # e.g. "4ply"; None => 2-tier cube
    close_threshold: float | None = None   # equity gap gating the mid tier

    @property
    def has_mid(self) -> bool:
        """True if either decision kind has a middle tier."""
        return self.mid_pass_checker is not None or self.mid_pass_cube is not None


class PresetConfigError(ValueError):
    """Raised when a preset spec (built-in or from presets.yaml) is invalid."""


_MID_KINDS = ("checker", "cube")


def _mid_levels(key: str, spec: dict) -> tuple[str | None, str | None]:
    """Resolve `mid_pass` into (checker_level, cube_level).

    A bare level string sets the middle tier for both decision kinds (the
    original shape, so every existing preset and presets.yaml still means what
    it did). A `{checker: ..., cube: ...}` mapping sets them separately, and a
    null or omitted key leaves that kind plain 2-tier -- which is the point of
    the mapping: measured against the 3-ply screen a middle tier sits on,
    4-ply is 4x on a cube and 19x on a move list, so the tier that pays for
    itself is not the same one for both.
    """
    raw = spec.get("mid_pass")
    if raw is None:
        return None, None
    if isinstance(raw, str):
        return raw, raw
    if isinstance(raw, dict):
        unknown = sorted(str(k) for k in raw if k not in _MID_KINDS)
        if unknown:
            raise PresetConfigError(
                f"preset {key!r}: mid_pass keys must be "
                f"{' / '.join(map(repr, _MID_KINDS))}, got {', '.join(unknown)}"
            )
        return raw.get("checker"), raw.get("cube")
    raise PresetConfigError(
        f"preset {key!r}: mid_pass must be a level or a "
        f"{{checker, cube}} mapping, got {type(raw).__name__}"
    )


def _make_preset(key: str, spec: dict) -> Preset:
    """Validate one preset spec dict into a Preset. Raises PresetConfigError."""
    first_pass = spec.get("first_pass")
    second_pass = spec.get("second_pass")  # may be absent/null
    mid_checker, mid_cube = _mid_levels(key, spec)
    close_threshold = spec.get("close_threshold")
    for label, lvl in (("first_pass", first_pass), ("second_pass", second_pass),
                       ("mid_pass.checker", mid_checker), ("mid_pass.cube", mid_cube)):
        if lvl is None and label != "first_pass":
            continue
        if lvl not in VALID_LEVELS:
            raise PresetConfigError(
                f"preset {key!r}: {label} {lvl!r} is not a valid level "
                f"(valid: {', '.join(sorted(VALID_LEVELS))})"
            )
    has_mid = mid_checker is not None or mid_cube is not None
    if has_mid:
        if second_pass is None:
            raise PresetConfigError(
                f"preset {key!r}: mid_pass requires second_pass (3-tier needs a "
                f"rollout tier for errors)"
            )
        if not isinstance(close_threshold, (int, float)) or close_threshold <= 0:
            raise PresetConfigError(
                f"preset {key!r}: mid_pass requires a positive close_threshold "
                f"(equity gap gating the middle tier)"
            )
    return Preset(key, str(spec.get("display", key)), first_pass, second_pass,
                  mid_checker, mid_cube,
                  float(close_threshold) if has_mid else None)


def _assemble(specs: dict[str, dict], default: str) -> tuple[dict[str, Preset], dict[str, str], str]:
    """Build (presets, aliases, default_key) from spec dicts. Raises on any
    invalid level, alias collision, or unknown default."""
    presets: dict[str, Preset] = {}
    aliases: dict[str, str] = {}
    for key, spec in specs.items():
        key = str(key).strip().lower()
        presets[key] = _make_preset(key, spec)
        aliases[key] = key
    # Aliases in a second pass so they never shadow a canonical key.
    for key, spec in specs.items():
        key = str(key).strip().lower()
        for alias in spec.get("aliases") or []:
            alias = str(alias).strip().lower()
            if alias in presets and alias != key:
                raise PresetConfigError(f"alias {alias!r} collides with preset {alias!r}")
            if aliases.get(alias, key) != key:
                raise PresetConfigError(
                    f"alias {alias!r} is claimed by both {aliases[alias]!r} and {key!r}"
                )
            aliases[alias] = key
    default_key = aliases.get(str(default).strip().lower(), str(default).strip().lower())
    if default_key not in presets:
        raise PresetConfigError(f"default {default!r} is not a defined preset")
    return presets, aliases, default_key


def _builtin_specs() -> dict[str, dict]:
    return {k: {**spec, "aliases": list(spec.get("aliases") or [])}
            for k, spec in _BUILTIN_SPECS.items()}


def _overlay_file(specs: dict[str, dict], default: str, path: Path) -> tuple[dict[str, dict], str]:
    """Merge one presets.yaml onto the accumulated specs. Raises PresetConfigError
    (or yaml.YAMLError) if the file is malformed; the caller decides whether to
    skip it. Overriding an existing key merges field-by-field; new keys are added.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:  # empty file
        return specs, default
    if not isinstance(raw, dict):
        raise PresetConfigError("top-level content must be a mapping")
    user_presets = raw.get("presets") or {}
    if not isinstance(user_presets, dict):
        raise PresetConfigError("'presets:' must be a mapping")
    merged = dict(specs)
    for key, spec in user_presets.items():
        key = str(key).strip().lower()
        if not isinstance(spec, dict):
            raise PresetConfigError(f"preset {key!r} must be a mapping")
        merged[key] = {**merged[key], **spec} if key in merged else spec
    return merged, raw.get("default", default)


def _build_config(paths: list[Path]) -> tuple[dict[str, Preset], dict[str, str], str]:
    """Built-in presets overlaid with each user presets.yaml, in order.

    The built-ins are a guaranteed-valid baseline. Each existing file is layered
    on top and validated; a missing file is skipped and a malformed one is
    ignored with a warning (later files still apply).
    """
    specs = _builtin_specs()
    default = _BUILTIN_DEFAULT
    for path in paths:
        if not path.exists():
            continue
        try:
            candidate, cand_default = _overlay_file(specs, default, path)
            _assemble(candidate, cand_default)  # validate before committing
            specs, default = candidate, cand_default
        except (yaml.YAMLError, PresetConfigError) as e:
            print(f"warning: ignoring {path} ({e})", file=sys.stderr)
    return _assemble(specs, default)


def write_template(path: Path = PROJECT_PRESETS_FILE, force: bool = False) -> Path:
    """Write the starter presets.yaml, creating parent dirs as needed. Raises
    FileExistsError unless force."""
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path


PRESETS, _ALIASES, DEFAULT_PRESET = _build_config(PRESET_FILES)


def resolve_preset(name: str | None) -> Preset:
    """Resolve a canonical key or alias (case-insensitive) to a Preset.

    Pass None to get the configured default preset. Raises ValueError listing
    the valid presets for an unknown name.
    """
    if name is None:
        return PRESETS[DEFAULT_PRESET]
    key = name.strip().lower()
    key = _ALIASES.get(key, key)
    if key not in PRESETS:
        valid = ", ".join(sorted(PRESETS))
        raise ValueError(f"Unknown preset {name!r}. Valid presets: {valid}")
    return PRESETS[key]
