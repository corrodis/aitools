"""Per-thread usage logging.

Deliberately the same schema, field names, field order and env knobs
(LOG_OUTPUT / LOG_PRIVACY) as ../goose/log-session.py and
../claude-code/log-session-claude.py, so all three harnesses read as one
dataset and the PostgreSQL backend sketched in goose/log-session.py
(write_postgres, with its DDL) only has to be written once. The unit of a
"session" here is a Slack thread rather than a CLI session.

Nothing about the conversation's content is written: counts, timings, model
and tool names only.

Mapping of the shared fields onto Slack:
    session_id    "<channel>-<thread_ts>" -- hashed under LOG_PRIVACY, so it
                  still joins a thread's rows together without pointing at a
                  findable conversation.
    user          the Slack user id who opened the thread, not a Unix
                  username: the service account running this process is the
                  same for everyone and would say nothing.
    working_dir   "slack://<channel>/<thread_ts>" -- the closest analogue of
                  "where this session happened". Privacy-gated like the others.

Fields in goose/log-session.py that are omitted here (no equivalent), for the
same reason claude-code omits them:
    session_name  -- Slack threads have no name, only a timestamp
    is_subagent   -- nothing here delegates to subagents

Field kept from goose that claude-code omits:
    provider      -- we do have a named provider slot, as goose does
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import version

log = logging.getLogger(__name__)


def session_id(channel: str, thread_ts: str, privacy: bool) -> str:
    raw = f"{channel}-{thread_ts}"
    if not privacy:
        return raw
    return "slack-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


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


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def build_record(snapshot: dict, cfg, channel: str, thread_ts: str, user: str) -> dict:
    """``snapshot`` is Conversation.usage_snapshot() -- see backend.py."""
    s = snapshot
    record = {
        "session_id":         session_id(channel, thread_ts, cfg.privacy),
        "host":               socket.gethostname(),
        "logged_at":          datetime.now(timezone.utc).isoformat(),
        "session_created_at": _iso(s.get("created_at")),
        "session_updated_at": _iso(s.get("updated_at")),
        "provider":           s.get("provider", "openai"),
        "endpoint_url":       s.get("endpoint_url"),
        "model":              s.get("model"),
        "harness":            _harness_commit(),
        "turns":              s.get("turns", 0),
        "llm_calls":          s.get("llm_calls", 0),
        "tool_calls":         s.get("tool_calls", 0),
        "tool_breakdown":     s.get("tool_breakdown", {}),
        "input_tokens":       s.get("input_tokens", 0),
        "output_tokens":      s.get("output_tokens", 0),
        "cache_read_tokens":  s.get("cache_read_tokens", 0),
        "cache_write_tokens": s.get("cache_write_tokens", 0),
    }
    if not cfg.privacy:
        record["user"] = user
        record["working_dir"] = f"slack://{channel}/{thread_ts}"
    return record


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


def record_turn(conv, cfg, channel: str, thread_ts: str, user: str) -> None:
    """Best effort: a logging failure must never cost the user their answer.
    Backends that keep their own usage log return None from usage_snapshot()
    and are skipped here."""
    try:
        snapshot = conv.usage_snapshot()
        if snapshot is None:
            return
        write(build_record(snapshot, cfg, channel, thread_ts, user), cfg.log_output)
    except Exception:
        log.exception("Failed to write usage record")
