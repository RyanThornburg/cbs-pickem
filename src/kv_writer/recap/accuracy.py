"""Pool accuracy (plus perfect and winless weeks) and "the spread mattered" recap items."""

from collections import defaultdict
from typing import Any

from config.config import PICKS_PER_WEEK
from src.game_rules import (
    ats_side,
    favorite_side,
    pick_side,
    winner_side,
)
from src.kv_writer.recap.common import (
    Season,
    game_line,
    make_item,
    names_text,
    pct,
    pct_text,
    people,
    side_team,
)


def pool_accuracy_series(season: Season) -> list[dict[str, Any]]:
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
        if len(picks) == PICKS_PER_WEEK and len(graded) == PICKS_PER_WEEK:
            if correct == PICKS_PER_WEEK:
                row["perfect"].append(picks[0])
            elif correct == 0:
                row["winless"].append(picks[0])

    series = []
    for week_number in sorted(weeks):
        row = weeks[week_number]
        row["accuracy"] = pct(row["correct"], row["graded"])
        row["perfect"] = people(row["perfect"])
        row["winless"] = people(row["winless"])
        series.append(row)
    return series


def pool_accuracy_items(
    season: Season, series: list[dict[str, Any]], week_complete: bool
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
        f"The pool hit {pct_text(current['correct'], current['graded'])} this week"
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
        f"Pool hit {pct_text(current['correct'], current['graded'])} this week{so_far}"
    )
    if rank_note:
        headline += f", its {rank_note} week of the season"
        short += f", {rank_note} of the season"
    elif prior_graded:
        headline += f", vs {pct_text(prior_correct, prior_graded)} before this week"
    items.append(
        make_item(
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
                "prior_accuracy": pct(prior_correct, prior_graded),
                "season_rank_note": rank_note,
            },
            sample_size=current["graded"],
        )
    )

    if current["perfect"]:
        count = len(current["perfect"])
        names = [p["name"] for p in current["perfect"]]
        headline = (
            f"{names_text(names)} went 5-0 this week."
            if count <= 3
            else f"{count} perfect 5-0 weeks: {names_text(names)}."
        )
        short = (
            f"5-0: {names_text(names, 2)}"
            if count <= 2
            else f"{count} perfect 5-0 weeks"
        )
        items.append(
            make_item(
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
            make_item(
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
            f"Rough week: {names_text(names)} went 0-5."
            if count <= 3
            else f"{count} people went 0-5 this week: {names_text(names)}."
        )
        short = (
            f"0-5: {names_text(names, 2)}" if count <= 2 else f"{count} people went 0-5"
        )
        items.append(
            make_item(
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
        winner = winner_side(game)
        side = ats_side(game)
        if winner is None or side is None or favorite_side(game) is None:
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
                if pick_side(game, p["picked_team_id"]) == winner
            ]
            winner_lost_picks += len(burned)
            flipped_games.append(
                {
                    **game_line(game),
                    "winner": side_team(game, winner),
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


def spread_mattered_items(season: Season) -> list[dict[str, Any]]:
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
                f" {pct_text(week['winner_covered'], week['games'])} of the time."
            )
            if week["winner_lost_picks"]:
                headline += (
                    f" {week['winner_lost_picks']} picks had the winner and still lost."
                )
            short = f"Spread flipped {week['spread_flipped']} of {week['games']} games this week"
            score = 1.5 + min(1.0, week["spread_flipped"] / max(1, week["games"]) * 2)
        items.append(
            make_item(
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
            f" {pct_text(season_counts['winner_covered'], season_counts['games'])} of the time"
            f" ({season_counts['winner_covered']} of {season_counts['games']} games)."
        )
        season_data = {k: v for k, v in season_counts.items() if k != "flipped_games"}
        items.append(
            make_item(
                "spread_mattered",
                "spread",
                "season",
                1.2,
                headline,
                "Winners cover"
                f" {pct_text(season_counts['winner_covered'], season_counts['games'])}"
                " of the time this season",
                season_data,
                sample_size=season_counts["games"],
                key="season",
            )
        )
    return items
