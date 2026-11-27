"""Per-block key/value labels for the rex runtime, kept in one JSON file per block.

Rex blocks carry only one text label, so fleet.role, fleet.domain, inbox.* and
the like live here: <state_dir>/rex-labels/<block number>.json.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
from pathlib import Path

import config


def _key(block_id: str) -> str:
    return block_id.split(":", 1)[-1]


def _dir() -> Path:
    return Path(config.CONFIG["state_dir"]).expanduser() / "rex-labels"


def _path(block_id: str) -> Path:
    return _dir() / f"{_key(block_id)}.json"


@contextlib.contextmanager
def _locked():
    """Hold the label directory's lock, so one writer's read-modify-write never drops another's keys."""
    directory = _dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def labels_for(block_id: str) -> dict[str, str]:
    """The block's labels; a missing or unreadable file reads as no labels."""
    try:
        data = json.loads(_path(block_id).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge(block_id: str, labels: dict[str, str]) -> None:
    """Set the given labels on the block; a value of "" removes that key."""
    import watch_cache  # here, not at the top: watch_cache imports fleet, which imports terminals, which imports this

    with _locked():
        current = labels_for(block_id)
        for key, value in labels.items():
            if value == "":
                current.pop(key, None)
            else:
                current[key] = value
        watch_cache.save_json_atomic(_path(block_id), current)


def forget(block_id: str) -> None:
    """Delete the block's label file; no file is no error."""
    if not _dir().is_dir():
        return
    with _locked():
        _path(block_id).unlink(missing_ok=True)


def prune(live_ids: set[str], older_than: float | None = None) -> int:
    """Delete label files for blocks not in live_ids; return how many were removed.

    With older_than, a file written at or after that time is kept: a block
    launched while the listing ran is not in it, but its labels are fresh.
    """
    directory = _dir()
    if not directory.is_dir():
        return 0
    live = {_key(block_id) for block_id in live_ids}
    removed = 0
    with _locked():
        for path in directory.glob("*.json"):
            if path.stem in live:
                continue
            try:
                if older_than is not None and path.stat().st_mtime >= older_than:
                    continue
            except OSError:
                continue
            path.unlink(missing_ok=True)
            removed += 1
    return removed
