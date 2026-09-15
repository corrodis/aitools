#!/usr/bin/env python3
"""Write ALCF settings into a live goose config.yaml, under the `openai`
provider slot -- the same slot setup.sh seeds with the internal
vllm.fnal.gov default. This *overwrites* that slot (unlike
litellm_set_provider.py, which configures the independent `litellm` slot
for litellm.fnal.gov and never touches `openai`).

Usage:
  alcf_set_provider.py set <config-yaml> <base-url> <model>
  alcf_set_provider.py restore-default <config-yaml> <template-yaml>

"set" writes OPENAI_BASE_URL and providers.openai.model into config.yaml,
sets active_provider: openai, and writes OPENAI_API_KEY (read from the
OPENAI_API_KEY_VALUE environment variable -- never argv, which is visible
to other users via `ps` on a shared machine) into secrets.yaml, a sibling
file in the same directory. Confirmed empirically: goose does NOT read a
provider API key from config.yaml -- it must be in secrets.yaml.

"restore-default" copies the OPENAI_* keys and providers.openai back out
of the tracked template (config.yaml.example), removes OPENAI_API_KEY
from secrets.yaml, and resets active_provider to the template's value --
undoing a previous "set" regardless of what's currently active.
"""
import os
import sys

import yaml

# Best-effort manual overrides for ALCF's real per-model context limit,
# used to set GOOSE_CONTEXT_LIMIT in config.yaml. goose has no way to
# discover this itself for ALCF: it's a documented upstream bug/gap
# (aaif-goose/goose issues #7839/#8835/#10058/#8780) that custom models
# outside goose's own static registry fall back to a hardcoded 128k --
# and separately, checked directly against ALCF's own API: its
# list-endpoints response carries no per-model metadata at all, just
# plain name strings, so there's nothing to auto-capture the way
# litellm_set_provider.py does for litellm.fnal.gov's /v1/models.
#
# Otherwise left EMPTY on purpose: we don't have ALCF's actual per-model
# `--max-model-len` deployment config for the rest, and a wrong HIGH
# number is worse than no override -- it can make goose under-compact
# and let a request fail outright with a hard context-length-exceeded
# error, instead of goose's own default (128k), which fails safe by
# compacting proactively. Only add an entry once you've confirmed the
# real served limit for a model -- never just paste in a model card's
# architectural max, which a vLLM deployment often doesn't actually
# serve at full size (see Llama-4-Scout below: 10M advertised, ~32k
# actually served).
CONTEXT_LIMITS = {
    # Confirmed empirically 2026-09-11 by probing ALCF's sophia/vllm
    # endpoint directly with progressively larger prompts: vLLM's own
    # error message reported "This model's maximum context length is
    # 32768 tokens" -- ~300x smaller than the model's advertised 10M.
    # Set a bit below that (32768) so goose has room to compact before
    # actually hitting the hard wall, rather than right at the edge.
    "meta-llama/Llama-4-Scout-17B-16E-Instruct": 30000,
}


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


def cmd_set(config_path, base_url, model):
    token = os.environ.get("OPENAI_API_KEY_VALUE", "")
    if not token:
        print("Error: OPENAI_API_KEY_VALUE not set in environment", file=sys.stderr)
        return 1

    config = load(config_path)
    config["OPENAI_BASE_URL"] = base_url
    config.setdefault("providers", {}).setdefault("openai", {})
    config["providers"]["openai"]["model"] = model
    config["providers"]["openai"]["enabled"] = True
    config["providers"]["openai"]["configured"] = True
    config["active_provider"] = "openai"
    limit = CONTEXT_LIMITS.get(model)
    if limit:
        config["GOOSE_CONTEXT_LIMIT"] = limit
    else:
        config.pop("GOOSE_CONTEXT_LIMIT", None)
    save(config, config_path)

    secrets_path = secrets_path_for(config_path)
    secrets = load(secrets_path)
    secrets["OPENAI_API_KEY"] = token
    save(secrets, secrets_path)

    print(f"Set openai provider -> {base_url} ({model})")
    return 0


def cmd_restore_default(config_path, template_path):
    config = load(config_path)
    template = load(template_path)

    for key in ("OPENAI_BASE_URL", "OPENAI_HOST", "OPENAI_BASE_PATH", "OPENAI_TIMEOUT", "GOOSE_CONTEXT_LIMIT"):
        if key in template:
            config[key] = template[key]
        else:
            config.pop(key, None)

    template_openai = template.get("providers", {}).get("openai", {})
    config.setdefault("providers", {}).setdefault("openai", {})
    config["providers"]["openai"] = dict(template_openai)
    config["active_provider"] = template.get("active_provider", "openai")
    save(config, config_path)

    secrets_path = secrets_path_for(config_path)
    if os.path.exists(secrets_path):
        secrets = load(secrets_path)
        secrets.pop("OPENAI_API_KEY", None)
        save(secrets, secrets_path)

    print("Restored default (template) openai provider settings")
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    action = sys.argv[1]
    if action == "set" and len(sys.argv) == 5:
        return cmd_set(sys.argv[2], sys.argv[3], sys.argv[4])
    if action == "restore-default" and len(sys.argv) == 4:
        return cmd_restore_default(sys.argv[2], sys.argv[3])

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
