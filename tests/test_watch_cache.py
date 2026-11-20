"""Tests for watch_cache.py (AgentCache, shared PR cache, StateSaver, pruning, backoff)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from tests.helpers import Sandbox, add_script_paths

add_script_paths()
import fleet  # noqa: E402
import watch_cache  # noqa: E402


class TestWatchCache(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_agent_cache_skips_unchanged_idle_and_shell_but_refreshes_working_and_changes(self):
        listed = [
            {"address": "t1", "agent": "claude-code", "status": "done", "turn": 5, "state": "live", "cwd": "/a", "where": "A"},
            {"address": "t2", "agent": "claude-code", "status": "working", "turn": 2, "state": "live", "cwd": "/b", "where": "B"},
            {"address": "t3", "agent": None, "status": None, "turn": 0, "state": "live", "cwd": "/c", "where": "C"},
        ]
        gets: list[str] = []

        def fake_get(addr):
            gets.append(addr)
            if addr == "t3":
                return {"address": "t3", "cwd": "/c", "labels": {}, "status": None, "foreground": {"argv": ["zsh"]}}
            return {
                "address": addr,
                "name": addr,
                "cwd": f"/{addr}",
                "labels": {"fleet.domain": "demo"},
                "status": {"state": "working" if addr == "t2" else "done", "app": "claude-code"},
                "foreground": {"pid": 1, "argv": ["claude"]},
                "turn": 2 if addr == "t2" else 5,
            }

        cache = watch_cache.AgentCache(ttl=30.0)
        with mock.patch.object(fleet, "listed_terminals", side_effect=lambda: listed), \
                mock.patch.object(fleet, "terminal_or_none", side_effect=fake_get):
            first = cache.load(now=100.0)
            self.assertEqual([a["address"] for a in first], ["t1", "t2"])
            self.assertEqual(gets, ["t1", "t2", "t3"])

            # Next poll 5s later: t1 (done) and t3 (shell) are cached; only t2 (working) is fetched
            gets.clear()
            second = cache.load(now=105.0)
            self.assertEqual([a["address"] for a in second], ["t1", "t2"])
            self.assertEqual(gets, ["t2"])

            # Turn advances on t1 -> t1 refreshed immediately
            listed[0] = dict(listed[0], turn=6)
            gets.clear()
            cache.load(now=110.0)
            self.assertEqual(gets, ["t1", "t2"])

            # Past TTL -> t3 refreshed too
            gets.clear()
            cache.load(now=135.0)
            self.assertEqual(gets, ["t2", "t3"])

    def test_shared_pr_cache_reuses_within_ttl_and_refreshes_after(self):
        calls = 0

        def fake_open(slug):
            nonlocal calls
            calls += 1
            return [10, 20] if calls == 1 else [10, 21]

        with mock.patch.object(fleet, "STATE_DIR", self.sb.state_dir), \
                mock.patch.object(fleet, "open_pull_request_numbers", side_effect=fake_open):
            self.assertEqual(watch_cache.cached_open_pr_numbers("org/repo", ttl=60.0, now=1000.0), [10, 20])
            self.assertEqual(watch_cache.cached_open_pr_numbers("org/repo", ttl=60.0, now=1030.0), [10, 20])
            self.assertEqual(calls, 1)
            self.assertEqual(watch_cache.cached_open_pr_numbers("org/repo", ttl=60.0, now=1061.0), [10, 21])
            self.assertEqual(calls, 2)

    def test_state_saver_skips_clock_only_writes_until_heartbeat_or_change(self):
        state_path = str(self.sb.state_dir / "watch.json")
        saver = watch_cache.StateSaver(state_path, heartbeat_seconds=60.0)
        base = {"source": "hc", "agents": {}, "prs": {}, "pending": [], "clock": [100.0, 50.0, 0]}

        self.assertTrue(saver.save(base, now=100.0))
        self.assertFalse(saver.save({**base, "clock": [105.0, 55.0, 0]}, now=105.0))
        self.assertTrue(saver.save({**base, "pending": ["gone a"], "clock": [110.0, 60.0, 0]}, now=110.0))
        self.assertFalse(saver.save({**base, "pending": ["gone a"], "clock": [120.0, 70.0, 0]}, now=120.0))
        # Heartbeat elapsed -> saves even when only clock moved
        self.assertTrue(saver.save({**base, "pending": ["gone a"], "clock": [175.0, 125.0, 0]}, now=175.0))
        # Force always writes
        self.assertTrue(saver.save({**base, "pending": ["gone a"], "clock": [176.0, 126.0, 0]}, force=True, now=176.0))
        self.assertTrue(os.path.exists(state_path))

    def test_prune_state_drops_stale_unwatched_agents_and_closed_prs(self):
        agents_seen = {
            "active": {"present": True},
            "still-live-unwatched": {"present": True},
            "old-gone": {"present": False},
            "old-stale-true": {"present": True},
        }
        prs_seen = {
            "org/repo#1": {"slug": "org/repo", "state": "OPEN"},
            "/tmp/repo#1": {"slug": "org/repo", "state": "OPEN"},  # alias of watched #1
            "org/repo#2": {"slug": "org/repo", "state": "MERGED"},
            "org/repo#3": {"slug": "org/repo", "state": "OPEN"},  # no longer in open_numbers
            "org/repo#4": {"slug": "org/repo", "state": "OPEN"},  # still open in open_numbers
            "org/repo#5": {"slug": "org/repo", "state": "CLOSED"},
        }
        live = [{"address": "t1", "name": "active"}, {"address": "t2", "name": "still-live-unwatched"}]
        watch_cache.prune_state(
            agents_seen,
            prs_seen,
            watched_agents={"active"},
            watched_prs={"org/repo#1"},
            live_agents=live,
            open_numbers={"org/repo": [1, 4]},
        )
        self.assertEqual(set(agents_seen), {"active", "still-live-unwatched"})
        self.assertEqual(set(prs_seen), {"org/repo#1", "/tmp/repo#1", "org/repo#4"})

    def test_network_error_classification_and_backoff(self):
        self.assertTrue(watch_cache.is_transient_network_error("error connecting to api.github.com"))
        self.assertTrue(watch_cache.is_transient_network_error("dial tcp: lookup api.github.com: no such host"))
        self.assertTrue(watch_cache.is_transient_network_error("HTTP 503 Service Unavailable"))
        self.assertFalse(watch_cache.is_transient_network_error("gh auth login required"))
        self.assertEqual(watch_cache.github_blind_limit("TLS handshake timeout"), watch_cache.BLIND_NET_SECONDS)
        self.assertEqual(watch_cache.github_blind_limit("HTTP 401 Unauthorized"), 900)
        self.assertEqual(watch_cache.pr_poll_delay(0, base=90, cap=300), 90)
        self.assertEqual(watch_cache.pr_poll_delay(1, base=90, cap=300), 90)
        self.assertEqual(watch_cache.pr_poll_delay(2, base=90, cap=300), 180)
        self.assertEqual(watch_cache.pr_poll_delay(3, base=90, cap=300), 300)
        self.assertEqual(watch_cache.pr_poll_delay(8, base=90, cap=300), 300)


if __name__ == "__main__":
    unittest.main()
