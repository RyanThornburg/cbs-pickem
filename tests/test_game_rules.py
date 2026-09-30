"""src/game_rules.py - favorite/winner/pick side. ats_side() is covered in
test_trends.py and standard_rank() in test_leaderboard.py."""

from src.game_rules import favorite_side, other_side, pick_side, winner_side

_GAME = {
    "cbs_spread": 3.0,
    "status": "FINAL",
    "home_score": 20,
    "away_score": 17,
    "home_id": 1,
    "away_id": 2,
}


def test_favorite_side() -> None:
    assert favorite_side(_GAME) == "away"
    assert favorite_side({**_GAME, "cbs_spread": -3.0}) == "home"
    assert favorite_side({**_GAME, "cbs_spread": 0}) is None
    assert favorite_side({**_GAME, "cbs_spread": None}) is None


def test_winner_side() -> None:
    assert winner_side(_GAME) == "home"
    assert winner_side({**_GAME, "away_score": 20}) is None
    assert winner_side({**_GAME, "status": "IN_PROGRESS"}) is None
    assert winner_side({**_GAME, "home_score": None}) is None


def test_pick_side() -> None:
    assert pick_side(_GAME, 1) == "home"
    assert pick_side(_GAME, 2) == "away"
    assert pick_side(_GAME, 99) is None
    assert other_side("home") == "away"
    assert other_side("away") == "home"
