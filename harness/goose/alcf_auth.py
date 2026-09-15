#!/usr/bin/env python3
"""ALCF (Argonne Leadership Computing Facility) inference-service auth helper.

A trimmed reimplementation of Argonne's own
https://github.com/argonne-lcf/inference-endpoints/blob/main/inference_auth_token.py,
differing in exactly one respect: it forces the "command-line" login flow
(print a URL, paste back the resulting code) instead of the default
"local-server" flow, which tries to open a local HTTP server on this
machine to catch a browser redirect -- that doesn't work well over SSH on
a headless gpvm with no local browser.

Uses the same public client IDs/scopes and the same token cache location
(~/.globus/app/<AUTH_CLIENT_ID>/inference_app/tokens.json) as upstream, so
a token obtained via either script is usable by the other.

Usage:
  alcf_auth.py authenticate        # interactive: prints a URL, prompts for a code
  alcf_auth.py get_access_token    # prints a valid access token (refreshing if needed)
"""
import os.path
import sys

import globus_sdk
from globus_sdk.login_flows import CommandLineLoginFlowManager  # noqa: F401  (registers "command-line")

APP_NAME = "inference_app"
AUTH_CLIENT_ID = "58fdd3bc-e1c3-4ce5-80ea-8d6b87cfb944"
GATEWAY_CLIENT_ID = "681c10cc-f684-4540-bcd7-0b4df3bc26ef"
GATEWAY_SCOPE = f"https://auth.globus.org/scopes/{GATEWAY_CLIENT_ID}/action_all"

TOKENS_PATH = f"{os.path.expanduser('~')}/.globus/app/{AUTH_CLIENT_ID}/{APP_NAME}/tokens.json"

GA_PARAMS = globus_sdk.gare.GlobusAuthorizationParameters(
    session_required_policies=["83732ff2-9c42-4548-b5ce-17e498c84f6a"]
)


class _ReloginErrorHandler:
    def __call__(self, app, error):
        print(f"Encountered error '{error}', initiating login...")
        app.login(auth_params=GA_PARAMS)


def _make_app() -> globus_sdk.UserApp:
    return globus_sdk.UserApp(
        APP_NAME,
        client_id=AUTH_CLIENT_ID,
        scope_requirements={GATEWAY_CLIENT_ID: [GATEWAY_SCOPE]},
        config=globus_sdk.GlobusAppConfig(
            request_refresh_tokens=True,
            token_validation_error_handler=_ReloginErrorHandler(),
            login_flow_manager="command-line",
        ),
    )


def authenticate() -> None:
    app = _make_app()
    app.login(auth_params=GA_PARAMS)


def get_access_token() -> str:
    app = _make_app()
    auth = app.get_authorizer(GATEWAY_CLIENT_ID)
    auth.ensure_valid_token()
    return auth.access_token


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("authenticate", "get_access_token"):
        print(__doc__, file=sys.stderr)
        return 2

    action = sys.argv[1]
    if action == "authenticate":
        authenticate()
        return 0

    # get_access_token
    if not os.path.isfile(TOKENS_PATH):
        print(
            'No token yet. Run "alcf_auth.py authenticate" first.',
            file=sys.stderr,
        )
        return 1
    print(get_access_token())
    return 0


if __name__ == "__main__":
    sys.exit(main())
