"""Pydantic models for ESPN's undocumented public scoreboard endpoint
(site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard).

This is an unofficial, undocumented ESPN API - no public docs, no terms of
service, no SLA - but well-known and stable in practice, widely used by the
sports-data community. Only the fields this project actually uses are
modeled (situation/status/team refs); the real payload also carries a large
amount of page-rendering data (logos, navigation links, betting deep links)
that's deliberately left unmodeled.

`Situation` (down/distance/field position) is only present on the
competition object once a game is actually IN_PROGRESS - confirmed live
2026-09-09 across a full week's scoreboard (absent on all 15 scheduled
games, present on the one live game). `odds` is the mirror image - present
on scheduled games, absent once live - and isn't modeled here at all since
The Odds API already covers pre-game odds and live in-game odds tracking
was deliberately deferred (would need ESPN's much heavier per-game
/core/nfl/game endpoint instead of this lightweight one).
"""

from pydantic import BaseModel, ConfigDict, Field


class EspnModel(BaseModel):
    """Base for every typed ESPN entity - tolerate unknown fields."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class LastPlayType(EspnModel):
    text: str | None = None


class WinProbability(EspnModel):
    # fractions 0-1, not percents
    home_win_percentage: float | None = Field(default=None, alias="homeWinPercentage")
    away_win_percentage: float | None = Field(default=None, alias="awayWinPercentage")
    tie_percentage: float | None = Field(default=None, alias="tiePercentage")


class DriveStart(EspnModel):
    # same frame as Situation.yard_line: yards from the home goal line
    yard_line: int | None = Field(default=None, alias="yardLine")
    text: str | None = None  # e.g. 'LAR 37'


class LastPlayDrive(EspnModel):
    description: str | None = None
    start: DriveStart | None = None


class LastPlay(EspnModel):
    id: str | None = None
    text: str | None = None
    type: LastPlayType | None = None
    probability: WinProbability | None = None
    drive: LastPlayDrive | None = None


class Situation(EspnModel):
    down: int | None = None
    distance: int | None = None
    yard_line: int | None = Field(default=None, alias="yardLine")
    down_distance_text: str | None = Field(default=None, alias="downDistanceText")
    possession_text: str | None = Field(default=None, alias="possessionText")
    is_red_zone: bool = Field(default=False, alias="isRedZone")
    home_timeouts: int | None = Field(default=None, alias="homeTimeouts")
    away_timeouts: int | None = Field(default=None, alias="awayTimeouts")
    # the team currently possessing the ball - ESPN's own key is just
    # "possession", not "possessionTeamId" despite that being the more
    # descriptive name
    possession_team_id: str | None = Field(default=None, alias="possession")
    last_play: LastPlay | None = Field(default=None, alias="lastPlay")


class StatusType(EspnModel):
    name: str
    state: str
    completed: bool
    detail: str
    short_detail: str = Field(alias="shortDetail")


class Status(EspnModel):
    period: int
    display_clock: str = Field(alias="displayClock")
    type: StatusType


class CompetitorTeam(EspnModel):
    id: str
    abbreviation: str


class Competitor(EspnModel):
    id: str
    home_away: str = Field(alias="homeAway")
    team: CompetitorTeam
    score: str


class Competition(EspnModel):
    id: str
    date: str
    neutral_site: bool = Field(alias="neutralSite")
    competitors: list[Competitor] = Field(default_factory=list[Competitor])
    status: Status
    situation: Situation | None = None


class Event(EspnModel):
    id: str
    date: str
    short_name: str = Field(alias="shortName")
    competitions: list[Competition] = Field(default_factory=list[Competition])


# --- /summary?event={id} (one game) ---
class PlayPeriod(EspnModel):
    number: int


class PlayClock(EspnModel):
    display_value: str = Field(alias="displayValue")


class SummaryPlay(EspnModel):
    id: str
    period: PlayPeriod | None = None
    clock: PlayClock | None = None
    home_score: int | None = Field(default=None, alias="homeScore")
    away_score: int | None = Field(default=None, alias="awayScore")
    scoring_play: bool = Field(default=False, alias="scoringPlay")


class SummaryDrive(EspnModel):
    plays: list[SummaryPlay] = Field(default_factory=list[SummaryPlay])


class SummaryDrives(EspnModel):
    previous: list[SummaryDrive] = Field(default_factory=list[SummaryDrive])
    current: SummaryDrive | None = None


class WinProbabilityPoint(EspnModel):
    # fraction 0-1; one point per play, keyed to a drive play by play_id,
    # plus one pre-kickoff point that matches no play (confirmed live
    # across all 43 FINAL 2026 games as of 2026-09-27)
    home_win_percentage: float = Field(alias="homeWinPercentage")
    tie_percentage: float | None = Field(default=None, alias="tiePercentage")
    play_id: str = Field(alias="playId")


class Summary(EspnModel):
    win_probability: list[WinProbabilityPoint] = Field(
        default_factory=list[WinProbabilityPoint], alias="winprobability"
    )
    drives: SummaryDrives | None = None


class Scoreboard(EspnModel):
    events: list[Event] = Field(default_factory=list[Event])
