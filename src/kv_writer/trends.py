"""week:{season}:{weekNN}:trends + season:{season}:trends - see
src/CLAUDE.md's KV writer section."""

import logging
from collections import Counter, defaultdict
from typing import Any

from config.config import SEASON, get_d1_config, get_kv_config
from db.d1_client import D1Client
from db.kv_client import KVClient
from src.kv_writer.odds import open_close_consensus_by_game
from src.kv_writer.shared import (
    GAMES_SQL,
    PICKS_SQL,
    game_team_dicts,
    now_iso,
    resolve_current_week,
    split_home_away,
)

logger = logging.getLogger(__name__)

# trends thresholds - hand-picked judgment calls, not derived from data
_ALL_ALONE_MIN_OPPOSING = (
    3  # how big the other side must be for a solo pick to mean anything
)
_ONE_SIDED_MIN_PICKS = (
    3  # floor so an early, barely-revealed game can't look "lopsided"
)
_ONE_SIDED_THRESHOLD = 0.8  # consensus share needed to call a game one-sided
_LINE_MOVER_MIN_POINTS = 1.0  # spread/total movement below this isn't worth surfacing

_SEASON_GAMES_SQL = """
SELECT g.game_id, w.week_number, g.status, g.home_score, g.away_score, g.cbs_spread,
       ht.team_id AS home_id, ht.abbreviation AS home_abbr, ht.nick_name AS home_name,
       at.team_id AS away_id, at.abbreviation AS away_abbr, at.nick_name AS away_name
FROM games g
JOIN teams ht ON ht.team_id = g.home_team_id
JOIN teams at ON at.team_id = g.away_team_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ?
ORDER BY g.game_time
"""

_SEASON_PICKS_SQL = """
SELECT up.game_id, up.user_id, u.name, up.picked_team_id
FROM user_picks up
JOIN users u ON u.user_id = up.user_id
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ?
"""

_ALL_TEAMS_SQL = "SELECT team_id, abbreviation AS abbr, nick_name AS name FROM teams"

# spread-size buckets for _spread_bucket_trends() - hand-picked cutoffs
# (roughly: a coin-flip game, a field-goal game, a touchdown-ish game, a
# blowout line), same "judgment call, not derived from data" spirit as the
# thresholds above. Upper bound of each bucket is exclusive.
_SPREAD_BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("0-3", 0.0, 3.0),
    ("3-7", 3.0, 7.0),
    ("7-14", 7.0, 14.0),
    ("14+", 14.0, None),
)


def _one_sided_entry(
    game: dict[str, Any],
    home_picks: list[dict[str, Any]],
    away_picks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    total = len(home_picks) + len(away_picks)
    if total < _ONE_SIDED_MIN_PICKS:
        return None

    home_pct = len(home_picks) / total
    away_pct = len(away_picks) / total
    if home_pct >= _ONE_SIDED_THRESHOLD:
        side, pct = "home", home_pct
    elif away_pct >= _ONE_SIDED_THRESHOLD:
        side, pct = "away", away_pct
    else:
        return None

    home_team, away_team = game_team_dicts(game)
    return {
        "game_id": game["game_id"],
        "home_team": home_team,
        "away_team": away_team,
        "home_picks": len(home_picks),
        "away_picks": len(away_picks),
        "consensus_side": side,
        "consensus_pct": round(pct, 3),
    }


def _all_alone_entries(
    game: dict[str, Any],
    home_picks: list[dict[str, Any]],
    away_picks: list[dict[str, Any]],
    week_number: int | None = None,
) -> list[dict[str, Any]]:
    """One user alone on a side while the other side has at least _ALL_ALONE_MIN_OPPOSING"""
    entries: list[dict[str, Any]] = []
    for picks, opposing, team_id, abbr in (
        (home_picks, away_picks, game["home_id"], game["home_abbr"]),
        (away_picks, home_picks, game["away_id"], game["away_abbr"]),
    ):
        if len(picks) != 1 or len(opposing) < _ALL_ALONE_MIN_OPPOSING:
            continue
        entry = {
            "game_id": game["game_id"],
            "user_id": picks[0]["user_id"],
            "name": picks[0]["name"],
            "picked_team_id": team_id,
            "abbr": abbr,
            "opposing_count": len(opposing),
        }
        if week_number is not None:
            entry["week_number"] = week_number
        entries.append(entry)
    return entries


def _movers_from_consensus(
    consensus_by_game: dict[int, dict[str, Any]], game_lookup: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Games whose consensus line moved open->close by at least
    _LINE_MOVER_MIN_POINTS, sorted by movement magnitude - anything
    smaller is just normal noise, not a real move."""
    movers: list[dict[str, Any]] = []
    for game_id, consensus in consensus_by_game.items():
        movement = consensus["close"] - consensus["open"]
        if abs(movement) < _LINE_MOVER_MIN_POINTS:
            continue
        home_team, away_team = game_team_dicts(game_lookup[game_id])
        movers.append(
            {
                "game_id": game_id,
                "home_team": home_team,
                "away_team": away_team,
                "open": consensus["open"],
                "close": consensus["close"],
                "movement": round(movement, 1),
                "book_count": consensus["book_count"],
            }
        )
    movers.sort(key=lambda e: -abs(e["movement"]))
    return movers


def write_week_trends(week_number: int) -> None:
    """Write week:{season}:{weekNN}:trends - pick popularity/cold teams,
    lopsided games, all-alone picks (one user alone on a side against a
    real crowd on the other), and the week's biggest spread movers.
    Popularity/cold-team splits only count a game once its picks are
    revealed (a game with zero total picks yet is unlocked, not actually
    cold - same ambiguity write_week_games() already documents)."""
    d1 = D1Client(**get_d1_config())

    games = d1.query(GAMES_SQL, [SEASON, week_number]).results
    if not games:
        logger.warning(
            "No games found for season %s week %s - not writing trends key",
            SEASON,
            week_number,
        )
        return

    picks_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in d1.query(PICKS_SQL, [SEASON, week_number]).results:
        picks_by_game[pick["game_id"]].append(pick)

    pick_popularity: list[dict[str, Any]] = []
    cold_teams: list[dict[str, Any]] = []
    one_sided_games: list[dict[str, Any]] = []
    loners: list[dict[str, Any]] = []

    for game in games:
        home_picks, away_picks = split_home_away(
            game, picks_by_game.get(game["game_id"], [])
        )
        total = len(home_picks) + len(away_picks)
        if total == 0:
            continue  # not revealed/locked yet - can't call this "cold"

        home_team, away_team = game_team_dicts(game)
        for team, picks, opponent_count in (
            (home_team, home_picks, len(away_picks)),
            (away_team, away_picks, len(home_picks)),
        ):
            if picks:
                pick_popularity.append(
                    {
                        **team,
                        "game_id": game["game_id"],
                        "pick_count": len(picks),
                        "opponent_pick_count": opponent_count,
                        "pct_of_game_pickers": round(len(picks) / total, 3),
                    }
                )
            else:
                cold_teams.append({**team, "game_id": game["game_id"]})

        one_sided = _one_sided_entry(game, home_picks, away_picks)
        if one_sided:
            one_sided_games.append(one_sided)

        loners.extend(_all_alone_entries(game, home_picks, away_picks))

    pick_popularity.sort(key=lambda e: -e["pick_count"])
    one_sided_games.sort(key=lambda e: -e["consensus_pct"])

    game_lookup = {game["game_id"]: game for game in games}
    spread_movers = _movers_from_consensus(
        open_close_consensus_by_game(d1, week_number, market="spread"), game_lookup
    )
    total_movers = _movers_from_consensus(
        open_close_consensus_by_game(d1, week_number, market="total"), game_lookup
    )

    kv = KVClient(**get_kv_config())
    kv.write(
        f"week:{SEASON}:{week_number:02d}:trends",
        {
            "week": week_number,
            "updated_at": now_iso(),
            "pick_popularity": pick_popularity,
            "cold_teams": cold_teams,
            "one_sided_games": one_sided_games,
            "all_alone": loners,
            "spread_movers": spread_movers,
            "total_movers": total_movers,
        },
    )
    logger.info(
        "Wrote week:%s:%02d:trends (%d popular, %d cold, %d one-sided, "
        "%d all alone, %d spread movers, %d total movers) to KV",
        SEASON,
        week_number,
        len(pick_popularity),
        len(cold_teams),
        len(one_sided_games),
        len(loners),
        len(spread_movers),
        len(total_movers),
    )


def write_current_week_trends() -> None:
    """Resolve weeks.is_current and write that week's trends key."""
    d1 = D1Client(**get_d1_config())
    current_week = resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s - not writing trends key",
            SEASON,
        )
        return

    write_week_trends(current_week)


def _ats_side(game: dict[str, Any]) -> str | None:
    """Which side covered game['cbs_spread'] - None if the game isn't
    FINAL yet or is missing a spread/score. cbs_spread is the home team's
    line (negative = home favored); home covers when its actual margin
    beats that line."""
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


def _spread_bucket(abs_spread: float) -> str:
    for label, low, high in _SPREAD_BUCKETS:
        if abs_spread >= low and (high is None or abs_spread < high):
            return label
    return _SPREAD_BUCKETS[-1][0]


def _pick_outcomes(
    game: dict[str, Any], picked_team_id: int
) -> tuple[bool | None, bool | None]:
    """(straight_up_correct, ats_correct) for one pick on a FINAL game -
    None for either half if that outcome isn't decided (not FINAL, missing
    scores, or an actual tie/push), rather than counting it as wrong."""
    if (
        game["status"] != "FINAL"
        or game["home_score"] is None
        or game["away_score"] is None
    ):
        return None, None

    if game["home_score"] == game["away_score"]:
        straight_up_correct = None  # an actual tie - nobody "won"
    else:
        winner_id = (
            game["home_id"] if game["home_score"] > game["away_score"] else game["away_id"]
        )
        straight_up_correct = picked_team_id == winner_id

    side = _ats_side(game)
    if side is None or side == "push":
        ats_correct = None
    else:
        covering_team_id = game["home_id"] if side == "home" else game["away_id"]
        ats_correct = picked_team_id == covering_team_id

    return straight_up_correct, ats_correct


def _new_accuracy_counter() -> dict[str, int]:
    return {"straight_up_correct": 0, "straight_up_total": 0, "ats_correct": 0, "ats_total": 0}


def _record_pick_outcome(
    counter: dict[str, int], straight_up_correct: bool | None, ats_correct: bool | None
) -> None:
    if straight_up_correct is not None:
        counter["straight_up_total"] += 1
        counter["straight_up_correct"] += int(straight_up_correct)
    if ats_correct is not None:
        counter["ats_total"] += 1
        counter["ats_correct"] += int(ats_correct)


def _finalize_accuracy(counter: dict[str, int]) -> dict[str, Any]:
    return {
        "straight_up_pick_count": counter["straight_up_total"],
        "straight_up_accuracy": round(
            counter["straight_up_correct"] / counter["straight_up_total"], 3
        )
        if counter["straight_up_total"]
        else None,
        "ats_pick_count": counter["ats_total"],
        "ats_accuracy": round(counter["ats_correct"] / counter["ats_total"], 3)
        if counter["ats_total"]
        else None,
    }


def _spread_bucket_trends(
    games: list[dict[str, Any]], picks_by_game: dict[int, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Buckets every pick by the size of that game's cbs_spread and compares
    straight-up pick accuracy (picked the actual winner) against ATS pick
    accuracy (picked the side that covered cbs_spread) - answers "does
    picking the spread differ from picking to win, and at what spread size."
    Split by which side (home/away) the pick was on, and rolled up per team
    within each bucket, since a team's own cover tendency can diverge from
    the pool's accuracy picking it."""
    overall: defaultdict[str, dict[str, int]] = defaultdict(_new_accuracy_counter)
    by_side: defaultdict[tuple[str, str], dict[str, int]] = defaultdict(
        _new_accuracy_counter
    )
    by_team: defaultdict[tuple[str, int], dict[str, int]] = defaultdict(
        _new_accuracy_counter
    )
    team_lookup: dict[int, dict[str, Any]] = {}

    for game in games:
        if game["cbs_spread"] is None:
            continue
        bucket_label = _spread_bucket(abs(game["cbs_spread"]))
        home_team, away_team = game_team_dicts(game)
        team_lookup[game["home_id"]] = home_team
        team_lookup[game["away_id"]] = away_team

        for pick in picks_by_game.get(game["game_id"], []):
            straight_up_correct, ats_correct = _pick_outcomes(
                game, pick["picked_team_id"]
            )
            _record_pick_outcome(overall[bucket_label], straight_up_correct, ats_correct)

            side = "home" if pick["picked_team_id"] == game["home_id"] else "away"
            _record_pick_outcome(
                by_side[(bucket_label, side)], straight_up_correct, ats_correct
            )
            _record_pick_outcome(
                by_team[(bucket_label, pick["picked_team_id"])],
                straight_up_correct,
                ats_correct,
            )

    by_bucket_json = [
        {
            "bucket": label,
            "overall": _finalize_accuracy(overall[label]),
            "home_picks": _finalize_accuracy(by_side[(label, "home")]),
            "away_picks": _finalize_accuracy(by_side[(label, "away")]),
        }
        for label, _low, _high in _SPREAD_BUCKETS
        if overall[label]["straight_up_total"] or overall[label]["ats_total"]
    ]

    by_team_json = [
        {**team_lookup[team_id], "bucket": label, **_finalize_accuracy(counter)}
        for (label, team_id), counter in by_team.items()
        if team_id in team_lookup
        and (counter["straight_up_total"] or counter["ats_total"])
    ]
    by_team_json.sort(key=lambda e: (e["bucket"], -(e["ats_accuracy"] or 0)))

    return {"by_bucket": by_bucket_json, "by_team": by_team_json}


def write_season_trends() -> None:
    """Write season:{season}:trends - season-long pick popularity, ATS
    cover record per team (from cbs_spread + final scores, independent of
    who actually picked them - works even for a team nobody in the pool
    ever picks), cold teams, every all-alone pick logged this season, and
    a spread-size breakdown (spread_analysis) comparing straight-up vs ATS
    pick accuracy by bucket/home-away/team - see _spread_bucket_trends()."""
    d1 = D1Client(**get_d1_config())

    games = d1.query(_SEASON_GAMES_SQL, [SEASON]).results
    if not games:
        logger.warning(
            "No games found for season %s - not writing season trends key", SEASON
        )
        return

    all_teams = {
        row["team_id"]: {"id": row["team_id"], "abbr": row["abbr"], "name": row["name"]}
        for row in d1.query(_ALL_TEAMS_SQL).results
    }

    picks_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    pick_counts: Counter[int] = Counter()
    for pick in d1.query(_SEASON_PICKS_SQL, [SEASON]).results:
        picks_by_game[pick["game_id"]].append(pick)
        pick_counts[pick["picked_team_id"]] += 1

    total_picks = sum(pick_counts.values())
    team_pick_totals = [
        {
            **all_teams[team_id],
            "total_picks": count,
            "pct_of_all_picks": round(count / total_picks, 3) if total_picks else 0.0,
        }
        for team_id, count in pick_counts.items()
        if team_id in all_teams
    ]
    team_pick_totals.sort(key=lambda e: -e["total_picks"])

    cold_teams_season = [
        team for team_id, team in all_teams.items() if pick_counts.get(team_id, 0) == 0
    ]

    covers: Counter[int] = Counter()
    pushes: Counter[int] = Counter()
    losses: Counter[int] = Counter()
    for game in games:
        side = _ats_side(game)
        if side is None:
            continue
        if side == "push":
            pushes[game["home_id"]] += 1
            pushes[game["away_id"]] += 1
        elif side == "home":
            covers[game["home_id"]] += 1
            losses[game["away_id"]] += 1
        else:
            covers[game["away_id"]] += 1
            losses[game["home_id"]] += 1

    team_ats_record = []
    for team_id, team in all_teams.items():
        c, p, l = covers.get(team_id, 0), pushes.get(team_id, 0), losses.get(team_id, 0)
        if c + p + l == 0:
            continue
        decided = c + l
        team_ats_record.append(
            {
                **team,
                "covers": c,
                "pushes": p,
                "losses": l,
                "cover_pct": round(c / decided, 3) if decided else None,
            }
        )
    team_ats_record.sort(key=lambda e: (-(e["cover_pct"] or 0), -e["covers"]))

    all_alone: list[dict[str, Any]] = []
    for game in games:
        home_picks, away_picks = split_home_away(
            game, picks_by_game.get(game["game_id"], [])
        )
        all_alone.extend(
            _all_alone_entries(
                game, home_picks, away_picks, week_number=game["week_number"]
            )
        )

    spread_analysis = _spread_bucket_trends(games, picks_by_game)

    kv = KVClient(**get_kv_config())
    kv.write(
        f"season:{SEASON}:trends",
        {
            "season": SEASON,
            "updated_at": now_iso(),
            "team_pick_totals": team_pick_totals,
            "cold_teams_season": cold_teams_season,
            "team_ats_record": team_ats_record,
            "all_alone_season": all_alone,
            "spread_analysis": spread_analysis,
        },
    )
    logger.info(
        "Wrote season:%s:trends (%d teams picked, %d cold, %d ATS records, "
        "%d all alone, %d spread buckets) to KV",
        SEASON,
        len(team_pick_totals),
        len(cold_teams_season),
        len(team_ats_record),
        len(all_alone),
        len(spread_analysis["by_bucket"]),
    )
