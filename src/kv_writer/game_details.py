"""game:{season}:{game_id}:details - see src/CLAUDE.md's KV writer section."""

import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from config.config import SEASON
from db.clients import get_d1, get_kv
from src.kv_writer.shared import for_current_week
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

# each {} is filled with one ? per game_id
_GAMES_SQL = """
SELECT game_id, home_team_id, away_team_id FROM games WHERE game_id IN ({})
"""

_TEAM_STATS_SQL = "SELECT * FROM game_team_stats WHERE game_id IN ({})"

_PLAYER_STATS_SQL = """
SELECT game_id, team_id, stat_group, player_name, sports_io_player_id,
    player_image, stats_json
FROM game_player_stats
WHERE game_id IN ({})
"""

_WIN_PROBABILITY_SQL = """
SELECT game_id, points_json FROM game_win_probability WHERE game_id IN ({})
"""

_WEEK_GAME_IDS_SQL = """
SELECT g.game_id
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
"""

# game_team_stats bookkeeping columns, not stats
_TEAM_STATS_KEY_COLUMNS = ("stat_id", "game_id", "team_id")

# the stat each group's players are ranked by, biggest first
_SORT_STAT = {
    "Passing": "yards",
    "Rushing": "yards",
    "Receiving": "yards",
    "Defensive": "tackles",
    "Kicking": "points",
    "Punting": "yards",
    "Kick_returns": "yards",
    "Punt_returns": "yards",
    "Interceptions": "yards",
    "Fumbles": "total",
}


def player_line(row: dict[str, Any]) -> dict[str, Any]:
    """one game_player_stats row as it appears in KV - shared with
    games.py's leaders"""
    return {
        "name": row["player_name"],
        "sports_io_player_id": row["sports_io_player_id"],
        "image": row["player_image"],
        "stats": json.loads(row["stats_json"]),
    }


def _sort_value(group: str, line: dict[str, Any]) -> float:
    value = line["stats"].get(_SORT_STAT.get(group, ""))
    return value if isinstance(value, int | float) else float("-inf")


def _punting(team_id: int, player_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """punts/punt_yards/punt_average for one team, summed from its
    Punting player lines - Sports IO's team stats have no punting at all.
    Summed rather than taking one punter, since a second player
    occasionally punts. All None until player stats exist; 0 punts (no
    Punting line) once they do."""
    if not player_rows:
        return {"punts": None, "punt_yards": None, "punt_average": None}
    punts = punt_yards = 0
    for row in player_rows:
        if row["team_id"] == team_id and row["stat_group"] == "Punting":
            stats = json.loads(row["stats_json"])
            punts += stats.get("total") or 0
            punt_yards += stats.get("yards") or 0
    return {
        "punts": punts,
        "punt_yards": punt_yards,
        # half-up to match Sports IO's own average (201/4 -> 50.3, where
        # round() would give 50.2)
        "punt_average": (
            float(
                (Decimal(punt_yards) / punts).quantize(
                    Decimal("0.1"), rounding=ROUND_HALF_UP
                )
            )
            if punts
            else None
        ),
    }


def _box_score(
    game: dict[str, Any],
    stats_by_team: dict[int, dict[str, Any]],
    player_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """None until game_team_stats has a row for both teams (first live
    stats capture). Every game_team_stats column, see db/schema.sql for
    what each means (offense vs. defense naming in particular), plus
    punting from the player box score."""
    home = stats_by_team.get(game["home_team_id"])
    away = stats_by_team.get(game["away_team_id"])
    if home is None or away is None:
        return None
    return {
        side: {
            **{k: v for k, v in row.items() if k not in _TEAM_STATS_KEY_COLUMNS},
            **_punting(row["team_id"], player_rows),
        }
        for side, row in (("home", home), ("away", away))
    }


def _players(game: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """None until player stats exist. Each side a {group: [player lines]}
    map (group keys lowercased, e.g. "kick_returns"), players ranked by
    their group's main stat."""
    if not rows:
        return None
    sides: dict[str, defaultdict[str, list[dict[str, Any]]]] = {
        "home": defaultdict(list),
        "away": defaultdict(list),
    }
    for row in rows:
        side = "home" if row["team_id"] == game["home_team_id"] else "away"
        sides[side][row["stat_group"]].append(player_line(row))
    return {
        side: {
            group.lower(): sorted(
                lines,
                key=lambda line, g=group: _sort_value(g, line),
                reverse=True,
            )
            for group, lines in groups.items()
        }
        for side, groups in sides.items()
    }


def write_game_details(game_ids: Iterable[int]) -> None:
    """Write game:{season}:{game_id}:details for each game - everything
    about one game that the scoreboard itself doesn't need: team box
    score, player box score and ESPN's full win probability curve. Each
    part is None until its data exists; a game with none of them yet is
    skipped rather than written empty."""
    game_ids = sorted(set(game_ids))
    if not game_ids:
        return

    d1 = get_d1()
    placeholders = ", ".join("?" * len(game_ids))

    def rows(sql: str) -> list[dict[str, Any]]:
        return d1.query(sql.format(placeholders), game_ids).results

    team_stats: defaultdict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows(_TEAM_STATS_SQL):
        team_stats[row["game_id"]][row["team_id"]] = row
    player_rows: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows(_PLAYER_STATS_SQL):
        player_rows[row["game_id"]].append(row)
    win_probability = {
        row["game_id"]: json.loads(row["points_json"])
        for row in rows(_WIN_PROBABILITY_SQL)
    }

    kv = get_kv()
    written = 0
    for game in rows(_GAMES_SQL):
        game_id = game["game_id"]
        details = {
            "box_score": _box_score(game, team_stats[game_id], player_rows[game_id]),
            "players": _players(game, player_rows[game_id]),
            # ESPN, chronological, one point per play plus a pre-kickoff
            # point (period 0) - only once the game is FINAL
            "win_probability": win_probability.get(game_id),
        }
        if all(value is None for value in details.values()):
            continue
        kv.write(
            f"game:{SEASON}:{game_id}:details",
            {"game_id": game_id, "updated_at": utc_iso(), **details},
        )
        written += 1
    logger.info("Wrote game details KV for %d game(s)", written)


def write_week_game_details(week_number: int) -> None:
    """every game in a week - backfill/full refresh"""
    d1 = get_d1()
    write_game_details(
        row["game_id"]
        for row in d1.query(_WEEK_GAME_IDS_SQL, [SEASON, week_number]).results
    )


def write_current_week_game_details() -> None:
    """Resolve weeks.is_current and write every game's details key."""
    for_current_week(write_week_game_details, "game details")
