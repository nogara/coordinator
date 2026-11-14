#!/usr/bin/env python3
"""Inventory a coordinator's leftovers and remove the ones that are safe.

  sweep.py report <repo>... [--base BRANCH]... [--keep PATH]... [--no-sizes]
  sweep.py remove <worktree> [--base BRANCH]... [--discard-untracked] [--park]
  sweep.py prune-branches <repo> (--list | <branch>...) [--base BRANCH]...

report prints one verdict per item: SAFE (remove it now), CLOSE (the task
looks finished but a session still runs there), ASK (finished-looking work
that would be lost), KEEP (in use). It changes nothing.

remove re-checks one worktree, moves its ignored files other than build output
to <main checkout>/.scratch/parked/<worktree name>, removes the worktree and
deletes its branch, and refuses when anything would be lost. With --park it
keeps the branch, or creates parked/<worktree name> for a detached worktree.
Each parked folder holds a PARKED_AT file with the time it was parked, for a
clean-up job that deletes old ones; nothing here deletes them. prune-branches
lists local branches whose work is on a base branch or whose PR merged, or
deletes the named ones and prints each one's hash.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

APP_WORKTREE_ROOTS = (
    os.path.expanduser("~/.codex/worktrees/"),
    "/.claude/worktrees/",
)
PROTECTED_BRANCHES = {"main", "master", "next", "develop"}
SELF_EXITING = {"caffeinate"}
SCRATCH_STALE_DAYS = 7
SCRATCH_LISTED_MB = 100
SIMULATOR_LISTED_MB = 500


PARKED_AT = ".parked-at"
PARKED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BUILD_OUTPUT = {
    "node_modules", ".derived-data", "dist", "build", ".next", "target", ".turbo", ".build",
    "coverage", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".swiftpm", "Pods", ".expo", "out", ".cache", ".vercel", "test-results", "playwright-report",
    ".svelte-kit", ".parcel-cache", "storybook-static", ".source", "generated", ".DS_Store",
    ".eslintcache", "DerivedData",
}
BUILD_FILE = re.compile(r"(\.log|\.tsbuildinfo|next-env\.d\.ts|\.pyc)$")


def is_build_output(rel):
    """Files a build makes again, which removing a worktree drops instead of parking."""
    return any(part in BUILD_OUTPUT for part in rel.split("/")) or bool(BUILD_FILE.search(rel))


def mark_parked(folder):
    with open(os.path.join(folder, PARKED_AT), "w") as f:
        f.write(time.strftime(PARKED_AT_FORMAT, time.gmtime()) + "\n")


def run(*args, cwd=None):
    try:
        out = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    except OSError:
        return None
    return out.stdout if out.returncode == 0 else None


def git(repo, *args):
    return run("git", "-C", repo, *args)


def default_bases(repo, extra):
    head = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    bases = [head.strip()] if head else []
    return bases + [b for b in extra if git(repo, "rev-parse", "--verify", "-q", b)]


def worktrees(repo):
    out = git(repo, "worktree", "list", "--porcelain") or ""
    for block in out.strip().split("\n\n"):
        fields = dict(
            line.split(" ", 1) if " " in line else (line, "")
            for line in block.splitlines()
        )
        yield fields.get("worktree", ""), fields.get("branch", "").removeprefix("refs/heads/")


def pull_requests(repo):
    out = run(
        "gh", "pr", "list", "--author", "@me", "--state", "all", "--limit", "500",
        "--json", "headRefName,headRefOid,state,number,mergedAt", cwd=repo,
    )
    if out is None:
        print(f"warning: gh pr list failed in {repo}, so worktrees whose PR merged show KEEP", file=sys.stderr)
    prs = {}
    for pr in json.loads(out) if out else []:
        prs.setdefault(pr["headRefName"], []).append(pr)
    return prs


def pr_summary(prs):
    if not prs:
        return None, ""
    states = {p["state"] for p in prs}
    label = ",".join(f"#{p['number']} {p['state'].lower()}" for p in prs)
    if "OPEN" in states:
        return "OPEN", label
    return ("MERGED" if "MERGED" in states else "CLOSED"), label


HC_NOT_FOUND = 3


def live_sessions():
    """Agent sessions in live herdr-core terminals, each with the folder it runs in now.

    None when herdr-core could not be read, apart from a terminal that ended
    between the listing and its lookup.
    """
    out = run("hc", "ls", "--json")
    if out is None:
        return None
    sessions = []
    for listed in json.loads(out).get("terminals", []):
        if listed.get("agent") != "claude-code" and listed.get("status") is None:
            continue
        try:
            got = subprocess.run(["hc", "get", listed["address"], "--json"], capture_output=True, text=True)
        except OSError:
            return None
        if got.returncode == HC_NOT_FOUND:
            continue
        if got.returncode != 0:
            return None
        terminal = json.loads(got.stdout)
        labels = terminal.get("labels") or {}
        cwd = (terminal.get("foreground") or {}).get("cwd") or terminal.get("cwd")
        sessions.append({
            "name": terminal.get("name") or terminal["address"],
            "tab": labels.get("where") or "no tab",
            "cwd": os.path.realpath(cwd) if cwd else "",
            "inbox": labels.get("inbox.status") if labels.get("inbox.needs-you") else None,
        })
    return sessions


def processes_by_cwd():
    """(pid, command, cwd or executable path) for every process."""
    out = run("lsof", "-d", "cwd,txt", "-Fpcn")
    if out is None:
        return None
    rows, pid, cmd = [], None, None
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = value
        elif tag == "c":
            cmd = value
        elif tag == "n":
            rows.append((pid, cmd, value))
    return rows


def inside(path, folder):
    """lsof gives real paths, and live_sessions resolves each cwd, so folder must be a real path."""
    return (path + "/").startswith(folder.rstrip("/") + "/")


def processes_inside(procs, folder):
    return sorted({
        f"{pid} {cmd}" for pid, cmd, name in procs
        if inside(name, folder) and cmd not in SELF_EXITING
    })


def unsaved_commits(worktree, bases):
    out = git(worktree, "log", "--oneline", "HEAD", "--not", "--remotes", *bases)
    return None if out is None else len(out.splitlines())


def dirty_files(worktree):
    out = git(worktree, "status", "--porcelain")
    return None if out is None else out.splitlines()


def size_mb(path):
    out = run("du", "-sk", path)
    return int(out.split()[0]) // 1024 if out else 0


def is_ancestor(repo, commit, descendant):
    return subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", commit, descendant],
        capture_output=True,
    ).returncode == 0


def branch_prs(repo, branch, prs):
    """A closed or merged PR speaks for the branch only when the branch holds
    nothing past that PR's head, so a reused branch name does not inherit it."""
    return [
        p for p in prs.get(branch, [])
        if p["state"] == "OPEN" or (p.get("headRefOid") and is_ancestor(repo, branch, p["headRefOid"]))
    ]


def never_committed(repo, branch):
    """Such a branch is an ancestor of its base, like a merged one. A missing
    reflog leaves it unknown, and it then counts as committed."""
    log = git(repo, "reflog", "show", "--format=%gs", f"refs/heads/{branch}")
    if not log:
        return False
    return not any(entry.startswith(("commit", "cherry-pick", "revert")) for entry in log.splitlines())


def branch_landed(repo, branch, bases, pr_state):
    if pr_state == "MERGED":
        return True
    return any(is_ancestor(repo, branch, base) for base in bases)


def worktree_rows(repo, bases, prs, sessions, procs, sizes, keep):
    main_checkout = os.path.realpath(repo)
    listed = list(worktrees(repo))
    candidates = [
        (path, branch) for path, branch in listed
        if os.path.realpath(path) != main_checkout
        and not any(r in path for r in APP_WORKTREE_ROOTS)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        yield from pool.map(
            lambda wt: worktree_row(repo, *wt, bases, prs, sessions, procs, sizes, keep),
            candidates,
        )
    landed = list(landed_branches(repo, bases, prs, {b for _, b in listed}))
    if landed:
        yield "SAFE", f"{len(landed)} landed branches in {repo} · list them: sweep.py prune-branches {repo} --list"


def worktree_row(repo, path, branch, bases, prs, sessions, procs, sizes, keep):
    pr_state, pr_label = pr_summary(branch_prs(repo, branch, prs))
    dirty = dirty_files(path)
    unsaved = unsaved_commits(path, bases)
    git_failed = dirty is None or unsaved is None
    real = os.path.realpath(path)
    here = [s for s in sessions if inside(s["cwd"], real)]
    pids = processes_inside(procs, real)
    on_base = branch in PROTECTED_BRANCHES or any(b.split("/")[-1] == branch for b in bases)
    not_started = bool(branch) and not pr_state and never_committed(repo, branch)
    landed = bool(branch) and not not_started and branch_landed(repo, branch, bases, pr_state)
    finished = not on_base and (landed or pr_state == "CLOSED")

    if on_base or any(inside(real, k) for k in keep):
        verdict = "KEEP"
    elif not branch or git_failed:
        verdict = "KEEP" if here else "ASK"
    elif here and (dirty or unsaved):
        verdict = "KEEP"
    elif dirty or unsaved:
        verdict = "ASK"
    elif here:
        verdict = "CLOSE" if finished else "KEEP"
    elif pids:
        verdict = "ASK"
    elif finished:
        verdict = "SAFE"
    else:
        verdict = "KEEP"

    notes = [pr_label or ("detached" if not branch else "base branch" if on_base else "no PR")]
    if landed and not pr_state:
        notes.append("merged into base")
    if git_failed:
        notes.append("git status or git log failed here")
    if dirty:
        notes.append(f"{len(dirty)} uncommitted")
    if unsaved:
        notes.append(f"{unsaved} commits on no remote or base")
    for s in here:
        notes.append(f"session {s['name']} in \"{s['tab']}\"" + (f" inbox: {s['inbox']}" if s["inbox"] else ""))
    if pids and not here:
        notes.append("processes: " + "; ".join(pids[:4]))
    size = f"{size_mb(path):>6}M " if sizes and verdict != "KEEP" else ""
    return verdict, f"{size}worktree {path} [{branch or 'detached'}] " + " · ".join(notes)


def landed_branches(repo, bases, prs, checked_out):
    """(branch, short sha, pr state) for local branches whose work is on a base."""
    out = git(repo, "for-each-ref", "--format=%(refname:short) %(objectname:short)", "refs/heads") or ""
    base_names = {b.split("/")[-1] for b in bases} | PROTECTED_BRANCHES
    for line in out.splitlines():
        branch, sha = line.split()
        if branch in checked_out or branch in base_names:
            continue
        pr_state, _ = pr_summary(branch_prs(repo, branch, prs))
        if pr_state != "OPEN" and branch_landed(repo, branch, bases, pr_state):
            yield branch, sha, pr_state


def checked_out_branches(repo):
    return {branch for _, branch in worktrees(repo)}


def orphan_process_rows(procs, worktree_roots):
    seen = set()
    for pid, cmd, path in procs:
        if pid in seen or cmd in SELF_EXITING:
            continue
        under_root = any(inside(path, r) for r in worktree_roots)
        if under_root and not os.path.exists(path):
            seen.add(pid)
            yield "SAFE", f"process {pid} {cmd} · runs in deleted folder {path}"


def scratch_rows(repo):
    """Leaves out parked/: the daily cleanup deletes parked folders."""
    scratch = os.path.join(repo, ".scratch")
    if not os.path.isdir(scratch):
        return
    now = time.time()
    small_count = small_mb = 0
    for name in sorted(os.listdir(scratch)):
        path = os.path.join(scratch, name)
        if name == "parked":
            continue
        age_days = int((now - os.lstat(path).st_mtime) / 86400)
        if age_days < SCRATCH_STALE_DAYS:
            continue
        mb = size_mb(path)
        if mb >= SCRATCH_LISTED_MB:
            yield "ASK", f"{mb:>6}M scratch {path} · untouched {age_days} days"
        else:
            small_count += 1
            small_mb += mb
    if small_count:
        yield "ASK", f"{small_mb:>6}M scratch {scratch}: {small_count} smaller entries untouched {SCRATCH_STALE_DAYS}+ days"


def simulator_rows():
    out = run("xcrun", "simctl", "list", "devices", "-j")
    if not out:
        return
    base = os.path.expanduser("~/Library/Developer/CoreSimulator/Devices")
    devices = [d for group in json.loads(out).get("devices", {}).values() for d in group]
    with ThreadPoolExecutor(max_workers=8) as pool:
        sizes = pool.map(lambda d: size_mb(os.path.join(base, d["udid"])), devices)
    for d, mb in zip(devices, sizes):
        if mb >= SIMULATOR_LISTED_MB:
            yield "ASK", f"{mb:>6}M simulator {d['name']} {d['udid']} {d['state']}"


def report(args):
    sessions = live_sessions()
    procs = processes_by_cwd()
    if sessions is None or procs is None:
        return refuse("hc ls or lsof failed, so no verdict is safe; run the report again")
    rows = []
    roots = set()
    for repo in args.repos:
        repo = os.path.abspath(os.path.expanduser(repo))
        bases = default_bases(repo, args.base)
        prs = pull_requests(repo)
        keep = [os.path.realpath(os.path.expanduser(k)) for k in args.keep]
        rows += worktree_rows(repo, bases, prs, sessions, procs, not args.no_sizes, keep)
        rows += scratch_rows(repo)
        roots.add(os.path.realpath(os.path.join(os.path.dirname(repo), "worktrees")))
        roots.add(os.path.realpath(os.path.join(repo, ".scratch")))
    rows += orphan_process_rows(procs, roots)
    if args.simulators:
        rows += simulator_rows()
    order = {"SAFE": 0, "CLOSE": 1, "ASK": 2, "KEEP": 3}
    for verdict, text in sorted(rows, key=lambda r: order[r[0]]):
        print(f"{verdict:5} {text}")
    return 0


def refuse(message):
    print(f"refused: {message}", file=sys.stderr)
    return 1


def ignored_paths(worktree):
    """(paths to park, build output to drop), relative to the worktree, or None
    when git cannot list them. `git worktree remove` deletes ignored files."""
    out = git(worktree, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
              "--directory", "--no-empty-directory")
    if out is None:
        return None
    park, drop = [], []
    for rel in out.split("\0"):
        if rel:
            split_build_output(worktree, rel.rstrip("/"), park, drop)
    return park, drop


def split_build_output(worktree, rel, park, drop):
    """git lists a folder whose contents are all ignored as one entry, and such
    a folder can hold build output, as pkg/ holds pkg/node_modules/. A .scratch
    folder moves whole: the build-output names include `out` and `.log`."""
    if ".scratch" in rel.split("/"):
        park.append(rel)
        return
    if is_build_output(rel):
        drop.append(rel)
        return
    full = os.path.join(worktree, rel)
    holds_build_output = os.path.isdir(full) and not os.path.islink(full) and any(
        is_build_output(name)
        for _, subdirs, files in os.walk(full) for name in subdirs + files
    )
    if not holds_build_output:
        park.append(rel)
        return
    for name in sorted(os.listdir(full)):
        split_build_output(worktree, f"{rel}/{name}", park, drop)


def listed(paths, limit=8):
    return ", ".join(paths[:limit]) + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def park_ignored(worktree, repo, paths):
    """Moves each path to the same place under parked/<worktree name>, and
    returns the folder and the moves, or moves them back and raises."""
    parked = os.path.join(repo, ".scratch", "parked")
    os.makedirs(parked, exist_ok=True)
    destination = os.path.join(parked, os.path.basename(worktree))
    if os.path.exists(destination):
        destination += time.strftime("-%Y%m%d-%H%M%S")
    moves = []
    try:
        for rel in paths:
            source = os.path.join(worktree, rel)
            target = os.path.join(destination, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(source, target)
            moves.append((source, target))
        mark_parked(destination)
    except OSError as error:
        try:
            unpark(destination, moves)
        except OSError as rollback_error:
            raise OSError(f"{error}; moving the files back also failed, so some are still in {destination}: {rollback_error}")
        raise OSError(f"{error}; the files moved back, so nothing changed")
    return destination, moves


def unpark(destination, moves):
    """Moves the files back, then deletes the parking record and the folders
    the moves left empty. rmdir refuses a folder that still holds a file."""
    for source, target in reversed(moves):
        shutil.move(target, source)
    record = os.path.join(destination, PARKED_AT)
    if os.path.exists(record):
        os.remove(record)
    for root, _, _ in os.walk(destination, topdown=False):
        try:
            os.rmdir(root)
        except OSError:
            pass


def parking_branch(repo, worktree):
    name = f"parked/{os.path.basename(worktree)}"
    candidate, n = name, 2
    while git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{candidate}"):
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def remove(args):
    path = os.path.realpath(os.path.expanduser(args.worktree))
    repo = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not repo:
        return refuse(f"{path} is not a git worktree")
    repo = os.path.dirname(repo.strip())
    if os.path.realpath(path) == os.path.realpath(repo):
        return refuse(f"{path} is the main checkout")
    branch = (git(path, "symbolic-ref", "--short", "-q", "HEAD") or "").strip()
    bases = default_bases(repo, args.base)

    dirty = dirty_files(path)
    unsaved = unsaved_commits(path, bases)
    sessions = live_sessions()
    procs = processes_by_cwd()
    if dirty is None or unsaved is None:
        return refuse("git status or git log failed in this worktree")
    if sessions is None or procs is None:
        return refuse("hc ls or lsof failed; try again")
    untracked_only = dirty and all(line.startswith("??") for line in dirty)
    discard = args.discard_untracked and not branch and untracked_only
    if dirty and not discard:
        return refuse(f"{len(dirty)} uncommitted files:\n" + "\n".join(dirty[:20]))
    if unsaved and not args.park:
        return refuse("commits on no remote or base branch; pass --park to keep the branch")
    if any(inside(s["cwd"], path) for s in sessions):
        return refuse("a Claude Code session still runs here; end its terminal first")
    pids = processes_inside(procs, path)
    if pids:
        return refuse("processes still run here: " + "; ".join(pids))
    ignored = ignored_paths(path)
    if ignored is None:
        return refuse("git ls-files failed, so the ignored files to keep are unknown")
    ignored, build_output = ignored

    created_branch = None
    if args.park and not branch:
        created_branch = parking_branch(repo, path)
        head = (git(path, "rev-parse", "HEAD") or "").strip()
        if not head or git(repo, "branch", created_branch, head) is None:
            return refuse(f"could not create branch {created_branch} for the detached worktree")
        branch = created_branch
    try:
        parked, moves = park_ignored(path, repo, ignored) if ignored else (None, [])
    except OSError as error:
        if created_branch:
            git(repo, "branch", "-D", created_branch)
        return refuse(f"could not park ignored files: {error}")
    cmd = ["git", "-C", repo, "worktree", "remove", path]
    if discard:
        cmd.insert(5, "--force")
    if subprocess.run(cmd).returncode != 0:
        if parked:
            unpark(parked, moves)
        if created_branch:
            git(repo, "branch", "-D", created_branch)
        return refuse("git worktree remove failed")
    print(f"removed worktree {path}")
    if parked:
        print(f"moved ignored files to {parked}, {size_mb(parked)} MB: {listed(ignored)}")
    if build_output:
        print(f"dropped build output: {listed(build_output)}")

    if created_branch:
        sha = (git(repo, "rev-parse", "--short", created_branch) or "").strip()
        print(f"created branch {created_branch} {sha} for the parked detached worktree")
    elif args.park:
        print(f"kept branch {branch} for the parked task")
    elif branch and branch not in PROTECTED_BRANCHES:
        sha = (git(repo, "rev-parse", "--short", branch) or "").strip()
        pr_state, _ = pr_summary(branch_prs(repo, branch, pull_requests(repo)))
        flag = "-D" if pr_state == "MERGED" else "-d"
        if subprocess.run(["git", "-C", repo, "branch", flag, branch]).returncode == 0:
            print(f"deleted branch {branch} {sha}")
        else:
            print(f"kept branch {branch} {sha}: not merged into a base")
    return 0


def prune_branches(args):
    repo = os.path.abspath(os.path.expanduser(args.repo))
    bases = default_bases(repo, args.base)
    prs = pull_requests(repo)
    for branch, sha, pr_state in landed_branches(repo, bases, prs, checked_out_branches(repo)):
        if args.list:
            print(f"{branch} {sha} {'PR merged' if pr_state == 'MERGED' else 'merged into base'}")
            continue
        if branch not in args.branches:
            continue
        flag = "-D" if pr_state == "MERGED" else "-d"
        if subprocess.run(["git", "-C", repo, "branch", flag, branch], capture_output=True).returncode == 0:
            print(f"deleted branch {branch} {sha}")
        else:
            print(f"kept branch {branch} {sha}: git refused")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("repos", nargs="+")
    rep.add_argument("--base", action="append", default=[])
    rep.add_argument("--keep", action="append", default=[])
    rep.add_argument("--no-sizes", action="store_true")
    rep.add_argument("--simulators", action="store_true", help="also list iOS simulators over 500 MB")
    rem = sub.add_parser("remove")
    rem.add_argument("worktree")
    rem.add_argument("--base", action="append", default=[])
    rem.add_argument("--discard-untracked", action="store_true")
    rem.add_argument("--park", action="store_true")
    pru = sub.add_parser("prune-branches")
    pru.add_argument("repo")
    pru.add_argument("branches", nargs="*")
    pru.add_argument("--list", action="store_true")
    pru.add_argument("--base", action="append", default=[])
    args = parser.parse_args()
    return {"report": report, "remove": remove, "prune-branches": prune_branches}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
