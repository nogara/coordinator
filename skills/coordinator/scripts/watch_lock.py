"""
The lock a fleet watcher holds on <state file>.lock while it runs.

The watcher writes its pid into the lock file, and the pid stays there after
it exits. watcher_running reads that pid and confirms with ps that the
process is a fleet watcher that started before the lock file was written,
so a later process that reuses the pid does not count. Nothing else should take the lock to test
it: a watcher that starts stops the process holding the lock.
"""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path

import fleet

LOCK_WAIT_SECONDS = 5
# ps reports elapsed time in whole seconds.
START_TIME_SLACK_SECONDS = 2


def try_lock(lock_file):
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def take_state_lock(state_path):
    """Stop the watcher holding the state file's lock, then hold it until this process exits.

    The kernel drops a flock when its holder exits, so a lock never outlives
    its watcher. Returns the open lock file, or the holder's pid when it did
    not let go in time.
    """
    lock_file = open(state_path + ".lock", "a+")
    deadline = time.time() + LOCK_WAIT_SECONDS
    holder = None
    while not try_lock(lock_file):
        if holder is None:
            lock_file.seek(0)
            written = lock_file.read().strip()
            if written.isdigit():
                holder = written
                try:
                    os.kill(int(holder), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        if time.time() >= deadline:
            return holder or "unknown"
        time.sleep(0.1)
    lock_file.truncate(0)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def seconds_since_start(elapsed: str) -> int:
    """Seconds from ps's elapsed time, written [[dd-]hh:]mm:ss."""
    days, _, clock = elapsed.rpartition("-")
    seconds = 0
    for part in clock.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds + int(days or 0) * 24 * 60 * 60


def watcher_running(state_path: str | Path) -> int | None:
    """The pid of the fleet watcher that runs on this state file, or None when none does.

    Raises CheckError when ps cannot tell.
    """
    lock_path = Path(f"{state_path}.lock")
    try:
        written = lock_path.read_text().strip()
        locked_at = lock_path.stat().st_mtime
    except OSError:
        return None
    if not written.isdigit():
        return None
    try:
        found = subprocess.run(["ps", "-p", written, "-o", "etime=,command="], capture_output=True, text=True,
                               timeout=fleet.COMMAND_TIMEOUT_SECONDS).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        raise fleet.CheckError(f"ps could not check pid {written}: {type(error).__name__}") from error
    elapsed, _, command = found.partition(" ")
    if "fleet_watch.py" not in command:
        return None
    started_at = time.time() - seconds_since_start(elapsed)
    wrote_its_own_lock = started_at <= locked_at + START_TIME_SLACK_SECONDS
    return int(written) if wrote_its_own_lock else None
