"""Tests for fleet_term.py, the CLI the fleet's agents use instead of `hc`, under a fake `hc` and a fake `rex`."""
from __future__ import annotations

import json
import unittest

from tests.helpers import COORD_SCRIPTS, Sandbox
from tests.test_rex_terminals import SESSION, block, claude_process, root_record  # noqa: E402

CLI = COORD_SCRIPTS / "fleet_term.py"
HC_GET = {"address": "t7", "name": "w", "labels": {"a": "b"}}


class FleetTermHc(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox({"runtime": "hc"})
        self.addCleanup(self.sb.cleanup)
        self.log = self.sb.root / "hc.log"
        self.sb.write_stub("hc", f'''echo "$@" >> {self.log}
case "$1" in
  ls) echo '{{"terminals": [{{"address": "t7"}}]}}';;
  get) if [ "$2" = gone ]; then echo nope >&2; exit 3; fi; echo '{json.dumps(HC_GET)}';;
  read) echo screen;;
esac
''')

    def run_cli(self, *args, env=None):
        return self.sb.run_script(CLI, *args, env_extra=env)

    def calls(self):
        return self.log.read_text().splitlines()

    def test_ls(self):
        done = self.run_cli("ls")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"terminals": [{"address": "t7"}]})

    def test_get_and_gone(self):
        self.assertEqual(json.loads(self.run_cli("get", "t7").stdout), HC_GET)
        done = self.run_cli("get", "gone")
        self.assertEqual(done.returncode, 3)

    def test_send_read_label(self):
        self.assertEqual(self.run_cli("send", "t7", "hello there").returncode, 0)
        self.assertEqual(self.run_cli("read", "t7", "--lines", "5").stdout.strip(), "screen")
        self.assertEqual(self.run_cli("label", "t7", "k=v", "old=").returncode, 0)
        self.assertEqual(self.calls(), ["send t7 hello there", "read --lines 5 t7", "label t7 k=v old="])

    def test_keys_and_dash_text(self):
        self.assertEqual(self.run_cli("keys", "t7", "escape").returncode, 0)
        self.assertEqual(self.run_cli("send", "t7", "-n", "x").returncode, 0)
        self.assertEqual(self.calls(), ["keys t7 escape", "send t7 -n x"])

    def test_self(self):
        done = self.run_cli("self", env={"HC_TERMINAL_ID": "t9"})
        self.assertEqual(json.loads(done.stdout), {"address": "t9"})
        done = self.sb.run_script(CLI, "self", env_extra={"HC_TERMINAL_ID": ""})
        self.assertEqual(done.returncode, 1)
        self.assertIn("not inside", done.stderr)


class FleetTermRex(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox({"runtime": "rex"})
        self.addCleanup(self.sb.cleanup)
        blocks = [{**block("block:a", "w", "window:1"), "session_id": SESSION}]
        self.sb.write_rex_stub(blocks, {"block:a": {"records": [root_record()]}}, {"block:a": claude_process()},
                               {"block:gone"}, screens={"block:a": "line1\nline2\nline3"})
        self.env = {"REX_BLOCK": "block:me"}

    def run_cli(self, *args, env=None):
        return self.sb.run_script(CLI, *args, env_extra=env if env is not None else self.env)

    def test_ls(self):
        done = self.run_cli("ls")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([t["address"] for t in json.loads(done.stdout)["terminals"]], ["block:a"])

    def test_get_and_gone(self):
        self.assertEqual(json.loads(self.run_cli("get", "block:a").stdout)["address"], "block:a")
        self.assertEqual(self.run_cli("get", "block:gone").returncode, 3)

    def test_send_read_label(self):
        self.assertEqual(self.run_cli("send", "block:a", "hello").returncode, 0)
        paste = json.loads((self.sb.root / "rex.stdin").read_text().splitlines()[0])
        self.assertEqual(paste["event"]["payload"]["text"], '<rex from="block:me">\nhello')
        self.assertIn(["send-key", "-b", "block:a", "enter"], self.sb.rex_calls())
        self.assertEqual(self.run_cli("read", "block:a", "--lines", "2").stdout.strip(), "line2\nline3")
        self.assertEqual(self.run_cli("label", "block:a", "k=v", "x=y").returncode, 0)
        self.assertEqual(self.run_cli("label", "block:a", "x=").returncode, 0)
        labels = json.loads(self.run_cli("get", "block:a").stdout)["labels"]
        self.assertEqual(labels["k"], "v")
        self.assertNotIn("x", labels)

    def test_name_resolves_to_block_id(self):
        self.assertEqual(json.loads(self.run_cli("get", "w").stdout)["address"], "block:a")
        self.assertEqual(self.run_cli("label", "w", "k=v").returncode, 0)
        self.assertEqual(json.loads(self.run_cli("get", "block:a").stdout)["labels"]["k"], "v")
        self.assertEqual(self.run_cli("read", "w", "--lines", "1").stdout.strip(), "line3")

    def test_ambiguous_name_exits_2(self):
        blocks = [{**block("block:a", "w", "window:1"), "session_id": SESSION},
                  {**block("block:b", "w", "window:1"), "session_id": SESSION}]
        self.sb.write_rex_stub(blocks, {}, {}, set())
        done = self.run_cli("get", "w")
        self.assertEqual(done.returncode, 2)
        self.assertIn("block:a, block:b", done.stderr)

    def test_keys_and_dash_text(self):
        self.assertEqual(self.run_cli("keys", "w", "escape").returncode, 0)
        self.assertIn(["send-key", "-b", "block:a", "escape"], self.sb.rex_calls())
        self.assertEqual(self.run_cli("send", "block:a", "-n").returncode, 0)
        paste = json.loads((self.sb.root / "rex.stdin").read_text().splitlines()[0])
        self.assertTrue(paste["event"]["payload"]["text"].endswith("\n-n"))

    def test_self(self):
        self.assertEqual(json.loads(self.run_cli("self").stdout), {"address": "block:me"})
        done = self.run_cli("self", env={"REX_BLOCK": ""})
        self.assertEqual(done.returncode, 1)
        self.assertIn("not inside", done.stderr)


if __name__ == "__main__":
    unittest.main()
