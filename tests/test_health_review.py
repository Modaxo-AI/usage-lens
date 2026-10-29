"""Fixes from the adversarial review of the usage-health work: the shell-command
classifier, what counts as a prompt, event de-duplication, the "checked after
last edit" rule and the long-context boundary."""
import json
import os
import shutil
import tempfile
import unittest

from claude_usage import health
from claude_usage.parser import FileFacts, parse_file
from tests.test_health import SENTINEL, _prompt, _response, _tool_result, _tool_use, _ts, _write


class TestClassifierLeadingPaths(unittest.TestCase):
    def test_a_path_on_the_first_token_is_stripped(self):
        cases = {
            ".venv/bin/python -m pytest -q": ["test"],
            "./.venv/bin/ruff check .": ["lint"],
            "venv/bin/pytest tests": ["test"],
            "/usr/local/bin/pytest": ["test"],
            "./node_modules/.bin/jest --ci": ["test"],
            "cd api && .venv/bin/python3 -m mypy src": ["lint"],
            "./gradlew test": ["test"],
        }
        for command, expected in cases.items():
            self.assertEqual(health.classify_command(command), expected, command)


class TestClassifierWrappersAndOptions(unittest.TestCase):
    def test_wrappers_and_options_before_the_command(self):
        cases = {
            "timeout 600 pytest -x": ["test"],
            "timeout -k 5 120s npm test": ["test"],
            "xvfb-run -a npm test": ["test"],
            "uv run --with requests pytest": ["test"],
            "uv run --python 3.12 --frozen pytest": ["test"],
            "git -c user.name=x -c user.email=y commit -m z": ["commit"],
            "git --no-pager commit --amend": ["commit"],
            "make -C build test": ["test"],
            "make -j8": ["build"],
            "make -j 8 check": ["test"],
            "python manage.py test app": ["test"],
            "python3 ./manage.py test": ["test"],
            "node --test": ["test"],
            "node --experimental-strip-types --test tests/": ["test"],
            "npm --prefix web test": ["test"],
            "yarn --cwd web test": ["test"],
            "pnpm -C web run lint": ["lint"],
            "npm --prefix web run build": ["build"],
        }
        for command, expected in cases.items():
            self.assertEqual(health.classify_command(command), expected, command)


class TestClassifierFalsePositives(unittest.TestCase):
    def test_heredoc_bodies_are_not_commands(self):
        for command in (
            "cat > notes.txt <<EOF\npytest\nmake\ngit commit\nEOF",
            "cat <<'EOF' > run.sh\nnpm test\nEOF",
            'python - <<"PY"\nimport os\nPY',
            "cat <<-END\n\tcargo build\n\tEND",
        ):
            self.assertEqual(health.classify_command(command), [], command)

    def test_commands_after_a_heredoc_still_count(self):
        self.assertEqual(health.classify_command("cat > x <<EOF\nmake\nEOF\npytest"), ["test"])
        # Claude Code's own commit idiom: the message is a heredoc.
        commit = "git commit -m \"$(cat <<'EOF'\nRun pytest and make build\n\nEOF\n)\""
        self.assertEqual(health.classify_command(commit), ["commit"])

    def test_a_heredoc_fed_to_a_shell_is_commands(self):
        self.assertEqual(health.classify_command("bash <<'EOF'\npytest -q\nEOF"), ["test"])
        self.assertEqual(health.classify_command("cd x && sh -s <<EOF\nmake\nEOF"), ["build"])

    def test_line_continuations_join_the_command(self):
        self.assertEqual(health.classify_command("rollup \\\n  -c"), ["build"])
        self.assertEqual(health.classify_command("isort \\\n  --check-only ."), ["lint"])

    def test_env_values_with_paths_are_not_the_command(self):
        for command in ("PYTHONPATH=/tmp/plug FOO=1 uv run pytest -p x",
                        'PYTHONPATH="/a b/c" python -m pytest'):
            self.assertEqual(health.classify_command(command), ["test"], command)

    def test_dev_servers_and_watchers_are_not_builds(self):
        for command in ("next dev", "next start -p 3000", "vite", "npx vite --port 5173",
                        "vite preview", "webpack serve", "webpack-dev-server",
                        "rollup -w", "rollup -cw", "rollup -c --watch", "tsup --watch",
                        "esbuild app.ts --watch"):
            self.assertEqual(health.classify_command(command), [], command)
        for command in ("next build", "vite build", "webpack", "webpack --mode production",
                        "rollup -c", "tsup", "esbuild app.ts --bundle"):
            self.assertEqual(health.classify_command(command), ["build"], command)

    def test_version_queries_are_not_checks(self):
        for command in ("pytest --version", "ruff --version", "npm --version",
                        "cargo --version", "python -m pytest --version"):
            self.assertEqual(health.classify_command(command), [], command)

    def test_formatters_count_only_when_checking(self):
        for command in ("black .", "isort .", "ruff format .", "cargo fmt"):
            self.assertEqual(health.classify_command(command), [], command)
        for command in ("black --check .", "isort --check-only .", "ruff format --check",
                        "cargo fmt --check", "black --diff src"):
            self.assertEqual(health.classify_command(command), ["lint"], command)


class _Parse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _parse(self, records, kind="main"):
        path = os.path.join(self.tmp, "s.jsonl")
        _write(path, records)
        return parse_file(path, kind)

    def _events(self, facts, kind):
        return [e for e in facts.health_events if e[2] == kind]


def _user(uuid, ts, text, as_list=False, **extra):
    content = [{"type": "text", "text": text}] if as_list else text
    return {"type": "user", "uuid": uuid, "timestamp": ts, "permissionMode": "auto",
            "origin": {"kind": "human"}, "message": {"role": "user", "content": content}, **extra}


class TestNotPrompts(_Parse):
    MARKUP = (
        "<command-name>/exit</command-name>\n<command-message>exit</command-message>",
        f"<command-message>review</command-message> {SENTINEL}",
        f"<local-command-stdout>{SENTINEL}</local-command-stdout>",
        f"<bash-input>ls {SENTINEL}</bash-input>",
        f"<ci-monitor-event>{SENTINEL}</ci-monitor-event>",
    )

    def test_slash_commands_and_their_output_are_not_prompts(self):
        records = [_user(f"u{i}", _ts(i), text, as_list=bool(i % 2))
                   for i, text in enumerate(self.MARKUP)]
        records.append(_user("real", _ts(9), f"<pasted_content>{SENTINEL}</pasted_content> fix it"))
        facts = self._parse(records)
        self.assertEqual([e[0] for e in self._events(facts, "prompt")], ["real"])

    def test_ending_on_interrupt_then_exit_still_ends_on_interrupt(self):
        facts = self._parse([
            _prompt("p1", _ts(0)),
            _response("m1", _ts(1)),
            _user("i1", _ts(2), "[Request interrupted by user]"),
            _user("c1", _ts(3), "<command-name>/exit</command-name>"),
            _user("c2", _ts(4), "<local-command-stdout>Bye!</local-command-stdout>"),
        ])
        self.assertTrue(facts.ended_on_interrupt)


class TestQueuedPrompts(_Parse):
    def _queued(self, uuid, minute, **att):
        return {"type": "attachment", "uuid": uuid, "timestamp": _ts(minute),
                "attachment": {"type": "queued_command", "commandMode": "prompt",
                               "prompt": SENTINEL, **att}}

    def test_only_human_queued_prompts_count(self):
        facts = self._parse([
            self._queued("q1", 0, origin={"kind": "human"}),
            self._queued("q2", 1),                                           # before `origin`
            self._queued("q3", 2, origin={"kind": "peer"}, isMeta=True),     # another session
            self._queued("q4", 3, origin={"kind": "peer"}),
            self._queued("q5", 4, origin={"kind": "human"}, isMeta=True),
        ])
        self.assertEqual([e[0] for e in self._events(facts, "queued")], ["q1", "q2"])


def _facts(**kw):
    f = FileFacts(path="x", kind="main")
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def _bucket(ts):
    return ts[:13] + ":00" if ts else None


class TestEventDedupe(unittest.TestCase):
    def test_one_record_can_be_a_denial_and_an_interrupt(self):
        facts = _facts(health_events=[["u1", _ts(0), "denial", "interrupted"],
                                      ["u1", _ts(0), "interrupt", None]])
        _, sessions = health.build([("s", facts)], [], set(), [], _bucket)
        self.assertEqual(sessions["s"]["interrupts"], 1)
        self.assertEqual(sessions["s"]["denials"], {"interrupted": 1})

    def test_a_copy_still_counts_once(self):
        events = [["u1", _ts(0), "interrupt", None]]
        _, sessions = health.build([("s", _facts(health_events=events)),
                                    ("copy", _facts(health_events=list(events)))],
                                   [], {"copy"}, [], _bucket)
        self.assertEqual((sessions["s"]["interrupts"], sessions["copy"]["interrupts"]), (1, 0))


class TestCheckedAfterLastEdit(_Parse):
    """Edits to notes, docs and Claude's own files after the last test don't
    make a session's code "unchecked"."""

    def _session(self, *paths_after_test, edit_first="src/app.py"):
        records = [
            _tool_use("m1", _ts(1), "tu-e1", "Edit", {"file_path": edit_first}),
            _tool_result("r1", _ts(2), "tu-e1"),
            _tool_use("m2", _ts(3), "tu-t", "Bash", {"command": "pytest"}),
            _tool_result("r2", _ts(4), "tu-t"),
        ]
        for i, path in enumerate(paths_after_test):
            records += [_tool_use(f"m{10 + i}", _ts(10 + i), f"tu-w{i}", "Write", {"file_path": path}),
                        _tool_result(f"r{10 + i}", _ts(10 + i), f"tu-w{i}")]
        facts = self._parse(records)
        _, sessions = health.build([("s", facts)], [], set(), [], _bucket)
        return sessions["s"]

    def test_trailing_notes_and_claude_files_do_not_uncheck(self):
        s = self._session("/Users/x/.claude/projects/p/memory/fact.md", ".claude/launch.json",
                          "docs/usage.md", "README.rst", "CHANGELOG.txt")
        self.assertTrue(s["checked_after_last_edit"])
        self.assertEqual(s["edits"], 1)

    def test_a_trailing_code_edit_still_unchecks(self):
        self.assertFalse(self._session("docs/x.md", "src/other.py")["checked_after_last_edit"])

    def test_a_session_that_only_edited_notes_had_nothing_to_check(self):
        s = self._session(edit_first="NOTES.md")
        self.assertIsNone(s["checked_after_last_edit"])
        self.assertEqual(s["edits"], 0)

    def test_no_path_is_kept(self):
        facts = self._parse([_tool_use("m1", _ts(1), "tu-1", "Write",
                                       {"file_path": f"/x/{SENTINEL}/notes.md"})])
        self.assertNotIn(SENTINEL, json.dumps(facts.__dict__, default=list))


class TestLongContextBoundary(unittest.TestCase):
    """"Long" is more than 200k tokens, the same in the session figure and the
    banded rows the strip, export and team view sum."""

    def _build(self, tokens):
        rec = {"ts": _ts(0), "model": "claude-sonnet-5", "input_tokens": tokens}
        payload, sessions = health.build([("s", _facts())], [], set(),
                                         [(rec, "s", "main", 1.0)], _bucket)
        t = payload["thresholds"]
        long_band = t["context_bands"].index(t["long_context_tokens"]) + 1
        banded = sum(r[5] for r in payload["context"] if r[3] >= long_band)
        return sessions["s"]["long_context_cost"], banded

    def test_exactly_200k_is_not_long(self):
        self.assertEqual(self._build(health.LONG_CONTEXT_TOKENS), (0.0, 0))

    def test_one_more_token_is_long_in_both(self):
        self.assertEqual(self._build(health.LONG_CONTEXT_TOKENS + 1), (1.0, 1.0))


class TestVeryLongContext(unittest.TestCase):
    """Context tax is flagged on requests over 500k tokens (the spec's line);
    200k stays as the export's long_context_cost_usd."""

    def _build(self, tokens):
        rec = {"ts": _ts(0), "model": "claude-sonnet-5", "input_tokens": tokens}
        payload, sessions = health.build([("s", _facts())], [], set(),
                                         [(rec, "s", "main", 1.0)], _bucket)
        return payload, sessions["s"]

    def test_constant_is_the_top_band_edge(self):
        self.assertEqual(health.VERY_LONG_CONTEXT_TOKENS, 500_000)
        self.assertEqual(health.VERY_LONG_CONTEXT_TOKENS, health.CONTEXT_BANDS[2])

    def test_exactly_500k_is_not_very_long(self):
        _, s = self._build(500_000)
        self.assertEqual(s["very_long_context_cost"], 0.0)
        self.assertEqual(s["very_long_context_share"], 0.0)

    def test_one_more_token_is_very_long(self):
        payload, s = self._build(500_001)
        self.assertEqual(s["very_long_context_cost"], 1.0)
        self.assertEqual(s["very_long_context_share"], 1.0)
        self.assertEqual(payload["thresholds"]["flag_context_tokens"], 500_000)
        top_band = len(health.CONTEXT_BANDS)  # "over the last edge"
        self.assertEqual(sum(r[5] for r in payload["context"] if r[3] == top_band), 1.0)

    def test_between_200k_and_500k_is_long_but_not_very_long(self):
        _, s = self._build(300_000)
        self.assertEqual(s["long_context_cost"], 1.0)
        self.assertEqual(s["very_long_context_cost"], 0.0)

    def test_no_main_spend_has_no_share(self):
        payload, sessions = health.build([("s", _facts())], [], set(), [], _bucket)
        self.assertIsNone(sessions["s"]["very_long_context_share"])


class TestMissCostLongPrompt(unittest.TestCase):
    """A cache miss is priced at the rates the request was billed at, and for
    a model priced by prompt length that depends on the prompt's size."""

    def test_long_prompt_miss_uses_the_long_prompt_rates(self):
        rec = {"ts": "2026-10-10T12:00:00Z", "model": "claude-haiku-5-5",
               "input_tokens": 0, "cache_write_5m": 150_000, "cache_missed_tokens": 150_000}
        extra, missed = health._miss_cost(rec)
        # 150k tokens at (0.625 - 0.05) per million, not the short (0.125 - 0.01).
        self.assertEqual(missed, 150_000)
        self.assertAlmostEqual(extra, 0.08625, places=9)

    def test_short_prompt_miss_keeps_the_base_rates(self):
        rec = {"ts": "2026-10-10T12:00:00Z", "model": "claude-haiku-5-5",
               "input_tokens": 0, "cache_write_5m": 50_000, "cache_missed_tokens": 50_000}
        self.assertAlmostEqual(health._miss_cost(rec)[0], 0.00575, places=9)


class TestMissCostWeightsTheWriteTiers(unittest.TestCase):
    """Writes split across the 5-minute and 1-hour tiers are priced at each
    tier's rate by its share, not all at whichever tier has more tokens."""

    def _extra(self, w5m, w1h):
        rec = {"ts": "2026-10-10T12:00:00Z", "model": "claude-opus-4-1", "input_tokens": 0,
               "cache_write_5m": w5m, "cache_write_1h": w1h}
        return health._miss_cost(rec)[0]

    def test_an_even_split_is_priced_at_the_mean_of_the_two_rates(self):
        # 100k at (18.75 - 1.5) + 100k at (30 - 1.5), per million.
        self.assertAlmostEqual(self._extra(100_000, 100_000), 4.575, places=9)

    def test_no_jump_where_the_larger_tier_changes(self):
        below, tie, above = (self._extra(100_001, 100_000), self._extra(100_000, 100_000),
                             self._extra(100_000, 100_001))
        self.assertLess(abs(above - tie), 0.001)
        self.assertLess(abs(tie - below), 0.001)

    def test_a_single_tier_keeps_its_own_rate(self):
        self.assertAlmostEqual(self._extra(100_000, 0), 1.725, places=9)
        self.assertAlmostEqual(self._extra(0, 100_000), 2.85, places=9)

    def test_claude_codes_missed_count_is_priced_at_the_blend(self):
        rec = {"ts": "2026-10-10T12:00:00Z", "model": "claude-opus-4-1", "input_tokens": 0,
               "cache_write_5m": 300_000, "cache_write_1h": 100_000, "cache_missed_tokens": 200_000}
        # Blend of 3:1 is 21.5625 per million; 200k of it over a 1.5 read.
        self.assertAlmostEqual(health._miss_cost(rec)[0], 4.0125, places=9)


if __name__ == "__main__":
    unittest.main()
