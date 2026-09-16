# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Parse Jellyfish/GNUbg/OpenGammon .mat match files into structured data."""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# Handles both "Match of N points" and "N point(s) match"
_MATCH_LEN_RE = re.compile(r"[Mm]atch\s+of\s+(\d+)|(\d+)\s+[Pp]oints?\s+[Mm]atch")
_GAME_HEADER_RE = re.compile(r"^\s*[Gg]ame\s+(\d+)\s*$")
_SCORE_LINE_RE = re.compile(r"^(.+?)\s*:\s*(\d+)\s{2,}(.+?)\s*:\s*(\d+)\s*$")
_TURN_NUM_RE = re.compile(r"^\s*\d+\)(.*)", re.DOTALL)
_DOUBLE_RE = re.compile(r"[Dd]oubles?\s*=>\s*(\d+)")
_TAKES_RE = re.compile(r"^[Tt]akes\b|^[Aa]ccepts\b")
_DROPS_RE = re.compile(r"^[Dd]rops\b|^[Rr]ejects\b|^[Pp]asses\b")
_WINS_RE = re.compile(r"^[Ww]ins\b")
_WINS_DETAIL_RE = re.compile(
    r"^[Ww]ins?\s+(\d+)\s+[Pp]oints?(?:\s+with\s+a?\s*(gammon|backgammon))?",
    re.IGNORECASE,
)
# "Resigns" is the Jellyfish/GNUbg wording; OpenGammon writes "Resigned Game"
# or "Resigned Match". Missing the latter meant every OpenGammon resignation
# parsed as "unknown" and was dropped before reconstruction ever saw it.
_RESIGNS_RE = re.compile(r"^[Rr]esign(?:s|ed)\b")
_FORFEITS_RE = re.compile(r"^[Ff]orfeits?\b")
_TIME_RE = re.compile(r"^[Ll]os(?:t|es)\s+on\s+time\b", re.IGNORECASE)
# Optional colon after dice: handles both "31 13/10" and "31: 13/10"
_MOVE_RE = re.compile(r"^(\d)(\d):?\s*(.*)")
# Player names from header comments: "; [Player 1 "name"]"
_P1_COMMENT_RE = re.compile(r';\s*\[Player 1 "([^"]+)"\]')
_P2_COMMENT_RE = re.compile(r';\s*\[Player 2 "([^"]+)"\]')
# Match rules and metadata from header comments
_CRAWFORD_RULE_RE = re.compile(r';\s*\[Crawford\s+"?(On|Off)"?\]', re.IGNORECASE)
_JACOBY_RE = re.compile(r';\s*\[Jacoby\s+"?(On|Off)"?\]', re.IGNORECASE)
_BEAVER_RE = re.compile(r';\s*\[Beaver\s+"?(On|Off)"?\]', re.IGNORECASE)
_CUBE_LIMIT_RE = re.compile(r';\s*\[CubeLimit\s+"?(\d+)"?\]', re.IGNORECASE)
_EVENT_RE = re.compile(r';\s*\[Event\s+"([^"]*)"\]')
_SITE_RE = re.compile(r';\s*\[Site\s+"([^"]*)"\]')
_DATE_RE = re.compile(r';\s*\[Date\s+"([^"]*)"\]')
_EVENT_DATE_RE = re.compile(r';\s*\[EventDate\s+"([^"]*)"\]')
_EVENT_TIME_RE = re.compile(r';\s*\[EventTime\s+"([^"]*)"\]')


# ---------------------------------------------------------------------------
# Action parsing
# ---------------------------------------------------------------------------

def _parse_action(s: str) -> tuple:
    s = s.strip()
    m = _DOUBLE_RE.match(s)
    if m:
        return ("double", int(m.group(1)))
    if _TAKES_RE.match(s):
        return ("take", None)
    if _DROPS_RE.match(s):
        return ("drop", None)
    m = _WINS_DETAIL_RE.match(s)
    if m:
        win_type = m.group(2).lower() if m.group(2) else "normal"
        return ("win", {"points": int(m.group(1)), "type": win_type})
    if _WINS_RE.match(s):
        return ("win", {"points": None, "type": "normal"})
    if _RESIGNS_RE.match(s):
        return ("resign", None)
    if _FORFEITS_RE.match(s):
        return ("forfeit", None)
    if _TIME_RE.match(s):
        return ("time", None)
    m = _MOVE_RE.match(s)
    if m:
        moves = m.group(3).strip()
        if moves == "???":
            # A dice line with no move behind it. Marked so reconstruction can
            # stop here rather than read on: unlike a resign line, this one
            # stands where a checker play should be, so the board after it is
            # not knowable.
            return ("resign", "unplayed")
        return ("move", (int(m.group(1)), int(m.group(2)), moves))
    return ("unknown", s)


def _split_player_actions(line: str, p1: str, p2: str) -> list[tuple[str, str]]:
    """Old-style format: player names appear in the line before each action."""
    if not p1 or not p2:
        return []
    names = sorted([p1, p2], key=len, reverse=True)
    pattern = re.compile(r"(?:^|\s)(" + "|".join(re.escape(n) for n in names) + r")\s*:")
    parts = pattern.split(line)
    result = []
    for i in range(1, len(parts), 2):
        player = parts[i]
        rest = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if rest:
            result.append((player, rest))
    return result


def _split_positional(rest: str, p1: str, p2: str) -> list[tuple[str, str]]:
    """New-style two-column format: left = P1, right = P2, split on 3+ spaces."""
    halves = re.split(r"\s{3,}", rest, maxsplit=1)
    left = halves[0].strip()
    right = halves[1].strip() if len(halves) > 1 else ""

    if not right and left:
        m = re.search(r" (\d\d:)", left[3:])
        if m:
            abs_pos = m.start() + 3
            right = left[abs_pos + 1:].strip()
            left = left[:abs_pos].strip()

    result = []
    for player, text in ((p1, left), (p2, right)):
        if not text:
            continue
        atype, _ = _parse_action(text)
        if atype != "unknown":
            result.append((player, text))
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_mat_file(text: str) -> dict:
    """Parse a Jellyfish/GNUbg .mat file.

    Handles both the player-name-prefixed format (GNUbg) and the two-column
    positional format used by OpenGammon / some Jellyfish variants.

    Returns:
        {
          'match_length': int,   # 0 = money game
          'player1': str,
          'player2': str,
          'crawford_rule': bool | None,
          'jacoby_rule': bool | None,
          'beaver_rule': bool | None,
          'cube_limit': int | None,
          'event': str,
          'site': str,
          'date': str,
          'event_time': str,
          'games': [
            {
              'game_number': int,
              'score1_start': int,
              'score2_start': int,
              'player_actions': [(player_name, action_str), ...],
            }, ...
          ]
        }
    """
    lines = text.splitlines()

    # --- Match length ---
    match_length = 0
    for line in lines[:30]:
        m = _MATCH_LEN_RE.search(line)
        if m:
            match_length = int(m.group(1) or m.group(2))
            break

    # --- Player names and match rules from header comments ---
    player1 = ""
    player2 = ""
    crawford_rule: bool | None = None
    jacoby_rule: bool | None = None
    beaver_rule: bool | None = None
    cube_limit: int | None = None
    event: str = ""
    site: str = ""
    date: str = ""
    event_time: str = ""

    for line in lines[:60]:
        if not player1:
            m = _P1_COMMENT_RE.search(line)
            if m:
                player1 = m.group(1).strip()
        if not player2:
            m = _P2_COMMENT_RE.search(line)
            if m:
                player2 = m.group(1).strip()
        if line.strip().startswith(";"):
            if crawford_rule is None:
                m = _CRAWFORD_RULE_RE.search(line)
                if m:
                    crawford_rule = m.group(1).lower() == "on"
            if jacoby_rule is None:
                m = _JACOBY_RE.search(line)
                if m:
                    jacoby_rule = m.group(1).lower() == "on"
            if beaver_rule is None:
                m = _BEAVER_RE.search(line)
                if m:
                    beaver_rule = m.group(1).lower() == "on"
            if cube_limit is None:
                m = _CUBE_LIMIT_RE.search(line)
                if m:
                    cube_limit = int(m.group(1))
            if not event:
                m = _EVENT_RE.search(line)
                if m:
                    event = m.group(1)
            if not site:
                m = _SITE_RE.search(line)
                if m:
                    site = m.group(1)
            if not date:
                m = _DATE_RE.search(line)
                if m:
                    date = m.group(1)
                else:
                    m = _EVENT_DATE_RE.search(line)
                    if m:
                        date = m.group(1)
            if not event_time:
                m = _EVENT_TIME_RE.search(line)
                if m:
                    event_time = m.group(1).replace(".", ":")

    games: list[dict] = []
    current_game: dict | None = None
    score_found = False
    # Per-game column players: the game score line tells us which player is on
    # the left vs right column, which can differ from the match-level player1/player2
    # (e.g. when the transcriber always writes their own name on the left).
    game_left_player = ""
    game_right_player = ""

    for line in lines:
        stripped = line.strip()

        gm = _GAME_HEADER_RE.match(line)
        if gm:
            if current_game is not None:
                games.append(current_game)
            current_game = {
                "game_number": int(gm.group(1)),
                "score1_start": 0,
                "score2_start": 0,
                "player_actions": [],
            }
            score_found = False
            game_left_player = player1
            game_right_player = player2
            continue

        if current_game is None:
            continue

        if not stripped or stripped.startswith(";"):
            continue

        # Score line comes before any move lines (no turn number)
        if not score_found and not re.match(r"^\s*\d+\)", line):
            sm = _SCORE_LINE_RE.match(line)
            if sm:
                left_name = sm.group(1).strip()
                left_score = int(sm.group(2))
                right_name = sm.group(3).strip()
                right_score = int(sm.group(4))
                if not player1 and left_name:
                    player1, player2 = left_name, right_name
                # Use the score line to determine which player is in which column
                # for this specific game (may differ from match-level ordering).
                game_left_player = left_name
                game_right_player = right_name
                # Normalize scores to match-level p1/p2 ordering so that
                # away calculations in the reconstructor are correct.
                if game_left_player == player1:
                    current_game["score1_start"] = left_score
                    current_game["score2_start"] = right_score
                else:
                    current_game["score1_start"] = right_score
                    current_game["score2_start"] = left_score
                score_found = True
                continue

        if not (player1 and player2):
            continue

        tm = _TURN_NUM_RE.match(line)

        actions = _split_player_actions(stripped, player1, player2)

        if not actions:
            if tm:
                actions = _split_positional(tm.group(1), game_left_player, game_right_player)
            else:
                atype, _ = _parse_action(stripped)
                if atype != "unknown":
                    leading = len(line) - len(line.lstrip())
                    player = game_right_player if leading >= 20 else game_left_player
                    actions = [(player, stripped)]

        current_game["player_actions"].extend(actions)

    if current_game is not None:
        games.append(current_game)

    return {
        "match_length": match_length,
        "player1": player1 or "Player 1",
        "player2": player2 or "Player 2",
        "crawford_rule": crawford_rule,
        "jacoby_rule": jacoby_rule,
        "beaver_rule": beaver_rule,
        "cube_limit": cube_limit,
        "event": event,
        "site": site,
        "date": date,
        "event_time": event_time,
        "games": games,
    }
