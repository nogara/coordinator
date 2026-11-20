"""Tests for fleet_watch.py, due_steps.py, watch_prs.py, and watcher_check.py."""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from tests.helpers import COORD_SCRIPTS, Sandbox, add_script_paths

add_script_paths()
import due_steps  # noqa: E402
import fleet_watch  # noqa: E402
import watch_prs  # noqa: E402


class TestDueSteps(unittest.TestCase):
    def test_parses_due_dates_skips_checked_and_fences(self):
        text = (
            "- Active task due: 2026-11-18 check metrics\n"
            "- [x] Done task due: 2026-11-15 ignored\n"
            "- ~~Struck due: 2026-11-16~~\n"
            "```\n- Fenced due: 2026-11-14\n```\n"
            "- Parent task\n  - Child step due: 2026-11-19 follow up\n"
        )
        steps = due_steps.dated_steps(text)
        dates = [d for d, _, _ in steps]
        self.assertEqual(dates, ["2026-11-18", "2026-11-19"])
        self.assertIn("Parent task ›", steps[1][2])


class TestWatcherEvents(unittest.TestCase):
    def test_agent_events_ignores_idle_compaction(self):
        old = {
            "d1": {"present": True, "status": "done", "turns": 5, "asks": 0, "report": None,
                   "report_mtime": None, "stuck": False, "responded": "2026-11-19T01:00:00Z", "compacted": None}
        }
        new = {
            "d1": {"present": True, "status": "done", "turns": 6, "asks": 0, "report": None,
                   "report_mtime": None, "stuck": False, "responded": "2026-11-19T01:00:00Z",
                   "compacted": "2026-11-19T02:00:00Z"}
        }
        self.assertEqual(list(fleet_watch.agent_events(old, new)), [])

    def test_agent_events_fires_settled_ask_report_and_gone(self):
        old = {
            "d1": {"present": True, "status": "working", "turns": 1, "asks": 0, "report": "/a/REPORT.md",
                   "report_mtime": 100.0, "stuck": False, "responded": "t1", "compacted": None},
            "d2": {"present": True, "status": "done", "turns": 1, "asks": 0, "report": None,
                   "report_mtime": None, "stuck": False, "responded": "t1", "compacted": None},
        }
        new = {
            "d1": {"present": True, "status": "done", "turns": 2, "asks": 1, "inbox": "1 · Q?",
                   "report": "/a/REPORT.md", "report_mtime": 200.0, "stuck": False, "responded": "t2", "compacted": None},
            "d2": {"present": False, "ended": False, "session": "s2"},
        }
        evs = list(fleet_watch.agent_events(old, new))
        self.assertIn("settled d1 done", evs)
        self.assertIn("ask d1 1 · Q?", evs)
        self.assertIn("report d1 /a/REPORT.md", evs)
        self.assertIn("gone d2", evs)

    def test_pr_events_detects_merge_checks_and_comments(self):
        old = {"org/repo#10": {"state": "OPEN", "checks": "pending", "review": "", "comments": 1}}
        new = {"org/repo#10": {"state": "MERGED", "checks": "success", "review": "APPROVED", "comments": 3}}
        evs = list(watch_prs.pr_events(old, new))
        self.assertIn("pr repo#10 merged", evs)
        self.assertIn("pr repo#10 checks success", evs)
        self.assertIn("pr repo#10 review approved", evs)
        self.assertIn("pr repo#10 comments 2", evs)

    def test_mac_clock_detects_sleep(self):
        an_hour_ago = time.time() - 3600
        clk = fleet_watch.MacClock([an_hour_ago, time.monotonic() - 10, 0.0])
        self.assertGreater(clk.mac_woke_at, an_hour_ago + 3000)
        self.assertLess(clk.awake_seconds_since(an_hour_ago), 60)


class TestWatcherCheckCLI(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.sb.write_backlog("demo", "# demo\n\n## Boundary\nOwns.\n\n## Standing yes\n\n## Open\n\n## Done\n")

    def tearDown(self):
        self.sb.cleanup()

    def test_reports_unwatched_coordinator_with_delegate(self):
        terms = [
            {"address": "t1", "name": None, "cwd": "/tmp", "labels": {"fleet.role": "coordinator", "fleet.domain": "demo"},
             "status": {"state": "done", "app": "claude-code"}, "foreground": {"pid": 1, "argv": ["claude"]}, "turn": 1},
            {"address": "t2", "name": "d1", "cwd": "/tmp", "labels": {"fleet.domain": "demo", "fleet.task": "work"},
             "status": {"state": "done", "app": "claude-code"}, "foreground": {"pid": 2, "argv": ["claude"]}, "turn": 1},
        ]
        tf = self.sb.root / "terms.json"
        tf.write_text(json.dumps(terms))
        res = self.sb.run_script(COORD_SCRIPTS / "watcher_check.py", "--terminals", str(tf))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("demo", res.stdout)
        self.assertIn("unwatched", res.stdout)


if __name__ == "__main__":
    unittest.main()
