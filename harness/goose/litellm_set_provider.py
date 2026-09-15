#!/usr/bin/env python3
"""Write litellm.fnal.gov settings into a live goose config.yaml, under
the `litellm` provider slot -- independent of `openai` (vllm.fnal.gov by
default, or ALCF via alcf.sh). Both stay configured at once; this only
changes which is currently active.

Usage:
  litellm_set_provider.py set <config-yaml> <host> <model> [context-limit]

Writes LITELLM_HOST and providers.litellm.model into config.yaml, sets
active_provider: litellm, and writes LITELLM_API_KEY (read from the
LITELLM_API_KEY_VALUE environment variable -- never argv, which is
visible to other users via `ps` on a shared machine) into secrets.yaml, a
sibling file in the same directory. Confirmed empirically (same finding
as for the openai/ALCF case): goose does NOT read a provider API key from
config.yaml -- it must be in secrets.yaml.

context-limit, if given (setup-litellm.sh fetches it from litellm.fnal
.gov's own /v1/models response, when the proxy publishes it), is written
as GOOSE_CONTEXT_LIMIT -- goose has a documented bug where it discards
this same metadata after fetching it itself for LiteLLM-style providers
(aaif-goose/goose issue #8835), silently falling back to a hardcoded
128k regardless of the model's real capacity. Omitted or empty clears
any previous override, so switching to a model without published
metadata doesn't keep an unrelated model's limit around.
"""
import os
import sys

import yaml


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def save(config, path):
    with open(path, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False)
    os.chmod(path, 0o600)


def secrets_path_for(config_path):
    return os.path.join(os.path.dirname(config_path), "secrets.yaml")


def cmd_set(config_path, host, model, context_limit=""):
    token = os.environ.get("LITELLM_API_KEY_VALUE", "")
    if not token:
        print("Error: LITELLM_API_KEY_VALUE not set in environment", file=sys.stderr)
        return 1

    config = load(config_path)
    config["LITELLM_HOST"] = host
    config.setdefault("providers", {}).setdefault("litellm", {})
    config["providers"]["litellm"]["model"] = model
    config["providers"]["litellm"]["enabled"] = True
    config["providers"]["litellm"]["configured"] = True
    config["active_provider"] = "litellm"
    try:
        limit = int(context_limit)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        config["GOOSE_CONTEXT_LIMIT"] = limit
        print(f"(litellm.fnal.gov published a context limit for this model: {limit})")
    else:
        config.pop("GOOSE_CONTEXT_LIMIT", None)
    save(config, config_path)

    secrets_path = secrets_path_for(config_path)
    secrets = load(secrets_path)
    secrets["LITELLM_API_KEY"] = token
    save(secrets, secrets_path)

    print(f"Set litellm provider (litellm.fnal.gov) -> {host} ({model}), active_provider: litellm")
    return 0


def main():
    if len(sys.argv) in (5, 6) and sys.argv[1] == "set":
        context_limit = sys.argv[5] if len(sys.argv) == 6 else ""
        return cmd_set(sys.argv[2], sys.argv[3], sys.argv[4], context_limit)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
