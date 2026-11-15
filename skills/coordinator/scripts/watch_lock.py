"""
The lock a fleet watcher holds on <state file>.lock while it runs.

The watcher writes its pid into the lock file, and the pid stays there after
it exits. watcher_running reads that pid and confirms with ps that the
process is still a fleet watcher. Nothing else should take the lock to test
it: a watcher that starts stops the process holding the lock.
"""
import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path

import fleet

LOCK_WAIT_SECONDS = 5


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


def watcher_running(state_path: str | Path) -> int | None:
    """The pid of the fleet watcher that runs on this state file, or None when none does."""
    try:
        written = Path(f"{state_path}.lock").read_text().strip()
    except OSError:
        return None
    if not written.isdigit():
        return None
    try:
        command = subprocess.run(["ps", "-p", written, "-o", "command="], capture_output=True, text=True,
                                 timeout=fleet.COMMAND_TIMEOUT_SECONDS).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(written) if "fleet_watch.py" in command else None
