"""Team cover streaks, leaderboard movers and the upset of the week."""

from collections import defaultdict
from typing import Any

from src.game_rules import (
    ats_side,
    favorite_side,
    other_side,
    pick_side,
    standard_rank,
    winner_side,
)
from src.kv_writer.recap.common import (
    Season,
    game_line,
    make_item,
    names_text,
    ordinal,
    people,
    side_team,
)

_MIN_COVER_STREAK = 3
MIN_RANK_MOVE = 3  # spots climbed/dropped before a mover is worth a recap item


def active_cover_streaks(season: Season) -> list[dict[str, Any]]:
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
            team = side_team(game, team_side)
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


def cover_streak_items(streaks: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        abbrs = names_text([t["abbr"] for t in leaders])
        headline = (
            f"{abbrs} {'have' if len(leaders) > 1 else 'has'} {verb} {best} straight."
        )
        items.append(
            make_item(
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


def rank_moves(season: Season) -> list[dict[str, Any]]:
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


def biggest_mover_items(moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Just the biggest climb and drop (ties listed together) - every move
    of MIN_RANK_MOVE+ is in the key's top-level `movers` instead."""
    items = []
    climb = max(moves, key=lambda m: m["change"], default=None)
    drop = min(moves, key=lambda m: m["change"], default=None)
    if climb and climb["change"] >= MIN_RANK_MOVE:
        climbers = sorted(m["name"] for m in moves if m["change"] == climb["change"])
        items.append(
            make_item(
                "biggest_mover",
                "users",
                "week",
                1.5 + climb["change"] / 10,
                f"{names_text(climbers)} jumped {climb['change']} spots"
                + (
                    f" to {ordinal(climb['rank_after'])}."
                    if len(climbers) == 1
                    else "."
                ),
                f"{names_text(climbers, 2)} up {climb['change']} spots"
                + (f" to {ordinal(climb['rank_after'])}" if len(climbers) == 1 else ""),
                {
                    "direction": "up",
                    "moves": [m for m in moves if m["change"] == climb["change"]],
                },
                sample_size=len(moves),
                key="up",
            )
        )
    if drop and -drop["change"] >= MIN_RANK_MOVE:
        droppers = sorted(m["name"] for m in moves if m["change"] == drop["change"])
        items.append(
            make_item(
                "biggest_mover",
                "users",
                "week",
                1.3 + -drop["change"] / 10,
                f"{names_text(droppers)} slid {-drop['change']} spots"
                + (
                    f" to {ordinal(drop['rank_after'])}." if len(droppers) == 1 else "."
                ),
                f"{names_text(droppers, 2)} down {-drop['change']} spots"
                + (f" to {ordinal(drop['rank_after'])}" if len(droppers) == 1 else ""),
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


def upset_items(season: Season) -> list[dict[str, Any]]:
    """Biggest-spread underdog to win outright this week, and who had it."""
    upsets = []
    for game in season.week_games(season.week):
        favorite, winner = favorite_side(game), winner_side(game)
        if favorite is None or winner is None or winner == favorite:
            continue
        upsets.append((abs(game["cbs_spread"]), game, winner))
    if not upsets:
        return []
    points, game, winner = max(upsets, key=lambda u: u[0])
    dog, favorite = side_team(game, winner), side_team(game, other_side(winner))
    picks = season.picks_by_game.get(game["game_id"], [])
    believers = people(
        [p for p in picks if pick_side(game, p["picked_team_id"]) == winner]
    )
    believer_names = [b["name"] for b in believers]
    headline = f"Upset of the week: {dog['abbr']} (+{points:g}) beat {favorite['abbr']} outright."
    if picks:
        headline += (
            f" {len(believers)} of the {len(picks)} who picked that game had them"
            + (f": {names_text(believer_names)}." if 0 < len(believers) <= 3 else ".")
        )
    return [
        make_item(
            "upset_of_week",
            "chaos",
            "week",
            1.5 + points / 7,
            headline,
            f"Upset: {dog['abbr']} (+{points:g}) beat {favorite['abbr']} outright",
            {
                **game_line(game),
                "underdog": dog,
                "favorite": favorite,
                "points": points,
                "believers": believers,
                "pool_picks": len(picks),
            },
            sample_size=1,
        )
    ]
