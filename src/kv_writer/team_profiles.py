"""team:{season}:{team_id} - see src/CLAUDE.md's KV writer section."""

import logging
from collections import defaultdict
from typing import Any

from config.config import SEASON
from db.clients import get_d1, get_kv
from db.d1_client import D1Client
from src.game_rules import ats_side
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

_TEAMS_SQL = """
SELECT team_id, abbreviation AS abbr, nick_name AS name, conference, division
FROM teams
"""

_SEASON_GAMES_SQL = """
SELECT g.game_id, w.week_number, g.game_time, g.status, g.home_score, g.away_score,
    g.cbs_spread, g.home_team_id AS home_id, g.away_team_id AS away_id
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ?
ORDER BY g.game_time
"""

_SEASON_PICKS_SQL = """
SELECT up.game_id, up.user_id, u.name, up.picked_team_id, up.is_correct
FROM user_picks up
JOIN users u ON u.user_id = up.user_id
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ?
"""

# everything a team key reads from one game - a change in any of it means
# both teams' keys need rewriting. Scores only once FINAL, so a live game
# doesn't rewrite its teams every tick.
_GAME_FINGERPRINT_SQL = """
SELECT g.game_id, g.home_team_id AS home_id, g.away_team_id AS away_id,
    g.status || '|' || COALESCE(g.cbs_spread, '') || '|'
    || CASE WHEN g.status = 'FINAL'
        THEN COALESCE(g.home_score, '') || '-' || COALESCE(g.away_score, '')
        ELSE '' END
    || '|' || COUNT(up.pick_id) || '|' || COUNT(up.is_correct)
    || '|' || COALESCE(SUM(up.is_correct), 0) AS fingerprint
FROM games g
JOIN weeks w ON w.week_id = g.week_id
LEFT JOIN user_picks up ON up.game_id = g.game_id
WHERE w.season_id = ?
GROUP BY g.game_id
"""


def game_fingerprints(d1: D1Client) -> dict[str, tuple[str, int, int]]:
    """{game_id: (fingerprint, home_id, away_id)} for this season - game_id
    as a string, since orchestration keeps the fingerprints as JSON"""
    return {
        str(row["game_id"]): (row["fingerprint"], row["home_id"], row["away_id"])
        for row in d1.query(_GAME_FINGERPRINT_SQL, [SEASON]).results
    }


def _team_line(game: dict[str, Any], side: str) -> float | None:
    """this team's line - cbs_spread is the home team's"""
    spread = game["cbs_spread"]
    if spread is None:
        return None
    return spread if side == "home" else -spread


def _wlt(results: list[str]) -> dict[str, int]:
    return {
        "wins": results.count("W"),
        "losses": results.count("L"),
        "ties": results.count("T"),
    }


def _ats(results: list[bool]) -> dict[str, Any]:
    covers = sum(results)
    return {
        "covers": covers,
        "losses": len(results) - covers,
        "cover_pct": round(covers / len(results), 3) if results else None,
    }


def _record(picks: list[dict[str, Any]]) -> dict[str, Any]:
    """same shape as a user profile's records - picks counts every pick,
    wins/losses only graded ones"""
    graded = [bool(p["is_correct"]) for p in picks if p["is_correct"] is not None]
    wins = sum(graded)
    return {
        "picks": len(picks),
        "wins": wins,
        "losses": len(graded) - wins,
        "win_pct": round(wins / len(graded), 3) if graded else None,
    }


def _by_user(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """every user in picks with their record, most picks first"""
    picks_by_user: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in picks:
        picks_by_user[pick["user_id"]].append(pick)
    users = [
        {"user_id": user_id, "name": user_picks[0]["name"], **_record(user_picks)}
        for user_id, user_picks in picks_by_user.items()
    ]
    users.sort(key=lambda u: (-u["picks"], -u["wins"], u["name"]))
    return users


def _team_profile(
    team: dict[str, Any],
    games: list[tuple[dict[str, Any], str]],
    picks_by_game: dict[int, list[dict[str, Any]]],
    teams_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """games: (game, side) for every game on this team's schedule, in
    kickoff order"""
    results: list[str] = []
    ats_by_split: defaultdict[str, list[bool]] = defaultdict(list)
    picked: list[dict[str, Any]] = []
    against: list[dict[str, Any]] = []
    game_log: list[dict[str, Any]] = []

    for game, side in games:
        opponent_id = game["away_id"] if side == "home" else game["home_id"]
        line = _team_line(game, side)
        final = game["status"] == "FINAL" and game["home_score"] is not None
        result = None
        own = other = None
        if final:
            own, other = (
                (game["home_score"], game["away_score"])
                if side == "home"
                else (game["away_score"], game["home_score"])
            )
            result = "W" if own > other else "L" if own < other else "T"
            results.append(result)

        covered_side = ats_side(game)
        covered = None if covered_side in (None, "push") else covered_side == side
        if covered is not None:
            ats_by_split["overall"].append(covered)
            ats_by_split[side].append(covered)
            if line is not None and line != 0:
                ats_by_split["favorite" if line < 0 else "underdog"].append(covered)

        game_picks = picks_by_game.get(game["game_id"], [])
        game_picked = [p for p in game_picks if p["picked_team_id"] == team["team_id"]]
        game_against = [p for p in game_picks if p["picked_team_id"] == opponent_id]
        picked.extend(game_picked)
        against.extend(game_against)

        opponent = teams_by_id.get(opponent_id, {})
        game_log.append(
            {
                "game_id": game["game_id"],
                "week_number": game["week_number"],
                "game_time": game["game_time"],
                "side": side,
                "opponent": {
                    "id": opponent_id,
                    "abbr": opponent.get("abbr"),
                    "name": opponent.get("name"),
                },
                "line": line,
                "status": game["status"],
                "score": own,
                "opponent_score": other,
                "result": result,
                "covered": covered,
                "pool_picked": len(game_picked),
                "pool_against": len(game_against),
            }
        )

    return {
        "season": SEASON,
        "team": {
            "id": team["team_id"],
            "abbr": team["abbr"],
            "name": team["name"],
            "conference": team["conference"],
            "division": team["division"],
        },
        "record": _wlt(results),
        "ats": {
            split: _ats(ats_by_split[split])
            for split in ("overall", "home", "away", "favorite", "underdog")
        },
        "pool": {
            "picked": _record(picked),
            "against": _record(against),
            "believers": _by_user(picked),
            "faders": _by_user(against),
        },
        "games": game_log,
    }


def compute_team_profiles(
    d1: D1Client, team_ids: set[int] | None = None
) -> dict[int, dict[str, Any]]:
    """One profile per team (only `team_ids` if given)"""
    teams_by_id = {row["team_id"]: row for row in d1.query(_TEAMS_SQL).results}
    games_by_team: defaultdict[int, list[tuple[dict[str, Any], str]]] = defaultdict(
        list
    )
    for game in d1.query(_SEASON_GAMES_SQL, [SEASON]).results:
        games_by_team[game["home_id"]].append((game, "home"))
        games_by_team[game["away_id"]].append((game, "away"))
    picks_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in d1.query(_SEASON_PICKS_SQL, [SEASON]).results:
        picks_by_game[pick["game_id"]].append(pick)

    wanted = teams_by_id.keys() if team_ids is None else team_ids & teams_by_id.keys()
    return {
        team_id: _team_profile(
            teams_by_id[team_id],
            games_by_team[team_id],
            picks_by_game,
            teams_by_id,
        )
        for team_id in wanted
    }


def write_team_profiles(team_ids: set[int] | None = None) -> None:
    """Write team:{season}:{team_id} for `team_ids`, or every team"""
    profiles = compute_team_profiles(get_d1(), team_ids)
    if not profiles:
        logger.warning("No teams to write team profile keys for")
        return
    kv = get_kv()
    now = utc_iso()
    for team_id, profile in profiles.items():
        kv.write(f"team:{SEASON}:{team_id}", {**profile, "updated_at": now})
    logger.info("Wrote %d team:%s:* profile keys to KV", len(profiles), SEASON)
