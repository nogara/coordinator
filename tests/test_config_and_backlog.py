"""Tests for config.py and backlog_check.py."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from tests.helpers import COORD_SCRIPTS, Sandbox, add_script_paths

add_script_paths()
import backlog  # noqa: E402
import config  # noqa: E402


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_missing_file_uses_defaults(self):
        loaded = config.load_config(self.sb.root / "missing.json")
        self.assertEqual(loaded["backlog_limit_kb"], 40)
        self.assertEqual(loaded["done_limit_kb"], 20)

    def test_unknown_key_raises(self):
        p = self.sb.root / "bad.json"
        p.write_text(json.dumps({"not_a_key": 1}))
        with self.assertRaises(ValueError):
            config.load_config(p)

    def test_wrong_type_raises(self):
        p = self.sb.root / "badtype.json"
        p.write_text(json.dumps({"backlog_limit_kb": 40.5}))
        with self.assertRaises(ValueError):
            config.load_config(p)


class TestBacklogCheck(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox({"user_names": ["Sam"], "open_ask_patterns": [r"\bin the inbox\b"]})

    def tearDown(self):
        self.sb.cleanup()

    def test_under_limits_exits_zero(self):
        self.sb.write_backlog(
            "demo",
            "# demo backlog\n\n## Boundary\nOwns demo.\n\n## Standing yes\n- Keep tests.\n\n## Open\n- Item one.\n\n## Done\n- 2026-11-15 Shipped.\n",
        )
        res = self.sb.run_script(COORD_SCRIPTS / "backlog_check.py", "demo")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("## Open", res.stdout)
        self.assertNotIn("OVER", res.stdout)

    def test_over_done_limit_and_cut_done(self):
        done_lines = "\n".join(f"- 2026-11-{(i % 18) + 1:02d} Item {i} " + ("x" * 200) for i in range(130))
        self.sb.write_backlog(
            "demo",
            f"# demo backlog\n\n## Boundary\nOwns demo.\n\n## Standing yes\n\n## Open\n- Open one.\n\n## Done\n{done_lines}\n",
        )
        res = self.sb.run_script(COORD_SCRIPTS / "backlog_check.py", "demo")
        self.assertEqual(res.returncode, 1)
        self.assertIn("OVER  Done", res.stdout)

        cut = self.sb.run_script(COORD_SCRIPTS / "backlog_check.py", "demo", "--cut-done")
        self.assertEqual(cut.returncode, 0, cut.stdout + cut.stderr)
        self.assertIn("deleted the", cut.stdout)
        self.assertTrue(list((self.sb.backlog_dir / "trims").glob("demo-done-cut-*.md")))

    def test_ledger_clears_dropped_quote_and_ask(self):
        before = self.sb.root / "before.md"
        before.write_text(
            "# demo\n\n## Boundary\nOwns.\n\n## Standing yes\n- Always test.\n\n## Open\n"
            '- Item A (11-14: "Drop this"). Ask: keep old flag? Recommend drop.\n\n## Done\n'
        )
        self.sb.write_backlog(
            "demo",
            "# demo\n\n## Boundary\nOwns.\n\n## Standing yes\n- Always test.\n\n## Open\n- Item B.\n\n## Done\n- 11-15 Item A.\n",
        )
        terms = self.sb.root / "terminals.json"
        terms.write_text("[]")
        res = self.sb.run_script(
            COORD_SCRIPTS / "backlog_check.py", "demo", "--before", str(before), "--terminals", str(terms)
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("LOST  the user's words", res.stdout)
        self.assertIn("LOST  sentence that held an open ask", res.stdout)

        ledger = self.sb.root / "ledger.md"
        ledger.write_text(
            '- Dropped quote "Drop this" because Item A finished.\n'
            "- Dropped ask: Ask: keep old flag? (answered)\n"
        )
        res2 = self.sb.run_script(
            COORD_SCRIPTS / "backlog_check.py", "demo", "--before", str(before), "--ledger", str(ledger), "--terminals", str(terms)
        )
        self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)

    def test_sections_parser(self):
        text = "# Title\n\n## Boundary\nA\n\n## Open\nB\n\n## Done\nC\n"
        secs = backlog.sections(text)
        self.assertIn("## Boundary", secs)
        self.assertIn("## Open", secs)
        self.assertIn("## Done", secs)


if __name__ == "__main__":
    unittest.main()
