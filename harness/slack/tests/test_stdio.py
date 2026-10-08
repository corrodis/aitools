"""stdio MCP servers next to the registry -- a real subprocess, no network."""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from mu2e_slack import mcp_tools
from mu2e_slack.config import parse_args
from mu2e_slack.mcp_tools import ToolRegistry, parse_stdio_servers, stdio_env

ECHO_SERVER = textwrap.dedent("""
    import os
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("echo")

    @mcp.tool(name="echo_env")
    def echo_env(name: str) -> str:
        return os.environ.get(name, "<unset>")

    mcp.run(transport="stdio")
""")


def test_parse_stdio_servers():
    assert parse_stdio_servers(["slack=slack-mcp-stdio", "x = /bin/foo --a 'b c'"]) == {
        "slack": ["slack-mcp-stdio"], "x": ["/bin/foo", "--a", "b c"]}
    for bad in ("slack", "=cmd", "slack=", "sl ack=cmd"):
        with pytest.raises(ValueError):
            parse_stdio_servers([bad])


def test_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("MU2E_SLACK_STDIO_SERVERS", "a=one;b=two")
    assert parse_args([])[0].stdio_servers == {"a": ["one"], "b": ["two"]}
    assert parse_args(["--stdio-server", "c=three"])[0].stdio_servers == {"c": ["three"]}


def test_stdio_env_passes_only_prefixed(monkeypatch):
    monkeypatch.setenv("SLACK_MCP_CHANNELS", "mu2e-ai-test")
    monkeypatch.setenv("MIKEY_TOKEN", "secret")
    env = stdio_env("slack")
    assert env["SLACK_MCP_CHANNELS"] == "mu2e-ai-test"
    assert "MIKEY_TOKEN" not in env


def test_stdio_round_trip(tmp_path, monkeypatch):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER)
    monkeypatch.setenv("ECHO_GREETING", "hello")
    monkeypatch.setenv("MIKEY_TOKEN", "secret")

    async def go():
        reg = ToolRegistry("", stdio_servers={"echo": [sys.executable, str(script)]})
        await reg.load()
        assert list(reg.tools) == ["echo__echo_env"] and not reg.failed
        assert await reg.call("echo__echo_env", {"name": "ECHO_GREETING"}) == "hello"
        assert await reg.call("echo__echo_env", {"name": "MIKEY_TOKEN"}) == "<unset>"

    asyncio.run(go())


def test_bare_command_resolves_next_to_interpreter(tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tool = fake_bin / "some-mcp-stdio"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setattr(mcp_tools.sys, "executable", str(fake_bin / "python"))
    monkeypatch.setenv("PATH", "/nonexistent")
    assert mcp_tools.resolve_command("some-mcp-stdio") == str(tool)
    assert mcp_tools.resolve_command("/abs/path") == "/abs/path"
