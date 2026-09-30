"""Chaos index - how upside-down a week was, ranked against the season's other weeks."""

from typing import Any

from src.game_rules import (
    ats_side,
    favorite_side,
    winner_side,
)
from src.kv_writer.recap.common import (
    Season,
    make_item,
)

_BIG_FAVORITE_POINTS = 7.0  # a favorite of this much or more losing outright is chaos
_CHAOS_MIN_GAMES = 8  # final games before a week in progress gets a chaos index
# final games before a week in progress can be called "on pace" for the
# season's most/least chaotic - replayed weeks 1-3 were within about half a
# point of their final index by 13-14 games, but off by up to 2 at 9-10
_CHAOS_PACE_MIN_GAMES = 13


def _week_chaos(
    games: list[dict[str, Any]], pool_row: dict[str, Any] | None
) -> dict[str, Any] | None:
    """0-10 chaos score for one week's FINAL games, the average of up to
    four 0-1 parts: underdog cover rate, outright upset rate (doubled,
    capped at 1 - upsets are rarer than covers), pool miss rate, and the
    share of big favorites (_BIG_FAVORITE_POINTS+) that lost outright. A
    part with nothing to measure (no big favorites yet, no graded picks)
    is left out rather than counted as 0, which would read as calm. Weights
    are a judgment call. None until _CHAOS_MIN_GAMES are final - `partial`
    is True while some of the week's games aren't."""
    final_games = [g for g in games if g["status"] == "FINAL"]
    if len(final_games) < min(_CHAOS_MIN_GAMES, len(games)) or not final_games:
        return None
    dog_covers = decided = upsets = su_decided = big_favs = big_fav_losses = 0
    for game in final_games:
        favorite = favorite_side(game)
        if favorite is None:
            continue
        side = ats_side(game)
        if side in ("home", "away"):
            decided += 1
            dog_covers += side != favorite
        winner = winner_side(game)
        if winner is not None:
            su_decided += 1
            upsets += winner != favorite
            if abs(game["cbs_spread"]) >= _BIG_FAVORITE_POINTS:
                big_favs += 1
                big_fav_losses += winner != favorite
    if not decided:
        return None
    pool_accuracy = pool_row["accuracy"] if pool_row else None
    parts = [dog_covers / decided]
    if su_decided:
        parts.append(min(1.0, 2 * upsets / su_decided))
    if pool_accuracy is not None:
        parts.append(1 - pool_accuracy)
    if big_favs:
        parts.append(big_fav_losses / big_favs)
    return {
        "week": games[0]["week_number"],
        "partial": len(final_games) < len(games),
        "games_final": len(final_games),
        "games_total": len(games),
        "index": round(10 * sum(parts) / len(parts), 1),
        "underdog_covers": dog_covers,
        "ats_decided": decided,
        "outright_upsets": upsets,
        "big_favorite_losses": big_fav_losses,
        "big_favorites": big_favs,
        "pool_accuracy": pool_accuracy,
    }


def chaos_series(
    season: Season, pool_series: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    pool_by_week = {row["week"]: row for row in pool_series}
    series = []
    for week_number in sorted({g["week_number"] for g in season.games}):
        chaos = _week_chaos(
            season.week_games(week_number), pool_by_week.get(week_number)
        )
        if chaos:
            series.append(chaos)
    return series


def chaos_items(
    season: Season, chaos_series: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """A complete week is ranked against the season's other complete weeks.
    A week in progress gets its index "so far", and only an "on pace"
    claim once _CHAOS_PACE_MIN_GAMES are final - before that, early swings
    are too big to call (see the constant)."""
    current = next((c for c in chaos_series if c["week"] == season.week), None)
    if current is None:
        return []
    so_far = (
        f" so far ({current['games_final']} of {current['games_total']} games)"
        if current["partial"]
        else ""
    )
    headline = (
        f"Chaos index{so_far}: {current['index']}. Underdogs covered"
        f" {current['underdog_covers']} of {current['ats_decided']} and"
        f" {current['outright_upsets']} won outright."
    )
    short = f"Chaos index {current['index']}" + (
        " so far" if current["partial"] else ""
    )
    score = 1.3 if current["partial"] else 1.5
    rank = None
    others = [c for c in chaos_series if c["week"] != season.week and not c["partial"]]
    can_rank = not current["partial"] or current["games_final"] >= _CHAOS_PACE_MIN_GAMES
    if others and can_rank:
        rank = 1 + sum(1 for c in others if c["index"] > current["index"])
        prefix = "On pace for the" if current["partial"] else "The"
        pace = ", on pace for" if current["partial"] else ","
        if rank == 1:
            headline += f" {prefix} most chaotic week of the season."
            short += f"{pace} most chaotic of the season"
            score = 2.3 if current["partial"] else 2.8
        elif rank == len(others) + 1:
            headline += f" {prefix} chalkiest week of the season."
            short += f"{pace} chalkiest of the season"
            score = 2.0 if current["partial"] else 2.3
    return [
        make_item(
            "chaos_index",
            "chaos",
            "week",
            score,
            headline,
            short,
            {**current, "season_rank": rank, "weeks_ranked": len(others) + 1},
            sample_size=current["ats_decided"],
        )
    ]
