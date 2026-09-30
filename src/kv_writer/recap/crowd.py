"""What the pool picked as a group: crowd record, fade the crowd, popular picks, twins and oppos."""

import math
from collections import defaultdict
from itertools import combinations
from typing import Any

from src.game_rules import (
    ats_side,
    pick_side,
)
from src.kv_writer.recap.common import (
    PICKS_PER_WEEK,
    Season,
    game_line,
    make_item,
    names_text,
    people,
    person,
    record_text,
    side_team,
    z_score,
)

# share of that week's pool on the crowd's side for it to count as a popular
# pick, rounded up (10 of 33) - a share rather than a flat count so it keeps
# its meaning if the pool size changes. Weeks 1-3 of 2026 had a clear gap
# right there: 4-5 teams a week at 10+, then 9 and below.
_POPULAR_POOL_SHARE = 0.3
_POPULAR_MIN_GAMES = 3  # season record needs a few games before it means anything


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
        home = sum(1 for p in picks if pick_side(game, p["picked_team_id"]) == "home")
        away = sum(1 for p in picks if pick_side(game, p["picked_team_id"]) == "away")
        if home == away:
            continue
        crowd_side = "home" if home > away else "away"
        results.append(
            {
                **game_line(game),
                "crowd_team": side_team(game, crowd_side),
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


def crowd_items(season: Season) -> list[dict[str, Any]]:
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
            f"The crowd's side is {record_text(wins, losses, pushes)}"
            " against the spread this season"
        )
        if wins < losses:
            headline += f" - fading it would be {record_text(losses, wins, pushes)}."
        else:
            headline += "."
        items.append(
            make_item(
                "crowd_record",
                "crowd",
                "season",
                1.0 + abs(z_score(wins, wins + losses)),
                headline,
                f"Crowd's side is {record_text(wins, losses, pushes)} ATS this season",
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
            make_item(
                "crowd_record",
                "crowd",
                "week",
                0.8 + abs(z_score(wins, wins + losses)) / 2,
                f"The crowd's side went {record_text(wins, losses, pushes)}"
                " against the spread this week.",
                f"Crowd's side went {record_text(wins, losses, pushes)} ATS this week",
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
            make_item(
                "popular_picks",
                "crowd",
                "week",
                1.0 + abs(z_score(wins, wins + losses)) / 2,
                f"Teams {min_picks}+ of you picked went"
                f" {record_text(wins, losses, pushes)} this week. Most picked:"
                f" {top['crowd_team']['abbr']} ({top['pick_count']} of you) {outcome}.",
                f"Popular picks went {record_text(wins, losses, pushes)}; most picked"
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
        # the count only reads right when every week had the same pool size -
        # that shared count, not this week's, which is unknown until this
        # week's weekly_performance rows exist
        counts = set(min_picks_by_week.values())
        who = (
            f"{counts.pop()}+ of you"
            if len(counts) == 1
            else f"{round(_POPULAR_POOL_SHARE * 100)}%+ of the pool"
        )
        items.append(
            make_item(
                "popular_picks",
                "crowd",
                "season",
                1.0 + abs(z_score(wins, wins + losses)),
                f"Teams {who} picked are"
                f" {record_text(wins, losses, pushes)} against the spread this season.",
                f"Popular picks are {record_text(wins, losses, pushes)} ATS this season",
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


# -- twins and oppos ----------------------------------------------------------


def twins_and_oppos_items(season: Season) -> list[dict[str, Any]]:
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
        if len(picks) == PICKS_PER_WEEK
    }

    def record(picks: list[dict[str, Any]]) -> dict[str, Any]:
        graded = [p for p in picks if p["is_correct"] is not None]
        correct = sum(1 for p in graded if p["is_correct"])
        return {"correct": correct, "graded": len(graded)}

    def picks_json(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "game_id": p["game_id"],
                "team": side_team(
                    games[p["game_id"]],
                    pick_side(games[p["game_id"]], p["picked_team_id"]),
                ),
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
        users = people([full[u][0] for u in user_ids])
        names = [u["name"] for u in users]
        first = full[user_ids[0]]
        items.append(
            make_item(
                "twins",
                "users",
                "week",
                2.0 + 0.2 * (len(user_ids) - 2),
                f"Twins: {names_text(names, limit=4)} made the exact same 5 picks.",
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
        if rec_a["graded"] == PICKS_PER_WEEK:
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
            make_item(
                "oppos",
                "users",
                "week",
                2.5,
                headline,
                f"Opposites: {name_a} vs {name_b}",
                {
                    "users": [
                        {
                            **person(picks_a[0]),
                            "picks": picks_json(picks_a),
                            "record": rec_a,
                        },
                        {
                            **person(picks_b[0]),
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
