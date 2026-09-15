#!/usr/bin/env python3
"""Upsert MCP registry servers into a goose config.yaml as streamable_http
extensions.

Usage: sync_mcp.py <registry-json-file> <config-yaml-file>

Reads the bearer token (for registry entries with "token": "yes") from the
MIKEY_TOKEN environment variable -- deliberately never from argv, which is
visible to other users on a shared machine via `ps`, and never hardcoded
here.
"""
import json
import os
import sys

import yaml


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: sync_mcp.py <registry-json-file> <config-yaml-file>", file=sys.stderr)
        return 2

    registry_path, config_path = sys.argv[1], sys.argv[2]
    token = os.environ.get("MIKEY_TOKEN", "")

    with open(registry_path) as f:
        registry = json.load(f)
    servers = registry.get("mcpServers", {})

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    extensions = config.setdefault("extensions", {})

    added, updated, disabled_no_token = [], [], []
    for name, entry in servers.items():
        if name == "registry":
            continue  # the registry endpoint itself, not an extension to add

        needs_token = entry.get("token") == "yes"
        ext = {
            "enabled": True,
            "type": "streamable_http",
            "name": name,
            "uri": entry["url"],
            "description": entry.get("description", ""),
            "timeout": 300,
            "bundled": False,
        }
        if needs_token:
            if token:
                ext["headers"] = {"Authorization": f"Bearer {token}"}
            else:
                ext["enabled"] = False
                disabled_no_token.append(name)

        (updated if name in extensions else added).append(name)
        extensions[name] = ext

    with open(config_path, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False)
    os.chmod(config_path, 0o600)

    if added:
        print(f"Added: {', '.join(sorted(added))}")
    if updated:
        print(f"Updated: {', '.join(sorted(updated))}")
    if disabled_no_token:
        print(
            f"No token available -- added disabled: {', '.join(sorted(disabled_no_token))}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
