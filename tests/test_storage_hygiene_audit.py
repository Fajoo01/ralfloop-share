import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools" / "storage_hygiene_audit.c"


class StorageHygieneAuditTests(unittest.TestCase):
    def build(self, out: Path) -> None:
        subprocess.run(
            ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(SOURCE), "-o", str(out)],
            check=True,
        )

    def test_reports_old_large_tmp_without_deleting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "audit"
            self.build(binary)

            tmp_root = root / "tmp"
            tmp_root.mkdir()
            old = tmp_root / "old-build"
            old.mkdir()
            payload = old / "payload.bin"
            payload.write_bytes(b"x" * (2 * 1024 * 1024))
            old_time = time.time() - (4 * 86400)
            os.utime(old, (old_time, old_time))

            recent = tmp_root / "recent-build"
            recent.mkdir()
            (recent / "payload.bin").write_bytes(b"x" * (2 * 1024 * 1024))

            cache = root / "cache"
            cache.mkdir()
            (cache / "wheel.bin").write_bytes(b"x" * 4096)

            proc = subprocess.run(
                [
                    str(binary),
                    "--tmp", str(tmp_root),
                    "--age-days", "2",
                    "--min-mib", "1",
                    "--path", f"pip={cache}",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            report = json.loads(proc.stdout)

            self.assertFalse(report["deletes_files"])
            self.assertEqual(report["candidate_count"], 1)
            self.assertEqual(Path(report["tmp_candidates"][0]["path"]), old)
            self.assertGreater(report["candidate_bytes"], 1024 * 1024)
            self.assertEqual(report["watched_paths"][0]["label"], "pip")
            self.assertTrue(report["watched_paths"][0]["exists"])
            self.assertTrue(payload.exists())
            self.assertTrue((recent / "payload.bin").exists())

    def test_missing_watched_path_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "audit"
            self.build(binary)
            tmp_root = root / "tmp"
            tmp_root.mkdir()

            proc = subprocess.run(
                [
                    str(binary),
                    "--tmp", str(tmp_root),
                    "--path", f"missing={root / 'missing'}",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            report = json.loads(proc.stdout)
            self.assertEqual(report["candidate_count"], 0)
            self.assertFalse(report["watched_paths"][0]["exists"])


if __name__ == "__main__":
    unittest.main()
