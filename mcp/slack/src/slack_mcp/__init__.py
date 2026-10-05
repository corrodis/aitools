"""slack-mcp: read-only MCP server for an allowlisted set of Slack channels.

No posting, no search, no channels beyond the allowlist -- see server.py.
"""

import importlib.metadata


def version() -> str:
    try:
        return importlib.metadata.version("slack-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "unknown (not installed as a package)"


__all__ = ["version"]
