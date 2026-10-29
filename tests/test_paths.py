"""The data folder: where the tool keeps the files it makes, outside the code,
so a new clone, zip or plugin update keeps the same machine id."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_usage import context, paths, price_check, scan

REPO = Path(__file__).resolve().parent.parent


class TestDataDir(unittest.TestCase):
    def test_default_is_in_the_home_folder(self):
        with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: ""}):
            self.assertEqual(paths.data_dir(), Path(os.path.expanduser("~/.usage-lens")))

    def test_the_variable_moves_it(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: tmp}):
            self.assertEqual(paths.data_dir(), Path(tmp))
            self.assertEqual(paths.cache_dir(), Path(tmp) / "cache")

    def test_a_relative_variable_is_made_absolute(self):
        with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: "rel/lens"}):
            self.assertEqual(paths.data_dir(), Path(os.path.abspath("rel/lens")))

    def test_set_data_dir_makes_the_path_absolute(self):
        with mock.patch.dict(os.environ, {}):
            paths.set_data_dir("relative/place")
            self.assertEqual(paths.data_dir(), Path(os.path.abspath("relative/place")))

    def test_never_beside_the_code(self):
        with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: ""}):
            self.assertNotIn(str(REPO), str(paths.data_dir()))

    def test_caches_follow_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: tmp}), \
                mock.patch.object(scan, "DEFAULT_CACHE_PATH", None), \
                mock.patch.object(price_check, "CACHE_FILE", None):
            self.assertEqual(scan.default_cache().path, str(Path(tmp) / "cache" / "parsed.json"))
            self.assertEqual(price_check._cache_file(), Path(tmp) / "cache" / "price-check.json")


class TestMachineIdLocation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.legacy = Path(self.tmp.name) / "old-copy" / ".cache" / "machine-id"
        patches = [mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: str(self.data)}),
                   mock.patch.object(context, "MACHINE_ID_FILE", None),
                   mock.patch.object(context, "LEGACY_MACHINE_ID_FILE", str(self.legacy))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_made_in_the_data_folder(self):
        mid = context.machine_id()
        self.assertRegex(mid, r"^[0-9a-f]{12}$")
        self.assertEqual((self.data / "machine-id").read_text(encoding="utf-8").strip(), mid)

    def test_an_older_copys_id_is_kept(self):
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text("0123456789ab\n", encoding="utf-8")
        self.assertEqual(context.machine_id(), "0123456789ab")
        self.assertEqual((self.data / "machine-id").read_text(encoding="utf-8").strip(), "0123456789ab")
        # Renamed, so it can't come back once the data folder's id is deleted.
        self.assertFalse(self.legacy.exists())
        self.assertTrue(self.legacy.with_name("machine-id.moved").exists())

    def test_carried_over_once_only(self):
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text("0123456789ab\n", encoding="utf-8")
        context.adopt_legacy_machine_id()
        (self.data / "machine-id").unlink()  # the copied-folder remedy
        mid = context.machine_id()
        self.assertNotEqual(mid, "0123456789ab")

    def test_carried_over_at_startup_not_only_on_export(self):
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text("0123456789ab\n", encoding="utf-8")
        context.adopt_legacy_machine_id()
        self.assertEqual((self.data / "machine-id").read_text(encoding="utf-8").strip(), "0123456789ab")

    def test_no_old_copy_makes_nothing_at_startup(self):
        context.adopt_legacy_machine_id()
        self.assertFalse((self.data / "machine-id").exists())

    def test_an_empty_file_is_waited_for_not_overwritten(self):
        # Another process has opened the file and is about to write its id.
        self.data.mkdir()
        (self.data / "machine-id").write_text("", encoding="utf-8")
        import threading
        threading.Timer(0.2, lambda: (self.data / "machine-id").write_text(
            "bbbbbbbbbbbb\n", encoding="utf-8")).start()
        self.assertEqual(context.machine_id(), "bbbbbbbbbbbb")

    def test_a_damaged_file_is_replaced(self):
        self.data.mkdir()
        (self.data / "machine-id").write_text("garbage\n", encoding="utf-8")
        mid = context.machine_id()
        self.assertRegex(mid, r"^[0-9a-f]{12}$")
        self.assertEqual((self.data / "machine-id").read_text(encoding="utf-8").strip(), mid)
        self.assertEqual([f.name for f in self.data.iterdir()], ["machine-id"])  # no temp left

    def test_the_data_folder_wins_over_an_older_copy(self):
        self.data.mkdir()
        (self.data / "machine-id").write_text("aaaaaaaaaaaa\n", encoding="utf-8")
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text("0123456789ab\n", encoding="utf-8")
        self.assertEqual(context.machine_id(), "aaaaaaaaaaaa")

    def test_a_damaged_old_id_is_not_carried_over(self):
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text("not an id\n", encoding="utf-8")
        mid = context.machine_id()
        self.assertRegex(mid, r"^[0-9a-f]{12}$")
        self.assertNotEqual(mid, "not an id")


class TestDataDirFlag(unittest.TestCase):
    """--data-dir on collect.py: everything it writes lands there, and nothing
    lands beside the code."""

    def test_collect_writes_only_to_the_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "lens"
            home = Path(tmp) / "home"
            (home / ".claude" / "projects").mkdir(parents=True)
            env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
                   price_check.OFFLINE_ENV: "1"}
            env.pop(paths.DATA_DIR_ENV, None)
            before = set(os.listdir(REPO))
            out = subprocess.run([sys.executable, str(REPO / "collect.py"), "--static",
                                  "--data-dir", str(data)],
                                 cwd=tmp, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue((data / "data.json").exists())
            self.assertTrue((data / "dashboard.html").exists())
            self.assertTrue((data / "cache" / "parsed.json").exists())
            self.assertFalse((home / ".usage-lens").exists())
            self.assertEqual(set(os.listdir(REPO)) - before, set())


if __name__ == "__main__":
    unittest.main()
