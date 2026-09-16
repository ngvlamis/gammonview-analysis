# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Pure-Python codec: bgsage mover-perspective board <-> OGID position string.

OGID ("OpenGammon Position ID") is a colon-separated backgammon position
format:

    white_positions:black_positions:cube:dice:color:game_state:
        score_w:score_b:match_length[:move_id[:nrof_checkers]]

e.g. the standard starting position (White on roll, sorted canonical form):

    11ccccchhhjjjjj:66666888dddddoo:N0N::W:IW:0:0:1:0

The full field spec is HedgeHog's ``docs/OGID.md``, at
https://gitlab.com/eranlambooij/hedgehog-public -- OGID is its format. This
module matches the GammonView production generator byte-for-byte, including
ascending-sorted position strings and always emitting through ``move_id``, with
``nrof_checkers`` appended only when it differs from 15. ``gvformat-js/src/ogid.js``
is the maintained JavaScript mirror and is in this repository.

Note on field 5: it names the player who *reached* the position -- who acted --
so the player **on roll is its complement**. HedgeHog's spec states this
correctly; be wary of any restatement that reads it as the player to move.

``board_to_ogid`` is the encoder; ``parse_ogid`` is its inverse (OGID string
-> :class:`OgidState`, whose ``board`` is the same mover-perspective array).
``looks_like_ogid`` tells an OGID from an XGID for callers that accept either.

No third-party dependencies -- stdlib only. This module does NOT import
bgsage; callers pass a plain ``list[int]`` board (bgsage's own array
convention, e.g. ``bgsage.STARTING_BOARD``).

Board convention (mover's perspective, as used throughout this repo --
see ``game_reconstructor.py``'s ``_apply_move_notation`` and
``bgsage.board.flip_board``, both confirmed empirically against bgsage):

  - Index 0:  the *opponent's* bar count (non-negative).
  - Index 25: the *mover's own* bar count (non-negative).
  - Index 1-24: signed count at the mover's own point i (1 = mover's
    ace/closest-to-bear-off, 24 = mover's farthest point/entry point).
    Positive = mover's own checkers, negative = opponent's checkers
    (magnitude = count).

OGID positions are ABSOLUTE (a single fixed frame, independent of whose
turn it is): pip 0 is White's bar, pip 25 is Black's bar, and points 1-24
run from White's home board (1-6) to Black's home board (19-24). Given the
above mover-perspective board and ``mover_is_white``, the absolute pip for
raw index i is:

    absolute_pip(i) = 25 - i   if mover_is_white
    absolute_pip(i) = i        if mover_is_black

This was derived and cross-validated against the canonical worked example
in OGID.md (the standard starting position) and against an independent
asymmetric golden vector from gammonview's ``xgid.test.js``, and confirmed
consistent with ``bgsage.board.flip_board``'s actual index-swap behaviour
(``flip_board(i) == 25 - i``, sign-negated for indices 1-24, unsigned copy
for the bar slots 0/25) -- see the ``__main__`` validation block below and
the accompanying task report for the full derivation. Note this directly
contradicts the informal formula sketched in ``OGXM_TRANSITION.md``
("White moving: point X -> absolute X"); that doc is a plan for a
different (not-yet-implemented) OGXM move-step feature and was NOT treated
as authoritative here -- the canonical OGID.md spec and the working
board.js/xgid.test.js reference implementation were.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import unquote

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: base-26 pip characters, index == pip value (0-25): '0'-'9' then 'a'-'p'.
_PIP_CHARS = "0123456789abcdefghijklmnop"

#: OGID cube-owner characters that may be passed straight through.
_ABSOLUTE_CUBE_OWNERS = {"N", "W", "B", "D", "?"}

#: OGID cube-action characters.
_CUBE_ACTIONS = {"N", "O", "T", "P"}


# ---------------------------------------------------------------------------
# Field encoders
# ---------------------------------------------------------------------------

def _pip_char(pip: int) -> str:
    if not (0 <= pip <= 25):
        raise ValueError(f"pip out of range 0-25: {pip}")
    return _PIP_CHARS[pip]


def _absolute_positions(board: list[int], mover_is_white: bool) -> tuple[str, str]:
    """Convert a mover-perspective board to (white_positions, black_positions).

    Both returned strings are the OGID base-26 checker encoding, sorted in
    ascending pip order (matching board.js's ``toPositionString``, which
    sorts the character array before joining).
    """
    if len(board) != 26:
        raise ValueError(f"board must have exactly 26 entries, got {len(board)}")

    white_pips: list[int] = []
    black_pips: list[int] = []

    for i, count in enumerate(board):
        if count == 0:
            continue

        if i == 0:
            # Opponent's bar: stored as a plain non-negative count.
            owner_is_mover = False
            n = count
        elif i == 25:
            # Mover's own bar: stored as a plain non-negative count.
            owner_is_mover = True
            n = count
        else:
            owner_is_mover = count > 0
            n = abs(count)

        absolute_pip = (25 - i) if mover_is_white else i
        owner_is_white = mover_is_white if owner_is_mover else (not mover_is_white)
        (white_pips if owner_is_white else black_pips).extend([absolute_pip] * n)

    white_positions = "".join(_pip_char(p) for p in sorted(white_pips))
    black_positions = "".join(_pip_char(p) for p in sorted(black_pips))
    return white_positions, black_positions


def _cube_value_exponent(cube_value: int) -> int:
    """Log2 exponent for the cube-value field (1->0, 2->1, 4->2, ... 64->6)."""
    if cube_value < 1 or (cube_value & (cube_value - 1)) != 0:
        raise ValueError(f"cube_value must be a power of two >= 1, got {cube_value}")
    return cube_value.bit_length() - 1


def _cube_owner_char(cube_owner: str, mover_is_white: bool) -> str:
    """Resolve a cube owner into an OGID owner character (N/W/B/D/?).

    Accepts either an absolute OGID character directly ("N", "W", "B", "D",
    "?"), or one of the mover-relative keywords this repo's bgsage API and
    ``game_reconstructor.py`` already use ("centered", "player", "opponent",
    "dead", "unknown"), or the literal color names "white"/"black".
    """
    if cube_owner in _ABSOLUTE_CUBE_OWNERS:
        return cube_owner

    key = cube_owner.lower()
    if key == "centered":
        return "N"
    if key == "dead":
        return "D"
    if key == "unknown":
        return "?"
    if key == "player":
        return "W" if mover_is_white else "B"
    if key == "opponent":
        return "B" if mover_is_white else "W"
    if key == "white":
        return "W"
    if key == "black":
        return "B"
    raise ValueError(f"Unrecognized cube_owner: {cube_owner!r}")


def _encode_cube(
    cube_value: int, cube_owner: str, cube_action: str, mover_is_white: bool
) -> str:
    if cube_action not in _CUBE_ACTIONS:
        raise ValueError(f"cube_action must be one of {_CUBE_ACTIONS}, got {cube_action!r}")
    owner_char = _cube_owner_char(cube_owner, mover_is_white)
    return f"{owner_char}{_cube_value_exponent(cube_value)}{cube_action}"


def _encode_dice(dice: tuple[int, int] | None) -> str:
    if not dice:
        return ""
    d1, d2 = dice
    if not (1 <= d1 <= 6 and 1 <= d2 <= 6):
        raise ValueError(f"dice values must each be 1-6, got {dice}")
    # Reference regenerates dice from the unordered action_id, so the field
    # is always ascending (min first) regardless of roll order.
    lo, hi = (d1, d2) if d1 <= d2 else (d2, d1)
    return f"{lo}{hi}"


def _encode_match_length(
    match_length: int,
    crawford: bool,
    post_crawford: bool,
    max_games: int | None,
) -> str:
    s = str(match_length)
    if crawford:
        s += "C"
    elif max_games is not None:
        s += f"G{max_games}"
    elif post_crawford:
        s += "L"
    return s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def board_to_ogid(
    board: list[int],
    *,
    mover_is_white: bool,
    cube_value: int = 1,
    cube_owner: str = "centered",
    cube_action: str = "N",
    dice: tuple[int, int] | None = None,
    on_roll: str | None = None,
    game_state: str = "",
    score_white: int = 0,
    score_black: int = 0,
    match_length: int = 0,
    crawford: bool = False,
    post_crawford: bool = False,
    max_games: int | None = None,
    move_id: int = 0,
    nrof_checkers: int = 15,
) -> str:
    """Encode a bgsage mover-perspective board as an OGID position string.

    Args:
        board: 26-element list[int], mover's perspective (see module
            docstring for the index convention). This is exactly the
            format bgsage uses, e.g. ``bgsage.STARTING_BOARD`` or any
            board returned by ``bgsage.board.flip_board`` /
            ``possible_moves``.
        mover_is_white: True if the player whose perspective ``board`` is
            recorded in (positive counts) is White; False if Black.
            Convention: White = Player 1, Black = Player 2.
        cube_value: current cube value (1, 2, 4, 8, 16, 32, or 64).
        cube_owner: "centered" (default), "player" (the mover owns it),
            "opponent" (the mover's opponent owns it), "dead", "unknown",
            or an absolute "white"/"black"/one of the raw OGID owner
            characters "N"/"W"/"B"/"D"/"?".
        cube_action: OGID cube action char: "N" (normal, default), "O"
            (offered), "T" (taken), "P" (passed/dropped).
        dice: (die1, die2) tuple, each 1-6, or None/empty for no roll yet.
        on_roll: override for the on-roll player ("W" or "B"), used to
            derive the OGID color field. Defaults to the mover's color
            (``mover_is_white``), which is correct for essentially all
            callers since ``board`` is, by this repo's convention, always
            the on-roll player's board. Per the OGID spec (Field 5,
            "Player Who Reached Position") and the reference encoder
            (``ogid.cpp``: ``color_char = (info->color == BG_WHITE) ? 'B'
            : 'W'``), the emitted color field is the COMPLEMENT of the
            on-roll player -- the player who just moved to reach this
            position, not the player about to move.
        game_state: OGID game-state code (e.g. "IW", "IB", "G", "M", "RG",
            "RM", or any engine-specific code such as "C"/"R"). Default ""
            (normal play, no special state).
        score_white: White's current match score.
        score_black: Black's current match score.
        match_length: match length in points; 0 = money game.
        crawford: True if this is the Crawford game (adds "C" to the
            match-length field).
        post_crawford: True if this is a post-Crawford game (adds "L").
            Ignored if ``crawford`` or ``max_games`` is set.
        max_games: if set, encodes a fixed-number-of-games money session
            (adds "G<max_games>"). Takes precedence over ``post_crawford``.
        move_id: move counter (default 0).
        nrof_checkers: checkers per side (default 15; the standard-game
            default). Only appended to the OGID string when it is not 15,
            matching board.js's ``toPositionString``.

    Returns:
        The OGID position string, always including fields through
        ``move_id`` (10 colon-separated fields), with the optional
        11th ``nrof_checkers`` field appended only when it differs
        from 15 -- this matches gammonview's ``BoardState.toPositionString``
        exactly (the production OGID generator this module mirrors).
    """
    white_positions, black_positions = _absolute_positions(board, mover_is_white)
    cube_field = _encode_cube(cube_value, cube_owner, cube_action, mover_is_white)
    dice_field = _encode_dice(dice)

    if on_roll is None:
        on_roll_color = "W" if mover_is_white else "B"
    else:
        if on_roll not in ("W", "B"):
            raise ValueError(f"on_roll must be 'W' or 'B', got {on_roll!r}")
        on_roll_color = on_roll
    # OGID color field = the player who REACHED this position (the
    # complement of on-roll), matching ogid.cpp's color_char derivation.
    color = "B" if on_roll_color == "W" else "W"

    match_length_field = _encode_match_length(
        match_length, crawford, post_crawford, max_games
    )

    parts = [
        white_positions,
        black_positions,
        cube_field,
        dice_field,
        color,
        game_state,
        str(score_white),
        str(score_black),
        match_length_field,
        str(move_id),
    ]
    ogid = ":".join(parts)
    if nrof_checkers != 15:
        ogid += f":{nrof_checkers}"
    return ogid


# ---------------------------------------------------------------------------
# Decoder: OGID position string -> mover-perspective board + state
# ---------------------------------------------------------------------------

#: The shape the OpenGammon backend accepts (``backend/match/boardstate.py``,
#: quoted in ``frontend/.../ogid.js``): only the first five fields are
#: constrained, the numeric tail is optional. Used to tell an OGID from an
#: XGID -- an XGID fails it on field 1 (it contains '-' and uppercase A-O)
#: and on field 3 (a bare signed integer, not ``[BWND?]\d+[OTPN]``).
_OGID_SHAPE = re.compile(r"^[0-9a-p]*:[0-9a-p]*:[BWND?]\d+[OTPN]:\d{0,2}:[WB]")

#: Field 9, ``<length>[LCG][<max_games>]``.
_MATCH_LENGTH_RE = re.compile(r"^(\d+)([LCG]?)(\d*)$")

#: Mover-relative cube owner per OGID owner character, given whether the
#: mover is White. "?" (uninitialized) reads as a centered cube, matching
#: ogid.js's sanitizeBoard.
_RELATIVE_CUBE_OWNER = {
    "N": ("centered", "centered"),
    "D": ("dead", "dead"),
    "?": ("centered", "centered"),
    "W": ("player", "opponent"),
    "B": ("opponent", "player"),
}

#: Game states that are -- or have just resolved -- a cube decision
#: (``CUBE_STATES`` in ogid.js): C offered-to-be-made, D pending double,
#: A taken, P passed.
CUBE_GAME_STATES = frozenset({"C", "D", "A", "P"})


@dataclass(frozen=True)
class OgidState:
    """A parsed OGID, with the board in the on-roll player's perspective.

    ``board`` uses the same 26-entry mover-perspective convention
    ``board_to_ogid`` consumes (see the module docstring), so
    ``board_to_ogid(state.board, mover_is_white=state.mover_is_white, ...)``
    round-trips back to the original string.
    """

    board: list[int]
    mover_is_white: bool      # True when the on-roll player is White
    die1: int                 # 0 when no dice are set
    die2: int
    cube_value: int           # 1, 2, 4, ... 64
    cube_owner: str           # mover-relative: centered/player/opponent/dead
    cube_action: str          # raw OGID action char: N/O/T/P
    on_roll: str              # "W" or "B" -- the player who owes the next action
    color: str                # raw field 5: the player who *made* the last action
    game_state: str
    score_white: int
    score_black: int
    match_length: int         # 0 = money game
    crawford: bool
    post_crawford: bool
    max_games: int | None
    move_id: int
    nrof_checkers: int

    @property
    def is_money(self) -> bool:
        return self.match_length == 0

    @property
    def away1(self) -> int:
        """Points the on-roll player still needs; 0 for a money game."""
        if self.is_money:
            return 0
        score = self.score_white if self.mover_is_white else self.score_black
        return self.match_length - score

    @property
    def away2(self) -> int:
        """Points the opponent still needs; 0 for a money game."""
        if self.is_money:
            return 0
        score = self.score_black if self.mover_is_white else self.score_white
        return self.match_length - score

    @property
    def is_cube_decision(self) -> bool:
        """True when the position is (or just resolved) a cube decision."""
        return self.game_state in CUBE_GAME_STATES

    def flipped(self) -> OgidState:
        """The same position read from the other player's perspective."""
        board = flip_board(self.board)
        owner = {"player": "opponent", "opponent": "player"}.get(
            self.cube_owner, self.cube_owner
        )
        return replace(
            self,
            board=board,
            mover_is_white=not self.mover_is_white,
            cube_owner=owner,
            on_roll="B" if self.on_roll == "W" else "W",
        )


def flip_board(board: list[int]) -> list[int]:
    """Swap a mover-perspective board to the other player's perspective.

    Mirrors ``bgsage.board.flip_board``: index ``i`` maps to ``25 - i``,
    sign-negated for the points (1-24) and copied unsigned for the two bar
    slots (0 = opponent's bar, 25 = mover's own bar).
    """
    if len(board) != 26:
        raise ValueError(f"board must have exactly 26 entries, got {len(board)}")
    flipped = [0] * 26
    flipped[0] = board[25]
    flipped[25] = board[0]
    for i in range(1, 25):
        flipped[25 - i] = -board[i]
    return flipped


def looks_like_ogid(text: str) -> bool:
    """True when ``text`` parses as an OGID rather than an XGID.

    An explicit ``OGID=``/``OGID:`` or ``XGID=``/``XGID:`` label decides it;
    otherwise the OpenGammon backend's position-shape regex does. An XGID
    never matches that shape (its board field carries ``-`` and ``A``-``O``,
    and its cube field is a bare integer).
    """
    s = text.strip().strip("\"'`")
    if re.match(r"^ogid[=:]", s, re.IGNORECASE):
        return True
    if re.match(r"^xgid[=:]", s, re.IGNORECASE):
        return False
    return bool(_OGID_SHAPE.match(s))


def _parse_cube(field: str) -> tuple[int, str, str]:
    """``"W1O"`` -> (cube_value, owner char, action char)."""
    m = re.fullmatch(r"([BWND?])(\d+)([OTPN])", field)
    if not m:
        raise ValueError(f"Invalid OGID cube field: {field!r}")
    exponent = int(m.group(2))
    if exponent > 6:
        raise ValueError(f"OGID cube exponent out of range 0-6: {exponent}")
    return 1 << exponent, m.group(1), m.group(3)


def _parse_dice(field: str) -> tuple[int, int]:
    if field == "":
        return 0, 0
    if not re.fullmatch(r"[1-6][1-6]", field):
        raise ValueError(f"Invalid OGID dice field: {field!r}")
    return int(field[0]), int(field[1])


def _parse_match_length(field: str) -> tuple[int, bool, bool, int | None]:
    """``"7C"`` -> (match_length, crawford, post_crawford, max_games)."""
    m = _MATCH_LENGTH_RE.match(field.strip())
    if not m:
        # board.js falls back to a money game rather than throwing.
        return 0, False, False, None
    modifier = m.group(2)
    max_games = int(m.group(3)) if m.group(3) else None
    return int(m.group(1)), modifier == "C", modifier == "L", max_games


def _pip_value(ch: str) -> int:
    pip = _PIP_CHARS.find(ch)
    if pip < 0:
        raise ValueError(f"Invalid OGID position character: {ch!r}")
    return pip


def _board_from_positions(
    white_positions: str, black_positions: str, mover_is_white: bool
) -> list[int]:
    """Inverse of ``_absolute_positions``.

    Absolute pip -> raw index is the inverse of the encoder's mapping:
    ``i = 25 - pip`` when the mover is White, ``i = pip`` when Black.
    """
    board = [0] * 26
    for positions, owner_is_white in ((white_positions, True), (black_positions, False)):
        for ch in positions:
            pip = _pip_value(ch)
            owner_is_mover = owner_is_white == mover_is_white
            i = (25 - pip) if mover_is_white else pip
            if i in (0, 25):
                # Bar slots hold a plain count: index 25 is the mover's own
                # bar, index 0 the opponent's. A checker on the *other*
                # colour's bar pip (White on 25, Black on 0) is not a
                # position that exists.
                if (i == 25) != owner_is_mover:
                    side = "White" if owner_is_white else "Black"
                    raise ValueError(
                        f"{side} checker on pip {pip} is not a valid OGID position"
                    )
                board[i] += 1
            else:
                board[i] += 1 if owner_is_mover else -1
    return board


def parse_ogid(ogid: str) -> OgidState:
    """Parse an OGID position string into an :class:`OgidState`.

    Accepts an optional ``OGID=``/``OGID:`` label and tolerates the wrapping
    quotes and trailing URL punctuation a pasted id arrives with (mirroring
    ``clean_raw_id`` in gammonview's ``ogid.js``). Fields 1-5 are required;
    the rest default as the spec says (game state ``IW``, 0-0, money game,
    move 0, 15 checkers).

    The returned ``board`` is in the perspective of the player who owes the
    next action -- ``playerToAct()`` in gammonview's ``board.js``, i.e. the
    *complement* of field 5, which records whoever made the last action. (The
    one exception board.js carves out, and this mirrors: the no-dice ``IW``
    that opens a game, where White still owes the opening roll.)
    """
    s = ogid.strip().strip("\"'`").strip()
    # A trailing fragment/slash left over from a URL. Note we do NOT split on
    # "?" the way gammonview's clean_raw_id does: "?" is a legal cube-owner
    # character ("?0N", an uninitialized cube), and splitting there would eat
    # the rest of the id.
    s = s.split("#")[0].rstrip("/").strip()
    if ":" not in s and "%3a" in s.lower():
        s = unquote(s)  # the id came straight out of a URL
    s = re.sub(r"^ogid[=:]", "", s, flags=re.IGNORECASE).strip()

    parts = s.split(":")
    if len(parts) < 5:
        raise ValueError(f"OGID needs at least 5 fields, got {len(parts)}: {ogid!r}")

    cube_value, owner_char, cube_action = _parse_cube(parts[2])
    die1, die2 = _parse_dice(parts[3])

    color = parts[4]
    if color not in ("W", "B"):
        raise ValueError(f"OGID color field must be 'W' or 'B', got {color!r}")

    game_state = parts[5] if len(parts) > 5 else "IW"

    # Field 5 names the player who *acted*; the next actor is the other one.
    # (board.js's playerToAct() returns null for a finished game; there is no
    # board to analyze there, so we still report the complement and leave the
    # game_state for the caller to notice.)
    if game_state == "IW" and die1 == 0:
        on_roll = "W"
    else:
        on_roll = "B" if color == "W" else "W"
    mover_is_white = on_roll == "W"

    board = _board_from_positions(parts[0], parts[1], mover_is_white)

    score_white = _int_or(parts[6], 0) if len(parts) > 6 else 0
    score_black = _int_or(parts[7], 0) if len(parts) > 7 else 0
    if len(parts) > 8:
        match_length, crawford, post_crawford, max_games = _parse_match_length(parts[8])
    else:
        match_length, crawford, post_crawford, max_games = 0, False, False, None
    move_id = _int_or(parts[9], 0) if len(parts) > 9 else 0
    nrof_checkers = _int_or(parts[10], 15) if len(parts) > 10 else 15

    return OgidState(
        board=board,
        mover_is_white=mover_is_white,
        die1=die1,
        die2=die2,
        cube_value=cube_value,
        cube_owner=_RELATIVE_CUBE_OWNER[owner_char][0 if mover_is_white else 1],
        cube_action=cube_action,
        on_roll=on_roll,
        color=color,
        game_state=game_state,
        score_white=score_white,
        score_black=score_black,
        match_length=match_length,
        crawford=crawford,
        post_crawford=post_crawford,
        max_games=max_games,
        move_id=move_id,
        nrof_checkers=nrof_checkers,
    )


def _int_or(field: str, default: int) -> int:
    """Parse an integer field, falling back like board.js's NaN guards."""
    try:
        return int(field)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Self-test / golden-vector validation
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # bgsage.STARTING_BOARD, inlined so this module stays stdlib-only and
    # the test doesn't require bgsage to be installed.
    _STARTING_BOARD = [
        0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5,
        5, 0, 0, 0, -3, 0, -5, 0, 0, 0, 0, 2, 0,
    ]

    checks = 0

    def check(actual: str, expected: str, label: str) -> None:
        global checks
        assert actual == expected, f"[{label}] {actual!r} != {expected!r}"
        checks += 1
        print(f"OK  {label}: {actual}")

    # --- 1. Standard starting position, White on roll -----------------------
    # Canonical (sorted) form of OGID.md's worked example
    # "11jjjjjhhhccccc:ooddddd88866666:N0N::W:IW:0:0:1:0" -- OGID.md's own
    # string is NOT ascending-sorted (it lists checkers in diagram-reading
    # order), but board.js's toPositionString() always sorts ascending, so
    # the byte-exact reference output is "11ccccchhhjjjjj:66666888dddddoo:...".
    # Content (the checker multiset per point) is identical either way.
    # NOTE: the OGID color field is the player who REACHED this position --
    # the complement of on-roll (see ogid.cpp's color_char derivation and
    # OGID.md Field 5). White on roll -> color field is 'B'.
    ogid = board_to_ogid(
        _STARTING_BOARD, mover_is_white=True, game_state="IW", match_length=1,
    )
    check(
        ogid,
        "11ccccchhhjjjjj:66666888dddddoo:N0N::B:IW:0:0:1:0",
        "starting position, White on roll (color = complement 'B')",
    )

    # --- 2. Same physical position, Black on roll ---------------------------
    # The starting position is symmetric under bgsage.board.flip_board, so
    # the identical raw board array, read as Black's perspective, encodes
    # to the SAME absolute white/black position strings -- only the color
    # field changes. Black on roll -> color field is the complement, 'W'.
    ogid = board_to_ogid(
        _STARTING_BOARD, mover_is_white=False, game_state="IB", match_length=1,
    )
    check(
        ogid,
        "11ccccchhhjjjjj:66666888dddddoo:N0N::W:IB:0:0:1:0",
        "starting position, Black on roll (color = complement 'W')",
    )

    # --- 3. Asymmetric position, White on roll (checkers already home) ------
    # Golden vector (gammonview xgid.test.js "asymmetric, turn 1"):
    #   OGID "jjjkkklllmmnnoo:111222333445566:N0N::W:C:0:0:0"
    # White (absolute) has 19x3,20x3,21x3,22x2,23x2,24x2; Black (absolute)
    # has 1x3,2x3,3x3,4x2,5x2,6x2. With mover_is_white=True the formula is
    # absolute = 25 - i, so White's own (positive) raw indices are 1-6 with
    # counts REVERSED relative to the absolute pip order (idx1->pip24 count2,
    # ... idx6->pip19 count3), and Black's (negative) are at 19-24 mirrored
    # likewise. This is the case that caught an ordering bug during
    # development (see task report) -- kept as a regression test.
    board = [0] * 26
    board[1], board[2], board[3] = 2, 2, 2
    board[4], board[5], board[6] = 3, 3, 3
    board[19], board[20], board[21] = -2, -2, -2
    board[22], board[23], board[24] = -3, -3, -3
    ogid = board_to_ogid(board, mover_is_white=True, game_state="C")
    check(
        ogid,
        "jjjkkklllmmnnoo:111222333445566:N0N::B:C:0:0:0:0",
        "asymmetric position, White on roll (regression: index reversal; color = complement 'B')",
    )

    # --- 4. Same asymmetric position, Black on roll -------------------------
    # Golden vector: OGID "jjjkkklllmmnnoo:111222333445566:N0N::B:C:0:0:0"
    # (identical absolute positions, only color differs). With
    # mover_is_white=False the formula is absolute = i (identity, no
    # reversal), and this board is exactly bgsage.board.flip_board() of the
    # White-mover board above (verified against the actual bgsage
    # flip_board implementation during development).
    board = [0] * 26
    board[1], board[2], board[3] = 3, 3, 3
    board[4], board[5], board[6] = 2, 2, 2
    board[19], board[20], board[21] = -3, -3, -3
    board[22], board[23], board[24] = -2, -2, -2
    ogid = board_to_ogid(board, mover_is_white=False, game_state="C")
    check(
        ogid,
        "jjjkkklllmmnnoo:111222333445566:N0N::W:C:0:0:0:0",
        "asymmetric position, Black on roll (color = complement 'W')",
    )

    # --- 5. Checkers on both bars --------------------------------------------
    # Golden vector: OGID "00jjj:ppp66:N0N::W:C:0:0:0"
    # White (absolute): 0x2 (White's bar), 19x3. Black (absolute): 25x3
    # (Black's bar), 6x2. Mover is White: mover's own bar is raw index 25
    # (2 checkers -> absolute pip 0), opponent's bar is raw index 0
    # (3 checkers -> absolute pip 25).
    board = [0] * 26
    board[0] = 3       # opponent's (Black's) bar
    board[6] = 3        # mover's (White's) own checkers -> absolute pip 19
    board[19] = -2       # opponent's (Black's) checkers -> absolute pip 6
    board[25] = 2       # mover's (White's) own bar
    ogid = board_to_ogid(board, mover_is_white=True, game_state="C")
    # Note: xgid.test.js's literal "00jjj:ppp66:N0N::W:C:0:0:0" is not
    # ascending-sorted either (pip6 < pip25, so canonical order is "66ppp",
    # not "ppp66") -- same reconciliation as the starting-position case.
    # Color field is the complement of on-roll (White on roll -> 'B').
    check(
        ogid,
        "00jjj:66ppp:N0N::B:C:0:0:0:0",
        "checkers on both bars (color = complement 'B')",
    )

    # --- 6. Cube ownership: mover owns it (White owns -> "W1N") -------------
    # Golden vector: "11jjjjjhhhccccc:ooddddd88866666:W1N::W:C:0:0:0"
    ogid = board_to_ogid(
        _STARTING_BOARD, mover_is_white=True, game_state="C",
        cube_value=2, cube_owner="player",
    )
    assert ogid.split(":")[2] == "W1N", ogid
    checks += 1
    print("OK  cube: mover(White) owns -> W1N")

    # --- 7. Cube ownership: opponent owns it (Black owns -> "B1N") ----------
    # Golden vector: "11jjjjjhhhccccc:ooddddd88866666:B1N::W:C:0:0:0"
    ogid = board_to_ogid(
        _STARTING_BOARD, mover_is_white=True, game_state="C",
        cube_value=2, cube_owner="opponent",
    )
    assert ogid.split(":")[2] == "B1N", ogid
    checks += 1
    print("OK  cube: opponent(Black) owns -> B1N")

    # --- 8. Scores, match length, dice, crawford, nrof_checkers -------------
    ogid = board_to_ogid(
        _STARTING_BOARD, mover_is_white=True, game_state="C",
        dice=(6, 3), score_white=3, score_black=5, match_length=7, crawford=True,
    )
    fields = ogid.split(":")
    # Dice field is always sorted ascending (min then max), regardless of
    # rolled order -- matches the reference, which regenerates dice from
    # the unordered action_id.
    assert fields[3] == "36", fields
    assert fields[6:9] == ["3", "5", "7C"], fields
    checks += 1
    print("OK  dice/score/crawford fields (dice sorted ascending)")

    ogid = board_to_ogid(_STARTING_BOARD, mover_is_white=True, nrof_checkers=15)
    assert len(ogid.split(":")) == 10, ogid  # omitted when == 15
    ogid = board_to_ogid(_STARTING_BOARD, mover_is_white=True, nrof_checkers=20)
    assert ogid.endswith(":20") and len(ogid.split(":")) == 11, ogid
    checks += 1
    print("OK  nrof_checkers omitted iff == 15")

    # --- 9. Cube value exponent table ----------------------------------------
    for value, exponent in [(1, 0), (2, 1), (4, 2), (8, 3), (16, 4), (32, 5), (64, 6)]:
        assert _cube_value_exponent(value) == exponent
    checks += 1
    print("OK  cube value exponent table")

    # --- 10. parse_ogid round-trips every encoder golden vector -------------
    for label, ogid in [
        ("starting, W on roll", "11ccccchhhjjjjj:66666888dddddoo:N0N::B:R:0:0:1:0"),
        ("starting, B on roll", "11ccccchhhjjjjj:66666888dddddoo:N0N::W:IB:0:0:1:0"),
        ("asymmetric, W on roll", "jjjkkklllmmnnoo:111222333445566:N0N::B:C:0:0:0:0"),
        ("asymmetric, B on roll", "jjjkkklllmmnnoo:111222333445566:N0N::W:C:0:0:0:0"),
        ("both bars", "00jjj:66ppp:N0N::B:C:0:0:0:0"),
        ("owned cube + dice + crawford",
         "11ccccchhhjjjjj:66666888dddddoo:W1N:36:B:R:3:5:7C:0"),
        ("variant checker count",
         "11ccccchhhjjjjj:66666888dddddoo:N0N::B:R:0:0:0:0:20"),
    ]:
        st = parse_ogid(ogid)
        again = board_to_ogid(
            st.board,
            mover_is_white=st.mover_is_white,
            cube_value=st.cube_value,
            cube_owner=st.cube_owner,
            cube_action=st.cube_action,
            dice=(st.die1, st.die2) if st.die1 else None,
            on_roll=st.on_roll,
            game_state=st.game_state,
            score_white=st.score_white,
            score_black=st.score_black,
            match_length=st.match_length,
            crawford=st.crawford,
            post_crawford=st.post_crawford,
            max_games=st.max_games,
            move_id=st.move_id,
            nrof_checkers=st.nrof_checkers,
        )
        check(again, ogid, f"round-trip: {label}")

    # --- 11. flipped() is an involution, and agrees with the encoder --------
    st = parse_ogid("00jjj:66ppp:N0N::B:C:0:0:0:0")
    assert st.on_roll == "W" and st.board[25] == 2 and st.board[0] == 3, st
    flipped = st.flipped()
    assert flipped.on_roll == "B" and flipped.board[25] == 3 and flipped.board[0] == 2
    assert flipped.flipped().board == st.board
    # Same absolute position either way -- only the color field differs.
    assert board_to_ogid(
        flipped.board, mover_is_white=False, game_state="C", on_roll="B",
    ) == "00jjj:66ppp:N0N::W:C:0:0:0:0"
    checks += 1
    print("OK  flipped(): involution + same absolute position")

    # --- 12. Cube owner is read relative to the mover ------------------------
    # White owns the cube; White on roll (color 'B') -> "player", and the
    # same string with Black on roll -> "opponent".
    assert parse_ogid(
        "11ccccchhhjjjjj:66666888dddddoo:W1N::B:R:0:0:0:0").cube_owner == "player"
    assert parse_ogid(
        "11ccccchhhjjjjj:66666888dddddoo:W1N::W:R:0:0:0:0").cube_owner == "opponent"
    assert parse_ogid(
        "11ccccchhhjjjjj:66666888dddddoo:D1N::B:R:0:0:0:0").cube_owner == "dead"
    checks += 1
    print("OK  cube owner resolved mover-relative (incl. dead cube)")

    # --- 13. away counts, minimal ids, and format detection -----------------
    st = parse_ogid("11ccccchhhjjjjj:66666888dddddoo:N0N:36:B:R:3:5:7C:0")
    assert st.on_roll == "W" and (st.away1, st.away2) == (4, 2), st
    assert st.crawford and (st.die1, st.die2) == (3, 6)
    # Fields 6-11 are optional; the spec's defaults apply.
    st = parse_ogid("11ccccchhhjjjjj:66666888dddddoo:N0N::W")
    assert st.game_state == "IW" and st.match_length == 0 and st.nrof_checkers == 15
    assert (st.away1, st.away2) == (0, 0) and st.is_money
    # The no-dice IW exception: White owes the opening roll despite color "W".
    assert st.on_roll == "W", st
    assert looks_like_ogid("11ccccchhhjjjjj:66666888dddddoo:N0N::W:R:0:0:0:0")
    assert not looks_like_ogid("-b----E-C---eE---c-e----B-:0:-1:1:31:0:0:0:0")
    assert not looks_like_ogid("XGID=-b----E-C---eE---c-e----B-:0:-1:1:31:0:0:0:0")
    assert looks_like_ogid("OGID=11ccccchhhjjjjj:66666888dddddoo:N0N::W")
    checks += 1
    print("OK  away counts, optional-field defaults, XGID/OGID detection")

    print(f"\nAll {checks} checks passed.")
