"""
The user's settings for the coordinator scripts, from
~/.config/coordinator/config.json or the file COORDINATOR_CONFIG names.
Every key is optional; DEFAULTS lists them. An unknown key, or a value of
the wrong JSON type, stops every script with exit status 2.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("COORDINATOR_CONFIG") or Path.home() / ".config" / "coordinator" / "config.json").expanduser()
DEFAULTS = {
    "backlog_dir": "~/.config/coordinator/backlogs",
    "state_dir": "~/.local/state/coordinator",
    "keep_running": [],
    "delegate_claude_args": [],
    "delegate_env": {},
    "inbox_store": None,
    "backlog_limit_kb": 40,
    "done_limit_kb": 20,
    "trim_by_delegate_over_kb": 150,
    "user_names": [],
    "open_ask_patterns": [],
    "check_for_updates": True,
}
# Accepted and ignored so older config files keep working during upgrade.
DEPRECATED_KEYS = {"pr_limits", "delegate_codex_overrides"}


def load_config(path: Path = CONFIG_FILE) -> dict:
    """The user's settings over DEFAULTS. A missing file means every default."""
    try:
        saved = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}")
    if not isinstance(saved, dict):
        raise ValueError(f"{path} holds no JSON object")
    unknown = sorted(set(saved) - set(DEFAULTS) - DEPRECATED_KEYS)
    if unknown:
        raise ValueError(f"{path} has unknown keys: {', '.join(unknown)}. Known keys: {', '.join(DEFAULTS)}")
    active = {key: value for key, value in saved.items() if key not in DEPRECATED_KEYS}
    for key, value in active.items():
        if DEFAULTS[key] is not None and value is not None and type(value) is not type(DEFAULTS[key]):
            raise ValueError(f"{path}: {key} must be a JSON {type(DEFAULTS[key]).__name__}")
    merged = {**DEFAULTS, **active}
    if merged["delegate_env"] is None:
        merged["delegate_env"] = {}
    for env_key, env_value in merged["delegate_env"].items():
        if not isinstance(env_key, str) or not env_key or "=" in env_key or not isinstance(env_value, str):
            raise ValueError(f"{path}: delegate_env must map non-empty variable names to strings")
    return merged


def with_delegate_env(program: list[str], env: dict[str, str] | None = None) -> list[str]:
    """Prefix a delegate command with configured environment variables."""
    mapping = CONFIG["delegate_env"] if env is None else env
    pairs = [f"{key}={value}" for key, value in mapping.items()]
    return ["env", *pairs, *program] if pairs else list(program)


try:
    CONFIG = load_config()
except ValueError as config_error:
    print(f"coordinator config: {config_error}", file=sys.stderr)
    sys.exit(2)
