-- Audit DB schema — TimescaleDB
-- Run once against database `audit` as user `audit`.
-- Idempotent where practical; drop+recreate is not supported, use a migration.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- -------------------------------------------------------------------
-- accounts
-- Every sock puppet, probe or study.
-- -------------------------------------------------------------------
CREATE TYPE account_role AS ENUM ('probe', 'study');
CREATE TYPE account_status AS ENUM ('active', 'suspended', 'retired');
CREATE TYPE account_gender AS ENUM ('M', 'F');

CREATE TABLE accounts (
    id                  TEXT PRIMARY KEY,                -- e.g. 'probe_01', 'study_m_03'
    email               TEXT NOT NULL,
    firefox_profile     TEXT NOT NULL,
    role                account_role NOT NULL,
    bucket              TEXT,                            -- probes: their single bucket; study: NULL
    assigned_interests  TEXT[],                          -- study: 2–3 bucket names; probes: NULL
    gender              account_gender,                  -- study: M/F (independent variable); probes: NULL
    status              account_status NOT NULL DEFAULT 'active',   -- can be removed dont think it used
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (role = 'probe' AND bucket IS NOT NULL AND assigned_interests IS NULL AND gender IS NULL) OR
        (role = 'study' AND bucket IS NULL AND assigned_interests IS NOT NULL AND gender IS NOT NULL)
    )
);

-- -------------------------------------------------------------------
-- sessions
-- One browser launch → one scroll run. ~30 min, ~40–50 posts.
-- Probes have NULL experiment_id.
-- -------------------------------------------------------------------
CREATE TYPE session_status AS ENUM ('running', 'completed', 'errored');

CREATE TABLE sessions (
    id                TEXT PRIMARY KEY,                  -- e.g. 'probe_01_20260417_143022'
    account_id        TEXT NOT NULL REFERENCES accounts(id),
    started_at        TIMESTAMPTZ NOT NULL,
    ended_at          TIMESTAMPTZ,
    planned_duration  INTERVAL,
    target_posts      INT,
    status            session_status NOT NULL DEFAULT 'running',
    final_stats       JSONB,                             -- from action_logger.log_session_end
    raw_archive_path  TEXT                               -- path to gzipped raw API responses for this session
);

CREATE INDEX idx_sessions_account_time ON sessions (account_id, started_at DESC);
-- -------------------------------------------------------------------
-- posts  (hypertable)
-- One row per (account, post, observation-time). If probe_02 sees post X twice, two rows.
-- Probes have NULL experiment_id. CLIP fields populated retroactively during calibration (C2).
-- -------------------------------------------------------------------
CREATE TABLE posts (
    id              BIGSERIAL,
    collected_at    TIMESTAMPTZ NOT NULL,
    account_id      TEXT NOT NULL REFERENCES accounts(id),
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    feed_position   INT NOT NULL,
    post_pk         TEXT NOT NULL,                       -- Instagram media pk/id
    post_link       TEXT,
    profile_name    TEXT,                                -- post author's Instagram username
    posted_at       TIMESTAMPTZ,                         -- when the post itself was created on Instagram
    caption         TEXT,                                -- post caption text, NULL if none
    like_count      INT,                                 -- raw integer like count
    is_suggested    BOOLEAN NOT NULL,
    is_following    BOOLEAN NOT NULL,
    clip_score      REAL,                                -- NULL until CLIP calibration/scoring
    clip_aligned    BOOLEAN,                             -- derived from clip_score + threshold
    clip_top_bucket TEXT,                                -- argmax CLIP category for the post
    vlm_scores      JSONB,                               -- full per-category softmax distribution
    post_data       JSONB NOT NULL,                      -- full parsed media object (everything else)
    PRIMARY KEY (id, collected_at)
);

SELECT create_hypertable('posts', 'collected_at', chunk_time_interval => INTERVAL '7 days');

CREATE INDEX idx_posts_account_time ON posts (account_id, collected_at DESC);
CREATE INDEX idx_posts_post_pk ON posts (post_pk);
CREATE INDEX idx_posts_suggested ON posts (is_suggested, collected_at DESC);
CREATE INDEX idx_posts_session ON posts (session_id);

-- -------------------------------------------------------------------
-- interactions  (hypertable)
-- All user actions from action_logger. Matches interaction_types:
-- scroll, pause, like, mouse_move, api_intercept, post_view,
-- session_start, session_end, error, back_scroll
-- api_intercept remains session-level summary (one row per session), not per-request.
-- -------------------------------------------------------------------
CREATE TABLE interactions (
    id                   BIGSERIAL,
    occurred_at          TIMESTAMPTZ NOT NULL,
    account_id           TEXT NOT NULL REFERENCES accounts(id),
    session_id           TEXT NOT NULL REFERENCES sessions(id),
    interaction_type     TEXT NOT NULL,
    post_observation_id  BIGINT,                         -- FK into posts.id; NULL for session_start, mouse_move, etc.
    details              JSONB,
    PRIMARY KEY (id, occurred_at)
);

SELECT create_hypertable('interactions', 'occurred_at', chunk_time_interval => INTERVAL '7 days');

CREATE INDEX idx_interactions_account_time ON interactions (account_id, occurred_at DESC);
CREATE INDEX idx_interactions_session ON interactions (session_id);
CREATE INDEX idx_interactions_type ON interactions (interaction_type, occurred_at DESC);
CREATE INDEX idx_interactions_post_obs ON interactions (post_observation_id) WHERE post_observation_id IS NOT NULL;

-- -------------------------------------------------------------------
-- Compression policies
-- Compress hypertable chunks older than 7 days. Bulk of the data is write-once.
-- -------------------------------------------------------------------
ALTER TABLE posts SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'account_id',
    timescaledb.compress_orderby = 'collected_at DESC'
);
SELECT add_compression_policy('posts', INTERVAL '7 days');

ALTER TABLE interactions SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'account_id',
    timescaledb.compress_orderby = 'occurred_at DESC'
);
SELECT add_compression_policy('interactions', INTERVAL '7 days');