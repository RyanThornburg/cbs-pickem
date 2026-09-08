"""
Used Claude to model the data
"""

# Typed models for the CBS Pick'em weekly-standings payload.
#
# `api/cbs_client.py` scrapes the weekly-standings page's SSR bootstrap: a
# `(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push({"rehydrate": {...}})`
# call whose `rehydrate[*].data.commonPool` is a fully-resolved GraphQL
# response.
#
# Several fields are `None` in real responses whenever the underlying value
# hasn't happened yet (a game's `gamePeriod` before kickoff, an entry's
# `rank`/pick's `pickInfo` before the week is scored) — those are modeled as
# optional rather than required. Fields/entities not currently used
# (tiebreakers, pool settings, myEntries) are left unmodeled; extend these
# models from real data if/when that changes.

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CBSModel(BaseModel):
    """Base for every typed CBS entity: tolerate unknown fields (CBS adds
    them without notice) and accept either the alias or the python name."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Team(CBSModel):
    """id/cbsTeamId/abbrev always come back; the rest only populate when
    fetched from the pool-home page (client.fetch_pool_home_data()) — the
    weekly-standings page's Team is just id/cbsTeamId/abbrev."""

    typename: Literal["Team"] = Field(alias="__typename")
    id: str
    cbs_team_id: int = Field(alias="cbsTeamId")
    abbrev: str
    color_primary_hex: str | None = Field(default=None, alias="colorPrimaryHex")
    color_secondary_hex: str | None = Field(default=None, alias="colorSecondaryHex")
    medium_name: str | None = Field(default=None, alias="mediumName")
    nick_name: str | None = Field(default=None, alias="nickName")
    location: str | None = None
    wins: int | None = None
    losses: int | None = None
    ties: int | None = None
    rank: int | None = None
    image_url: str | None = Field(default=None, alias="imageUrl")


class PoolEvent(CBSModel):
    """CBS Game details"""

    typename: Literal["PoolEvent"] = Field(alias="__typename")
    id: str
    cbs_event_id: int = Field(alias="cbsEventId")
    away_team: Team = Field(alias="awayTeam")
    away_team_score: int | None = Field(default=None, alias="awayTeamScore")
    home_team_id: str = Field(alias="homeTeamId")
    home_team: Team = Field(alias="homeTeam")
    home_team_score: int | None = Field(default=None, alias="homeTeamScore")
    home_team_spread: float | None = Field(default=None, alias="homeTeamSpread")
    game_status_desc: str = Field(alias="gameStatusDesc")
    is_locked: bool = Field(alias="isLocked")
    possession: str  # 'NONE', 'HOME', 'AWAY'
    tv_info_name: str | None = Field(default=None, alias="tvInfoName")
    time_remaining: str | None = Field(default=None, alias="timeRemaining")
    starts_at: int = Field(alias="startsAt")  # epoch millis
    game_period: int | None = Field(default=None, alias="gamePeriod")
    sport_type: str = Field(alias="sportType")
    gametracker_link: str | None = Field(default=None, alias="gametrackerLink")


class TvNetwork(CBSModel):
    typename: Literal["TvNetwork"] = Field(alias="__typename")
    call_letters: str = Field(alias="callLetters")
    name: str


class EventExtra(CBSModel):
    """Pick-ownership percentages — pool-home page only."""

    typename: Literal["EventExtra"] = Field(alias="__typename")
    home_team_pickem_percent_owned: int | None = Field(
        default=None, alias="homeTeamPickemPercentOwned"
    )
    away_team_pickem_percent_owned: int | None = Field(
        default=None, alias="awayTeamPickemPercentOwned"
    )
    home_team_pickem_percent_owned_against_spread: int | None = Field(
        default=None, alias="homeTeamPickemPercentOwnedAgainstSpread"
    )
    away_team_pickem_percent_owned_against_spread: int | None = Field(
        default=None, alias="awayTeamPickemPercentOwnedAgainstSpread"
    )


class MoneyLine(CBSModel):
    typename: Literal["EventTeamOddsMoneyLine"] = Field(alias="__typename")
    team_id: str = Field(alias="teamId")
    odds: str
    opening_odds: str | None = Field(default=None, alias="openingOdds")


class Spread(CBSModel):
    typename: Literal["EventTeamOddsSpread"] = Field(alias="__typename")
    team_id: str = Field(alias="teamId")
    spread: str
    odds: str
    opening_spread: str | None = Field(default=None, alias="openingSpread")
    opening_odds: str | None = Field(default=None, alias="openingOdds")


class Total(CBSModel):
    typename: Literal["EventTeamOddsTotal"] = Field(alias="__typename")
    choice: str  # 'OVER', 'UNDER'
    total: str
    odds: str
    opening_total: str | None = Field(default=None, alias="openingTotal")
    opening_odds: str | None = Field(default=None, alias="openingOdds")


class OddsBook(CBSModel):
    typename: Literal["EventOddsBook"] = Field(alias="__typename")
    name: str


class OddsMarket(CBSModel):
    """CBS's own book's current + opening lines — pool-home page only."""

    typename: Literal["EventOddsMarket"] = Field(alias="__typename")
    book_used: OddsBook = Field(alias="bookUsed")
    money_lines: list[MoneyLine] = Field(default_factory=list, alias="moneyLines")
    spreads: list[Spread] = Field(default_factory=list)
    totals: list[Total] = Field(default_factory=list)


class PoolPeriod(CBSModel):
    """Shares a __typename with PoolPeriodSummary but is a
    different shape — CBS reuses PoolPeriod for both a detailed "current
    period" view and a lightweight list entry."""

    typename: Literal["PoolPeriod"] = Field(alias="__typename")
    id: str
    pool_events: list[PoolEvent] = Field(default_factory=list, alias="poolEvents")


class PoolHomePoolEvent(PoolEvent):
    """PoolEvent, with the additional fields only the pool-home page's query
    returns: odds market, pick-ownership percentages, and week number.
    home_team/away_team are inherited as-is — Team's extra detail fields
    just populate since this is the page that returns them."""

    away_team_id: str = Field(alias="awayTeamId")
    game_status: str = Field(alias="gameStatus")
    marked_final_at: str | None = Field(default=None, alias="markedFinalAt")
    season_type: str = Field(alias="seasonType")
    week_number: int = Field(alias="weekNumber")
    winning_team_id: str | None = Field(default=None, alias="winningTeamId")
    extra: EventExtra | None = None
    odds_market: OddsMarket | None = Field(default=None, alias="oddsMarket")
    tv_networks: list[TvNetwork] = Field(default_factory=list, alias="tvNetworks")


class PoolHomePoolPeriod(PoolPeriod):
    """PoolPeriod, with the fields the pool-home page's query returns that
    the weekly-standings page's PoolPeriod doesn't (order/isPlayOff/isCurrent
    live on PoolPeriodSummary there instead)."""

    order: int
    is_playoff: bool = Field(alias="isPlayOff")
    is_current: bool = Field(alias="isCurrent")
    pool_events: list[PoolHomePoolEvent] = Field(
        default_factory=list, alias="poolEvents"
    )


class Season(CBSModel):
    typename: Literal["Season"] = Field(alias="__typename")
    id: str
    year: int


class PoolPeriodSummary(CBSModel):
    """One entry in the pool's `poolPeriods` list (e.g. for a week picker)."""

    typename: Literal["PoolPeriod"] = Field(alias="__typename")
    id: str
    description: str
    is_current: bool = Field(alias="isCurrent")
    order: int


class Member(CBSModel):
    """member name/id. `email` is only present on the pool's /players page —
    absent (None) everywhere else Member shows up, e.g. weekly standings."""

    typename: Literal["Member"] = Field(alias="__typename")
    id: str
    name: str
    email: str | None = None


class FootballPickemEntry(CBSModel):
    """only using the weekly version of this, ignoring the myEntries portion
    so all fields are required
    """

    typename: Literal["FootballPickemEntry"] = Field(alias="__typename")
    id: str
    name: str
    is_mine: bool = Field(alias="isMine")
    member: Member


class FootballPickemStandingsRank(CBSModel):
    """integer and string (1st)"""

    typename: Literal["FootballPickemStandingsRank"] = Field(alias="__typename")
    formatted_value: str = Field(alias="formattedValue")
    value: int


class FootballPickemWeeklyStandingsPickInfo(CBSModel):
    """class related to actual pick"""

    typename: Literal["FootballPickemWeeklyStandingsPickInfo"] = Field(
        alias="__typename"
    )
    cbs_item_id: int = Field(alias="cbsItemId")
    item_id: str = Field(alias="itemId")
    pick_status: str = Field(alias="pickStatus")
    trending_status: str = Field(alias="trendingStatus")
    weight: float | None = None


class FootballPickemWeeklyStandingsPick(CBSModel):
    """users picks"""

    typename: Literal["FootballPickemWeeklyStandingsPick"] = Field(alias="__typename")
    id: str
    cbs_slot_id: int = Field(alias="cbsSlotId")
    entry_id: str = Field(alias="entryId")
    pool_id: str = Field(alias="poolId")
    pool_period_id: str = Field(alias="poolPeriodId")
    slot_id: str = Field(alias="slotId")
    display_status: str = Field(alias="displayStatus")
    pick_info: FootballPickemWeeklyStandingsPickInfo | None = Field(
        default=None, alias="pickInfo"
    )


class FootballPickemWeeklyStandingsEntry(CBSModel):
    """list of users week picks/standings"""

    typename: Literal["FootballPickemWeeklyStandingsEntry"] = Field(alias="__typename")
    id: str
    entry: FootballPickemEntry
    rank: FootballPickemStandingsRank | None = None
    period_score: int = Field(alias="periodScore")
    score: int
    trending_score: int = Field(alias="trendingScore")
    picks: list[FootballPickemWeeklyStandingsPick] = Field(default_factory=list)


class FootballPickemManagerWeeklyStandings(CBSModel):
    """list of ranked weekly"""

    typename: Literal["FootballPickemManagerWeeklyStandings"] = Field(
        alias="__typename"
    )
    ranked_entries: list[FootballPickemWeeklyStandingsEntry] = Field(
        default_factory=list, alias="rankedEntries"
    )


class FootballPickemManagerPoolStandings(CBSModel):
    "weekly ranking"

    typename: Literal["FootballPickemManagerPoolStandings"] = Field(alias="__typename")
    weekly: FootballPickemManagerWeeklyStandings | None = None


class FootballPickemManagerPool(CBSModel):
    """Root of the commonPool payload returned by
    `client.extract_weekly_standings()`."""

    typename: Literal["FootballPickemManagerPool"] = Field(alias="__typename")
    id: str
    name: str
    is_using_spread: bool = Field(alias="isUsingSpread")
    are_games_available: bool = Field(alias="areGamesAvailable")
    has_ended: bool = Field(alias="hasEnded")
    pool_period: PoolPeriod = Field(alias="poolPeriod")
    pool_periods: list[PoolPeriodSummary] = Field(
        default_factory=list, alias="poolPeriods"
    )
    standings: FootballPickemManagerPoolStandings | None = None

    def pool_period_for_week(self, week: int) -> PoolPeriodSummary:
        """find pool period id from week/current status"""
        pool_periods = self.pool_periods
        return next(
            p for p in pool_periods if (p.is_current and week == 0) or (p.order == week)
        )

    @property
    def ranked_entry_count(self) -> int:
        """if entries exist return their count"""
        return (
            len(self.standings.weekly.ranked_entries)
            if self.standings and self.standings.weekly
            else 0
        )


class FootballPickemPoolHome(FootballPickemManagerPool):
    """Root of the commonPool payload returned by the pool-home page
    (`client.fetch_pool_home_data()`) — same FootballPickemManagerPool
    shape as the weekly-standings page, but its query asks for more
    fields: `season`, and per-event odds/pick-percentage/team detail via
    PoolHomePoolPeriod. Has no `standings`/ranked entries — those only
    come from the weekly-standings page."""

    season: Season
    pool_period: PoolHomePoolPeriod = Field(alias="poolPeriod")
