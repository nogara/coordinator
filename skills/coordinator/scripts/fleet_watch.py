#!/usr/bin/env python3
"""Wait until something in a coordinator's fleet needs attention, then exit.

  fleet_watch.py [--agent NAME]... [--pr REPO#N]... [--state FILE]
                 [--owner TERMINAL]

The state file defaults to <state_dir>/watch/<domain>.json, for the domain
of the coordinator in this terminal.

Watches the named delegates and pull requests, prints one line per event,
and exits. An agent is named by its herdr-core terminal name or address. The owner is
the terminal the watcher reports to: by default the one it runs in.
herdr-core is polled every 5 seconds and GitHub every 90. An urgent
event exits after a 15-second pause that gathers events that happen
together. Other events wait until 10 minutes after the last exit.

Urgent events:

  ask <agent> <count> · <text>   its inbox holds more items for the user
  gone <agent>                   no live terminal runs it any more
  unknown <agent>                its terminal has reported no status for 5 minutes
                                 with the Mac awake
  settled <agent> blocked        stopped on a dialog or a question
  settled <agent> error          stopped on an error: read its terminal
  pr <repo>#<n> merged|closed    the PR left the open state
  pr <repo>#<n> checks failure   checks finished and failed
  pr <repo>#<n> review changes_requested
  pr <repo>#<n> not found        GitHub has no such PR: fix the --pr argument

Other events:

  settled <agent> idle|done      finished a turn. A compaction while idle is
                                 not a turn and prints nothing.
  report <agent> <path>          its REPORT.md changed and it has settled
  pr <repo>#<n> checks success   checks finished and passed
  pr <repo>#<n> review <state>   review decision changed
  pr <repo>#<n> comments <n>     new reviews or comments from other accounts

Lines that end the watch without an event:

  blind hc|<repo> · <reason>     that source failed for 15 minutes with the Mac
                                 awake: arm again
  owner gone                     the owner's terminal ran no agent in two polls
  parent gone                    the shell that started it exited: arm again
  stopped                        you or a newer watcher stopped this one

Lines that end the watch with exit status 2:

  no agent <name>                no live agent has that name: fix or drop the --agent argument
  locked <pid>                   the watcher holding the state file's lock did not stop

Lines printed after the events, when the owner is a coordinator.
They never end the watch on their own:

  ended <agent> holds <repo>#<n> · resume: <command>
                                 an event above is for the PR of a delegate
                                 you ended with delegate_end.py. Run the
                                 command to give it the work.
  rule · <text>                  a standing rule and its command: the limit
                                 on open PRs once it is reached, and
                                 delegates quiet for over a day. Follow each.

A delegate ended with delegate_end.py prints no `gone` and no `no agent`.

`alive` is printed every 10 minutes while nothing fires, so Claude Code
sees output and sends no quiet-task check-in. It needs nothing.

The state file keeps the last snapshot, held events and the last exit time,
so a change made while the watch was not running fires once it is armed
again. The first run with a new state file records a snapshot and waits.
A delegate that comes back after `gone` starts a new snapshot.

A watcher holds a lock on <state>.lock while it runs. Starting one stops
the watcher holding that lock. Under Claude Code, a watcher started with a
trailing & cannot wake the session when it exits, so it refuses to start
and messages the owner instead.
"""
import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time

AGENT_POLL_SECONDS = 5
PR_POLL_SECONDS = 90
GATHER_SECONDS = 15
NON_URGENT_GAP_SECONDS = 600
ALIVE_SECONDS = 600
BLIND_SECONDS = 900
SECONDS_ASLEEP_THAT_COUNT_AS_A_SLEEP = 30
UNKNOWN_SECONDS = 300
COMMAND_TIMEOUT_SECONDS = 60
OWNER_MISSES_BEFORE_GONE = 2
NAME_MISSES_BEFORE_UNKNOWN = 2
LOCK_WAIT_SECONDS = 5
SETTLED = {"idle", "done", "blocked", "error"}
URGENT = re.compile(r"^(ask|gone|unknown|owner|pr \S+ (merged|closed|checks failure|review changes_requested|not found))|(blocked|error)$")
STATE_SOURCE = "hc"
RULE_SECONDS = 20

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402


def ended_delegates():
    """Records of delegates ended with delegate_end.py."""
    try:
        return fleet.ended_records()
    except Exception:
        return []


def was_ended(name, was, ended):
    """By session when the watcher saw the delegate alive, so a reused name is not mistaken for it."""
    records = [record for record in ended if isinstance(record, dict)]
    if was.get("session"):
        return any(record.get("session") == was["session"] for record in records)
    return any(record.get("name") == name for record in records)


def transcript_marks(agent):
    try:
        return fleet.transcript_marks(fleet.session_of(agent))
    except Exception:
        return None, None


def run_json(*args, cwd=None):
    """The command's parsed stdout and why it failed. Either is None when there is none."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"{args[0]} timed out after {COMMAND_TIMEOUT_SECONDS} seconds"
    except OSError as error:
        return None, f"{args[0]} did not run: {error}"
    failure = None
    if out.returncode:
        error_lines = out.stderr.strip().splitlines()
        failure = f"{args[0]} exit {out.returncode}: {error_lines[0][:200] if error_lines else 'no error text'}"
    try:
        return json.loads(out.stdout), failure
    except json.JSONDecodeError:
        return None, failure or f"{args[0]} printed no JSON"


class MacClock:
    """The time, and how long this Mac has stayed awake since an earlier time.

    time.monotonic() stops while the Mac sleeps and time.time() keeps
    running, so wall time that passed with no monotonic time is a sleep.
    time.monotonic() starts again from zero when the Mac restarts, so a
    lower reading than the last is a restart. Both count as a wake.

    last_reading is the reading an earlier watcher saved. It carries the
    last wake that watcher saw, and shows a sleep between the two watchers.
    """

    def __init__(self, last_reading=None):
        try:
            self.wall, self.awake, *saved_wake = (float(value) for value in last_reading)
        except (TypeError, ValueError):
            self.wall, self.awake, saved_wake = time.time(), time.monotonic(), []
        self.mac_woke_at = saved_wake[0] if saved_wake else 0
        self.read()

    def read(self):
        wall, awake = time.time(), time.monotonic()
        seconds_asleep = (wall - self.wall) - (awake - self.awake)
        if awake < self.awake or seconds_asleep >= SECONDS_ASLEEP_THAT_COUNT_AS_A_SLEEP:
            self.mac_woke_at = wall
        self.wall, self.awake = wall, awake
        return wall

    def reading(self):
        return [self.wall, self.awake, self.mac_woke_at]

    def awake_seconds_since(self, wall_time):
        """Seconds since wall_time, or since the Mac last woke when that is later."""
        now = self.read()
        return now - max(wall_time, self.mac_woke_at)


def live_agents():
    """Every live agent, or None when herdr-core could not be read."""
    try:
        return fleet.load_agents()
    except Exception:
        return None


def named(agents):
    """Each agent under its terminal name and its terminal address."""
    return {key: a for a in agents for key in (a.get("name"), a["address"]) if key}


def report_mtime(cwd, name, cwd_is_shared):
    """A repo delegate reports in its worktree's .scratch; a delegate with no repo reports beside its brief.

    A .scratch/REPORT.md in a cwd that another watched delegate shares
    cannot be told apart, so it is not read.
    """
    candidates = [os.path.join(cwd, ".scratch", "REPORT.md")] if cwd and not cwd_is_shared else []
    candidates.append(os.path.join(fleet.BRIEFS_DIR, name, "REPORT.md"))
    for path in candidates:
        if os.path.exists(path):
            return path, os.path.getmtime(path)
    return None, None


def agent_snapshot(names, agents, seen, clock, ended=()):
    by_name = named(agents)
    watched_cwds = [by_name[name].get("cwd") for name in names if name in by_name]
    snap = {}
    for name in names:
        a = by_name.get(name)
        was = seen.get(name, {})
        if a is None:
            snap[name] = {"present": False, "ended": was_ended(name, was, ended), "session": was.get("session")}
            continue
        status = a["status"]
        turn_ended = status in SETTLED and (a["turn"] != was.get("turns") or was.get("status") not in SETTLED)
        if turn_ended or "responded" not in was:
            responded, compacted = transcript_marks(a)
        else:
            responded, compacted = was.get("responded"), was.get("compacted")
        cwd = a["cwd"]
        path, mtime = report_mtime(cwd, name, watched_cwds.count(cwd) > 1)
        if status not in SETTLED:
            mtime = was.get("report_mtime", mtime)
        unknown_since = (was.get("unknown_since") or clock.read()) if status is None else None
        without_status_too_long = unknown_since is not None and (
            bool(was.get("stuck")) or clock.awake_seconds_since(unknown_since) >= UNKNOWN_SECONDS)
        snap[name] = {
            "present": True,
            "status": status,
            "turns": a["turn"],
            "asks": a["inbox_asks"],
            "inbox": f"{a['inbox_asks']} · {a['inbox_status']}",
            "report": path,
            "report_mtime": mtime,
            "unknown_since": unknown_since,
            "stuck": without_status_too_long,
            "responded": responded,
            "compacted": compacted,
            "session": a["session"],
        }
    return snap


def only_compacted(was, now):
    """The transcript gained a compaction and no assistant entry since the last snapshot."""
    return bool(
        now.get("compacted") and now["compacted"] != was.get("compacted")
        and was.get("responded") and now.get("responded") == was["responded"]
    )


def agent_events(old, new):
    for name, now in new.items():
        was = old.get(name)
        if was is None:
            continue
        if was.get("present") and not now["present"]:
            if not now.get("ended"):
                yield f"gone {name}"
            continue
        if not now["present"] or not was.get("present"):
            continue
        became_settled = now["status"] in SETTLED and was.get("status") not in SETTLED
        turn_ended = now["status"] in SETTLED and (now["turns"] != was.get("turns") or became_settled)
        if turn_ended and (now["status"] in ("blocked", "error") or not only_compacted(was, now)):
            yield f"settled {name} {now['status']}"
        if now["stuck"] and not was.get("stuck"):
            yield f"unknown {name}"
        if now["asks"] > was.get("asks", 0):
            yield f"ask {name} {now['inbox']}"
        report_moved = bool(was.get("report")) and now["report"] != was.get("report")
        if now["report_mtime"] and now["report_mtime"] != was.get("report_mtime") and not report_moved:
            yield f"report {name} {now['report']}"


PR_FIELDS = """state reviewDecision headRefName
  reviews(last: 100) { totalCount nodes { author { login } } }
  comments(last: 100) { totalCount nodes { author { login } } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }"""
ROLLUP_RESULT = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
                 "PENDING": "pending", "EXPECTED": "pending"}
repo_names = {}


def repo_name(repo):
    """The repo's owner/name and why GitHub did not give it. Either is None when there is none."""
    if repo in repo_names:
        return repo_names[repo], None
    data, failure = run_json("gh", "repo", "view", "--json", "nameWithOwner", cwd=os.path.expanduser(repo))
    name = data.get("nameWithOwner") if isinstance(data, dict) else None
    if not name:
        return None, failure or "gh repo view gave no nameWithOwner"
    repo_names[repo] = name
    return name, None


def count_from_others(connection, viewer):
    own = sum((node["author"] or {}).get("login") == viewer for node in connection["nodes"])
    return connection["totalCount"] - own


def pr_snapshot(prs):
    """One GraphQL query per repo, so a watch costs the same for 1 PR or 20.

    Returns the snapshot and, for each repo whose query failed, why. GitHub answers a
    query naming a missing PR with the other PRs' data plus an error, so the
    data is read whatever the exit status. A null PR is missing only when
    GitHub's error for it says NOT_FOUND; any other null is a failed poll.
    """
    by_repo = {}
    for spec in prs:
        repo, number = spec.rsplit("#", 1)
        by_repo.setdefault(repo, []).append(int(number))
    snap, failed = {}, {}
    for repo, numbers in by_repo.items():
        name, failure = repo_name(repo)
        if not name:
            failed[repo] = failure
            continue
        owner, short = name.split("/")
        fields = "\n".join(f"p{n}: pullRequest(number: {n}) {{ {PR_FIELDS} }}" for n in numbers)
        query = f'{{ viewer {{ login }} repository(owner: "{owner}", name: "{short}") {{ {fields} }} }}'
        data, failure = run_json("gh", "api", "graphql", "-f", f"query={query}")
        body = data.get("data") if isinstance(data, dict) else None
        repository = body and body.get("repository")
        if not repository:
            failed[repo] = failure or "GitHub's answer held no repository"
            continue
        viewer = (body.get("viewer") or {}).get("login")
        not_found = {
            error["path"][-1] for error in data.get("errors") or []
            if isinstance(error, dict) and error.get("type") == "NOT_FOUND" and error.get("path")
        }
        for n in numbers:
            pr = repository.get(f"p{n}")
            if not pr:
                if f"p{n}" in not_found:
                    snap[f"{repo}#{n}"] = {"state": "MISSING"}
                else:
                    failed[repo] = failure or f"GitHub's answer held no data for #{n}"
                continue
            nodes = pr["commits"]["nodes"]
            rollup = nodes and nodes[0]["commit"]["statusCheckRollup"]
            snap[f"{repo}#{n}"] = {
                "slug": name,
                "branch": pr.get("headRefName"),
                "state": pr["state"],
                "checks": ROLLUP_RESULT.get(rollup["state"]) if rollup else None,
                "review": pr.get("reviewDecision") or "",
                "comments": count_from_others(pr["reviews"], viewer) + count_from_others(pr["comments"], viewer),
            }
    return snap, failed


def pr_events(old, new):
    for spec, now in new.items():
        was = old.get(spec)
        label = f"pr {os.path.basename(spec.rsplit('#', 1)[0])}#{spec.rsplit('#', 1)[1]}"
        if now["state"] == "MISSING":
            if (was or {}).get("state") != "MISSING":
                yield f"{label} not found"
            continue
        if was is None or was["state"] == "MISSING":
            continue
        if now["state"] != was["state"] and now["state"] != "OPEN":
            yield f"{label} {now['state'].lower()}"
        if now["checks"] != was["checks"] and now["checks"] in ("success", "failure"):
            yield f"{label} checks {now['checks']}"
        if now["review"] != was["review"] and now["review"]:
            yield f"{label} review {now['review'].lower()}"
        if now["comments"] > was["comments"]:
            yield f"{label} comments {now['comments'] - was['comments']}"


def pr_label(spec):
    repo, number = spec.rsplit("#", 1)
    return f"{os.path.basename(repo)}#{number}"


def ended_lines(events, prs_seen, ended):
    """For each PR event whose PR belongs to an ended delegate: how to resume it."""
    lines = []
    for spec, pr in prs_seen.items():
        if not any(line.startswith(f"pr {pr_label(spec)} ") for line in events):
            continue
        for record in ended:
            if record.get("branch") and (record.get("repo"), record["branch"]) == (pr.get("slug"), pr.get("branch")):
                lines.append(f"ended {record['name']} holds {pr_label(spec)} · resume: "
                             f"{fleet.SCRIPTS}/delegate_end.py --resume {record['name']}")
    return lines


class RulesTimeout(BaseException):
    pass


def rule_lines(domain):
    """Standing rules for the domain's coordinator, each with its command."""
    herd = fleet.Fleet()
    if domain not in herd.backlogs:
        return []
    lines = []
    for slug in sorted(herd.repos_of(domain) & set(fleet.PR_LIMITS)):
        limit = fleet.PR_LIMITS[slug]
        try:
            count = fleet.open_pull_request_count(slug)
        except Exception:
            lines.append(f"rule · could not count the open PRs in {slug}, limit {limit} · run {fleet.SCRIPTS}/delegate_cap.py "
                         "before starting work that opens one")
            continue
        if count >= limit:
            lines.append(f"rule · {slug} has {count} open PRs from your GitHub account, limit {limit} · start nothing that "
                         "opens a PR; work on an open PR goes ahead, and so does what the user asks for")
    now = fleet.dt.datetime.now(fleet.dt.timezone.utc)
    quiet = 0
    for member in herd.members_of(domain) if domain not in fleet.DOMAINS_KEPT_RUNNING else []:
        agent = member["agent"]
        if agent["status"] in ("working", "blocked"):
            continue
        responded = fleet.last_response(fleet.session_of(agent), now)
        if responded and now - responded >= fleet.IDLE_BEFORE_ENDING:
            quiet += 1
    if quiet:
        lines.append(f"rule · {quiet} of your delegates quiet for over a day · list them with "
                     f"{fleet.SCRIPTS}/delegate_end.py --list-idle and end each one")
    return lines


def print_rules(agents, owner):
    """New lines may fail; the events above them are already printed and saved.

    A newer watcher's SIGTERM in this window ends the process without
    `stopped`, which would tell the coordinator to ignore the events above.
    """
    def give_up(signum, frame):
        raise RulesTimeout()
    signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(0))
    try:
        owning_agent = next((a for a in agents or [] if a["address"] == owner), None)
        if not owning_agent or owning_agent["role"] != "coordinator" or not owning_agent["domain"]:
            return
        signal.signal(signal.SIGALRM, give_up)
        signal.alarm(RULE_SECONDS)
        for line in rule_lines(owning_agent["domain"]):
            print(line, flush=True)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        signal.alarm(0)


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def merged(old, new):
    """Keep entries that a failed poll left out of the new snapshot."""
    return {**old, **new}


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


def print_stopped_and_exit(signum, frame):
    print("stopped", flush=True)
    sys.exit(0)


def started_with_trailing_ampersand():
    """A trailing & leaves the process with SIGINT ignored, then orphaned once its shell exits."""
    detached = os.getppid() == 1 or signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    return os.environ.get("CLAUDECODE") == "1" and detached


def parent_shell_gone():
    """Claude Code quit or its Bash wrapper died. Claude Code reports a background task as ended
    when the wrapper dies, so the owner is not messaged: after a quit its terminal holds a plain shell."""
    return os.environ.get("CLAUDECODE") == "1" and os.getppid() == 1


def tell_owner_it_stopped(owner):
    message = ("fleet_watch.py stopped: it was started with a trailing & or lost its parent shell, "
               "so its exit cannot wake this session. Nothing is watching the fleet. "
               "Start it again with Bash run_in_background and no &.")
    print(message, file=sys.stderr, flush=True)
    if owner:
        try:
            subprocess.run(["hc", "send", owner, message], capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


def owner_present(agents, owner, owner_session):
    """The owner's terminal still runs an agent, or its Claude session runs in another terminal."""
    return any(a["address"] == owner or (owner_session and a["session"] == owner_session) for a in agents)


def wake_time(first_event_at, first_urgent_at, last_wake_at):
    if first_urgent_at is not None:
        return first_urgent_at + GATHER_SECONDS
    if first_event_at is not None:
        return max(first_event_at + GATHER_SECONDS, last_wake_at + NON_URGENT_GAP_SECONDS)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", help="default: <state_dir>/watch/<domain>.json")
    parser.add_argument("--agent", action="append", default=[])
    parser.add_argument("--pr", action="append", default=[])
    parser.add_argument("--owner", default=os.environ.get("HC_TERMINAL_ID"))
    args = parser.parse_args()
    if not args.state:
        try:
            args.state = str(fleet.WATCH_DIR / f"{fleet.Fleet().own_domain()}.json")
        except Exception as error:
            print(f"fleet_watch: pass --state, since the default needs this terminal's domain: {error}", flush=True)
            return 2
    args.state = os.path.realpath(os.path.expanduser(args.state))

    if started_with_trailing_ampersand():
        tell_owner_it_stopped(args.owner)
        return 2
    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    lock = take_state_lock(args.state)
    if isinstance(lock, str):
        print(f"locked {lock}", flush=True)
        return 2
    signal.signal(signal.SIGTERM, print_stopped_and_exit)

    saved = load(args.state) or {}
    agents_seen = saved.get("agents", {}) if saved.get("source") == STATE_SOURCE else {}
    prs_seen = saved.get("prs", {})
    events = saved.get("pending", [])
    last_wake_at = saved.get("last_wake_at", 0)
    owner_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    started_at = time.time()
    agents_ok_at = started_at
    github_ok_at = {}
    clock = MacClock(saved.get("clock"))
    owner_misses = 0
    name_misses = {name: 0 for name in args.agent if not agents_seen.get(name, {}).get("present")}
    next_pr_poll = 0
    next_alive_at = started_at + ALIVE_SECONDS
    first_event_at = None
    first_urgent_at = None

    def save_state(pending):
        save(args.state, {"source": STATE_SOURCE, "agents": agents_seen, "prs": prs_seen, "pending": pending,
                          "last_wake_at": last_wake_at, "clock": clock.reading()})

    def end(line):
        save_state(events)
        print(line, flush=True)
        return 0

    while True:
        clock.read()
        if parent_shell_gone():
            return end("parent gone")

        agents = live_agents()
        if agents is None:
            if clock.awake_seconds_since(agents_ok_at) >= BLIND_SECONDS:
                return end(f"blind hc · hc ls failed for {BLIND_SECONDS // 60} minutes")
        else:
            agents_ok_at = time.time()
            if args.owner:
                owner_misses = 0 if owner_present(agents, args.owner, owner_session) else owner_misses + 1
                if owner_misses >= OWNER_MISSES_BEFORE_GONE:
                    return end("owner gone")
            ended = ended_delegates()
            listed = set(named(agents))
            for name in list(name_misses):
                if name in listed or was_ended(name, {}, ended):
                    del name_misses[name]
                else:
                    name_misses[name] += 1
            never_listed = [name for name, misses in name_misses.items() if misses >= NAME_MISSES_BEFORE_UNKNOWN]
            if never_listed:
                for name in never_listed:
                    print(f"no agent {name}", flush=True)
                return 2
            now_agents = agent_snapshot(args.agent, agents, agents_seen, clock, ended)
            events += agent_events(agents_seen, now_agents)
            agents_seen = merged(agents_seen, now_agents)

        if time.time() >= next_pr_poll:
            now_prs, failed = pr_snapshot(args.pr)
            events += pr_events(prs_seen, now_prs)
            prs_seen = merged(prs_seen, now_prs)
            next_pr_poll = time.time() + PR_POLL_SECONDS
            for spec in args.pr:
                repo = spec.rsplit("#", 1)[0]
                if repo not in failed:
                    github_ok_at[repo] = time.time()
                elif clock.awake_seconds_since(github_ok_at.get(repo, started_at)) >= BLIND_SECONDS:
                    return end(f"blind {os.path.basename(repo)} · GitHub queries failed for {BLIND_SECONDS // 60} minutes"
                               f" · {failed[repo]}")

        save_state(events)

        now = time.time()
        if events and first_event_at is None:
            first_event_at = now
        if first_urgent_at is None and any(URGENT.search(line) for line in events):
            first_urgent_at = now
        wake_at = wake_time(first_event_at, first_urgent_at, last_wake_at)
        if wake_at is not None and now >= wake_at:
            for line in events:
                print(line, flush=True)
            last_wake_at = now
            save_state([])
            try:
                for line in ended_lines(events, prs_seen, ended_delegates()):
                    print(line, flush=True)
            except Exception:
                pass
            if args.owner:
                print_rules(agents, args.owner)
            return 0
        if now >= next_alive_at:
            print("alive", flush=True)
            next_alive_at = now + ALIVE_SECONDS
        time.sleep(AGENT_POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
