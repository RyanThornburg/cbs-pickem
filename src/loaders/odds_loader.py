"""Load odds data

Usage: uv run python -m src.loaders.odds_loader [local|prod]
"""

import logging
from datetime import datetime, timedelta

from api.the_odds_api_client import get_odds
from api.the_odds_api_models import Event
from config.config import SEASON, run_cli
from db.clients import get_d1
from db.d1_client import Statement
from src.loaders.loader_helper import id_map, mapping_gap_statement, sql_batch_call
from src.timestamps import parse_utc_iso, utc_iso

logger = logging.getLogger(__name__)

# not inserting games from odds load.
_UPDATE_GAME_ODDS_API_EVENT_ID_SQL = """
UPDATE games SET odds_api_event_id = ? WHERE game_id = ?
"""

_INSERT_ODDS_SNAPSHOT_SQL = """
INSERT INTO odds_snapshots (
    game_id, source, bookmaker, market, captured_at,
    home_point, home_price, away_point, away_price)
SELECT ?1, 'the_odds_api', ?2, ?3, ?4, ?5, ?6, ?7, ?8
WHERE NOT EXISTS (
    SELECT 1 FROM odds_snapshots
    WHERE game_id = ?1 AND source = 'the_odds_api' AND bookmaker = ?2
        AND market = ?3 AND captured_at = ?4
)
"""

# The Odds API's kickoff can differ from Sports IO's (CHI @ SEA, week 8 2026:
# 01:20Z vs 01:15Z), so first sighting takes the same matchup's nearest game
# within this window. The same home/away pair never meets twice in a day.
_MATCH_WINDOW = timedelta(hours=24)

_SEASON_GAMES_SQL = """
SELECT g.game_id, g.home_team_id, g.away_team_id, g.game_time
FROM games g JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND g.game_time IS NOT NULL
"""

_MARKET_MAP = {
    "spreads": "spread",
    "totals": "total",
    "h2h": "moneyline",
}


def _resolve_game_id(
    event: Event,
    odds_event_ids: dict[str, int],
    team_ids: dict[str, int],
    games_by_matchup: dict[tuple[int, int], list[tuple[datetime, int]]],
    last_game_time: datetime | None,
) -> tuple[int | None, list[Statement]]:
    """(game_id, mapping gaps) - by odds_api_event_id if it's linked
    already, else by home id, away id and the nearest game time within
    _MATCH_WINDOW"""
    game_id = odds_event_ids.get(event.id)
    if game_id is not None:
        return game_id, []

    home_team_id = team_ids.get(event.home_team)
    away_team_id = team_ids.get(event.away_team)
    if home_team_id is None or away_team_id is None:
        logger.warning(
            "Skipping event %s - no teams row for %r/%r",
            event.id,
            event.home_team,
            event.away_team,
        )
        gaps = [
            mapping_gap_statement("the_odds_api", "team", name, "_resolve_game_id")
            for name, team_id in (
                (event.home_team, home_team_id),
                (event.away_team, away_team_id),
            )
            if team_id is None
        ]
        return None, gaps

    commence = parse_utc_iso(event.commence_time)
    candidates = [
        (abs(game_time - commence), game_id)
        for game_time, game_id in games_by_matchup.get((home_team_id, away_team_id), [])
        if abs(game_time - commence) <= _MATCH_WINDOW
    ]
    if candidates:
        return min(candidates)[1], []

    logger.warning(
        "Skipping event %s - no games row for %s @ %s at %s yet",
        event.id,
        event.away_team,
        event.home_team,
        event.commence_time,
    )
    # playoff games are never loaded (sports_io_loader skips them), so only
    # a miss within the regular season is a real gap
    if last_game_time is None or commence > last_game_time + _MATCH_WINDOW:
        return None, []
    raw = f"{event.away_team} @ {event.home_team} {event.commence_time}"
    return None, [
        mapping_gap_statement("the_odds_api", "game", raw, "_resolve_game_id")
    ]


def _snapshot_statements_for_event(event: Event, game_id: int) -> list[Statement]:
    statements: list[Statement] = []
    for bookmaker in event.bookmakers:
        for market in bookmaker.markets:
            common_market = _MARKET_MAP.get(market.key)
            if common_market is None:
                continue

            by_name = {outcome.name: outcome for outcome in market.outcomes}
            if market.key == "totals":
                home_outcome = by_name.get("Over")
                away_outcome = by_name.get("Under")
            else:
                home_outcome = by_name.get(event.home_team)
                away_outcome = by_name.get(event.away_team)

            if home_outcome is None or away_outcome is None:
                logger.warning(
                    "Skipping %s/%s market for event %s - missing outcome",
                    bookmaker.key,
                    market.key,
                    event.id,
                )
                continue

            statements.append(
                (
                    _INSERT_ODDS_SNAPSHOT_SQL,
                    [
                        game_id,
                        bookmaker.key,
                        common_market,
                        market.last_update,
                        home_outcome.point,
                        int(home_outcome.price),
                        away_outcome.point,
                        int(away_outcome.price),
                    ],
                )
            )
    return statements


def load_the_odds_api_odds() -> None:
    """load odds from the odds api"""
    client = get_d1()
    events: list[Event] = get_odds()

    odds_event_ids = id_map(client, "games", "odds_api_event_id", "game_id")
    team_ids = id_map(client, "teams", "name", "team_id")
    games_by_matchup: dict[tuple[int, int], list[tuple[datetime, int]]] = {}
    for row in client.query(_SEASON_GAMES_SQL, [SEASON]).results:
        games_by_matchup.setdefault(
            (row["home_team_id"], row["away_team_id"]), []
        ).append((parse_utc_iso(row["game_time"]), row["game_id"]))
    last_game_time = max(
        (t for games in games_by_matchup.values() for t, _ in games), default=None
    )

    backfill_statements: list[Statement] = []
    gap_statements: list[Statement] = []
    snapshot_statements: list[Statement] = []

    now = utc_iso()

    for event in events:
        if event.commence_time <= now:
            # skip games that start so odds_snapshots only ever holds pre-kickoff lines.
            continue

        game_id, gaps = _resolve_game_id(
            event, odds_event_ids, team_ids, games_by_matchup, last_game_time
        )
        gap_statements.extend(gaps)
        if game_id is None:
            continue
        if event.id not in odds_event_ids:
            # first sighting - link it so later captures join on the id
            backfill_statements.append(
                (_UPDATE_GAME_ODDS_API_EVENT_ID_SQL, [event.id, game_id])
            )

        snapshot_statements.extend(_snapshot_statements_for_event(event, game_id))

    if backfill_statements or gap_statements:
        sql_batch_call(backfill_statements + gap_statements, client)
    logger.info("Linked %d games to odds_api_event_id", len(backfill_statements))

    if not snapshot_statements:
        logger.warning("No odds from The Odds API to load")
        return

    sql_batch_call(snapshot_statements, client)
    logger.info(
        "Wrote %d odds snapshots (captures already stored are skipped)",
        len(snapshot_statements),
    )


def main() -> None:
    """load all odds"""
    load_the_odds_api_odds()
    # TODO: add odds from sports io


if __name__ == "__main__":
    run_cli(main)
