import json
import os
import shutil
import tempfile
import unittest

from claude_usage.cache import FileCache
from claude_usage.scan import scan

FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "projects")


class TestFileCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "parsed.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_warm_scan_reparses_nothing(self):
        cache1 = FileCache(self.cache_path)
        payload1 = scan(FIXTURES_ROOT, cache=cache1)
        self.assertGreater(payload1["files_parsed"], 0)

        cache2 = FileCache(self.cache_path)
        payload2 = scan(FIXTURES_ROOT, cache=cache2)
        self.assertEqual(payload2["files_parsed"], 0)
        self.assertEqual(payload2["files_cached"], payload1["files_parsed"] + payload1["files_cached"])

    def test_changing_one_file_reparses_only_it(self):
        cache1 = FileCache(self.cache_path)
        scan(FIXTURES_ROOT, cache=cache1)

        target = os.path.join(FIXTURES_ROOT, "-Users-x-proj1", "sess-main-2.jsonl")
        st = os.stat(target)
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

        cache2 = FileCache(self.cache_path)
        payload2 = scan(FIXTURES_ROOT, cache=cache2)
        self.assertEqual(payload2["files_parsed"], 1)

        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime for other tests

    def test_deleted_file_dropped_from_cache(self):
        cache1 = FileCache(self.cache_path)
        scan(FIXTURES_ROOT, cache=cache1)

        other_root = os.path.join(self.tmpdir, "projects")
        shutil.copytree(FIXTURES_ROOT, other_root)
        cache2 = FileCache(self.cache_path)
        scan(other_root, cache=cache2)

        removed = os.path.join(other_root, "-Users-x-proj1", "sess-main-2.jsonl")
        os.remove(removed)

        cache3 = FileCache(self.cache_path)
        scan(other_root, cache=cache3)
        self.assertNotIn(removed, cache3._store)

    def test_force_reparses_everything(self):
        cache1 = FileCache(self.cache_path)
        payload1 = scan(FIXTURES_ROOT, cache=cache1)
        total_files = payload1["files_parsed"] + payload1["files_cached"]

        cache2 = FileCache(self.cache_path)
        payload2 = scan(FIXTURES_ROOT, cache=cache2, force=True)
        self.assertEqual(payload2["files_parsed"], total_files)
        self.assertEqual(payload2["files_cached"], 0)


if __name__ == "__main__":
    unittest.main()


class TestCacheFormat(unittest.TestCase):
    """The cache is plain JSON, not a pickle: a writable cache file should never
    be able to execute code on the next scan."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "parsed.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cache_file_is_json(self):
        scan(FIXTURES_ROOT, cache=FileCache(self.cache_path))
        with open(self.cache_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertIn("entries", raw)
        self.assertIn("facts_version", raw)

    def test_round_trip_restores_counters(self):
        scan(FIXTURES_ROOT, cache=FileCache(self.cache_path))
        reloaded = FileCache(self.cache_path)
        facts = [v[2] for v in reloaded._store.values()]
        self.assertTrue(facts)
        entrypoints = [f for f in facts if f.entrypoints]
        self.assertTrue(entrypoints, "no file had entrypoints to restore")
        # Counter behaviour has to survive the round trip, not just dict equality.
        self.assertEqual(entrypoints[0].entrypoints.most_common(1)[0][1] > 0, True)
        self.assertEqual(entrypoints[0].entrypoints["does-not-exist"], 0)

    def test_version_bump_invalidates_the_cache(self):
        scan(FIXTURES_ROOT, cache=FileCache(self.cache_path))
        with open(self.cache_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["facts_version"] = -1
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        payload = scan(FIXTURES_ROOT, cache=FileCache(self.cache_path))
        self.assertEqual(payload["files_cached"], 0)
        self.assertGreater(payload["files_parsed"], 0)

    def test_corrupt_cache_falls_back_to_a_cold_scan(self):
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        payload = scan(FIXTURES_ROOT, cache=FileCache(self.cache_path))
        self.assertGreater(payload["files_parsed"], 0)

    def test_pickle_payload_is_not_unpickled(self):
        import pickle
        with open(self.cache_path, "wb") as fh:
            pickle.dump({"anything": "at all"}, fh)
        # Must be ignored as unreadable rather than deserialized.
        payload = scan(FIXTURES_ROOT, cache=FileCache(self.cache_path))
        self.assertGreater(payload["files_parsed"], 0)


class TestConcurrentSaves(unittest.TestCase):
    """serve.py and collect.py share .cache/parsed.json. Two saves at once used
    to write the same "parsed.json.tmp": the first rename took it, and the
    second raised FileNotFoundError out of the scan."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "parsed.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _entry(self, cache, name):
        from claude_usage.parser import FileFacts
        cache.put("/x/" + name, 1, 2, FileFacts(path="/x/" + name, kind="main"))

    def test_a_save_interleaved_with_another_does_not_raise(self):
        from unittest import mock
        first, second = FileCache(self.cache_path), FileCache(self.cache_path)
        self._entry(first, "a.jsonl")
        self._entry(second, "b.jsonl")
        real_replace = os.replace

        def replace_after_the_other_save(src, dst):
            # The other process saves between this one's write and its rename.
            with mock.patch("os.replace", real_replace):
                second.save()
            real_replace(src, dst)

        with mock.patch("os.replace", replace_after_the_other_save):
            first.save()
        self.assertIsNotNone(FileCache(self.cache_path).get("/x/a.jsonl", 1, 2))
        self.assertEqual(os.listdir(self.tmpdir), ["parsed.json"])

    def test_a_failed_save_is_not_fatal_and_leaves_no_temp_file(self):
        from unittest import mock
        cache = FileCache(self.cache_path)
        self._entry(cache, "a.jsonl")
        with mock.patch("os.replace", side_effect=PermissionError("locked")):
            cache.save()  # must not raise: the scan's result is still good
        self.assertEqual(os.listdir(self.tmpdir), [])


@unittest.skipIf(os.name == "nt", "POSIX permission bits")
@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads any file")
class TestUnreadableFileIsNotCached(unittest.TestCase):
    """A transcript that can't be opened (permissions, a lock) used to come back
    as empty facts and be stored under its valid mtime/size. Fixing the
    permissions changes neither, so the file read as 0 requests until it was
    edited or --force was used."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "parsed.json")
        self.root = os.path.join(self.tmpdir, "projects")
        shutil.copytree(FIXTURES_ROOT, self.root)
        self.folder = os.path.join(self.root, "-Users-x-proj1")
        self.target = os.path.join(self.folder, "sess-main-2.jsonl")
        self.locked = []

    def tearDown(self):
        for path in self.locked:
            os.chmod(path, 0o644)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _lock(self, path):
        os.chmod(path, 0o000)
        self.locked.append(path)

    def _unlock(self, path):
        os.chmod(path, 0o644)

    def _requests(self, payload):
        return sum(row["requests"] for row in payload["sessions"])

    def test_the_file_is_read_once_it_can_be_opened(self):
        cache = FileCache(self.cache_path)
        full = self._requests(scan(self.root, cache=FileCache(os.path.join(self.tmpdir, "ref.json"))))

        self._lock(self.target)
        before = scan(self.root, cache=cache)
        self.assertLess(self._requests(before), full)
        self.assertFalse(self.target in cache._store)

        self._unlock(self.target)
        after = scan(self.root, cache=cache)  # same cache, same mtime and size
        self.assertEqual(self._requests(after), full)

    def test_a_cache_reloaded_from_disk_does_not_remember_it_as_empty(self):
        full = self._requests(scan(self.root, cache=FileCache(os.path.join(self.tmpdir, "ref.json"))))
        self._lock(self.target)
        scan(self.root, cache=FileCache(self.cache_path))
        self._unlock(self.target)
        after = scan(self.root, cache=FileCache(self.cache_path))
        self.assertEqual(self._requests(after), full)

    def test_an_unreadable_file_is_left_out_and_not_counted_as_parsed(self):
        reference = scan(self.root, cache=FileCache(os.path.join(self.tmpdir, "ref.json")))
        self._lock(self.target)
        payload = scan(self.root, cache=FileCache(self.cache_path))
        self.assertEqual(payload["files_parsed"], reference["files_parsed"] - 1)
        self.assertEqual(len(payload["sessions"]), len(reference["sessions"]) - 1)

    def test_an_unreadable_subagent_file_is_not_cached_either(self):
        subs = [os.path.join(dp, f) for dp, _, fs in os.walk(self.root) for f in fs
                if f.endswith(".jsonl") and "subagents" in dp]
        self.assertTrue(subs, "fixtures have no subagent transcript")
        sub = subs[0]
        cache = FileCache(self.cache_path)
        self._lock(sub)
        scan(self.root, cache=cache)
        self.assertFalse(sub in cache._store)

    def test_an_entry_cached_while_readable_survives_a_later_lock(self):
        cache = FileCache(self.cache_path)
        full = self._requests(scan(self.root, cache=cache))
        self._lock(self.target)
        # Unchanged mtime/size: the cached facts are still right, no re-read.
        self.assertEqual(self._requests(scan(self.root, cache=cache)), full)

    def test_force_on_an_unreadable_file_drops_the_old_entry(self):
        cache = FileCache(self.cache_path)
        scan(self.root, cache=cache)
        self._lock(self.target)
        scan(self.root, cache=cache, force=True)
        self.assertFalse(self.target in cache._store)
