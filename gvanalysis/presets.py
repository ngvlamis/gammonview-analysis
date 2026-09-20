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
# close_threshold add the optional 3-tier scheme (checker plays and cubes);
# error_threshold is what a decision has to cost to earn the second pass.
_BUILTIN_SPECS: dict[str, dict] = {
    "very_quick": {"display": "Very quick", "first_pass": "2ply", "second_pass": None, "aliases": ["vq"]},
    # error_threshold 0, not the 0.02 default, because `fast`'s second pass is
    # 3-ply rather than a rollout. The default exists to RATION ROLLOUTS -- on a
    # checker move list a rollout costs ~1.2s against a 3-ply look's ~0.036s, so
    # an error too small for a rollout to size usefully is worth leaving at the
    # screen. Here the second pass costs ~6x the screen, not ~200x, and the
    # trade goes the other way: measured over the five golden matches, 0.02 saves
    # 6% of wall clock (40.7s -> 38.2s) and moves PR by a mean of 0.14 (max
    # 0.35). Cheap depth is worth taking on every error.
    "fast": {"display": "Fast", "first_pass": "2ply", "second_pass": "3ply",
             "error_threshold": 0.0, "aliases": ["f"]},
    "deep": {"display": "Deep", "first_pass": "3ply", "second_pass": None, "aliases": ["d"]},
    # XG World Class, modelled on XG's ROUTING rather than on a single depth.
    # This shipped for a long time as a flat 4-ply screen, which was never what
    # XG does. Mining 63,548 XG World Class checker decisions for the level it
    # actually used, bucketed by top-2 gap:
    #
    #     gap < 0.005     93.7% 4-ply    1.1% 3-ply
    #     gap 0.05-0.08   64.9% 4-ply    0.2% 3-ply
    #     gap 0.08-0.12   15.0% 4-ply   57.5% 3-ply
    #     gap 0.12 +       8.3% 4-ply   74.2% 3-ply
    #
    # XG's DEFAULT is 3-ply; 4-ply is what it spends on a decision near enough
    # to a tie that depth is what settles it, and the crossover is at 0.08. So
    # the faithful analog is a 3-ply screen with a 4-ply middle tier on
    # near-ties, not 4-ply everywhere -- and it is cheaper, since the 4-ply pass
    # now runs on the ~54% of checker decisions that are borderline at 0.08
    # instead of all of them. Errors above 0.02 still go to the rollout, from
    # whichever tier last looked at them.
    #
    # Measured against the flat-4-ply shape on three matches, 353 checker
    # decisions, serial: 104.9 -> 55.7 s/match, a 1.88x speedup. It comes from
    # both tiers at once --
    #
    #     flat 4-ply   4-ply 71.1%   rollout 28.9%
    #     3+4-ply      3-ply 37.4%   4-ply 49.0%   rollout 13.6%
    #
    # -- the full-width 4-ply pass falling to half the decisions, and the
    # rollout share halving again because the old shape had no error_threshold
    # and rolled out on ANY disagreement. Agreement with XG moved the right way
    # on the same three matches (MAE 0.5087 -> 0.4505), but three matches
    # cannot resolve that: the 95% CI is [-0.0370, +0.1735]. The speedup is the
    # measured claim here; the accuracy is not yet.
    #
    # Unlike world_class_fast, the cube middle tier here is 4-ply rather than
    # truncated2, so both kinds deepen before they roll out. That is safe only
    # because a borderline decision now ESCALATES: the arbiter test that made
    # 4-ply the weaker cube estimator (14% wrong verdicts, mean gap error
    # 0.0127, against truncated2's 12%/0.0091) was scoring it as a TERMINAL
    # tier, which it no longer is. A borderline cube the 4-ply tier then sizes
    # above error_threshold goes to truncated2 on 4-ply's own numbers.
    "world_class": {"display": "World Class", "first_pass": "3ply",
                    "mid_pass": "4ply", "second_pass": "truncated2",
                    "close_threshold": 0.08, "aliases": ["wc", "worldclass"]},
    # 3-tier XG World Class analog: 3-ply screen, truncated2 rollout on error,
    # and a middle tier on cubes only.
    #
    # The checker middle tier stays gone, but on one argument rather than the
    # two it used to rest on. What decides it is how often "borderline" fires:
    # at a 0.04 window a cube is borderline ~5% of the time and a checker play
    # 53.8% of the time, so the same rule costs ~0.4s per match on cubes and
    # ~42-91s per match on checker plays depending on the level named. The
    # second argument -- that restoring it "diverts 65 of the 83 checker
    # rollouts into 4-ply, the weaker estimator" -- described the escalation
    # defect, not the tier: a borderline decision used to be capped at the
    # middle tier however large its error turned out to be. It escalates now,
    # so a middle tier no longer steals rollouts. It would still cost, and
    # more than before, since those decisions would pay both tiers.
    #
    # (Also note the cost order has flipped since that measurement: on 760
    # paired checker decisions 4-ply is faster than truncated2 on 710 of them,
    # median 2.23x, because checker_eval.py screens candidates before the
    # full-width pass. The old "4-ply on a move list is the dear one" framing
    # no longer holds -- the borderline rate is the whole argument.)
    #
    # Cubes keep a middle tier, and it is truncated2 rather than 4-ply: a
    # borderline cube is the one place 4-ply looked like a bargain, and against
    # a truncated3 arbiter it still lost to the rollout, at a difference in cost
    # too small to buy the accuracy back. So the cube tier is really "borderline
    # OR wrong -> roll it out"; the middle tier survives only to widen what
    # counts as worth rolling out, not to name a shallower level. Because it
    # names second_pass's own level, the escalation re-check is a no-op here --
    # the analyzers are one object and game_eval skips the second call.
    #
    # close_threshold is 0.08 for the same reason world_class's is: it is XG's
    # own depth crossover, and there is no principled reason the cube boundary
    # should sit at half the checker one. Only the cube value bites here (no
    # checker middle tier for the checker one to gate). Measured, 33 holdout
    # matches paired against 0.04 on the same matches: MAE 0.4826 -> 0.4840,
    # 95% CI [-0.0235, +0.0192], wall +1.5%, and 2 of 33 matches produced any PR
    # change at all. So this is adopted as a coherence fix, NOT a measured gain
    # -- 489 of 686 cube decisions already reached the rollout at 0.04, leaving
    # about one per match that widening can touch. What the test does establish
    # is that it is not harmful. The corpus mining points the same way: missed
    # errors fall from 7.2% of mid-tier cubes at 0.04 to 3.3% at 0.08.
    "world_class_fast": {"display": "World Class Fast", "first_pass": "3ply",
                         "mid_pass": {"cube": "truncated2"},
                         "second_pass": "truncated2", "close_threshold": 0.08, "aliases": ["wcf"]},
    # TODO Extensive: needs a full-completion `rollout` second pass plus a tiered
    # outplay-vs-error escalation trigger, so it is not yet defined.
}
_BUILTIN_DEFAULT = "fast"

#: Presets that were built in and no longer are, mapped to what to say instead.
#: A removed preset would otherwise fail as a plain "unknown preset" alongside
#: names that were never real, which tells a user with `--preset balanced` in a
#: script that they made a typo. Deliberately NOT silently aliased to its
#: replacement: the preset name is recorded in the output document, so a quiet
#: redirect would label the analysis as something it is not.
_RETIRED: dict[str, str] = {
    "balanced": (
        "retired after 1.0.0 -- it cost about what world_class_fast costs "
        "while agreeing with XG less often. Use world_class_fast for analysis "
        "you intend to trust, or deep for something genuinely cheap."
    ),
    "b": "balanced",   # its alias, resolved to the entry above
}

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
# Built-in presets: very_quick (vq), fast (f), deep (d),
#                   world_class (wc), world_class_fast (wcf). Default: fast.
#
# Each preset is a two-pass scheme: a cheap first_pass screens every decision,
# a stronger second_pass runs only on an error (the played checker move or cube
# action disagrees with the first pass, by more than error_threshold). Omit
# second_pass (null) for single-pass.
#
# The two tiers answer two different questions, so they have two thresholds:
#
#   mid_pass   is a SECOND LOOK BEFORE JUDGING. It fires on a decision the
#              screen cannot call confidently -- the top two checker moves
#              within close_threshold, or a cube that close to its double or
#              take point -- and its job is to settle it.
#   second_pass is SIZING AN ERROR YOU ALREADY BELIEVE IN. It fires when a
#              decision is wrong by more than error_threshold, where the
#              magnitude is what a rollout can actually measure.
#
# A decision reaches second_pass straight from the screen, or by way of
# mid_pass when the closer look is what revealed the error.
#
# Pick the two independently: a middle tier is cheap (a 3-ply look at a move
# list costs ~0.036s) and a rollout is not (~1.2s), so close_threshold can
# afford to be generous where error_threshold cannot. error_threshold defaults
# to 0.02, the standard cutoff below which an error is not worth sizing
# precisely.
#
# Optional 3-tier: add mid_pass + close_threshold. mid_pass is either a level
# (both decision kinds get that middle tier, as in world_class) or a
# {checker, cube} mapping naming one per kind, since the same level is not
# equally cheap on a move list and on a single cube -- see world_class_fast,
# which gives cubes a middle tier and leaves checker plays 2-tier. An omitted
# kind stays plain 2-tier. close_threshold and error_threshold take the same
# bare-value-or-{checker, cube}-mapping shape. Naming the same level as
# second_pass in mid_pass is allowed and costs nothing extra (one analyzer is
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
  #   error_threshold: 0.02     # error worth a rollout (default 0.02)
  #   aliases: [mp]
  # my_3tier:
  #   display: My 3-tier Preset
  #   first_pass: 2ply
  #   mid_pass: {checker: 3ply, cube: truncated2}
  #   second_pass: truncated2
  #   close_threshold: {checker: 0.08, cube: 0.04}
  #   error_threshold: 0.02
"""


@dataclass(frozen=True)
class Preset:
    key: str                   # canonical, e.g. "world_class"
    display: str               # "World Class"
    first_pass: str            # bgsage eval_level for pass 1 (the screen)
    second_pass: str | None    # None => single pass; else the on-error tier
    # Optional 3-tier scheme (XG World Class style): a decision the first pass
    # calls borderline -- top-2 moves within close_threshold, or a cube within
    # it of its double/take point -- is deepened to that kind's mid pass. An
    # error costing more than error_threshold goes to second_pass instead, and
    # a borderline decision the middle tier then finds to be such an error is
    # escalated there too. (Until Sep 2026 that last rule was documented but
    # only implemented for cubes, and even there only off the screen's numbers:
    # a borderline checker play was capped at the middle tier however large its
    # error proved to be. 10% of borderline checker plays cost more than the
    # threshold; the worst cost 0.56.)
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
    # Two thresholds, not one. `close_threshold` gates the MIDDLE tier: how
    # near a tie a decision has to be before the screen's verdict stops being
    # trusted. `error_threshold` gates the SECOND pass: how much a decision has
    # to cost before its size is worth a rollout. They answer different
    # questions and want different values -- measured against XG's own routing
    # over 78,661 decisions, the depth crossover sits near 0.08 and the rollout
    # cliff at 0.02 -- and they have wildly different price tags, since a
    # middle tier is ~34x cheaper than a rollout on a checker move list. Both
    # are per decision kind for the same reason `mid_pass` is.
    close_threshold_checker: float | None = None
    close_threshold_cube: float | None = None
    error_threshold_checker: float = 0.02
    error_threshold_cube: float = 0.02

    @property
    def has_mid(self) -> bool:
        """True if either decision kind has a middle tier."""
        return self.mid_pass_checker is not None or self.mid_pass_cube is not None

    @property
    def close_threshold(self) -> float | None:
        """The shared close threshold, or None when the kinds differ.

        Back-compatible read for callers (and tests) written before the
        threshold was split per decision kind; reporting code uses it to print
        one number when there is only one to print.
        """
        if self.close_threshold_checker == self.close_threshold_cube:
            return self.close_threshold_checker
        return None


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


DEFAULT_ERROR_THRESHOLD = 0.02
"""Default `error_threshold`: the standard cutoff below which an error is not
worth sizing precisely.

Three independent measurements land on this number. XG's own World Class
routing rolls out 22.9% of decisions whose error is 0.015-0.020 and 94.7% of
those in 0.020-0.030 -- a cliff, not a ramp. Scored against XG as referee, a
rollout improves error sizing by +8.3% below 0.02 and +31.9% above it. And
across a 101-match corpus the errors under 0.02 are 43-46% of everything a
screen flags while carrying 7-8% of the error equity: nearly half the rollouts
for a PR fidelity cost of roughly 0.02.
"""


def _kind_thresholds(key: str, spec: dict, field: str,
                     default: float | None) -> tuple[float | None, float | None]:
    """Resolve a threshold field into (checker, cube).

    A bare number sets both kinds (the original shape, so every preset and
    presets.yaml written before the split still means what it did); a
    `{checker, cube}` mapping sets them separately, exactly as `mid_pass`
    does. An omitted kind falls back to `default`.
    """
    raw = spec.get(field, None)
    if raw is None:
        return default, default
    if isinstance(raw, bool):  # bool is an int subclass; never a threshold
        raise PresetConfigError(f"preset {key!r}: {field} must be a number")
    if isinstance(raw, (int, float)):
        return float(raw), float(raw)
    if isinstance(raw, dict):
        unknown = sorted(str(k) for k in raw if k not in _MID_KINDS)
        if unknown:
            raise PresetConfigError(
                f"preset {key!r}: {field} keys must be "
                f"{' / '.join(map(repr, _MID_KINDS))}, got {', '.join(unknown)}"
            )
        out = []
        for kind in _MID_KINDS:
            v = raw.get(kind, default)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise PresetConfigError(
                    f"preset {key!r}: {field}.{kind} must be a number")
            out.append(float(v) if v is not None else None)
        return out[0], out[1]
    raise PresetConfigError(
        f"preset {key!r}: {field} must be a number or a "
        f"{{checker, cube}} mapping, got {type(raw).__name__}"
    )


def _make_preset(key: str, spec: dict) -> Preset:
    """Validate one preset spec dict into a Preset. Raises PresetConfigError."""
    first_pass = spec.get("first_pass")
    second_pass = spec.get("second_pass")  # may be absent/null
    mid_checker, mid_cube = _mid_levels(key, spec)
    close_checker, close_cube = _kind_thresholds(key, spec, "close_threshold", None)
    err_checker, err_cube = _kind_thresholds(key, spec, "error_threshold",
                                             DEFAULT_ERROR_THRESHOLD)
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
    if has_mid and second_pass is None:
        raise PresetConfigError(
            f"preset {key!r}: mid_pass requires second_pass (3-tier needs a "
            f"rollout tier for errors)"
        )
    # A middle tier needs a threshold to fire on; a kind without one does not.
    for kind, mid, close in (("checker", mid_checker, close_checker),
                             ("cube", mid_cube, close_cube)):
        if mid is not None and not (isinstance(close, float) and close > 0):
            raise PresetConfigError(
                f"preset {key!r}: mid_pass.{kind} requires a positive "
                f"close_threshold (the equity gap gating the middle tier)"
            )
    for kind, err in (("checker", err_checker), ("cube", err_cube)):
        if err is not None and err < 0:
            raise PresetConfigError(
                f"preset {key!r}: error_threshold.{kind} must not be negative")
    # A threshold for a kind with no middle tier would never be read; drop it
    # so `Preset.close_threshold` reports honestly and repr stays truthful.
    if mid_checker is None:
        close_checker = None
    if mid_cube is None:
        close_cube = None
    return Preset(key, str(spec.get("display", key)), first_pass, second_pass,
                  mid_checker, mid_cube,
                  close_checker, close_cube,
                  0.0 if err_checker is None else err_checker,
                  0.0 if err_cube is None else err_cube)


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
        note = _RETIRED.get(key)
        if note is not None:
            note = _RETIRED.get(note, note)   # an alias points at its preset
            raise ValueError(
                f"Preset {name!r} is {note} Valid presets: {valid}")
        raise ValueError(f"Unknown preset {name!r}. Valid presets: {valid}")
    return PRESETS[key]
