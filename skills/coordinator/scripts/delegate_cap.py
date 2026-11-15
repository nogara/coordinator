#!/usr/bin/env python3
"""
Refuse a coordinator's fourth working delegate, and new work that would open
a pull request while its repo is at the limit on open ones.

A coordinator runs this before starting a delegate, and sends a delegate
new work or an answer through it with --send. It exits 0 when the step may
go ahead and 1 when it must wait in the backlog queue.

  delegate_cap.py                       may I start another delegate?
  delegate_cap.py --to <agent>          may I send <agent> work or an answer?
  delegate_cap.py --to <agent> --send "<text>"
                                        send it, unless the check refuses

Run it from the coordinator's terminal, or name the domain. A delegate is
yours when its terminal carries fleet.domain=<your domain>, which
delegate_start.py sets, or carries no such label and your backlog's Open
section names its session id (the first 8 characters are enough). An
unlabelled agent named in two backlogs counts toward both. A delegate is
working when `hc ls` shows its status as "working".

Sending to a delegate that is already working leaves the count unchanged,
so it is allowed even at the cap.

The limits on open pull requests come from pr_limits in the config: the
most open pull requests from the signed-in GitHub account in each repo. A
limit applies to starting a delegate, in a domain that works in that repo,
and never to --to. At or over the limit, a
start is refused unless it opens no new pull request:

  --resuming <agent>    the start resumes that ended delegate
  --for-pr <number>     the delegate works on that open pull request
  --user-asked "<their words>"
                        the user asked for this work; note in the backlog
                        that it started over the limit. Pass it only when
                        the check refuses a start for the limit

Each run is logged to <state_dir>/cap-checks.jsonl.

Exit status: 0 allowed, 1 refused, 2 the check could not run. With --send,
an allowed message that `hc send` then fails to deliver exits with 2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet  # noqa: E402
from fleet import CheckError  # noqa: E402

CAP = 3
SECONDS_TO_WAIT_FOR_A_SEND = 120
LOG = fleet.STATE_DIR / "cap-checks.jsonl"


def described(member: dict) -> str:
    agent = member["agent"]
    placed_by = "labelled fleet.domain" if member["by_label"] else "backlog names its session"
    return f"{agent.get('name') or '(unnamed)'} {agent['address']} [{member['task']}]  ({placed_by})"


def record(entry: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as log:
            log.write(json.dumps(dict(at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **entry)) + "\n")
    except OSError:
        pass


def pull_request_is_open(slug: str, number: int) -> bool:
    try:
        out = fleet.run(["gh", "pr", "view", str(number), "-R", slug, "--json", "state", "-q", ".state"])
    except CheckError as error:
        if "Could not resolve" in str(error):
            return False
        raise
    return out.strip() == "OPEN"


def sent(to: str, text: str, entry: dict) -> int:
    """Send the allowed message with `hc send`, print what it printed, and log whether it arrived."""
    try:
        send = subprocess.run(["hc", "send", "--json", "--", to, text], capture_output=True, text=True,
                              timeout=SECONDS_TO_WAIT_FOR_A_SEND)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"delegate_cap: NOT SENT, hc send {to}: {error}", file=sys.stderr)
        record(dict(entry, decision="allowed", sent=False))
        return 2
    print((send.stdout or send.stderr).strip())
    record(dict(entry, decision="allowed", sent=send.returncode == 0))
    if send.returncode != 0:
        print(f"delegate_cap: NOT SENT, hc send {to} exited with {send.returncode}", file=sys.stderr)
        return 2
    return 0


def allowed(entry: dict, to: dict | None, text: str | None) -> int:
    """Log an allowed step, sending the text first when there is one."""
    if text is None:
        record(dict(entry, decision="allowed"))
        return 0
    return sent(to["address"], text, entry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", help="the coordinator's domain; default: the coordinator in this terminal")
    parser.add_argument("--to", metavar="AGENT", help="the delegate you are about to send work or an answer")
    parser.add_argument("--send", metavar="TEXT", help="send this text to the --to delegate when the check allows it")
    parser.add_argument("--resuming", metavar="AGENT", help="the start resumes this ended delegate")
    parser.add_argument("--for-pr", metavar="NUMBER", type=int, help="the new delegate works on this open pull request")
    parser.add_argument("--user-asked", metavar="WORDS", nargs="?", const="(their words were not given)",
                        help="the user asked for this work, in these words")
    parser.add_argument("--terminals", metavar="FILE", help="read a saved JSON list of `hc get` output instead of running hc")
    args = parser.parse_args()
    if args.send is not None and not args.to:
        parser.error("--send needs --to <agent>")
    if args.send is not None and not args.send.strip():
        parser.error("--send needs the text to send")

    try:
        herd = fleet.Fleet(args.terminals)
        domain = args.domain or herd.own_domain()
        coordinator = f"coordinator-{domain}"
        members = herd.members_of(domain)
        target = fleet.find_agent(herd.agents, args.to) if args.to else None
        if args.to and target is None:
            raise CheckError(f"no live agent named {args.to} in hc ls")
    except (CheckError, OSError, KeyError, json.JSONDecodeError) as error:
        print(f"delegate_cap: {error}", file=sys.stderr)
        return 2

    working = [member for member in members if member["agent"]["status"] == "working"]
    for member in working:
        print(f"working  {described(member)}")

    count = len(working)
    entry = dict(coordinator=coordinator, to=args.to, working=count)
    target_is_working = target is not None and any(member["agent"] is target for member in working)
    if target_is_working:
        print(f"ALLOWED  {coordinator}: {count} of {CAP} working; {args.to} is already one of them")
        return allowed(entry, target, args.send)
    if count >= CAP:
        step = f"sending {args.to} work" if args.to else "starting a delegate"
        not_sent = " NOT SENT." if args.send is not None else ""
        print(f"REFUSED  {coordinator}: {count} of {CAP} working;{not_sent} queue {step} in the backlog until one settles")
        record(dict(entry, decision="refused: cap"))
        return 1
    if args.to or args.resuming:
        print(f"ALLOWED  {coordinator}: {count} of {CAP} working")
        return allowed(dict(entry, resuming=args.resuming), target, args.send)

    try:
        limited = sorted(herd.repos_of(domain) & set(fleet.PR_LIMITS)) if domain in herd.backlogs else []
        open_counts = {slug: fleet.open_pull_request_count(slug) for slug in limited}
        over = [slug for slug in limited if open_counts[slug] >= fleet.PR_LIMITS[slug]]
        for_open_pr = bool(over) and args.for_pr is not None and any(pull_request_is_open(slug, args.for_pr) for slug in over)
    except (CheckError, OSError, KeyError, json.JSONDecodeError) as error:
        print(f"delegate_cap: could not check open pull requests: {error}", file=sys.stderr)
        return 2
    entry.update(open_prs=open_counts, for_pr=args.for_pr, user_asked=args.user_asked)
    for slug in over:
        print(f"at limit  {slug}: {open_counts[slug]} open pull requests from your GitHub account, limit {fleet.PR_LIMITS[slug]}")
    if over and args.for_pr is not None and not for_open_pr:
        print(f"REFUSED  {coordinator}: #{args.for_pr} is not an open pull request in {' or '.join(over)}")
        record(dict(entry, decision="refused: --for-pr is not open"))
        return 1
    if over and not for_open_pr and not args.user_asked:
        print(f"REFUSED  {coordinator}: start nothing that opens a pull request until the count is under the limit. "
              "Queue it in the backlog. To restart or resume a delegate you already have, pass --resuming <agent>. "
              "For work on an open pull request, pass --for-pr <number>. "
              "For work the user asked for, pass --user-asked \"<their words>\".")
        record(dict(entry, decision="refused: pull request limit"))
        return 1
    reason = ""
    if over and for_open_pr:
        reason = f"; work on open pull request #{args.for_pr}"
    elif over:
        reason = "; the user asked for it. Note in the backlog that it started over the limit"
    print(f"ALLOWED  {coordinator}: {count} of {CAP} working{reason}")
    record(dict(entry, decision="allowed: over the limit" if over else "allowed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
