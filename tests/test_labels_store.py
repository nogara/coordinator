"""Tests for labels_store.py (per-block key/value labels kept in sidecar JSON files)."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from tests.helpers import Sandbox, add_script_paths

add_script_paths()
import config  # noqa: E402
import labels_store  # noqa: E402


class TestLabelsStore(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        patcher = mock.patch.dict(config.CONFIG, {"state_dir": str(self.sb.state_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.sb.cleanup)

    def label_dir(self):
        return self.sb.state_dir / "rex-labels"

    def test_merge_then_read(self):
        labels_store.merge("block:7", {"fleet.role": "worker", "fleet.domain": "demo"})
        self.assertEqual(labels_store.labels_for("block:7"), {"fleet.role": "worker", "fleet.domain": "demo"})
        self.assertEqual(
            json.loads((self.label_dir() / "7.json").read_text()),
            {"fleet.role": "worker", "fleet.domain": "demo"},
        )

    def test_merge_keeps_other_keys(self):
        labels_store.merge("block:7", {"fleet.role": "worker"})
        labels_store.merge("block:7", {"inbox.state": "open"})
        self.assertEqual(labels_store.labels_for("block:7"), {"fleet.role": "worker", "inbox.state": "open"})

    def test_value_empty_string_removes_key(self):
        # Same meaning as `hc label k=`: an empty value clears the label.
        labels_store.merge("block:7", {"fleet.role": "worker", "fleet.domain": "demo"})
        labels_store.merge("block:7", {"fleet.role": ""})
        self.assertEqual(labels_store.labels_for("block:7"), {"fleet.domain": "demo"})

    def test_prune_removes_only_absent_ids(self):
        labels_store.merge("block:1", {"a": "1"})
        labels_store.merge("block:2", {"a": "2"})
        labels_store.merge("block:3", {"a": "3"})
        removed = labels_store.prune({"block:1", "3"})
        self.assertEqual(removed, 1)
        self.assertEqual(labels_store.labels_for("block:1"), {"a": "1"})
        self.assertEqual(labels_store.labels_for("block:2"), {})
        self.assertEqual(labels_store.labels_for("block:3"), {"a": "3"})

    def test_labels_for_missing_or_corrupt_file_is_empty(self):
        self.assertEqual(labels_store.labels_for("block:9"), {})
        self.label_dir().mkdir()
        (self.label_dir() / "9.json").write_text("{not json")
        self.assertEqual(labels_store.labels_for("block:9"), {})

    def test_directory_created_lazily_on_first_write(self):
        self.assertFalse(self.label_dir().exists())
        self.assertEqual(labels_store.prune(set()), 0)
        self.assertFalse(self.label_dir().exists())
        labels_store.merge("5", {"k": "v"})
        self.assertEqual(labels_store.labels_for("block:5"), {"k": "v"})


if __name__ == "__main__":
    unittest.main()
