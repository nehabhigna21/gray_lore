-- Graylore OTT Platform — Full Schema (PostgreSQL)
--
-- Create the database once, then load this file into it:
--     createdb graylore
--     psql graylore -f schema.sql
--     psql graylore -f triggers.sql
--     psql graylore -f seed_data.sql

DROP TABLE IF EXISTS Payouts      CASCADE;
DROP TABLE IF EXISTS Payments     CASCADE;
DROP TABLE IF EXISTS Watchlist    CASCADE;
DROP TABLE IF EXISTS Ratings      CASCADE;
DROP TABLE IF EXISTS WatchHistory CASCADE;
DROP TABLE IF EXISTS Sessions     CASCADE;
DROP TABLE IF EXISTS Licenses     CASCADE;
DROP TABLE IF EXISTS ContentMoods CASCADE;
DROP TABLE IF EXISTS Moods        CASCADE;
DROP TABLE IF EXISTS Profiles     CASCADE;
DROP TABLE IF EXISTS Subscriptions CASCADE;
DROP TABLE IF EXISTS Content      CASCADE;
DROP TABLE IF EXISTS Users        CASCADE;

CREATE TABLE Users (
    user_id       SERIAL PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    email         VARCHAR(150) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE Subscriptions (
    sub_id       SERIAL PRIMARY KEY,
    user_id      INT NOT NULL REFERENCES Users(user_id) ON DELETE CASCADE,
    plan_type    VARCHAR(20) NOT NULL,            -- Basic / Standard / Premium
    device_limit INT NOT NULL,
    start_date   DATE NOT NULL,
    end_date     DATE,
    status       VARCHAR(20) DEFAULT 'active'     -- active / expired / cancelled
);

CREATE TABLE Profiles (
    profile_id   SERIAL PRIMARY KEY,
    user_id      INT NOT NULL REFERENCES Users(user_id) ON DELETE CASCADE,
    profile_name VARCHAR(50) NOT NULL,
    is_kids      BOOLEAN DEFAULT FALSE
);

CREATE TABLE Content (
    content_id   SERIAL PRIMARY KEY,
    title        VARCHAR(150) NOT NULL,
    genre        VARCHAR(50),
    language     VARCHAR(30) DEFAULT 'English',
    age_rating   VARCHAR(10) DEFAULT 'U',          -- U / 13+ / 16+ / 18+
    release_date DATE,
    duration_min INT,
    -- VOD / metadata fields (populated by import_tmdb.py or the admin panel)
    tmdb_id      INT UNIQUE,                       -- TMDB movie id, NULL for hand-entered titles
    overview     TEXT,
    poster_url   VARCHAR(500),
    backdrop_url VARCHAR(500),
    trailer_key  VARCHAR(50),                      -- YouTube video key from TMDB
    video_url    VARCHAR(500),                     -- the stream the player actually plays (MP4 / HLS)
    pitch        TEXT                              -- one-line "how it feels" pitch, spoken by the Lore bot
);

-- Moods the recommendation bot can pick by ("make me laugh", "give me chills", ...)
CREATE TABLE Moods (
    mood_id SERIAL PRIMARY KEY,
    slug    VARCHAR(20) NOT NULL UNIQUE,
    label   VARCHAR(40) NOT NULL,
    emoji   VARCHAR(8),
    blurb   TEXT                                   -- what the bot says before naming the film
);

-- Many-to-many: which moods a title satisfies
CREATE TABLE ContentMoods (
    content_id INT NOT NULL REFERENCES Content(content_id) ON DELETE CASCADE,
    mood_id    INT NOT NULL REFERENCES Moods(mood_id) ON DELETE CASCADE,
    PRIMARY KEY (content_id, mood_id)
);

CREATE TABLE Licenses (
    license_id    SERIAL PRIMARY KEY,
    content_id    INT NOT NULL REFERENCES Content(content_id) ON DELETE CASCADE,
    region        VARCHAR(50) NOT NULL,
    license_start DATE NOT NULL,
    license_end   DATE NOT NULL,
    CHECK (license_end >= license_start)
);

-- One row per (user, device). is_active is flipped off by logout or by
-- trg_device_limit when the user exceeds their plan's device cap.
CREATE TABLE Sessions (
    session_id SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES Users(user_id) ON DELETE CASCADE,
    device_id  VARCHAR(100) NOT NULL,
    login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active  BOOLEAN DEFAULT TRUE
);

CREATE TABLE WatchHistory (
    watch_id      SERIAL PRIMARY KEY,
    profile_id    INT NOT NULL REFERENCES Profiles(profile_id) ON DELETE CASCADE,
    content_id    INT NOT NULL REFERENCES Content(content_id) ON DELETE CASCADE,
    watch_minutes INT DEFAULT 0,
    position_sec  INT DEFAULT 0,                   -- last playback position, used for "Resume from"
    watched_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE Ratings (
    rating_id  SERIAL PRIMARY KEY,
    profile_id INT NOT NULL REFERENCES Profiles(profile_id) ON DELETE CASCADE,
    content_id INT NOT NULL REFERENCES Content(content_id) ON DELETE CASCADE,
    rating     INT CHECK (rating BETWEEN 1 AND 5),
    review     TEXT,
    UNIQUE (profile_id, content_id)
);

-- "My List": titles a profile has saved to watch later
CREATE TABLE Watchlist (
    watchlist_id SERIAL PRIMARY KEY,
    profile_id   INT NOT NULL REFERENCES Profiles(profile_id) ON DELETE CASCADE,
    content_id   INT NOT NULL REFERENCES Content(content_id) ON DELETE CASCADE,
    added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (profile_id, content_id)
);

CREATE TABLE Payments (
    payment_id   SERIAL PRIMARY KEY,
    user_id      INT NOT NULL REFERENCES Users(user_id) ON DELETE CASCADE,
    sub_id       INT NOT NULL REFERENCES Subscriptions(sub_id) ON DELETE CASCADE,
    amount       DECIMAL(8,2) NOT NULL,
    payment_date DATE DEFAULT CURRENT_DATE,
    status       VARCHAR(20) DEFAULT 'paid'
);

-- Reporting table — intentionally outside strict 3NF (see project writeup).
-- Populated by refresh_payouts(period) in triggers.sql, not by the app's live queries.
CREATE TABLE Payouts (
    payout_id     SERIAL PRIMARY KEY,
    content_id    INT NOT NULL REFERENCES Content(content_id) ON DELETE CASCADE,
    total_minutes INT,
    rate_per_min  DECIMAL(6,4) DEFAULT 0.05,
    payout_amount DECIMAL(10,2),
    period        VARCHAR(20),
    UNIQUE (content_id, period)
);

-- Indexes for the hot query paths
CREATE INDEX idx_licenses_content_region ON Licenses(content_id, region);
CREATE INDEX idx_watch_profile_content   ON WatchHistory(profile_id, content_id, watched_at DESC);
CREATE INDEX idx_sessions_user_active    ON Sessions(user_id, is_active);
