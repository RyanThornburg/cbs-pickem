"""Stand-out splits - home/road, favorite/underdog, kickoff slot, division games - that only
show up once they clear _stands_out()."""

from collections import defaultdict
from typing import Any

from src.game_rules import (
    ats_side,
    favorite_side,
    pick_side,
)
from src.kv_writer.recap.common import (
    Season,
    is_division_game,
    kickoff_slot,
    make_item,
    pct,
    pct_text,
    record_text,
    side_team,
    z_score,
)

# hand-picked judgment calls, same spirit as trends.py's thresholds
_STANDOUT_MIN_Z = 1.5  # distance from a coin flip, in standard deviations
# z grows with sample size, so without a cap a big season-long split would
# outrank every weekly recap item by midseason
_STANDOUT_MAX_SCORE = 3.0
_STANDOUT_MIN_POOL_PICKS = 20  # floor for a pool-wide pick split
_STANDOUT_MIN_GAMES = 10  # floor for a league-wide game split
_STANDOUT_MIN_TEAM_GAMES = 4  # floor for one team's split
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


def _stands_out(successes: int, n: int, min_n: int) -> bool:
    return n >= min_n and abs(z_score(successes, n)) >= _STANDOUT_MIN_Z


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
    z = z_score(successes, n)
    return [
        make_item(
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
                "pct": pct(successes, n),
                "z": round(z, 2),
            },
            sample_size=n,
            key=key,
        )
    ]


def game_split_items(season: Season) -> list[dict[str, Any]]:
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
        favorite = favorite_side(game)
        if favorite is None:
            continue
        fav_n += 1
        fav_covers += side == favorite
        if favorite == "away" and not game["neutral_site"]:
            home_dog_n += 1
            home_dog_covers += side == "home"
        if is_division_game(game):
            div_n += 1
            div_dog_covers += side != favorite

    # category "league": league-wide cover trends, independent of the pool's
    # own picks (those are pool_split, category "splits"). The ":league" id
    # suffix predates the category and is kept so ids stay stable.
    items = []
    leader = "Home" if home_covers * 2 >= home_n else "Road"
    lead = home_covers if leader == "Home" else home_n - home_covers
    record = record_text(lead, home_n - lead)
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
    record = record_text(lead, fav_n - lead)
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
    record = record_text(home_dog_covers, home_dog_n - home_dog_covers)
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
    record = record_text(div_dog_covers, div_n - div_dog_covers)
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


def pool_split_items(season: Season) -> list[dict[str, Any]]:
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
        favorite = favorite_side(game)
        slot = kickoff_slot(game)
        for pick in season.picks_by_game.get(game["game_id"], []):
            picked = pick_side(game, pick["picked_team_id"])
            if picked is None:
                continue
            correct = picked == side
            buckets = [f"slot:{slot}"]
            if not game["neutral_site"]:
                buckets.append(f"side:{picked}")
            if favorite is not None:
                is_fav = picked == favorite
                buckets.append("fav:favorite" if is_fav else "fav:underdog")
                fav_lean[0] += is_fav
                fav_lean[1] += 1
            if is_division_game(game):
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
            f"The pool is {record_text(correct, n - correct)} ({pct_text(correct, n)})"
            f" {labels[bucket]} this season.",
            f"Pool is {record_text(correct, n - correct)} {labels[bucket]}",
            {"bucket": bucket, "label": labels[bucket]},
        )

    # the lean itself isn't news (a pool always leans favorite), so it rides
    # along on the favorite/underdog splits rather than being its own recap item
    for item in items:
        if item["data"]["bucket"] in ("fav:favorite", "fav:underdog"):
            item["data"]["favorite_pick_share"] = pct(*fav_lean)
    return items


def team_split_items(season: Season) -> list[dict[str, Any]]:
    """One team's ATS record in primetime, in division games, at home and
    on the road - only the ones that clear _STANDOUT_MIN_TEAM_GAMES and the
    z bar, which a team's small samples rarely do before midseason."""
    counters: defaultdict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
    teams: dict[int, dict[str, Any]] = {}
    for game in season.games:
        side = ats_side(game)
        if side not in ("home", "away"):
            continue
        primetime = kickoff_slot(game) in _PRIMETIME_SLOTS
        division = is_division_game(game)
        for team_side in ("home", "away"):
            team = side_team(game, team_side)
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
            f"{team['abbr']} are {record_text(covers, n - covers)} against the spread"
            f" {labels[bucket]} this season.",
            f"{team['abbr']} are {record_text(covers, n - covers)} ATS {labels[bucket]}",
            {"team": team, "bucket": bucket, "label": labels[bucket]},
        )
    return items
