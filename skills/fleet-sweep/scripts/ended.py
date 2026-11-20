"""
Records of delegates ended with the coordinator's delegate_end.py, in its
<state_dir>/ended, and which of them are finished: the worktree is gone and
its branch has no open PR, or the PR of the branch it ended on merged or
closed. A record whose PR state is unknown stays.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from prs import branch_prs, pr_summary, pull_requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "coordinator", "scripts"))
import fleet  # noqa: E402


def ended_records():
    """(path, record) for each record that delegate_end.py wrote."""
    found = []
    for path in sorted(fleet.ENDED_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and isinstance(record.get("session"), str) and isinstance(record.get("cwd"), str):
            found.append((path, record))
    return found


def finished_ended_records():
    """(path, record, why) for each finished record."""
    records = ended_records()
    slugs = sorted({r["repo"] for _, r in records if r.get("repo") and r.get("branch")})
    with ThreadPoolExecutor(max_workers=4) as pool:
        prs_by_slug = dict(zip(slugs, pool.map(lambda slug: pull_requests(None, slug), slugs)))
    finished = []
    for path, record in records:
        prs = prs_by_slug.get(record.get("repo"))
        if not (record["cwd"] and os.path.isdir(record["cwd"])):
            branch_has_an_open_pr = any(p["state"] == "OPEN" for p in (prs or {}).get(record.get("branch"), []))
            pr_state_unknown = bool(record.get("repo") and record.get("branch")) and prs is None
            if not branch_has_an_open_pr and not pr_state_unknown:
                finished.append((path, record, f"worktree gone: {record['cwd']}"))
            continue
        if not prs or not record.get("branch"):
            continue
        pr_state, pr_label = pr_summary(branch_prs(record["cwd"], record["branch"], prs))
        if pr_state in ("MERGED", "CLOSED"):
            finished.append((path, record, f"{record['branch']} {pr_label}"))
    return finished


def ended_record_rows():
    finished = finished_ended_records()
    if finished:
        yield "SAFE", (f"{len(finished)} ended-delegate records whose PR merged or closed, or whose worktree is gone "
                       f"with no open PR, in {fleet.ENDED_DIR} · list them: sweep.py prune-ended --list")


def prune_ended(args):
    for path, record, why in finished_ended_records():
        program = record.get("program") or "claude"
        line = f"{record.get('name', '?')} of {record.get('domain', '?')}, {program} session {record['session']} · {why}"
        if args.list:
            print(line)
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        print(f"removed ended record {line}")
    return 0
