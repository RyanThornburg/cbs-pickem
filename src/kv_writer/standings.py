"""season:{season}:standings - NFL standings, see src/CLAUDE.md's KV writer
section."""

import logging
from collections import defaultdict
from typing import Any

from config.config import SEASON
from db.clients import get_d1, get_kv
from src.game_rules import ats_side
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

_TEAMS_SQL = """
SELECT team_id, abbreviation AS abbr, nick_name AS name, conference, division,
    division_rank
FROM teams
WHERE division IS NOT NULL
"""

_FINAL_GAMES_SQL = """
SELECT g.game_id, g.status, g.home_score, g.away_score, g.cbs_spread,
    g.home_team_id AS home_id, g.away_team_id AS away_id
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND g.status = 'FINAL'
    AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
ORDER BY g.game_time
"""


def _conference_abbr(conference: str) -> str:
    """'American Football Conference' -> 'AFC'"""
    return "".join(word[0] for word in conference.split()).upper()


def _win_pct(wins: int, losses: int, ties: int) -> float | None:
    games = wins + losses + ties
    return round((wins + ties / 2) / games, 3) if games else None


def _wlt(results: list[str]) -> dict[str, int]:
    return {
        "wins": results.count("W"),
        "losses": results.count("L"),
        "ties": results.count("T"),
    }


def _streak(results: list[str]) -> str | None:
    """'W3' - the run of the same result at the end of the season so far"""
    if not results:
        return None
    count = 0
    for result in reversed(results):
        if result != results[-1]:
            break
        count += 1
    return f"{results[-1]}{count}"


def _team_entry(
    team: dict[str, Any],
    games: list[tuple[dict[str, Any], str]],
    teams_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """games: (game, side) for every FINAL game this team played, in
    kickoff order"""
    results: list[str] = []
    home: list[str] = []
    road: list[str] = []
    division: list[str] = []
    conference: list[str] = []
    points_for = points_against = covers = ats_losses = 0
    for game, side in games:
        own, other = (
            (game["home_score"], game["away_score"])
            if side == "home"
            else (game["away_score"], game["home_score"])
        )
        result = "W" if own > other else "L" if own < other else "T"
        results.append(result)
        (home if side == "home" else road).append(result)
        opponent = teams_by_id.get(
            game["away_id"] if side == "home" else game["home_id"]
        )
        if opponent and opponent["division"] == team["division"]:
            division.append(result)
        if opponent and opponent["conference"] == team["conference"]:
            conference.append(result)
        points_for += own
        points_against += other
        covered = ats_side(game)
        if covered == side:
            covers += 1
        elif covered in ("home", "away"):
            ats_losses += 1

    record = _wlt(results)
    return {
        "team": {"id": team["team_id"], "abbr": team["abbr"], "name": team["name"]},
        **record,
        "win_pct": _win_pct(record["wins"], record["losses"], record["ties"]),
        "points_for": points_for,
        "points_against": points_against,
        "point_diff": points_for - points_against,
        "home": _wlt(home),
        "road": _wlt(road),
        "division_record": _wlt(division),
        "conference_record": _wlt(conference),
        "streak": _streak(results),
        "ats": {
            "covers": covers,
            "losses": ats_losses,
            "cover_pct": round(covers / (covers + ats_losses), 3)
            if covers + ats_losses
            else None,
        },
    }


def compute_standings() -> list[dict[str, Any]]:
    """Records come from our own FINAL games, so they match the scoreboard
    the moment a game ends. Sports IO's division_rank only breaks a tie in
    win_pct (it applies the NFL tiebreakers, which we don't), and can lag
    a game behind until the next load_standings()."""
    d1 = get_d1()
    teams_by_id = {row["team_id"]: row for row in d1.query(_TEAMS_SQL).results}
    games_by_team: defaultdict[int, list[tuple[dict[str, Any], str]]] = defaultdict(
        list
    )
    for game in d1.query(_FINAL_GAMES_SQL, [SEASON]).results:
        games_by_team[game["home_id"]].append((game, "home"))
        games_by_team[game["away_id"]].append((game, "away"))

    entries_by_division: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    conference_by_division: dict[str, str] = {}
    for team_id, team in teams_by_id.items():
        entry = _team_entry(team, games_by_team[team_id], teams_by_id)
        entries_by_division[team["division"]].append(
            {**entry, "_rank": team["division_rank"]}
        )
        conference_by_division[team["division"]] = team["conference"]

    conferences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for division in sorted(entries_by_division):
        entries = sorted(
            entries_by_division[division],
            key=lambda e: (
                -(e["win_pct"] or 0),
                e["_rank"] if e["_rank"] is not None else 99,
                e["team"]["abbr"],
            ),
        )
        teams = [
            {"rank": rank, **{k: v for k, v in e.items() if k != "_rank"}}
            for rank, e in enumerate(entries, start=1)
        ]
        conferences[conference_by_division[division]].append(
            {"name": division, "teams": teams}
        )

    return [
        {"name": name, "abbr": _conference_abbr(name), "divisions": divisions}
        for name, divisions in sorted(conferences.items())
    ]


def write_season_standings() -> None:
    """Write season:{season}:standings"""
    conferences = compute_standings()
    if not conferences:
        logger.warning("No teams with a division - not writing standings key")
        return
    get_kv().write(
        f"season:{SEASON}:standings",
        {"season": SEASON, "updated_at": utc_iso(), "conferences": conferences},
    )
    logger.info("Wrote season:%s:standings to KV", SEASON)
