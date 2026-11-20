#!/usr/bin/env python3
"""
Start a delegate in its own Herdr app tab, listed under its coordinator.

  delegate_start.py <name> --task "<task>" --cwd <worktree> --brief <path> --model sonnet|opus|fable [--effort <effort>]
  delegate_start.py <name> --task "<task>" --cwd <worktree> --resume <session id>

Run it from the coordinator's terminal, after the worktree and the brief
exist. It runs the cap check, then starts Claude Code in a new herdr-core
terminal whose first message is "Read <brief> and do what it says." With
--resume it starts that existing session again and sends nothing: use it
for a delegate whose terminal is gone, with the session id from its backlog
handle. A delegate ended with delegate_end.py comes back with
`delegate_end.py --resume <name>`. The terminal is this coordinator's child,
so the app lists the delegate under it, and it keeps running when the
coordinator's session ends. Its tab is named "<domain> [<task>]" and its
terminal carries fleet.domain=<domain>, which is how delegate_cap.py,
fleet_status.py and fleet_watch.py find it.

<name> is the terminal's name: 1 to 32 of a-z, 0-9, - and _. Use it with
`hc send`, `hc read`, `hc get` and the other scripts.

It prints the delegate's backlog handle. Write that line under the task in
your backlog's Open section.

Exit status: 0 started, 1 refused by the cap check, 2 the start could not run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import fleet  # noqa: E402
from fleet import CheckError  # noqa: E402

CLAUDE_CODE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="the delegate's terminal name")
    parser.add_argument("--task", required=True, help='a few words for the tab, as in "retry button"')
    parser.add_argument("--cwd", required=True, help="the delegate's worktree, or the domain's folder when it has no repo")
    parser.add_argument("--brief", help="path to the brief the delegate reads first")
    parser.add_argument("--model", choices=["sonnet", "opus", "fable"], help="for a new Claude Code delegate")
    parser.add_argument("--effort", choices=CLAUDE_CODE_EFFORTS,
                        help="for a new delegate. Without it, the delegate runs at its settings' effort")
    parser.add_argument("--resume", metavar="SESSION", help="start this existing session again instead of a new one")
    args = parser.parse_args()
    if bool(args.resume) == bool(args.brief and args.model):
        parser.error("for a new delegate pass --brief with --model; for an existing one pass --resume <session id>")

    try:
        herd = fleet.Fleet()
        domain = herd.own_domain()
        coordinator = herd.coordinator_of(domain)
        cwd = os.path.realpath(os.path.expanduser(args.cwd))
        brief = os.path.realpath(os.path.expanduser(args.brief)) if args.brief else None
        if not os.path.isdir(cwd):
            raise CheckError(f"no folder at {cwd}")
        if brief and not os.path.isfile(brief):
            raise CheckError(f"no brief at {brief}")
        if fleet.keep_scratch_out_of_git(cwd):
            print(f"Git did not ignore .scratch/ in {cwd}, so it was added to the repo's info/exclude")
        if fleet.find_agent(herd.agents, args.name):
            raise CheckError(f"a live agent is already named {args.name}")
        if args.resume and any(fleet.session_of(a) == args.resume for a in herd.agents):
            raise CheckError(f"session {args.resume} is already running")
        if args.resume and not fleet.transcript_of(args.resume):
            raise CheckError(f"no transcript for session {args.resume}")
        if args.resume and args.effort:
            raise CheckError(f"session {args.resume} keeps the effort it has")
        if not coordinator.get("space"):
            raise CheckError(f"terminal {coordinator['address']} has no herdr.space label, so the new tab has no space to open in")

        cap_flags = ["--resuming", args.name] if args.resume else []
        cap = subprocess.run([sys.executable, str(Path(__file__).with_name("delegate_cap.py")), *cap_flags],
                             capture_output=True, text=True)
        print(cap.stdout + cap.stderr, end="")
        if cap.returncode != 0:
            return cap.returncode

        tab_label = f"{domain} [{args.task}]"
        first_message = None if args.resume else f"Read {brief} and do what it says."
        session = args.resume or str(uuid.uuid4())
        effort_flags = ["--effort", args.effort] if args.effort else []
        how_it_starts = (["--resume", session] if args.resume
                         else ["--model", args.model, *effort_flags, "--session-id", session, first_message])
        program = [fleet.CLAUDE, *fleet.DELEGATE_FLAGS, "--name", tab_label, *how_it_starts]
        terminal = fleet.launch(
            name=args.name, cwd=cwd, tab_label=tab_label, space=coordinator["space"],
            labels={"fleet.domain": domain, "fleet.task": args.task},
            program=config.with_delegate_env(program), parent=coordinator["address"],
        )
        if not terminal["running"]:
            raise CheckError(f"no agent showed in terminal {terminal['address']} within {fleet.SECONDS_TO_WAIT_FOR_AGENT} "
                             f"seconds. It shows:\n{fleet.terminal_text(terminal['address'])}\n"
                             f"Remove a finished terminal with `hc rm {terminal['address']}` before trying again")
        ended_record = fleet.ended_record_path(session)
        if ended_record.exists():
            ended_record.unlink()
    except (CheckError, OSError, KeyError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"delegate_start: could not start: {type(error).__name__}: {error}", file=sys.stderr)
        return 2

    address = terminal["address"]
    _, branch, commit, _ = fleet.repo_of(cwd, {})
    started = dt.datetime.now(dt.timezone.utc).strftime("%m-%d %H:%MZ")
    model_and_effort = " ".join(filter(None, [args.model, args.effort]))
    print(f"started {args.name} as terminal {address}, tab \"{tab_label}\". Backlog handle:")
    print(f"  - {args.name} | {address} | \"{tab_label}\" | {cwd} | {branch or commit or 'no repo'} | {session} | "
          + (f"resumed {started}" if args.resume else f"{model_and_effort}; started {started}; brief {brief}"))
    if args.resume:
        print(f"Nothing was sent. Send it the work with:\n"
              f"  {fleet.SCRIPTS}/delegate_cap.py --to {args.name} --send \"<what arrived>\"")
    if not terminal["tab_named"]:
        print(f"The app did not name the tab. Name it with: herdr-app do tab.rename "
              f"'{json.dumps(dict(id=terminal['labels']['herdr.tab'], name=tab_label))}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
