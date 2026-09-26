from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ralfloop_agent import storage_checker as sc


class StorageCheckerTests(unittest.TestCase):
    def test_classify_ok_warning_critical(self):
        t = sc.Thresholds(warn_percent=80, critical_percent=90, warn_free_gib=100, critical_free_gib=50)
        self.assertEqual(sc.classify(50, 500, t), "ok")
        self.assertEqual(sc.classify(85, 500, t), "warning")
        self.assertEqual(sc.classify(50, 75, t), "warning")
        self.assertEqual(sc.classify(95, 500, t), "critical")
        self.assertEqual(sc.classify(50, 40, t), "critical")

    def test_threshold_validation_rejects_bad_order(self):
        with self.assertRaises(ValueError):
            sc.Thresholds(warn_percent=95, critical_percent=90).validate()
        with self.assertRaises(ValueError):
            sc.Thresholds(warn_free_gib=10, critical_free_gib=20).validate()

    def test_parse_paths(self):
        self.assertEqual(sc.parse_paths(None), ["/"])
        self.assertEqual(sc.parse_paths(" /,/data ,, /srv "), ["/", "/data", "/srv"])

    def test_sample_path_and_report_trigger(self):
        total = 100 * sc.GIB
        used = 95 * sc.GIB
        free = 5 * sc.GIB
        with patch.object(sc.shutil, "disk_usage", return_value=SimpleNamespace(total=total, used=used, free=free)), patch.object(sc.Path, "resolve", lambda self: self):
            t = sc.Thresholds(warn_percent=80, critical_percent=90, warn_free_gib=20, critical_free_gib=10)
            report = sc.build_report(["/fake"], t)
        self.assertEqual(report["overall"], "critical")
        self.assertEqual(report["filesystems"][0]["used_percent"], 95.0)
        self.assertEqual(report["filesystems"][0]["free_gib"], 5.0)
        self.assertEqual(report["research_trigger"]["type"], "disk_purchase_research")
        self.assertEqual(report["research_trigger"]["paths"], ["/fake"])
        self.assertEqual(sc.exit_code(report), 2)

    def test_warning_does_not_trigger_purchase_research(self):
        total = 100 * sc.GIB
        used = 85 * sc.GIB
        free = 15 * sc.GIB
        with patch.object(sc.shutil, "disk_usage", return_value=SimpleNamespace(total=total, used=used, free=free)), patch.object(sc.Path, "resolve", lambda self: self):
            t = sc.Thresholds(warn_percent=80, critical_percent=90, warn_free_gib=20, critical_free_gib=10)
            report = sc.build_report(["/fake"], t)
        self.assertEqual(report["overall"], "warning")
        self.assertIsNone(report["research_trigger"])
        self.assertEqual(sc.exit_code(report), 1)

    def test_missing_path_is_critical_without_purchase_trigger(self):
        with patch.object(sc.shutil, "disk_usage", side_effect=FileNotFoundError("missing")):
            report = sc.build_report(["/missing"], sc.Thresholds())
        self.assertEqual(report["overall"], "critical")
        self.assertEqual(report["filesystems"], [])
        self.assertEqual(report["errors"][0]["path"], "/missing")
        self.assertIsNone(report["research_trigger"])
        self.assertEqual(sc.exit_code(report), 2)


if __name__ == "__main__":
    unittest.main()
