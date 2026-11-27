"""
Where the fleet's terminals live, and the one place that talks to them.

Every call to `hc` (herdr-core) or `herdr-app` (the Herdr app) goes through
HcTerminals, and every call to `rex` through RexTerminals; the rest of the
scripts ask `current()` for the backend the user's `runtime` setting names.
Terminals is the interface they rely on.
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

import labels_store
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

    def get(self, address: str, *, strict: bool = False) -> dict | None:
        """The terminal as `hc get` prints it, or None when it is gone. strict raises CheckError for a missing one too."""

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

    def discard(self, address: str) -> None:
        """Drop a terminal a coordinator was started away from: its record, and on Rex the block itself."""

    def own_label_command(self, address: str | None) -> str:
        """The command a coordinator runs once to label its own terminal, for error messages."""

    def end_command(self, address: str) -> str:
        """The command, in backquotes, that ends a terminal by hand, for error messages."""

    def remove_command(self, address: str) -> str:
        """The command, in backquotes, that removes a finished terminal by hand, for error messages."""


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

    def get(self, address: str, *, strict: bool = False) -> dict | None:
        if strict:
            return json.loads(_run(["hc", "get", address, "--json"]))
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

    def discard(self, address: str) -> None:
        """Remove an ended terminal's record."""
        _run(["hc", "rm", address])

    def own_label_command(self, address: str | None) -> str:
        return 'hc label "$HC_TERMINAL_ID" fleet.role=coordinator fleet.domain=<domain>'

    def end_command(self, address: str) -> str:
        return f"`hc kill {address}`, then `hc rm {address}`"

    def remove_command(self, address: str) -> str:
        return f"`hc rm {address}`"


REX_NOT_FOUND = 3
REX_TERMINAL_FLAVOR = "com.superlogical.terminal"
# A stopped server must fail the call, not come back new and empty: an empty
# fleet would read as every delegate gone and prune every label file.
REX = ["rex", "--autostart=false"]
SECONDS_BETWEEN_PASTE_AND_ENTER = 0.5


class _RexGone(Exception):
    """The block is not on the server: closed, or never there."""


def _rex(args: list[str], *, timeout: float | None = COMMAND_TIMEOUT_SECONDS) -> str:
    """Run rex; its stdout. _RexGone when the block, window or session named is not found, CheckError on any other failure."""
    command = [*REX, *args]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"rex {' '.join(args[:3])}: {error}")
    if proc.returncode == REX_NOT_FOUND and "not found" in proc.stderr:
        raise _RexGone(proc.stderr.strip())
    if proc.returncode != 0:
        raise CheckError(f"rex {' '.join(args[:3])}: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


def _rex_json(args: list[str]) -> dict:
    out = _rex(args)
    try:
        return json.loads(out)
    except ValueError:
        raise CheckError(f"rex {' '.join(args[:3])} printed no JSON: {out.strip()[:200]}")


def _root_record(records: list[dict]) -> dict | None:
    return next((record for record in records if not record.get("id")), None)


def _agent_record(records: list[dict]) -> dict | None:
    """The root record when Claude Code reports it; other programs that report a status are not agents."""
    root = _root_record(records)
    if root and AGENT_PROGRAM in (root.get("app"), root.get("effective_app")):
        return root
    return None


class RexTerminals:
    """Rex blocks, driven with `rex`. Addresses are block ids; a block's labels live in labels_store.

    herdr.space is the block's session id and herdr.tab its window id, so the
    rest of the scripts keep reading them where hc puts them.
    """

    def _sessions(self) -> list[str]:
        try:
            sessions = _rex_json(["ls", "--json"]).get("sessions")
        except _RexGone as gone:
            raise CheckError(f"rex ls: {gone}")
        if sessions is None:
            raise CheckError("rex ls returned no sessions field")
        return [session["session_id"] for session in sessions]

    def _blocks(self, session: str) -> list[dict]:
        blocks = _rex_json(["block", "ls", "-s", session, "--json"]).get("blocks")
        if blocks is None:
            raise CheckError(f"rex block ls -s {session} returned no blocks field")
        return [block for block in blocks if (block.get("flavor") or "").startswith(REX_TERMINAL_FLAVOR)]

    def _records(self, address: str) -> list[dict]:
        return _rex_json(["block", "call", "-b", address, "program_status", "{}"]).get("records") or []

    def ls(self) -> list[dict]:
        listing_started = time.time()
        listed = []
        live = set()
        for session in self._sessions():
            try:
                blocks = self._blocks(session)
            except _RexGone:
                continue  # the session closed after `rex ls` listed it; its blocks are gone, not unread
            for block in blocks:
                live.add(block["block_id"])
                try:
                    root = _agent_record(self._records(block["block_id"]))
                except _RexGone:
                    continue
                listed.append(dict(address=block["block_id"], name=block.get("label"),
                                   agent=AGENT_PROGRAM if root else None,
                                   status=root))
        labels_store.prune(live, older_than=listing_started)
        return listed

    def get(self, address: str, *, strict: bool = False) -> dict | None:
        try:
            inspected = _rex_json(["block", "inspect", address, "--json"])
            process = _rex_json(["block", "call", "-b", address, "process", "{}"])
            records = self._records(address)
        except _RexGone:
            if strict:
                raise CheckError(f"rex: no block {address}; it was closed")
            return None
        block = inspected.get("block") or {}
        root = _agent_record(records)
        foreground = process.get("foreground") or {}
        program = foreground.get("invoked_path") or foreground.get("argv0")
        labels = labels_store.labels_for(address)
        return dict(
            address=address,
            state="live",
            name=block.get("label"),
            status={key: root.get(key) for key in ("state", "msg", "updated_at")} if root else None,
            records={record["id"]: dict(state=record.get("state"), title=record.get("title") or record.get("msg"))
                     for record in records if record.get("id")},
            foreground=dict(pid=foreground.get("pid"), cwd=foreground.get("cwd"),
                            argv=[program] if program else []) if foreground else None,
            cwd=foreground.get("cwd"),
            parent=labels.get("fleet.parent"),
            turn=None,
            labels={**labels, "herdr.space": inspected.get("session_id"), "herdr.tab": block.get("window_id")},
        )

    def launch(self, name: str | None, cwd: str, labels: dict[str, str], command: list[str], parent: str | None,
               space: str, tab: str | None, tab_label: str) -> dict:
        """Start a coding agent in a new window of the space's session, or as a split of window tab.

        As HcTerminals.launch: the shell outlives the agent unless there is a
        parent, and `running` is false when no agent showed in the block.
        """
        if name is not None and not TERMINAL_NAME.fullmatch(name):
            raise CheckError(f"{name!r} is not a terminal name: 1 to 32 of a-z, 0-9, - and _")
        shell = LOGIN_SHELL_THAT_BECOMES_ITS_COMMAND if parent else LOGIN_SHELL_THAT_OUTLIVES_ITS_COMMAND
        try:
            if tab:
                started = _rex_json(["run", "-w", tab, "--split", "right", "--focus=false",
                                     *(["--label", name] if name else []), "-c", cwd, "--json", "--", *shell, *command])
            else:
                started = _rex_json(["window", "new", tab_label, "-s", space, "-c", cwd, "--focus=false",
                                     "--shell", "none", "--json", "--", *shell, *command])
        except _RexGone as gone:
            where = f"window {tab}" if tab else f"session {space}"
            raise CheckError(f"rex: cannot open a block in {where}, which is gone: {gone}")
        address = (started.get("block_ids") or [started.get("block_id")])[0]
        if not address:
            raise CheckError(f"rex did not say which block it opened: {json.dumps(started)[:200]}")
        if name and not tab:
            try:
                _rex(["block", "rename", address, name])
            except _RexGone as gone:
                raise CheckError(f"rex: block {address} closed before it could be named {name}: {gone}")
        labels_store.merge(address, {**labels, **({"fleet.parent": parent} if parent else {})})
        window = started.get("window_id") or tab
        try:
            terminal = self.get(address)
        except CheckError:
            terminal = None
        terminal = terminal or dict(address=address, name=name,
                                    labels={**labels_store.labels_for(address), "herdr.space": space,
                                            "herdr.tab": window})
        terminal["tab_named"] = self.rename_tab(tab, tab_label) if tab else True
        terminal["running"] = self._agent_showed_in(address)
        return terminal

    def _agent_showed_in(self, address: str) -> bool:
        deadline = time.time() + SECONDS_TO_WAIT_FOR_AGENT
        while time.time() < deadline:
            try:
                terminal = self.get(address)
            except CheckError:
                terminal = {}
            if terminal is None:
                return False
            argv = (terminal.get("foreground") or {}).get("argv") or []
            if terminal.get("status") is not None or (argv and os.path.basename(argv[0]) == "claude"):
                return True
            time.sleep(1)
        return False

    def send(self, address: str, text: str, tagged: bool = True, *, structured: bool = False,
             timeout: float | None = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
        """Paste the text, then press Enter: two calls, and no Enter when the paste failed.

        From a block, the text starts with a <rex from="..."> line, as hc's
        sender tag; tagged=False leaves it out. structured has no meaning here.
        """
        sender = self.self_address() if tagged else None
        if sender:
            text = f'<rex from="{sender}">\n{text}'
        event = dict(block_id=address, event=dict(type="paste", payload=dict(text=text)))
        pasted = subprocess.run([*REX, "api", "call", "-s", address, "block.event", "-"], input=json.dumps(event),
                                capture_output=True, text=True, timeout=timeout)
        if pasted.returncode != 0:
            return pasted
        time.sleep(SECONDS_BETWEEN_PASTE_AND_ENTER)
        return subprocess.run([*REX, "send-key", "-b", address, "enter"], capture_output=True, text=True,
                              timeout=timeout)

    def read(self, address: str, lines: int | None = None, *, strict: bool = False,
             timeout: float | None = COMMAND_TIMEOUT_SECONDS) -> str:
        if strict:
            try:
                screen = _rex(["capture", "-b", address, "--trim"], timeout=timeout)
            except _RexGone:
                raise CheckError(f"rex: no block {address}; it was closed")
        else:
            screen = subprocess.run([*REX, "capture", "-b", address, "--trim"], capture_output=True, text=True,
                                    timeout=timeout).stdout
        return screen if lines is None else "".join(screen.splitlines(keepends=True)[-lines:])

    def keys(self, address: str, keys: list[str]) -> None:
        """Single characters are typed with `rex send`; named keys such as enter are pressed with `rex send-key`."""
        index = 0
        while index < len(keys):
            typed = len(keys[index]) == 1
            end = index
            while end < len(keys) and (len(keys[end]) == 1) == typed:
                end += 1
            run = keys[index:end]
            try:
                if typed:
                    _rex(["send", "-b", address, "--", "".join(run)])
                else:
                    _rex(["send-key", "-b", address, *run])
            except _RexGone:
                raise CheckError(f"rex: no block {address}; it was closed")
            index = end

    def set_labels(self, address: str, labels: dict[str, str], *, quiet_timeout: float | None = None) -> None:
        """Merge into the block's label file. quiet_timeout ignores a failed write."""
        if quiet_timeout is None:
            labels_store.merge(address, labels)
            return
        try:
            labels_store.merge(address, labels)
        except OSError:
            pass

    def end(self, address: str) -> None:
        """Close the block's window when the block is alone in it, else the block. A gone block is left as it is."""
        try:
            inspected = _rex_json(["block", "inspect", address, "--json"])
            window = (inspected.get("block") or {}).get("window_id")
            in_window = [block for block in _rex_json(["block", "ls", "-s", inspected["session_id"], "--json"])
                         .get("blocks") or [] if block.get("window_id") == window]
            if window and len(in_window) == 1:
                _rex(["window", "close", window])
            else:
                _rex(["block", "close", address])
        except _RexGone:
            return

    def self_address(self) -> str | None:
        return os.environ.get("REX_BLOCK")

    def rename_tab(self, tab: str, label: str) -> bool:
        """Name a Rex window."""
        try:
            _rex(["window", "rename", tab, label])
        except (CheckError, _RexGone):
            return False
        return True

    def focus_hint(self, address: str) -> str | None:
        """The command that shows the block; printed for the user, never run, so focus is never taken."""
        return f"In Rex, this agent is block {address}: rex focus {address}"

    def inbox_panel(self, address: str, pane: str | None) -> str | None:
        print(f"Rex has no inbox panel, so none was opened beside block {address}. Typing /inbox in the session opens it.")
        return None

    def discard(self, address: str) -> None:
        """Close the block, as end does, and forget its labels."""
        self.end(address)
        labels_store.forget(address)

    def own_label_command(self, address: str | None) -> str:
        target = address or '"$REX_BLOCK"'
        return (f"python3 ~/.claude/skills/coordinator/scripts/fleet_term.py label {target} "
                "fleet.role=coordinator fleet.domain=<domain>")

    def end_command(self, address: str) -> str:
        return f"`rex block close {address}`"

    def remove_command(self, address: str) -> str:
        return f"`rex block close {address}`"


@functools.lru_cache(maxsize=None)
def current() -> Terminals:
    runtime = CONFIG["runtime"]
    if runtime == "hc":
        return HcTerminals()
    return RexTerminals()
