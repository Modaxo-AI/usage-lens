import os
import unittest

from claude_usage.parser import parse_file

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "projects")
MAIN_1 = os.path.join(FIXTURES, "-Users-x-proj1", "sess-main-1.jsonl")


class TestToolParsing(unittest.TestCase):
    def setUp(self):
        self.facts = parse_file(MAIN_1, "main")

    def test_tool_use_names_captured(self):
        self.assertEqual(self.facts.tool_calls["tu-bash-1"], "Bash")
        self.assertEqual(self.facts.tool_calls["tu-skill-1"], "Skill")
        self.assertEqual(self.facts.tool_calls["tu-mcp-1"], "mcp__myserver__do_thing")

    def test_tool_result_errors_captured(self):
        self.assertTrue(self.facts.tool_errors["tu-bash-1"])
        self.assertFalse(self.facts.tool_errors["tu-mcp-1"])

    def test_orphan_tool_result_recorded_without_matching_call(self):
        # The join to "(unknown)" happens downstream in aggregate.py; parser.py
        # just needs to not drop or crash on a tool_result with no matching tool_use.
        self.assertTrue(self.facts.tool_errors["tu-orphan-unknown"])
        self.assertNotIn("tu-orphan-unknown", self.facts.tool_calls)

    def test_mcp_tool_name_format(self):
        name = self.facts.tool_calls["tu-mcp-1"]
        parts = name.split("__")
        self.assertEqual(parts[0], "mcp")
        self.assertEqual(parts[1], "myserver")


if __name__ == "__main__":
    unittest.main()
