import tempfile
import unittest
from pathlib import Path

from src.rotating_log import RotatingTextLog, append_rotating_text


class RotatingTextLogTests(unittest.TestCase):
    def test_log_rotation_keeps_only_the_configured_number_of_backups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "collector.log"
            writer = RotatingTextLog(path, max_bytes=8, backup_count=2)
            try:
                writer.write("first\n")
                writer.write("second\n")
                writer.write("third\n")
                writer.write("fourth\n")
            finally:
                writer.close()

            self.assertTrue(path.exists())
            self.assertTrue(path.with_name("collector.log.1").exists())
            self.assertTrue(path.with_name("collector.log.2").exists())
            self.assertFalse(path.with_name("collector.log.3").exists())

    def test_zero_size_disables_rotation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "watcher.log"
            writer = RotatingTextLog(path, max_bytes=0, backup_count=3)
            try:
                writer.write("one\n")
                writer.write("two\n")
            finally:
                writer.close()

            self.assertEqual(path.read_text(encoding="utf-8"), "one\ntwo\n")
            self.assertFalse(path.with_name("watcher.log.1").exists())

    def test_append_helper_releases_the_file_handle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "watcher.log"
            append_rotating_text(path, "one\n", max_bytes=20)
            append_rotating_text(path, "two\n", max_bytes=20)
            self.assertEqual(path.read_text(encoding="utf-8"), "one\ntwo\n")


if __name__ == "__main__":
    unittest.main()
