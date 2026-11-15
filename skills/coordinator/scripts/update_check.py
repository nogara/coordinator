"""
Whether the Git clone these scripts are installed from is behind its
upstream branch, checked at most once a day.

The install links each skill folder to the clone, so the clone is the Git
work tree that holds this file's real path. The check fetches the upstream
branch and counts the commits the clone lacks. It saves the day and the
count in the state folder, shared by every coordinator's watcher, and gives
the line once for each new count. It never pulls. It gives nothing when
check_for_updates is false in the config, when the scripts are not in a
clone, when the branch has no upstream, or when the fetch fails.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import fleet
from fleet import CONFIG

UPDATE_CHECK_FILE = fleet.STATE_DIR / "update-check.json"
FETCH_SECONDS = 15


def git(clone: str, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", clone, *args], capture_output=True, text=True, timeout=FETCH_SECONDS,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def commits_behind() -> tuple[str, int] | None:
    """The clone's folder and how many upstream commits it lacks, or None when that cannot be told."""
    clone = git(fleet.SCRIPTS, "rev-parse", "--show-toplevel")
    if not clone or git(clone, "rev-parse", "--abbrev-ref", "@{upstream}") is None:
        return None
    if git(clone, "fetch", "--quiet") is None:
        return None
    count = git(clone, "rev-list", "--count", "HEAD..@{upstream}")
    return (clone, int(count)) if count and count.isdigit() else None


def update_line(today: str) -> str | None:
    """A `rule ·` line when the clone is behind by a count no watcher has given yet."""
    if not CONFIG["check_for_updates"]:
        return None
    try:
        saved = json.loads(UPDATE_CHECK_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    if saved.get("checked_on") != today:
        saved["checked_on"] = today
        found = commits_behind()
        if found:
            saved["clone"], saved["behind"] = found
            if found[1] == 0:
                saved["told"] = 0
    behind = saved.get("behind") or 0
    line = None
    if behind > 0 and behind != saved.get("told"):
        saved["told"] = behind
        line = (f"rule · the coordinator skills are {behind} commit{'s' if behind != 1 else ''} behind their "
                f"upstream · tell the user in your next reply. To update, they run `git pull` in "
                f"{saved.get('clone')}, then restart each coordinator")
    UPDATE_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(f"{UPDATE_CHECK_FILE}.tmp")
    temporary.write_text(json.dumps(saved))
    os.replace(temporary, UPDATE_CHECK_FILE)
    return line
