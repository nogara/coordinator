"""
The pull requests fleet_status.py lists for a repo: those opened from the
signed-in GitHub account that are open or closed within RECENTLY_CLOSED_DAYS.
"""

from __future__ import annotations

import datetime as dt
import json

from fleet import CheckError, run

RECENTLY_CLOSED_DAYS = 14
SEARCH = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number title url state createdAt mergedAt closedAt headRefName headRefOid
      %s
    } }
  }
}
"""
OPEN_FIELDS = """
      isDraft baseRefName mergeable reviewDecision
      commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
      reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes { createdAt } } } }
      reviewRequests { totalCount }
      latestOpinionatedReviews(first: 30) { nodes { state submittedAt author { login __typename } } }
      timelineItems(itemTypes: READY_FOR_REVIEW_EVENT, last: 1) { nodes { ... on ReadyForReviewEvent { createdAt } } }
"""


def fetch_pull_requests(slug: str, now: dt.datetime) -> list[dict]:
    since = (now - dt.timedelta(days=RECENTLY_CLOSED_DAYS)).strftime("%Y-%m-%d")
    found: dict[int, dict] = {}
    searches = (
        (f"repo:{slug} is:pr is:open author:@me", SEARCH % OPEN_FIELDS),
        (f"repo:{slug} is:pr is:closed author:@me closed:>={since}", SEARCH % ""),
    )
    for search, query in searches:
        fetched, cursor = [], None
        while True:
            command = ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"q={search}"]
            if cursor:
                command += ["-f", f"cursor={cursor}"]
            page = json.loads(run(command))["data"]["search"]
            fetched += [node for node in page["nodes"] if node]
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        if len(fetched) != page["issueCount"]:
            raise CheckError(f"{slug}: fetched {len(fetched)} pull requests but GitHub counts {page['issueCount']}")
        found.update({pr["number"]: pr for pr in fetched})
    return list(found.values())
