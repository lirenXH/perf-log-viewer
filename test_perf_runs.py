import tempfile
import unittest
import csv
import os
from pathlib import Path
from unittest.mock import patch

from perf_runs import (RunDirectory, load_checkpoint, paired_keys, read_summary, summary_rows,
                       compare_run_ipc, export_ipc_comparison, check_simulation_log, check_run_status,
                       filter_ipc_instrcount)


class RunDirectoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def checkpoint(self, run, benchmark, checkpoint, files):
        path = self.root / run / benchmark / checkpoint
        path.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (path / name).write_text(text, encoding="utf-8")
        return path

    def test_screenshot_layout_auxiliary_logs_multiple_files_and_numeric_order(self):
        path = self.checkpoint("current", "perlbench_diffmail", "10015", {
            "10015_20261011_062011_2281844.perf": "",
            "10015_20261012_062011_2281844.perf": "",
            "_10015_0.153974.log": "",
            "_10015_0.153974_gzip_check.log": "",
            "_10015_0.153974_cmd.txt": "",
        })
        self.checkpoint("current", "perlbench_diffmail", "180", {})
        self.checkpoint("current", "perlbench_diffmail", "not_a_checkpoint", {})
        run = RunDirectory.scan(self.root / "current")
        self.assertEqual(paired_keys(run), [("perlbench_diffmail", "180"), ("perlbench_diffmail", "10015")])
        entry = run.checkpoints["perlbench_diffmail", "10015"]
        self.assertEqual(entry.directory, path)
        self.assertEqual(len(entry.perf), 2)
        self.assertEqual([p.name for p in entry.logs], ["_10015_0.153974.log"])

    def test_pair_by_benchmark_and_checkpoint_without_using_timestamp_or_weight(self):
        for benchmark, checkpoint in (("perl", "180"), ("gcc", "180")):
            self.checkpoint("current", benchmark, checkpoint, {"current.perf": ""})
        for benchmark, checkpoint in (("perl", "180"), ("perl", "236")):
            self.checkpoint("baseline", benchmark, checkpoint, {"other_timestamp.perf": ""})
        keys = paired_keys(RunDirectory.scan(self.root / "current"), RunDirectory.scan(self.root / "baseline"))
        self.assertEqual(keys, [("gcc", "180"), ("perl", "180"), ("perl", "236")])

    def test_invalid_root_and_no_numeric_checkpoints(self):
        with self.assertRaises(ValueError):
            RunDirectory.scan(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "根目录"):
            RunDirectory.scan(self.root)

    def test_checkpoint_missing_one_side_and_counter_precision(self):
        path = self.checkpoint("baseline", "gcc", "1240", {
            "test.perf": "[PERF] TOP.L2: [L2] access, 18446744073709551615\n",
            "test.log": "instrCnt = 40,000,001, cycleCnt = 15,881,339, IPC = 2.518679\n",
        })
        current, baseline, comparison, summary = load_checkpoint(None, path / "test.perf", None, path / "test.log", True)
        for store in (current, baseline, comparison):
            self.addCleanup(store.close)
        rows = comparison.query({})["rows"]
        self.assertEqual(rows[0]["direction"], "缺失")
        self.assertEqual(rows[0]["baseline_value"], "18446744073709551615")
        self.assertEqual(summary[0]["direction"], "缺失")
        self.assertEqual(summary[0]["baseline_value"], "2.518679")
        self.assertEqual(current.info["records"], 0)

    def test_single_checkpoint_preserves_all_repeated_perf_records(self):
        path = self.checkpoint("current", "gcc", "1240", {
            "test.perf": "[PERF] [L2] access, 0\n[PERF] [L2] access, 5\n",
        })
        current, baseline, comparison, summary = load_checkpoint(path / "test.perf", None, None, None)
        for store in (current, baseline, comparison):
            self.addCleanup(store.close)
        self.assertEqual([row["value"] for row in current.query({})["rows"]], ["0", "5"])
        self.assertEqual({row["direction"] for row in summary}, {"未识别"})

    def compare(self):
        return compare_run_ipc(RunDirectory.scan(self.root / "current"), RunDirectory.scan(self.root / "baseline"))

    def test_simulation_status_keyword_precedence_ansi_and_unfinished_logs(self):
        cases = (("Core 0: \x1b[33mEXCEEDING CYCLE/INSTR LIMIT at pc=0x118cc\nIPC=0.895375", "PASS", 1),
                 ("IPC=1\n^[[33mEXCEEDING CYCLE/INSTR LIMIT\n", "PASS", 2),
                 ("HIT BAD TRAP\nIPC=1\n", "FAIL", 2),
                 ("IPC=0", "FAIL", 1), ("checkpoint restore!\n", "未判定", 0), ("", "未判定", 0))
        for index, (contents, result, line) in enumerate(cases):
            path = self.root / f"_{index}.log"
            path.write_text(contents)
            checked = check_simulation_log(path)
            self.assertEqual((checked["result"], checked["line"]), (result, line))
        self.assertEqual(check_simulation_log(self.root / "missing.log")["result"], "未判定")

    def test_run_status_checks_only_underscore_main_logs_and_retains_all_files(self):
        self.checkpoint("current", "perl", "180", {"_180_0.01.log": "INSTR LIMIT\nIPC=1",
            "_180_other.log": "IPC=1", "_180_gzip_check.log": "IPC=999", "not_sim.log": "IPC=999"})
        self.checkpoint("current", "gcc", "236", {"_236_0.02.log": "running"})
        self.checkpoint("current", "gcc", "999", {"_999_gzip_check.log": "INSTR LIMIT"})
        rows = check_run_status(RunDirectory.scan(self.root / "current"))
        self.assertEqual(len(rows), 4)
        self.assertEqual([row["result"] for row in rows], ["未判定", "未判定", "PASS", "FAIL"])
        self.assertIsNone(rows[1]["path"])
        self.assertTrue(all(row["path"] is None or row["path"].name.startswith("_") for row in rows))

    def test_assert_abort_without_ipc_is_fail_and_preserves_evidence(self):
        contents = ('checkpoint restore!\n'
                    '[7869467] %Error: L2TLB.sv:418: Assertion failed in TOP.core.mmu.l2TLB: '
                    'L2TLB miss request to PTW has been stall for 1024 cycle\n'
                    '  at l2tlb.scala:124 assert(!reqToPTWStallCounter.andR)\n'
                    '%Error: generated/core/L2TLB.sv:418: Verilog $stop\nAborting...\n')
        path = self.checkpoint("current", "omnetpp", "261", {"_261_0.001264.log": contents}) / "_261_0.001264.log"
        checked = check_simulation_log(path)
        self.assertEqual((checked["result"], checked["line"]), ("FAIL", 2))
        self.assertIn("Assertion failed", checked["reason"])
        self.assertIn("1024 cycle", checked["raw"])
        self.assertEqual(check_run_status(RunDirectory.scan(self.root / "current"))[0]["result"], "FAIL")

    def test_abort_markers_ansi_and_existing_limit_precedence(self):
        path = self.root / "_test.log"
        for marker in ("Assertion failed", "Verilog $stop", "Aborting..."):
            path.write_text(f"IPC=1\n\x1b[31m{marker}\x1b[0m\n")
            checked = check_simulation_log(path)
            self.assertEqual((checked["result"], checked["line"]), ("FAIL", 2))
            self.assertIn(marker, checked["reason"])
            path.write_text(f"{marker}\nEXCEEDING CYCLE/INSTR LIMIT\n")
            self.assertEqual(check_simulation_log(path)["result"], "PASS")
        path.write_text("assert checks enabled\nNo errors found\n")
        self.assertEqual(check_simulation_log(path)["result"], "未判定")

    def test_status_missing_and_unreadable_log_are_not_fail(self):
        directory = self.checkpoint("current", "perl", "1", {"_1.log": "IPC=1"})
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            checked = check_simulation_log(directory / "_1.log")
        self.assertEqual(checked["result"], "未判定")
        self.assertIn("读取失败", checked["reason"])

    def test_all_ipc_pairs_by_checkpoint_with_different_filenames_and_keeps_missing(self):
        self.checkpoint("current", "perl", "180", {"new_timestamp.log": "IPC=3\nIPC=2.5\n", "extra_gzip_check.log": "IPC=999"})
        self.checkpoint("baseline", "perl", "180", {"old_timestamp.log": "IPC=2"})
        self.checkpoint("current", "gcc", "180", {"main.log": "IPC=0"})
        self.checkpoint("baseline", "gcc", "180", {"main.log": "IPC=1"})
        self.checkpoint("current", "perl", "10015", {"main.log": "IPC=3"})
        self.checkpoint("baseline", "perl", "236", {"main.log": "IPC=1"})
        self.checkpoint("current", "perl", "999", {"main.log": "no summary"})
        self.checkpoint("baseline", "perl", "999", {"main.log": "IPC=1"})
        rows = self.compare()
        self.assertEqual(len(rows), 5)
        by_key = {(row["benchmark"], row["checkpoint"]): row for row in rows}
        self.assertEqual(by_key["perl", "180"]["percent"], "+25.00%")
        self.assertEqual(by_key["gcc", "180"]["percent"], "-100.00%")
        self.assertEqual(by_key["perl", "10015"]["direction"], "新增")
        self.assertEqual(by_key["perl", "236"]["direction"], "缺失")
        self.assertEqual(by_key["perl", "999"]["direction"], "无法比较")
        self.assertIn("未识别", by_key["perl", "999"]["note"])

    def test_all_ipc_keeps_every_main_log_pairs_multiple_by_filename_and_cores(self):
        self.checkpoint("current", "gcc", "1240", {
            "same.log": "Core 0: limit\nIPC=3\nCore 1: limit\nIPC=4\n",
            "only-current.log": "IPC=2\n",
        })
        self.checkpoint("baseline", "gcc", "1240", {"same.log": "Core 0: limit\nIPC=2\nCore 1: limit\nIPC=5\n"})
        rows = self.compare()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["direction"] for row in rows}, {"新增", "上升", "下降"})
        self.assertTrue(all("多份日志按文件名" in row["note"] for row in rows))
        self.assertEqual({row["core"] for row in rows if row["log"] == "same.log"}, {"Core 0", "Core 1"})

    def test_all_ipc_read_error_and_empty_checkpoint_do_not_abort_other_logs(self):
        self.checkpoint("current", "gcc", "1", {"bad.log": "IPC=2"})
        self.checkpoint("baseline", "gcc", "1", {"main.log": "IPC=1"})
        self.checkpoint("current", "gcc", "2", {})
        self.checkpoint("baseline", "gcc", "2", {})
        self.checkpoint("current", "gcc", "3", {"main.log": "IPC=2"})
        self.checkpoint("baseline", "gcc", "3", {"main.log": "IPC=2"})
        def read(path, encoding):
            if path and path.name == "bad.log":
                raise PermissionError("denied")
            return read_summary(path, encoding)
        with patch("perf_runs.read_summary", side_effect=read):
            rows = self.compare()
        self.assertEqual([row["direction"] for row in rows], ["无法比较", "无法比较", "不变"])
        self.assertIn("读取失败", rows[0]["note"])
        self.assertIn("缺少主日志", rows[1]["note"])

    def test_all_ipc_notes_derived_values_and_instruction_count_difference(self):
        self.checkpoint("current", "gcc", "1", {"main.log": "instrCnt=12, cycleCnt=4"})
        self.checkpoint("baseline", "gcc", "1", {"main.log": "instrCnt=10, cycleCnt=5, IPC=2"})
        row = self.compare()[0]
        self.assertEqual(row["percent"], "+50.00%")
        self.assertIn("由 instrcount/cycle 计算", row["note"])
        self.assertIn("instrcount 不同", row["note"])

    def test_instrcount_gate_inclusive_bounds_and_no_raw_row_mutation(self):
        self.checkpoint("current", "gcc", "1", {"main.log": "instrCnt=105, cycleCnt=10, IPC=3"})
        self.checkpoint("baseline", "gcc", "1", {"main.log": "instrCnt=95, cycleCnt=10, IPC=2"})
        original = self.compare()
        accepted, lower, upper = filter_ipc_instrcount(original, "100", "5")
        self.assertEqual((lower, upper), (95, 105))
        self.assertEqual(accepted[0]["percent"], "+50.00%")
        self.assertNotIn("instrcount", accepted[0]["note"])
        rejected, _, _ = filter_ipc_instrcount(original, "100", "4")
        self.assertEqual((rejected[0]["direction"], rejected[0]["delta"], rejected[0]["percent"]), ("无法比较", "—", "—"))
        self.assertIn("instrcount不同", rejected[0]["note"])
        self.assertIn("基准 95，当前 105", rejected[0]["note"])
        self.assertEqual(original[0]["percent"], "+50.00%")
        self.assertIn("两边 instrcount 不同", original[0]["note"])

    def test_instrcount_gate_identical_outside_counts_missing_counts_and_integer_precision(self):
        target = 18446744073709551614
        self.checkpoint("current", "gcc", "1", {"main.log": f"instrCnt={target + 1}, IPC=3"})
        self.checkpoint("baseline", "gcc", "1", {"main.log": f"instrCnt={target + 1}, IPC=2"})
        rows = self.compare()
        self.assertEqual(filter_ipc_instrcount(rows, f"{target:,}", "1")[0][0]["direction"], "上升")
        self.assertEqual(filter_ipc_instrcount(rows, str(target), "0")[0][0]["direction"], "无法比较")
        rows[0]["current_instr"] = None
        rejected = filter_ipc_instrcount(rows, str(target), "10")[0][0]
        self.assertIn("当前 缺失", rejected["note"])
        self.assertEqual(rejected["direction"], "无法比较")
        for a, b in (("", "1"), ("-1", "1"), ("100", "-1"), ("100", "1.5"), ("1,,00", "0")):
            with self.assertRaises(ValueError):
                filter_ipc_instrcount(rows, a, b)

    def test_ipc_csv_export_and_protect_all_source_files_and_hardlinks(self):
        source = self.checkpoint("current", "gcc", "1", {"main.log": "IPC=2"}) / "main.log"
        self.checkpoint("baseline", "gcc", "1", {"main.log": "IPC=1"})
        rows = self.compare()
        destination = self.root / "output.csv"
        self.assertEqual(export_ipc_comparison(rows, destination), 1)
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            values = list(csv.reader(stream))
        self.assertEqual(values[1][4:9], ["1", "2", "+1", "+100.00%", "上升"])
        alias = self.root / "alias.log"
        os.link(source, alias)
        for target in (source, alias):
            with self.assertRaises(ValueError):
                export_ipc_comparison(rows, target)
        with self.assertRaises(ValueError):
            export_ipc_comparison([], source, sources=(source,))
        self.assertEqual(source.read_text(), "IPC=2")


class SummaryTests(unittest.TestCase):
    def read(self, text, encoding="utf-8"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.log"
            path.write_bytes(text.encode(encoding))
            return read_summary(path, encoding)

    def test_real_screenshot_summary_ansi_visible_ansi_and_distinct_guest_cycles(self):
        for escape in ("\x1b[35m", "^[[35m"):
            values = self.read(
                "Core 0: EXCEEDING CYCLE/INSTR LIMIT at pc = 0x453b8\n" +
                escape + "total guest instructions = 40,000,001\n" +
                escape + "instrCnt = 40,000,001, cycleCnt = 15,881,339, IPC = 2.518679\n" +
                "Seed=0 Guest cycle spent: 15,881,341 (this will be different from cycleCnt if emu loads a snapshot)\n" +
                "Host time spent: 12,670,505ms\n")
            self.assertEqual(values["Core 0", "instrcount"].value, "40000001")
            self.assertEqual(values["Core 0", "cycle"].value, "15881339")
            self.assertEqual(values["Core 0", "IPC"].value, "2.518679")
            self.assertEqual(values["Core 0", "Guest cycle spent"].value, "15881341")
            self.assertEqual(values["Core 0", "Host time spent (ms)"].value, "12670505")
            self.assertEqual(values["Core 0", "cycle"].line, 3)

    def test_last_summary_per_core_ignore_perf_and_no_cross_core_sums(self):
        values = self.read("Core 0: limit\ninstrCnt = 10, cycleCnt = 5, IPC = 2\n"
                           "[PERF] [ROB] instrcount = 999, cycleCnt = 999, IPC = 999\n"
                           "instrCnt = 15, cycleCnt = 5, IPC = 3\n"
                           "Core 1: limit\ninstrCnt = 20, cycleCnt = 10, IPC = 2\n")
        self.assertEqual(values["Core 0", "IPC"].value, "3")
        self.assertEqual(values["Core 1", "instrcount"].value, "20")
        self.assertEqual(len(summary_rows(values, {}, False)), 6)

    def test_aliases_gbk_zero_derivation_and_relative_change(self):
        values = self.read("说明\ninstrcount: 12\ncycle: 4\n", "gb18030")
        self.assertEqual(values["", "IPC"].value, "3")
        self.assertTrue(values["", "IPC"].derived)
        baseline = self.read("instr_count=10\ncycle_count=5\nIPC=2\n")
        rows = summary_rows(values, baseline, True)
        self.assertEqual((rows[0]["delta"], rows[0]["percent"]), ("+1", "+50.00%"))
        self.assertNotIn(("", "IPC"), self.read("instrCnt=10, cycleCnt=0\n"))

    def test_full_uint64_and_thousands_spaces_do_not_capture_neighbor_fields(self):
        values = self.read("instrCnt = 18,446,744,073,709,551,615, cycleCnt = 15 881 339, IPC = 2.5e0\n")
        self.assertEqual(values["", "instrcount"].value, "18446744073709551615")
        self.assertEqual(values["", "cycle"].value, "15881339")
        self.assertEqual(values["", "IPC"].value, "2.5e0")


if __name__ == "__main__":
    unittest.main()
