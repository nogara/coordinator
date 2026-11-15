"""
How the fleet watcher polls GitHub: one GraphQL query per repo for the
pull requests it watches, the events between two snapshots, and the resume
command for a pull request whose delegate was ended.
"""
import json
import os
import re
import subprocess

import fleet


def run_json(*args, cwd=None):
    """The command's parsed stdout and why it failed. Either is None when there is none."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=fleet.COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"{args[0]} timed out after {fleet.COMMAND_TIMEOUT_SECONDS} seconds"
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


PR_FIELDS = """state reviewDecision headRefName
  reviews(last: 100) { totalCount nodes { author { login } } }
  comments(last: 100) { totalCount nodes { author { login } } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }"""
ROLLUP_RESULT = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
                 "PENDING": "pending", "EXPECTED": "pending"}
OWNER_AND_NAME = re.compile(r"[\w.-]+/[\w.-]+")
repo_names = {}


def repo_name(repo):
    """The repo's owner/name and why GitHub did not give it. Either is None when there is none.

    repo is a checkout's path, or an owner/name that is not a folder, as --domain finds them.
    """
    if repo in repo_names:
        return repo_names[repo], None
    if OWNER_AND_NAME.fullmatch(repo) and not os.path.isdir(os.path.expanduser(repo)):
        return repo, None
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
    return list(dict.fromkeys(lines))
