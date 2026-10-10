import csv
import io
import os
import tempfile
import unittest
from pathlib import Path

from perf_compare import ComparisonStore, compare_values, export_comparison
from perf_viewer import LogStore


class ValueComparisonTests(unittest.TestCase):
    def test_increase_decrease_zero_and_negative_baselines(self):
        self.assertEqual(compare_values("120", "100"), ("+20", "+20.00%", "上升"))
        self.assertEqual(compare_values("80", "100"), ("-20", "-20.00%", "下降"))
        self.assertEqual(compare_values("0", "0"), ("0", "—（基准为0）", "不变"))
        self.assertEqual(compare_values("5", "0"), ("+5", "—（基准为0）", "上升"))
        self.assertEqual(compare_values("-5", "-10"), ("+5", "+50.00%", "上升"))

    def test_uint64_precision_numeric_formats_and_units(self):
        self.assertEqual(compare_values("18446744073709551615", "18446744073709551614")[0], "+1")
        self.assertEqual(compare_values("0x20", "0x10"), ("+16", "+100.00%", "上升"))
        self.assertEqual(compare_values("1,200", "1e3"), ("+200", "+20.00%", "上升"))
        self.assertEqual(compare_values("12.5%", "10%"), ("+2.5 个百分点", "+25.00%", "上升"))
        self.assertEqual(compare_values("12.5%", "10"), ("—", "—", "单位不同"))
        self.assertEqual(compare_values("unparsed", "10"), ("—", "—", "非数值"))


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.current, self.baseline, self.comparison = LogStore(), LogStore(), ComparisonStore()
        for store in (self.current, self.baseline, self.comparison):
            self.addCleanup(store.close)

    def load(self, store, text):
        data = text.encode()
        store.load(io.BytesIO(data), len(data), "test.log")

    def test_union_exact_instance_matching_and_missing_numeric_records(self):
        self.load(self.current, "[PERF] TOP.bank0: [L2] access, 120\n[PERF] TOP.bank1: [L2] hit, 3\n[PERF] [L2] only_current, 0\n[PERF] [L2] unsupported\n")
        self.load(self.baseline, "[PERF] TOP.bank0: [L2] access, 100\n[PERF] TOP.bank0: [L2] hit, 9\n[PERF] [L2] only_baseline, 7\n[PERF] [L2] unsupported\n")
        info = self.comparison.build(self.current, self.baseline)
        rows = self.comparison.query({})["rows"]
        self.assertEqual(info["records"], 6)
        self.assertEqual(info["directions"], {"上升": 1, "新增": 2, "缺失": 2, "非数值": 1})
        access = next(row for row in rows if row["metric"] == "access")
        self.assertEqual((access["delta"], access["percent"]), ("+20", "+20.00%"))
        self.assertEqual({row["direction"] for row in rows if row["metric"] == "hit"}, {"新增", "缺失"})

    def test_occurrences_pair_without_sum_and_last_mode(self):
        self.load(self.current, "[PERF] [L2] access, 15\n[PERF] [L2] access, 30\n[PERF] [L2] access, 35\n")
        self.load(self.baseline, "[PERF] [L2] access, 10\n[PERF] [L2] access, 20\n")
        self.comparison.build(self.current, self.baseline)
        rows = self.comparison.query({})["rows"]
        self.assertEqual([(row["occurrence"], row["delta"], row["direction"]) for row in rows],
                         [(1, "+5", "上升"), (2, "+10", "上升"), (3, "—", "新增")])
        self.comparison.build(self.current, self.baseline, "last")
        rows = self.comparison.query({})["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["baseline_value"], rows[0]["value"], rows[0]["delta"], rows[0]["percent"]),
                         ("20", "35", "+15", "+75.00%"))

    def test_filters_search_both_values_and_current_line_range(self):
        self.load(self.current, "[PERF] TOP.bank0: [L2] Useful prefetch, 12\n[PERF] [ROB] commitInstr, 100\n")
        self.load(self.baseline, "[PERF] TOP.bank0: [L2] Useful prefetch, 8\n[PERF] [ROB] commitInstr, 100\n[PERF] [L2] only_baseline, 99\n")
        self.comparison.build(self.current, self.baseline)
        rows = self.comparison.query({"module": ["L2"], "q": ['"USEFUL PREFETCH" 8'], "path": ["BANK0"], "direction": ["上升"]})["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delta"], "+4")
        self.assertEqual(self.comparison.query({"direction": ["有变化"]})["total"], 2)
        self.assertEqual(self.comparison.query({"start": ["1"], "end": ["2"]})["total"], 2)
        self.assertEqual(self.comparison.query({"q": ["%' OR 1=1"]})["total"], 0)

    def test_batched_import_pagination_and_full_comparison_export(self):
        self.load(self.current, "".join(f"[PERF] [L2] counter_{i}, {i + 1}\n" for i in range(2100)))
        self.load(self.baseline, "".join(f"[PERF] [L2] counter_{i}, {i}\n" for i in range(2100)))
        self.comparison.build(self.current, self.baseline)
        result = self.comparison.query({"limit": ["100"], "page": ["2"]})
        self.assertEqual((result["total"], result["pages"], len(result["rows"])), (2100, 21, 100))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "compare.csv"
            self.assertEqual(export_comparison(self.comparison, {"limit": ["1"]}, destination), 2100)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(len(rows), 2101)
            self.assertEqual(rows[1][4:9], ["0", "1", "+1", "—（基准为0）", "上升"])

    def test_export_protects_both_source_logs_and_hardlinks(self):
        self.load(self.current, "[PERF] [L2] access, 10\n")
        self.load(self.baseline, "[PERF] [L2] access, 20\n")
        self.comparison.build(self.current, self.baseline)
        with tempfile.TemporaryDirectory() as directory:
            current, baseline, alias = (Path(directory) / name for name in ("current.log", "baseline.log", "alias.log"))
            current.write_text("current source")
            baseline.write_text("baseline source")
            os.link(baseline, alias)
            for destination in (current, baseline, alias):
                with self.assertRaises(ValueError):
                    export_comparison(self.comparison, {}, destination, (current, baseline))
            self.assertEqual(current.read_text(), "current source")
            self.assertEqual(baseline.read_text(), "baseline source")


if __name__ == "__main__":
    unittest.main()
