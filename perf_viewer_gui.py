#!/usr/bin/env python3
"""Native desktop viewer for server-local logs (including ETX desktops)."""

import argparse
import queue
import sys
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    print("缺少 Tkinter。Ubuntu 22.04 请安装：sudo apt install python3-tk", file=sys.stderr)
    raise SystemExit(1)

from perf_viewer import ANSI, LogStore, export_to_file
from perf_compare import ComparisonStore, export_comparison


class PerfViewerApp:
    def __init__(self, root):
        self.root = root
        root.title("性能计数器日志查看器")
        root.geometry("1400x850")
        root.minsize(850, 550)
        self.store = LogStore()
        self.baseline = LogStore()
        self.comparison = ComparisonStore()
        self.active_store = self.store
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="perf-log")
        self.completions = queue.Queue()
        self.closed = False
        self.info = {"loaded": False, "records": 0, "modules": []}
        self.source = None
        self.baseline_info = {"loaded": False}
        self.baseline_source = None
        self.module = ""
        self.page = 1
        self.pages = 1
        self.query_id = 0
        self.debounce = None
        self.loading = False
        self.exporting = False
        self.rows = {}
        self.module_ids = {}
        self.encoding = tk.StringVar(value="UTF-8")
        self.module_search = tk.StringVar()
        self.keyword = tk.StringVar()
        self.path_filter = tk.StringVar()
        self.start_line = tk.StringVar()
        self.end_line = tk.StringVar()
        self.page_size = tk.StringVar(value="100")
        self.compare_mode = tk.StringVar(value="逐次配对")
        self.direction = tk.StringVar(value="全部")
        self.files_text = tk.StringVar(value="当前：未打开    |    基准：未打开")
        self.status = tk.StringVar(value="打开服务器上的日志文件，然后按模块和关键字筛选。")
        self.match_text = tk.StringVar(value="尚未加载日志")
        self.selected_text = tk.StringVar(value="全部模块")
        self.page_text = tk.StringVar(value="1 / 1")
        self._build()
        self.module_search.trace_add("write", lambda *_: self.render_modules())
        for variable in (self.keyword, self.path_filter, self.start_line, self.end_line, self.direction):
            variable.trace_add("write", self.schedule_query)
        self.page_size.trace_add("write", self.schedule_query)
        self.compare_mode.trace_add("write", lambda *_: self.rebuild_comparison())
        root.bind("<Control-o>", lambda _: self.choose_file())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.poll)
        self.render_modules()

    def _build(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=28)
        style.configure("Treeview.Heading", padding=(5, 6))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)
        toolbar = ttk.Frame(self.root, padding=(16, 12))
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="性能计数器日志查看器", font=("sans", 17, "bold")).pack(side="left")
        self.open_button = ttk.Button(toolbar, text="打开当前日志…", command=self.choose_file)
        self.open_button.pack(side="right", padx=(12, 0))
        self.baseline_button = ttk.Button(toolbar, text="打开基准日志…", command=lambda: self.choose_file(True))
        self.baseline_button.pack(side="right", padx=(8, 0))
        self.remove_baseline_button = ttk.Button(toolbar, text="移除基准", command=self.remove_baseline, state="disabled")
        self.remove_baseline_button.pack(side="right", padx=(8, 0))
        ttk.Combobox(toolbar, textvariable=self.encoding, values=("UTF-8", "GB18030 / GBK"), state="readonly", width=16).pack(side="right")
        ttk.Label(toolbar, text="日志编码：").pack(side="right")
        ttk.Label(self.root, textvariable=self.files_text, padding=(16, 0, 16, 10), wraplength=1300).grid(row=1, column=0, sticky="ew")

        panes = ttk.Panedwindow(self.root, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew", padx=16)
        sidebar = ttk.Frame(panes, padding=(0, 0, 12, 0), width=285)
        content = ttk.Frame(panes)
        panes.add(sidebar, weight=0)
        panes.add(content, weight=1)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(2, weight=1)
        ttk.Label(sidebar, text="模块", font=("sans", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        entry = ttk.Entry(sidebar, textvariable=self.module_search)
        entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.modules = ttk.Treeview(sidebar, columns=("count",), show="tree headings", selectmode="browse")
        self.modules.heading("#0", text="模块名称")
        self.modules.heading("count", text="条目")
        self.modules.column("#0", width=205, minwidth=130)
        self.modules.column("count", width=65, minwidth=55, anchor="e", stretch=False)
        self.modules.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(sidebar, orient="vertical", command=self.modules.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.modules.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(sidebar, orient="horizontal", command=self.modules.xview)
        horizontal.grid(row=3, column=0, sticky="ew")
        self.modules.configure(xscrollcommand=horizontal.set)
        self.modules.bind("<<TreeviewSelect>>", self.select_module)

        content.columnconfigure(0, weight=1)
        content.rowconfigure(2, weight=1)
        filters = ttk.Frame(content)
        filters.grid(row=0, column=0, sticky="ew")
        filters.columnconfigure(0, weight=2)
        filters.columnconfigure(1, weight=1)
        for column, label, variable in ((0, "关键字搜索", self.keyword), (1, "层级路径包含", self.path_filter)):
            ttk.Label(filters, text=label).grid(row=0, column=column, sticky="w", padx=(0, 8))
            ttk.Entry(filters, textvariable=variable).grid(row=1, column=column, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Label(filters, text='多个词同时匹配；双引号搜索短语，例如 "Useful prefetch"。不区分大小写。').grid(row=2, column=0, columnspan=2, sticky="w")
        ranges = ttk.Frame(filters, padding=(0, 10, 0, 10))
        ranges.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Label(ranges, text="当前行号：").pack(side="left")
        ttk.Entry(ranges, textvariable=self.start_line, width=10).pack(side="left")
        ttk.Label(ranges, text=" 至 ").pack(side="left")
        ttk.Entry(ranges, textvariable=self.end_line, width=10).pack(side="left")
        ttk.Button(ranges, text="清除筛选", command=self.clear_filters).pack(side="left", padx=10)
        comparison_options = ttk.Frame(filters, padding=(0, 0, 0, 10))
        comparison_options.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(comparison_options, text="重复条目：").pack(side="left")
        self.mode_combo = ttk.Combobox(comparison_options, textvariable=self.compare_mode, values=("逐次配对", "最后一次"), state="disabled", width=12)
        self.mode_combo.pack(side="left", padx=(0, 16))
        ttk.Label(comparison_options, text="变化筛选：").pack(side="left")
        self.direction_combo = ttk.Combobox(comparison_options, textvariable=self.direction,
            values=("全部", "有变化", "上升", "下降", "不变", "新增", "缺失", "无法比较"), state="disabled", width=12)
        self.direction_combo.pack(side="left")
        summary = ttk.Frame(content, padding=(0, 4, 0, 10))
        summary.grid(row=1, column=0, sticky="ew")
        summary.columnconfigure(0, weight=1)
        ttk.Label(summary, textvariable=self.match_text, font=("sans", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(summary, textvariable=self.selected_text).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.csv_button = ttk.Button(summary, text="导出 CSV…", command=lambda: self.export("csv"), state="disabled")
        self.csv_button.grid(row=0, column=1, rowspan=2, padx=4)
        self.log_button = ttk.Button(summary, text="导出日志…", command=lambda: self.export("log"), state="disabled")
        self.log_button.grid(row=0, column=2, rowspan=2, padx=4)

        table_frame = ttk.Frame(content)
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(table_frame, show="headings", selectmode="browse")
        for name, color in (("上升", "#215ca5"), ("下降", "#ad5718"), ("不变", "#555555"),
                            ("新增", "#74439b"), ("缺失", "#74439b"), ("非数值", "#777777"), ("单位不同", "#777777")):
            self.table.tag_configure(name, foreground=color)
        self.configure_table()
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.bind("<Double-1>", lambda _: self.show_original())
        self.table.bind("<Return>", lambda _: self.show_original())
        pager = ttk.Frame(content, padding=(0, 10, 0, 0))
        pager.grid(row=3, column=0, sticky="ew")
        self.previous = ttk.Button(pager, text="上一页", command=lambda: self.change_page(-1), state="disabled")
        self.previous.pack(side="left")
        ttk.Label(pager, textvariable=self.page_text).pack(side="left", padx=12)
        self.next = ttk.Button(pager, text="下一页", command=lambda: self.change_page(1), state="disabled")
        self.next.pack(side="left")
        ttk.Button(pager, text="查看原始行", command=self.show_original).pack(side="left", padx=12)
        ttk.Combobox(pager, textvariable=self.page_size, values=("100", "200", "500"), state="readonly", width=5).pack(side="right")
        ttk.Label(pager, text="每页条数：").pack(side="right")
        ttk.Label(self.root, textvariable=self.status, anchor="w", padding=(16, 12), wraplength=1300).grid(row=3, column=0, sticky="ew")
        ttk.Label(self.root, text="基准匹配：模块＋路径＋名称。差值＝当前−基准，幅度＝差值/|基准|。上升/下降不代表性能好坏；双击查看原文。", padding=(16, 0, 16, 12)).grid(row=4, column=0, sticky="w")

    def configure_table(self):
        if self.active_store is self.comparison:
            definitions = (("metric", "计数器名称", 270), ("baseline_value", "基准值", 110), ("value", "当前值", 110),
                           ("delta", "差值", 130), ("percent", "变化百分比", 135), ("direction", "方向", 80),
                           ("occurrence", "出现次序", 80), ("line", "当前行号", 90), ("baseline_line", "基准行号", 90),
                           ("module", "模块", 240), ("path", "层级路径", 460))
        else:
            definitions = (("line", "原始行号", 85), ("metric", "计数器名称", 300), ("value", "数值", 130),
                           ("module", "模块", 240), ("path", "层级路径", 460))
        self.columns = tuple(item[0] for item in definitions)
        self.table.configure(columns=self.columns)
        for name, title, width in definitions:
            self.table.heading(name, text=title)
            self.table.column(name, width=width, minwidth=60, stretch=False,
                              anchor="e" if name in ("line", "baseline_line", "value", "baseline_value", "delta", "percent", "occurrence") else "w")

    def submit(self, operation, success, failure=None):
        future = self.executor.submit(operation)
        def done(result):
            if not self.closed:
                self.completions.put((result, success, failure))
        future.add_done_callback(done)
        return future

    def poll(self):
        if self.closed:
            return
        while True:
            try:
                future, success, failure = self.completions.get_nowait()
            except queue.Empty:
                break
            try:
                result = future.result()
            except CancelledError:
                continue
            except Exception as error:
                if failure:
                    failure(error)
                else:
                    self.status.set("操作失败：" + str(error))
                    messagebox.showerror("操作失败", str(error), parent=self.root)
            else:
                success(result)
        self.root.after(50, self.poll)

    def choose_file(self, baseline=False):
        if self.loading or self.exporting:
            return
        filename = filedialog.askopenfilename(parent=self.root, title="打开基准日志" if baseline else "打开当前日志", filetypes=(("日志文件", "*.log *.txt *.out"), ("所有文件", "*")))
        if filename:
            self.load_file(filename, baseline)

    def set_loading(self, loading):
        self.loading = loading
        self.query_id += 1
        for button in (self.open_button, self.baseline_button):
            button.configure(state="disabled" if loading or self.exporting else "normal")
        self.remove_baseline_button.configure(state="normal" if self.baseline_info["loaded"] and not loading and not self.exporting else "disabled")
        self.mode_combo.configure(state="readonly" if self.info["loaded"] and self.baseline_info["loaded"] and not loading and not self.exporting else "disabled")
        if loading:
            self.csv_button.configure(state="disabled")
            self.log_button.configure(state="disabled")

    def update_file_labels(self):
        self.files_text.set(f"当前：{self.source or '未打开'}    |    基准：{self.baseline_source or '未打开'}")

    def load_file(self, filename, baseline=False):
        path = Path(filename)
        encoding = "utf-8" if self.encoding.get() == "UTF-8" else "gb18030"
        target = self.baseline if baseline else self.store
        self.set_loading(True)
        self.status.set("正在读取并解析：" + str(path))
        def operation():
            with path.open("rb") as stream:
                return target.load(stream, path.stat().st_size, path.name, encoding)
        def success(info):
            if baseline:
                self.baseline_source, self.baseline_info = path, info
            else:
                self.source, self.info = path, info
                self.clear_filters()
            self.update_file_labels()
            self.set_loading(False)
            warning = "；存在无法解码字符，可切换编码后重新打开" if info["replacement_chars"] else ""
            self.status.set(f"已打开 {path} · 原文件 {info['total_lines']:,} 行 · {info['records']:,} 条 PERF 记录" + warning)
            if self.info["loaded"] and self.baseline_info["loaded"]:
                self.rebuild_comparison()
            else:
                self.active_store = self.store
                self.configure_table()
                self.render_modules()
                self.schedule_query()
        def failure(error):
            self.set_loading(False)
            self.status.set("打开失败：" + str(error))
            messagebox.showerror("打开失败", str(error), parent=self.root)
            self.schedule_query()
        self.submit(operation, success, failure)

    def rebuild_comparison(self):
        if not self.info["loaded"] or not self.baseline_info["loaded"] or self.loading or self.exporting:
            return
        mode = "last" if self.compare_mode.get() == "最后一次" else "occurrence"
        self.set_loading(True)
        self.status.set("正在匹配当前日志与基准日志…")
        def success(info):
            self.active_store = self.comparison
            self.set_loading(False)
            self.direction_combo.configure(state="readonly")
            self.configure_table()
            if self.module not in {item["name"] for item in info["modules"]}:
                self.module = ""
            self.render_modules()
            counts = info["directions"]
            self.status.set("对比完成 · " + " · ".join(f"{name} {counts.get(name, 0):,}" for name in ("上升", "下降", "不变", "新增", "缺失", "非数值", "单位不同")))
            self.schedule_query()
        def failure(error):
            self.active_store = self.store
            self.set_loading(False)
            self.configure_table()
            self.render_modules()
            self.status.set("对比失败：" + str(error))
            messagebox.showerror("对比失败", str(error), parent=self.root)
            self.schedule_query()
        self.submit(lambda: self.comparison.build(self.store, self.baseline, mode), success, failure)

    def remove_baseline(self):
        if self.loading or self.exporting:
            return
        previous_baseline, previous_comparison = self.baseline, self.comparison
        self.baseline, self.comparison = LogStore(), ComparisonStore()
        self.baseline_source, self.baseline_info = None, {"loaded": False}
        self.active_store = self.store
        if self.module not in {item["name"] for item in self.store.info["modules"]}:
            self.module = ""
        self.direction.set("全部")
        self.direction_combo.configure(state="disabled")
        self.set_loading(False)
        self.update_file_labels()
        self.configure_table()
        self.render_modules()
        self.schedule_query()
        self.status.set("已移除基准，显示当前日志。")
        self.submit(lambda: (previous_baseline.close(), previous_comparison.close()), lambda _: None)

    def render_modules(self):
        info = self.active_store.info
        selected = self.module
        needle = self.module_search.get().casefold().strip()
        self.modules.delete(*self.modules.get_children())
        self.module_ids = {"all": ""}
        self.modules.insert("", "end", iid="all", text="全部模块", values=(f"{info['records']:,}",))
        chosen = "all" if not selected else None
        for index, module in enumerate(info["modules"]):
            if needle and needle not in module["name"].casefold():
                continue
            iid = "module-" + str(index)
            self.module_ids[iid] = module["name"]
            self.modules.insert("", "end", iid=iid, text=module["name"], values=(f"{module['count']:,}",))
            if module["name"] == selected:
                chosen = iid
        if chosen:
            self.modules.selection_set(chosen)

    def select_module(self, _event=None):
        selection = self.modules.selection()
        if not selection or selection[0] not in self.module_ids:
            return
        name = self.module_ids[selection[0]]
        if name != self.module:
            self.module = name
            self.schedule_query()

    def params(self):
        return {"module": [self.module], "q": [self.keyword.get()], "path": [self.path_filter.get()],
                "start": [self.start_line.get()], "end": [self.end_line.get()],
                "page": [str(self.page)], "limit": [self.page_size.get()],
                "direction": ["" if self.direction.get() == "全部" else self.direction.get()]}

    def schedule_query(self, *_):
        self.query_id += 1
        self.page = 1
        if self.debounce is not None:
            self.root.after_cancel(self.debounce)
        self.debounce = self.root.after(250, self.refresh)

    def clear_filters(self):
        self.module = ""
        for variable in (self.keyword, self.path_filter, self.start_line, self.end_line, self.module_search):
            variable.set("")
        self.direction.set("全部")
        self.render_modules()
        self.schedule_query()

    def refresh(self):
        self.debounce = None
        if not self.info["loaded"] or self.loading:
            return
        params = self.params()
        token = self.query_id
        store = self.active_store
        def success(result):
            if token != self.query_id or store is not self.active_store or result["serial"] != store.info["serial"]:
                return
            self.page, self.pages = result["page"], result["pages"]
            self.table.delete(*self.table.get_children())
            self.rows = {}
            for index, row in enumerate(result["rows"]):
                iid = "row-" + str(index)
                self.rows[iid] = row
                values = ["—" if row.get(name) is None else row.get(name, "") for name in self.columns]
                if store is self.comparison and store.info["mode"] == "last":
                    values[self.columns.index("occurrence")] = "最后"
                self.table.insert("", "end", iid=iid, values=values, tags=(row.get("direction", ""),))
            self.match_text.set(f"匹配 {result['total']:,} / {store.info['records']:,} 条")
            self.selected_text.set(self.module or "全部模块")
            self.page_text.set(f"{self.page} / {self.pages}")
            self.previous.configure(state="normal" if self.page > 1 else "disabled")
            self.next.configure(state="normal" if self.page < self.pages else "disabled")
            state = "normal" if result["total"] and not self.exporting else "disabled"
            self.csv_button.configure(state=state)
            self.csv_button.configure(text="导出对比 CSV…" if store is self.comparison else "导出 CSV…")
            self.log_button.configure(state="disabled" if store is self.comparison else state)
        def failure(error):
            if token == self.query_id:
                self.status.set("筛选条件无效：" + str(error))
                self.csv_button.configure(state="disabled")
                self.log_button.configure(state="disabled")
        self.submit(lambda: store.query(params), success, failure)

    def change_page(self, delta):
        self.page = min(self.pages, max(1, self.page + delta))
        self.query_id += 1
        if self.debounce is not None:
            self.root.after_cancel(self.debounce)
            self.debounce = None
        self.refresh()

    def show_original(self):
        selection = self.table.selection()
        if not selection or selection[0] not in self.rows:
            return
        row = self.rows[selection[0]]
        if "baseline_raw" in row:
            original = (f"基准日志 · 行号 {row['baseline_line'] or '—'}\n{row['baseline_raw'] or '（基准中不存在）'}\n\n"
                        f"当前日志 · 行号 {row['line'] or '—'}\n{row['raw'] or '（当前中不存在）'}")
        else:
            original = row["raw"]
        window = tk.Toplevel(self.root)
        window.title("原始日志 · " + row["metric"])
        window.geometry("1000x350")
        text = tk.Text(window, wrap="word", padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", ANSI.sub("", original))
        text.configure(state="disabled")
        ttk.Button(window, text="复制原文", command=lambda: self.copy(original)).pack(pady=8)

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def export(self, kind):
        if self.loading or self.exporting or not self.info["loaded"]:
            return
        params = self.params()
        store = self.active_store
        if store is self.comparison and kind != "csv":
            return
        try:
            store.filters(params)
        except ValueError as error:
            messagebox.showerror("筛选条件无效", str(error), parent=self.root)
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title="导出全部匹配结果", initialfile="perf-filtered." + kind,
                                               defaultextension="." + kind, filetypes=((kind.upper() + " 文件", "*." + kind), ("所有文件", "*")))
        if not filename:
            return
        self.exporting = True
        self.set_loading(False)
        self.csv_button.configure(state="disabled")
        self.log_button.configure(state="disabled")
        self.status.set("正在导出全部匹配结果：" + filename)
        source = self.source
        def success(count):
            self.exporting = False
            self.set_loading(False)
            self.status.set(f"已导出 {count:,} 条：{filename}")
            self.refresh()
        def failure(error):
            self.exporting = False
            self.set_loading(False)
            self.status.set("导出失败：" + str(error))
            messagebox.showerror("导出失败", str(error), parent=self.root)
            self.refresh()
        if store is self.comparison:
            baseline_source = self.baseline_source
            self.submit(lambda: export_comparison(store, params, filename, (source, baseline_source)), success, failure)
        else:
            self.submit(lambda: export_to_file(store, params, filename, kind, source), success, failure)

    def close(self):
        self.closed = True
        self.root.destroy()
        def cleanup():
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.store.close()
            self.baseline.close()
            self.comparison.close()
        threading.Thread(target=cleanup, name="perf-cleanup").start()


def main():
    parser = argparse.ArgumentParser(description="独立桌面性能计数器日志查看器，适用于 Ubuntu / ETX 远程桌面")
    parser.add_argument("log", nargs="?", help="可选：启动时打开的服务器日志")
    parser.add_argument("--encoding", choices=("utf-8", "gb18030"), default="utf-8")
    args = parser.parse_args()
    try:
        root = tk.Tk()
    except tk.TclError as error:
        parser.exit(1, f"无法打开图形窗口：{error}\n请在 ETX 或服务器桌面会话中启动，使用该会话的 DISPLAY 环境变量。\n")
    app = PerfViewerApp(root)
    app.encoding.set("UTF-8" if args.encoding == "utf-8" else "GB18030 / GBK")
    if args.log:
        root.after(100, lambda: app.load_file(args.log))
    root.mainloop()


if __name__ == "__main__":
    main()
