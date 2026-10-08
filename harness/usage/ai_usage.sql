-- Shared usage table for the Mu2e AI harnesses (slack bot, goose, claude-code).
--
-- One row per session, upserted as the session goes on. Counts, timings,
-- model and tool names only: no user, no session name, no working directory,
-- no conversation content. session_id is opaque (the Slack bot hashes its
-- channel/thread).
--
-- Run once, by a member of admin_role (owner of the other schemas here):
--   psql "host=ifdb11 port=5477 dbname=mu2e_ai_prd" -f ai_usage.sql
-- Safe to re-run.
--
-- Writers (mu2eai, via update_role) can insert and update rows, not delete
-- them or change the table.

SET ROLE admin_role;

CREATE SCHEMA IF NOT EXISTS usage AUTHORIZATION admin_role;

CREATE TABLE IF NOT EXISTS usage.ai_usage (
    session_id          TEXT        PRIMARY KEY,
    interface           TEXT        NOT NULL,   -- slack | goose | claude-code
    harness_version     TEXT,                   -- aitools commit of the harness
    host                TEXT,
    logged_at           TIMESTAMPTZ NOT NULL,   -- last time this row was written
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
    thinking_tokens     BIGINT
);

CREATE INDEX IF NOT EXISTS ai_usage_interface_updated ON usage.ai_usage (interface, session_updated_at);
CREATE INDEX IF NOT EXISTS ai_usage_model ON usage.ai_usage (model);

GRANT USAGE ON SCHEMA usage TO update_role;
GRANT SELECT, INSERT, UPDATE ON usage.ai_usage TO update_role;

RESET ROLE;
