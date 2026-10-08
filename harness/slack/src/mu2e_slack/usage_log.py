"""Per-thread usage logging.

One record per Slack thread and turn, holding the thread's running totals, in
the shape of the shared table ``usage.sessions`` (``harness/usage/sessions.sql``)
that the goose and claude-code harnesses feed as well. ``interface`` tells
them apart.

Nothing that identifies a person or a conversation is recorded: no Slack
user, no channel or thread, no content -- counts, timings, model and tool
names only. ``session_id`` is a hash of channel and thread, so a thread's
turns still land on one row without pointing back at it.

Where it goes:
    LOG_OUTPUT   jsonl file, always written (one line per thread, rewritten
                 in place) -- the local record, and the fallback.
    LOG_PG_DSN   if set, the same record is also appended to Postgres
                 (LOG_PG_TABLE, default usage.sessions; the table is
                 insert-only, usage.sessions_current has the latest row per
                 session), e.g.
                 "postgresql://ifdb11:5477/mu2e_ai_prd" with Kerberos
                 (KRB5CCNAME) supplying the credential. A database failure is
                 logged and never costs the user their answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import version

log = logging.getLogger(__name__)


INTERFACE = "slack"

# Column order of usage.sessions; also the key order of the jsonl records.
COLUMNS = (
    "session_id", "interface", "harness_version", "host", "logged_at",
    "session_created_at", "session_updated_at", "provider", "endpoint_url", "model",
    "turns", "llm_calls", "tool_calls", "tool_breakdown",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "thinking_tokens",
)

# Up to --max-concurrent turns finish at once and each records itself from a
# worker thread; the jsonl rewrite is read-modify-replace, so serialize it.
_file_lock = threading.Lock()


def session_id(channel: str, thread_ts: str) -> str:
    raw = f"{channel}-{thread_ts}"
    return "slack-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def provider_from_endpoint(url: str | None) -> str:
    """"litellm" for https://litellm.fnal.gov/v1/, "vllm" for vllm.fnal.gov:
    the bot speaks the OpenAI protocol to all of them, so the protocol says
    nothing; the gateway does."""
    host = urlparse(url or "").hostname or ""
    return host.split(".")[0] or "unknown"


def _harness_commit() -> str:
    """The aitools commit this code came from -- same meaning as the `harness`
    field in the other two harnesses, which read it with `git rev-parse`.

    A deployed release has no checkout to ask (uv installs the package, not the
    source tree), but setuptools_scm bakes the commit into the version as
    "0.7.3.dev2+g01cf501f0", so recover it from there and fall back to the
    checkout only for local development.
    """
    match = re.search(r"\+g([0-9a-f]+)", version())
    if match:
        # Trimmed to 7 characters because that is what `git rev-parse --short`
        # gives the other two harnesses (setuptools_scm embeds 9). Same commit
        # written two different widths would not group in SQL.
        return match.group(1)[:7]
    try:
        return subprocess.check_output(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return ""


def _iso(value) -> str | None:
    if value is None:
        return None  # a NULL in the table, not the string "None"
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def build_record(snapshot: dict, channel: str, thread_ts: str) -> dict:
    """``snapshot`` is Conversation.usage_snapshot() -- see backend.py."""
    s = snapshot
    return {
        "session_id":         session_id(channel, thread_ts),
        "interface":          INTERFACE,
        "harness_version":    _harness_commit(),
        "host":               socket.gethostname(),
        "logged_at":          datetime.now(timezone.utc).isoformat(),
        "session_created_at": _iso(s.get("created_at")),
        "session_updated_at": _iso(s.get("updated_at")),
        "provider":           s.get("provider") or provider_from_endpoint(s.get("endpoint_url")),
        "endpoint_url":       s.get("endpoint_url"),
        "model":              s.get("model"),
        "turns":              s.get("turns", 0),
        "llm_calls":          s.get("llm_calls", 0),
        "tool_calls":         s.get("tool_calls", 0),
        "tool_breakdown":     s.get("tool_breakdown", {}),
        "input_tokens":       s.get("input_tokens", 0),
        "output_tokens":      s.get("output_tokens", 0),
        "cache_read_tokens":  s.get("cache_read_tokens", 0),
        "cache_write_tokens": s.get("cache_write_tokens", 0),
        "thinking_tokens":    s.get("thinking_tokens", 0),
    }


def write(record: dict, path: str) -> None:
    """Upsert one JSON line keyed by session_id.

    Called after every turn, so a thread occupies exactly one line however long
    it runs. Write-to-tmp-then-rename, so a crash mid-write cannot corrupt the
    log -- same approach as the other two harnesses.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    new_line = json.dumps(record, default=str)

    lines: list[str] = []
    replaced = False
    if out.exists():
        with open(out) as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    existing = json.loads(stripped)
                except json.JSONDecodeError:
                    lines.append(stripped + "\n")
                    continue
                if existing.get("session_id") == record["session_id"]:
                    lines.append(new_line + "\n")
                    replaced = True
                else:
                    lines.append(stripped + "\n")

    if not replaced:
        lines.append(new_line + "\n")

    tmp = out.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        fh.writelines(lines)
    os.chmod(tmp, 0o600)
    tmp.replace(out)


def _table(name: str):
    from psycopg import sql
    return sql.Identifier(*name.split("."))


def insert(conn, record: dict, table: str = "usage.sessions") -> None:
    """Append one row. The table is insert-only (writers have no UPDATE), so
    a session is a series of rows of running totals; a retried write of the
    same (session_id, logged_at) is a no-op."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    query = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES ({vals}) "
        "ON CONFLICT (session_id, logged_at) DO NOTHING"
    ).format(
        table=_table(table),
        cols=sql.SQL(", ").join(map(sql.Identifier, COLUMNS)),
        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in COLUMNS),
    )
    params = {c: record.get(c) for c in COLUMNS}
    params["tool_breakdown"] = Jsonb(params["tool_breakdown"] or {})
    with conn.cursor() as cur:
        cur.execute(query, params)


def write_postgres(record: dict, dsn: str, table: str) -> None:
    import psycopg
    # gssencmode=prefer explicitly: psycopg warns that its binary build may
    # default to disable, and Kerberos is the only credential we have.
    with psycopg.connect(dsn, connect_timeout=10, gssencmode="prefer") as conn:
        insert(conn, record, table)


def _count_query(table: str):
    from psycopg import sql
    return sql.SQL("SELECT count(DISTINCT session_id) FROM {t} WHERE interface = {i}").format(
        t=_table(table), i=sql.Literal(INTERFACE))


def check_postgres(dsn: str, table: str) -> str:
    """For --check: can this account reach the table and write to it?
    Writes nothing."""
    import psycopg
    with psycopg.connect(dsn, connect_timeout=10, gssencmode="prefer") as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_user, to_regclass(%s) IS NOT NULL", (table,))
        user, exists = cur.fetchone()
        if not exists:
            raise RuntimeError(f"connected as {user}, but {table} does not exist "
                               "(create it with harness/usage/sessions.sql)")
        cur.execute("SELECT has_table_privilege(%s, 'INSERT')", (table,))
        if not cur.fetchone()[0]:
            raise RuntimeError(f"connected as {user}, but it may not INSERT into {table}")
        cur.execute(_count_query(table))
        return f"connected as {user}; {table} writable, {cur.fetchone()[0]} slack thread(s) so far"


def record_turn(conv, cfg, channel: str, thread_ts: str) -> None:
    """Best effort: a logging failure must never cost the user their answer.
    Blocking (file and database I/O) -- call it from a worker thread.
    Backends that keep their own usage log return None from usage_snapshot()
    and are skipped here."""
    try:
        snapshot = conv.usage_snapshot()
        if snapshot is None:
            return
        record = build_record(snapshot, channel, thread_ts)
    except Exception:
        log.exception("Failed to build usage record")
        return
    try:
        with _file_lock:
            write(record, cfg.log_output)
    except Exception:
        log.exception("Failed to write usage record to %s", cfg.log_output)
    dsn = getattr(cfg, "pg_dsn", "")
    if dsn:
        try:
            write_postgres(record, dsn, cfg.pg_table)
        except Exception as exc:  # noqa: BLE001 -- the file has it; say why the DB does not
            log.warning("Usage record not written to Postgres (%s): %s", cfg.pg_table, exc)
