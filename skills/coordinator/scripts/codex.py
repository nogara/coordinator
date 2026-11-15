"""
Starting a Codex delegate: its command, the folder trust Codex requires, and
the session id of a new one, which Codex writes to no file Herdr can read.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from pathlib import Path

from fleet import (  # noqa: E402
    CODEX, CODEX_EFFORTS, CODEX_SESSION_FILES, DELEGATE_CODEX_OVERRIDES, HOME, SECONDS_TO_WAIT_FOR_AGENT,
    CheckError, end_terminal, git, parse_time, run, terminal_text,
)

CODEX_CONFIG = HOME / ".codex" / "config.toml"
SECONDS_TO_WAIT_FOR_CODEX_SESSION_FILE = 20
BYTES_OF_A_NEW_CODEX_SESSION_TO_READ = 2_000_000


def codex_program(effort: str, session: str | None = None, first_message: str | None = None) -> list[str]:
    """The command that starts a Codex delegate, or resumes one, with what it overrides in ~/.codex/config.toml:
    the effort, the update check, and the config's delegate_codex_overrides."""
    if effort not in CODEX_EFFORTS:
        raise CheckError(f"{effort!r} is not a Codex effort: {', '.join(CODEX_EFFORTS)}")
    overrides = [
        f'model_reasoning_effort="{effort}"',
        # Codex's update prompt reports no status, so a terminal stopped on it looks like one where nothing started.
        "check_for_update_on_startup=false",
        *DELEGATE_CODEX_OVERRIDES,
    ]
    # Without --no-daemon Codex runs its commands in a shared server, outside this terminal (`hc --skill`).
    command = [CODEX, *(["resume", session] if session else []), "--no-daemon"]
    for override in overrides:
        command += ["-c", override]
    return command + ([first_message] if first_message else [])


def folder_codex_asks_to_trust(cwd: str) -> str:
    """The folder whose trust Codex checks before it starts in cwd: a Git checkout's main worktree, else cwd."""
    git_dir = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return str(Path(git_dir).parent) if git_dir and Path(git_dir).name == ".git" else cwd


def codex_trusts(folder: str, config: Path = CODEX_CONFIG) -> bool:
    try:
        text = config.read_text()
    except OSError:
        return False
    entry = re.search(rf'^\[projects\."{re.escape(folder)}"\]\n((?:(?!\[).*\n?)*)', text, re.MULTILINE)
    return bool(entry and re.search(r'^trust_level\s*=\s*"trusted"', entry.group(1), re.MULTILINE))


def require_codex_trust(cwd: str) -> None:
    folder = folder_codex_asks_to_trust(cwd)
    if not codex_trusts(folder):
        raise CheckError(f"Codex has not trusted {folder}, and trusting a folder is the user's choice. "
                         f'They trust it by running `codex` there once and choosing "Trust and continue"')


def codex_labels(effort: str, session: str | None = None) -> dict[str, str]:
    """What a Codex delegate's terminal carries beside fleet.domain and fleet.task."""
    return {"fleet.effort": effort, **({"fleet.session": session} if session else {})}


def codex_sessions_started(cwd: str, since: float, first_message: str) -> set[str]:
    """The Codex sessions started in cwd since that time whose first message was first_message.

    Codex Desktop saves its sessions in the same place, and a domain with no
    repo starts every delegate in one folder, so the folder and the time do
    not name the session. A session's reviewer threads save files of their
    own. Each names the session it reviews as session_id, so it differs from
    the thread's own id.
    """
    sent = json.dumps(first_message, ensure_ascii=False)[1:-1]
    started = set()
    for path in glob.glob(str(CODEX_SESSION_FILES / "*/*/*/rollout-*.jsonl")):
        try:
            if os.path.getmtime(path) < since:
                continue
            with open(path) as handle:
                saved = json.loads(handle.readline()).get("payload") or {}
                holds_the_message = sent in handle.read(BYTES_OF_A_NEW_CODEX_SESSION_TO_READ)
            is_a_session = bool(saved.get("id")) and saved.get("id") == saved.get("session_id")
            if (holds_the_message and is_a_session and os.path.samefile(saved.get("cwd") or "", cwd)
                    and parse_time(saved.get("timestamp")).timestamp() >= since):
                started.add(saved["id"])
        except (OSError, json.JSONDecodeError, AttributeError, ValueError):
            continue
    return started


def codex_session_started(cwd: str, since: float, first_message: str) -> str:
    """The id of the Codex session this launch started, once Codex has saved its first message."""
    deadline = time.time() + SECONDS_TO_WAIT_FOR_CODEX_SESSION_FILE
    while True:
        started = codex_sessions_started(cwd, since, first_message)
        if len(started) == 1:
            return started.pop()
        if started or time.time() >= deadline:
            raise CheckError(f"expected one new Codex session in {cwd} that starts with the brief and found {len(started)}"
                             + (f": {', '.join(sorted(started))}" if started else ""))
        time.sleep(1)


def finish_codex_start(terminal: dict, cwd: str, launched_at: float, resumed_session: str | None,
                       first_message: str | None) -> str:
    """Return a launched Codex delegate's session, written on its terminal when the launch started a new one.

    A terminal with no recorded session cannot be ended or resumed by the
    fleet scripts, so one that did not start, or whose session cannot be
    told apart, is ended here. A resumed session is on the terminal from
    its launch.
    """
    address = terminal["address"]
    try:
        if not terminal["running"]:
            raise CheckError(f"no agent showed in terminal {address} within {SECONDS_TO_WAIT_FOR_AGENT} seconds. "
                             f"It showed:\n{terminal_text(address)}")
        if resumed_session:
            return resumed_session
        session = codex_session_started(cwd, launched_at, first_message)
        run(["hc", "label", address, f"fleet.session={session}"])
    except CheckError as error:
        try:
            end_terminal(address)
        except CheckError as not_ended:
            raise CheckError(f"{error}\nTerminal {address} could not be ended: {not_ended}")
        raise CheckError(f"{error}\nTerminal {address} was ended")
    return session
