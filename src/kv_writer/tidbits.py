"""week:{season}:{weekNN}:tidbits - see src/CLAUDE.md's KV writer section.

Short "did you know" items for the web UI's weekly infographic. Each
generator below returns zero or more candidate tidbits, and the key holds
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
from datetime import datetime
from itertools import combinations
from typing import Any
from zoneinfo import ZoneInfo

from config.config import SEASON, get_d1_config, get_kv_config
from db.d1_client import D1Client
from db.kv_client import KVClient
from src.kv_writer.shared import (
    ats_side,
    game_team_dicts,
    now_iso,
    resolve_current_week,
    standard_rank,
)

logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")

# hand-picked judgment calls, same spirit as trends.py's thresholds
_STANDOUT_MIN_Z = 1.5  # distance from a coin flip, in standard deviations
# z grows with sample size, so without a cap a big season-long split would
# outrank every weekly tidbit by midseason
_STANDOUT_MAX_SCORE = 3.0
_STANDOUT_MIN_POOL_PICKS = 20  # floor for a pool-wide pick split
_STANDOUT_MIN_GAMES = 10  # floor for a league-wide game split
_STANDOUT_MIN_TEAM_GAMES = 4  # floor for one team's split
_LOCK_THRESHOLD = 0.8  # consensus share that makes a game a "lock"
_LOCK_MIN_PICKS = 3
_LOCK_MIN_GAMES = 3  # locks record needs a few games before it means anything
_BIG_FAVORITE_POINTS = 7.0  # a favorite of this much or more losing outright is chaos
_MIN_COVER_STREAK = 3
_MIN_RANK_MOVE = 3  # spots climbed/dropped before a mover is worth a tidbit
_PICKS_PER_WEEK = 5
_PRIMETIME_SLOTS = frozenset({"thursday", "sunday_night", "monday"})

_SLOT_LABELS = {
    "thursday": "Thursday night",
    "friday": "Friday",
    "saturday": "Saturday",
    "sunday_morning": "Sunday morning (international)",
    "sunday_early": "Sunday early",
    "sunday_late": "Sunday late afternoon",
    "sunday_night": "Sunday night",
    "monday": "Monday night",
    "other": "Other",
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
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _names_text(names: list[str], limit: int = 3) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def _tidbit(
    kind: str,
    category: str,
    scope: str,
    score: float,
    headline: str,
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
    return {0: "monday", 3: "thursday", 4: "friday", 5: "saturday"}.get(weekday, "other")


def _is_division_game(game: dict[str, Any]) -> bool:
    return game["home_division"] is not None and game["home_division"] == game["away_division"]


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
            {"week": week_number, "correct": 0, "graded": 0, "perfect": [], "winless": []},
        )
        graded = [p for p in picks if p["is_correct"] is not None]
        correct = sum(1 for p in graded if p["is_correct"])
        row["graded"] += len(graded)
        row["correct"] += correct
        if len(picks) == _PICKS_PER_WEEK and len(graded) == _PICKS_PER_WEEK:
            if correct == _PICKS_PER_WEEK:
                row["perfect"].append(picks[0]["name"])
            elif correct == 0:
                row["winless"].append(picks[0]["name"])

    series = []
    for week_number in sorted(weeks):
        row = weeks[week_number]
        row["accuracy"] = _pct(row["correct"], row["graded"])
        row["perfect"].sort()
        row["winless"].sort()
        series.append(row)
    return series


def _pool_accuracy_tidbits(
    season: _Season, series: list[dict[str, Any]], week_complete: bool
) -> list[dict[str, Any]]:
    by_week = {row["week"]: row for row in series}
    current = by_week.get(season.week)
    if current is None or not current["graded"]:
        return []

    tidbits = []
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
    if rank_note:
        headline += f", its {rank_note} week of the season"
    elif prior_graded:
        headline += f", vs {_pct_text(prior_correct, prior_graded)} before this week"
    tidbits.append(
        _tidbit(
            "pool_accuracy",
            "pool",
            "week",
            score,
            headline + ".",
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
        headline = (
            f"{_names_text(current['perfect'])} went 5-0 this week."
            if count <= 3
            else f"{count} perfect 5-0 weeks: {_names_text(current['perfect'])}."
        )
        tidbits.append(
            _tidbit(
                "perfect_week",
                "pool",
                "week",
                2.5 + 0.2 * count,
                headline,
                {"names": current["perfect"]},
                sample_size=count,
            )
        )
    elif week_complete:
        tidbits.append(
            _tidbit(
                "perfect_week",
                "pool",
                "week",
                1.0,
                "Nobody went 5-0 this week.",
                {"names": []},
                sample_size=0,
            )
        )

    if current["winless"]:
        count = len(current["winless"])
        headline = (
            f"Rough week: {_names_text(current['winless'])} went 0-5."
            if count <= 3
            else f"{count} people went 0-5 this week: {_names_text(current['winless'])}."
        )
        tidbits.append(
            _tidbit(
                "winless_week",
                "pool",
                "week",
                2.2 + 0.2 * count,
                headline,
                {"names": current["winless"]},
                sample_size=count,
            )
        )
    return tidbits


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
                {**_game_line(game), "winner": _team(game, winner), "picks_burned": len(burned)}
            )
    return {
        "games": covered + flipped + pushes,
        "winner_covered": covered,
        "spread_flipped": flipped,
        "pushes": pushes,
        "winner_lost_picks": winner_lost_picks,
        "flipped_games": flipped_games,
    }


def _spread_mattered_tidbits(season: _Season) -> list[dict[str, Any]]:
    tidbits = []
    week = _spread_mattered_counts(season.week_games(season.week), season.picks_by_game)
    if week["games"]:
        if week["spread_flipped"] == 0:
            headline = (
                f"Pick the winner and you covered all {week['games']} games this week"
                " - the spread never mattered."
            )
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
            score = 1.5 + min(1.0, week["spread_flipped"] / max(1, week["games"]) * 2)
        tidbits.append(
            _tidbit(
                "spread_mattered",
                "spread",
                "week",
                score,
                headline,
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
        tidbits.append(
            _tidbit(
                "spread_mattered",
                "spread",
                "season",
                1.2,
                headline,
                season_data,
                sample_size=season_counts["games"],
                key="season",
            )
        )
    return tidbits


# -- consensus record, fade the crowd, locks -----------------------------------


def _consensus_results(
    games: list[dict[str, Any]], picks_by_game: dict[int, list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(every game with a strict pool majority, just the locks), each entry
    carrying `result` = 'win'/'loss'/'push' for the majority side against
    the spread. Only FINAL games with a spread."""
    results, locks = [], []
    for game in games:
        side_covered = ats_side(game)
        if side_covered is None:
            continue
        picks = picks_by_game.get(game["game_id"], [])
        home = sum(1 for p in picks if _pick_side(game, p) == "home")
        away = sum(1 for p in picks if _pick_side(game, p) == "away")
        if home == away:
            continue
        majority = "home" if home > away else "away"
        share = max(home, away) / (home + away)
        result = (
            "push" if side_covered == "push" else "win" if side_covered == majority else "loss"
        )
        entry = {
            **_game_line(game),
            "consensus_team": _team(game, majority),
            "consensus_pct": round(share, 3),
            "result": result,
        }
        results.append(entry)
        if share >= _LOCK_THRESHOLD and home + away >= _LOCK_MIN_PICKS:
            locks.append(entry)
    return results, locks


def _wlp(entries: list[dict[str, Any]]) -> tuple[int, int, int]:
    wins = sum(1 for e in entries if e["result"] == "win")
    losses = sum(1 for e in entries if e["result"] == "loss")
    return wins, losses, len(entries) - wins - losses


def _consensus_tidbits(season: _Season) -> list[dict[str, Any]]:
    tidbits = []
    season_results, season_locks = _consensus_results(season.games, season.picks_by_game)
    week_results = [e for e in season_results if e["week"] == season.week]

    wins, losses, pushes = _wlp(season_results)
    if wins + losses:
        z = _z(wins, wins + losses)
        headline = f"The crowd's side is {_record_text(wins, losses, pushes)} against the spread this season"
        if wins < losses:
            headline += f" - fading it would be {_record_text(losses, wins, pushes)}."
        else:
            headline += "."
        tidbits.append(
            _tidbit(
                "consensus_record",
                "crowd",
                "season",
                1.0 + abs(z),
                headline,
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
        tidbits.append(
            _tidbit(
                "consensus_record",
                "crowd",
                "week",
                0.8 + abs(_z(wins, wins + losses)) / 2,
                f"The crowd's side went {_record_text(wins, losses, pushes)} against the spread this week.",
                {"wins": wins, "losses": losses, "pushes": pushes, "games": week_results},
                sample_size=wins + losses,
            )
        )

    wins, losses, pushes = _wlp(season_locks)
    if wins + losses >= _LOCK_MIN_GAMES:
        tidbits.append(
            _tidbit(
                "consensus_locks",
                "crowd",
                "season",
                1.0 + abs(_z(wins, wins + losses)),
                f"When {round(_LOCK_THRESHOLD * 100)}%+ of the pool is on one side, that side is"
                f" {_record_text(wins, losses, pushes)} this season.",
                {
                    "threshold": _LOCK_THRESHOLD,
                    "wins": wins,
                    "losses": losses,
                    "pushes": pushes,
                    "games": [e for e in season_locks if e["week"] == season.week],
                },
                sample_size=wins + losses,
            )
        )
    return tidbits


# -- chaos index --------------------------------------------------------------


def _week_chaos(
    games: list[dict[str, Any]], pool_row: dict[str, Any] | None
) -> dict[str, Any] | None:
    """0-10 chaos score for one fully-FINAL week, the average of four 0-1
    parts: underdog cover rate, outright upset rate (doubled, capped at 1 -
    upsets are rarer than covers), pool miss rate, and the share of big
    favorites (_BIG_FAVORITE_POINTS+) that lost outright. Weights are a
    judgment call."""
    if not games or any(g["status"] != "FINAL" for g in games):
        return None
    dog_covers = decided = upsets = su_decided = big_favs = big_fav_losses = 0
    for game in games:
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
    pool_miss = (
        1 - pool_row["accuracy"] if pool_row and pool_row["accuracy"] is not None else 0.5
    )
    parts = [
        dog_covers / decided,
        min(1.0, 2 * upsets / su_decided) if su_decided else 0.0,
        pool_miss,
        big_fav_losses / big_favs if big_favs else 0.0,
    ]
    return {
        "week": games[0]["week_number"],
        "index": round(10 * sum(parts) / len(parts), 1),
        "underdog_covers": dog_covers,
        "ats_decided": decided,
        "outright_upsets": upsets,
        "big_favorite_losses": big_fav_losses,
        "big_favorites": big_favs,
        "pool_accuracy": pool_row["accuracy"] if pool_row else None,
    }


def _chaos_series(season: _Season, pool_series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pool_by_week = {row["week"]: row for row in pool_series}
    series = []
    for week_number in sorted({g["week_number"] for g in season.games}):
        chaos = _week_chaos(season.week_games(week_number), pool_by_week.get(week_number))
        if chaos:
            series.append(chaos)
    return series


def _chaos_tidbits(season: _Season, chaos_series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = next((c for c in chaos_series if c["week"] == season.week), None)
    if current is None:
        return []
    headline = (
        f"Chaos index {current['index']}: underdogs covered {current['underdog_covers']}"
        f" of {current['ats_decided']} and {current['outright_upsets']} won outright."
    )
    score = 1.5
    rank = None
    if len(chaos_series) >= 2:
        ordered = sorted(chaos_series, key=lambda c: -c["index"])
        rank = next(i for i, c in enumerate(ordered, start=1) if c["week"] == season.week)
        if rank == 1:
            headline += " Most chaotic week of the season."
            score = 2.8
        elif rank == len(ordered):
            headline += " Chalkiest week of the season."
            score = 2.3
    return [
        _tidbit(
            "chaos_index",
            "chaos",
            "week",
            score,
            headline,
            {**current, "season_rank": rank, "weeks_ranked": len(chaos_series)},
            sample_size=current["ats_decided"],
        )
    ]


# -- twins and oppos ----------------------------------------------------------


def _twins_and_oppos_tidbits(season: _Season) -> list[dict[str, Any]]:
    """Twins: two or more users with the exact same 5 picks. Oppos: two
    users on the same 5 games, every one on the opposite side. Only users
    whose 5 picks are all visible count."""
    games = {g["game_id"]: g for g in season.week_games(season.week)}
    picks_by_user: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for game_id in games:
        for pick in season.picks_by_game.get(game_id, []):
            picks_by_user[pick["user_id"]].append(pick)

    full = {
        user_id: picks for user_id, picks in picks_by_user.items() if len(picks) == _PICKS_PER_WEEK
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

    tidbits = []
    groups: defaultdict[frozenset[tuple[int, int]], list[int]] = defaultdict(list)
    for user_id, picks in full.items():
        groups[frozenset((p["game_id"], p["picked_team_id"]) for p in picks)].append(user_id)
    for user_ids in groups.values():
        if len(user_ids) < 2:
            continue
        names = sorted(full[u][0]["name"] for u in user_ids)
        first = full[user_ids[0]]
        tidbits.append(
            _tidbit(
                "twins",
                "users",
                "week",
                2.0 + 0.2 * (len(user_ids) - 2),
                f"Twins: {_names_text(names, limit=4)} made the exact same 5 picks.",
                {"names": names, "picks": picks_json(first), "record": record(first)},
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
        headline = f"Opposites: {name_a} and {name_b} took opposite sides of the same 5 games."
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
        tidbits.append(
            _tidbit(
                "oppos",
                "users",
                "week",
                2.5,
                headline,
                {
                    "users": [
                        {"name": name_a, "picks": picks_json(picks_a), "record": rec_a},
                        {"name": name_b, "picks": picks_json(picks_b), "record": rec_b},
                    ]
                },
                sample_size=2,
                key=f"{min(user_a, user_b)}-{max(user_a, user_b)}",
            )
        )
    return tidbits


# -- cover streaks ------------------------------------------------------------


def _cover_streak_tidbits(season: _Season) -> list[dict[str, Any]]:
    """Each team's active run of covers or non-covers, through its latest
    FINAL game with a spread. A push ends a streak either way."""
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

    active: dict[str, list[tuple[int, dict[str, Any]]]] = {"cover": [], "miss": []}
    for team_id, results in results_by_team.items():
        last = results[-1]
        if last == "push":
            continue
        length = 0
        for result in reversed(results):
            if result != last:
                break
            length += 1
        active[last].append((length, teams[team_id]))

    tidbits = []
    for kind, verb in (("cover", "covered"), ("miss", "failed to cover")):
        if not active[kind]:
            continue
        best = max(length for length, _team_dict in active[kind])
        if best < _MIN_COVER_STREAK:
            continue
        leaders = sorted(
            (team for length, team in active[kind] if length == best), key=lambda t: t["abbr"]
        )
        abbrs = _names_text([t["abbr"] for t in leaders])
        headline = f"{abbrs} {'have' if len(leaders) > 1 else 'has'} {verb} {best} straight."
        tidbits.append(
            _tidbit(
                "cover_streak",
                "teams",
                "season",
                1.0 + best / 3,
                headline,
                {"streak_type": kind, "length": best, "teams": leaders},
                sample_size=best,
                key=kind,
            )
        )
    return tidbits


# -- biggest mover ------------------------------------------------------------


def _biggest_mover_tidbits(season: _Season) -> list[dict[str, Any]]:
    """Cumulative-score rank after last week vs after this week, ranked the
    same way as the leaderboard (standard_rank, ties share a place)."""
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

    tidbits = []
    climb = max(moves, key=lambda m: m["change"], default=None)
    drop = min(moves, key=lambda m: m["change"], default=None)
    if climb and climb["change"] >= _MIN_RANK_MOVE:
        climbers = sorted(m["name"] for m in moves if m["change"] == climb["change"])
        tidbits.append(
            _tidbit(
                "biggest_mover",
                "users",
                "week",
                1.5 + climb["change"] / 10,
                f"{_names_text(climbers)} jumped {climb['change']} spots"
                + (f" to {_ordinal(climb['rank_after'])}." if len(climbers) == 1 else "."),
                {"direction": "up", "moves": [m for m in moves if m["change"] == climb["change"]]},
                sample_size=len(moves),
                key="up",
            )
        )
    if drop and -drop["change"] >= _MIN_RANK_MOVE:
        droppers = sorted(m["name"] for m in moves if m["change"] == drop["change"])
        tidbits.append(
            _tidbit(
                "biggest_mover",
                "users",
                "week",
                1.3 + -drop["change"] / 10,
                f"{_names_text(droppers)} slid {-drop['change']} spots"
                + (f" to {_ordinal(drop['rank_after'])}." if len(droppers) == 1 else "."),
                {"direction": "down", "moves": [m for m in moves if m["change"] == drop["change"]]},
                sample_size=len(moves),
                key="down",
            )
        )
    return tidbits


# -- upset of the week --------------------------------------------------------


def _upset_tidbits(season: _Season) -> list[dict[str, Any]]:
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
    believers = sorted(p["name"] for p in picks if _pick_side(game, p) == winner)
    headline = f"Upset of the week: {dog['abbr']} (+{points:g}) beat {favorite['abbr']} outright."
    if picks:
        headline += (
            f" {len(believers)} of the {len(picks)} who picked that game had them"
            + (f": {_names_text(believers)}." if 0 < len(believers) <= 3 else ".")
        )
    return [
        _tidbit(
            "upset_of_week",
            "chaos",
            "week",
            1.5 + points / 7,
            headline,
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
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    if not _stands_out(successes, n, min_n):
        return []
    z = _z(successes, n)
    return [
        _tidbit(
            kind,
            category,
            "season",
            min(_STANDOUT_MAX_SCORE, abs(z)),
            headline,
            {**data, "successes": successes, "n": n, "pct": _pct(successes, n), "z": round(z, 2)},
            sample_size=n,
            key=key,
        )
    ]


def _game_split_tidbits(season: _Season) -> list[dict[str, Any]]:
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

    tidbits = []
    leader = "Home" if home_covers * 2 >= home_n else "Road"
    lead = home_covers if leader == "Home" else home_n - home_covers
    tidbits += _standout(
        "home_road_covers",
        "league",
        "splits",
        home_covers,
        home_n,
        _STANDOUT_MIN_GAMES,
        f"{leader} teams are {_record_text(lead, home_n - lead)} against the spread this season.",
        {"side": "home"},
    )
    leader = "Favorites" if fav_covers * 2 >= fav_n else "Underdogs"
    lead = fav_covers if leader == "Favorites" else fav_n - fav_covers
    tidbits += _standout(
        "favorite_covers",
        "league",
        "splits",
        fav_covers,
        fav_n,
        _STANDOUT_MIN_GAMES,
        f"{leader} are {_record_text(lead, fav_n - lead)} against the spread this season.",
        {"side": "favorite"},
    )
    tidbits += _standout(
        "home_underdog_covers",
        "league",
        "splits",
        home_dog_covers,
        home_dog_n,
        _STANDOUT_MIN_GAMES,
        f"Home underdogs are {_record_text(home_dog_covers, home_dog_n - home_dog_covers)}"
        " against the spread this season.",
        {"side": "home_underdog"},
    )
    tidbits += _standout(
        "division_underdog_covers",
        "league",
        "splits",
        div_dog_covers,
        div_n,
        _STANDOUT_MIN_GAMES,
        f"Underdogs are {_record_text(div_dog_covers, div_n - div_dog_covers)}"
        " against the spread in division games.",
        {"side": "division_underdog"},
    )
    return tidbits


def _pool_split_tidbits(season: _Season) -> list[dict[str, Any]]:
    """The pool's own ATS record split by what kind of pick it was:
    favorite/underdog, home/road, kickoff slot, division game. Graded by
    ats_side() (same as season:trends), pushes left out. Also flags a
    lopsided favorite/underdog lean on its own."""
    counters: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])  # [correct, n]
    fav_lean = [0, 0]  # [favorite picks, picks with a favorite] - share only, not a tidbit

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

    tidbits = []
    for bucket, (correct, n) in sorted(counters.items()):
        tidbits += _standout(
            "pool_split",
            bucket,
            "splits",
            correct,
            n,
            _STANDOUT_MIN_POOL_PICKS,
            f"The pool is {_record_text(correct, n - correct)} ({_pct_text(correct, n)})"
            f" {labels[bucket]} this season.",
            {"bucket": bucket, "label": labels[bucket]},
        )

    # the lean itself isn't news (a pool always leans favorite), so it rides
    # along on the favorite/underdog splits rather than being its own tidbit
    for tidbit in tidbits:
        if tidbit["data"]["bucket"] in ("fav:favorite", "fav:underdog"):
            tidbit["data"]["favorite_pick_share"] = _pct(*fav_lean)
    return tidbits


def _team_split_tidbits(season: _Season) -> list[dict[str, Any]]:
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
    tidbits = []
    for (team_id, bucket), (covers, n) in sorted(counters.items()):
        team = teams[team_id]
        tidbits += _standout(
            "team_split",
            f"{team['abbr']}:{bucket}",
            "teams",
            covers,
            n,
            _STANDOUT_MIN_TEAM_GAMES,
            f"{team['abbr']} are {_record_text(covers, n - covers)} against the spread"
            f" {labels[bucket]} this season.",
            {"team": team, "bucket": bucket, "label": labels[bucket]},
        )
    return tidbits


# -- writer -------------------------------------------------------------------


def compute_week_tidbits(d1: D1Client, week_number: int) -> dict[str, Any] | None:
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

    tidbits = [
        *_pool_accuracy_tidbits(season, pool_series, week_complete),
        *_spread_mattered_tidbits(season),
        *_consensus_tidbits(season),
        *_chaos_tidbits(season, chaos_series),
        *_twins_and_oppos_tidbits(season),
        *_cover_streak_tidbits(season),
        *_biggest_mover_tidbits(season),
        *_upset_tidbits(season),
        *_game_split_tidbits(season),
        *_pool_split_tidbits(season),
        *_team_split_tidbits(season),
    ]
    tidbits.sort(key=lambda t: -t["score"])

    return {
        "season": SEASON,
        "week": week_number,
        "updated_at": now_iso(),
        "week_complete": week_complete,
        "games_final": games_final,
        "games_total": len(week_games),
        "tidbits": tidbits,
        "series": {
            "pool_accuracy": pool_series,
            "chaos": chaos_series,
        },
    }


def write_week_tidbits(week_number: int) -> None:
    """Write week:{season}:{weekNN}:tidbits from compute_week_tidbits()."""
    d1 = D1Client(**get_d1_config())
    payload = compute_week_tidbits(d1, week_number)
    if payload is None:
        logger.warning(
            "No games found for season %s week %s - not writing tidbits key",
            SEASON,
            week_number,
        )
        return

    kv = KVClient(**get_kv_config())
    kv.write(f"week:{SEASON}:{week_number:02d}:tidbits", payload)
    logger.info(
        "Wrote week:%s:%02d:tidbits (%d tidbits) to KV",
        SEASON,
        week_number,
        len(payload["tidbits"]),
    )


def write_current_week_tidbits() -> None:
    """Resolve weeks.is_current and write that week's tidbits key."""
    d1 = D1Client(**get_d1_config())
    current_week = resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s - not writing tidbits key",
            SEASON,
        )
        return

    write_week_tidbits(current_week)
