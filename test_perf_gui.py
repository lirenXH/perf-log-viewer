"""Opt-in GUI integration tests: PERF_VIEWER_GUI_TEST=1 python3 -m unittest test_perf_gui."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(os.environ.get("PERF_VIEWER_GUI_TEST") == "1", "需要显式启用 GUI 测试和可用 DISPLAY")
class DesktopTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from perf_viewer_gui import PerfViewerApp
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = PerfViewerApp(self.root)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.app.close)
        self.errors = []
        errors = patch("perf_viewer_gui.messagebox.showerror", side_effect=lambda *args, **kwargs: self.errors.append(args))
        errors.start()
        self.addCleanup(errors.stop)
        sockets = patch("socket.socket", side_effect=AssertionError("桌面 GUI 不应使用网络"))
        sockets.start()
        self.addCleanup(sockets.stop)
        self.path = Path(self.directory.name)
        self.make_checkpoint("current", "perl", "10015", 120, "2.5")
        self.make_checkpoint("baseline", "perl", "10015", 100, "2")
        self.make_checkpoint("current", "perl", "180", 10, "1")
        self.make_checkpoint("baseline", "perl", "236", 30, "1.5")
        self.make_checkpoint("current", "gcc", "180", 77, "3")

    def make_checkpoint(self, run, benchmark, checkpoint, access, ipc):
        path = self.path / run / benchmark / checkpoint
        path.mkdir(parents=True)
        (path / f"{checkpoint}_{run}_timestamp.perf").write_text(
            f"[PERF] TOP.L2: [L2] access, {access}\n[PERF] TOP.L2: [L2] hit, 0\n", encoding="utf-8")
        (path / f"_{checkpoint}_0.153974.log").write_text(
            f"Core 0: limit\ninstrCnt = 40,000,001, cycleCnt = 15,881,339, IPC = {ipc}\n", encoding="utf-8")
        (path / f"_{checkpoint}_0.153974_gzip_check.log").write_text("IPC = 999", encoding="utf-8")
        return path

    def wait(self, condition, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(0.01)
        self.fail("GUI 等待超时：" + self.app.status.get() + repr(self.errors))

    def load_directory(self, run, baseline=False):
        previous = self.app.store
        self.app.open_directory(self.path / run, baseline)
        self.wait(lambda: not self.app.loading and self.app.store is not previous and len(self.app.rows) == 2)

    def select(self, benchmark, checkpoint):
        previous = self.app.store
        self.app.benchmark.set(benchmark)
        self.app.checkpoint.set(checkpoint)
        self.app.select_benchmark()
        self.wait(lambda: not self.app.loading and self.app.store is not previous and len(self.app.rows) == 2)

    def test_directory_pairing_numeric_navigation_missing_checkpoints_and_summary_sources(self):
        self.load_directory("current")
        self.assertEqual(self.app.benchmark.get(), "gcc")
        self.assertEqual(self.app.summary_items["summary-0"]["value"], "3")
        self.load_directory("baseline", True)
        self.select("perl", "10015")
        access = next(row for row in self.app.rows.values() if row["metric"] == "access")
        self.assertEqual(access["delta"], "+20")
        summary = self.app.summary_items["summary-0"]
        self.assertEqual((summary["value"], summary["baseline_value"], summary["percent"]), ("2.5", "2", "+25.00%"))
        self.assertEqual(self.app.checkpoint_combo["values"], ("180", "236", "10015"))
        self.assertNotIn("gzip_check", self.app.run_files["current_log"].get())
        self.app.summary_table.selection_set("summary-0")
        self.app.show_summary_original()
        self.assertGreater(len(self.root.winfo_children()), 1)
        self.select("perl", "236")
        self.assertEqual({row["direction"] for row in self.app.rows.values()}, {"缺失"})
        self.assertEqual(self.app.summary_items["summary-0"]["direction"], "缺失")
        self.select("perl", "180")
        self.assertEqual({row["direction"] for row in self.app.rows.values()}, {"新增"})
        previous = self.app.store
        self.app.remove_baseline()
        self.wait(lambda: not self.app.loading and self.app.store is not previous and len(self.app.rows) == 2)
        self.assertIsNone(self.app.baseline_run)
        self.assertIs(self.app.active_store, self.app.store)
        self.assertFalse(self.errors)

    def test_multiple_perf_selection_and_single_file_mode_still_work(self):
        directory = self.path / "current" / "gcc" / "180"
        older = directory / "180_aaa.perf"
        older.write_text("[PERF] [L2] access, 12\n[PERF] [L2] hit, 3\n")
        self.load_directory("current")
        self.assertIn("请确认选择", self.app.checkpoint_presence.get())
        previous = self.app.store
        self.app.run_files["current_perf"].set(older.name)
        self.app.reload_checkpoint()
        self.wait(lambda: not self.app.loading and self.app.store is not previous and len(self.app.rows) == 2)
        self.assertEqual(next(row for row in self.app.rows.values() if row["metric"] == "access")["value"], "12")
        self.app.load_file(older)
        self.wait(lambda: not self.app.loading and not self.app.run_mode and len(self.app.rows) == 2)
        self.app.load_file(older, True)
        self.wait(lambda: not self.app.loading and self.app.active_store is self.app.comparison and len(self.app.rows) == 2)
        self.assertEqual({row["delta"] for row in self.app.rows.values()}, {"0"})
        self.assertFalse(self.errors)

    def test_bad_directory_preserves_loaded_results_and_export_protects_main_log(self):
        self.load_directory("current")
        previous = self.app.store
        self.app.open_directory(self.path / "missing")
        self.wait(lambda: not self.app.loading)
        self.assertIs(self.app.store, previous)
        self.assertEqual(len(self.errors), 1)
        log = self.app.summary_sources["current_log"]
        before = log.read_bytes()
        with patch("perf_viewer_gui.filedialog.asksaveasfilename", return_value=str(log)):
            self.app.export("csv")
        self.assertEqual(len(self.errors), 2)
        self.assertEqual(log.read_bytes(), before)
        destination = self.path / "output.csv"
        with patch("perf_viewer_gui.filedialog.asksaveasfilename", return_value=str(destination)):
            self.app.export("csv")
        self.wait(lambda: destination.exists() and not self.app.exporting)
        self.assertIn("access", destination.read_text(encoding="utf-8-sig"))

    def test_bulk_ipc_overview_filter_sort_export_refresh_and_sources(self):
        self.make_checkpoint("current", "perl", "200", 20, "1.5")
        self.make_checkpoint("baseline", "perl", "200", 20, "2")
        self.load_directory("current")
        self.load_directory("baseline", True)
        view = self.app.open_ipc_overview()
        self.wait(lambda: not view.loading and len(view.rows) == 5)
        self.assertEqual({row["direction"] for row in view.rows}, {"新增", "缺失", "上升", "下降"})
        view.direction.set("下降")
        self.assertEqual(len(view.visible), 1)
        self.assertEqual(view.visible[0]["percent"], "-25.00%")
        view.direction.set("全部")
        view.query.set("perl")
        self.assertEqual(len(view.visible), 4)
        view.sort_by("percent")
        self.assertEqual(view.visible[0]["percent"], "-25.00%")
        view.sort_by("percent")
        self.assertEqual(view.visible[0]["percent"], "+25.00%")
        view.table.selection_set("0")
        view.show_original()
        destination = self.path / "ipc.csv"
        with patch("perf_viewer_gui.filedialog.asksaveasfilename", return_value=str(destination)):
            view.export()
        self.wait(lambda: not view.exporting and destination.exists())
        self.assertEqual(len(destination.read_text(encoding="utf-8-sig").splitlines()), 5)
        view.query.set("200")
        excluded_source = next(row["current_log"] for row in view.rows if row["checkpoint"] == "10015")
        before = excluded_source.read_bytes()
        with patch("perf_viewer_gui.filedialog.asksaveasfilename", return_value=str(excluded_source)):
            view.export()
        self.wait(lambda: not view.exporting)
        self.assertIn("不能覆盖", view.status.get())
        self.assertEqual(excluded_source.read_bytes(), before)
        self.make_checkpoint("current", "perl", "300", 30, "2")
        self.make_checkpoint("baseline", "perl", "300", 30, "2")
        view.refresh()
        self.wait(lambda: not view.loading and len(view.rows) == 6)
        view.close()
        self.assertTrue(view.closed)
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()
