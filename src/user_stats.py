"""Derived per-user profile stats (streaks, tendencies, historical record) -
computed straight from D1, no new loader, no external API calls. Written to
KV by src/kv_writer/user_profiles.py's write_user_profiles(). See
src/CLAUDE.md's KV writer section.
"""

from collections import Counter, defaultdict
from statistics import pstdev
from typing import Any

from config.config import SEASON, SECOND_HALF_START_WEEK
from db.d1_client import D1Client

_HOT_STREAK_THRESHOLD_PCT = 0.8  # "80% or better" - the user's own bar
_MIN_TEAM_PICKS_FOR_RECORD = 2  # floor so a single 0-1/1-0 isn't a "nemesis"/"lucky team"

_SEASON_USER_PICKS_SQL = """
SELECT up.user_id, u.name, w.week_number, g.game_id, g.game_time,
    g.home_team_id, g.away_team_id, g.cbs_spread, up.picked_team_id, up.is_correct,
    t.abbreviation AS team_abbr, t.nick_name AS team_name
FROM user_picks up
JOIN users u ON u.user_id = up.user_id
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
JOIN teams t ON t.team_id = up.picked_team_id
WHERE w.season_id = ? AND u.is_active = TRUE
ORDER BY up.user_id, g.game_time
"""

_SEASON_WEEKLY_PERFORMANCE_SQL = """
SELECT wp.user_id, w.week_number, w.is_complete AS week_is_complete,
    wp.picks_made, wp.picks_correct
FROM weekly_performance wp
JOIN weeks w ON w.week_id = wp.week_id
JOIN users u ON u.user_id = wp.user_id
WHERE w.season_id = ? AND u.is_active = TRUE
ORDER BY wp.user_id, w.week_number
"""

_ACTIVE_USERS_SQL = "SELECT user_id, name FROM users WHERE is_active = TRUE"

_FINAL_WEEK_SQL = "SELECT MAX(week_number) AS final_week FROM weeks WHERE season_id = ?"


def _rank_by_score(score_by_user: dict[int, int]) -> dict[int, int]:
    """highest first, ties share a place and the next place skips - same
    shape as src/kv_writer/leaderboard.py's _standard_rank(), duplicated here
    (rather than imported) since kv_writer is what imports this module, not
    the other way around."""
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


def _season_trend(season_history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rank/score direction from the two most recent prior seasons - None
    if there aren't at least two to compare. Negative rank_change means the
    more recent season's rank number went down, i.e. improved."""
    usable = [s for s in season_history if not s["incomplete"]]
    if len(usable) < 2:
        return None
    last_season, prior_season = usable[-1], usable[-2]
    rank_change = last_season["rank"] - prior_season["rank"]
    return {
        "last_season": {"season": last_season["season"], "rank": last_season["rank"], "score": last_season["score"]},
        "prior_season": {"season": prior_season["season"], "rank": prior_season["rank"], "score": prior_season["score"]},
        "rank_change": rank_change,
        "direction": "improving" if rank_change < 0 else "declining" if rank_change > 0 else "same",
    }


def _current_and_longest_streak(flags: list[bool]) -> tuple[int, int]:
    """flags in chronological order - (streak ending at the last entry,
    longest streak anywhere in the list)."""
    longest = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return current, longest


def _consecutive_week_runs(weeks: list[int], latest_week: int) -> tuple[int, int]:
    """(longest run of consecutive week numbers, current run - 0 unless the
    most recent of these weeks is the user's own most recently picked week,
    since a gap after the last of these weeks means the streak already
    ended)."""
    weeks_sorted = sorted(set(weeks))
    longest = run = 1
    for i in range(1, len(weeks_sorted)):
        run = run + 1 if weeks_sorted[i] == weeks_sorted[i - 1] + 1 else 1
        longest = max(longest, run)
    current = run if weeks_sorted[-1] == latest_week else 0
    return longest, current


def _team_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["picked_team_id"], "abbr": row["team_abbr"], "name": row["team_name"]}


def _is_favorite(row: dict[str, Any], is_home: bool) -> bool | None:
    """None for a pick'em (cbs_spread == 0) or a missing spread - no
    favorite/underdog to speak of."""
    spread = row["cbs_spread"]
    if spread is None or spread == 0:
        return None
    return (is_home and spread < 0) or (not is_home and spread > 0)


def _bias_block(matches: list[bool]) -> dict[str, Any] | None:
    """Season-wide pct + pick count for one pick classification (home/away/
    favorite/underdog). None if the user has no decided picks for it yet.

    Deliberately no streak here (removed 2026-09-23, previously computed by
    _current_and_longest_streak) - these picks are only orderable by each
    game's kickoff time, not the user's actual decision order (CBS exposes
    no per-pick timestamp at all, since a pick can be changed anytime
    before its game locks), and the old streak calc didn't even reset at
    week boundaries the way team_pick_streak/hot_streak deliberately do -
    it could silently chain the last pick of one week into the next as if
    back to back. A streak claim we can't stand behind is worse than none;
    pct alone is the part of this that's actually reliable."""
    if not matches:
        return None
    return {
        "pct": round(sum(matches) / len(matches), 3),
        "picks": len(matches),
    }


def _team_pick_streaks(user_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Longest/current run of consecutive weeks picking the same team, plus
    the team picked most often overall (not necessarily consecutively)."""
    weeks_by_team: defaultdict[int, list[int]] = defaultdict(list)
    team_dict_by_id: dict[int, dict[str, Any]] = {}
    pick_counts: Counter[int] = Counter()
    for row in user_rows:
        team_id = row["picked_team_id"]
        weeks_by_team[team_id].append(row["week_number"])
        team_dict_by_id[team_id] = _team_dict(row)
        pick_counts[team_id] += 1

    latest_week = max(row["week_number"] for row in user_rows)

    best_current: tuple[int, int] | None = None
    best_longest: tuple[int, int] | None = None
    for team_id, weeks in weeks_by_team.items():
        longest, current = _consecutive_week_runs(weeks, latest_week)
        if best_current is None or current > best_current[1]:
            best_current = (team_id, current)
        if best_longest is None or longest > best_longest[1]:
            best_longest = (team_id, longest)

    most_picked_team_id, most_picked_count = pick_counts.most_common(1)[0]

    return {
        "current": (
            {"team": team_dict_by_id[best_current[0]], "weeks": best_current[1]}
            if best_current and best_current[1] >= 2
            else None
        ),
        "longest": (
            {"team": team_dict_by_id[best_longest[0]], "weeks": best_longest[1]}
            if best_longest and best_longest[1] >= 2
            else None
        ),
        "most_picked_team": {
            "team": team_dict_by_id[most_picked_team_id],
            "count": most_picked_count,
        },
    }


def _pick_bias(user_rows: list[dict[str, Any]]) -> dict[str, Any]:
    home_matches = [row["picked_team_id"] == row["home_team_id"] for row in user_rows]
    away_matches = [not m for m in home_matches]

    favorite_flags = [
        _is_favorite(row, row["picked_team_id"] == row["home_team_id"])
        for row in user_rows
    ]
    decided = [f for f in favorite_flags if f is not None]
    favorite_matches = decided
    underdog_matches = [not f for f in decided]

    return {
        "home": _bias_block(home_matches),
        "away": _bias_block(away_matches),
        "favorite": _bias_block(favorite_matches),
        "underdog": _bias_block(underdog_matches),
    }


def _team_records(user_rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Per-team personal record (graded picks only), for teams picked at
    least _MIN_TEAM_PICKS_FOR_RECORD times."""
    results_by_team: defaultdict[int, list[bool]] = defaultdict(list)
    team_dict_by_id: dict[int, dict[str, Any]] = {}
    for row in user_rows:
        if row["is_correct"] is None:
            continue
        results_by_team[row["picked_team_id"]].append(bool(row["is_correct"]))
        team_dict_by_id[row["picked_team_id"]] = _team_dict(row)

    records: dict[int, dict[str, Any]] = {}
    for team_id, results in results_by_team.items():
        if len(results) < _MIN_TEAM_PICKS_FOR_RECORD:
            continue
        wins = sum(results)
        losses = len(results) - wins
        records[team_id] = {
            "team": team_dict_by_id[team_id],
            "wins": wins,
            "losses": losses,
            "win_pct": round(wins / len(results), 3),
        }
    return records


def _nemesis_and_lucky_team(
    records: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """min()/max() alone would always return *something* - with only one
    qualifying team (or a tie), the same team could come back as both
    nemesis and lucky regardless of whether it's actually been good or bad
    for this user (e.g. a perfect 2-0 team forced into "nemesis" just for
    being the only candidate). Guarded so each direction only fires when
    the record actually points that way - a losing record for nemesis, a
    winning one for lucky; an exact .500 team is neither."""
    if not records:
        return None, None
    nemesis = min(records.values(), key=lambda r: (r["win_pct"], -r["losses"]))
    lucky = max(records.values(), key=lambda r: (r["win_pct"], -r["losses"]))
    return (
        nemesis if nemesis["win_pct"] < 0.5 else None,
        lucky if lucky["win_pct"] > 0.5 else None,
    )


def _trap_team(records: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    """The team this user keeps going back to that keeps burning them -
    weighted by how big a share of their picks (among teams that clear
    _MIN_TEAM_PICKS_FOR_RECORD - the same pool records draws from) went to
    that team, not just raw win_pct like nemesis_team above. A team picked
    twice and lost both counts the same toward nemesis_team as a team
    picked ten times and lost eight - but only the second is really a
    habit that's hurting them (a real sports-betting "trap team"), which
    is what trap_score (share of picks * (1 - win_pct), same shape as the
    group-level _trap_team_ranking() in kv_writer/trends.py) is meant to
    surface instead.

    Same max()-always-returns-something risk as nemesis_team above: a
    trap_score of 0 means this team hasn't actually burned them at all
    (e.g. it's their only qualifying team and they're 2-0 on it), so that
    case returns None rather than a "trap" that isn't one."""
    if not records:
        return None
    total_graded = sum(r["wins"] + r["losses"] for r in records.values())
    if not total_graded:
        return None
    ranked = max(
        records.values(),
        key=lambda r: ((r["wins"] + r["losses"]) / total_graded) * (1 - r["win_pct"]),
    )
    pct_of_picks = round((ranked["wins"] + ranked["losses"]) / total_graded, 3)
    trap_score = round(pct_of_picks * (1 - ranked["win_pct"]), 4)
    if trap_score == 0:
        return None
    return {**ranked, "pct_of_picks": pct_of_picks, "trap_score": trap_score}


def _game_side_pick_counts(all_picks_rows: list[dict[str, Any]]) -> dict[int, tuple[int, int]]:
    """Per game, (home_pick_count, away_pick_count) across every user in the
    pool - what a single pick is judged "contrarian" against."""
    home_counts: Counter[int] = Counter()
    away_counts: Counter[int] = Counter()
    for row in all_picks_rows:
        if row["picked_team_id"] == row["home_team_id"]:
            home_counts[row["game_id"]] += 1
        else:
            away_counts[row["game_id"]] += 1
    game_ids = set(home_counts) | set(away_counts)
    return {gid: (home_counts.get(gid, 0), away_counts.get(gid, 0)) for gid in game_ids}


def _contrarian_block(
    user_rows: list[dict[str, Any]], side_counts_by_game: dict[int, tuple[int, int]]
) -> dict[str, Any]:
    """Picks made against that game's pool majority ("contrarian") vs with
    it ("chalk"), and accuracy on each - reuses the exact same per-game
    home/away pick counts src/kv_writer/trends.py's write_week_trends()
    derives, just computed fresh here for the whole season across every
    user."""
    contrarian_correct = contrarian_graded = contrarian_total = 0
    chalk_correct = chalk_graded = chalk_total = 0

    for row in user_rows:
        home_n, away_n = side_counts_by_game.get(row["game_id"], (0, 0))
        is_home = row["picked_team_id"] == row["home_team_id"]
        own_side_n = home_n if is_home else away_n
        other_side_n = away_n if is_home else home_n
        if own_side_n == other_side_n:
            continue  # no clear majority either way

        is_contrarian = own_side_n < other_side_n
        graded = row["is_correct"] is not None
        correct = bool(row["is_correct"]) if graded else False
        if is_contrarian:
            contrarian_total += 1
            contrarian_graded += graded
            contrarian_correct += correct
        else:
            chalk_total += 1
            chalk_graded += graded
            chalk_correct += correct

    return {
        "contrarian_picks": contrarian_total,
        "contrarian_accuracy_pct": (
            round(contrarian_correct / contrarian_graded, 3) if contrarian_graded else None
        ),
        "chalk_picks": chalk_total,
        "chalk_accuracy_pct": round(chalk_correct / chalk_graded, 3) if chalk_graded else None,
    }


def _weekly_accuracy(rows: list[dict[str, Any]]) -> float | None:
    made = sum(row["picks_made"] or 0 for row in rows)
    correct = sum(row["picks_correct"] or 0 for row in rows)
    return round(correct / made, 3) if made else None


def _hot_streak(completed_weeks: list[dict[str, Any]]) -> dict[str, Any]:
    flags = [
        (row["picks_correct"] or 0) / row["picks_made"] >= _HOT_STREAK_THRESHOLD_PCT
        for row in completed_weeks
        if row["picks_made"]
    ]
    current, longest = _current_and_longest_streak(flags)
    return {
        "threshold_pct": _HOT_STREAK_THRESHOLD_PCT,
        "current_streak": current,
        "longest_streak": longest,
    }


def _best_and_worst_week(
    completed_weeks: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not completed_weeks:
        return None, None
    best = max(completed_weeks, key=lambda r: r["picks_correct"] or 0)
    worst = min(completed_weeks, key=lambda r: r["picks_correct"] or 0)
    return (
        {"week_number": best["week_number"], "score": best["picks_correct"] or 0},
        {"week_number": worst["week_number"], "score": worst["picks_correct"] or 0},
    )


def _consistency(completed_weeks: list[dict[str, Any]]) -> dict[str, Any] | None:
    scores = [row["picks_correct"] or 0 for row in completed_weeks]
    if len(scores) < 2:
        return None
    return {"stddev": round(pstdev(scores), 2), "weeks_counted": len(scores)}


def _clutch(completed_weeks: list[dict[str, Any]], money_weeks: set[int]) -> dict[str, Any] | None:
    if not completed_weeks:
        return None
    money_rows = [row for row in completed_weeks if row["week_number"] in money_weeks]
    return {
        "money_week_accuracy_pct": _weekly_accuracy(money_rows) if money_rows else None,
        "season_accuracy_pct": _weekly_accuracy(completed_weeks),
        "money_weeks_counted": len(money_rows),
    }


def compute_user_profiles(
    d1: D1Client, career_by_user: dict[int, dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    """One profile per active user: historical career record (from
    career_by_user, see src/kv_writer/historical.py's career_record_by_user())
    plus this season's streaks/tendencies. career_by_user is passed in
    rather than queried here so this module never has to import kv_writer
    (kv_writer is what imports this module, not the other way around)."""
    users = {row["user_id"]: row["name"] for row in d1.query(_ACTIVE_USERS_SQL).results}

    all_picks = d1.query(_SEASON_USER_PICKS_SQL, [SEASON]).results
    picks_by_user: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in all_picks:
        picks_by_user[row["user_id"]].append(row)
    side_counts_by_game = _game_side_pick_counts(all_picks)

    all_weekly = d1.query(_SEASON_WEEKLY_PERFORMANCE_SQL, [SEASON]).results
    weekly_by_user: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in all_weekly:
        weekly_by_user[row["user_id"]].append(row)

    # same "sum picks_correct across every week" cumulative score
    # src/kv_writer/leaderboard.py's compute_week_leaderboard() ranks on - not gated on
    # week_is_complete there either, so this stays consistent with the live
    # leaderboard's own current-standing rank rather than only counting
    # weeks that have fully finished.
    cumulative_score_by_user = {
        user_id: sum(row["picks_correct"] or 0 for row in rows)
        for user_id, rows in weekly_by_user.items()
    }
    current_rank_by_user = _rank_by_score(cumulative_score_by_user)

    final_week_row = d1.query(_FINAL_WEEK_SQL, [SEASON]).results
    final_week = final_week_row[0]["final_week"] if final_week_row else None
    money_weeks = {w for w in (SECOND_HALF_START_WEEK - 1, final_week) if w is not None}

    profiles: dict[int, dict[str, Any]] = {}
    for user_id, name in users.items():
        career = career_by_user.get(user_id)
        season_history = career["season_history"] if career else []
        career_block = {
            "years_played": (len(career["appearances"]) if career else 0) + 1,
            "titles": career["titles"] if career else 0,
            "best_finish": career["best_finish"] if career else None,
            "best_finish_years": career["best_finish_years"] if career else [],
            "season_history": season_history,
            "trend": _season_trend(season_history),
        }

        user_rows = picks_by_user.get(user_id, [])
        weekly_rows = weekly_by_user.get(user_id, [])
        completed_weeks = [row for row in weekly_rows if row["week_is_complete"]]

        total_made = sum(row["picks_made"] or 0 for row in weekly_rows)
        total_correct = sum(row["picks_correct"] or 0 for row in weekly_rows)
        best_week, worst_week = _best_and_worst_week(completed_weeks)
        team_records = _team_records(user_rows)
        nemesis_team, lucky_team = _nemesis_and_lucky_team(team_records)
        trap_team = _trap_team(team_records)

        current_season = {
            "total_picks": total_made,
            "total_correct": total_correct,
            "accuracy_pct": round(total_correct / total_made, 3) if total_made else None,
            "current_rank": current_rank_by_user.get(user_id),
            "hot_streak": _hot_streak(completed_weeks),
            "team_pick_streak": _team_pick_streaks(user_rows) if user_rows else None,
            "pick_bias": _pick_bias(user_rows) if user_rows else None,
            "contrarian": _contrarian_block(user_rows, side_counts_by_game)
            if user_rows
            else None,
            "nemesis_team": nemesis_team,
            "lucky_team": lucky_team,
            "trap_team": trap_team,
            "best_week": best_week,
            "worst_week": worst_week,
            "consistency": _consistency(completed_weeks),
            "clutch": _clutch(completed_weeks, money_weeks),
        }

        profiles[user_id] = {
            "user_id": user_id,
            "name": name,
            "season": SEASON,
            "career": career_block,
            "current_season": current_season,
        }

    return profiles
