-- SQLite dialect (Cloudflare D1). Foreign key enforcement is turned on by
-- D1Client, which issues `PRAGMA foreign_keys = ON` alongside every query.

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE,
    cbs_id VARCHAR(100) UNIQUE, -- CBS Sports user ID for imports
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- NFL teams
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    season INT NOT NULL,
    city VARCHAR(100),
    abbreviation VARCHAR(10) NOT NULL UNIQUE,
    established INT,
    logo VARCHAR(150), -- URL? local? bytes?
    conference VARCHAR(10), -- AFC/NFC
    division VARCHAR(20), -- North, South, East, West
    cbs_team_id INT UNIQUE, -- CBS Sports team ID for resolving CBS game/pick imports
    sports_io_team_id INT UNIQUE, --- Sports IO team ID
    medium_name VARCHAR(50), -- CBS's market/brand name, e.g. 'Arizona'
    nick_name VARCHAR(50), -- e.g. 'Cardinals'
    color_primary_hex VARCHAR(6),
    color_secondary_hex VARCHAR(6),
    wins INT,
    losses INT,
    ties INT
);

-- Stadiums and venues (including international)
CREATE TABLE IF NOT EXISTS stadiums (
    stadium_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL UNIQUE, -- matched against Sports IO's game.venue.name for upserts
    city VARCHAR(100) NOT NULL,
    state VARCHAR(50),
    country VARCHAR(50) DEFAULT 'USA',
    latitude DECIMAL(9,6), -- for weather lookups (api/weather_api.py)
    longitude DECIMAL(9,6),
    surface_type VARCHAR(50), -- 'Grass', 'Artificial Turf'
    roof_type VARCHAR(50) -- 'Open', 'Dome', 'Retractable'
);

-- Seasons
CREATE TABLE IF NOT EXISTS seasons (
    season_id INTEGER PRIMARY KEY, -- the season's year (e.g. 2026), not autoincremented
    name VARCHAR(50),
    start_date DATE,
    end_date DATE,
    is_active BOOLEAN DEFAULT FALSE,
    historical_data_incomplete BOOLEAN DEFAULT FALSE -- our archived standings for this season are missing entries (confirmed for 2015/2016 - the actual rank-1, and for 2016 rank-2, are absent from the saved CBS export), not a property of the season itself
);

-- Weeks within a season
CREATE TABLE IF NOT EXISTS weeks (
    week_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INT NOT NULL,
    week_number INT NOT NULL,
    name VARCHAR(50), -- "Week 1", "Wild Card", etc.
    start_time DATETIME, -- ISO8601 UTC (e.g. "2026-09-14T17:00:00Z")
    end_time DATETIME, -- same format, max game_time in the week
    is_complete BOOLEAN DEFAULT FALSE, -- set once every game in the week is FINAL (orchestration._run_finished_game_stats)
    cbs_pool_period_id VARCHAR(50) UNIQUE, -- for mapping weeks in cbs
    is_current BOOLEAN DEFAULT FALSE, 
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    UNIQUE (season_id, week_number)
);

-- Individual games
CREATE TABLE IF NOT EXISTS games (
    game_id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id INT NOT NULL,
    home_team_id INT NOT NULL,
    away_team_id INT NOT NULL,
    stadium_id INT,
    cbs_event_id INT UNIQUE, 
    sports_io_game_id INT UNIQUE, 
    odds_api_event_id VARCHAR(50) UNIQUE, -- The Odds API event ID (string, not int)
    espn_event_id VARCHAR(50) UNIQUE, -- ESPN's event id (string) - matched by team abbreviation on first sighting, then joined on directly
    game_time DATETIME, -- ISO8601 UTC (e.g. "2026-09-14T17:00:00Z")
    cbs_spread DECIMAL(4,1), -- home team line that cbs uses/once its set it does not change
    home_score INT,
    away_score INT,
    home_q1_score INT,
    home_q2_score INT,
    home_q3_score INT,
    home_q4_score INT,
    home_ot_score INT,
    away_q1_score INT,
    away_q2_score INT,
    away_q3_score INT,
    away_q4_score INT,
    away_ot_score INT,
    status VARCHAR(20), -- normalized common status across CBS/Sports IO: SCHEDULED, IN_PROGRESS, HALFTIME, FINAL, CANCELLED, POSTPONED
    is_complete BOOLEAN AS (status = 'FINAL'),
    has_final_stats BOOLEAN NOT NULL DEFAULT FALSE, -- set once game_team_stats has been reloaded after this game went FINAL (see orchestration._run_finished_game_stats) - not a generated column since it tracks something game_team_stats did, not games itself
    is_international BOOLEAN DEFAULT FALSE,
    neutral_site BOOLEAN DEFAULT FALSE, -- from ESPN's competitions[].neutralSite (src/loaders/espn_loader.py) - international games plus any domestic neutral-site game
    tv_network VARCHAR(50),
    gametracker_url VARCHAR(255),
    status_desc VARCHAR(30), -- raw per-source status string, kept for debugging/audit
    -- pregame forecast before kickoff (overwriting on each call) -
    -- not keeping historical forecasts, just the latest. In-game
    -- weather is tracked separately, in the game_snapshots table.
    forecast_temp_f INT,
    forecast_feels_like_f INT,
    forecast_condition VARCHAR(50),
    forecast_icon VARCHAR(30), -- Pirate Weather's icon identifier, e.g. 'partly-cloudy-day'
    forecast_precip_type VARCHAR(20),
    forecast_wind_speed_mph INT,
    forecast_wind_gust_mph INT,
    forecast_wind_direction VARCHAR(10),
    forecast_precipitation_pct INT,
    forecast_visibility_mi DECIMAL(4,1),
    forecast_alert VARCHAR(255),
    forecast_captured_at TIMESTAMP,
    FOREIGN KEY (week_id) REFERENCES weeks(week_id),
    FOREIGN KEY (home_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (away_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (stadium_id) REFERENCES stadiums(stadium_id),
    UNIQUE (week_id, home_team_id, away_team_id) -- lets CBS/Sports IO upserts match a game seeded by the other source before its own external id is known
);

-- Periodic in-game snapshots (score/weather)
CREATE TABLE IF NOT EXISTS game_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INT NOT NULL,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    quarter INT, -- 1-4, 5=OT
    time_remaining VARCHAR(10), -- "MM:SS" left in the quarter, from CBS
    status_desc VARCHAR(30), -- CBS raw status at time of capture
    possession VARCHAR(10), -- 'HOME', 'AWAY'
    home_score INT,
    away_score INT,
    down INT, -- from ESPN - null between plays (e.g. halftime)
    distance INT,
    yard_line INT,
    down_distance_text VARCHAR(30), -- e.g. '1st & 10 at SEA 18'
    possession_text VARCHAR(20), -- e.g. 'SEA 18'
    is_red_zone BOOLEAN DEFAULT FALSE,
    home_timeouts INT,
    away_timeouts INT,
    temperature_f INT,
    feels_like_f INT,
    weather_condition VARCHAR(50), -- Pirate Weather's summary text, e.g. 'Overcast', 'Possible Drizzle', 'Fog'
    weather_icon VARCHAR(30), -- Pirate Weather's icon identifier, e.g. 'partly-cloudy-day'
    precip_type VARCHAR(20), -- 'rain', 'snow', 'sleet', 'none'
    wind_speed_mph INT,
    wind_gust_mph INT,
    wind_direction VARCHAR(10),
    precipitation_pct INT,
    visibility_mi DECIMAL(4,1),
    weather_alert VARCHAR(255), -- active alert title(s) at capture time, e.g. 'Winter Storm Warning' - NULL if none
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Team box-score stats for a game (one row per team per game), mirroring
-- Sports IO's games/statistics/teams response. "N-M" fields in that response
-- (comp_att, sacks_yards_lost, made_att, third/fourth_down_efficiency,
-- penalties.total) are split into two int columns each rather than stored
-- as raw strings. turnovers/fumbles_lost are this team's own giveaways
-- (offense), interceptions/fumbles_recovered/sacks_recorded are this team's
-- takeaways (defense), don't confuse the two despite similar names.
CREATE TABLE IF NOT EXISTS game_team_stats (
    stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INT NOT NULL,
    team_id INT NOT NULL,

    first_downs_total INT,
    first_downs_passing INT,
    first_downs_rushing INT,
    first_downs_penalties INT,
    third_down_conversions INT,
    third_down_attempts INT,
    fourth_down_conversions INT,
    fourth_down_attempts INT,

    plays_total INT,
    yards_total INT,
    yards_per_play DECIMAL(4,1),
    total_drives INT,

    passing_yards INT,
    passing_completions INT,
    passing_attempts INT,
    yards_per_pass DECIMAL(4,1),
    interceptions_thrown INT, -- this team's QB(s) getting picked off (offense)
    sacks_given_up INT,
    sack_yards_lost INT,

    rushing_yards INT,
    rushing_attempts INT,
    yards_per_rush DECIMAL(4,1),

    redzone_made INT,
    redzone_attempts INT,

    penalties INT,
    penalty_yards INT,

    total_turnovers INT, -- fumbles_lost + interceptions_thrown (offense)
    fumbles_lost INT,

    interceptions INT, -- interceptions this team's defense recorded (takeaway)
    fumbles_recovered INT, -- fumbles this team's defense recovered (takeaway)
    sacks_recorded INT, -- sacks this team's defense recorded (takeaway) - distinct from sacks_given_up
    int_touchdowns INT, -- touchdowns off a return (pick-six etc.)

    safeties INT,
    points_against INT,
    time_of_possession_sec INT,

    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    UNIQUE (game_id, team_id)
);


-- User picks for each game
CREATE TABLE IF NOT EXISTS user_picks (
    pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INT NOT NULL,
    game_id INT NOT NULL,
    picked_team_id INT NOT NULL,
    is_correct BOOLEAN DEFAULT NULL, -- NULL until game is complete
    trending_status VARCHAR(10) DEFAULT 'NONE', -- CBS's own hot/cold signal for this pick
    cbs_pick_id VARCHAR(150) UNIQUE, -- CBS's Pick.id, for reference
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (picked_team_id) REFERENCES teams(team_id),
    UNIQUE (user_id, game_id)
);

-- Weekly performance summary (0-5 scores per week)
CREATE TABLE IF NOT EXISTS weekly_performance (
    performance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INT NOT NULL,
    week_id INT NOT NULL,
    has_submitted_picks BOOLEAN DEFAULT FALSE, -- did a user forget picks?
    picks_made INT DEFAULT 0,
    picks_correct INT DEFAULT 0,
    weekly_score AS (picks_correct), -- Simple 0-5 score
    trending_score INT DEFAULT 0, -- CBS's own per-entry trending score
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (week_id) REFERENCES weeks(week_id),
    UNIQUE (user_id, week_id)
);

CREATE TRIGGER IF NOT EXISTS trg_weekly_performance_updated_at
AFTER UPDATE ON weekly_performance
BEGIN
    UPDATE weekly_performance SET updated_at = CURRENT_TIMESTAMP WHERE performance_id = OLD.performance_id;
END;

-- Final standings from prior seasons
CREATE TABLE IF NOT EXISTS historical_standings (
    historical_standing_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INT NOT NULL,
    user_id INT NOT NULL,
    pool_name VARCHAR(50), -- e.g. "MorLocked 8.0" - changes most years
    final_rank INT NOT NULL, -- CBS's own tie-aware rank.value
    final_score INT NOT NULL,
    is_champion BOOLEAN AS (final_rank = 1),
    -- don't have all the data so allow nulls
    first_half_rank INT,
    first_half_score INT,
    second_half_rank INT,
    second_half_score INT,
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    UNIQUE (season_id, user_id)
);

-- Identity resolution audit trail for the historical_standings backfill -
-- CBS's entry/member ids for the same real person differ every season (no
-- stable natural key across years, confirmed live), so matching onto
-- users.user_id is done by exact (case-insensitive) name instead. This
-- table records which raw CBS entry each season resolved to which user_id,
-- so a re-run doesn't need to re-derive (or re-ask about) the same mapping.
CREATE TABLE IF NOT EXISTS historical_user_mapping (
    season_id INT NOT NULL,
    cbs_entry_id VARCHAR(150) NOT NULL,
    cbs_member_id VARCHAR(150),
    raw_name VARCHAR(100) NOT NULL, -- the name exactly as it appeared that season
    user_id INT NOT NULL,
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    PRIMARY KEY (season_id, cbs_entry_id)
);

-- Point-in-time odds snapshots (spread/total lines), one row per
-- game+source+bookmaker+market+capture. Repeated captures over time let
-- opening/closing lines and best/worst available line be derived with
-- MIN/MAX over captured_at or over home_point/away_point, rather than
-- needing dedicated columns for each. game_id is resolved by the loader
-- via games.cbs_event_id / sports_io_game_id / odds_api_event_id before
-- insert - those are the join keys for their respective sources.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    odds_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INT NOT NULL,
    source VARCHAR(20) NOT NULL, -- 'cbs', 'the_odds_api', 'sports_io'
    bookmaker VARCHAR(50), -- e.g. 'draftkings' for the_odds_api
    market VARCHAR(20) NOT NULL, -- 'spread', 'total', 'moneyline' - home_point/away_point are NULL for moneyline (no line, just a price)
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    home_point DECIMAL(4,1), -- for market='total', represents the Over line
    home_price INT, -- American odds
    away_point DECIMAL(4,1), -- for market='total', represents the Under line
    away_price INT,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE INDEX IF NOT EXISTS idx_odds_snapshots_game_source_market ON odds_snapshots(game_id, source, market);

-- Small key-value store for src/orchestration.py's polling cursors
-- (sports_io_live_last_poll_at, cbs_live_last_poll_at, odds_last_call_at,
-- deadline_last_synced_sunday, housekeeping_last_run_at) - lets a stateless
-- cron tick know what it last did without re-deriving it from other tables.
CREATE TABLE IF NOT EXISTS orchestration_state (
    key VARCHAR(50) PRIMARY KEY,
    value VARCHAR(255),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER IF NOT EXISTS trg_orchestration_state_updated_at
AFTER UPDATE ON orchestration_state
BEGIN
    UPDATE orchestration_state SET updated_at = CURRENT_TIMESTAMP WHERE key = OLD.key;
END;

-- Tracks lookup misses in the loaders (a mapper couldn't resolve a raw
-- external value to an internal id) so unmapped values can be reviewed
-- and turned into a correction table entry instead of silently skipping
-- rows forever. Written next to the existing logger.warning() at each
-- lookup-miss site, not instead of it.
CREATE TABLE IF NOT EXISTS mapping_gaps (
    mapping_gap_id INTEGER PRIMARY KEY,
    source VARCHAR(20) NOT NULL,
    entity_type VARCHAR(20) NOT NULL,
    raw_value VARCHAR(255),
    context VARCHAR(255),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source, entity_type, raw_value)
);

-- Structured failures worth an admin's attention, same shape/intent as
-- mapping_gaps above but for real exceptions (e.g. a failed odds capture)
-- rather than lookup misses. Written next to the existing logger.exception()
-- at each catch site, not instead of it - the log has the full traceback for
-- debugging, this table is the queryable "is anything broken" summary
-- src/kv_writer.py's meta:admin key surfaces.
CREATE TABLE IF NOT EXISTS system_events (
    system_event_id INTEGER PRIMARY KEY,
    source VARCHAR(50) NOT NULL,
    message VARCHAR(500) NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source, message)
);

-- Simple indexes for performance
CREATE INDEX IF NOT EXISTS idx_games_week ON games(week_id);
CREATE INDEX IF NOT EXISTS idx_games_complete ON games(is_complete);
CREATE INDEX IF NOT EXISTS idx_game_snapshots_game ON game_snapshots(game_id);
CREATE INDEX IF NOT EXISTS idx_game_team_stats_game ON game_team_stats(game_id);
CREATE INDEX IF NOT EXISTS idx_picks_user ON user_picks(user_id);
CREATE INDEX IF NOT EXISTS idx_picks_game ON user_picks(game_id);
CREATE INDEX IF NOT EXISTS idx_weekly_performance_week ON weekly_performance(week_id);
