#!/usr/bin/env python3
"""
log-session.py  --  extract per-session usage summary from goose's SQLite
                        and write it to a flat JSON-lines file (or, later, PostgreSQL).

Usage:
    log-session.py [SESSION_ID]

    SESSION_ID  The goose session id to harvest (e.g. "20260915_2").
                Defaults to the most-recently-updated session in the DB.

Output backends (selected by env vars or flags -- see CONFIGURATION below):

    FILE (default)
        Appends one JSON line per session to $LOG_OUTPUT
        (default: $XDG_DATA_HOME/goose/log/usage.jsonl, i.e. next to
        sessions.db in the same group-storage tree).

    POSTGRESQL (future -- uncomment the psycopg2 block below)
        Connects via Kerberos GSSAPI (no password): set LOG_PG_DSN to a
        libpq connection string, e.g.:
          LOG_PG_DSN="host=mu2edb.fnal.gov dbname=mu2eai"
        The invoking user must have a valid Kerberos ticket; the DB must have
        a pg_hba.conf entry allowing gss auth from this host.

Privacy:
    Set LOG_PRIVACY=1 to omit fields that identify a specific person:
    user, session_name, working_dir are suppressed; all other fields
    (host, tokens, model, etc.) are kept.

Schema (one row / JSON object per session):
    session_id          TEXT    goose session id ("20260915_2")
    session_name        TEXT    auto-generated or user-set name  [omitted if LOG_PRIVACY=1]
    user                TEXT    Unix username                    [omitted if LOG_PRIVACY=1]
    working_dir         TEXT    cwd goose was launched from      [omitted if LOG_PRIVACY=1]
    host                TEXT    hostname
    logged_at           TEXT    ISO-8601 UTC timestamp of this log run
    session_created_at  TEXT    when the session was first created
    session_updated_at  TEXT    when it was last updated (proxy for end time)
    provider            TEXT    goose provider name ("litellm", "openai", ...)
    endpoint_url        TEXT    OPENAI_BASE_URL from config.yaml (the actual API endpoint)
    model               TEXT    model identifier ("azure/claude-sonnet-4-6")
    harness     TEXT    git commit of the aitools repo (best-effort)
    turns               INT     number of user turns (user-role messages)
    llm_calls           INT     number of individual LLM API calls
    tool_calls          INT     total tool invocations (all types)
    tool_breakdown      OBJECT  {tool_name: count, ...}
    input_tokens        INT     accumulated input tokens for the whole session
    output_tokens       INT     accumulated output tokens
    cache_read_tokens   INT     prompt-cache hits (Claude)
    cache_write_tokens  INT     prompt-cache writes (Claude)
    is_subagent         BOOL    true if this was a delegated subagent session
"""

import json
import os
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

def _default_db() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    return Path(xdg) / "goose/sessions/sessions.db"


def _default_output() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    return Path(xdg) / "goose/log/usage.jsonl"


DB_PATH      = Path(os.environ.get("LOG_DB",      _default_db()))
OUTPUT_FILE  = Path(os.environ.get("LOG_OUTPUT",  _default_output()))
PG_DSN       = os.environ.get("LOG_PG_DSN", "")   # empty → use file backend
PRIVACY      = os.environ.get("LOG_PRIVACY", "").strip() not in ("", "0", "false", "no")


# ---------------------------------------------------------------------------
# DATA EXTRACTION
# ---------------------------------------------------------------------------

def fetch_session(db: sqlite3.Connection, session_id: str) -> dict:
    """Pull everything we need from the SQLite DB for one session."""

    # -- sessions table -------------------------------------------------------
    row = db.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Session not found: {session_id!r}")

    s = dict(row)  # sqlite3.Row → plain dict

    # -- usage_ledger: aggregate across all LLM calls -------------------------
    ledger = db.execute("""
        SELECT
            COUNT(*)              AS llm_calls,
            SUM(input_tokens)     AS input_tokens,
            SUM(output_tokens)    AS output_tokens,
            SUM(cache_read_tokens)  AS cache_read_tokens,
            SUM(cache_write_tokens) AS cache_write_tokens,
            SUM(cost)             AS cost_usd   -- kept for ledger reconciliation if needed
        FROM usage_ledger
        WHERE session_id = ?
    """, (session_id,)).fetchone()

    # -- messages: count user turns and tool calls ----------------------------
    msg_rows = db.execute(
        "SELECT role, content_json FROM messages WHERE session_id = ?",
        (session_id,)
    ).fetchall()

    turns = 0
    tool_breakdown: dict[str, int] = {}

    for msg in msg_rows:
        role = msg["role"]
        if role == "user":
            turns += 1
        elif role == "assistant":
            try:
                content = json.loads(msg["content_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(content, list):
                content = [content]
            for item in content:
                if isinstance(item, dict) and item.get("type") == "toolRequest":
                    name = (item
                            .get("toolCall", {})
                            .get("value", {})
                            .get("name", "unknown"))
                    tool_breakdown[name] = tool_breakdown.get(name, 0) + 1

    tool_calls = sum(tool_breakdown.values())

    # -- model name from model_config_json ------------------------------------
    model = ""
    try:
        mcj = json.loads(s.get("model_config_json") or "{}")
        model = mcj.get("model_name", "")
    except (json.JSONDecodeError, TypeError):
        pass

    # -- endpoint URL: read from goose's live config.yaml ---------------------
    # Not stored in SQLite; lives in $XDG_CONFIG_HOME/goose/config.yaml as
    # OPENAI_BASE_URL (used by both the openai and litellm provider slots).
    endpoint_url = ""
    try:
        import yaml  # PyYAML -- available in most Python envs
        xdg_config = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        config_yaml = Path(xdg_config) / "goose/config.yaml"
        if config_yaml.exists():
            with open(config_yaml) as fh:
                cfg = yaml.safe_load(fh) or {}
            endpoint_url = cfg.get("OPENAI_BASE_URL", "")
    except Exception:
        pass  # yaml not installed or file unreadable -- non-fatal

    # -- harness version: git commit of the aitools repo (best-effort) --------
    harness = ""
    try:
        import subprocess
        # The harness lives inside the aitools git repo. Find it relative to
        # this script's own location so it works regardless of checkout path.
        script_dir = Path(__file__).resolve().parent
        git_dir = script_dir
        while git_dir != git_dir.parent:
            if (git_dir / ".git").exists():
                break
            git_dir = git_dir.parent
        if (git_dir / ".git").exists():
            env = {**os.environ, "HOME": str(Path.home().parent / os.environ.get("USER", ""))}
            result = subprocess.run(
                ["git", "-C", str(git_dir), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5, env=env
            )
            harness = result.stdout.strip()
    except Exception:
        pass

    record: dict = {
        "session_id":         s["id"],
        "host":               socket.gethostname(),
        "logged_at":          datetime.now(timezone.utc).isoformat(),
        "session_created_at": s.get("created_at", ""),
        "session_updated_at": s.get("updated_at", ""),
        "provider":           s.get("provider_name", ""),
        "endpoint_url":       endpoint_url,
        "model":              model,
        "harness":    harness,
        "turns":              turns,
        "llm_calls":          int(ledger["llm_calls"] or 0),
        "tool_calls":         tool_calls,
        "tool_breakdown":     tool_breakdown,
        "input_tokens":       int(s.get("accumulated_input_tokens") or 0),
        "output_tokens":      int(s.get("accumulated_output_tokens") or 0),
        "cache_read_tokens":  int(s.get("accumulated_cache_read_tokens") or 0),
        "cache_write_tokens": int(s.get("accumulated_cache_write_tokens") or 0),
        "is_subagent":        s.get("session_type") != "user",
    }

    # Fields suppressed under LOG_PRIVACY=1 (identify a specific person)
    if not PRIVACY:
        record["user"]         = os.environ.get("USER", os.environ.get("LOGNAME", ""))
        record["session_name"] = s.get("name", "")
        record["working_dir"]  = s.get("working_dir", "")

    return record


# ---------------------------------------------------------------------------
# OUTPUT BACKENDS
# ---------------------------------------------------------------------------

def write_file(record: dict) -> str:
    """Upsert one JSON line in OUTPUT_FILE keyed by session_id.

    If a line for this session_id already exists (from a previous turn's
    UserPromptSubmit call) it is replaced in-place with the latest totals.
    Otherwise the record is appended.  The file stays at one line per
    session regardless of how many times this is called during a session.

    Uses a write-to-tmp-then-rename pattern so a crash mid-write never
    corrupts the existing log.
    """
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    session_id = record["session_id"]
    new_line = json.dumps(record, default=str)

    existing: list[str] = []
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as fh:
            existing = fh.readlines()

    updated = False
    out_lines: list[str] = []
    for line in existing:
        stripped = line.rstrip("\n")
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            out_lines.append(line)
            continue
        if obj.get("session_id") == session_id:
            out_lines.append(new_line + "\n")
            updated = True
        else:
            out_lines.append(line if line.endswith("\n") else line + "\n")

    if not updated:
        out_lines.append(new_line + "\n")

    # Atomic replace via tmp file in the same directory.
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        fh.writelines(out_lines)
    tmp.replace(OUTPUT_FILE)

    return str(OUTPUT_FILE)


def write_postgres(record: dict) -> None:
    """
    Insert one row into PostgreSQL via Kerberos GSSAPI (no password).

    Requires:
      - psycopg2 installed (pip install psycopg2-binary)
      - LOG_PG_DSN env var set, e.g.:
          host=mu2edb.fnal.gov dbname=mu2eai
      - A valid Kerberos ticket (kinit) in the shell that calls this script.
        NOTE: if goose() in env.sh is used, KRB5CCNAME is scrubbed for goose
        itself but NOT for external scripts called by the user -- so calling
        this script by hand or from a SessionStop hook that runs outside
        goose's subprocess is fine.
      - pg_hba.conf on the server allowing: host mu2eai <user> <cidr> gss

    PostgreSQL table DDL (run once on the server):

        CREATE TABLE goose_usage (
            id                  SERIAL PRIMARY KEY,
            session_id          TEXT        NOT NULL,
            session_name        TEXT,
            "user"              TEXT,
            host                TEXT,
            logged_at           TIMESTAMPTZ,
            session_created_at  TIMESTAMPTZ,
            session_updated_at  TIMESTAMPTZ,
            working_dir         TEXT,
            provider            TEXT,
            endpoint_url        TEXT,
            model               TEXT,
            goose_version       TEXT,
            turns               INTEGER,
            llm_calls           INTEGER,
            tool_calls          INTEGER,
            tool_breakdown      JSONB,
            input_tokens        BIGINT,
            output_tokens       BIGINT,
            cache_read_tokens   BIGINT,
            cache_write_tokens  BIGINT,
            is_subagent         BOOLEAN,
            UNIQUE (session_id, logged_at)
        );
        CREATE INDEX ON goose_usage (session_id);
        CREATE INDEX ON goose_usage ("user");
        CREATE INDEX ON goose_usage (session_updated_at);
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("ERROR: psycopg2 not installed -- cannot write to PostgreSQL", file=sys.stderr)
        print("       pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(PG_DSN, sslmode="require")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO goose_usage (
                        session_id, session_name, "user", host,
                        logged_at, session_created_at, session_updated_at,
                        working_dir, provider, endpoint_url, model, goose_version,
                        turns, llm_calls, tool_calls, tool_breakdown,
                        input_tokens, output_tokens,
                        cache_read_tokens, cache_write_tokens,
                        is_subagent
                    ) VALUES (
                        %(session_id)s, %(session_name)s, %(user)s, %(host)s,
                        %(logged_at)s, %(session_created_at)s, %(session_updated_at)s,
                        %(working_dir)s, %(provider)s, %(endpoint_url)s, %(model)s,
                        %(goose_version)s,
                        %(turns)s, %(llm_calls)s, %(tool_calls)s,
                        %(tool_breakdown)s,
                        %(input_tokens)s, %(output_tokens)s,
                        %(cache_read_tokens)s, %(cache_write_tokens)s,
                        %(is_subagent)s
                    )
                    ON CONFLICT (session_id, logged_at) DO NOTHING
                """, {**record,
                      "tool_breakdown": psycopg2.extras.Json(record["tool_breakdown"])})
    finally:
        conn.close()
        conn.close()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    session_id = sys.argv[1] if len(sys.argv) > 1 else None

    if not DB_PATH.exists():
        print(f"ERROR: goose sessions DB not found: {DB_PATH}", file=sys.stderr)
        print("       Source env.sh first, or set LOG_DB.", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    if session_id is None:
        row = db.execute(
            "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("ERROR: no sessions found in DB", file=sys.stderr)
            sys.exit(1)
        session_id = row["id"]
        print(f"No session ID given -- using most recent: {session_id}")

    record = fetch_session(db, session_id)
    db.close()

    # Pretty-print what we collected
    print(json.dumps(record, indent=2, default=str))
    print()

    # Write to the configured backend
    if PG_DSN:
        write_postgres(record)
        print(f"Written to PostgreSQL ({PG_DSN.split()[0]}...)")
    else:
        path = write_file(record)
        print(f"Appended to: {path}")


if __name__ == "__main__":
    main()
