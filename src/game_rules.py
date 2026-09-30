"""Pick'em rules for one game - who's favored, who won, who covered, which
side a pick was on - and the pool's tie-aware ranking. Imports nothing
from the project, so both kv_writer and src/user_stats.py (which kv_writer
imports) can use it without a cycle.

`game` is a row with status, home_score/away_score, cbs_spread (the home
team's line, negative = home favored) and home_id/away_id."""

from typing import Any


def favorite_side(game: dict[str, Any]) -> str | None:
    """home/away - None for a pick'em or missing spread."""
    spread = game["cbs_spread"]
    if spread is None or spread == 0:
        return None
    return "home" if spread < 0 else "away"


def winner_side(game: dict[str, Any]) -> str | None:
    """Straight-up winner - None until FINAL, or for an actual tie."""
    if (
        game["status"] != "FINAL"
        or game["home_score"] is None
        or game["away_score"] is None
        or game["home_score"] == game["away_score"]
    ):
        return None
    return "home" if game["home_score"] > game["away_score"] else "away"


def ats_side(game: dict[str, Any]) -> str | None:
    """Which side covered game['cbs_spread'] ("home"/"away"/"push") - None
    if the game isn't FINAL yet or is missing a spread/score. Home covers
    when its actual margin beats its line."""
    if game["status"] != "FINAL":
        return None
    if (
        game["cbs_spread"] is None
        or game["home_score"] is None
        or game["away_score"] is None
    ):
        return None
    adjusted = game["home_score"] - game["away_score"] + game["cbs_spread"]
    if adjusted > 0:
        return "home"
    if adjusted < 0:
        return "away"
    return "push"


def pick_side(game: dict[str, Any], picked_team_id: int) -> str | None:
    if picked_team_id == game["home_id"]:
        return "home"
    if picked_team_id == game["away_id"]:
        return "away"
    return None


def other_side(side: str) -> str:
    return "away" if side == "home" else "home"


def standard_rank(score_by_user: dict[int, int]) -> dict[int, int]:
    """highest first, ties share a place and the next place skips"""
    ranked = sorted(score_by_user.items(), key=lambda item: -item[1])
    rank_by_user: dict[int, int] = {}
    prev_score: int | None = None
    prev_rank = 0
    for i, (user_id, score) in enumerate(ranked, start=1):
        if score != prev_score:
            prev_rank = i
            prev_score = score
        rank_by_user[user_id] = prev_rank
    return rank_by_user
