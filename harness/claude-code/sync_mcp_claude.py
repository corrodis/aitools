#!/usr/bin/env python3
"""Upsert MCP registry servers into Claude Code's ~/.claude.json as HTTP MCP
servers (user scope -- applies to all projects for this user).

Usage: sync_mcp_claude.py <registry-json-file> <claude-json-file>

Reads the bearer token (for registry entries with "token": "yes") from the
MIKEY_TOKEN environment variable. Never from argv (visible to other users
via `ps`).

Claude Code's ~/.claude.json structure (user-scope MCP servers):

    {
      "mcpServers": {
        "<name>": {
          "type": "http",
          "url": "<url>",
          "headers": { "Authorization": "Bearer <token>" }   # if token needed
        }
      },
      ...other claude.json fields...
    }

Existing entries for the same server name are overwritten; everything else
in the file is left untouched. The file is written atomically (via a temp
file rename) to avoid corruption on partial writes.
"""
import json
import os
import sys
import tempfile


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: sync_mcp_claude.py <registry-json-file> <claude-json-file>",
              file=sys.stderr)
        return 2

    registry_path, claude_json_path = sys.argv[1], sys.argv[2]
    token = os.environ.get("MIKEY_TOKEN", "")

    with open(registry_path) as f:
        registry = json.load(f)
    servers = registry.get("mcpServers", {})

    # Read existing ~/.claude.json, or start from a minimal skeleton.
    if os.path.exists(claude_json_path):
        with open(claude_json_path) as f:
            config = json.load(f)
    else:
        config = {}

    # User-scope MCP servers live at the top level under "mcpServers".
    mcp = config.setdefault("mcpServers", {})

    added, updated, disabled_no_token = [], [], []

    for name, entry in servers.items():
        if name == "registry":
            continue  # the registry endpoint itself, skip

        needs_token = entry.get("token") == "yes"

        server: dict = {
            "type": "http",
            "url": entry["url"],
        }

        if needs_token:
            if token:
                server["headers"] = {"Authorization": f"Bearer {token}"}
            else:
                # Mark as disabled -- Claude Code doesn't have an 'enabled'
                # field in its MCP server spec, so we add a custom comment
                # key that Claude Code ignores but makes the intent clear.
                server["_disabled_reason"] = "no MIKEY token -- run sync-mcp.sh after obtaining one"
                disabled_no_token.append(name)

        (updated if name in mcp else added).append(name)
        mcp[name] = server

    # Write atomically: write to a temp file in the same directory then rename.
    dir_ = os.path.dirname(os.path.abspath(claude_json_path))
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".claude.json.tmp.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, claude_json_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if added:
        print(f"Added: {', '.join(sorted(added))}")
    if updated:
        print(f"Updated: {', '.join(sorted(updated))}")
    if disabled_no_token:
        print(
            f"No token available -- added with disabled marker: "
            f"{', '.join(sorted(disabled_no_token))}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
