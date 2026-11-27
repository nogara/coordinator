"""Tests for RexTerminals, the Rex backend of terminals.py, against a fake `rex` on PATH."""
from __future__ import annotations

import functools
import json
import os
import unittest
from unittest import mock

from tests.helpers import Sandbox, add_script_paths

add_script_paths()
import config  # noqa: E402
import coordinator_restart  # noqa: E402
import fleet  # noqa: E402
import labels_store  # noqa: E402
import terminals  # noqa: E402
from check_error import CheckError  # noqa: E402

SESSION = "session:s1"
OTHER_SESSION = "session:s2"


def block(block_id: str, label: str, window: str, session: str = SESSION,
          flavor: str = "com.superlogical.terminal.shell") -> dict:
    return dict(block_id=block_id, label=label, creator_name="com.superlogical.terminal", flavor=flavor,
                host_id="h", placement="placed", window_id=window, session_id=session)


def root_record(state: str = "working", msg: str = "m") -> dict:
    return dict(state=state, app="claude-code", effective_app="claude-code", msg=msg,
                updated_at="2026-11-27T09:44:23-03:00")


def claude_process(pid: int = 4242, cwd: str = "/work") -> dict:
    return dict(foreground=dict(pid=pid, name="2.1.332", path="/v/2.1.332", invoked_path="/home/u/.local/bin/claude",
                                argv0="claude", cwd=cwd))


class RexTestCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        for patcher in (
            mock.patch.dict(config.CONFIG, {"state_dir": str(self.sb.state_dir), "runtime": "rex"}),
            mock.patch.dict(os.environ, {"PATH": f"{self.sb.bin_dir}:{os.environ.get('PATH', '')}"}),
            mock.patch.object(terminals.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("REX_BLOCK", None)
        terminals.current.cache_clear()
        self.addCleanup(terminals.current.cache_clear)
        self.rex = terminals.current()

    def stub(self, blocks, statuses=None, processes=None, gone=(), **kwargs):
        self.sb.write_rex_stub(blocks, statuses or {}, processes or {}, set(gone), **kwargs)


class TestCurrent(RexTestCase):
    def test_current_is_rex_for_runtime_rex(self):
        self.assertIsInstance(self.rex, terminals.RexTerminals)

    def test_every_call_refuses_to_autostart_a_server(self):
        # An autostarted, empty server would read as an empty fleet.
        self.stub([block("block:a", "rev-x", "window:1")])
        self.rex.ls()
        raw = [json.loads(line) for line in (self.sb.root / "rex.calls").read_text().splitlines()]
        self.assertTrue(raw)
        self.assertTrue(all(call[0] == "--autostart=false" for call in raw), raw)


class TestListing(RexTestCase):
    def test_ls_maps_blocks_to_hc_shape(self):
        self.stub([block("block:a", "rev-x", "window:1")],
                  statuses={"block:a": {"records": [root_record("working", "m")]}})
        listed = self.rex.ls()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["address"], "block:a")
        self.assertEqual(listed[0]["name"], "rev-x")
        self.assertEqual(listed[0]["agent"], "claude-code")
        self.assertEqual(listed[0]["status"]["state"], "working")
        self.assertEqual(listed[0]["status"]["msg"], "m")
        self.assertIn(["block", "ls", "-s", SESSION, "--json"], self.sb.rex_calls())

    def test_ls_lists_every_session(self):
        self.stub([block("block:a", "a", "window:1"), block("block:b", "b", "window:2", OTHER_SESSION)])
        self.assertEqual([t["address"] for t in self.rex.ls()], ["block:a", "block:b"])

    def test_ls_block_with_no_status_has_none(self):
        self.stub([block("block:a", "shell", "window:1")])
        listed = self.rex.ls()[0]
        self.assertIsNone(listed["status"])
        self.assertIsNone(listed["agent"])

    def test_session_closed_between_ls_and_block_ls_is_skipped(self):
        self.stub([block("block:a", "a", "window:1")], gone_sessions=("session:closed",))
        self.assertEqual([t["address"] for t in self.rex.ls()], ["block:a"])
        self.assertIn(["block", "ls", "-s", "session:closed", "--json"], self.sb.rex_calls())

    def test_non_claude_status_block_is_not_an_agent(self):
        mole = dict(state="done", app="mole", effective_app="mole", msg="", updated_at="t")
        self.stub([block("block:a", "a", "window:1"), block("block:m", "m", "window:2")],
                  statuses={"block:a": {"records": [root_record()]}, "block:m": {"records": [mole]}},
                  processes={"block:a": claude_process(),
                             "block:m": dict(foreground=dict(pid=7, invoked_path="/bin/mole", argv0="mole", cwd="/"))})
        listed = {t["address"]: t for t in self.rex.ls()}
        self.assertIsNone(listed["block:m"]["status"])
        self.assertIsNone(listed["block:m"]["agent"])
        self.assertIsNone(self.rex.get("block:m")["status"])
        self.assertEqual([a["address"] for a in fleet.load_agents()], ["block:a"])

    def test_claude_started_from_its_shell_is_an_agent_without_status(self):
        self.stub([block("block:a", "a", "window:1")], processes={"block:a": claude_process()})
        self.assertEqual([a["address"] for a in fleet.load_agents()], ["block:a"])

    def test_ls_skips_non_terminal_blocks(self):
        self.stub([block("block:a", "a", "window:1"),
                   block("block:web", "web", "window:1", flavor="com.superlogical.browser")])
        self.assertEqual([t["address"] for t in self.rex.ls()], ["block:a"])

    def test_unreachable_server_raises_not_empty(self):
        self.stub([block("block:a", "a", "window:1")], fail_ls=True)
        with self.assertRaises(CheckError):
            fleet.load_agents()

    def test_prune_skipped_when_listing_failed(self):
        labels_store.merge("block:a", {"fleet.role": "worker"})
        self.stub([block("block:a", "a", "window:1")], fail_ls=True)
        with self.assertRaises(CheckError):
            self.rex.ls()
        self.assertEqual(labels_store.labels_for("block:a"), {"fleet.role": "worker"})

    def test_prune_after_listing_drops_only_absent_blocks(self):
        labels_store.merge("block:a", {"fleet.role": "worker"})
        labels_store.merge("block:old", {"fleet.role": "worker"})
        old_file = self.sb.state_dir / "rex-labels" / "old.json"
        os.utime(old_file, (0, 0))
        self.stub([block("block:a", "a", "window:1")])
        self.rex.ls()
        self.assertEqual(labels_store.labels_for("block:a"), {"fleet.role": "worker"})
        self.assertFalse(old_file.exists())

    def test_prune_keeps_labels_written_after_the_listing_started(self):
        # A block launched while the listing runs is not in it; its fresh labels must stay.
        labels_store.merge("block:new", {"fleet.role": "worker"})
        self.stub([block("block:a", "a", "window:1")])
        with mock.patch.object(terminals.time, "time", return_value=0):
            self.rex.ls()
        self.assertEqual(labels_store.labels_for("block:new"), {"fleet.role": "worker"})


class TestGet(RexTestCase):
    def test_get_maps_process_status_and_labels(self):
        labels_store.merge("block:a", {"fleet.domain": "demo", "fleet.parent": "block:boss"})
        sleep_record = dict(id="r1", state="working", app="claude-code", title="sleep 120", updated_at="t")
        self.stub([block("block:a", "rev-x", "window:1")],
                  statuses={"block:a": {"records": [root_record("working", "1 shell still running"), sleep_record]}},
                  processes={"block:a": claude_process(4242, "/work")})
        got = self.rex.get("block:a")
        self.assertEqual(got["address"], "block:a")
        self.assertEqual(got["name"], "rev-x")
        self.assertEqual(got["foreground"]["pid"], 4242)
        self.assertEqual(got["foreground"]["cwd"], "/work")
        self.assertEqual(got["foreground"]["argv"], ["/home/u/.local/bin/claude"])
        self.assertEqual(got["cwd"], "/work")
        self.assertEqual(got["labels"]["herdr.space"], SESSION)
        self.assertEqual(got["labels"]["herdr.tab"], "window:1")
        self.assertEqual(got["labels"]["fleet.domain"], "demo")
        self.assertEqual(got["parent"], "block:boss")
        self.assertIsNone(got["turn"])
        self.assertEqual(got["status"], {"state": "working", "msg": "1 shell still running",
                                         "updated_at": "2026-11-27T09:44:23-03:00"})
        self.assertEqual(got["records"], {"r1": {"state": "working", "title": "sleep 120"}})
        self.assertEqual(fleet.still_running(got), "1 shell still running: sleep 120")

    def test_record_without_title_uses_its_msg(self):
        self.stub([block("block:a", "a", "window:1")],
                  statuses={"block:a": {"records": [dict(id="r2", state="idle", msg="done")]}})
        got = self.rex.get("block:a")
        self.assertEqual(got["records"], {"r2": {"state": "idle", "title": "done"}})
        self.assertIsNone(got["status"])

    def test_get_returns_none_for_gone_block(self):
        self.stub([block("block:a", "a", "window:1")], gone={"block:a"})
        self.assertIsNone(self.rex.get("block:a"))

    def test_get_strict_names_the_gone_block(self):
        self.stub([block("block:a", "a", "window:1")], gone={"block:a"})
        with self.assertRaises(CheckError) as caught:
            self.rex.get("block:a", strict=True)
        self.assertIn("block:a", str(caught.exception))

    def test_block_closed_between_list_and_get_is_left_out(self):
        self.stub([block("block:a", "a", "window:1"), block("block:b", "b", "window:2")],
                  statuses={"block:a": {"records": [root_record()]}, "block:b": {"records": [root_record()]}},
                  processes={"block:a": claude_process(), "block:b": claude_process()},
                  gone={"block:b"})
        agents = fleet.load_agents()
        self.assertEqual([a["address"] for a in agents], ["block:a"])
        self.assertIsNone(self.rex.get("block:b"))


class TestLaunch(RexTestCase):
    def new_block_fixture(self, **kwargs):
        # Two sessions, the space being the second one, so "first session" cannot pass for the space.
        self.stub([block("block:boss", "boss", "window:1", SESSION),
                   block("block:new", "shell", "window:new", OTHER_SESSION)],
                  statuses={"block:new": {"records": [root_record("idle", "")]}},
                  processes={"block:new": claude_process()}, **kwargs)

    def test_launch_opens_in_the_space_session(self):
        self.new_block_fixture()
        started = self.rex.launch("dlg-1", "/work", {"fleet.role": "worker", "fleet.domain": "demo"},
                                  ["claude", "--model", "opus"], "block:boss", OTHER_SESSION, None, "Demo")
        calls = self.sb.rex_calls()
        new = calls[0]
        self.assertEqual(new[:new.index("--") + 1],
                         ["window", "new", "Demo", "-s", OTHER_SESSION, "-c", "/work", "--focus=false", "--shell", "none",
                          "--json", "--"])
        self.assertEqual(new[-3:], ["claude", "--model", "opus"])
        self.assertEqual(calls[1], ["block", "rename", "block:new", "dlg-1"])
        self.assertEqual(labels_store.labels_for("block:new"),
                         {"fleet.role": "worker", "fleet.domain": "demo", "fleet.parent": "block:boss"})
        self.assertEqual(started["address"], "block:new")
        self.assertEqual(started["labels"]["herdr.space"], OTHER_SESSION)
        self.assertTrue(started["tab_named"])
        self.assertTrue(started["running"])

    def test_launch_into_a_closed_space_raises_check_error_naming_it(self):
        self.stub([block("block:a", "a", "window:1")], gone_sessions=("session:closed",))
        with self.assertRaises(CheckError) as caught:
            self.rex.launch("dlg-3", "/work", {}, ["claude"], None, "session:closed", None, "Demo")
        self.assertIn("session:closed", str(caught.exception))

    def test_launch_whose_block_closes_before_rename_raises_check_error(self):
        self.stub([block("block:a", "a", "window:1")], gone={"block:new"})
        with self.assertRaises(CheckError) as caught:
            self.rex.launch("dlg-4", "/work", {}, ["claude"], None, SESSION, None, "Demo")
        self.assertIn("block:new", str(caught.exception))

    def test_launch_with_tab_splits_that_window(self):
        self.new_block_fixture()
        started = self.rex.launch("dlg-2", "/work", {}, ["claude"], None, SESSION, "window:1", "Demo")
        run = next(call for call in self.sb.rex_calls() if call[0] == "run")
        self.assertEqual(run[:run.index("--") + 1],
                         ["run", "-w", "window:1", "--split", "right", "--focus=false", "--label", "dlg-2",
                          "-c", "/work", "--json", "--"])
        self.assertEqual(run[-1], "claude")
        self.assertNotIn("fleet.parent", labels_store.labels_for("block:new"))
        self.assertEqual(started["address"], "block:new")

    def test_launch_rejects_a_bad_name(self):
        self.new_block_fixture()
        with self.assertRaises(CheckError):
            self.rex.launch("Bad Name", "/work", {}, ["claude"], None, SESSION, None, "Demo")
        self.assertEqual(self.sb.rex_calls(), [])


class TestSendReadKeys(RexTestCase):
    def test_send_pastes_then_enters_and_stops_on_paste_failure(self):
        self.stub([block("block:a", "a", "window:1")])
        with mock.patch.dict(os.environ, {"REX_BLOCK": "block:me"}):
            sent = self.rex.send("block:a", "hello")
        self.assertEqual(sent.returncode, 0)
        calls = self.sb.rex_calls()
        self.assertEqual(calls, [["api", "call", "-s", "block:a", "block.event", "-"],
                                 ["send-key", "-b", "block:a", "enter"]])
        event = json.loads((self.sb.root / "rex.stdin").read_text().splitlines()[0])
        self.assertEqual(event["block_id"], "block:a")
        self.assertEqual(event["event"]["type"], "paste")
        self.assertEqual(event["event"]["payload"]["text"], '<rex from="block:me">\nhello')
        terminals.time.sleep.assert_any_call(0.5)

        (self.sb.root / "rex.calls").unlink()
        self.stub([block("block:a", "a", "window:1")], fail_paste=True)
        failed = self.rex.send("block:a", "hello")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual([c[0] for c in self.sb.rex_calls()], ["api"])

    def test_send_untagged_or_outside_a_block_has_no_tag(self):
        self.stub([block("block:a", "a", "window:1")])
        with mock.patch.dict(os.environ, {"REX_BLOCK": "block:me"}):
            self.rex.send("block:a", "one", tagged=False)
        self.rex.send("block:a", "two")
        texts = [json.loads(line)["event"]["payload"]["text"]
                 for line in (self.sb.root / "rex.stdin").read_text().splitlines()]
        self.assertEqual(texts, ["one", "two"])

    def test_read_returns_last_n_lines(self):
        self.stub([block("block:a", "a", "window:1")], screens={"block:a": "l1\nl2\nl3\nl4"})
        self.assertEqual(self.rex.read("block:a", 2).splitlines(), ["l3", "l4"])
        self.assertEqual(self.rex.read("block:a").splitlines(), ["l1", "l2", "l3", "l4"])
        self.assertIn(["capture", "-b", "block:a", "--trim"], self.sb.rex_calls())

    def test_read_strict_raises_for_gone_block(self):
        self.stub([block("block:a", "a", "window:1")], gone={"block:a"})
        self.assertEqual(self.rex.read("block:a"), "")
        with self.assertRaises(CheckError):
            self.rex.read("block:a", strict=True)

    def test_keys_types_letters_and_presses_named_keys(self):
        self.stub([block("block:a", "a", "window:1")])
        self.rex.keys("block:a", list("/exit"))
        self.rex.keys("block:a", ["backspace", "backspace"])
        self.rex.keys("block:a", ["enter"])
        self.assertEqual(self.sb.rex_calls(), [["send", "-b", "block:a", "--", "/exit"],
                                               ["send-key", "-b", "block:a", "backspace", "backspace"],
                                               ["send-key", "-b", "block:a", "enter"]])

    def test_set_labels_merges_into_the_sidecar(self):
        self.stub([block("block:a", "a", "window:1")])
        self.rex.set_labels("block:a", {"inbox.status": "open"})
        self.rex.set_labels("block:a", {"fleet.role": "worker"}, quiet_timeout=5)
        self.assertEqual(labels_store.labels_for("block:a"), {"inbox.status": "open", "fleet.role": "worker"})
        self.assertEqual(self.sb.rex_calls(), [])


class TestEndAndMisc(RexTestCase):
    def test_end_closes_window_when_block_is_alone(self):
        self.stub([block("block:a", "a", "window:1"), block("block:b", "b", "window:2"),
                   block("block:c", "c", "window:2")])
        self.rex.end("block:a")
        self.assertEqual(self.sb.rex_calls()[-1], ["window", "close", "window:1"])
        self.rex.end("block:b")
        self.assertEqual(self.sb.rex_calls()[-1], ["block", "close", "block:b"])

    def test_end_of_gone_block_does_nothing(self):
        self.stub([block("block:a", "a", "window:1")], gone={"block:a"})
        self.rex.end("block:a")
        self.assertFalse(any(c[1:2] == ["close"] for c in self.sb.rex_calls()))

    def test_self_address_is_rex_block(self):
        with mock.patch.dict(os.environ, {"REX_BLOCK": "block:me"}):
            self.assertEqual(self.rex.self_address(), "block:me")
        self.assertIsNone(self.rex.self_address())

    def test_rename_tab_renames_the_window(self):
        self.stub([block("block:a", "a", "window:1")])
        self.assertTrue(self.rex.rename_tab("window:1", "Demo"))
        self.assertEqual(self.sb.rex_calls(), [["window", "rename", "window:1", "Demo"]])

    def test_focus_hint_is_printed_never_run(self):
        self.stub([block("block:a", "a", "window:1")])
        hint = self.rex.focus_hint("block:a")
        self.assertIn("rex focus block:a", hint)
        self.assertEqual(self.sb.rex_calls(), [])

    def test_inbox_panel_unsupported_returns_none(self):
        self.stub([block("block:a", "a", "window:1")])
        with mock.patch("builtins.print") as printed:
            self.assertIsNone(self.rex.inbox_panel("block:a", None))
        printed.assert_called()
        self.assertEqual(self.sb.rex_calls(), [])


class TestCoordinatorRestart(RexTestCase):
    def test_restart_on_rex_reopens_in_same_session_and_relabels(self):
        self.sb.write_notes("demo", "# Demo\n\n## Open\n\n## Done\n")
        labels_store.merge("block:old", {"fleet.role": "coordinator", "fleet.domain": "demo", "herdr.pinned": "1"})
        screen = "\u23fa ready for restart\n\u276f /exit\n"
        self.stub([block("block:old", "Demo", "window:1", OTHER_SESSION),
                   block("block:new", "Demo", "window:new", OTHER_SESSION)],
                  statuses={"block:old": {"records": [root_record("idle", "")]},
                            "block:new": {"records": [root_record("idle", "")]}},
                  processes={"block:old": claude_process(4242, "/work/demo"),
                             "block:new": claude_process(4243, "/work/demo")},
                  screens={"block:old": screen})
        old_label_file = self.sb.state_dir / "rex-labels" / "old.json"
        self.assertTrue(old_label_file.exists())
        gone_after_exit = mock.Mock(return_value=None)
        folder = self.sb.backlog_dir
        with mock.patch.object(fleet, "domain_files", functools.partial(fleet.domain_files, folder=folder)), \
                mock.patch.object(fleet, "domains_in", functools.partial(fleet.domains_in, folder=folder)), \
                mock.patch.object(fleet, "why_not_a_domain", functools.partial(fleet.why_not_a_domain, folder=folder)), \
                mock.patch.object(coordinator_restart, "listed", gone_after_exit), \
                mock.patch.object(coordinator_restart, "background_copies_since", return_value=[]), \
                mock.patch.object(coordinator_restart, "report_started"), \
                mock.patch.object(coordinator_restart.inbox_state, "inbox_store", return_value=None), \
                mock.patch("sys.argv", ["coordinator_restart.py", "restart", "demo"]):
            self.assertEqual(coordinator_restart.main(), 0)
        calls = self.sb.rex_calls()
        sent = [c for c in calls if c[0] in ("send", "send-key")]
        self.assertEqual(sent[0], ["send", "-b", "block:old", "--", "/exit"])
        self.assertEqual(sent[1], ["send-key", "-b", "block:old", "enter"])
        new = next(c for c in calls if c[:2] == ["window", "new"])
        self.assertLess(calls.index(sent[1]), calls.index(new))
        self.assertEqual(new[:new.index("--")],
                         ["window", "new", "Demo", "-s", OTHER_SESSION, "-c", "/work/demo", "--focus=false",
                          "--shell", "none", "--json"])
        self.assertEqual(labels_store.labels_for("block:new"),
                         {"fleet.role": "coordinator", "fleet.domain": "demo", "herdr.pinned": "1"})
        self.assertFalse(old_label_file.exists())
        self.assertNotIn("--focus=true", sum(calls, []))


if __name__ == "__main__":
    unittest.main()
