import json
import unittest
from unittest.mock import patch

from test_activity import activity


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.api = activity.Zellij("synthetic-zellij", "synthetic-session")
        self.pane = dict(id=1, tab_id=10, title="Synthetic pane", tab_name="Synthetic tab",
                         is_plugin=False, exited=False)

    def snapshot(self, panes, tabs=None, required=()):
        replies = [json.dumps(panes)]
        if tabs is not None:
            replies.append(json.dumps(tabs))
        with patch.object(self.api, "action", side_effect=replies) as action:
            result = self.api.snapshot(required)
            return result, action.call_args_list

    def test_one_query_with_tab_metadata_and_fresh_names(self):
        (panes, tabs), calls = self.snapshot([self.pane])
        self.assertEqual(len(calls), 1)
        self.assertEqual(tabs["10"]["name"], "Synthetic tab")
        self.assertEqual(panes["1"]["title"], "Synthetic pane")
        self.pane.update(tab_id=20, tab_name="User tab", title="User pane")
        (panes, tabs), calls = self.snapshot([self.pane])
        self.assertEqual(len(calls), 1)
        self.assertEqual(tabs["20"]["name"], "User tab")
        self.assertEqual(panes["1"]["title"], "User pane")

    def test_plugin_and_exited_panes_still_supply_tab_names(self):
        plugin = dict(self.pane, is_plugin=True)
        exited = dict(self.pane, id=2, tab_id=20, tab_name="Other", exited=True)
        (panes, tabs), calls = self.snapshot([plugin, exited], required=("10", "20"))
        self.assertEqual(panes, {})
        self.assertEqual(set(tabs), {"10", "20"})
        self.assertEqual(len(calls), 1)

    def test_legacy_schema_and_unresolved_owned_tab_use_fallback(self):
        legacy = dict(self.pane)
        del legacy["tab_name"]
        for panes, required in (([legacy], ()), ([self.pane], ("20",)), ([], ("10",))):
            (result, tabs), calls = self.snapshot(
                panes, [dict(tab_id=10, name="Synthetic tab"), dict(tab_id=20, name="Other")],
                required)
            self.assertEqual(set(tabs), {"10", "20"})
            self.assertEqual(len(calls), 2)

    def test_invalid_metadata_is_visible(self):
        for panes in ({}, [None], [dict(self.pane, id=True)], [dict(self.pane, tab_name=None)],
                      [self.pane, self.pane], [self.pane, dict(self.pane, id=2, tab_name="Conflict")]):
            with self.subTest(panes=panes), self.assertRaises(ValueError):
                self.snapshot(panes)
        legacy = dict(self.pane)
        del legacy["tab_name"]
        for tabs in ({}, [None], [dict(tab_id=10, name=1)],
                     [dict(tab_id=10, name="A")] * 2):
            with self.subTest(tabs=tabs), self.assertRaises(ValueError):
                self.snapshot([legacy], tabs)

    def test_fallback_race_and_rpc_failure_are_not_empty_success(self):
        with self.assertRaisesRegex(ValueError, "changed"):
            self.snapshot([self.pane], [dict(tab_id=10, name="Changed")], ("20",))
        with patch.object(self.api, "action", side_effect=RuntimeError("RPC unavailable")):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                self.api.snapshot()


if __name__ == "__main__":
    unittest.main()
