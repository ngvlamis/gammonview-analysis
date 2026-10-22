# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""eXtreme Gammon (XG) style analysis presets.

Each preset is a two-pass scheme: a cheap first pass evaluates every decision,
and a stronger second pass runs only on an error -- the played checker move or
cube action disagrees with the first pass (matching XG World Class, which only
rolls out cube decisions on a cube error). A preset with no second pass is a
single-pass scheme that judges everything at the first-pass level.

The canonical presets are defined in code here (`_BUILTIN_SPECS`), so they
are always available and can never be broken by editing. Users add their own in
an optional `presets.yaml`, under names of their own, and the file's `default:`
key overrides the default preset. A built-in cannot be overridden: an entry
reusing a built-in's name or alias is skipped with a warning. To restore
defaults, just delete presets.yaml; to start one, run `gvan-match
--init-presets`. A malformed presets.yaml is ignored with a warning (the
built-ins still load).

**Every preset names one method, and its label says which.** An analysis
records `Preset.label`: `world_class@2` for a built-in at revision 2, or
`custom/<name>@<digest>` for a user preset, where the digest is of its whole
definition. A built-in's revision is bumped whenever anything that can move its
results changes -- its levels and thresholds, and the screening constants in
`checker_eval` -- and `_REVISIONS` pins the digest each revision stood for, so
`test_presets` fails if a definition changes without a new revision. Two
analyses with the same label, `model_id` and `engine_build` were produced the same
way; a caller that needs that guarantee passes `name@revision` and is refused
if this install runs anything else. Resign errors are an analysis option, not a
preset's: an analysis that judged them is labelled `world_class@2+resign`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import dataclasses
from dataclasses import dataclass
from pathlib import Path

import yaml

# Valid bgsage eval levels, shallow -> deep. Kept in sync with the eval levels
# documented in CLAUDE.md.
VALID_LEVELS: frozenset[str] = frozenset({
    "1ply", "2ply", "3ply", "4ply",
    "truncated1", "truncated2", "truncated3", "rollout",
})

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


# Canonical built-in presets (spec dicts, same shape as presets.yaml entries).
# These are the guaranteed baseline. second_pass None => single pass; mid_pass +
# close_threshold add the optional 3-tier scheme (checker plays and cubes);
# error_threshold is what a decision has to cost to earn the second pass.
_BUILTIN_SPECS: dict[str, dict] = {
    "very_quick": {"display": "Very quick", "revision": 1, "first_pass": "2ply", "second_pass": None, "aliases": ["vq"]},
    # error_threshold 0, not the 0.02 default, because `fast`'s second pass is
    # 3-ply rather than a rollout. The default exists to RATION ROLLOUTS -- on a
    # checker move list a rollout costs ~1.2s against a 3-ply look's ~0.036s, so
    # an error too small for a rollout to size usefully is worth leaving at the
    # screen. Here the second pass costs ~6x the screen, not ~200x, and the
    # trade goes the other way: measured over the five golden matches, 0.02 saves
    # 6% of wall clock (40.7s -> 38.2s) and moves PR by a mean of 0.14 (max
    # 0.35). Cheap depth is worth taking on every error.
    "fast": {"display": "Fast", "revision": 2, "first_pass": "2ply", "second_pass": "3ply",
             "error_threshold": 0.0, "aliases": ["f"]},
    "deep": {"display": "Deep", "revision": 2, "first_pass": "3ply", "second_pass": None, "aliases": ["d"]},
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
    # CUBES: a 3-ply screen, 4-ply on a cube within 0.12 of its double or take
    # point, and the rollout on ANY wrong action (XG's "If Different"), so cube
    # error_threshold is 0. (At @1 they shared the checker shape: 0.08 window,
    # rollout only above 0.02.) The window is wider than the checker one
    # because a cube is cheap to deepen -- 4-ply is 4x the 3-ply screen on one
    # pre-roll cube against 19x on a move list. XG's own routing, read off the
    # corpus, is a 2-ply screen with 4-ply within ~0.25 of the double point;
    # this keeps the deeper screen instead of copying the shallower one.
    #
    # Resignations are not judged here, as in XG's own World Class mode:
    # resign errors are an analysis option (`resign_errors`), never part of a
    # preset, and an analysis that ran with them says so in its label.
    #
    # The checker interval stays XG's Normal (0.08, 4-ply on up to
    # DEEPEN_MAX_MOVES). Gigantic -- 16 moves within 0.32, the BMAB setting --
    # was measured on 11,037 checker decisions and costs 3x the non-rollout
    # checker CPU to recover 0.089 equity of best-move regret in all, 0.071 of
    # it on one decision, and an unchanged rollout count: no PR effect. (Its gain is
    # from a wider 3-ply set, not the window; widening the window alone buys
    # nothing.)
    "world_class": {"display": "World Class", "revision": 2,
                    "first_pass": "3ply", "mid_pass": "4ply", "second_pass": "truncated2",
                    "close_threshold": {"checker": 0.08, "cube": 0.12},
                    "error_threshold": {"checker": DEFAULT_ERROR_THRESHOLD, "cube": 0.0},
                    "aliases": ["wc", "worldclass"]},
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
        "while agreeing with XG less often. Use world_class for analysis "
        "you intend to trust, or deep for something genuinely cheap."
    ),
    "b": "balanced",   # its alias, resolved to the entry above
    # Retired before preset revisions shipped, so no file carries its label.
    # Once world_class's rollouts got cheaper it cost three quarters of
    # world_class for a PR 0.13 away from it (11 matches, 2026-10-21), and a
    # second world-class method splits exactly the comparison the labels exist
    # for. The gap below world_class is deep, at an eighth of the cost.
    "world_class_fast": (
        "retired after 2.0.2 -- it cost three quarters of what world_class "
        "costs for results 0.13 PR away from it. Use world_class for analysis "
        "you intend to trust, or deep for something genuinely cheap."
    ),
    "wcf": "world_class_fast",
}

#: The definition digest (`Preset.digest`, first 12 hex) of every revision each
#: built-in has shipped, oldest first; the last is the revision that runs. A
#: revision is a promise that its label means one method, so a digest here is
#: never edited -- a change to a definition appends a revision, and
#: `test_presets` fails until it does. Old revisions are kept so a file that
#: names one can be told what it was, not so they can be run: a revision
#: includes code paths (the screening in `checker_eval`) that are not kept.
#
#: 1 -- the definitions as they stood when revisions were introduced.
#: 2 -- staged checker screening (1 -> 2 -> N-ply, the 2-ply stage widened to
#:      the 1-ply window on a crowded field, and a middle tier deepening only
#:      the screen's close moves); very_quick never goes past 2-ply, so it
#:      is unchanged and stays at 1. world_class@2 also widens the cube
#:      window to 0.12 and rolls out any wrong cube action.
_REVISIONS: dict[str, dict[int, str]] = {
    "very_quick": {1: "454c535ddc16"},
    "fast": {1: "7030cc90eace", 2: "a44dc6c17c2f"},
    "deep": {1: "f0d71d4caf31", 2: "062a598d4e7f"},
    "world_class": {1: "e5b1989b5c6c", 2: "5b8d38df0b0d"},
}

#: What a custom preset's name may be: it is written into the label, so it
#: must stay readable and never carry the label's own separators.
_CUSTOM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")

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
# The built-in presets always apply and cannot be changed: each names one
# method, recorded with its revision (e.g. world_class@2) in every analysis.
# This file is for ADDING your own, under names of your own; an entry reusing a
# built-in's name or alias is skipped with a warning. Your preset is recorded as
# custom/<name>@<digest>, the digest being of its whole definition, so the same
# definition always carries the same label and any edit gives it a new one.
# Delete this file to restore defaults. A malformed file is ignored with a
# warning.
#
# This file is read from two locations (project-local wins over global):
#   - global:  ~/.config/bgsage/presets.yaml
#   - project: ./presets.yaml (next to the scripts)
#
# Built-in presets: very_quick (vq), fast (f), deep (d), world_class (wc).
# Default: fast.
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
# equally cheap on a move list and on a single cube -- a 4-ply look is 19x
# the 3-ply screen on a move list and 4x on one cube. An omitted kind stays
# plain 2-tier. close_threshold and error_threshold take the same
# bare-value-or-{checker, cube}-mapping shape (world_class gives cubes a 0.12
# window and checker plays 0.08), and so do first_pass and second_pass, whose
# checker levels are required: a mapped second_pass sizes a checker error and a
# cube error at different levels (a rollout of one cube position costs a
# fraction of a rollout of a move list's candidates), but every kind has one.
# Naming the same level as
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
  # my_cube_rollouts:
  #   display: Roll Out the Cubes
  #   first_pass: 3ply
  #   second_pass: {checker: 4ply, cube: truncated3}
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
    # The built-in's revision (see `_REVISIONS`); None for a custom preset.
    revision: int | None = None
    # The cube screen's level when it differs from the checker one; None =>
    # `first_pass` for both. Last, so positional construction keeps working.
    # XG World Class screens cubes at 4-ply and checker plays at 3-ply, and
    # the cost argument is the middle tier's: 4-ply is 4x the 3-ply screen on a
    # cube, 19x on a move list.
    first_pass_cube: str | None = None
    # Judge accepted resignations: the resigner's error in giving up the
    # position for what was conceded, and the acceptor's in taking it. XG has
    # this as a global switch, off in its World Class mode and on in BMAB's
    # published configuration. Here it is an analysis option, never a preset's
    # (`with_resign_errors`): it moves PR, so it shows in the label as
    # `+resign`, but it is not part of the definition a pin names.
    resign_errors: bool = False
    # The cube's on-error level when it differs from the checker one; None =>
    # `second_pass` for both. Like `first_pass_cube`, and last for the same
    # reason. A rollout of a cube is one position; a rollout of a checker play
    # is every surviving candidate, so the level worth paying for an error is
    # not the same one for both. Never set without `second_pass`.
    second_pass_cube: str | None = None

    @property
    def cube_first_pass(self) -> str:
        """The level every cube decision is screened at."""
        return self.first_pass_cube or self.first_pass

    @property
    def cube_second_pass(self) -> str | None:
        """The level a cube error is sized at; None for a single-pass preset."""
        return self.second_pass_cube or self.second_pass

    def definition(self) -> dict:
        """Everything that decides this preset's results, as plain data.

        The levels and thresholds, plus the screening constants the levels it
        names actually reach (`checker_eval.screening`), read at call time so
        an environment override (GVAN_NO_SCREEN, GVAN_STAGED, GVAN_ROLLOUT_SELECT) shows up here
        instead of running under a label that says otherwise. Not the display
        name or aliases: renaming a preset does not change what it computes.
        """
        from .checker_eval import screening   # lazy: pulls in bgsage
        levels = [lv for lv in (self.first_pass, self.mid_pass_checker,
                                self.mid_pass_cube, self.second_pass,
                                self.second_pass_cube) if lv]
        # A shared first or second pass stays a bare level, so every preset
        # that never split it keeps the digest it always had.
        first = (self.first_pass if self.cube_first_pass == self.first_pass
                 else [self.first_pass, self.cube_first_pass])
        second = (self.second_pass if self.cube_second_pass == self.second_pass
                  else [self.second_pass, self.cube_second_pass])
        return {
            "first_pass": first,
            "mid_pass": [self.mid_pass_checker, self.mid_pass_cube],
            "second_pass": second,
            "close_threshold": [self.close_threshold_checker, self.close_threshold_cube],
            "error_threshold": [self.error_threshold_checker, self.error_threshold_cube],
            "screening": screening(levels, has_mid=self.mid_pass_checker is not None),
        }

    def digest(self) -> str:
        """SHA-256 (hex) of `definition()`, canonically serialised."""
        text = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        """What an analysis records as having produced it.

        `world_class@2` for a built-in running its pinned definition;
        `world_class@2+<digest>` for one whose definition has been changed
        under it (an environment override), so it is never mistaken for the
        real revision; `custom/<name>@<digest>` for a user preset. Any of
        them ends `+resign` when the analysis judged resignations.
        """
        return self.method_label + ("+resign" if self.resign_errors else "")

    @property
    def method_label(self) -> str:
        """The label of the preset's definition alone -- what a pin names."""
        d = self.digest()[:12]
        if self.revision is None:
            return f"custom/{self.key}@{d[:6]}"
        if _REVISIONS.get(self.key, {}).get(self.revision) == d:
            return f"{self.key}@{self.revision}"
        return f"{self.key}@{self.revision}+{d[:6]}"

    def with_resign_errors(self, on: bool = True) -> "Preset":
        """This preset, judging accepted resignations (or not)."""
        return dataclasses.replace(self, resign_errors=on)

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


def _second_levels(key: str, spec: dict) -> tuple[str | None, str | None]:
    """Resolve `second_pass` into (checker_level_or_None, cube_level_or_None).

    Absent or null is a single-pass preset; a bare level sizes both kinds'
    errors. A `{checker, cube}` mapping names each, and its checker level is
    required: the two-pass scheme screens every decision and sizes every error
    worth sizing, so there is no kind without an on-error tier -- a preset
    that wants one sizes it at the screen's own level. A cube level equal to
    the checker one, or omitted, collapses to None as in `first_pass`.
    """
    raw = spec.get("second_pass")
    if not isinstance(raw, dict):
        return raw, None
    unknown = sorted(str(k) for k in raw if k not in _MID_KINDS)
    if unknown:
        raise PresetConfigError(
            f"preset {key!r}: second_pass keys must be "
            f"{' / '.join(map(repr, _MID_KINDS))}, got {', '.join(unknown)}")
    checker, cube = raw.get("checker"), raw.get("cube")
    if checker is None:
        raise PresetConfigError(
            f"preset {key!r}: second_pass.checker is required in a mapping "
            f"(omit second_pass for a single-pass preset)")
    return checker, (None if cube in (None, checker) else cube)


def _first_levels(key: str, spec: dict) -> tuple[str | None, str | None]:
    """Resolve `first_pass` into (checker_level, cube_level_or_None).

    A bare level screens both kinds (every preset written before the split); a
    `{checker, cube}` mapping names each, and the checker level is required --
    there is no screen to fall back to. A cube level equal to the checker one
    collapses to None, so the two spellings of one method are one preset.
    """
    raw = spec.get("first_pass")
    if not isinstance(raw, dict):
        return raw, None
    unknown = sorted(str(k) for k in raw if k not in _MID_KINDS)
    if unknown:
        raise PresetConfigError(
            f"preset {key!r}: first_pass keys must be "
            f"{' / '.join(map(repr, _MID_KINDS))}, got {', '.join(unknown)}")
    checker, cube = raw.get("checker"), raw.get("cube")
    return checker, (None if cube in (None, checker) else cube)


def _make_preset(key: str, spec: dict) -> Preset:
    """Validate one preset spec dict into a Preset. Raises PresetConfigError."""
    first_pass, first_cube = _first_levels(key, spec)
    second_pass, second_cube = _second_levels(key, spec)  # may be absent/null
    mid_checker, mid_cube = _mid_levels(key, spec)
    close_checker, close_cube = _kind_thresholds(key, spec, "close_threshold", None)
    err_checker, err_cube = _kind_thresholds(key, spec, "error_threshold",
                                             DEFAULT_ERROR_THRESHOLD)
    for label, lvl in (("first_pass", first_pass), ("first_pass.cube", first_cube),
                       ("second_pass", second_pass), ("second_pass.cube", second_cube),
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
    if "resign_errors" in spec:
        raise PresetConfigError(
            f"preset {key!r}: resign_errors is an analysis option "
            f"(--resign-errors), not part of a preset")
    revision = spec.get("revision")
    if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int)
                                 or revision < 1):
        raise PresetConfigError(f"preset {key!r}: revision must be a positive integer")
    return Preset(key, str(spec.get("display", key)), first_pass, second_pass,
                  mid_checker, mid_cube,
                  close_checker, close_cube,
                  0.0 if err_checker is None else err_checker,
                  0.0 if err_cube is None else err_cube,
                  revision, first_cube, second_pass_cube=second_cube)


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
    reserved = _reserved_names()
    for key, spec in user_presets.items():
        key = str(key).strip().lower()
        # A built-in names one method, so it cannot be redefined -- not even a
        # field of it. Skipped, not fatal, so the file's other presets load.
        if key in reserved:
            print(f"warning: {path}: preset {key!r} is a built-in name and cannot "
                  f"be overridden; skipping it (give your preset a name of its own)",
                  file=sys.stderr)
            continue
        _check_custom(key, spec, reserved)
        merged[key] = spec
    return merged, raw.get("default", default)


def _check_custom(key: str, spec, reserved: set[str]) -> None:
    """The rules a custom preset's entry obeys, wherever it came from.

    One set, shared by `presets.yaml` and `custom_preset`, so a definition
    accepted by one is accepted by the other and labelled the same. Raises
    PresetConfigError; `_make_preset` then checks the levels and thresholds.
    """
    if not isinstance(spec, dict):
        raise PresetConfigError(f"preset {key!r} must be a mapping")
    if key in reserved:
        raise PresetConfigError(
            f"preset {key!r} is a built-in name and cannot be redefined; "
            f"give your preset a name of its own")
    if not _CUSTOM_NAME.fullmatch(key):
        raise PresetConfigError(
            f"preset {key!r}: a custom preset's name must be lowercase letters, "
            f"digits, '_' and '-', starting with a letter or digit")
    if "revision" in spec:
        raise PresetConfigError(
            f"preset {key!r}: revision is for built-ins; a custom preset is "
            f"identified by the digest of its definition")
    for alias in spec.get("aliases") or []:
        if str(alias).strip().lower() in reserved:
            raise PresetConfigError(
                f"preset {key!r}: alias {alias!r} is a built-in name")


def custom_preset(name: str, spec: dict) -> Preset:
    """A custom preset from a definition held in memory rather than a file.

    The in-memory twin of one `presets.yaml` entry: the same spec shape, the
    same rules, and so the same `custom/<name>@<digest>` label -- the digest
    is of the definition, so the file and this function cannot label one
    method two ways. For a program that keeps its presets somewhere of its own
    (a settings page, a database, a league's published rules) and would
    otherwise have to write a YAML file and reload this module to run one.

    The result is not registered anywhere: pass it as `preset=` to
    `analyze_match` and its relatives. Raises PresetConfigError, with the
    messages a malformed `presets.yaml` entry gets.
    """
    key = str(name).strip().lower()
    _check_custom(key, spec, _reserved_names())
    return _make_preset(key, spec)


def _reserved_names() -> set[str]:
    """Built-in keys and aliases, and retired names: none can be redefined."""
    names = set(_BUILTIN_SPECS) | set(_RETIRED)
    for spec in _BUILTIN_SPECS.values():
        names.update(str(a).strip().lower() for a in spec.get("aliases") or [])
    return names


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


def resolve_preset(name: "str | Preset | None") -> Preset:
    """Resolve a canonical key or alias (case-insensitive) to a Preset.

    Pass None to get the configured default preset, or a Preset (from
    `custom_preset`) to have it back unchanged. Raises ValueError listing the
    valid presets for an unknown name.
    """
    if isinstance(name, Preset):
        return name
    if name is None:
        return PRESETS[DEFAULT_PRESET]
    key = name.strip().lower()
    key, _, pin = key.partition("@")
    if key.startswith("custom/"):
        key = key[len("custom/"):]
    key = _ALIASES.get(key, key)
    if key not in PRESETS:
        valid = ", ".join(sorted(PRESETS))
        note = _RETIRED.get(key)
        if note is not None:
            note = _RETIRED.get(note, note)   # an alias points at its preset
            raise ValueError(
                f"Preset {name!r} is {note} Valid presets: {valid}")
        raise ValueError(f"Unknown preset {name!r}. Valid presets: {valid}")
    p = PRESETS[key]
    if pin:
        _check_pin(name, p, pin)
    return p


def _check_pin(name: str, p: Preset, pin: str) -> None:
    """Refuse to run anything but the pinned method.

    `world_class@2` runs only if this install's world_class is revision 2 and
    unmodified; a custom preset's pin is a prefix of its definition digest. A
    pin is how a caller who must compare like with like -- a title-granting
    body, a league -- says so, and a silent upgrade is exactly what it rules
    out.
    """
    if p.revision is None:
        if not p.digest().startswith(pin):
            raise ValueError(
                f"Preset {name!r}: this install's {p.key!r} is {p.label}, a "
                f"different definition")
        return
    if not pin.isdigit():
        raise ValueError(f"Preset {name!r}: a built-in is pinned by revision number, "
                         f"e.g. {p.key}@{p.revision}")
    want = int(pin)
    history = _REVISIONS.get(p.key, {})
    if want != p.revision:
        when = ("an earlier revision" if want in history
                else "a newer revision than this install has" if want > p.revision
                else "not a revision")
        raise ValueError(
            f"Preset {name!r} is {when} of {p.key}; this install runs "
            f"{p.key}@{p.revision}. Install the release that shipped "
            f"{p.key}@{want} to reproduce it.")
    if p.method_label != f"{p.key}@{p.revision}":
        raise ValueError(
            f"Preset {name!r}: this install's {p.key} has been modified "
            f"({p.label}), so it is not revision {want} -- check GVAN_STAGED / "
            f"GVAN_NO_SCREEN / GVAN_ROLLOUT_SELECT in the environment.")
