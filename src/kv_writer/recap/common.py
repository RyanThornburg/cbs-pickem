"""Shared by every recap item module: the season data they all read
(Season), the text/stat helpers, and make_item() - the one shape every
recap item has."""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.kv_writer.shared import game_team_dicts

EASTERN = ZoneInfo("America/New_York")
SHORT_MAX = 80  # `short` headline length, so a one-line strip keeps its height


@dataclass
class Season:
    week: int
    games: list[dict[str, Any]]  # every game through `week`
    picks_by_game: dict[int, list[dict[str, Any]]]
    performance: list[dict[str, Any]]

    def week_games(self, week_number: int) -> list[dict[str, Any]]:
        return [g for g in self.games if g["week_number"] == week_number]


def z_score(successes: int, n: int) -> float:
    """Binomial z-score against a coin flip (p = 0.5)."""
    if n == 0:
        return 0.0
    return (successes - n / 2) / math.sqrt(n / 4)


def pct(successes: int, n: int) -> float | None:
    return round(successes / n, 3) if n else None


def pct_text(successes: int, n: int) -> str:
    return f"{round(100 * successes / n)}%"


def record_text(wins: int, losses: int, pushes: int = 0) -> str:
    return f"{wins}-{losses}-{pushes}" if pushes else f"{wins}-{losses}"


def ordinal(n: int) -> str:
    suffix = (
        "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    )
    return f"{n}{suffix}"


def names_text(names: list[str], limit: int = 3) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def person(row: dict[str, Any]) -> dict[str, Any]:
    """How every recap item lists a person - the UI matches on user_id"""
    return {"user_id": row["user_id"], "name": row["name"]}


def people(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((person(r) for r in rows), key=lambda p: p["name"].lower())


def fit(text: str) -> str:
    """Trim a short headline to SHORT_MAX at a word boundary - only names
    can push one over, the fixed wording never does"""
    if len(text) <= SHORT_MAX:
        return text
    cut = text[: SHORT_MAX - 1].rsplit(" ", 1)[0].rstrip(",:")
    return cut + "…"


def make_item(
    kind: str,
    category: str,
    scope: str,
    score: float,
    headline: str,
    short: str,
    data: dict[str, Any],
    sample_size: int | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    return {
        "id": kind if key is None else f"{kind}:{key}",
        "kind": kind,
        "category": category,
        "scope": scope,
        "score": round(score, 2),
        "headline": headline,
        "short": fit(short),
        "sample_size": sample_size,
        "data": data,
    }


def side_team(game: dict[str, Any], side: str) -> dict[str, Any]:
    home, away = game_team_dicts(game)
    return home if side == "home" else away


def kickoff_slot(game: dict[str, Any]) -> str:
    kickoff = datetime.fromisoformat(game["game_time"]).astimezone(EASTERN)
    weekday = kickoff.weekday()  # Monday = 0
    if weekday == 6:
        if kickoff.hour < 12:
            return "sunday_morning"
        if kickoff.hour < 16:
            return "sunday_early"
        if kickoff.hour < 19:
            return "sunday_late"
        return "sunday_night"
    # a season opener can land on a Wednesday (2026's did), Christmas on a Tuesday
    return ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday")[weekday]


def is_division_game(game: dict[str, Any]) -> bool:
    return (
        game["home_division"] is not None
        and game["home_division"] == game["away_division"]
    )


def game_line(game: dict[str, Any]) -> dict[str, Any]:
    home, away = game_team_dicts(game)
    return {
        "game_id": game["game_id"],
        "week": game["week_number"],
        "home_team": home,
        "away_team": away,
        "home_score": game["home_score"],
        "away_score": game["away_score"],
        "cbs_spread": game["cbs_spread"],
    }
