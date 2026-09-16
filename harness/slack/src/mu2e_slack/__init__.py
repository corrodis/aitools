"""mu2e-slack-bot: a Slack frontend for a Mu2e AI agent.

One Slack thread is one conversation. The agent can call the MCP servers
listed in the internal registry and nothing else -- there is no shell, no
filesystem, and no code execution anywhere in this package.
"""

import importlib.metadata


def version() -> str:
    try:
        return importlib.metadata.version("mu2e-slack-bot")
    except importlib.metadata.PackageNotFoundError:
        return "unknown (not installed as a package)"


__all__ = ["version"]
