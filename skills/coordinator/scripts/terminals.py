"""
Where the fleet's terminals live, and the one place that talks to them.

Every call to `hc` (herdr-core) or `herdr-app` (the Herdr app) goes through
HcTerminals; the rest of the scripts ask `current()` for the backend the
user's `runtime` setting names. Terminals is the interface they rely on.
"""

from __future__ import annotations

import functools
import json
import os
import pwd
import re
import subprocess
import time
import uuid
from typing import Protocol

from check_error import CheckError
from config import CONFIG

COMMAND_TIMEOUT_SECONDS = 60
AGENT_PROGRAM = "claude-code"
HC_NOT_FOUND = 3
TERMINAL_NAME = re.compile(r"[a-z0-9_-]{1,32}")
SECONDS_TO_WAIT_FOR_TAB = 15
SECONDS_TO_WAIT_FOR_AGENT = 60

# herdr-core starts terminals with its own environment, not the user's. A login
# shell gives the session the PATH of the user's own terminals.
POSIX_SHELLS = {"zsh", "bash", "sh", "ksh", "dash"}
LOGIN_SHELL = pwd.getpwuid(os.getuid()).pw_shell or os.environ.get("SHELL") or ""
SHELL = LOGIN_SHELL if os.path.basename(LOGIN_SHELL) in POSIX_SHELLS else "/bin/sh"
LOGIN_SHELL_THAT_BECOMES_ITS_COMMAND = [SHELL, "-lic", 'exec "$@"', SHELL]
LOGIN_SHELL_THAT_OUTLIVES_ITS_COMMAND = [SHELL, "-lic", '"$@"; exec "$0" -li', SHELL]


def runs_an_agent(listed_terminal: dict) -> bool:
    """A terminal as `hc ls` lists it: its foreground program is Claude Code, or reports a status."""
    return listed_terminal.get("agent") == AGENT_PROGRAM or listed_terminal.get("status") is not None


class Terminals(Protocol):
    """What the scripts need from a terminal runtime. Addresses are the runtime's own, such as t7."""

    def ls(self) -> list[dict]:
        """Every live terminal, as `hc ls` lists it."""

    def get(self, address: str) -> dict | None:
        """The terminal as `hc get` prints it, or None when it is gone."""

    def launch(self, name: str | None, cwd: str, labels: dict[str, str], command: list[str], parent: str | None,
               space: str, tab: str | None, tab_label: str) -> dict:
        """Start `command` in a new terminal; the `hc get` shape plus `tab_named` and `running`."""

    def send(self, address: str, text: str, tagged: bool = True, *, structured: bool = False,
             timeout: float | None = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
        """Type text and Enter. tagged=False sends with no sender; structured asks for JSON output."""

    def read(self, address: str, lines: int | None = None, *, strict: bool = False,
             timeout: float | None = COMMAND_TIMEOUT_SECONDS) -> str:
        """The terminal's screen text. strict raises CheckError on failure; otherwise the output is returned as it is."""

    def keys(self, address: str, keys: list[str]) -> None: ...

    def set_labels(self, address: str, labels: dict[str, str], *, quiet_timeout: float | None = None) -> None:
        """Set labels. With quiet_timeout, failures are ignored and the call gives up after that many seconds."""

    def end(self, address: str) -> None: ...

    def self_address(self) -> str | None: ...

    def rename_tab(self, tab: str, label: str) -> bool: ...

    def focus_hint(self, address: str) -> str | None: ...

    def inbox_panel(self, address: str, pane: str | None) -> str | None:
        """Open the inbox mod's pane beside the terminal; the new pane id, or None when unsupported."""


def _run(command: list[str]) -> str:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"{command[0]}: {error}")
    if proc.returncode != 0:
        raise CheckError(f"{' '.join(command[:4])}: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


class HcTerminals:
    """herdr-core terminals, driven with `hc`, shown in the Herdr app, driven with `herdr-app`."""

    def ls(self) -> list[dict]:
        listed = json.loads(_run(["hc", "ls", "--json"])).get("terminals")
        if listed is None:
            raise CheckError("hc ls returned no terminals field")
        return listed

    def get(self, address: str) -> dict | None:
        try:
            got = subprocess.run(["hc", "get", address, "--json"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CheckError(f"hc get {address}: {error}")
        if got.returncode == HC_NOT_FOUND:
            return None
        if got.returncode != 0:
            raise CheckError(f"hc get {address}: {(got.stderr or got.stdout).strip()[:300]}")
        return json.loads(got.stdout)

    def launch(self, name: str | None, cwd: str, labels: dict[str, str], command: list[str], parent: str | None,
               space: str, tab: str | None, tab_label: str) -> dict:
        """Start a coding agent in a new herdr-core terminal shown in the Herdr app.

        command is the agent's program and its arguments, such as ["claude",
        "--model", "opus"]. With a parent, the app lists the new agent under that
        terminal's agent and the terminal keeps running when the parent's program
        ends, and ends when the agent exits. Without one, the terminal keeps a
        shell when the agent exits. The
        terminal joins the tab with that id, or gets a new one, and the tab is
        named tab_label. Returns the new terminal as `hc get` prints it, with
        `running` false when no agent showed in it: `hc run` returns before
        the agent starts, and the terminal ends at once when it cannot.
        """
        if name is not None and not TERMINAL_NAME.fullmatch(name):
            raise CheckError(f"{name!r} is not a terminal name: 1 to 32 of a-z, 0-9, - and _")
        tab = tab or f"tb{uuid.uuid4().hex}"
        all_labels = {"herdr.space": space, "herdr.tab": tab, **labels}
        lineage = ["--parent", parent, "--outlive-parent", "--no-notify"] if parent else ["--no-parent"]
        run_command = ["hc", "run", "--bg", "--json", *(["--name", name] if name else []), "--cwd", cwd, *lineage]
        for key, value in all_labels.items():
            run_command += ["--label", f"{key}={value}"]
        shell = LOGIN_SHELL_THAT_BECOMES_ITS_COMMAND if parent else LOGIN_SHELL_THAT_OUTLIVES_ITS_COMMAND
        started = json.loads(_run([*run_command, "--", *shell, *command]))
        try:
            terminal = self.get(started["address"]) or dict(started, labels=all_labels)
        except CheckError:
            terminal = dict(started, labels=all_labels)
        terminal["tab_named"] = self.rename_tab(tab, tab_label)
        terminal["running"] = self._agent_showed_in(started["address"])
        return terminal

    def _agent_showed_in(self, address: str) -> bool:
        deadline = time.time() + SECONDS_TO_WAIT_FOR_AGENT
        while time.time() < deadline:
            try:
                listed = next((t for t in self.ls() if t["address"] == address), None)
            except CheckError:
                listed = {}
            if listed is None:
                return False
            if runs_an_agent(listed):
                return True
            time.sleep(1)
        return False

    def send(self, address: str, text: str, tagged: bool = True, *, structured: bool = False,
             timeout: float | None = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
        """`hc send`. From a terminal, it wraps the text in an <hc from="..."> tag, which a shell cannot run;
        tagged=False removes the sender, so there is no tag. structured sends `--json --` before the address."""
        command = ["hc", "send", *(["--json", "--"] if structured else []), address, text]
        environment = None if tagged else {key: value for key, value in os.environ.items() if key != "HC_TERMINAL_ID"}
        return subprocess.run(command, capture_output=True, text=True, env=environment, timeout=timeout)

    def read(self, address: str, lines: int | None = None, *, strict: bool = False,
             timeout: float | None = COMMAND_TIMEOUT_SECONDS) -> str:
        command = ["hc", "read", *(["--lines", str(lines)] if lines is not None else []), address]
        if strict:
            return _run(command)
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout).stdout

    def keys(self, address: str, keys: list[str]) -> None:
        _run(["hc", "keys", address, *keys])

    def set_labels(self, address: str, labels: dict[str, str], *, quiet_timeout: float | None = None) -> None:
        command = ["hc", "label", address, *(f"{key}={value}" for key, value in labels.items())]
        if quiet_timeout is None:
            _run(command)
            return
        try:
            subprocess.run(command, capture_output=True, timeout=quiet_timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def end(self, address: str) -> None:
        """End a terminal's program, if it still runs, and remove its record.

        The app closes a tab when its last terminal ends.
        """
        try:
            subprocess.run(["hc", "kill", address], capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CheckError(f"hc kill {address}: {error}")
        _run(["hc", "rm", address])

    def self_address(self) -> str | None:
        return os.environ.get("HC_TERMINAL_ID")

    def rename_tab(self, tab: str, label: str) -> bool:
        """Name a Herdr app tab. The app takes about two seconds to show a tab for a new terminal."""
        deadline = time.time() + SECONDS_TO_WAIT_FOR_TAB
        while time.time() < deadline:
            try:
                renamed = subprocess.run(["herdr-app", "do", "tab.rename", json.dumps(dict(id=tab, name=label))],
                                         capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if renamed.returncode == 0:
                return True
            time.sleep(0.5)
        return False

    def focus_hint(self, address: str) -> str | None:
        """The line naming a terminal's address in the app, when the app runs on another machine."""
        machine = CONFIG["herdr_machine"]
        if not machine:
            return None
        app_address = f"{machine}/{address}"
        return (f"In the Herdr app, on the machine that runs it, this terminal is {app_address}: "
                f"herdr-app do agent.focus '{json.dumps(dict(terminal=app_address))}'")

    def inbox_panel(self, address: str, pane: str | None) -> str | None:
        if pane is None:
            shown = json.loads(_run(["herdr-app", "ls"]))
            pane = next((shown_terminal["pane"] for space in shown["spaces"] for tab in space["tabs"]
                         for shown_terminal in tab["terminals"] if shown_terminal["terminal"] == address), None)
            if pane is None:
                raise CheckError(f"the app shows no pane for terminal {address}")
        panel = json.loads(_run(["herdr-app", "do", "pane.split", json.dumps(dict(direction="right", pane=pane, mod="inbox"))]))
        return panel["pane"]


@functools.lru_cache(maxsize=None)
def current() -> Terminals:
    runtime = CONFIG["runtime"]
    if runtime == "hc":
        return HcTerminals()
    raise CheckError(f"runtime {runtime} is not implemented yet")
