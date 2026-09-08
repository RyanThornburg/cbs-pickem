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
    medium_name VARCHAR(50), -- CBS's market/brand name, e.g. 'Arizona' — not always the same as city (Arizona Cardinals play in Glendale)
    nick_name VARCHAR(50), -- e.g. 'Cardinals'
    color_primary_hex VARCHAR(6),
    color_secondary_hex VARCHAR(6)
);

-- Stadiums and venues (including international)
CREATE TABLE IF NOT EXISTS stadiums (
    stadium_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    city VARCHAR(100) NOT NULL,
    state VARCHAR(50),
    country VARCHAR(50) DEFAULT 'USA',
    surface_type VARCHAR(50), -- 'Grass', 'Artificial Turf'
    roof_type VARCHAR(50) -- 'Open', 'Dome', 'Retractable'
);

-- Seasons
CREATE TABLE IF NOT EXISTS seasons (
    season_id INTEGER PRIMARY KEY, -- the season's year (e.g. 2026), not autoincremented
    name VARCHAR(50),
    start_date DATE,
    end_date DATE,
    is_active BOOLEAN DEFAULT FALSE
);

-- Weeks within a season
CREATE TABLE IF NOT EXISTS weeks (
    week_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INT NOT NULL,
    week_number INT NOT NULL,
    name VARCHAR(50), -- "Week 1", "Wild Card", etc.
    start_date DATE,
    end_date DATE,
    is_complete BOOLEAN DEFAULT FALSE,
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
    cbs_event_id INT UNIQUE, -- CBS Sports event ID, for idempotent upserts on re-scrape
    sports_io_game_id INT UNIQUE, -- Sports IO game ID, for joining stats/odds by game
    odds_api_event_id VARCHAR(50) UNIQUE, -- The Odds API event ID (string, not int) — matched by team+commence_time on first sighting, then joined on directly
    game_time DATETIME,
    cbs_spread DECIMAL(4,1), -- the line CBS locked in for scoring picks
    home_score INT,
    away_score INT,
    is_complete BOOLEAN DEFAULT FALSE,
    is_international BOOLEAN DEFAULT FALSE,
    tv_network VARCHAR(50),
    gametracker_url VARCHAR(255),
    status_desc VARCHAR(30), -- 'SCHEDULED', 'IN_PROGRESS', 'HALFTIME', 'FINAL', etc.
    FOREIGN KEY (week_id) REFERENCES weeks(week_id),
    FOREIGN KEY (home_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (away_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (stadium_id) REFERENCES stadiums(stadium_id)
);

-- Periodic in-game snapshots (score/weather)
CREATE TABLE IF NOT EXISTS game_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INT NOT NULL,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    quarter INT, -- 1-4, 5=OT
    status_desc VARCHAR(30), -- CBS raw status at time of capture
    possession VARCHAR(10), -- 'HOME', 'AWAY'
    home_score INT,
    away_score INT,
    temperature_f INT,
    weather_condition VARCHAR(50), -- 'Clear', 'Rain', 'Snow', etc.
    wind_speed_mph INT,
    wind_direction VARCHAR(10),
    precipitation_pct INT,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Team box-score stats for a game (one row per team per game)
CREATE TABLE IF NOT EXISTS game_team_stats (
    stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INT NOT NULL,
    team_id INT NOT NULL,
    total_yards INT,
    passing_yards INT,
    rushing_yards INT,
    turnovers INT,
    time_of_possession_sec INT,
    penalties INT,
    penalty_yards INT,
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

-- Season-long user statistics
CREATE TABLE IF NOT EXISTS user_stats (
    stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INT NOT NULL,
    season_id INT NOT NULL,
    total_picks INT DEFAULT 0,
    total_correct INT DEFAULT 0,
    accuracy_pct DECIMAL(5,2) DEFAULT 0,
    current_streak INT DEFAULT 0,
    current_streak_type VARCHAR(10) DEFAULT 'NONE', -- 'WIN', 'LOSS', 'NONE'
    longest_win_streak INT DEFAULT 0,
    home_team_picks INT DEFAULT 0,
    away_team_picks INT DEFAULT 0,
    favorite_picks INT DEFAULT 0,
    underdog_picks INT DEFAULT 0,
    best_week_score INT DEFAULT 0,
    worst_week_score INT DEFAULT 5,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    UNIQUE (user_id, season_id)
);

CREATE TRIGGER IF NOT EXISTS trg_user_stats_updated_at
AFTER UPDATE ON user_stats
BEGIN
    UPDATE user_stats SET updated_at = CURRENT_TIMESTAMP WHERE stat_id = OLD.stat_id;
END;

-- Point-in-time odds snapshots (spread/total lines), one row per
-- game+source+bookmaker+market+capture. Repeated captures over time let
-- opening/closing lines and best/worst available line be derived with
-- MIN/MAX over captured_at or over home_point/away_point, rather than
-- needing dedicated columns for each. game_id is resolved by the loader
-- via games.cbs_event_id / sports_io_game_id / odds_api_event_id before
-- insert — those are the join keys for their respective sources.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    odds_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INT NOT NULL,
    source VARCHAR(20) NOT NULL, -- 'cbs', 'the_odds_api', 'sports_io'
    bookmaker VARCHAR(50), -- e.g. 'draftkings' for the_odds_api
    market VARCHAR(20) NOT NULL, -- 'spread', 'total'
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    home_point DECIMAL(4,1), -- for market='total', represents the Over line
    home_price INT, -- American odds
    away_point DECIMAL(4,1), -- for market='total', represents the Under line
    away_price INT,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE INDEX IF NOT EXISTS idx_odds_snapshots_game_source_market ON odds_snapshots(game_id, source, market);

-- Simple indexes for performance
CREATE INDEX IF NOT EXISTS idx_games_week ON games(week_id);
CREATE INDEX IF NOT EXISTS idx_games_complete ON games(is_complete);
CREATE INDEX IF NOT EXISTS idx_game_snapshots_game ON game_snapshots(game_id);
CREATE INDEX IF NOT EXISTS idx_game_team_stats_game ON game_team_stats(game_id);
CREATE INDEX IF NOT EXISTS idx_picks_user ON user_picks(user_id);
CREATE INDEX IF NOT EXISTS idx_picks_game ON user_picks(game_id);
CREATE INDEX IF NOT EXISTS idx_weekly_performance_week ON weekly_performance(week_id);
CREATE INDEX IF NOT EXISTS idx_user_stats_season ON user_stats(season_id);
