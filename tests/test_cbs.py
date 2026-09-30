"""CBS parsing and pick loading, replayed against saved (anonymized) CBS
payloads - see tests/fixtures/sanitize_cbs.py. Weeks 1 and 3 were captured
after every game was final, week 2 mid-week (final, live and scheduled
games, and one entry with no picks yet)."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from api.cbs_client import APOLLO_MARKER, _extract_common_pool
from api.cbs_models import FootballPickemManagerPool, FootballPickemPoolHome
from config.config import SEASON
from src.loaders import cbs_loader
from tests.conftest import FakeD1, Seed

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "cbs"
WEEKS = [1, 2, 3]
COMPLETE_WEEKS = [1, 3]
PICKS_PER_WEEK = 5


def _raw(prefix: str, week: int) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{prefix}_{week:02d}.json").read_text())


def _weekly(week: int) -> FootballPickemManagerPool:
    return FootballPickemManagerPool.model_validate(_raw("cbs_week", week))


def _pool_home(week: int) -> FootballPickemPoolHome:
    return FootballPickemPoolHome.model_validate(_raw("cbs_pool_home", week))


@pytest.mark.parametrize("week", WEEKS)
class TestWeeklyStandingsPage:
    def test_period_resolves_to_its_week_number(self, week: int) -> None:
        # the same lookup get_cbs_weekly() uses to name the saved file
        data = _weekly(week)
        summary = next(p for p in data.pool_periods if p.id == data.pool_period.id)
        assert summary.order == week

    def test_one_current_period(self, week: int) -> None:
        assert sum(p.is_current for p in _weekly(week).pool_periods) == 1

    def test_has_the_whole_pool(self, week: int) -> None:
        data = _weekly(week)
        assert data.ranked_entry_count > 0
        assert len(data.pool_period.pool_events) == 16

    def test_every_pick_points_at_a_game_this_week(self, week: int) -> None:
        data = _weekly(week)
        assert data.standings is not None and data.standings.weekly is not None
        event_ids = {e.cbs_event_id for e in data.pool_period.pool_events}
        for entry in data.standings.weekly.ranked_entries:
            assert len(entry.picks) <= PICKS_PER_WEEK
            assert {p.cbs_slot_id for p in entry.picks} <= event_ids


@pytest.mark.parametrize("week", WEEKS)
class TestPoolHomePage:
    def test_season_and_week(self, week: int) -> None:
        data = _pool_home(week)
        assert data.season.year == SEASON
        assert data.pool_period.order == week
        assert {e.week_number for e in data.pool_period.pool_events} == {week}

    def test_team_detail_is_populated(self, week: int) -> None:
        for event in _pool_home(week).pool_period.pool_events:
            for team in (event.home_team, event.away_team):
                assert team.abbrev
                assert team.medium_name
                assert team.nick_name

    def test_final_games_have_epoch_millis_marked_final_at(self, week: int) -> None:
        finals = [
            e for e in _pool_home(week).pool_period.pool_events if e.game_status == "F"
        ]
        assert finals
        for event in finals:
            # was typed str once and blocked every CBS loader, see api/CLAUDE.md
            assert isinstance(event.marked_final_at, int)
            assert event.marked_final_at > event.starts_at


class TestModelContract:
    def test_unknown_fields_are_tolerated(self) -> None:
        raw = _raw("cbs_week", 1)
        raw["someNewCbsField"] = {"anything": 1}
        raw["poolPeriod"]["poolEvents"][0]["anotherNewField"] = True
        FootballPickemManagerPool.model_validate(raw)

    def test_missing_required_field_raises(self) -> None:
        raw = _raw("cbs_week", 1)
        del raw["poolPeriod"]["poolEvents"][0]["cbsEventId"]
        with pytest.raises(ValidationError):
            FootballPickemManagerPool.model_validate(raw)

    def test_marked_final_at_rejects_a_string(self) -> None:
        raw = _raw("cbs_pool_home", 1)
        raw["poolPeriod"]["poolEvents"][0]["markedFinalAt"] = "2026-09-10T00:00:00Z"
        with pytest.raises(ValidationError):
            FootballPickemPoolHome.model_validate(raw)


class TestExtractCommonPool:
    @staticmethod
    def _push(common_pool: dict[str, Any] | None) -> str:
        payload = {"rehydrate": {"query1": {"data": {"commonPool": common_pool}}}}
        return f"<script>{APOLLO_MARKER}{json.dumps(payload)});</script>"

    def test_picks_the_push_with_the_required_key(self) -> None:
        wanted = _raw("cbs_week", 1)
        html = (
            "<html>"
            + self._push({"id": "other", "name": "no standings"})
            + self._push(None)
            + self._push(wanted)
            + "</html>"
        )
        assert _extract_common_pool(html, "standings") == wanted

    def test_skips_malformed_json(self) -> None:
        wanted = {"standings": {}}
        html = f"{APOLLO_MARKER}{{not json" + self._push(wanted)
        assert _extract_common_pool(html, "standings") == wanted

    def test_none_when_missing(self) -> None:
        assert _extract_common_pool("<html></html>", "standings") is None
        assert _extract_common_pool(self._push({"id": "x"}), "standings") is None


class TestLoaderHelpers:
    @pytest.mark.parametrize(
        ("cbs", "common"),
        [
            ("SCHEDULED", "SCHEDULED"),
            ("InProgress", "IN_PROGRESS"),
            ("HALFTIME", "HALFTIME"),
            ("final", "FINAL"),
            ("POSTPONED", "POSTPONED"),
            ("CANCELLED", "CANCELLED"),
            ("SOMETHING_NEW", "SOMETHING_NEW"),
        ],
    )
    def test_status_mapping(self, cbs: str, common: str) -> None:
        assert cbs_loader._cbs_status_to_common(cbs) == common

    def test_starts_at_is_utc_iso(self) -> None:
        # 2026-09-10 20:20 ET kickoff
        assert cbs_loader._cbs_starts_at_to_iso(1789086000000) == "2026-09-11T00:20:00Z"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [("CORRECT", True), ("INCORRECT", False), ("NONE", None), ("PUSH", None)],
    )
    def test_pick_status(self, status: str, expected: bool | None) -> None:
        assert cbs_loader._pick_status_to_correct(status) is expected


def _seed_from_weekly(
    seed: Seed, data: FootballPickemManagerPool, skip_members: int = 0
) -> None:
    """users/weeks/teams/games rows mapped to this payload's CBS ids, the way
    load_cbs_users()/load_cbs_weeks()/load_cbs_games() would have left them"""
    assert data.standings is not None and data.standings.weekly is not None
    summary = next(p for p in data.pool_periods if p.id == data.pool_period.id)
    week_id = seed.week(summary.order)
    seed.d1.query(
        "UPDATE weeks SET cbs_pool_period_id = ? WHERE week_id = ?",
        [data.pool_period.id, week_id],
    )
    for entry in data.standings.weekly.ranked_entries[skip_members:]:
        member = entry.entry.member
        seed._insert("users", name=member.name, cbs_id=member.id)

    team_ids: dict[int, int] = {}
    for event in data.pool_period.pool_events:
        for team in (event.home_team, event.away_team):
            if team.cbs_team_id not in team_ids:
                team_ids[team.cbs_team_id] = seed._insert(
                    "teams",
                    name=team.abbrev,
                    season=SEASON,
                    abbreviation=team.abbrev,
                    cbs_team_id=team.cbs_team_id,
                )
        seed.game(
            week_id,
            home_team_id=team_ids[event.home_team.cbs_team_id],
            away_team_id=team_ids[event.away_team.cbs_team_id],
            cbs_event_id=event.cbs_event_id,
        )


def _patch_cbs(
    monkeypatch: pytest.MonkeyPatch, d1: FakeD1, data: FootballPickemManagerPool
) -> None:
    """load_cbs_user_picks() reads `data` instead of scraping CBS, and
    writes to the fake D1"""
    monkeypatch.setattr(cbs_loader, "get_cbs_weekly", lambda _period=None: data)
    monkeypatch.setattr(cbs_loader, "get_d1", lambda: d1)


def _load_week(
    monkeypatch: pytest.MonkeyPatch,
    d1: FakeD1,
    seed: Seed,
    week: int,
    skip_members: int = 0,
) -> FootballPickemManagerPool:
    data = _weekly(week)
    _seed_from_weekly(seed, data, skip_members)
    _patch_cbs(monkeypatch, d1, data)
    cbs_loader.load_cbs_user_picks()
    return data


class TestLoadUserPicks:
    @pytest.mark.parametrize("week", COMPLETE_WEEKS)
    def test_complete_week(
        self, monkeypatch: pytest.MonkeyPatch, d1: FakeD1, seed: Seed, week: int
    ) -> None:
        data = _load_week(monkeypatch, d1, seed, week)
        assert data.standings is not None and data.standings.weekly is not None
        period_score = {
            e.entry.member.id: e.period_score
            for e in data.standings.weekly.ranked_entries
        }

        rows = d1.query(
            """
            SELECT u.cbs_id, wp.picks_made, wp.picks_correct, wp.has_submitted_picks,
                COUNT(up.pick_id) AS picks_loaded,
                SUM(up.is_correct) AS picks_graded_correct,
                SUM(up.is_correct IS NULL) AS picks_ungraded
            FROM weekly_performance wp
            JOIN users u ON u.user_id = wp.user_id
            LEFT JOIN user_picks up ON up.user_id = wp.user_id
            GROUP BY wp.performance_id
            """
        ).results

        assert len(rows) == data.ranked_entry_count
        for row in rows:
            # picks_made once silently equaled picks_correct (8db02b6)
            assert row["picks_made"] == PICKS_PER_WEEK
            assert row["picks_loaded"] == PICKS_PER_WEEK
            assert row["has_submitted_picks"] == 1
            assert row["picks_ungraded"] == 0
            assert row["picks_correct"] == period_score[row["cbs_id"]]
            # CBS's own score agrees with the picks it graded
            assert row["picks_graded_correct"] == row["picks_correct"]
        assert d1.query("SELECT COUNT(*) AS n FROM mapping_gaps").results[0]["n"] == 0

    def test_mid_week(
        self, monkeypatch: pytest.MonkeyPatch, d1: FakeD1, seed: Seed
    ) -> None:
        _load_week(monkeypatch, d1, seed, 2)

        picks_made = Counter(
            row["picks_made"]
            for row in d1.query("SELECT picks_made FROM weekly_performance").results
        )
        assert picks_made == {PICKS_PER_WEEK: 32, 0: 1}

        graded = d1.query(
            "SELECT is_correct, COUNT(*) AS n FROM user_picks GROUP BY is_correct"
        ).results
        by_state = {row["is_correct"]: row["n"] for row in graded}
        # unplayed games' picks load ungraded rather than being dropped
        assert by_state[None] > 0
        assert by_state[1] > 0 and by_state[0] > 0

    def test_only_revealed_picks_on_locked_games_load(
        self, monkeypatch: pytest.MonkeyPatch, d1: FakeD1, seed: Seed
    ) -> None:
        # before kickoff/the deadline CBS still returns the logged-in
        # account's own picks - an unlocked game or a LOCKED pick must not load
        raw = _raw("cbs_week", 1)
        events = raw["poolPeriod"]["poolEvents"]
        unlocked = {e["cbsEventId"] for e in events[:4]}
        for event in events[:4]:
            event["isLocked"] = False
        entries = raw["standings"]["weekly"]["rankedEntries"]
        hidden = entries[0]["picks"][0]
        hidden["displayStatus"] = "LOCKED"
        data = FootballPickemManagerPool.model_validate(raw)
        _patch_cbs(monkeypatch, d1, data)
        _seed_from_weekly(seed, data)

        cbs_loader.load_cbs_user_picks()

        loaded = d1.query(
            "SELECT up.cbs_pick_id, g.cbs_event_id FROM user_picks up "
            "JOIN games g ON g.game_id = up.game_id"
        ).results
        assert not {row["cbs_event_id"] for row in loaded} & unlocked
        assert hidden["id"] not in {row["cbs_pick_id"] for row in loaded}
        expected = sum(
            1
            for entry in entries
            for pick in entry["picks"]
            if pick["cbsSlotId"] not in unlocked and pick["displayStatus"] != "LOCKED"
        )
        assert len(loaded) == expected
        # picks_made counts what was loaded, not what CBS returned
        made = d1.query("SELECT SUM(picks_made) AS n FROM weekly_performance")
        assert made.results[0]["n"] == expected

    def test_reload_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch, d1: FakeD1, seed: Seed
    ) -> None:
        data = _load_week(monkeypatch, d1, seed, 1)
        cbs_loader.load_cbs_user_picks()

        counts = d1.query(
            "SELECT (SELECT COUNT(*) FROM user_picks) AS picks, "
            "(SELECT COUNT(*) FROM weekly_performance) AS perf"
        ).results[0]
        assert counts["perf"] == data.ranked_entry_count
        assert counts["picks"] == data.ranked_entry_count * PICKS_PER_WEEK

    def test_unknown_member_is_a_mapping_gap(
        self, monkeypatch: pytest.MonkeyPatch, d1: FakeD1, seed: Seed
    ) -> None:
        data = _load_week(monkeypatch, d1, seed, 1, skip_members=1)
        assert data.standings is not None and data.standings.weekly is not None
        missing = data.standings.weekly.ranked_entries[0].entry.member.id

        gaps = d1.query("SELECT entity_type, raw_value FROM mapping_gaps").results
        assert gaps == [{"entity_type": "user", "raw_value": missing}]
        perf = d1.query("SELECT COUNT(*) AS n FROM weekly_performance").results[0]
        assert perf["n"] == data.ranked_entry_count - 1

    def test_unseeded_week_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, d1: FakeD1
    ) -> None:
        data = _weekly(1)
        _patch_cbs(monkeypatch, d1, data)

        cbs_loader.load_cbs_user_picks()

        perf = d1.query("SELECT COUNT(*) AS n FROM weekly_performance").results[0]
        assert perf["n"] == 0
