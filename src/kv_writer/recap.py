"""week:{season}:{weekNN}:recap - see src/CLAUDE.md's KV writer section.

Short "did you know" items for the web UI's weekly infographic. Each
generator below returns zero or more candidate recap items, and the key holds
all of them ranked by `score`, highest first - the UI rotates through the
top few. The always-on kinds (pool accuracy, spread mattered, chaos index,
...) carry a hand-picked base score nudged up when the week is extreme.
The split kinds (home/road, favorites/underdogs, kickoff slot, division
games) only show up at all once they clear _stands_out(): enough sample
and far enough from a coin flip that it's a real lean, not noise. Early
in a season most splits won't qualify, which is the point.

Everything is computed "as of" the key's week (season data through that
week only), so rewriting an old week's key gives the same answer it would
have had at the time, apart from late grading."""

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any
from zoneinfo import ZoneInfo

from config.config import SEASON
from db.clients import get_d1, get_kv
from db.d1_client import D1Client
from src.kv_writer.shared import (
    ats_side,
    for_current_week,
    game_team_dicts,
    standard_rank,
)
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")

# Bump whenever a kind is renamed or removed, or a field changes shape, so
# the UI can tell a stale card mapping from a real change - see the
# changelog in the UI reference Artifact linked from CLAUDE.local.md.
# 1: first version. 2: consensus_* -> crowd_record/popular_picks, people as
# {user_id, name}, short, movers/cover_streaks lists, league category.

SCHEMA_VERSION = 3
_SHORT_MAX = 80  # `short` headline length, so a one-line strip keeps its height
# a week keeps getting rewritten this long after its last kickoff, so its
# final state (last game FINAL, CBS's last grades) lands even after
# weeks.is_current has moved on
_RECENT_WEEK_HOURS = 12

# hand-picked judgment calls, same spirit as trends.py's thresholds
_STANDOUT_MIN_Z = 1.5  # distance from a coin flip, in standard deviations
# z grows with sample size, so without a cap a big season-long split would
# outrank every weekly recap item by midseason
_STANDOUT_MAX_SCORE = 3.0
_STANDOUT_MIN_POOL_PICKS = 20  # floor for a pool-wide pick split
_STANDOUT_MIN_GAMES = 10  # floor for a league-wide game split
_STANDOUT_MIN_TEAM_GAMES = 4  # floor for one team's split
# share of that week's pool on the crowd's side for it to count as a popular
# pick, rounded up (10 of 33) - a share rather than a flat count so it keeps
# its meaning if the pool size changes. Weeks 1-3 of 2026 had a clear gap
# right there: 4-5 teams a week at 10+, then 9 and below.
_POPULAR_POOL_SHARE = 0.3
_POPULAR_MIN_GAMES = 3  # season record needs a few games before it means anything
_BIG_FAVORITE_POINTS = 7.0  # a favorite of this much or more losing outright is chaos
_CHAOS_MIN_GAMES = 8  # final games before a week in progress gets a chaos index
# final games before a week in progress can be called "on pace" for the
# season's most/least chaotic - replayed weeks 1-3 were within about half a
# point of their final index by 13-14 games, but off by up to 2 at 9-10
_CHAOS_PACE_MIN_GAMES = 13
_MIN_COVER_STREAK = 3
_MIN_RANK_MOVE = 3  # spots climbed/dropped before a mover is worth a recap item
_PICKS_PER_WEEK = 5
# Wednesday for a night season opener
_PRIMETIME_SLOTS = frozenset({"wednesday", "thursday", "sunday_night", "monday"})

_SLOT_LABELS = {
    "tuesday": "Tuesday",
    "wednesday": "Wednesday",
    "thursday": "Thursday night",
    "friday": "Friday",
    "saturday": "Saturday",
    "sunday_morning": "Sunday morning (international)",
    "sunday_early": "Sunday early",
    "sunday_late": "Sunday late afternoon",
    "sunday_night": "Sunday night",
    "monday": "Monday night",
}

_SEASON_GAMES_SQL = """
SELECT g.game_id, w.week_number, g.status, g.home_score, g.away_score, g.cbs_spread,
    g.game_time, g.neutral_site,
    ht.team_id AS home_id, ht.abbreviation AS home_abbr, ht.nick_name AS home_name,
    ht.division AS home_division,
    at.team_id AS away_id, at.abbreviation AS away_abbr, at.nick_name AS away_name,
    at.division AS away_division
FROM games g
JOIN teams ht ON ht.team_id = g.home_team_id
JOIN teams at ON at.team_id = g.away_team_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number <= ?
ORDER BY g.game_time
"""

_SEASON_PICKS_SQL = """
SELECT up.game_id, up.user_id, u.name, up.picked_team_id, up.is_correct, w.week_number
FROM user_picks up
JOIN users u ON u.user_id = up.user_id
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number <= ? AND u.is_active = TRUE
"""

# weeks.start_time/end_time are the first/last kickoff, ISO8601 UTC text,
# so plain string comparison works
_RECENT_WEEKS_SQL = """
SELECT week_number FROM weeks
WHERE season_id = ?
    AND (is_current = 1 OR (start_time <= ? AND (is_complete = 0 OR end_time >= ?)))
ORDER BY week_number
"""

# same source and filter as leaderboard.py, so mover ranks match the leaderboard's
_SEASON_PERFORMANCE_SQL = """
SELECT wp.user_id, u.name, w.week_number, wp.picks_correct
FROM weekly_performance wp
JOIN weeks w ON w.week_id = wp.week_id
JOIN users u ON u.user_id = wp.user_id
WHERE w.season_id = ? AND w.week_number <= ? AND u.is_active = TRUE
"""


@dataclass
class _Season:
    week: int
    games: list[dict[str, Any]]  # every game through `week`
    picks_by_game: dict[int, list[dict[str, Any]]]
    performance: list[dict[str, Any]]

    def week_games(self, week_number: int) -> list[dict[str, Any]]:
        return [g for g in self.games if g["week_number"] == week_number]


# -- helpers ------------------------------------------------------------------


def _z(successes: int, n: int) -> float:
    """Binomial z-score against a coin flip (p = 0.5)."""
    if n == 0:
        return 0.0
    return (successes - n / 2) / math.sqrt(n / 4)


def _stands_out(successes: int, n: int, min_n: int) -> bool:
    return n >= min_n and abs(_z(successes, n)) >= _STANDOUT_MIN_Z


def _pct(successes: int, n: int) -> float | None:
    return round(successes / n, 3) if n else None


def _pct_text(successes: int, n: int) -> str:
    return f"{round(100 * successes / n)}%"


def _record_text(wins: int, losses: int, pushes: int = 0) -> str:
    return f"{wins}-{losses}-{pushes}" if pushes else f"{wins}-{losses}"


def _ordinal(n: int) -> str:
    suffix = (
        "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    )
    return f"{n}{suffix}"


def _names_text(names: list[str], limit: int = 3) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def _person(row: dict[str, Any]) -> dict[str, Any]:
    """How every recap item lists a person - the UI matches on user_id"""
    return {"user_id": row["user_id"], "name": row["name"]}


def _people(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((_person(r) for r in rows), key=lambda p: p["name"].lower())


def _fit(text: str) -> str:
    """Trim a short headline to _SHORT_MAX at a word boundary - only names
    can push one over, the fixed wording never does"""
    if len(text) <= _SHORT_MAX:
        return text
    cut = text[: _SHORT_MAX - 1].rsplit(" ", 1)[0].rstrip(",:")
    return cut + "…"


def _item(
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
        "short": _fit(short),
        "sample_size": sample_size,
        "data": data,
    }


def _team(game: dict[str, Any], side: str) -> dict[str, Any]:
    home, away = game_team_dicts(game)
    return home if side == "home" else away


def _other(side: str) -> str:
    return "away" if side == "home" else "home"


def _favorite_side(game: dict[str, Any]) -> str | None:
    """None for a pick'em or missing spread."""
    spread = game["cbs_spread"]
    if spread is None or spread == 0:
        return None
    return "home" if spread < 0 else "away"


def _winner_side(game: dict[str, Any]) -> str | None:
    """Straight-up winner - None until FINAL, or for an actual tie."""
    if (
        game["status"] != "FINAL"
        or game["home_score"] is None
        or game["away_score"] is None
        or game["home_score"] == game["away_score"]
    ):
        return None
    return "home" if game["home_score"] > game["away_score"] else "away"


def _pick_side(game: dict[str, Any], pick: dict[str, Any]) -> str | None:
    if pick["picked_team_id"] == game["home_id"]:
        return "home"
    if pick["picked_team_id"] == game["away_id"]:
        return "away"
    return None


def _kickoff_slot(game: dict[str, Any]) -> str:
    kickoff = datetime.fromisoformat(game["game_time"]).astimezone(_EASTERN)
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


def _is_division_game(game: dict[str, Any]) -> bool:
    return (
        game["home_division"] is not None
        and game["home_division"] == game["away_division"]
    )


def _game_line(game: dict[str, Any]) -> dict[str, Any]:
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


# -- pool accuracy, perfect and winless weeks ----------------------------------


def _pool_accuracy_series(season: _Season) -> list[dict[str, Any]]:
    """Per week: graded picks (CBS's own is_correct, the pool's official
    grade) across active users, plus who went 5-0/0-5. A user only counts
    as perfect/winless once all 5 of their picks are graded."""
    weeks: dict[int, dict[str, Any]] = {}
    user_weeks: defaultdict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for picks in season.picks_by_game.values():
        for pick in picks:
            user_weeks[(pick["week_number"], pick["user_id"])].append(pick)

    for (week_number, _user_id), picks in user_weeks.items():
        row = weeks.setdefault(
            week_number,
            {
                "week": week_number,
                "correct": 0,
                "graded": 0,
                "perfect": [],
                "winless": [],
            },
        )
        graded = [p for p in picks if p["is_correct"] is not None]
        correct = sum(1 for p in graded if p["is_correct"])
        row["graded"] += len(graded)
        row["correct"] += correct
        if len(picks) == _PICKS_PER_WEEK and len(graded) == _PICKS_PER_WEEK:
            if correct == _PICKS_PER_WEEK:
                row["perfect"].append(picks[0])
            elif correct == 0:
                row["winless"].append(picks[0])

    series = []
    for week_number in sorted(weeks):
        row = weeks[week_number]
        row["accuracy"] = _pct(row["correct"], row["graded"])
        row["perfect"] = _people(row["perfect"])
        row["winless"] = _people(row["winless"])
        series.append(row)
    return series


def _pool_accuracy_items(
    season: _Season, series: list[dict[str, Any]], week_complete: bool
) -> list[dict[str, Any]]:
    by_week = {row["week"]: row for row in series}
    current = by_week.get(season.week)
    if current is None or not current["graded"]:
        return []

    items = []
    graded_weeks = [row for row in series if row["graded"]]
    prior = [row for row in graded_weeks if row["week"] < season.week]
    prior_correct = sum(row["correct"] for row in prior)
    prior_graded = sum(row["graded"] for row in prior)
    so_far = "" if week_complete else " so far"

    headline = (
        f"The pool hit {_pct_text(current['correct'], current['graded'])} this week"
        f"{so_far} ({current['correct']} of {current['graded']})"
    )
    score = 1.5
    rank_note = None
    if week_complete and len(graded_weeks) >= 2:
        ordered = sorted(graded_weeks, key=lambda r: -(r["accuracy"] or 0))
        if ordered[0]["week"] == season.week:
            rank_note, score = "best", 2.5
        elif ordered[-1]["week"] == season.week:
            rank_note, score = "worst", 2.5
    short = (
        f"Pool hit {_pct_text(current['correct'], current['graded'])} this week{so_far}"
    )
    if rank_note:
        headline += f", its {rank_note} week of the season"
        short += f", {rank_note} of the season"
    elif prior_graded:
        headline += f", vs {_pct_text(prior_correct, prior_graded)} before this week"
    items.append(
        _item(
            "pool_accuracy",
            "pool",
            "week",
            score,
            headline + ".",
            short,
            {
                "correct": current["correct"],
                "graded": current["graded"],
                "accuracy": current["accuracy"],
                "prior_accuracy": _pct(prior_correct, prior_graded),
                "season_rank_note": rank_note,
            },
            sample_size=current["graded"],
        )
    )

    if current["perfect"]:
        count = len(current["perfect"])
        names = [p["name"] for p in current["perfect"]]
        headline = (
            f"{_names_text(names)} went 5-0 this week."
            if count <= 3
            else f"{count} perfect 5-0 weeks: {_names_text(names)}."
        )
        short = (
            f"5-0: {_names_text(names, 2)}"
            if count <= 2
            else f"{count} perfect 5-0 weeks"
        )
        items.append(
            _item(
                "perfect_week",
                "pool",
                "week",
                2.5 + 0.2 * count,
                headline,
                short,
                {"users": current["perfect"]},
                sample_size=count,
            )
        )
    elif week_complete:
        items.append(
            _item(
                "perfect_week",
                "pool",
                "week",
                1.0,
                "Nobody went 5-0 this week.",
                "Nobody went 5-0",
                {"users": []},
                sample_size=0,
            )
        )

    if current["winless"]:
        count = len(current["winless"])
        names = [p["name"] for p in current["winless"]]
        headline = (
            f"Rough week: {_names_text(names)} went 0-5."
            if count <= 3
            else f"{count} people went 0-5 this week: {_names_text(names)}."
        )
        short = (
            f"0-5: {_names_text(names, 2)}"
            if count <= 2
            else f"{count} people went 0-5"
        )
        items.append(
            _item(
                "winless_week",
                "pool",
                "week",
                2.2 + 0.2 * count,
                headline,
                short,
                {"users": current["winless"]},
                sample_size=count,
            )
        )
    return items


# -- the spread mattered ------------------------------------------------------


def _spread_mattered_counts(
    games: list[dict[str, Any]], picks_by_game: dict[int, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Of FINAL games with a straight-up winner and a spread: how many the
    winner also covered, how many the spread flipped (winner didn't
    cover), and pushes. `winner_lost_picks` counts pool picks that had the
    winner and still lost the pick."""
    covered = flipped = pushes = winner_lost_picks = 0
    flipped_games = []
    for game in games:
        winner = _winner_side(game)
        side = ats_side(game)
        if winner is None or side is None or _favorite_side(game) is None:
            continue
        if side == "push":
            pushes += 1
        elif side == winner:
            covered += 1
        else:
            flipped += 1
            burned = [
                p["name"]
                for p in picks_by_game.get(game["game_id"], [])
                if _pick_side(game, p) == winner
            ]
            winner_lost_picks += len(burned)
            flipped_games.append(
                {
                    **_game_line(game),
                    "winner": _team(game, winner),
                    "picks_burned": len(burned),
                }
            )
    return {
        "games": covered + flipped + pushes,
        "winner_covered": covered,
        "spread_flipped": flipped,
        "pushes": pushes,
        "winner_lost_picks": winner_lost_picks,
        "flipped_games": flipped_games,
    }


def _spread_mattered_items(season: _Season) -> list[dict[str, Any]]:
    items = []
    week = _spread_mattered_counts(season.week_games(season.week), season.picks_by_game)
    if week["games"]:
        if week["spread_flipped"] == 0:
            headline = (
                f"Pick the winner and you covered all {week['games']} games this week"
                " - the spread never mattered."
            )
            short = f"Winners covered all {week['games']} games this week"
            score = 2.3
        else:
            headline = (
                f"The spread only flipped {week['spread_flipped']} of {week['games']} games"
                f" this week: pick the winner and you covered"
                f" {_pct_text(week['winner_covered'], week['games'])} of the time."
            )
            if week["winner_lost_picks"]:
                headline += (
                    f" {week['winner_lost_picks']} picks had the winner and still lost."
                )
            short = f"Spread flipped {week['spread_flipped']} of {week['games']} games this week"
            score = 1.5 + min(1.0, week["spread_flipped"] / max(1, week["games"]) * 2)
        items.append(
            _item(
                "spread_mattered",
                "spread",
                "week",
                score,
                headline,
                short,
                week,
                sample_size=week["games"],
            )
        )

    season_counts = _spread_mattered_counts(season.games, season.picks_by_game)
    if season_counts["games"] and season.week > 1:
        headline = (
            f"This season, picking the winner covered the spread"
            f" {_pct_text(season_counts['winner_covered'], season_counts['games'])} of the time"
            f" ({season_counts['winner_covered']} of {season_counts['games']} games)."
        )
        season_data = {k: v for k, v in season_counts.items() if k != "flipped_games"}
        items.append(
            _item(
                "spread_mattered",
                "spread",
                "season",
                1.2,
                headline,
                "Winners cover"
                f" {_pct_text(season_counts['winner_covered'], season_counts['games'])}"
                " of the time this season",
                season_data,
                sample_size=season_counts["games"],
                key="season",
            )
        )
    return items


# -- crowd record, fade the crowd, popular picks -------------------------------


def _crowd_results(
    games: list[dict[str, Any]], picks_by_game: dict[int, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Every FINAL game with a spread where more of the pool took one side
    than the other (a tie is skipped), with `result` = 'win'/'loss'/'push'
    for that side against the spread and `pick_count` on it."""
    results = []
    for game in games:
        side_covered = ats_side(game)
        if side_covered is None:
            continue
        picks = picks_by_game.get(game["game_id"], [])
        home = sum(1 for p in picks if _pick_side(game, p) == "home")
        away = sum(1 for p in picks if _pick_side(game, p) == "away")
        if home == away:
            continue
        crowd_side = "home" if home > away else "away"
        results.append(
            {
                **_game_line(game),
                "crowd_team": _team(game, crowd_side),
                "pick_count": max(home, away),
                "crowd_pct": round(max(home, away) / (home + away), 3),
                "result": (
                    "push"
                    if side_covered == "push"
                    else "win"
                    if side_covered == crowd_side
                    else "loss"
                ),
            }
        )
    return results


def _wlp(entries: list[dict[str, Any]]) -> tuple[int, int, int]:
    wins = sum(1 for e in entries if e["result"] == "win")
    losses = sum(1 for e in entries if e["result"] == "loss")
    return wins, losses, len(entries) - wins - losses


def _crowd_items(season: _Season) -> list[dict[str, Any]]:
    """crowd_record: the side more of the pool took, game by game.
    popular_picks: just the crowd sides picked by _POPULAR_POOL_SHARE of
    that week's pool - a team 12 people picked says more about the pool
    than one a 4-1 split made the crowd side of a game few people picked.
    The pool size is that week's weekly_performance rows (every active
    member), not the people whose picks are visible so far, which before
    the Sunday deadline is only a handful."""
    items = []
    season_results = _crowd_results(season.games, season.picks_by_game)
    week_results = [e for e in season_results if e["week"] == season.week]

    pool_size_by_week: defaultdict[int, int] = defaultdict(int)
    for row in season.performance:
        pool_size_by_week[row["week_number"]] += 1
    min_picks_by_week = {
        week: math.ceil(_POPULAR_POOL_SHARE * size)
        for week, size in pool_size_by_week.items()
    }
    min_picks = min_picks_by_week.get(season.week)

    wins, losses, pushes = _wlp(season_results)
    if wins + losses:
        headline = (
            f"The crowd's side is {_record_text(wins, losses, pushes)}"
            " against the spread this season"
        )
        if wins < losses:
            headline += f" - fading it would be {_record_text(losses, wins, pushes)}."
        else:
            headline += "."
        items.append(
            _item(
                "crowd_record",
                "crowd",
                "season",
                1.0 + abs(_z(wins, wins + losses)),
                headline,
                f"Crowd's side is {_record_text(wins, losses, pushes)} ATS this season",
                {
                    "wins": wins,
                    "losses": losses,
                    "pushes": pushes,
                    "fade_wins": losses,
                    "fade_losses": wins,
                },
                sample_size=wins + losses,
                key="season",
            )
        )

    wins, losses, pushes = _wlp(week_results)
    if wins + losses:
        items.append(
            _item(
                "crowd_record",
                "crowd",
                "week",
                0.8 + abs(_z(wins, wins + losses)) / 2,
                f"The crowd's side went {_record_text(wins, losses, pushes)}"
                " against the spread this week.",
                f"Crowd's side went {_record_text(wins, losses, pushes)} ATS this week",
                {
                    "wins": wins,
                    "losses": losses,
                    "pushes": pushes,
                    "games": week_results,
                },
                sample_size=wins + losses,
            )
        )

    # a week with no weekly_performance rows has no pool size to judge by
    popular_season = [
        e
        for e in season_results
        if e["week"] in min_picks_by_week
        and e["pick_count"] >= min_picks_by_week[e["week"]]
    ]
    popular_week = sorted(
        (e for e in popular_season if e["week"] == season.week),
        key=lambda e: -e["pick_count"],
    )

    wins, losses, pushes = _wlp(popular_week)
    if wins + losses:
        top = popular_week[0]
        outcome = {"win": "covered", "loss": "didn't cover", "push": "pushed"}[
            top["result"]
        ]
        items.append(
            _item(
                "popular_picks",
                "crowd",
                "week",
                1.0 + abs(_z(wins, wins + losses)) / 2,
                f"Teams {min_picks}+ of you picked went"
                f" {_record_text(wins, losses, pushes)} this week. Most picked:"
                f" {top['crowd_team']['abbr']} ({top['pick_count']} of you) {outcome}.",
                f"Popular picks went {_record_text(wins, losses, pushes)}; most picked"
                f" {top['crowd_team']['abbr']} {outcome}",
                {
                    "pool_share": _POPULAR_POOL_SHARE,
                    "pool_size": pool_size_by_week[season.week],
                    "min_picks": min_picks,
                    "wins": wins,
                    "losses": losses,
                    "pushes": pushes,
                    "games": popular_week,
                },
                sample_size=wins + losses,
            )
        )

    wins, losses, pushes = _wlp(popular_season)
    if wins + losses >= _POPULAR_MIN_GAMES:
        # the count only reads right when every week had the same pool size
        who = (
            f"{min_picks}+ of you"
            if len(set(min_picks_by_week.values())) == 1
            else f"{round(_POPULAR_POOL_SHARE * 100)}%+ of the pool"
        )
        items.append(
            _item(
                "popular_picks",
                "crowd",
                "season",
                1.0 + abs(_z(wins, wins + losses)),
                f"Teams {who} picked are"
                f" {_record_text(wins, losses, pushes)} against the spread this season.",
                f"Popular picks are {_record_text(wins, losses, pushes)} ATS this season",
                {
                    "pool_share": _POPULAR_POOL_SHARE,
                    "min_picks_by_week": {
                        str(w): n for w, n in sorted(min_picks_by_week.items())
                    },
                    "wins": wins,
                    "losses": losses,
                    "pushes": pushes,
                },
                sample_size=wins + losses,
                key="season",
            )
        )
    return items


# -- chaos index --------------------------------------------------------------


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
        favorite = _favorite_side(game)
        if favorite is None:
            continue
        side = ats_side(game)
        if side in ("home", "away"):
            decided += 1
            dog_covers += side != favorite
        winner = _winner_side(game)
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


def _chaos_series(
    season: _Season, pool_series: list[dict[str, Any]]
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


def _chaos_items(
    season: _Season, chaos_series: list[dict[str, Any]]
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
        _item(
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


# -- twins and oppos ----------------------------------------------------------


def _twins_and_oppos_items(season: _Season) -> list[dict[str, Any]]:
    """Twins: two or more users with the exact same 5 picks. Oppos: two
    users on the same 5 games, every one on the opposite side. Only users
    whose 5 picks are all visible count."""
    games = {g["game_id"]: g for g in season.week_games(season.week)}
    picks_by_user: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for game_id in games:
        for pick in season.picks_by_game.get(game_id, []):
            picks_by_user[pick["user_id"]].append(pick)

    full = {
        user_id: picks
        for user_id, picks in picks_by_user.items()
        if len(picks) == _PICKS_PER_WEEK
    }

    def record(picks: list[dict[str, Any]]) -> dict[str, Any]:
        graded = [p for p in picks if p["is_correct"] is not None]
        correct = sum(1 for p in graded if p["is_correct"])
        return {"correct": correct, "graded": len(graded)}

    def picks_json(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "game_id": p["game_id"],
                "team": _team(games[p["game_id"]], _pick_side(games[p["game_id"]], p)),
            }
            for p in sorted(picks, key=lambda p: games[p["game_id"]]["game_time"])
        ]

    items = []
    groups: defaultdict[frozenset[tuple[int, int]], list[int]] = defaultdict(list)
    for user_id, picks in full.items():
        groups[frozenset((p["game_id"], p["picked_team_id"]) for p in picks)].append(
            user_id
        )
    for user_ids in groups.values():
        if len(user_ids) < 2:
            continue
        users = _people([full[u][0] for u in user_ids])
        names = [u["name"] for u in users]
        first = full[user_ids[0]]
        items.append(
            _item(
                "twins",
                "users",
                "week",
                2.0 + 0.2 * (len(user_ids) - 2),
                f"Twins: {_names_text(names, limit=4)} made the exact same 5 picks.",
                f"Twins: {' & '.join(names)}"
                if len(names) == 2
                else f"{len(names)} people made the same 5 picks",
                {"users": users, "picks": picks_json(first), "record": record(first)},
                sample_size=len(user_ids),
                key="-".join(str(u) for u in sorted(user_ids)),
            )
        )

    for (user_a, picks_a), (user_b, picks_b) in combinations(full.items(), 2):
        by_game_a = {p["game_id"]: p["picked_team_id"] for p in picks_a}
        by_game_b = {p["game_id"]: p["picked_team_id"] for p in picks_b}
        if by_game_a.keys() != by_game_b.keys():
            continue
        if any(by_game_a[g] == by_game_b[g] for g in by_game_a):
            continue
        rec_a, rec_b = record(picks_a), record(picks_b)
        name_a, name_b = picks_a[0]["name"], picks_b[0]["name"]
        headline = (
            f"Opposites: {name_a} and {name_b} took opposite sides of the same 5 games."
        )
        if rec_a["graded"] == _PICKS_PER_WEEK:
            if rec_a["correct"] == rec_b["correct"]:
                headline += f" They split it {rec_a['correct']}-{rec_b['correct']}."
            else:
                leader, lead, trail = (
                    (name_a, rec_a, rec_b)
                    if rec_a["correct"] > rec_b["correct"]
                    else (name_b, rec_b, rec_a)
                )
                headline += f" {leader} won it {lead['correct']}-{trail['correct']}."
        items.append(
            _item(
                "oppos",
                "users",
                "week",
                2.5,
                headline,
                f"Opposites: {name_a} vs {name_b}",
                {
                    "users": [
                        {
                            **_person(picks_a[0]),
                            "picks": picks_json(picks_a),
                            "record": rec_a,
                        },
                        {
                            **_person(picks_b[0]),
                            "picks": picks_json(picks_b),
                            "record": rec_b,
                        },
                    ]
                },
                sample_size=2,
                key=f"{min(user_a, user_b)}-{max(user_a, user_b)}",
            )
        )
    return items


# -- cover streaks ------------------------------------------------------------


def _active_cover_streaks(season: _Season) -> list[dict[str, Any]]:
    """Every team's active run of covers or non-covers of _MIN_COVER_STREAK+,
    through its latest FINAL game with a spread, longest first. A push ends
    a streak either way. The key's top-level `cover_streaks` (a badge per
    team) - the recap items below only headline the longest."""
    results_by_team: defaultdict[int, list[str]] = defaultdict(list)
    teams: dict[int, dict[str, Any]] = {}
    for game in season.games:  # already game_time order
        side = ats_side(game)
        if side is None:
            continue
        for team_side in ("home", "away"):
            team = _team(game, team_side)
            teams[team["id"]] = team
            results_by_team[team["id"]].append(
                "push" if side == "push" else "cover" if side == team_side else "miss"
            )

    streaks = []
    for team_id, results in results_by_team.items():
        last = results[-1]
        if last == "push":
            continue
        length = 0
        for result in reversed(results):
            if result != last:
                break
            length += 1
        if length >= _MIN_COVER_STREAK:
            streaks.append(
                {"team": teams[team_id], "streak_type": last, "length": length}
            )
    streaks.sort(key=lambda s: (-s["length"], s["team"]["abbr"]))
    return streaks


def _cover_streak_items(streaks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for kind, verb, short_verb in (
        ("cover", "covered", "covered"),
        ("miss", "failed to cover", "missed"),
    ):
        of_kind = [s for s in streaks if s["streak_type"] == kind]
        if not of_kind:
            continue
        best = of_kind[0]["length"]  # longest first
        leaders = [s["team"] for s in of_kind if s["length"] == best]
        abbrs = _names_text([t["abbr"] for t in leaders])
        headline = (
            f"{abbrs} {'have' if len(leaders) > 1 else 'has'} {verb} {best} straight."
        )
        items.append(
            _item(
                "cover_streak",
                "teams",
                "season",
                1.0 + best / 3,
                headline,
                f"{abbrs} {short_verb} {best} straight",
                {"streak_type": kind, "length": best, "teams": leaders},
                sample_size=best,
                key=kind,
            )
        )
    return items


# -- biggest mover ------------------------------------------------------------


def _rank_moves(season: _Season) -> list[dict[str, Any]]:
    """Every user's cumulative-score rank after last week vs after this
    week, ranked the same way as the leaderboard (standard_rank, ties share
    a place). Empty in week 1."""
    if season.week < 2:
        return []
    names: dict[int, str] = {}
    before: defaultdict[int, int] = defaultdict(int)
    after: defaultdict[int, int] = defaultdict(int)
    for row in season.performance:
        names[row["user_id"]] = row["name"]
        correct = row["picks_correct"] or 0
        after[row["user_id"]] += correct
        if row["week_number"] < season.week:
            before[row["user_id"]] += correct
    for user_id in after:
        before.setdefault(user_id, 0)

    rank_before = standard_rank(dict(before))
    rank_after = standard_rank(dict(after))
    moves = [
        {
            "user_id": user_id,
            "name": names[user_id],
            "rank_before": rank_before[user_id],
            "rank_after": rank_after[user_id],
            "change": rank_before[user_id] - rank_after[user_id],  # positive = climbed
        }
        for user_id in after
    ]
    moves.sort(key=lambda m: (-m["change"], m["rank_after"]))
    return moves


def _biggest_mover_items(moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Just the biggest climb and drop (ties listed together) - every move
    of _MIN_RANK_MOVE+ is in the key's top-level `movers` instead."""
    items = []
    climb = max(moves, key=lambda m: m["change"], default=None)
    drop = min(moves, key=lambda m: m["change"], default=None)
    if climb and climb["change"] >= _MIN_RANK_MOVE:
        climbers = sorted(m["name"] for m in moves if m["change"] == climb["change"])
        items.append(
            _item(
                "biggest_mover",
                "users",
                "week",
                1.5 + climb["change"] / 10,
                f"{_names_text(climbers)} jumped {climb['change']} spots"
                + (
                    f" to {_ordinal(climb['rank_after'])}."
                    if len(climbers) == 1
                    else "."
                ),
                f"{_names_text(climbers, 2)} up {climb['change']} spots"
                + (
                    f" to {_ordinal(climb['rank_after'])}" if len(climbers) == 1 else ""
                ),
                {
                    "direction": "up",
                    "moves": [m for m in moves if m["change"] == climb["change"]],
                },
                sample_size=len(moves),
                key="up",
            )
        )
    if drop and -drop["change"] >= _MIN_RANK_MOVE:
        droppers = sorted(m["name"] for m in moves if m["change"] == drop["change"])
        items.append(
            _item(
                "biggest_mover",
                "users",
                "week",
                1.3 + -drop["change"] / 10,
                f"{_names_text(droppers)} slid {-drop['change']} spots"
                + (
                    f" to {_ordinal(drop['rank_after'])}."
                    if len(droppers) == 1
                    else "."
                ),
                f"{_names_text(droppers, 2)} down {-drop['change']} spots"
                + (f" to {_ordinal(drop['rank_after'])}" if len(droppers) == 1 else ""),
                {
                    "direction": "down",
                    "moves": [m for m in moves if m["change"] == drop["change"]],
                },
                sample_size=len(moves),
                key="down",
            )
        )
    return items


# -- upset of the week --------------------------------------------------------


def _upset_items(season: _Season) -> list[dict[str, Any]]:
    """Biggest-spread underdog to win outright this week, and who had it."""
    upsets = []
    for game in season.week_games(season.week):
        favorite, winner = _favorite_side(game), _winner_side(game)
        if favorite is None or winner is None or winner == favorite:
            continue
        upsets.append((abs(game["cbs_spread"]), game, winner))
    if not upsets:
        return []
    points, game, winner = max(upsets, key=lambda u: u[0])
    dog, favorite = _team(game, winner), _team(game, _other(winner))
    picks = season.picks_by_game.get(game["game_id"], [])
    believers = _people([p for p in picks if _pick_side(game, p) == winner])
    believer_names = [b["name"] for b in believers]
    headline = f"Upset of the week: {dog['abbr']} (+{points:g}) beat {favorite['abbr']} outright."
    if picks:
        headline += (
            f" {len(believers)} of the {len(picks)} who picked that game had them"
            + (f": {_names_text(believer_names)}." if 0 < len(believers) <= 3 else ".")
        )
    return [
        _item(
            "upset_of_week",
            "chaos",
            "week",
            1.5 + points / 7,
            headline,
            f"Upset: {dog['abbr']} (+{points:g}) beat {favorite['abbr']} outright",
            {
                **_game_line(game),
                "underdog": dog,
                "favorite": favorite,
                "points": points,
                "believers": believers,
                "pool_picks": len(picks),
            },
            sample_size=1,
        )
    ]


# -- stand-out splits ---------------------------------------------------------


def _standout(
    kind: str,
    key: str,
    category: str,
    successes: int,
    n: int,
    min_n: int,
    headline: str,
    short: str,
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    if not _stands_out(successes, n, min_n):
        return []
    z = _z(successes, n)
    return [
        _item(
            kind,
            category,
            "season",
            min(_STANDOUT_MAX_SCORE, abs(z)),
            headline,
            short,
            {
                **data,
                "successes": successes,
                "n": n,
                "pct": _pct(successes, n),
                "z": round(z, 2),
            },
            sample_size=n,
            key=key,
        )
    ]


def _game_split_items(season: _Season) -> list[dict[str, Any]]:
    """League-wide cover splits: home vs road, favorites vs underdogs,
    home underdogs, underdogs in division games. Neutral-site games skip
    anything home/road."""
    home_covers = home_n = fav_covers = fav_n = home_dog_covers = home_dog_n = 0
    div_dog_covers = div_n = 0
    for game in season.games:
        side = ats_side(game)
        if side not in ("home", "away"):
            continue
        if not game["neutral_site"]:
            home_n += 1
            home_covers += side == "home"
        favorite = _favorite_side(game)
        if favorite is None:
            continue
        fav_n += 1
        fav_covers += side == favorite
        if favorite == "away" and not game["neutral_site"]:
            home_dog_n += 1
            home_dog_covers += side == "home"
        if _is_division_game(game):
            div_n += 1
            div_dog_covers += side != favorite

    # category "league": league-wide cover trends, independent of the pool's
    # own picks (those are pool_split, category "splits"). The ":league" id
    # suffix predates the category and is kept so ids stay stable.
    items = []
    leader = "Home" if home_covers * 2 >= home_n else "Road"
    lead = home_covers if leader == "Home" else home_n - home_covers
    record = _record_text(lead, home_n - lead)
    items += _standout(
        "home_road_covers",
        "league",
        "league",
        home_covers,
        home_n,
        _STANDOUT_MIN_GAMES,
        f"{leader} teams are {record} against the spread this season.",
        f"{leader} teams are {record} ATS this season",
        {"side": "home"},
    )
    leader = "Favorites" if fav_covers * 2 >= fav_n else "Underdogs"
    lead = fav_covers if leader == "Favorites" else fav_n - fav_covers
    record = _record_text(lead, fav_n - lead)
    items += _standout(
        "favorite_covers",
        "league",
        "league",
        fav_covers,
        fav_n,
        _STANDOUT_MIN_GAMES,
        f"{leader} are {record} against the spread this season.",
        f"{leader} are {record} ATS this season",
        {"side": "favorite"},
    )
    record = _record_text(home_dog_covers, home_dog_n - home_dog_covers)
    items += _standout(
        "home_underdog_covers",
        "league",
        "league",
        home_dog_covers,
        home_dog_n,
        _STANDOUT_MIN_GAMES,
        f"Home underdogs are {record} against the spread this season.",
        f"Home underdogs are {record} ATS this season",
        {"side": "home_underdog"},
    )
    record = _record_text(div_dog_covers, div_n - div_dog_covers)
    items += _standout(
        "division_underdog_covers",
        "league",
        "league",
        div_dog_covers,
        div_n,
        _STANDOUT_MIN_GAMES,
        f"Underdogs are {record} against the spread in division games.",
        f"Underdogs are {record} ATS in division games",
        {"side": "division_underdog"},
    )
    return items


def _pool_split_items(season: _Season) -> list[dict[str, Any]]:
    """The pool's own ATS record split by what kind of pick it was:
    favorite/underdog, home/road, kickoff slot, division game. Graded by
    ats_side() (same as season:trends), pushes left out. Also flags a
    lopsided favorite/underdog lean on its own."""
    counters: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])  # [correct, n]
    fav_lean = [
        0,
        0,
    ]  # [favorite picks, picks with a favorite] - share only, not a recap item

    for game in season.games:
        side = ats_side(game)
        if side not in ("home", "away"):
            continue
        favorite = _favorite_side(game)
        slot = _kickoff_slot(game)
        for pick in season.picks_by_game.get(game["game_id"], []):
            pick_side = _pick_side(game, pick)
            if pick_side is None:
                continue
            correct = pick_side == side
            buckets = [f"slot:{slot}"]
            if not game["neutral_site"]:
                buckets.append(f"side:{pick_side}")
            if favorite is not None:
                is_fav = pick_side == favorite
                buckets.append("fav:favorite" if is_fav else "fav:underdog")
                fav_lean[0] += is_fav
                fav_lean[1] += 1
            if _is_division_game(game):
                buckets.append("division:yes")
            for bucket in buckets:
                counters[bucket][0] += correct
                counters[bucket][1] += 1

    labels = {
        "side:home": "picking home teams",
        "side:away": "picking road teams",
        "fav:favorite": "picking favorites",
        "fav:underdog": "picking underdogs",
        "division:yes": "in division games",
        **{f"slot:{slot}": f"on {label} games" for slot, label in _SLOT_LABELS.items()},
    }

    items = []
    for bucket, (correct, n) in sorted(counters.items()):
        items += _standout(
            "pool_split",
            bucket,
            "splits",
            correct,
            n,
            _STANDOUT_MIN_POOL_PICKS,
            f"The pool is {_record_text(correct, n - correct)} ({_pct_text(correct, n)})"
            f" {labels[bucket]} this season.",
            f"Pool is {_record_text(correct, n - correct)} {labels[bucket]}",
            {"bucket": bucket, "label": labels[bucket]},
        )

    # the lean itself isn't news (a pool always leans favorite), so it rides
    # along on the favorite/underdog splits rather than being its own recap item
    for item in items:
        if item["data"]["bucket"] in ("fav:favorite", "fav:underdog"):
            item["data"]["favorite_pick_share"] = _pct(*fav_lean)
    return items


def _team_split_items(season: _Season) -> list[dict[str, Any]]:
    """One team's ATS record in primetime, in division games, at home and
    on the road - only the ones that clear _STANDOUT_MIN_TEAM_GAMES and the
    z bar, which a team's small samples rarely do before midseason."""
    counters: defaultdict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
    teams: dict[int, dict[str, Any]] = {}
    for game in season.games:
        side = ats_side(game)
        if side not in ("home", "away"):
            continue
        primetime = _kickoff_slot(game) in _PRIMETIME_SLOTS
        division = _is_division_game(game)
        for team_side in ("home", "away"):
            team = _team(game, team_side)
            teams[team["id"]] = team
            covered = side == team_side
            buckets = []
            if primetime:
                buckets.append("primetime")
            if division:
                buckets.append("division")
            if not game["neutral_site"]:
                buckets.append(team_side)
            for bucket in buckets:
                counters[(team["id"], bucket)][0] += covered
                counters[(team["id"], bucket)][1] += 1

    labels = {
        "primetime": "in primetime",
        "division": "in division games",
        "home": "at home",
        "away": "on the road",
    }
    items = []
    for (team_id, bucket), (covers, n) in sorted(counters.items()):
        team = teams[team_id]
        items += _standout(
            "team_split",
            f"{team['abbr']}:{bucket}",
            "teams",
            covers,
            n,
            _STANDOUT_MIN_TEAM_GAMES,
            f"{team['abbr']} are {_record_text(covers, n - covers)} against the spread"
            f" {labels[bucket]} this season.",
            f"{team['abbr']} are {_record_text(covers, n - covers)} ATS {labels[bucket]}",
            {"team": team, "bucket": bucket, "label": labels[bucket]},
        )
    return items


# -- writer -------------------------------------------------------------------


def compute_week_recap(d1: D1Client, week_number: int) -> dict[str, Any] | None:
    games = d1.query(_SEASON_GAMES_SQL, [SEASON, week_number]).results
    if not any(g["week_number"] == week_number for g in games):
        return None

    picks_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in d1.query(_SEASON_PICKS_SQL, [SEASON, week_number]).results:
        picks_by_game[pick["game_id"]].append(pick)
    performance = d1.query(_SEASON_PERFORMANCE_SQL, [SEASON, week_number]).results

    season = _Season(week_number, games, picks_by_game, performance)
    week_games = season.week_games(week_number)
    games_final = sum(1 for g in week_games if g["status"] == "FINAL")
    week_complete = games_final == len(week_games)

    pool_series = _pool_accuracy_series(season)
    chaos_series = _chaos_series(season, pool_series)

    moves = _rank_moves(season)
    cover_streaks = _active_cover_streaks(season)

    items = [
        *_pool_accuracy_items(season, pool_series, week_complete),
        *_spread_mattered_items(season),
        *_crowd_items(season),
        *_chaos_items(season, chaos_series),
        *_twins_and_oppos_items(season),
        *_cover_streak_items(cover_streaks),
        *_biggest_mover_items(moves),
        *_upset_items(season),
        *_game_split_items(season),
        *_pool_split_items(season),
        *_team_split_items(season),
    ]
    items.sort(key=lambda t: -t["score"])

    return {
        "version": SCHEMA_VERSION,
        "season": SEASON,
        "week": week_number,
        "updated_at": utc_iso(),
        "week_complete": week_complete,
        "games_final": games_final,
        "games_total": len(week_games),
        "items": items,
        "series": {
            "pool_accuracy": pool_series,
            "chaos": chaos_series,
        },
        # every leaderboard move of _MIN_RANK_MOVE+ places (the mover
        # recap items only headline the biggest), for arrows on each row
        "movers": [m for m in moves if abs(m["change"]) >= _MIN_RANK_MOVE],
        # every active team streak of _MIN_COVER_STREAK+, for game badges
        "cover_streaks": cover_streaks,
    }


def write_week_recap(week_number: int) -> None:
    """Write week:{season}:{weekNN}:recap from compute_week_recap()."""
    d1 = get_d1()
    payload = compute_week_recap(d1, week_number)
    if payload is None:
        logger.warning(
            "No games found for season %s week %s - not writing recap key",
            SEASON,
            week_number,
        )
        return

    get_kv().write(f"week:{SEASON}:{week_number:02d}:recap", payload)
    logger.info(
        "Wrote week:%s:%02d:recap (%d items) to KV",
        SEASON,
        week_number,
        len(payload["items"]),
    )


def write_current_week_recap() -> None:
    """Resolve weeks.is_current and write that week's recap key."""
    for_current_week(write_week_recap, "recap key")


def write_recent_weeks_recap() -> None:
    """What orchestration refreshes: the current week, any week that has
    started but isn't complete, and any week whose last kickoff was within
    _RECENT_WEEK_HOURS. CBS can move weeks.is_current on before Monday
    night's game ends, and the pool's grades (CBS's is_correct) land a
    poll or two after a game goes FINAL - writing only the current week
    lost the old week's final state (final chaos index, "nobody went 5-0",
    final accuracy)."""
    d1 = get_d1()
    now = datetime.now(UTC)
    rows = d1.query(
        _RECENT_WEEKS_SQL,
        [
            SEASON,
            utc_iso(now),
            utc_iso(now - timedelta(hours=_RECENT_WEEK_HOURS)),
        ],
    ).results
    if not rows:
        logger.info(
            "No current or recent weeks for season %s - no recap to write", SEASON
        )
        return
    for row in rows:
        write_week_recap(row["week_number"])
