#!/usr/bin/env python3
"""
log-session-claude.py  --  extract per-session usage summary from Claude Code's
                            JSONL transcript files and write to a flat JSON-lines
                            file (mirroring what goose/log-session.py does for goose).

Usage:
    log-session-claude.py [SESSION_ID]

    SESSION_ID  A Claude Code session UUID (e.g. "d6e1339a-2731-4ff7-...").
                Defaults to the most-recently-updated session across all projects.

Output backends (selected by env vars):

    FILE (default)
        Appends one JSON line per session to $LOG_OUTPUT
        (default: /exp/mu2e/app/users/$USER/claude-code/log/usage.jsonl)

Privacy:
    Set LOG_PRIVACY=1 to omit fields that identify a specific person:
    user, working_dir are suppressed; all other fields are kept.

Schema (one row / JSON object per session) -- kept consistent with goose/log-session.py:
    session_id          TEXT    Claude Code session UUID (vs goose's "20260915_2" style)
    user                TEXT    Unix username               [omitted if LOG_PRIVACY=1]
    working_dir         TEXT    cwd the session was run in  [omitted if LOG_PRIVACY=1]
    host                TEXT    hostname
    logged_at           TEXT    ISO-8601 UTC timestamp of this log run
    session_created_at  TEXT    timestamp of first message in session
    session_updated_at  TEXT    timestamp of last message in session
    endpoint_url        TEXT    ANTHROPIC_BASE_URL from .env (the actual API endpoint)
    model               TEXT    model identifier (from first assistant message)
    harness             TEXT    git commit of the aitools repo (best-effort)
    turns               INT     number of user turns (user-role messages)
    llm_calls           INT     number of assistant messages (proxy for API calls;
                                goose counts individual streaming calls, same intent)
    tool_calls          INT     total tool invocations
    tool_breakdown      OBJECT  {tool_name: count, ...}
    input_tokens        INT     accumulated input tokens for the whole session
    output_tokens       INT     accumulated output tokens
    cache_read_tokens   INT     prompt-cache hits (Claude)
    cache_write_tokens  INT     prompt-cache writes (Claude)

Fields present in goose/log-session.py but not here (no equivalent in Claude Code):
    session_name  -- goose auto-names sessions; Claude Code uses opaque UUIDs
    provider      -- goose has named provider slots; Claude Code just has endpoint_url
    is_subagent   -- goose marks delegated subagent sessions; no equivalent in Claude Code

Fields here not in goose/log-session.py:
    (none -- claude_version is intentionally omitted to keep the schema identical)
"""

import json
import os
import socket
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

def _default_projects_root() -> Path:
    claude_home = os.environ.get("CLAUDE_HOME") or \
                  f"/exp/mu2e/app/users/{os.environ.get('USER', 'unknown')}/claude-code/home"
    return Path(claude_home) / ".claude" / "projects"


def _default_output() -> Path:
    claude_root = os.environ.get("CLAUDE_HOME") or \
                  f"/exp/mu2e/app/users/{os.environ.get('USER', 'unknown')}/claude-code"
    return Path(claude_root) / "log" / "usage.jsonl"


PROJECTS_ROOT = Path(os.environ.get("LOG_PROJECTS_ROOT", _default_projects_root()))
OUTPUT_FILE   = Path(os.environ.get("LOG_OUTPUT",         _default_output()))
PRIVACY       = os.environ.get("LOG_PRIVACY", "0").strip() not in ("0", "", "false", "no")


# ---------------------------------------------------------------------------
# FIND SESSION FILE
# ---------------------------------------------------------------------------

def find_session_file(session_id: Optional[str]) -> Optional[Path]:
    """Return the JSONL file for the given session_id, or the most recently
    modified one if session_id is None."""
    if not PROJECTS_ROOT.exists():
        return None

    all_files = sorted(
        PROJECTS_ROOT.rglob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not all_files:
        return None

    if session_id is None:
        return all_files[0]

    for f in all_files:
        if f.stem == session_id:
            return f
    return None


# ---------------------------------------------------------------------------
# PARSE TRANSCRIPT
# ---------------------------------------------------------------------------

def parse_session(jsonl_path: Path) -> dict:
    lines = []
    with open(jsonl_path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue

    session_id  = jsonl_path.stem
    cwd         = None
    model       = None
    timestamps  = []
    turns       = 0
    llm_calls   = 0
    tool_calls  = Counter()
    cost_state  = None  # last cost-state entry = session-level totals

    for d in lines:
        t = d.get("type")
        ts = d.get("timestamp")
        if ts:
            timestamps.append(ts)

        if t == "user":
            turns += 1
            if not cwd:
                cwd = d.get("cwd")

        elif t == "assistant":
            llm_calls += 1
            msg = d.get("message", {})
            if not model:
                m = msg.get("model", "")
                if m and m != "<synthetic>":
                    model = m
            # Tool use blocks in assistant content
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls[block.get("name", "unknown")] += 1

        elif t == "cost-state":
            # Prefer the last cost-state -- it's the running cumulative total.
            # This is the same data /usage shows inside Claude Code.
            cost_state = d

    # Sort timestamps for created/updated
    timestamps.sort()
    created_at = timestamps[0]  if timestamps else None
    updated_at = timestamps[-1] if timestamps else None

    # Extract cumulative token counts from cost-state (best source).
    # Falls back to 0 if unavailable (e.g. session ended abnormally).
    input_tok   = 0
    output_tok  = 0
    cache_read  = 0
    cache_write = 0
    think_tok   = 0

    if cost_state:
        # model_usage is a dict keyed by model name -- sum across all models
        # (shouldn't be more than one in practice, but be safe)
        for mu in (cost_state.get("modelUsage") or {}).values():
            input_tok   += mu.get("inputTokens", 0)
            output_tok  += mu.get("outputTokens", 0)
            cache_read  += mu.get("cacheReadInputTokens", 0)
            cache_write += mu.get("cacheCreationInputTokens", 0)
            think_tok   += mu.get("thinkingTokens", 0)
        # cost_usd intentionally not logged (privacy / not needed)
        # Use the model from cost-state if we haven't found one yet
        if not model and cost_state.get("modelUsage"):
            model = next(iter(cost_state["modelUsage"]))

    return {
        "session_id":         session_id,
        "cwd":                cwd,
        "model":              model,
        "turns":              turns,
        "llm_calls":          llm_calls,
        "tool_calls_total":   sum(tool_calls.values()),
        "tool_breakdown":     dict(tool_calls),
        "input_tokens":       input_tok,
        "output_tokens":      output_tok,
        "cache_read_tokens":  cache_read,
        "cache_write_tokens": cache_write,
        "thinking_tokens":    think_tok,
        "session_created_at": created_at,
        "session_updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# ASSEMBLE LOG RECORD
# ---------------------------------------------------------------------------

def _git_commit(repo_path: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_path), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return ""


def _endpoint_url() -> str:
    """Read ANTHROPIC_BASE_URL from the .env file (already sourced into env
    by env.sh, but read here directly so the hook subprocess also has it)."""
    if os.environ.get("ANTHROPIC_BASE_URL"):
        return os.environ["ANTHROPIC_BASE_URL"]
    claude_home = os.environ.get("CLAUDE_HOME") or \
                  f"/exp/mu2e/app/users/{os.environ.get('USER', '')}/claude-code/home"
    env_file = Path(claude_home) / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("export ANTHROPIC_BASE_URL="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                return val
    return ""


def build_record(parsed: dict) -> dict:
    script_dir = Path(__file__).parent
    harness_commit = _git_commit(script_dir)

    record: dict = {
        "session_id":         parsed["session_id"],
        "host":               socket.gethostname(),
        "logged_at":          datetime.now(timezone.utc).isoformat(),
        "session_created_at": parsed["session_created_at"],
        "session_updated_at": parsed["session_updated_at"],
        "endpoint_url":       _endpoint_url(),
        "model":              parsed.get("model") or "",
        "harness":            harness_commit,
        "turns":              parsed["turns"],
        "llm_calls":          parsed["llm_calls"],
        "tool_calls":         parsed["tool_calls_total"],
        "tool_breakdown":     parsed["tool_breakdown"],
        "input_tokens":       parsed["input_tokens"],
        "output_tokens":      parsed["output_tokens"],
        "cache_read_tokens":  parsed["cache_read_tokens"],
        "cache_write_tokens": parsed["cache_write_tokens"],
        "thinking_tokens":    parsed["thinking_tokens"],
    }

    if not PRIVACY:
        record["user"]        = os.environ.get("USER", "")
        record["working_dir"] = parsed.get("cwd") or ""

    return record


# ---------------------------------------------------------------------------
# WRITE OUTPUT
# ---------------------------------------------------------------------------

def write_record(record: dict) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if "session_id" in r:
                        existing[r["session_id"]] = r
                except json.JSONDecodeError:
                    pass

    existing[record["session_id"]] = record

    tmp = OUTPUT_FILE.with_suffix(".jsonl.tmp")
    with open(tmp, "w") as f:
        for r in existing.values():
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    tmp.replace(OUTPUT_FILE)
    OUTPUT_FILE.chmod(0o600)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    session_id = sys.argv[1] if len(sys.argv) > 1 else None

    jsonl_path = find_session_file(session_id)
    if jsonl_path is None:
        if session_id:
            print(f"Error: session {session_id!r} not found under {PROJECTS_ROOT}",
                  file=sys.stderr)
        else:
            print(f"Error: no session files found under {PROJECTS_ROOT}",
                  file=sys.stderr)
        return 1

    parsed = parse_session(jsonl_path)
    record = build_record(parsed)
    write_record(record)

    print(f"Logged session {record['session_id']}: "
          f"{record['turns']} turns, "
          f"{record['input_tokens']}+{record['output_tokens']} tokens "
          f"({record['model'] or 'model unknown'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
