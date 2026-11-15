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
    "pr_limits": {},
    "keep_running": [],
    "delegate_claude_args": [],
    "delegate_codex_overrides": [],
    "inbox_store": None,
    "backlog_limit_kb": 40,
    "done_limit_kb": 20,
    "trim_by_delegate_over_kb": 150,
    "user_names": [],
    "open_ask_patterns": [],
    "check_for_updates": True,
}


def load_config(path: Path = CONFIG_FILE) -> dict:
    """The user's settings over DEFAULTS. A missing file means every default."""
    try:
        saved = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}")
    if not isinstance(saved, dict):
        raise ValueError(f"{path} holds no JSON object")
    unknown = sorted(set(saved) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"{path} has unknown keys: {', '.join(unknown)}. Known keys: {', '.join(DEFAULTS)}")
    for key, value in saved.items():
        if DEFAULTS[key] is not None and value is not None and type(value) is not type(DEFAULTS[key]):
            raise ValueError(f"{path}: {key} must be a JSON {type(DEFAULTS[key]).__name__}")
    return {**DEFAULTS, **saved}


try:
    CONFIG = load_config()
except ValueError as config_error:
    print(f"coordinator config: {config_error}", file=sys.stderr)
    sys.exit(2)
