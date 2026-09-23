"""week:{season}:{weekNN}:odds - see src/CLAUDE.md's KV writer section.

open_close_consensus_by_game() is exported (no leading underscore) because
trends.py's spread/total movers reuse it directly, so both keys can never
disagree on what "the consensus line" was for a given game."""

import logging
from collections import Counter, defaultdict
from typing import Any

from config.config import SEASON, get_d1_config, get_kv_config
from db.d1_client import D1Client
from db.kv_client import KVClient
from src.kv_writer.shared import now_iso, resolve_current_week

logger = logging.getLogger(__name__)

# filtering down sportsbooks to common ones
# not running a gambling site, so just return recognizable ones
_ODDS_BOOKMAKERS = ("draftkings", "fanduel", "betmgm", "betrivers", "bovada")

_STANDARD_JUICE = -110  # the "no edge" American-odds price a line is priced around

_WEEK_CBS_SPREADS_SQL = """
SELECT g.game_id, g.cbs_spread
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
"""

# market is 'spread' or 'total' - home_point holds the Over line for 'total'
# (see odds_snapshots' own column comment in db/schema.sql)
_ODDS_MARKET_SQL = f"""
SELECT os.game_id, os.bookmaker, os.home_point, os.home_price, os.captured_at
FROM odds_snapshots os
JOIN games g ON g.game_id = os.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ? AND os.market = ?
  AND os.bookmaker IN ({",".join("?" * len(_ODDS_BOOKMAKERS))})
ORDER BY os.captured_at ASC
"""

# every market for this week's _ODDS_BOOKMAKERS books - unlike _ODDS_MARKET_SQL
# this isn't for a consensus, just each book's own latest line per market
_LATEST_BOOK_ODDS_SQL = f"""
SELECT os.game_id, os.bookmaker, os.market, os.home_point, os.home_price,
       os.away_point, os.away_price, os.captured_at
FROM odds_snapshots os
JOIN games g ON g.game_id = os.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
  AND os.market IN ('spread', 'total', 'moneyline')
  AND os.bookmaker IN ({",".join("?" * len(_ODDS_BOOKMAKERS))})
ORDER BY os.captured_at ASC
"""


def _consensus_line(rows: list[dict[str, Any]]) -> tuple[float, int]:
    """mode of home_point across books.
    Ties are decided by the odds/juice offered and consensus closest to -110 or better"""
    counts = Counter(row["home_point"] for row in rows)
    max_count = max(counts.values())
    tied = sorted(value for value, count in counts.items() if count == max_count)
    if len(tied) == 1:
        return tied[0], max_count
    best_value = min(
        tied,
        key=lambda value: min(
            abs(row["home_price"] - _STANDARD_JUICE)
            for row in rows
            if row["home_point"] == value
        ),
    )
    return best_value, max_count


def open_close_consensus_by_game(
    d1: D1Client, week_number: int, market: str = "spread"
) -> dict[int, dict[str, Any]]:
    """Per game, this week's opening/closing consensus line (spread or
    total) across _ODDS_BOOKMAKERS - each book's own earliest/latest
    odds_snapshots row stands in for "opening"/"closing" (see
    db/CLAUDE.md's odds_snapshots note on why MIN/MAX over captured_at
    replaces dedicated columns). Shared by write_week_odds() (spread) and
    trends.py's write_week_trends() (spread + total movers) so all three
    use the exact same consensus numbers."""
    opening_by_game_book: dict[tuple[int, str], dict[str, Any]] = {}
    closing_by_game_book: dict[tuple[int, str], dict[str, Any]] = {}
    for row in d1.query(
        _ODDS_MARKET_SQL, [SEASON, week_number, market, *_ODDS_BOOKMAKERS]
    ).results:
        key = (row["game_id"], row["bookmaker"])
        opening_by_game_book.setdefault(key, row)  # first-seen wins (ASC order)
        closing_by_game_book[key] = row  # last-seen wins

    opens_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for (game_id, _bookmaker), row in opening_by_game_book.items():
        opens_by_game[game_id].append(row)

    closes_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for (game_id, _bookmaker), row in closing_by_game_book.items():
        closes_by_game[game_id].append(row)

    consensus_by_game: dict[int, dict[str, Any]] = {}
    for game_id in set(opens_by_game) | set(closes_by_game):
        opens = opens_by_game.get(game_id)
        closes = closes_by_game.get(game_id)
        if not opens or not closes:
            continue
        open_line, open_agreement = _consensus_line(opens)
        close_line, close_agreement = _consensus_line(closes)
        consensus_by_game[game_id] = {
            "book_count": len(closes),
            "open": open_line,
            "open_agreement": open_agreement,
            "open_captured_at": min(row["captured_at"] for row in opens),
            "close": close_line,
            "close_agreement": close_agreement,
            "close_captured_at": max(row["captured_at"] for row in closes),
        }
    return consensus_by_game


def _latest_book_odds_by_game(
    d1: D1Client, week_number: int
) -> dict[int, list[dict[str, Any]]]:
    """Per game, each _ODDS_BOOKMAKERS book's latest spread/total/moneyline
    line. Not a consensus like open_close_consensus_by_game - a single
    book's own line doesn't need an open/close split, only its latest
    snapshot is worth showing per book."""
    latest_by_game_book_market: dict[tuple[int, str, str], dict[str, Any]] = {}
    for row in d1.query(
        _LATEST_BOOK_ODDS_SQL, [SEASON, week_number, *_ODDS_BOOKMAKERS]
    ).results:
        key = (row["game_id"], row["bookmaker"], row["market"])
        latest_by_game_book_market[key] = row  # last-seen wins (ASC order)

    markets_by_game_book: defaultdict[tuple[int, str], dict[str, Any]] = defaultdict(
        dict
    )
    for (game_id, bookmaker, market), row in latest_by_game_book_market.items():
        markets_by_game_book[(game_id, bookmaker)][market] = {
            "home_point": row["home_point"],
            "home_price": row["home_price"],
            "away_point": row["away_point"],
            "away_price": row["away_price"],
            "captured_at": row["captured_at"],
        }

    books_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for (game_id, bookmaker), markets in markets_by_game_book.items():
        books_by_game[game_id].append({"bookmaker": bookmaker, **markets})
    return books_by_game


def write_week_odds(week_number: int) -> None:
    """Write week:{season}:{weekNN}:odds - each game's cbs_spread (what the
    pool is graded against) alongside an opening/closing consensus line and
    each _ODDS_BOOKMAKERS book's own latest spread/total/moneyline line."""
    d1 = D1Client(**get_d1_config())

    games = d1.query(_WEEK_CBS_SPREADS_SQL, [SEASON, week_number]).results
    if not games:
        logger.warning(
            "No games found for season %s week %s - not writing odds key",
            SEASON,
            week_number,
        )
        return

    consensus_by_game = open_close_consensus_by_game(d1, week_number)
    books_by_game = _latest_book_odds_by_game(d1, week_number)

    games_json: list[dict[str, Any]] = [
        {
            "game_id": game["game_id"],
            "cbs_spread": game["cbs_spread"],
            "market_spread": consensus_by_game.get(game["game_id"]),
            "books": books_by_game.get(game["game_id"], []),
        }
        for game in games
    ]

    kv = KVClient(**get_kv_config())
    kv.write(
        f"week:{SEASON}:{week_number:02d}:odds",
        {
            "week": week_number,
            "updated_at": now_iso(),
            "games": games_json,
        },
    )
    logger.info(
        "Wrote week:%s:%02d:odds (%d games) to KV",
        SEASON,
        week_number,
        len(games_json),
    )


def write_current_week_odds() -> None:
    """Resolve weeks.is_current and write that week's odds key."""
    d1 = D1Client(**get_d1_config())
    current_week = resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s - not writing odds key",
            SEASON,
        )
        return

    write_week_odds(current_week)
