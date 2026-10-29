import json
import os
import tempfile
import unittest

import collect
from claude_usage.cache import FileCache
from claude_usage.parser import parse_file, parse_meta
from claude_usage.scan import scan

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "projects")
SUB_DIR = os.path.join(FIXTURES, "-Users-x-proj1", "sess-main-1", "subagents")
SUB1 = os.path.join(SUB_DIR, "agent-sub1.jsonl")
SUB1_META = os.path.join(SUB_DIR, "agent-sub1.meta.json")
SUB2 = os.path.join(SUB_DIR, "agent-sub2.jsonl")
SUB2_META = os.path.join(SUB_DIR, "agent-sub2.meta.json")  # does not exist


class TestSubagentParsing(unittest.TestCase):
    def test_subagent_usage_deduped_and_flagged(self):
        facts = parse_file(SUB1, "sub")
        self.assertEqual(len(facts.requests), 1)
        rec = facts.requests[0]
        self.assertEqual(rec["output_tokens"], 45)  # largest of 5, 45
        self.assertTrue(rec["is_sub"])
        self.assertEqual(rec["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(rec["skill"], "dataviz")

    def test_meta_fields(self):
        meta = parse_meta(SUB1_META)
        self.assertEqual(meta["agent_type"], "general-purpose")
        self.assertEqual(meta["spawn_depth"], 1)
        self.assertIsNone(meta["parent_agent_id"])
        self.assertEqual(meta["tool_use_id"], "toolu_abc123")

    def test_description_truncated_to_120_chars(self):
        meta = parse_meta(SUB1_META)
        self.assertLessEqual(len(meta["description"]), 120)
        self.assertEqual(len(meta["description"]), 120)  # the fixture description is longer than 120 chars

    def test_missing_meta_file_returns_empty_dict(self):
        meta = parse_meta(SUB2_META)
        self.assertEqual(meta, {})

    def test_subagent_without_meta_can_still_be_parsed(self):
        facts = parse_file(SUB2, "sub")
        self.assertEqual(len(facts.requests), 1)
        self.assertTrue(facts.requests[0]["is_sub"])


if __name__ == "__main__":
    unittest.main()


class TestSubagentsByTypeMatchesTheTile(unittest.TestCase):
    """"Subagents by type" reads the same date- and model-filtered usage rows
    as the Subagent cost tile, so its bars add up to the tile. An agent that
    started before the range still has its in-range work counted, and a
    filtered-out model's share of an agent is not."""

    def _chart(self, metric):
        from tests.jsrun import run_js
        from tests.test_health_ui import DASHBOARD, DAY, _dashboard, _session

        def usage(day, sid, model, agent_type, cost, tokens):
            row = [f"{day}T10:00", sid, model, "(none)", agent_type] + [0] * 11
            row[5], row[12], row[13] = tokens, 1, cost
            return row
        rows = [usage(DAY, "s1", "m", "Explore", 1.0, 100),       # agent started the day before
                usage(DAY, "s1", "m", "Plan", 2.0, 200),
                usage(DAY, "s1", "other", "Plan", 4.0, 400),      # filtered-out model
                usage(DAY, "s1", "m", -1, 8.0, 800)]              # main thread
        agents = [{"session_id": "s1", "agent_id": "a", "date": "2026-02-09",
                   "agent_type": "Explore", "spawn_depth": 1, "models": ["m"],
                   "cost": 1.0, "tokens": 100},
                  {"session_id": "s1", "agent_id": "b", "date": DAY, "agent_type": "Plan",
                   "spawn_depth": 1, "models": ["m", "other"], "cost": 6.0, "tokens": 600}]
        setup = (_dashboard(sessions=[dict(_session("s1"), models=["m", "other"])], usage=rows)
                 + f"PAYLOAD.agents = {json.dumps(agents)}; state.models = ['m'];"
                 f"state.agentsMetric = '{metric}'; renderAgents();")
        chart = run_js(DASHBOARD, setup + "out(charts['agentTypeCost'].data)")
        return dict(zip(chart["labels"], chart["datasets"][0]["data"]))

    def test_cost_bars_use_the_filtered_usage(self):
        self.assertEqual(self._chart("cost"), {"Explore": 1.0, "Plan": 2.0})

    def test_token_bars_use_the_filtered_usage(self):
        self.assertEqual(self._chart("tokens"), {"Explore": 100, "Plan": 200})


def _request(sid, ts, msg_id, cwd=None, agent_id=None):
    line = {"type": "assistant", "sessionId": sid, "timestamp": ts, "requestId": "req-" + msg_id,
            "message": {"id": msg_id, "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 1000, "output_tokens": 10}}}
    if cwd:
        line["cwd"] = cwd
    if agent_id:
        line.update(agentId=agent_id, isSidechain=True)
    return line


def _write_lines(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(l) for l in lines) + ("\n" if lines else ""))


class TestSubagentWithoutItsParentTranscript(unittest.TestCase):
    """The subagent's cost is in every total, so it must also belong to a
    project the dashboard can filter on -- else a project filter drops it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "projects")

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self):
        return scan(self.root, cache=FileCache(os.path.join(self.tmp.name, "c.json")))

    def _orphan(self, folder="-w-proj", cwd="/w/proj", sid="gone"):
        _write_lines(os.path.join(self.root, folder, sid, "subagents", "agent-o.jsonl"),
                     [_request(sid, "2026-01-01T10:00:00Z", "o-1", cwd=cwd, agent_id="o")])

    def test_it_lands_in_the_project_of_its_cwd(self):
        self._orphan()
        payload = self._scan()
        (agent,) = payload["agents"]
        self.assertEqual(agent["project"], "/w/proj")
        self.assertIn("/w/proj", payload["dims"]["projects"])

    def test_without_a_cwd_it_takes_its_folder(self):
        self._orphan(cwd=None)
        payload = self._scan()
        (agent,) = payload["agents"]
        self.assertEqual(agent["project"], "/w/proj (decoded)")
        self.assertIn("/w/proj (decoded)", payload["dims"]["projects"])

    def test_a_parent_with_a_row_keeps_its_own_project(self):
        _write_lines(os.path.join(self.root, "-w-proj", "sess.jsonl"),
                     [_request("sess", "2026-01-01T10:00:00Z", "m-1", cwd="/w/main")])
        _write_lines(os.path.join(self.root, "-w-proj", "sess", "subagents", "agent-a.jsonl"),
                     [_request("sess", "2026-01-01T10:01:00Z", "a-1", cwd="/w/elsewhere", agent_id="a")])
        payload = self._scan()
        (agent,) = payload["agents"]
        self.assertEqual(agent["project"], "/w/main")
        self.assertEqual(payload["dims"]["projects"], ["/w/main"])

    def test_every_usage_key_has_a_project(self):
        self._orphan()
        payload = self._scan()
        projects = {s["id"]: s["project"] for s in payload["sessions"]}
        projects.update({a["session_id"]: a["project"] for a in payload["agents"]})
        self.assertTrue(payload["usage"])
        for row in payload["usage"]:
            self.assertIn(projects[row[1]], payload["dims"]["projects"])

    def test_the_dashboard_filters_it_by_that_project(self):
        from tests.jsrun import run_js
        self._orphan()
        payload = self._scan()
        slim = {k: payload[k] for k in ("usage", "agents", "sessions")}
        setup = (f"PAYLOAD = {json.dumps(slim)};"
                 "sessionById = Object.fromEntries(PAYLOAD.sessions.map(s => [s.id, s]));"
                 "indexAgentProjects();")
        key = payload["agents"][0]["session_id"]
        got = run_js(["static/habits.js", "static/app.js"], setup + f"out([projectOf({json.dumps(key)}), projectOf('nope')])")
        self.assertEqual(got, ["/w/proj", "(unknown)"])

    def test_redact_hides_the_orphans_project_path(self):
        self._orphan(folder="-Users-x-secret", cwd=None)
        red = collect._redact(self._scan())
        text = json.dumps(red)
        for needle in ("secret", "Users"):
            self.assertNotIn(needle, text)
        (agent,) = red["agents"]
        self.assertIn(agent["project"], red["dims"]["projects"])
        self.assertTrue(agent["project"].startswith("project-"))

    def test_redact_names_a_shared_project_once(self):
        _write_lines(os.path.join(self.root, "-w-proj", "sess.jsonl"),
                     [_request("sess", "2026-01-01T10:00:00Z", "m-1", cwd="/w/proj")])
        self._orphan()
        red = collect._redact(self._scan())
        self.assertEqual(red["dims"]["projects"], ["project-1"])
        self.assertEqual({s["project"] for s in red["sessions"]}
                         | {a["project"] for a in red["agents"]}, {"project-1"})


class TestEmptyTranscriptsAreNotSessions(unittest.TestCase):
    def test_a_transcript_with_no_requests_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "projects")
            _write_lines(os.path.join(root, "-w-proj", "real.jsonl"),
                         [_request("real", "2026-01-01T10:00:00Z", "m-1", cwd="/w/proj")])
            _write_lines(os.path.join(root, "-w-proj", "empty.jsonl"),
                         [{"type": "user", "sessionId": "empty", "timestamp": "2026-01-01T09:00:00Z",
                           "cwd": "/w/proj", "message": {"role": "user", "content": "hi"}}])
            payload = scan(root, cache=FileCache(os.path.join(tmp, "c.json")))
            summary = payload["summary"]
            self.assertEqual(summary["total_sessions"], 1)
            self.assertEqual(summary["total_transcripts"], 2)
            self.assertEqual(summary["derived_sessions"], 0)
            self.assertEqual({s["id"] for s in payload["sessions"]}, {"real", "empty"})
