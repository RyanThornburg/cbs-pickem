"""
Used Claude to model the data

Typed models for the api-sports.io American Football (NFL) v1 payloads
fetched by `api/sports_io_client.py`. Every endpoint shares the same
envelope (`get`/`parameters`/`errors`/`results`/`paging`/`response`), but
`sports_io_client.py`'s `_fetch_page`/`_request` already unwrap and
error-check that envelope by hand before anything here gets involved — so
these models only cover each endpoint's per-item shape (the contents of
`response`), validated per item, the same way `get_cbs_users` validates
each member individually against `Member` in `cbs_models.py`.

Quirks observed from real responses (not documented, discovered by calling
the live API):
  - `errors` is `[]` on success but can come back as a `dict[str, str]`
    (field name -> message) when a request errors while still returning
    HTTP 200 — the client checks this before trusting `response`.
  - `paging` was never observed on any of the modeled endpoints (all come
    back as a flat list in `response`); `sports_io_client.py`'s pagination
    loop handles it defensively without needing a model for it.
  - `/standings` and `/odds` return `results: 0, response: []` for
    games/seasons that don't have that data yet (e.g. odds for a
    preseason-only game, standings mid-preseason before any are posted) —
    an empty list is a valid response, not an error.
  - Team statistics' `posession` key is the API's own spelling (not ours —
    don't "fix" it or the field silently stops populating).
  - Several stat groups in `TeamGameStatistics` are just `{"total": int}`;
    those all reuse `CountStat` rather than one-off classes.
"""

from pydantic import BaseModel, ConfigDict, Field


class SportsIOModel(BaseModel):
    """Base for every typed api-sports entity: tolerate unknown fields
    (new stat groups, extra bet types, etc. get added without notice)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Country(SportsIOModel):
    name: str
    code: str | None = None
    flag: str | None = None


class League(SportsIOModel):
    id: int
    name: str
    season: int | str
    logo: str | None = None
    country: Country | None = None


class TeamRef(SportsIOModel):
    """Lean team stub embedded in games/statistics/events/odds — as
    opposed to `Team`, the full record `/teams` returns. `id` is `0` and
    `name` is `None` for not-yet-determined playoff matchups (e.g. a Wild
    Card game before the seeding is known)."""

    id: int
    name: str | None = None
    logo: str | None = None


# --- /leagues ---
class LeagueSeason(SportsIOModel):
    year: int
    start: str
    end: str
    current: bool


class LeagueInfo(SportsIOModel):
    id: int
    name: str
    logo: str | None = None


class LeagueSeasons(SportsIOModel):
    """
    /leagues item shape
    Distinct from `League` above, which is {id, name, season, logo, country} standings/games/odds.
    """

    league: LeagueInfo
    country: Country | None = None
    seasons: list[LeagueSeason]


# --- /teams ---
class Team(SportsIOModel):
    id: int
    name: str
    code: str | None = None
    city: str | None = None
    coach: str | None = None
    owner: str | None = None
    stadium: str | None = None
    established: int | None = None
    logo: str | None = None
    country: Country | None = None


# --- /standings ---
class Points(SportsIOModel):
    for_: int = Field(alias="for")
    against: int
    difference: int


class Records(SportsIOModel):
    home: str | None = None
    road: str | None = None
    conference: str | None = None
    division: str | None = None


class Standing(SportsIOModel):
    league: League
    conference: str
    division: str
    position: int
    team: TeamRef
    won: int
    lost: int
    ties: int
    points: Points
    records: Records
    streak: str | None = None


# --- /games ---
class GameDate(SportsIOModel):
    timezone: str
    date: str
    time: str
    timestamp: int


class Venue(SportsIOModel):
    name: str | None = None
    city: str | None = None


class GameStatus(SportsIOModel):
    short: str | None = None
    long: str | None = None
    timer: str | None = None


class GameInfo(SportsIOModel):
    id: int
    stage: str
    week: str
    date: GameDate
    venue: Venue | None = None
    status: GameStatus


class GameTeams(SportsIOModel):
    home: TeamRef
    away: TeamRef


class QuarterScore(SportsIOModel):
    quarter_1: int | None = None
    quarter_2: int | None = None
    quarter_3: int | None = None
    quarter_4: int | None = None
    overtime: int | None = None
    total: int | None = None


class GameScores(SportsIOModel):
    home: QuarterScore
    away: QuarterScore


class Game(SportsIOModel):
    game: GameInfo
    league: League
    teams: GameTeams
    scores: GameScores


# --- /games/statistics/teams ---
class FirstDowns(SportsIOModel):
    total: int
    passing: int
    rushing: int
    from_penalties: int
    third_down_efficiency: str
    fourth_down_efficiency: str


class Plays(SportsIOModel):
    total: int


class Yards(SportsIOModel):
    total: int
    yards_per_play: str
    total_drives: str


class Passing(SportsIOModel):
    total: int
    comp_att: str
    yards_per_pass: str
    interceptions_thrown: int
    sacks_yards_lost: str


class Rushing(SportsIOModel):
    total: int
    attempts: int
    yards_per_rush: str


class RedZone(SportsIOModel):
    made_att: str


class Penalties(SportsIOModel):
    total: str


class Turnovers(SportsIOModel):
    total: int
    lost_fumbles: int
    interceptions: int


class Possession(SportsIOModel):
    total: str


class CountStat(SportsIOModel):
    """Shared shape for the stat groups that are just `{"total": int}`."""

    total: int


class TeamGameStatistics(SportsIOModel):
    first_downs: FirstDowns
    plays: Plays
    yards: Yards
    passing: Passing
    rushings: Rushing
    red_zone: RedZone
    penalties: Penalties
    turnovers: Turnovers
    posession: Possession  # API's own typo, not ours — see module docstring
    interceptions: CountStat
    fumbles_recovered: CountStat
    sacks: CountStat
    safeties: CountStat
    int_touchdowns: CountStat
    points_against: CountStat


class TeamStatistics(SportsIOModel):
    team: TeamRef
    statistics: TeamGameStatistics


# --- /games/events ---
class Player(SportsIOModel):
    id: int
    name: str
    image: str | None = None


class EventScore(SportsIOModel):
    home: int
    away: int


class GameEvent(SportsIOModel):
    quarter: str
    minute: str
    team: TeamRef
    player: Player
    type: str
    comment: str | None = None
    score: EventScore


# --- /odds ---
class OddValue(SportsIOModel):
    value: str
    odd: str


class Bet(SportsIOModel):
    id: int
    name: str
    values: list[OddValue]


class Bookmaker(SportsIOModel):
    id: int
    name: str
    bets: list[Bet]


class OddsGameRef(SportsIOModel):
    id: int


class Odds(SportsIOModel):
    game: OddsGameRef
    league: League
    country: Country | None = None
    update: str
    bookmakers: list[Bookmaker]
