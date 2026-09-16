# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Pure-stdlib converter: BGBlitz ``.bgf`` match files → OGXM JSON.

Reads a ``.bgf`` file (UTF-8 JSON header + gzip/zlib Smile payload), decodes
the Smile binary JSON, and produces a dict conforming to
``OGXM_JSON_SPEC_GAMMONVIEW.md``.

No third-party dependencies — the Smile decoder is a minimal re-implementation
covering the subset of the format emitted by BGBlitz.

Public API::

    from gvformat.bgf import convert_bgf

    ogxm = convert_bgf(Path("match.bgf"))

CLI entry point ``bgf2gva`` (added in ``pyproject.toml``).
"""

from __future__ import annotations

import gzip
import json
import struct
import sys
import zlib
from pathlib import Path

from .binary import RESIGN_ACTIONS
from .export import (
    _canonical_orientation, _flip_board, _notation_to_steps,
    fit_move_steps, set_position_ply,
)
from .legality import is_play_legal
from .notation import canonical_notation, from_bgf_abs_frame
from .ogid import board_to_ogid

# ---------------------------------------------------------------------------
# Smile decoder (subset used by BGBlitz)
# ---------------------------------------------------------------------------

_SMILE_MAGIC = b":)\n"
_FEATURE_SHARED_NAMES = 0x01

_SENTINEL = -999.0  # BGF sentinel for unanalyzed


def _decompress_if_needed(b: bytes) -> bytes:
    if b.startswith(b"\x1f\x8b"):
        return gzip.decompress(b)
    if b.startswith((b"\x78\x01", b"\x78\x9c", b"\x78\xda")):
        return zlib.decompress(b)
    return b


def read_bgf(path: Path) -> tuple[dict, bytes]:
    """Return ``(header_dict, smile_bytes)`` from a BGBlitz ``.bgf`` file."""
    with path.open("rb") as f:
        header = json.loads(f.readline().decode("utf-8"))
        tail = f.read()
    payload = _decompress_if_needed(tail)
    if not payload.startswith(_SMILE_MAGIC):
        raise ValueError("Payload is not Smile (missing b':)\\n' magic).")
    return header, payload


class _SmileDecoder:
    """Minimal Smile decoder covering BGBlitz's output."""

    def __init__(self, data: bytes) -> None:
        assert data[:3] == _SMILE_MAGIC
        self.buf = data
        self.pos = 4
        self.shared_names = bool(data[3] & _FEATURE_SHARED_NAMES)
        self.name_table: list[str] = []

    def _rb(self) -> int:
        if self.pos >= len(self.buf):
            raise EOFError("Unexpected end of Smile stream")
        b = self.buf[self.pos]
        self.pos += 1
        return b

    def _read(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.buf):
            raise EOFError(f"Need {n} bytes at pos {self.pos}")
        chunk = self.buf[self.pos : end]
        self.pos = end
        return chunk

    @staticmethod
    def _zigzag(n: int) -> int:
        return (n >> 1) ^ -(n & 1)

    def _vint(self) -> int:
        acc = 0
        while True:
            b = self._rb()
            acc = (acc << 6) | (b & 0x3F)
            if b & 0x80:
                return self._zigzag(acc)

    def _safe_double(self) -> float:
        r = self._read(10)
        bits = (
            (r[0] & 0x7F) << 57
            | (r[1] & 0x7F) << 50
            | (r[2] & 0x7F) << 43
            | (r[3] & 0x7F) << 36
            | (r[4] & 0x7F) << 29
            | (r[5] & 0x7F) << 22
            | (r[6] & 0x7F) << 15
            | (r[7] & 0x7F) << 8
            | (r[8] & 0x7F) << 1
            | ((r[9] & 0x7F) >> 6)
        )
        return struct.unpack(">d", struct.pack(">Q", bits))[0]

    def _read_key(self) -> str | None:
        b = self._rb()
        if b in (0xFB, 0xFF):
            return None
        if 0x40 <= b <= 0x7F:
            return self.name_table[b - 0x40]
        if 0x80 <= b <= 0xBF:
            key = self._read((b & 0x3F) + 1).decode("utf-8", errors="replace")
            if self.shared_names:
                self.name_table.append(key)
            return key
        if b == 0x30:
            return self.name_table[self._rb()]
        raise ValueError(f"Unknown Smile key token 0x{b:02x} at pos {self.pos - 1}")

    _END_ARRAY = object()

    def _read_value(self) -> object:
        b = self._rb()
        if b == 0xFA:
            return self._parse_object()
        if b == 0xF8:
            return self._parse_array()
        if b in (0xF9, 0xFF):
            return self._END_ARRAY
        if b == 0x20:
            return ""
        if b == 0x21:
            return None
        if b == 0x22:
            return False
        if b == 0x23:
            return True
        if b == 0x24:
            return self._vint()
        if b == 0x25:
            return self._vint()
        if b == 0x28:
            return struct.unpack(">f", self._read(4))[0]
        if b == 0x29:
            return self._safe_double()
        if 0x40 <= b <= 0x5F:
            return self._read(b - 0x3F).decode("ascii")
        if 0x60 <= b <= 0x7F:
            return self._read(b - 0x5E).decode("utf-8", errors="replace")
        if 0xC0 <= b <= 0xDF:
            return self._zigzag(b - 0xC0)
        if 0xE0 <= b <= 0xEF:
            end = self.buf.index(0xFC, self.pos)
            chunk = bytes(self.buf[self.pos : end])
            self.pos = end + 1
            return chunk.decode("utf-8" if (b & 0x04) else "ascii", errors="replace")
        raise ValueError(f"Unknown Smile value token 0x{b:02x} at pos {self.pos - 1}")

    def _parse_object(self) -> dict:
        obj: dict = {}
        while True:
            key = self._read_key()
            if key is None:
                return obj
            obj[key] = self._read_value()

    def _parse_array(self) -> list:
        arr: list = []
        while True:
            val = self._read_value()
            if val is self._END_ARRAY:
                return arr
            arr.append(val)

    def decode(self) -> object:
        b = self._rb()
        if b == 0xFA:
            return self._parse_object()
        if b == 0xF8:
            return self._parse_array()
        self.pos -= 1
        return self._read_value()


def decode_smile(smile_bytes: bytes) -> object:
    """Decode a Smile-encoded payload to a Python object."""
    return _SmileDecoder(smile_bytes).decode()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _flt(v: object) -> float:
    if v is None:
        return 0.0
    if isinstance(v, str):
        return float(v)
    return float(v)


def _nonempty(s: object) -> str | None:
    return str(s) if s else None


def _parse_date(s: str) -> str | None:
    if not s:
        return None
    return s.replace(".", "-")


def _ply_level(ply: object) -> str | None:
    if ply is None or ply == 0:
        return None
    return f"{int(ply)}ply"


def _make_probs(eq: dict) -> list[float]:
    """Extract [win, gammon_win, bg_win, gammon_loss, bg_loss] from BGF equity."""
    if not eq or eq.get("myWins") is None:
        return []
    return [
        round(_flt(eq.get("myWins")), 4),
        round(_flt(eq.get("myGammon")), 4),
        round(_flt(eq.get("myBackGammon")), 4),
        round(_flt(eq.get("oppGammon")), 4),
        round(_flt(eq.get("oppBackGammon")), 4),
    ]


def _probs_to_eval(probs: list[float]) -> dict:
    """flat [win, gammon_win, bg_win, gammon_loss, bg_loss] -> named Eval."""
    if len(probs) < 5:
        return {}
    win, gwin, bgwin, gloss, bgloss = probs[0], probs[1], probs[2], probs[3], probs[4]
    equity = win + gwin + bgwin - gloss - bgloss
    return {
        "win": win, "gammon_win": gwin, "bg_win": bgwin,
        "gammon_loss": gloss, "bg_loss": bgloss, "equity": round(equity, 4),
    }


def _state_action(state: str | None) -> str | None:
    if state in ("DOUBLE", "RE_DOUBLE"):
        return "double"
    if state == "NO_DOUBLE":
        return "no_double"
    return None


def _state_response(state: str | None) -> str | None:
    if state == "ACCEPT":
        return "take"
    if state == "REJECT":
        return "pass"
    return None


def _cube_lost(pr_val: object) -> float:
    v = _flt(pr_val)
    return 0.0 if v <= -98 else round(abs(v), 4)


def _trivial_cube(nd: float, dt: float, dp: float) -> bool:
    """Cube decision so clear it should not count toward PR. Mirrors
    gvanalysis.game_eval._trivial_cube / gvformat.xg._trivial_cube."""
    return (
        abs(nd - min(dt, dp)) < 0.001
        or (nd - dt) > 0.200
        or (nd - dp) > 0.200
        or (nd < -0.900 and dt < -0.900)
    )


def _trivial_take_pass(dt: float, dp: float) -> bool:
    """Take/pass response so clear it should not count toward PR. Mirrors
    gvanalysis.game_eval._trivial_take_pass."""
    return abs(dt - dp) < 0.001


# ---------------------------------------------------------------------------
# Board helpers (BGF → absolute → mover-perspective for OGID)
# ---------------------------------------------------------------------------

# BGF uses a 24-point board: positive = green (player1/white), negative = red
# (player2/black). Index i represents point (i+1) in BGF's coordinate system.
# Green's point N is absolute point N; Red's point N is absolute point (25 - N)
# (verified against gvformat.export's independently-derived, libogxm-matched
# absolute frame: the standard starting position's green/positive checkers
# land at the same absolute indices as XG's/export's White checkers only with
# this mapping, not the reverse one -- green's own numbering is the *direct*
# index, red's is reflected). Points 24 and 25 in the extended array are bar
# checkers.


def _bgf_initial_board_to_absolute(initial: dict) -> list[int]:
    """Convert BGF initial.board to absolute 26-element board.

    Absolute convention (for ogid.board_to_ogid with mover_is_white=True,
    matching ``gvformat.xg._xg_to_absolute``): index 25 = White's bar
    (non-negative count), index 0 = Black's bar (also a non-negative count
    -- NOT sign-encoded, unlike points 1-24), 1-24 signed count (positive =
    White, negative = Black).
    """
    pts = initial.get("points", [])
    # BGF points[:24] = the 24-point board. points[24] = green bar, points[25] = red bar
    #
    # ``points`` is a SINGLE shared frame -- index i is physical point i+1 in
    # RED's numbering -- and the sign alone says who owns the checkers
    # (positive = green, negative = red). Since green (assumed White, pre any
    # canonical-orientation flip applied by the caller) needs to land at its
    # own absolute white-numbering index directly (see module note above),
    # and red's numbering is the mirror of green's (red's point N == green's
    # point 25-N), ``points[i]`` (physical point i+1 in red's numbering) goes
    # to absolute index ``25 - (i + 1)`` -- i.e. ``board[24 - i]`` -- matching
    # the same reflection ``_apply_bgf_move`` applies to a red sub-move.
    board = [0] * 26
    for i in range(24):
        val = int(pts[i]) if i < len(pts) else 0
        if val:
            board[24 - i] = val
    # Bar checkers
    green_bar = int(pts[24]) if len(pts) > 24 else 0
    red_bar = int(pts[25]) if len(pts) > 25 else 0
    board[25] = green_bar   # White's bar (green = white), stored positive
    # Opponent's (red's) bar: ``ogid._absolute_positions`` reads index 0 as a
    # plain non-negative count (``n = count``, no ``abs()``) -- same as
    # ``gvformat.xg._xg_to_absolute``'s ``board[0] = -pos[25]`` (XG's raw
    # pos[25] is itself negative, so the double negation lands positive).
    # Storing it negative here silently drops every black-bar checker from
    # the OGID position string (Python's ``[x] * n`` for negative ``n`` is
    # simply empty).
    board[0] = red_bar
    return board


def _apply_bgf_move(board: list[int], from_pt: int, to_pt: int, pid: int) -> bool:
    """Apply one sub-move to the absolute board. Returns True if it hit.

    BGF coordinates: from_pt and to_pt are player-relative.
      green: point N -> absolute N
      red:   point N -> absolute (25 - N)
      bar entry: from_pt == 25 (no source update)
      bearing off: to_pt <= 0 (no dest update)
    """
    if from_pt == 25:
        abs_f = 25 if pid == -1 else 0  # green bar -> abs 25, red bar -> abs 0
    elif 1 <= from_pt <= 24:
        abs_f = from_pt if pid == -1 else (25 - from_pt)
    else:
        return False

    if to_pt <= 0:
        abs_t = 0  # bearing off — just remove from source
    elif 1 <= to_pt <= 24:
        abs_t = to_pt if pid == -1 else (25 - to_pt)
    else:
        return False

    # Remove from source. Green is stored positive and red negative on points
    # 1-24 (see the hit/place logic below), so removing one of the mover's
    # checkers moves the point count *toward* zero: -1 for green, +1 for red.
    # The bar slots (0 and 25) are both plain non-negative *counts* (not
    # signed like the points -- see ``_bgf_initial_board_to_absolute``), so
    # leaving one's own bar always decrements it regardless of color; they
    # must be decremented too, or entering from the bar leaves a phantom
    # checker there forever.
    if 1 <= abs_f <= 24:
        board[abs_f] += -1 if pid == -1 else 1
    elif abs_f == 25:   # green's bar (own bar, non-negative count)
        board[25] -= 1
    elif abs_f == 0:    # red's bar (own bar, non-negative count)
        board[0] -= 1

    if to_pt <= 0:
        return False  # bearing off

    # Check for hit and place
    hit = False
    if 1 <= abs_t <= 24:
        if pid == -1:  # green moving; red blot = -1
            if board[abs_t] == -1:
                hit = True
                board[abs_t] = 1
                board[0] += 1  # opponent (red) checker to bar (count, +1)
            else:
                board[abs_t] += 1
        else:  # red moving; green blot = +1
            if board[abs_t] == 1:
                hit = True
                board[abs_t] = -1
                board[25] += 1  # opponent (green) checker to bar
            else:
                board[abs_t] -= 1
    return hit


def _apply_bgf_moves(from_pts: list, to_pts: list, board: list[int], pid: int) -> None:
    """Advance ``board`` in place by every played sub-move (hit markers are
    irrelevant here -- the display string is rendered separately by
    ``_bgf_fmt_notation``/``_bgf_canonical_notation``)."""
    for f, t in zip(from_pts, to_pts):
        if f == -1:
            break
        _apply_bgf_move(board, f, t if t else 0, pid)


def _bgf_fmt_notation(from_pts: list, to_pts: list, pid: int, board_abs: list[int]) -> str:
    """Render BGF player-relative from/to as OGXM notation (``bar``/``off`` +
    hit ``*``), without mutating the caller's board.

    Runs on a *copy* of the absolute pre-move board so it is safe to call per
    candidate, and emits the spec's ``bar``/``off`` tokens -- so the string
    parses cleanly through ``_notation_to_steps`` into structured ``move``
    steps and reconstructs standard ``.mat`` notation. (The canonicalized
    *display* string comes from ``_bgf_canonical_notation``.)
    """
    work = list(board_abs)
    parts = []
    for f, t in zip(from_pts, to_pts):
        if f == -1:
            break
        t_val = t if t else 0
        hit = _apply_bgf_move(work, f, t_val, pid)
        f_str = "bar" if f == 25 else str(f)
        t_str = "off" if t_val <= 0 else str(t_val)
        parts.append(f"{f_str}/{t_str}{'*' if hit else ''}")
    return " ".join(parts)


def _bgf_canonical_notation(
    from_pts: list, to_pts: list, pid: int, board_abs: list[int], d1: int, d2: int
) -> str:
    """Render the *display* ``notation`` string via the shared board-diff
    canonicalizer -- collapsing hops and combining identical legs -- so BGF
    import matches the analyzer and XG paths exactly.

    Applies the play to a copy of the absolute pre-move board (same
    ``_apply_bgf_move`` the per-hop renderer uses), then diffs before/after in
    the mover's perspective. Runs on a copy, so it's safe per candidate.
    """
    after = list(board_abs)
    for f, t in zip(from_pts, to_pts):
        if f == -1:
            break
        _apply_bgf_move(after, f, t if t else 0, pid)
    return canonical_notation(
        from_bgf_abs_frame(board_abs, pid),
        from_bgf_abs_frame(after, pid),
        d1, d2,
    )


# ---------------------------------------------------------------------------
# Dice action ID mapping
# ---------------------------------------------------------------------------

_DICE_PAIRS: list[tuple[int, int]] = [
    (d1, d2) for d1 in range(1, 7) for d2 in range(d1, 7)
]
_DICE_ACTION_ID: dict[tuple[int, int], int] = {
    pair: i for i, pair in enumerate(_DICE_PAIRS)
}


def _dice_action_id(d1: int, d2: int) -> int:
    return _DICE_ACTION_ID[(min(d1, d2), max(d1, d2))]


# ---------------------------------------------------------------------------
# OGID state constants (mirroring export.py)
# ---------------------------------------------------------------------------

_OGID_STATE_INITIAL_BOTH = "IB"
_OGID_STATE_ROLLED = "R"
_OGID_STATE_CHECKER_DONE = "C"
_OGID_STATE_AFTER_TAKE = "A"
_OGID_STATE_DOUBLE_OFFERED = "D"
_OGID_STATE_GAME_OVER = "G"
_OGID_STATE_FINISHED = "F"

_OGID_ACTION_NONE = "N"
_OGID_ACTION_DOUBLE = "O"
_OGID_ACTION_TAKE = "T"
_OGID_ACTION_PASS = "P"

_OGID_CUBE_CENTERED = "N"
_OGID_CUBE_WHITE = "W"
_OGID_CUBE_BLACK = "B"


class _TurnState:
    """Per-game mutable state for OGID turn-phase tracking."""

    __slots__ = (
        "cur_state", "cube_owner", "cube_action", "cube_log2",
        "move_id", "awaiting_response", "is_first_ply",
    )

    def __init__(self) -> None:
        self.cur_state = _OGID_STATE_INITIAL_BOTH
        self.cube_owner = _OGID_CUBE_CENTERED
        self.cube_action = _OGID_ACTION_NONE
        self.cube_log2 = 0
        self.move_id = 0
        self.awaiting_response = False
        self.is_first_ply = True

    @property
    def cube_value(self) -> int:
        return 1 << self.cube_log2


def _ogid(
    board: list[int], *, cube_value: int, cube_owner: str, cube_action: str,
    dice: tuple[int, int] | None, on_roll: str, game_state: str,
    score_white: int, score_black: int, match_length: int, crawford: bool,
    move_id: int = 0,
) -> str:
    return board_to_ogid(
        board,
        mover_is_white=True,
        cube_value=cube_value,
        cube_owner=cube_owner,
        cube_action=cube_action,
        dice=dice,
        on_roll=on_roll,
        game_state=game_state,
        score_white=score_white,
        score_black=score_black,
        match_length=match_length,
        crawford=crawford,
        move_id=move_id,
    )


def _build_alternatives(
    move_options: list[dict], mover_is_white: bool, d1: int, d2: int,
    board_before_mover: list[int] | None = None,
) -> list[dict]:
    """Build OGXM alternatives list from BGF moveAnalysis entries.

    ``board_before_mover`` is the pre-move board in the mover's own numbering
    (see ``export._build_alternatives`` for why the pre-move board is the right
    one for every candidate). BGBlitz records only a move's endpoints, so
    without it a one-checker two-die alternative ("18/7" off a 5-6) is split
    larger-die-first and can be drawn through a point the opponent has made --
    the board arrows are built from these steps, so the display shows a checker
    landing on enemy checkers and moving on.
    """
    alts = []
    best_equity = move_options[0]["equity"] if move_options else None
    for opt in move_options:
        # Each move option already carries the 5-output probability vector
        # computed by _checker_analysis (see _make_probs there); reuse it. The
        # options never have an ``eq`` field, so reading ``opt["eq"]`` here
        # silently dropped every alternative's win/gammon/backgammon eval.
        probs = opt.get("probs") or []
        alt: dict = {
            "equity": opt["equity"],
            "is_played": bool(opt.get("played", False)),
            "diff": round(opt["equity"] - best_equity, 4) if best_equity is not None else 0.0,
        }
        if probs:
            alt["eval"] = _probs_to_eval(probs)
        lvl = _ply_level(opt.get("ply"))
        if lvl is not None:
            alt["eval_level"] = lvl
        # Per the OGXM spec, ``move`` is the structured source of truth
        # (Step[] of {from, pips} in absolute coords) and ``notation`` its
        # derived display string. Parse the candidate's notation into steps
        # with the same helper the analyze/XG paths use.
        notation = opt.get("move", "")
        alt["move"] = _notation_to_steps(notation, mover_is_white, d1, d2,
                                         board_before_mover)
        alt["notation"] = opt.get("notation") or notation
        alts.append(alt)
    return alts


# ---------------------------------------------------------------------------
# Checker analysis builder
# ---------------------------------------------------------------------------


def _checker_analysis(
    eq_obj: dict,
    move_analysis: list[dict],
    pr_obj: dict,
    pid: int,
    d1: int,
    d2: int,
    board_before: list[int],
    mover_is_white: bool,
    dancing_eq: dict | None = None,
    ply_raw: object = None,
) -> dict | None:
    """Build OGXM analysis for a checker ply from BGF data.

    ``pid`` (BGF's raw green(-1)/red(1) mover flag) is used only for the
    board-frame-internal helpers below (``_bgf_fmt_notation``, which mutates
    a *copy* of the green-positive absolute board and is orientation-
    independent); ``mover_is_white`` is the caller's already flip-corrected
    canonical-white flag, used only where absolute white-numbering matters
    (``_build_alternatives`` -> ``_notation_to_steps``).
    """
    if not move_analysis:
        # A dance has no candidate list -- there was nothing to choose between.
        # BGBlitz still evaluates the position the non-play leaves behind and
        # stores it in `dancingEquity`, kept apart from the pre-roll `equity`
        # that carries this ply's cube decision (you can double even when you
        # cannot move). Without it the ply's only probabilities are the cube's,
        # which describe the board before the dice were thrown. Fed in here as a
        # single played option with no move, it takes the same path as every
        # other ply and comes out in the shape XG writes for a dance.
        if not dancing_eq:
            return None
        move_analysis = [{
            "eq": dancing_eq,
            "ply": ply_raw,
            "played": True,
            "move": {"from": [], "to": []},
        }]

    # Build move options with equity from BGF's eq objects. Each candidate
    # carries its own player-relative from/to; render it to notation now (with
    # hit markers off the shared pre-move board) so _build_alternatives can
    # parse the spec-required structured ``move`` steps from it.
    move_options = []
    for ma in move_analysis:
        eq = ma.get("eq") or {}
        # Per-candidate equity that PR/error is measured in. Match sessions
        # populate `emg` (match-equity-adjusted EMG). Money sessions leave emg
        # (and matchEquity) as the -999 sentinel (hasEMG=false); there the
        # equivalent quantity is the cubeful money equity in
        # eq.cubeDecision.eqCubeFul. Using the raw emg sentinel in money mode
        # made every candidate equal (-999), zeroing all checker error -- so
        # PR reflected only cube error (BGBlitz hLVH: checker 0.617/2.390 was
        # dropped, leaving cube 0.162/1.297).
        if eq.get("hasEMG", True):
            emg = _flt(eq.get("emg"))
        else:
            emg = _flt((eq.get("cubeDecision") or {}).get("eqCubeFul"))
        probs = _make_probs(eq)
        mv = ma.get("move") or {}
        notation = _bgf_fmt_notation(
            mv.get("from", []), mv.get("to", []), pid, board_before,
        )
        display = _bgf_canonical_notation(
            mv.get("from", []), mv.get("to", []), pid, board_before, d1, d2,
        )
        opt = {
            "equity": round(emg, 4),
            "played": bool(ma.get("played")),
            "emg": emg,
            "matchEquity": _flt(eq.get("matchEquity")),
            "probs": probs,
            "ply": ma.get("ply"),
            "move": notation,  # per-hop string; _build_alternatives -> steps
            "notation": display,  # canonical display string (bar/off, collapsed)
        }
        move_options.append(opt)

    if not move_options:
        return None

    best_emg = move_options[0]["equity"]
    played_opt = next((o for o in move_options if o.get("played")), move_options[-1])
    played_emg = played_opt["equity"]

    # Compute alternatives (structured move steps + derived notation)
    alternatives = _build_alternatives(move_options, mover_is_white, d1, d2,
                                       from_bgf_abs_frame(board_before, pid))

    checker_err = _flt(pr_obj.get("checkerError", -99))
    checker_cnt = checker_err > -98

    equity_loss = round(max(0.0, best_emg - played_emg), 4)

    analysis: dict = {
        "best_equity": best_emg,
        "played_equity": played_emg,
        "equity_loss": equity_loss,
        "decision": checker_cnt,
        "alternatives": alternatives,
    }

    if move_options[0].get("probs"):
        analysis["eval"] = _probs_to_eval(move_options[0]["probs"])

    return analysis


# ---------------------------------------------------------------------------
# Cube analysis builder
# ---------------------------------------------------------------------------


def _cube_decision_analysis(
    cd: dict,
    eq_full: dict,
    ply_level: str | None,
    counted: bool | None = None,
) -> dict | None:
    """Build OGXM analysis for a cube decision (double) ply."""
    eq_nd_raw = _flt(cd.get("eqNoDouble"))
    if eq_nd_raw <= _SENTINEL / 2:
        return None

    # Convert MWC cube equities to EMG using the two calibration points
    emg = _flt(eq_full.get("emg"))
    meq = _flt(eq_full.get("matchEquity"))
    eq_dp_raw = _flt(cd.get("eqDoublePass"))

    denom = 1.0 - emg
    if abs(denom) < 1e-9:
        return None

    half = (eq_dp_raw - meq) / denom
    center = eq_dp_raw - half

    if abs(half) < 1e-9:
        return None

    eq_nd = round((eq_nd_raw - center) / half, 4)
    eq_dt = round((_flt(cd.get("eqDoubleTake")) - center) / half, 4)
    eq_dp = 1.0

    opt_action = _state_action(cd.get("stateOnMove"))
    act_action = "double" if cd.get("hasDoubled") else "no_double"

    probs = _make_probs(eq_full)
    doubler_err = 0.0 if opt_action == act_action else round(abs(eq_nd - min(eq_dt, eq_dp)), 4)

    analysis: dict = {
        "correct_action": opt_action or "no_double",
        "played_action": act_action,
        "no_double_equity": eq_nd,
        "double_take_equity": eq_dt,
        "double_pass_equity": eq_dp,
        "equity_loss": doubler_err,
        # BGBlitz's own counted-ness when the file records it (see
        # _pr_cube_counted); otherwise fall back to the derived rule (the cube
        # is trivial AND the doubler made no error) -- mirrors
        # game_eval._eval_cube_decision (doubler_counts).
        "decision": (counted if counted is not None
                     else not (_trivial_cube(eq_nd, eq_dt, eq_dp) and doubler_err < 0.001)),
    }

    if probs:
        analysis["eval"] = _probs_to_eval(probs)
    if ply_level:
        analysis["eval_level"] = ply_level

    return analysis


def _cube_response_analysis(
    cd: dict,
    eq_full: dict,
    no_double_equity: float | None,
    counted: bool | None = None,
) -> dict | None:
    """Build OGXM analysis for a cube response (take/pass) ply."""
    eq_nd_raw = _flt(cd.get("eqNoDouble"))
    if eq_nd_raw <= _SENTINEL / 2:
        return None

    emg = _flt(eq_full.get("emg"))
    meq = _flt(eq_full.get("matchEquity"))
    eq_dp_raw = _flt(cd.get("eqDoublePass"))

    denom = 1.0 - emg
    if abs(denom) < 1e-9:
        return None

    half = (eq_dp_raw - meq) / denom
    center = eq_dp_raw - half

    if abs(half) < 1e-9:
        return None

    eq_dt = round((_flt(cd.get("eqDoubleTake")) - center) / half, 4)
    eq_dp = 1.0

    opt_resp = _state_response(cd.get("stateOther"))
    has_accepted = cd.get("hasAccepted")
    act_resp = "take" if has_accepted else "pass"

    probs = _make_probs(eq_full)

    analysis: dict = {
        "correct_action": opt_resp or "take",
        "played_action": act_resp,
        "double_take_equity": eq_dt,
        "double_pass_equity": eq_dp,
        "equity_loss": 0.0 if opt_resp == act_resp else round(abs(eq_dt - eq_dp), 4),
        # BGBlitz's own counted-ness when recorded (see _pr_cube_counted);
        # otherwise the derived rule (take and pass equities within 0.001) --
        # mirrors game_eval (resp_counts).
        "decision": (counted if counted is not None
                     else not _trivial_take_pass(eq_dt, eq_dp)),
    }
    if no_double_equity is not None:
        analysis["no_double_equity"] = no_double_equity
    if probs:
        analysis["eval"] = _probs_to_eval(probs)

    return analysis


# ---------------------------------------------------------------------------
# Embedded cube analysis (no-double decision on a checker ply)
# ---------------------------------------------------------------------------


def _pr_cube_counted(pr: dict) -> bool:
    """Whether BGBlitz counted a cube decision at this ply.

    BGBlitz stores a per-ply ``pr.cubeError`` with a -99 sentinel meaning "no
    cube decision counted here" (the same convention as ``checkerError``). Its
    match-level ``cubeCnt`` is exactly the number of non-sentinel entries, and
    its reported PR divides by that count -- verified against the BGBlitz app
    itself (3WNK_g1Z: Iris09 6.88, Jade1 6.02, both reproduced exactly).

    Counted-ness cannot be re-derived from the ``cubeDecision`` fields:
    identical-looking ``NO_DOUBLE``/``close=false`` plies appear both counted
    and uncounted, and neither cube access nor an equity threshold separates
    them. So take it from the file rather than guessing with a heuristic.
    """
    return _flt(pr.get("cubeError", -99)) > -98


def _embedded_cube_analysis(
    cd_chk: dict,
    eq_obj: dict,
    cube_value: int,
    is_money_game: bool,
    ply_level: str | None,
    counted: bool = False,
) -> tuple[str, dict] | None:
    """The cube sub-analysis for a live cube on a checker ply, as ``(key, sub)``.

    The key is returned rather than left for the caller to work out, because
    the two are one decision: which shape gets built and which field it belongs
    in are both ``_state_action(state) == "double"``. Splitting them is what
    broke -- the caller used to re-derive the key and got ``RE_DOUBLE`` wrong,
    filing a missed redouble under ``cube_decision``, whose ``equity_loss`` is
    zero by definition on disk. The error was real in memory and gone from the
    ``.gvab``.

    Mirrors ``gvformat.export``'s ``_cube_sub_analysis``, which returns
    ``(key, sub)`` for the same reason.
    """
    state = cd_chk.get("stateOnMove")
    if state is None or cd_chk.get("hasDoubled") is not None:
        return None

    emg_chk = _flt(eq_obj.get("emg"))
    meq_chk = _flt(eq_obj.get("matchEquity"))
    eq_nd_raw = _flt(cd_chk.get("eqNoDouble"))
    eq_dp_raw = _flt(cd_chk.get("eqDoublePass"))

    if eq_nd_raw <= _SENTINEL / 2:
        return None

    denom = 1.0 - emg_chk
    if abs(denom) < 1e-9:
        return None

    half = (eq_dp_raw - meq_chk) / denom
    center = eq_dp_raw - half

    if abs(half) < 1e-9:
        return None

    eq_nd = round((eq_nd_raw - center) / half, 4)
    eq_dt = round((_flt(cd_chk.get("eqDoubleTake")) - center) / half, 4)
    eq_dp = 1.0

    opt_action = _state_action(state)
    probs = _make_probs(eq_obj)

    if opt_action == "double":
        # Player should have doubled but didn't
        md: dict = {
            "no_double_equity": eq_nd,
            "double_take_equity": eq_dt,
            "double_pass_equity": eq_dp,
            "equity_loss": round(max(0.0, min(eq_dt, eq_dp) - eq_nd), 4),
            "correct_action": "double",
            # No ``decision`` here, though BGBlitz records one. A CUBE type=2
            # entry has a single decision bit and no way to spell "the source
            # said nothing", so storing BGBlitz's answer would need a new
            # format flag -- and a value the writer cannot keep is worse than
            # none: it would read back one way in the viewer and another from
            # the saved file. Readers derive it from the three equities
            # (``gvformat.stats``'s ``_missed_double_counts``), the same rule
            # every other source already relies on.
        }
        # Same pre-roll probabilities the correct-no-double branch keeps.
        if probs:
            md["eval"] = _probs_to_eval(probs)
        if ply_level:
            md["eval_level"] = ply_level
        return ("missed_double", md)

    # Correct no-double (non-error live cube)
    sub: dict = {
        "should_double": False,
        "no_double_equity": eq_nd,
        "double_take_equity": eq_dt,
        "double_pass_equity": eq_dp,
        "action": "no_double",
        # Zero, always: the player was right not to double, so the cube cost
        # nothing -- and ``CUBE type=4``, where this lands, has no room for
        # anything else. A cube *error* belongs in ``missed_double`` above.
        "equity_loss": 0.0,
        "decision": counted,
    }
    if probs:
        sub["eval"] = _probs_to_eval(probs)
    if ply_level:
        sub["eval_level"] = ply_level
    return ("cube_decision", sub)


# ---------------------------------------------------------------------------
# Notation from BGF move arrays
# ---------------------------------------------------------------------------


def _bgf_relative_board(board: list[int], pid: int) -> tuple[list[int], list[int]]:
    """Split bgf's absolute board into mover-relative ``(mine, opp)`` counts.

    ``gvformat.legality`` works in the mover's own numbering (1 = ace point,
    25 = own bar). In bgf's absolute frame green's point N lives at ``N`` and
    red's at ``25 - N`` (see ``_apply_bgf_move``), with green positive.
    """
    mine = [0] * 26
    opp = [0] * 26
    if pid == -1:  # green: positive, own point p at absolute p
        for p in range(1, 25):
            v = board[p]
            if v > 0:
                mine[p] = v
            elif v < 0:
                opp[p] = -v
        mine[25] = max(0, board[25])
    else:  # red: negative, own point p at absolute 25 - p
        for p in range(1, 25):
            v = board[25 - p]
            if v < 0:
                mine[p] = -v
            elif v > 0:
                opp[p] = v
        mine[25] = max(0, board[0])
    return mine, opp


def _bgf_played_pairs(from_pts: list, to_pts: list) -> list[tuple[int, int]]:
    """The actually-played ``(from, to)`` sub-move pairs, bear-off as 0."""
    return [
        (int(f), int(t) if t and int(t) > 0 else 0)
        for f, t in zip(from_pts, to_pts)
        if f != -1
    ]


# ---------------------------------------------------------------------------
# Main converter
# ---------------------------------------------------------------------------


def convert_bgf(bgf_path: Path) -> dict:
    """Convert a BGBlitz ``.bgf`` file to OGXM JSON.

    Returns a dict conforming to ``OGXM_JSON_SPEC_GAMMONVIEW.md``.
    """
    header, smile_bytes = read_bgf(bgf_path)
    data = decode_smile(smile_bytes)

    name_green = str(data.get("nameGreen", ""))
    name_red = str(data.get("nameRed", ""))
    matchlen = int(data.get("matchlen", 0))
    is_money_game = matchlen == 0

    # Canonical orientation: BGF hardcodes green == white internally (every
    # helper above -- _bgf_initial_board_to_absolute, _apply_bgf_move,
    # _bgf_fmt_notation -- assumes it). When canonical white is actually red,
    # every board fed to _ogid must be mirrored at that boundary (_canon_board
    # below), and every green/red-derived "is white" flag must incorporate
    # `flip`. The internal `board` bookkeeping itself is left in its native
    # green-positive frame throughout -- only OGID-bound boards and the
    # mover_is_white passed to _notation_to_steps need the correction.
    player_white, player_black, green_is_white = _canonical_orientation(name_green, name_red)
    flip = not green_is_white

    def _canon_board(b: list[int]) -> list[int]:
        return _flip_board(b) if flip else b

    all_games = data.get("games", [])
    games_out: list[dict] = []

    for gi, g in enumerate(all_games):
        score_green = int(g.get("scoreGreen", 0))
        score_red = int(g.get("scoreRed", 0))
        score_white = score_red if flip else score_green
        score_black = score_green if flip else score_red
        is_crawford = bool(g.get("isCrawford", False))
        cube_value = int(g.get("initial", {}).get("cube", 1))
        cube_owner_bgf = int(g.get("initial", {}).get("cubeOwner", 0))

        # Board in absolute coordinates
        board = _bgf_initial_board_to_absolute(g.get("initial", {}))

        turn = _TurnState()
        # Set initial cube state
        if cube_owner_bgf == -1:  # green owns the cube
            turn.cube_owner = _OGID_CUBE_BLACK if flip else _OGID_CUBE_WHITE
        elif cube_owner_bgf == 1:  # red owns the cube
            turn.cube_owner = _OGID_CUBE_WHITE if flip else _OGID_CUBE_BLACK
        if cube_value > 1:
            turn.cube_log2 = cube_value.bit_length() - 1

        plies: list[dict] = []
        raw = g.get("moves", [])
        idx = 0
        pending_nd_equity: float | None = None

        while idx < len(raw):
            m = raw[idx]
            from_pts = m.get("from", [-1, -1, -1, -1])
            eq_obj = m.get("equity") or {}
            cd = eq_obj.get("cubeDecision") or {}
            has_doubled = cd.get("hasDoubled")
            is_cube_rec = from_pts[0] == -1

            # Phantom terminal record
            if is_cube_rec and has_doubled is None:
                idx += 1
                continue

            # ── Cube action pair ─────────────────────────────────────────────
            if is_cube_rec and has_doubled is not None:
                m_d = m
                m_r = raw[idx + 1] if idx + 1 < len(raw) else {}
                idx += 2

                cd_d = (m_d.get("equity") or {}).get("cubeDecision") or {}
                pr_d = m_d.get("pr") or {}
                pr_r = (m_r.get("pr") or {}) if m_r else {}

                pid = m_d["player"]
                is_white = (pid == -1) != flip  # green = white, unless flipped

                eq_full = m_d.get("equity") or {}
                ply_level = _ply_level(m_d.get("ply"))

                # ── Doubler ply ──────────────────────────────────────────────
                on_roll = "W" if is_white else "B"
                ogid_before = _ogid(
                    _canon_board(board), cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                    cube_action=turn.cube_action, dice=None, on_roll=on_roll,
                    game_state=turn.cur_state, score_white=score_white,
                    score_black=score_black, match_length=matchlen,
                    crawford=is_crawford, move_id=turn.move_id,
                )

                turn.awaiting_response = True
                turn.cur_state = _OGID_STATE_DOUBLE_OFFERED
                turn.cube_action = _OGID_ACTION_DOUBLE

                ogid_after = _ogid(
                    _canon_board(board), cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                    cube_action=turn.cube_action, dice=None,
                    on_roll="B" if is_white else "W",
                    game_state=turn.cur_state, score_white=score_white,
                    score_black=score_black, match_length=matchlen,
                    crawford=is_crawford, move_id=turn.move_id,
                )

                analysis = _cube_decision_analysis(
                    cd_d, eq_full, ply_level, counted=_pr_cube_counted(pr_d),
                )

                ply: dict = {
                    "color": 1 if is_white else 0,
                    "action_id": 21,
                    "ogid_before": ogid_before,
                    "ogid_after": ogid_after,
                }
                if analysis:
                    ply["analysis"] = analysis
                plies.append(ply)

                pending_nd_equity = analysis.get("no_double_equity") if analysis else None

                # ── Response ply ─────────────────────────────────────────────
                # The take/pass labels themselves are derived inside
                # _cube_response_analysis from the same `cd_d`; only the
                # branch flag is needed out here.
                has_accepted = cd_d.get("hasAccepted")

                resp_is_white = not is_white
                on_roll_r = "W" if resp_is_white else "B"

                ogid_before_r = _ogid(
                    _canon_board(board), cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                    cube_action=_OGID_ACTION_DOUBLE, dice=None, on_roll=on_roll_r,
                    game_state=_OGID_STATE_DOUBLE_OFFERED, score_white=score_white,
                    score_black=score_black, match_length=matchlen,
                    crawford=is_crawford, move_id=turn.move_id,
                )

                turn.awaiting_response = False
                if has_accepted:
                    turn.cube_log2 += 1
                    turn.cube_owner = _OGID_CUBE_WHITE if resp_is_white else _OGID_CUBE_BLACK
                    turn.cur_state = _OGID_STATE_AFTER_TAKE
                    turn.cube_action = _OGID_ACTION_TAKE
                else:
                    turn.cur_state = _OGID_STATE_GAME_OVER
                    turn.cube_action = _OGID_ACTION_PASS

                ogid_after_r = _ogid(
                    _canon_board(board), cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                    cube_action=turn.cube_action, dice=None,
                    on_roll="B" if resp_is_white else "W",
                    game_state=turn.cur_state, score_white=score_white,
                    score_black=score_black, match_length=matchlen,
                    crawford=is_crawford, move_id=turn.move_id,
                )

                resp_analysis = _cube_response_analysis(
                    cd_d, eq_full, pending_nd_equity, counted=_pr_cube_counted(pr_r),
                )
                resp_ply: dict = {
                    "color": 1 if resp_is_white else 0,
                    "action_id": 22 if has_accepted else 23,
                    "ogid_before": ogid_before_r,
                    "ogid_after": ogid_after_r,
                }
                if resp_analysis:
                    resp_ply["analysis"] = resp_analysis
                plies.append(resp_ply)

                # On a take, `turn` has already advanced cube_log2/cube_owner
                # above; on a pass the game end is handled below in the game
                # result. Either way there is nothing to track out here.
                pending_nd_equity = None
                continue

            # ── Checker move ─────────────────────────────────────────────────
            pid = m["player"]
            is_white = (pid == -1) != flip  # green = white, unless flipped

            eq_obj_full = m.get("equity") or {}
            move_analysis = m.get("moveAnalysis", [])
            pr_obj = m.get("pr") or {}
            is_dance = m.get("dancingEquity") is not None

            if is_dance:
                d1 = int(from_pts[0]) if from_pts[0] != -1 else 0
                d2 = int(m.get("to", [-1, -1, -1, -1])[0]) if m.get("to", [-1])[0] != -1 else 0
            else:
                d1 = int(m.get("red", 0))
                d2 = int(m.get("green", 0))
            dice = [max(d1, d2), min(d1, d2)] if d1 and d2 else [0, 0]

            # Cube owner relative to current player
            # Board before for OGID
            on_roll = "W" if is_white else "B"
            before_state = _OGID_STATE_INITIAL_BOTH if turn.is_first_ply else _OGID_STATE_ROLLED

            ogid_before = _ogid(
                _canon_board(board), cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                cube_action=turn.cube_action, dice=tuple(dice) if dice[0] else None,
                on_roll=on_roll, game_state=before_state,
                score_white=score_white, score_black=score_black,
                match_length=matchlen, crawford=is_crawford, move_id=turn.move_id,
            )

            turn.move_id += 1
            turn.is_first_ply = False
            turn.cur_state = _OGID_STATE_CHECKER_DONE
            turn.cube_action = _OGID_ACTION_NONE

            # Pre-move board (absolute) — captured before any advance so it can
            # size hit markers / structured steps for this ply's candidates.
            pre_board = list(board)

            # Is the move the player actually made legal for this roll? BGBlitz
            # has no flag for this (unlike XG's invalid_m), and its candidate
            # list is capped at 8 entries, so the only sound test is the rules
            # themselves -- see gvformat.legality. A dance record plays no
            # checkers, which is legal only if the position is a real dance.
            _mine, _opp = _bgf_relative_board(pre_board, pid)
            _played_pairs = (
                [] if is_dance
                else _bgf_played_pairs(from_pts, m.get("to", [-1, -1, -1, -1]))
            )
            is_illegal = bool(d1 and d2) and not is_play_legal(
                _mine, _opp, d1, d2, _played_pairs,
            )

            # Apply move to board
            if not is_dance:
                to_pts_list = m.get("to", [-1, -1, -1, -1])
                _apply_bgf_moves(from_pts, to_pts_list, board, pid)

                # For a legal play, re-derive the board from BGBlitz's own
                # played sub-moves (same destination, canonical ordering). For
                # an illegal play the top-level move is the only faithful
                # record — every moveAnalysis entry is a *legal* alternative —
                # so keep the board we just advanced with the real move.
                played_ma = (
                    None if is_illegal
                    else next((o for o in move_analysis if o.get("played")), None)
                )
                if played_ma:
                    pm = played_ma.get("move") or {}
                    post_board = list(pre_board)
                    _apply_bgf_moves(pm.get("from", []), pm.get("to", []), post_board, pid)
                    # Use the post_board from the detailed sub-moves
                    board = post_board

            ogid_after = _ogid(
                _canon_board(board), cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                cube_action=turn.cube_action, dice=None,
                on_roll="B" if is_white else "W",
                game_state=turn.cur_state, score_white=score_white,
                score_black=score_black, match_length=matchlen,
                crawford=is_crawford, move_id=turn.move_id,
            )

            # Action ID
            if is_dance:
                action_id = _dice_action_id(d1, d2) if d1 and d2 else 30
            else:
                action_id = _dice_action_id(d1, d2) if d1 and d2 else 30

            # Build checker analysis
            analysis = _checker_analysis(
                eq_obj_full, move_analysis, pr_obj, pid, d1, d2, pre_board, is_white,
                dancing_eq=m.get("dancingEquity"), ply_raw=m.get("ply"),
            )

            # Flag a rules violation so gvformat.stats tallies it under
            # illegal_moves and PR excludes it (mirrors the XG path's
            # analysis.illegal_move). BGBlitz's candidates are all legal
            # alternatives, so the "played equity" it implies is fiction —
            # never count this ply as a checker decision.
            if is_illegal:
                if analysis is None:
                    analysis = {"decision": False, "alternatives": []}
                analysis["illegal_move"] = True
                analysis["decision"] = False

            # BGBlitz stores this roll's luck two ways whose meaning flips with
            # the luck object's `mode`. The spec's `luck` is the normalized
            # (cube-independent) single-game EMG equity delta -- matching XG's
            # luck to within engine differences -- which is:
            #   * mode == "Match": `luckWeighted` (= luckPlain / MET slope, i.e.
            #     MWC converted back to equity); `luckPlain` is the raw MWC delta.
            #   * mode == "Money": `luckPlain` (already money EMG equity);
            #     `luckWeighted` is that scaled by the cube (weighted==plain*cube),
            #     which inflated money-session totals by up to the cube factor.
            # Luck is computed for every rolled ply, not just counted decisions
            # (see gvformat.stats), so a ply with no candidate list (forced / not
            # analyzed) still carries luck -- attach a minimal analysis rather
            # than dropping it (mirrors bgf2gva.js). luck_mwc is derived on read.
            luck_obj = m.get("luck") or {}
            luck_val = (luck_obj.get("luckPlain")
                        if luck_obj.get("mode") == "Money"
                        else luck_obj.get("luckWeighted"))
            if luck_val is not None:
                if analysis is None:
                    analysis = {"decision": False, "alternatives": []}
                analysis["luck"] = round(_flt(luck_val), 4)

            # Ply-level structured moves (spec's checker steps): parse the
            # actually-played from/to off the pre-move board. Empty on a dance.
            if is_dance:
                ply_moves: list[dict] = []
            else:
                played_notation = _bgf_fmt_notation(
                    from_pts, m.get("to", [-1, -1, -1, -1]), pid, pre_board,
                )
                # BGBlitz records only a move's endpoints, so a single checker
                # playing both dice ("18/7" off 5-6) leaves the intermediate
                # point to be inferred. Hand the splitter the pre-move board in
                # the mover's own numbering so it rejects an intermediate the
                # opponent has made -- without it the tie-break picks the larger
                # die first and can route the checker through a made point,
                # which replays as a hit and corrupts the board from there on.
                ply_moves = _notation_to_steps(
                    played_notation, is_white, d1, d2,
                    board=[_mine[p] - _opp[p] for p in range(26)],
                )

            # Add embedded cube analysis
            cd_chk = eq_obj_full.get("cubeDecision") or {}
            chk_state = cd_chk.get("stateOnMove")
            if chk_state and cd_chk.get("hasDoubled") is None:
                emb = _embedded_cube_analysis(
                    cd_chk, eq_obj_full, cube_value, is_money_game,
                    _ply_level(m.get("ply")),
                    counted=_pr_cube_counted(pr_obj),
                )
                if emb is not None and analysis is not None:
                    key, sub = emb
                    analysis[key] = sub
            # A cube decision BGBlitz counted but stored no equities for is
            # dropped, deliberately. There is nothing to put in a CUBE record
            # -- the record *is* the three equities -- so keeping it would mean
            # carrying a bare "+1 decision" that the format cannot hold and the
            # viewer cannot draw, and the saved match would then disagree with
            # the one on screen.
            #
            # It is not a loss worth chasing. In the one match where this
            # appeared it fired exactly once in 215 pre-roll cube decisions, on
            # a dead cube (post-Crawford, the player on roll 1-away, so no
            # double is possible) -- and BGBlitz wrote its own "not counted"
            # sentinel on the four identical dead-cube plies later in that same
            # game. The lone marked one is a BGBlitz bookkeeping slip, not a
            # decision, and our own triviality rules already give the right
            # answer by ignoring it.

            # BGBlitz has no invalid-play flag, but a site can still hand it a
            # play that broke the rules, and the steps then overflow what a ply
            # record holds. Same ladder as the other two converters.
            fitted = (fit_move_steps(ply_moves, played_notation, is_white, d1, d2)
                      if 0 <= action_id <= 20 else ply_moves)
            if fitted is None:
                plies.append(set_position_ply(
                    is_white, d1, d2, _canon_board(board), ogid_before, ogid_after))
                idx += 1
                continue

            ply = {
                "color": 1 if is_white else 0,
                "action_id": action_id,
                "d1": d1,
                "d2": d2,
                "moves": fitted,  # structured steps, parsed from the played from/to
                "ogid_before": ogid_before,
                "ogid_after": ogid_after,
            }
            if analysis:
                ply["analysis"] = analysis
            plies.append(ply)

            idx += 1

        # ── Game result ──────────────────────────────────────────────────────
        won_pts = int(g.get("wonPoints", 0))

        # Determine winner
        if gi + 1 < len(all_games):
            next_g = all_games[gi + 1]
            winner_name = name_green if int(next_g.get("scoreGreen", 0)) > score_green else name_red
        else:
            final_green = int(data.get("finalGreen", matchlen))
            winner_name = name_green if final_green > score_green else name_red

        winner_is_white = winner_name == player_white

        # Result type
        if g.get("wasResignation"):
            result_type = "resign"
        elif cube_value > 0:
            mult = won_pts // cube_value if cube_value > 0 else 1
            result_type = {1: "normal", 2: "gammon", 3: "backgammon"}.get(mult, "normal")
        else:
            result_type = "normal"

        # Match completion (score_white/score_black already set per-game above)
        match_complete_here = bool(
            matchlen and (
                (winner_is_white and score_white + won_pts >= matchlen)
                or (not winner_is_white and score_black + won_pts >= matchlen)
            )
        )

        # Game-end ply
        if winner_name:
            if result_type == "resign":
                action_id = 28 if match_complete_here else 27
            elif result_type == "pass":
                action_id = 26
            else:
                action_id = 26 if match_complete_here else 24
            # A terminal ply names the winner: nobody *does* a game-over, and
            # ogxm_replay.cpp just carries the winner through. Resignation is the
            # one exception -- it is an act, and the player who resigns is the one
            # who lost -- so 27/28 gets the resigner, both as the ply's color
            # (`color` is documented as the player a ply belongs to) and as the
            # player on roll (the resigner is the one facing the roll they chose
            # not to take).
            actor_is_white = (
                not winner_is_white if action_id in RESIGN_ACTIONS else winner_is_white
            )
            on_roll = "W" if actor_is_white else "B"

            end_state = turn.cur_state
            end_cube_action = turn.cube_action
            ogid_before_end = _ogid(
                _canon_board(board), cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                cube_action=end_cube_action, dice=None, on_roll=on_roll,
                game_state=end_state, score_white=score_white, score_black=score_black,
                match_length=matchlen, crawford=is_crawford, move_id=turn.move_id,
            )
            ogid_after_end = _ogid(
                _canon_board(board), cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                cube_action=end_cube_action, dice=None,
                on_roll="B" if actor_is_white else "W",
                game_state=end_state, score_white=score_white, score_black=score_black,
                match_length=matchlen, crawford=is_crawford, move_id=turn.move_id,
            )
            plies.append({
                "color": 1 if actor_is_white else 0,
                "action_id": action_id,
                "ogid_before": ogid_before_end,
                "ogid_after": ogid_after_end,
            })

        winner_code = 0 if winner_is_white else 1

        game_obj = {
            "game_index": gi,
            "winner": winner_code,
            "points_won": won_pts,
            "is_crawford": is_crawford,
            "plies": plies,
        }
        games_out.append(game_obj)

    # ── Match-level fields ───────────────────────────────────────────────────
    final_green = int(data.get("finalGreen", 0))
    final_red = int(data.get("finalRed", 0))
    score_white = final_red if flip else final_green
    score_black = final_green if flip else final_red

    if matchlen and score_white >= matchlen:
        match_result = 1
    elif matchlen and score_black >= matchlen:
        match_result = 2
    else:
        match_result = 0

    event_str = _nonempty(data.get("event"))
    site_str = _nonempty(data.get("site"))

    # Find max eval level across all games for analysis_info
    max_ply = 0
    for g in all_games:
        for m in g.get("moves", []):
            p = m.get("ply")
            if p and int(p) > max_ply:
                max_ply = int(p)

    timestamp = 0
    date_str = data.get("date", "")
    if date_str:
        try:
            from datetime import datetime, timezone
            d = date_str.replace(".", "-")
            dt = datetime.fromisoformat(f"{d}T00:00:00")
            timestamp = int(dt.replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            pass

    ogxm: dict = {
        "match_length": matchlen,
        "player_white": player_white,
        "player_black": player_black,
        "white_score": score_white,
        "black_score": score_black,
        "result": match_result,
        "source": 4,  # bgblitz_import
        "timestamp": timestamp,
        "crawford": bool(data.get("useCrawford", False)),
        "jacoby": bool(data.get("useJacoby", False)),
        "beaver": bool(data.get("useBeaver", False)),
        "cube_limit": int(data.get("cubeLimit", 64)),
        "event": event_str,
        "site": site_str,
        "analysis_info": {
            "ply": max(1, max_ply),
            "eval_level": f"{max(1, max_ply)}ply",
            "model_id": "bgblitz",
            "timestamp": timestamp,
        },
        "games": games_out,
    }
    return ogxm


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Convert a BGBlitz .bgf file to OGXM JSON.")
    parser.add_argument("bgf_file", help="Input .bgf file")
    parser.add_argument(
        "output",
        nargs="?",
        metavar="OUTPUT",
        help="Output file (default: input name with .gva or .gva.gz extension)",
    )
    parser.add_argument(
        "-z", "--compress",
        action="store_true",
        help="Write gzip-compressed output (implied by .gz output filename)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON (default: compact)",
    )
    args = parser.parse_args()

    bgf_path = Path(args.bgf_file)
    if not bgf_path.exists():
        print(f"File not found: {bgf_path}", file=sys.stderr)
        sys.exit(1)

    out = convert_bgf(bgf_path)

    out_path = (
        Path(args.output) if args.output
        else bgf_path.with_suffix(".gva.gz" if args.compress else ".gva")
    )
    indent = 2 if args.pretty else None
    seps = None if args.pretty else (",", ":")
    data = json.dumps(out, indent=indent, separators=seps, ensure_ascii=False).encode()
    if args.compress or out_path.suffix == ".gz":
        with gzip.open(out_path, "wb") as f:
            f.write(data)
    else:
        out_path.write_bytes(data)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
