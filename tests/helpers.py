"""Shared test fixtures: temp config/state dirs and stub hc/gh binaries."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COORD_SCRIPTS = ROOT / "skills" / "coordinator" / "scripts"
SWEEP_SCRIPTS = ROOT / "skills" / "fleet-sweep" / "scripts"


def add_script_paths() -> None:
    for p in (str(COORD_SCRIPTS), str(SWEEP_SCRIPTS)):
        if p not in sys.path:
            sys.path.insert(0, p)


class Sandbox:
    """Isolated temp workspace with config.json, backlog_dir, state_dir, and stub PATH."""

    def __init__(self, config_overrides: dict | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.backlog_dir = self.root / "backlogs"
        self.backlog_dir.mkdir()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.config_path = self.root / "config.json"
        cfg = {
            "backlog_dir": str(self.backlog_dir),
            "state_dir": str(self.state_dir),
            "check_for_updates": False,
        }
        if config_overrides:
            cfg.update(config_overrides)
        self.config_path.write_text(json.dumps(cfg))

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def write_stub(self, name: str, script: str) -> Path:
        path = self.bin_dir / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def write_rex_stub(self, blocks: list[dict], statuses: dict, processes: dict, gone: set[str],
                       fail_ls: bool = False, *, screens: dict | None = None, fail_paste: bool = False,
                       new_block: str = "block:new", new_window: str = "window:new") -> Path:
        """A fake `rex` on PATH. blocks are `rex block ls` entries plus their session_id; statuses
        and processes are the `program_status` and `process` answers by block id; a block in gone
        answers as a closed one does (exit 3, not found). Every call's argv goes to rex.calls as a
        JSON list, and the pasted event JSON to rex.stdin."""
        fixtures = dict(blocks=blocks, statuses=statuses, processes=processes, gone=sorted(gone),
                        fail_ls=fail_ls, screens=screens or {}, fail_paste=fail_paste,
                        new_block=new_block, new_window=new_window)
        (self.root / "rex-fixtures.json").write_text(json.dumps(fixtures))
        path = self.bin_dir / "rex"
        path.write_text(f"#!{sys.executable}\n" + REX_STUB.replace("ROOT", repr(str(self.root))))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def rex_calls(self) -> list[list[str]]:
        """Each `rex` call's arguments, in order, without the leading --autostart=false."""
        path = self.root / "rex.calls"
        if not path.exists():
            return []
        calls = [json.loads(line) for line in path.read_text().splitlines()]
        return [call[1:] if call[:1] == ["--autostart=false"] else call for call in calls]

    def write_backlog(self, domain: str, text: str) -> Path:
        path = self.backlog_dir / f"{domain}-backlog.md"
        path.write_text(text)
        return path

    def write_notes(self, domain: str, text: str, later: str | None = None) -> Path:
        path = self.backlog_dir / f"{domain}-notes.md"
        path.write_text(text)
        if later is not None:
            (self.backlog_dir / f"{domain}-later.md").write_text(later)
        return path

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.backlog_dir), *args], check=True, capture_output=True)

    def commit_all(self, message: str = "save") -> None:
        if not (self.backlog_dir / ".git").exists():
            self.git("init", "-q")
            self.git("config", "user.email", "test@example.com")
            self.git("config", "user.name", "Test")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run_python(self, code: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(COORD_SCRIPTS)!r})\n{code}"],
            capture_output=True,
            text=True,
            env=self.env(env_extra),
            cwd=str(self.root),
            timeout=30,
        )

    def env(self, extra: dict | None = None) -> dict:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        env["COORDINATOR_CONFIG"] = str(self.config_path)
        env.pop("CLAUDECODE", None)
        if extra:
            env.update(extra)
        return env

    def run_script(self, path: Path, *args: str, env_extra: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(path), *args],
            capture_output=True,
            text=True,
            env=self.env(env_extra),
            cwd=str(cwd or self.root),
            timeout=30,
        )


REX_STUB = r'''
import json, sys
from pathlib import Path

root = Path(ROOT)
argv = sys.argv[1:]
with (root / "rex.calls").open("a") as log:
    log.write(json.dumps(argv) + "\n")
f = json.loads((root / "rex-fixtures.json").read_text())
if argv[:1] == ["--autostart=false"]:
    argv = argv[1:]
blocks = {b["block_id"]: b for b in f["blocks"]}


def out(value):
    print(json.dumps(value))
    sys.exit(0)


def not_found(block):
    if block in f["gone"] or block not in blocks:
        print(f'Error: block target "{block}" not found', file=sys.stderr)
        sys.exit(3)


def placed(block):
    return {k: v for k, v in blocks[block].items() if k != "session_id"}


if argv[:2] == ["ls", "--json"]:
    if f["fail_ls"]:
        print("Error: connect to the Rex server: connection refused", file=sys.stderr)
        sys.exit(1)
    sessions = list(dict.fromkeys(b["session_id"] for b in f["blocks"]))
    out(dict(sessions=[dict(session_id=s, label=s.split(":")[-1]) for s in sessions]))
if argv[:2] == ["block", "ls"]:
    session = argv[argv.index("-s") + 1]
    out(dict(blocks=[placed(b) for b in blocks if blocks[b]["session_id"] == session]))
if argv[:2] == ["block", "inspect"]:
    not_found(argv[2])
    out(dict(session_id=blocks[argv[2]]["session_id"], block=placed(argv[2]), api={}))
if argv[:2] == ["block", "call"]:
    block = argv[argv.index("-b") + 1]
    not_found(block)
    method = argv[argv.index("-b") + 2]
    if method == "process":
        out(f["processes"].get(block, {}))
    out(f["statuses"].get(block, {"records": []}))
if argv[:1] == ["capture"]:
    block = argv[argv.index("-b") + 1]
    not_found(block)
    print(f["screens"].get(block, ""))
    sys.exit(0)
if argv[:2] == ["window", "new"] or argv[:1] == ["run"]:
    out(dict(window_id=f["new_window"], block_ids=[f["new_block"]], revision=1))
if argv[:2] == ["api", "call"]:
    with (root / "rex.stdin").open("a") as log:
        log.write(sys.stdin.read().strip() + "\n")
    if f["fail_paste"]:
        print("Error: paste refused", file=sys.stderr)
        sys.exit(1)
    out({})
if argv[:1] in (["send"], ["send-key"]):
    not_found(argv[argv.index("-b") + 1])
if argv[:2] in (["block", "rename"], ["block", "close"]):
    not_found(argv[2])
sys.exit(0)
'''
