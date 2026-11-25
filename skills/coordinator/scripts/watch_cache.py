"""Caching, dirty-only state persistence, state pruning, and network backoff for fleet_watch.py."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import fleet

AGENT_CACHE_TTL_SECONDS = 30.0
PR_CACHE_TTL_SECONDS = 60.0
STATE_HEARTBEAT_SECONDS = 60.0
BLIND_NET_SECONDS = 3600
PR_BACKOFF_CAP_SECONDS = 300
UNWATCHED_PR_KEPT_SECONDS = 24 * 3600
WATCHER_GAP_THAT_FORGETS_FAILURES_SECONDS = 600

TRANSIENT_NETWORK = re.compile(
    r"error connecting|no such host|dial tcp|tls handshake|i/o timeout|connection reset|"
    r"connection refused|timed out|temporary failure|network is unreachable|\beof\b|"
    r"http 50[234]|502 bad gateway|503 service|504 gateway|rate limit",
    re.IGNORECASE,
)


def load_json(path: str | Path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_json_atomic(path: str | Path, data) -> None:
    target = str(path)
    tmp = f"{target}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, target)


class AgentCache:
    """Avoids N+1 `hc get` calls for unchanged idle terminals and plain shells."""

    def __init__(self, ttl: float = AGENT_CACHE_TTL_SECONDS):
        self.ttl = ttl
        self._entries: dict[str, tuple[tuple, float, dict | None]] = {}

    @staticmethod
    def _fingerprint(listed: dict) -> tuple:
        return (
            listed.get("agent"),
            listed.get("status"),
            listed.get("turn"),
            listed.get("state"),
            listed.get("cwd"),
            listed.get("where"),
            listed.get("name"),
        )

    def load(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        listed = fleet.listed_terminals()
        seen_addresses: set[str] = set()
        agents: list[dict] = []
        for item in listed:
            address = item["address"]
            seen_addresses.add(address)
            key = self._fingerprint(item)
            cached = self._entries.get(address)
            is_working = item.get("status") == "working"
            if cached is not None and not is_working and cached[0] == key and (now - cached[1]) < self.ttl:
                if cached[2] is not None:
                    agents.append(cached[2])
                continue
            terminal = fleet.terminal_or_none(address)
            if terminal is None:
                self._entries.pop(address, None)
                continue
            if fleet.runs_an_agent(item) or fleet.agent_started_from_its_shell(terminal):
                agent = fleet.as_agent(terminal)
                self._entries[address] = (key, now, agent)
                agents.append(agent)
            else:
                self._entries[address] = (key, now, None)
        for stale in list(self._entries):
            if stale not in seen_addresses:
                del self._entries[stale]
        return agents


def cached_open_pr_numbers(slug: str, ttl: float = PR_CACHE_TTL_SECONDS, now: float | None = None) -> list[int]:
    """Shared short-TTL cache of open PR numbers per repo slug across domain watchers."""
    now = time.time() if now is None else now
    cache_dir = fleet.STATE_DIR / "pr-cache"
    cache_path = cache_dir / f"{slug.replace('/', '__')}.json"
    saved = load_json(cache_path)
    if (
        isinstance(saved, dict)
        and isinstance(saved.get("fetched_at"), (int, float))
        and isinstance(saved.get("numbers"), list)
        and 0 <= (now - saved["fetched_at"]) < ttl
    ):
        return saved["numbers"]
    numbers = fleet.open_pull_request_numbers(slug)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        save_json_atomic(cache_path, {"fetched_at": now, "numbers": numbers})
    except OSError:
        pass
    return numbers


class StateSaver:
    """Writes watcher state only when non-clock fields change or the heartbeat interval passes."""

    def __init__(self, path: str, heartbeat_seconds: float = STATE_HEARTBEAT_SECONDS):
        self.path = path
        self.heartbeat_seconds = heartbeat_seconds
        self._last_fingerprint: str | None = None
        self._last_saved_at: float = 0.0

    def save(self, state: dict, force: bool = False, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        without_clock = {key: value for key, value in state.items() if key != "clock"}
        fingerprint = json.dumps(without_clock, sort_keys=True)
        if not force and fingerprint == self._last_fingerprint and (now - self._last_saved_at) < self.heartbeat_seconds:
            return False
        save_json_atomic(self.path, state)
        self._last_fingerprint = fingerprint
        self._last_saved_at = now
        return True


def prune_state(
    agents_seen: dict,
    prs_seen: dict,
    watched_agents: set[str],
    watched_prs: set[str],
    live_agents: list[dict] | None,
    open_numbers: dict[str, list[int]] | None = None,
    now: float | None = None,
) -> None:
    """Drop stale unwatched history from agents_seen and prs_seen so state files stay small.

    An unwatched pull request that ended or left the open list stays for
    UNWATCHED_PR_KEPT_SECONDS, so an event still pending for it can name the
    delegate to resume, and watching it again finds what was last seen.
    """
    if live_agents is not None:
        live_keys = {key for a in live_agents for key in (a.get("name"), a["address"]) if key}
        for key in list(agents_seen):
            if key not in watched_agents and key not in live_keys:
                del agents_seen[key]

    watched_pr_pairs: set[tuple[str | None, str]] = set()
    for spec in watched_prs:
        repo, number = spec.rsplit("#", 1)
        slug = prs_seen.get(spec, {}).get("slug") or (repo if "/" in repo and not repo.startswith("/") else None)
        watched_pr_pairs.add((slug, number))
    open_map = open_numbers or {}
    now = time.time() if now is None else now
    for key, entry in list(prs_seen.items()):
        number = key.rsplit("#", 1)[-1]
        slug = entry.get("slug")
        if key in watched_prs or (slug and (slug, number) in watched_pr_pairs):
            entry.pop("unwatched_at", None)
            continue
        ended = entry.get("state") in ("MERGED", "CLOSED", "MISSING")
        left_open_list = slug in open_map and number.isdigit() and int(number) not in open_map[slug]
        if not (ended or left_open_list):
            entry.pop("unwatched_at", None)
            continue
        if now - entry.setdefault("unwatched_at", now) >= UNWATCHED_PR_KEPT_SECONDS:
            del prs_seen[key]


def is_transient_network_error(reason: str) -> bool:
    return bool(TRANSIENT_NETWORK.search(reason or ""))


def github_blind_limit(reason: str, default_seconds: int = 900) -> int:
    return BLIND_NET_SECONDS if is_transient_network_error(reason) else default_seconds


def github_blind_line(repo: str, reason: str | None, failing_since: dict, clock, default_seconds: int) -> str | None:
    """The line that ends the watch once a repo's queries have failed too long, counted across watchers."""
    if reason is None:
        failing_since.pop(repo, None)
        return None
    limit = github_blind_limit(reason, default_seconds)
    if clock.awake_seconds_since(failing_since.setdefault(repo, time.time())) < limit:
        return None
    del failing_since[repo]
    return f"blind {os.path.basename(repo)} · GitHub queries failed for {limit // 60} minutes · {reason}"


def failures_carried_over(saved: dict, started_at: float) -> dict:
    """When each repo's GitHub queries began to fail, as the last watcher saved it.

    Empty when that watcher stopped saving long before this one started:
    the queries may have worked in between.
    """
    try:
        last_saved_at = float(saved["clock"][0])
        failing = {str(repo): float(since) for repo, since in saved["github_failing_since"].items()}
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return {}
    return failing if started_at - last_saved_at <= WATCHER_GAP_THAT_FORGETS_FAILURES_SECONDS else {}


def pr_poll_delay(consecutive_failures: int, base: int = 90, cap: int = PR_BACKOFF_CAP_SECONDS) -> int:
    if consecutive_failures <= 0:
        return base
    return min(cap, base * (2 ** min(consecutive_failures - 1, 3)))
