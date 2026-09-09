"""
Used Claude to model the data

Typed models for The Odds API's `GET /v4/sports/{sport}/odds` response —
the only endpoint `api/the_odds_api_client.py` calls. Unlike api-sports.io
(see `api/sports_io_models.py`), the response body is a bare JSON array —
no envelope, no `errors`/`results`/`response` wrapper to unwrap. Errors
come back as real non-200 HTTP statuses with a small JSON body
(`{"message": ..., "error_code": ..., "details_url": ...}`), confirmed
live with a bad API key (401/INVALID_KEY).

`point` is optional on `Outcome` because it's only present for spreads/
totals markets, not h2h
"""

from pydantic import BaseModel, ConfigDict


class TheOddsApiModel(BaseModel):
    """Base for every typed The Odds API entity: tolerate unknown fields
    (new bookmakers/markets get added without notice)."""

    model_config = ConfigDict(extra="allow")


class Outcome(TheOddsApiModel):
    name: str
    price: float
    point: float | None = None


class Market(TheOddsApiModel):
    key: str
    last_update: str
    outcomes: list[Outcome]


class Bookmaker(TheOddsApiModel):
    key: str
    title: str
    last_update: str
    markets: list[Market]


class Event(TheOddsApiModel):
    id: str
    sport_key: str
    sport_title: str
    commence_time: str
    home_team: str
    away_team: str
    bookmakers: list[Bookmaker]
