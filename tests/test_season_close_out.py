"""src/season_close_out.py (the once-a-year close-out - never run for real
yet) and the KV keys it and the season's bookkeeping feed: meta:historical,
plus meta:current, the odds key and the user profile keys."""

import json
from typing import Any

import pytest

from config.config import (
    PERIODS_BY_KEY,
    SEASON,
    CBSConfig,
    Period,
)
from src import season_close_out
from src.kv_writer import historical, leaderboard, odds, shared, user_profiles
from tests.conftest import Clients, FakeD1, Seed

OVERALL_PAID_PLACES = PERIODS_BY_KEY["overall"].paid_places
FIRST_HALF_PAID_PLACES = PERIODS_BY_KEY["first_half"].paid_places
SECOND_HALF_PAID_PLACES = PERIODS_BY_KEY["second_half"].paid_places
SECOND_HALF_START_WEEK = PERIODS_BY_KEY["second_half"].start_week

LAST_WEEK = 18


@pytest.fixture(autouse=True)
def _fakes(clients: Clients, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "get_cbs_config", lambda: CBSConfig("u", "p", "pool1"))


def _full_season(seed: Seed, scores: dict[str, list[int]]) -> dict[str, int]:
    """every week 1-18 seeded, each user's score for every week (a list
    shorter than 18 is padded with its last value)"""
    seed.season()
    seed.d1.query(
        "UPDATE seasons SET name = 'MorLocked 10.0' WHERE season_id = ?", [SEASON]
    )
    week_ids = [seed.week(n) for n in range(1, LAST_WEEK + 1)]
    seed.d1.query("UPDATE weeks SET is_complete = 1 WHERE season_id = ?", [SEASON])
    users = {}
    for name, week_scores in scores.items():
        users[name] = seed.user(name)
        padded = week_scores + [week_scores[-1]] * (LAST_WEEK - len(week_scores))
        for week_id, score in zip(week_ids, padded):
            seed.performance(users[name], week_id, score)
    return users


def _standings(d1: FakeD1) -> dict[int, dict[str, Any]]:
    rows = d1.query(
        "SELECT * FROM historical_standings WHERE season_id = ?", [SEASON]
    ).results
    return {r["user_id"]: r for r in rows}


class TestCloseOut:
    def test_final_standings(self, clients: Clients, seed: Seed) -> None:
        first_half = SECOND_HALF_START_WEEK - 1
        second_half = LAST_WEEK - first_half
        users = _full_season(
            seed,
            {
                # a wins the first half, b the second, c wins overall
                "a": [5] * first_half + [1],
                "b": [1] * first_half + [5],
                "c": [4],
            },
        )

        season_close_out.close_out_season()

        rows = _standings(clients.d1)
        a, c = rows[users["a"]], rows[users["c"]]
        assert (c["final_rank"], c["final_score"]) == (1, 4 * LAST_WEEK)
        assert c["is_champion"] == 1
        assert a["final_score"] == 5 * first_half + second_half
        periods = {
            (r["user_id"], r["period_key"]): (r["rank"], r["score"])
            for r in clients.d1.query(
                "SELECT * FROM historical_period_standings WHERE season_id = ?",
                [SEASON],
            ).results
        }
        assert periods[(users["a"], "first_half")] == (1, 5 * first_half)
        assert periods[(users["b"], "second_half")] == (1, 5 * second_half)
        assert {r["pool_name"] for r in rows.values()} == {"MorLocked 10.0"}

    def test_matches_the_live_leaderboard(self, clients: Clients, seed: Seed) -> None:
        # close-out reuses compute_week_leaderboard(), so the final week's
        # leaderboard and the historical row can never disagree
        from src.kv_writer.leaderboard import compute_week_leaderboard

        _full_season(seed, {"a": [3], "b": [3], "c": [2]})
        board = compute_week_leaderboard(clients.d1, LAST_WEEK)
        assert board is not None

        season_close_out.close_out_season()

        rows = _standings(clients.d1)
        for entry in board:
            row = rows[entry["user_id"]]
            assert (row["final_rank"], row["final_score"]) == (
                entry["place"],
                entry["cumulative_score"],
            )
        # a tie at the top shares first place - two champions
        assert sum(r["is_champion"] for r in rows.values()) == 2

    def test_refreshes_meta_historical(self, clients: Clients, seed: Seed) -> None:
        _full_season(seed, {"a": [5], "b": [1]})

        season_close_out.close_out_season()

        champions = clients.kv.values["meta:historical"]["champions"]
        assert champions == [
            {
                "year": SEASON,
                "incomplete": False,
                "names": ["a"],
                "score": 5 * LAST_WEEK,
            }
        ]

    def test_rerun_updates_in_place(self, clients: Clients, seed: Seed) -> None:
        users = _full_season(seed, {"a": [5], "b": [1]})
        season_close_out.close_out_season()
        clients.d1.query(
            "UPDATE weekly_performance SET picks_correct = 5 WHERE user_id = ?",
            [users["b"]],
        )

        season_close_out.close_out_season()

        rows = _standings(clients.d1)
        assert len(rows) == 2
        assert rows[users["b"]]["final_rank"] == 1  # now tied

    def test_periods_and_last_place(
        self, clients: Clients, seed: Seed, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        thirds = (
            Period("overall", "Overall", 1, None, 1, pay_last_place=True),
            Period("first_third", "First Third", 1, 6, 1, pay_last_place=True),
            Period("second_third", "Second Third", 7, 12, 1),
            Period("third_third", "Third Third", 13, None, 1),
        )
        monkeypatch.setattr(leaderboard, "PERIODS", thirds)
        monkeypatch.setattr(season_close_out, "PERIODS", thirds)
        monkeypatch.setattr("src.periods.PERIODS", thirds)
        users = _full_season(seed, {"a": [5], "b": [3], "c": [1]})
        # c is lowest but skipped week 2, so b takes overall last place
        clients.d1.query(
            "UPDATE weekly_performance SET picks_made = 0 WHERE user_id = ? AND week_id ="
            " (SELECT week_id FROM weeks WHERE week_number = 2)",
            [users["c"]],
        )

        season_close_out.close_out_season()

        rows = _standings(clients.d1)
        assert {u: r["last_place"] for u, r in rows.items()} == {
            users["a"]: 0,
            users["b"]: 1,
            users["c"]: 0,
        }
        periods = clients.d1.query(
            "SELECT user_id, period_key, rank, score, last_place"
            " FROM historical_period_standings WHERE season_id = ?",
            [SEASON],
        ).results
        by_key = {(r["user_id"], r["period_key"]): r for r in periods}
        assert {k for _, k in by_key} == {"first_third", "second_third", "third_third"}
        assert by_key[(users["a"], "first_third")]["rank"] == 1
        assert by_key[(users["a"], "first_third")]["score"] == 30
        assert by_key[(users["b"], "first_third")]["last_place"] == 1
        # not paid in the second third
        assert not any(
            r["last_place"] for r in periods if r["period_key"] == "second_third"
        )
        saved = clients.d1.query(
            "SELECT periods_json FROM seasons WHERE season_id = ?", [SEASON]
        ).results[0]["periods_json"]
        assert [(p["key"], p["end_week"]) for p in json.loads(saved)] == [
            ("overall", LAST_WEEK),
            ("first_third", 6),
            ("second_third", 12),
            ("third_third", LAST_WEEK),
        ]

        key = clients.kv.values["meta:historical"]
        assert [(w["period_key"], w["names"]) for w in key["last_place"]] == [
            ("overall", ["b"]),
            ("first_third", ["b"]),
        ]
        assert [(w["label"], w["names"]) for w in key["period_champions"]] == [
            ("First Third", ["a"]),
            ("Second Third", ["a"]),
            ("Third Third", ["a"]),
        ]

    def test_rerun_drops_periods_no_longer_configured(
        self, clients: Clients, seed: Seed, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _full_season(seed, {"a": [5], "b": [1]})
        season_close_out.close_out_season()
        overall_only = (Period("overall", "Overall", 1, None, 1),)
        monkeypatch.setattr(leaderboard, "PERIODS", overall_only)
        monkeypatch.setattr(season_close_out, "PERIODS", overall_only)

        season_close_out.close_out_season()

        assert (
            clients.d1.query("SELECT * FROM historical_period_standings").results == []
        )

    def test_unfinished_week_blocks_close_out(
        self, clients: Clients, seed: Seed
    ) -> None:
        _full_season(seed, {"a": [5]})
        clients.d1.query(
            "UPDATE weeks SET is_complete = 0 WHERE week_number = ?", [LAST_WEEK]
        )

        season_close_out.close_out_season()

        assert _standings(clients.d1) == {}

    def test_nothing_to_close_out(self, clients: Clients, seed: Seed) -> None:
        season_close_out.close_out_season()  # no weeks at all
        seed.week(1)
        season_close_out.close_out_season()  # weeks, no scores
        assert _standings(clients.d1) == {}
        assert "meta:historical" not in clients.kv.values

    def test_inactive_users_are_left_out(self, clients: Clients, seed: Seed) -> None:
        users = _full_season(seed, {"a": [3], "gone": [5]})
        clients.d1.query(
            "UPDATE users SET is_active = 0 WHERE user_id = ?", [users["gone"]]
        )

        season_close_out.close_out_season()

        assert set(_standings(clients.d1)) == {users["a"]}


class TestMetaHistorical:
    def _history(self, seed: Seed) -> dict[str, int]:
        users = {name: seed.user(name) for name in ("ann", "bob", "cy")}
        rows = [
            # (season, user, rank, score)
            (2023, "ann", 1, 80),
            (2023, "bob", 2, 75),
            (2024, "ann", 1, 70),  # a shared title
            (2024, "bob", 1, 70),
            (2024, "cy", 3, 60),
        ]
        for season, name, rank, score in rows:
            seed.season(season)
            # meta:historical's pool name comes from seasons.name
            seed.d1.query(
                "UPDATE seasons SET name = ? WHERE season_id = ?",
                [f"Pool {season}", season],
            )
            seed.d1.query(
                "INSERT INTO historical_standings (season_id, user_id, pool_name,"
                " final_rank, final_score) VALUES (?, ?, ?, ?, ?)",
                [season, users[name], f"Pool {season}", rank, score],
            )
        # 2024 as an archive season (no periods_json), 2025 closed out with thirds
        for name, rank, last in (("ann", 1, False), ("bob", 2, True)):
            seed.d1.query(
                "INSERT INTO historical_period_standings (season_id, user_id,"
                " period_key, rank, score, last_place) VALUES (2024, ?, 'first_half', ?, 40, ?)",
                [users[name], rank, last],
            )
        seed.season(2025)
        seed.d1.query(
            "UPDATE seasons SET periods_json = ? WHERE season_id = 2025",
            [
                json.dumps(
                    [
                        {"key": "overall", "label": "Overall"},
                        {"key": "early", "label": "Early Third"},
                    ]
                )
            ],
        )
        seed.historical(users["cy"], 2025, final_rank=1, final_score=90)
        seed.d1.query(
            "UPDATE historical_standings SET last_place = 1 WHERE season_id = 2025"
        )
        seed.d1.query(
            "INSERT INTO historical_period_standings (season_id, user_id, period_key,"
            " rank, score, last_place) VALUES (2025, ?, 'early', 1, 33, 0)",
            [users["cy"]],
        )
        # 2016's archive is missing its actual champion
        seed.season(2016)
        seed.d1.query(
            "UPDATE seasons SET historical_data_incomplete = 1 WHERE season_id = 2016"
        )
        seed.historical(users["cy"], 2016, final_rank=3, final_score=50)
        # a rank 1 that survived in an incomplete archive still isn't named
        # champion - the real one may be the missing row
        seed.historical(users["bob"], 2016, final_rank=1, final_score=60)
        return users

    def test_champions(self, clients: Clients, seed: Seed) -> None:
        self._history(seed)

        historical.write_historical()

        key = clients.kv.values["meta:historical"]
        assert [(c["year"], c["names"]) for c in key["champions"]] == [
            (2016, ["??? unknown/missing user"]),
            (2023, ["ann"]),
            (2024, ["ann", "bob"]),
            (2025, ["cy"]),
        ]
        assert key["champions"][0]["incomplete"] is True
        assert "first_half_champions" not in key
        assert set(key["years"]) == {"2016", "2023", "2024", "2025"}
        assert key["years"]["2024"]["pool_name"] == "Pool 2024"

    def test_periods(self, clients: Clients, seed: Seed) -> None:
        users = self._history(seed)

        historical.write_historical()

        key = clients.kv.values["meta:historical"]
        assert key["period_champions"] == [
            # an archive season's label comes from its key
            {
                "year": 2024,
                "period_key": "first_half",
                "label": "First Half",
                "names": ["ann"],
                "score": 40,
            },
            {
                "year": 2025,
                "period_key": "early",
                "label": "Early Third",
                "names": ["cy"],
                "score": 33,
            },
        ]
        assert [
            (w["year"], w["period_key"], w["names"]) for w in key["last_place"]
        ] == [
            (2024, "first_half", ["bob"]),
            (2025, "overall", ["cy"]),
        ]
        assert key["years"]["2024"]["periods"] is None
        assert [p["key"] for p in key["years"]["2025"]["periods"]] == [
            "overall",
            "early",
        ]
        bob_2024 = next(
            s for s in key["years"]["2024"]["standings"] if s["user_id"] == users["bob"]
        )
        assert bob_2024["periods"] == {
            "first_half": {"rank": 2, "score": 40, "last_place": True}
        }
        assert bob_2024["last_place"] is False
        career = {c["name"]: c for c in key["career"]}
        assert career["cy"]["season_history"][-1]["last_place"] is True
        assert career["cy"]["season_history"][-1]["periods"]["early"]["rank"] == 1

    def test_career(self, clients: Clients, seed: Seed) -> None:
        users = self._history(seed)

        historical.write_historical()

        career = {c["name"]: c for c in clients.kv.values["meta:historical"]["career"]}
        assert career["ann"]["titles"] == 2
        assert career["ann"]["best_finish_years"] == [2023, 2024]
        assert career["cy"]["appearances"] == [2016, 2024, 2025]
        assert career["bob"]["titles"] == 2  # career counts the 2016 row
        assert [
            c["user_id"] for c in clients.kv.values["meta:historical"]["career"]
        ] == sorted(users.values())

    def test_no_history(self, clients: Clients) -> None:
        historical.write_historical()
        assert clients.kv.values == {}


def test_meta_current(clients: Clients, seed: Seed) -> None:
    week_id = seed.week(4)
    clients.d1.query("UPDATE weeks SET is_current = 1 WHERE week_id = ?", [week_id])

    shared.write_meta_current()

    assert clients.kv.values["meta:current"] == {
        "season": SEASON,
        "current_week": 4,
        # end_week None in config resolves to the season's last week loaded
        "periods": [
            {
                "key": "overall",
                "label": "Overall",
                "start_week": 1,
                "end_week": 4,
                "paid_places": OVERALL_PAID_PLACES,
                "pay_last_place": False,
            },
            {
                "key": "first_half",
                "label": "First Half",
                "start_week": 1,
                "end_week": SECOND_HALF_START_WEEK - 1,
                "paid_places": FIRST_HALF_PAID_PLACES,
                "pay_last_place": False,
            },
            {
                "key": "second_half",
                "label": "Second Half",
                "start_week": SECOND_HALF_START_WEEK,
                "end_week": 4,
                "paid_places": SECOND_HALF_PAID_PLACES,
                "pay_last_place": False,
            },
        ],
        "cbs_pool_url": "https://picks.cbssports.com/football/pickem/pools/pool1",
    }


def test_meta_current_without_a_current_week(clients: Clients, seed: Seed) -> None:
    seed.week(4)
    shared.write_meta_current()
    assert clients.kv.values == {}


class TestOddsKey:
    def _odds(
        self,
        d1: FakeD1,
        game_id: int,
        book: str,
        market: str,
        point: float | None,
        price: int,
        at: str,
    ) -> None:
        d1.query(
            "INSERT INTO odds_snapshots (game_id, source, bookmaker, market, captured_at,"
            " home_point, home_price, away_point, away_price)"
            " VALUES (?, 'the_odds_api', ?, ?, ?, ?, ?, ?, ?)",
            [
                game_id,
                book,
                market,
                at,
                point,
                price,
                -point if point is not None else None,
                -price,
            ],
        )

    @pytest.fixture
    def week(self, seed: Seed) -> dict[str, int]:
        week_id = seed.week(1)
        seed.d1.query("UPDATE weeks SET is_current = 1 WHERE week_id = ?", [week_id])
        return {
            "priced": seed.game(week_id, cbs_spread=-4.0),
            "bare": seed.game(week_id, cbs_spread=3.0),
        }

    def test_consensus_is_the_mode(
        self, clients: Clients, week: dict[str, int]
    ) -> None:
        game = week["priced"]
        for book, open_point, close_point in (
            ("draftkings", -3.0, -4.0),
            ("fanduel", -3.0, -4.0),
            ("betmgm", -2.5, -4.5),
        ):
            self._odds(
                clients.d1,
                game,
                book,
                "spread",
                open_point,
                -110,
                "2026-09-08T12:00:00Z",
            )
            self._odds(
                clients.d1,
                game,
                book,
                "spread",
                close_point,
                -110,
                "2026-09-13T12:00:00Z",
            )

        odds.write_current_week_odds()

        games = {
            g["game_id"]: g
            for g in clients.kv.values[f"week:{SEASON}:01:odds"]["games"]
        }
        market = games[game]["market_spread"]
        assert (market["open"], market["open_agreement"]) == (-3.0, 2)
        assert (market["close"], market["close_agreement"]) == (-4.0, 2)
        assert market["book_count"] == 3
        assert games[game]["cbs_spread"] == -4.0
        assert games[week["bare"]]["market_spread"] is None
        assert games[week["bare"]]["books"] == []

    def test_a_tie_goes_to_the_line_priced_nearest_110(
        self, clients: Clients, week: dict[str, int]
    ) -> None:
        # the real 2026-09-19 case: bovada's -4 at -110 beats two books'
        # -4.5 at -107/-102 - never a -4.25 nobody offers
        game = week["priced"]
        for book, point, price in (
            ("bovada", -4.0, -110),
            ("draftkings", -4.0, -115),
            ("betrivers", -4.5, -107),
            ("fanduel", -4.5, -102),
        ):
            self._odds(
                clients.d1, game, book, "spread", point, price, "2026-09-13T12:00:00Z"
            )

        odds.write_week_odds(1)

        market = clients.kv.values[f"week:{SEASON}:01:odds"]["games"][0][
            "market_spread"
        ]
        assert market["close"] == -4.0

    def test_each_books_latest_line(
        self, clients: Clients, week: dict[str, int]
    ) -> None:
        game = week["priced"]
        self._odds(
            clients.d1, game, "draftkings", "spread", -3.0, -110, "2026-09-08T12:00:00Z"
        )
        self._odds(
            clients.d1, game, "draftkings", "spread", -4.0, -105, "2026-09-13T12:00:00Z"
        )
        self._odds(
            clients.d1, game, "draftkings", "total", 44.5, -110, "2026-09-13T12:00:00Z"
        )
        self._odds(
            clients.d1,
            game,
            "draftkings",
            "moneyline",
            None,
            -190,
            "2026-09-13T12:00:00Z",
        )
        self._odds(
            clients.d1, game, "betus", "spread", -9.0, -110, "2026-09-13T12:00:00Z"
        )

        odds.write_week_odds(1)

        (book,) = clients.kv.values[f"week:{SEASON}:01:odds"]["games"][0]["books"]
        assert book["bookmaker"] == "draftkings"  # an offshore book isn't listed
        assert (book["spread"]["home_point"], book["spread"]["home_price"]) == (
            -4.0,
            -105,
        )
        assert book["total"]["home_point"] == 44.5
        assert book["moneyline"]["home_price"] == -190

    def test_no_current_week(self, clients: Clients, seed: Seed) -> None:
        seed.week(1)
        odds.write_current_week_odds()
        assert clients.kv.values == {}


class TestUserProfileKeys:
    def test_one_key_per_active_user(self, clients: Clients, seed: Seed) -> None:
        active = [seed.user("a"), seed.user("b")]
        seed.user("gone", is_active=False)

        user_profiles.write_user_profiles()

        keys = {k for k in clients.kv.values if k.startswith("user:")}
        assert keys == {f"user:{u}:season:{SEASON}" for u in active}
        profile = clients.kv.values[f"user:{active[0]}:season:{SEASON}"]
        assert set(profile) == {
            "user_id",
            "name",
            "season",
            "career",
            "current_season",
            "updated_at",
        }

    def test_no_users(self, clients: Clients) -> None:
        user_profiles.write_user_profiles()
        assert clients.kv.values == {}
