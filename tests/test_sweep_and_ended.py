"""Tests for fleet-sweep scripts (ended.py, prs.py, parking.py, sweep.py)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.helpers import SWEEP_SCRIPTS, Sandbox, add_script_paths

add_script_paths()
import parking  # noqa: E402
import prs as sweep_prs  # noqa: E402


class TestParking(unittest.TestCase):
    def test_build_output_patterns(self):
        self.assertTrue(parking.is_build_output("node_modules/pkg/index.js"))
        self.assertTrue(parking.is_build_output("out/bundle.js"))
        self.assertTrue(parking.is_build_output("debug.log"))
        self.assertFalse(parking.is_build_output(".scratch/BRIEF.md"))
        park, drop = [], []
        parking.split_build_output("/nonexistent", ".scratch/test.log", park, drop)
        self.assertEqual(park, [".scratch/test.log"])
        self.assertEqual(drop, [])


class TestPrsAndEnded(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_pr_summary(self):
        self.assertEqual(sweep_prs.pr_summary([]), (None, ""))
        self.assertEqual(
            sweep_prs.pr_summary([{"number": 5, "state": "MERGED"}]),
            ("MERGED", "#5 merged"),
        )
        self.assertEqual(
            sweep_prs.pr_summary([{"number": 5, "state": "CLOSED"}, {"number": 6, "state": "OPEN"}]),
            ("OPEN", "#5 closed,#6 open"),
        )

    def test_prune_ended_removes_merged_pr_record(self):
        ended_dir = self.sb.state_dir / "ended"
        ended_dir.mkdir(parents=True)
        rec_path = ended_dir / "sess-1.json"
        rec_path.write_text(json.dumps({
            "name": "task-a",
            "domain": "demo",
            "session": "sess-1",
            "cwd": str(self.sb.root / "gone-wt"),
            "repo": "org/repo",
            "branch": "feat-a",
            "ended_at": "2026-11-15T00:00:00+00:00",
        }))
        self.sb.write_stub("gh", 'echo \'[{"headRefName":"feat-a","headRefOid":"abc","state":"MERGED","number":1,"mergedAt":"2026-11-15T01:00:00Z"}]\'\n')
        res = self.sb.run_script(SWEEP_SCRIPTS / "sweep.py", "prune-ended", "--list")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("task-a of demo", res.stdout)

        res2 = self.sb.run_script(SWEEP_SCRIPTS / "sweep.py", "prune-ended")
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertFalse(rec_path.exists())

    def test_prune_ended_handles_zero_pr_repo_vs_gh_failure(self):
        ended_dir = self.sb.state_dir / "ended"
        ended_dir.mkdir(parents=True)
        rec_path = ended_dir / "sess-zero.json"
        rec_path.write_text(json.dumps({
            "name": "task-z",
            "domain": "demo",
            "session": "sess-zero",
            "cwd": str(self.sb.root / "gone-wt"),
            "repo": "org/no-prs-yet",
            "branch": "feat-z",
            "ended_at": "2026-11-15T00:00:00+00:00",
        }))
        # gh failure -> keep record (PR state unknown)
        self.sb.write_stub("gh", "exit 1\n")
        fail_res = self.sb.run_script(SWEEP_SCRIPTS / "sweep.py", "prune-ended")
        self.assertEqual(fail_res.returncode, 0, fail_res.stderr)
        self.assertTrue(rec_path.exists())

        # gh succeeds with [] (0 PRs in repo) -> prune record
        self.sb.write_stub("gh", "echo '[]'\n")
        ok_res = self.sb.run_script(SWEEP_SCRIPTS / "sweep.py", "prune-ended")
        self.assertEqual(ok_res.returncode, 0, ok_res.stderr)
        self.assertFalse(rec_path.exists())


if __name__ == "__main__":
    unittest.main()
