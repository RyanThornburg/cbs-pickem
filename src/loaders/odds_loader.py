"""Load odds data

Usage: uv run python -m src.loaders.odds_loader [local|prod]
"""

import logging

from api.the_odds_api_client import get_odds
from api.the_odds_api_models import Event
from config.config import run_cli
from db.clients import get_d1
from db.d1_client import Statement
from src.loaders.loader_helper import id_map, mapping_gap_statement, sql_batch_call
from src.timestamps import utc_iso

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

_MARKET_MAP = {
    "spreads": "spread",
    "totals": "total",
    "h2h": "moneyline",
}


def _resolve_game_id(
    event: Event,
    odds_event_ids: dict[str, int],
    team_ids: dict[str, int],
    games_by_matchup: dict[tuple[int, int, str], int],
) -> tuple[int | None, list[Statement]]:
    """(game_id, mapping gaps) - by odds_api_event_id if it's linked
    already, else by home id, away id, game time"""
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

    game_id = games_by_matchup.get((home_team_id, away_team_id, event.commence_time))
    if game_id is None:
        logger.warning(
            "Skipping event %s - no games row for %s @ %s at %s yet",
            event.id,
            event.away_team,
            event.home_team,
            event.commence_time,
        )
    return game_id, []


def _snapshot_statements_for_event(
    event: Event, game_id: int
) -> list[Statement]:
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
    games_by_matchup = {
        (row["home_team_id"], row["away_team_id"], row["game_time"]): row["game_id"]
        for row in client.query(
            "SELECT game_id, home_team_id, away_team_id, game_time FROM games"
        ).results
    }

    backfill_statements: list[Statement] = []
    gap_statements: list[Statement] = []
    snapshot_statements: list[Statement] = []

    now = utc_iso()

    for event in events:
        if event.commence_time <= now:
            # skip games that start so odds_snapshots only ever holds pre-kickoff lines.
            continue

        game_id, gaps = _resolve_game_id(
            event, odds_event_ids, team_ids, games_by_matchup
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
