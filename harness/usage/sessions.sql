-- Shared usage table for the Mu2e AI harnesses (slack bot, goose, claude-code).
--
-- Append-only, like the memory schema: a harness inserts a row after every
-- turn holding the session's running totals so far, and never updates or
-- deletes one. usage.sessions_current shows the latest row per session --
-- query that, not the table, or totals are counted once per turn. Counts, timings,
-- model and tool names only: no user, no session name, no working directory,
-- no conversation content. session_id is opaque (the Slack bot hashes its
-- channel/thread).
--
-- Run once, by a member of admin_role (owner of the other schemas here):
--   psql "host=ifdb11 port=5477 dbname=mu2e_ai_prd" -f sessions.sql
-- Safe to re-run.
--
-- Writers (mu2eai, via update_role) can insert and read, nothing else --
-- the same grants update_role has on the memory tables.

SET ROLE admin_role;

CREATE SCHEMA IF NOT EXISTS usage AUTHORIZATION admin_role;

CREATE TABLE IF NOT EXISTS usage.sessions (
    session_id          TEXT        NOT NULL,
    interface           TEXT        NOT NULL,   -- slack | goose | claude-code
    harness_version     TEXT,                   -- aitools commit of the harness
    host                TEXT,
    logged_at           TIMESTAMPTZ NOT NULL,   -- when this row was written
    session_created_at  TIMESTAMPTZ,
    session_updated_at  TIMESTAMPTZ,
    provider            TEXT,                   -- litellm | vllm | ...
    endpoint_url        TEXT,
    model               TEXT,
    turns               INTEGER,
    llm_calls           INTEGER,
    tool_calls          INTEGER,
    tool_breakdown      JSONB,                  -- {"kb__kb_search": 2, ...}
    input_tokens        BIGINT,
    output_tokens       BIGINT,
    cache_read_tokens   BIGINT,
    cache_write_tokens  BIGINT,
    thinking_tokens     BIGINT,
    PRIMARY KEY (session_id, logged_at)
);

CREATE INDEX IF NOT EXISTS sessions_interface_updated ON usage.sessions (interface, session_updated_at);
CREATE INDEX IF NOT EXISTS sessions_model ON usage.sessions (model);

-- One row per session: its latest totals. This is what to sum over.
CREATE OR REPLACE VIEW usage.sessions_current AS
    SELECT DISTINCT ON (session_id) *
    FROM usage.sessions
    ORDER BY session_id, logged_at DESC;

GRANT USAGE ON SCHEMA usage TO update_role;
GRANT SELECT, INSERT ON usage.sessions TO update_role;
GRANT SELECT ON usage.sessions_current TO update_role;

RESET ROLE;
