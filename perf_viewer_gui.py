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
    from tkinter import filedialog, messagebox, ttk, font as tkfont
except ImportError:
    print("缺少 Tkinter。Ubuntu 22.04 请安装：sudo apt install python3-tk", file=sys.stderr)
    raise SystemExit(1)

from perf_viewer import ANSI, LogStore, export_to_file
from perf_compare import ComparisonStore, decimal_value, export_comparison
from perf_runs import (RunDirectory, paired_keys, load_checkpoint, compare_run_ipc,
                       export_ipc_comparison, IPC_FIELDS, IPC_HEADERS, check_run_status, filter_ipc_instrcount)


class PerfViewerApp:
    def __init__(self, root):
        self.root = root
        root.title("性能计数器日志查看器")
        root.geometry("1500x1000")
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
        self.current_run = None
        self.baseline_run = None
        self.run_mode = False
        self.current_directory = tk.StringVar()
        self.baseline_directory = tk.StringVar()
        self.benchmark = tk.StringVar()
        self.checkpoint = tk.StringVar()
        self.checkpoint_presence = tk.StringVar()
        self.run_files = {name: tk.StringVar() for name in ("current_perf", "current_log", "baseline_perf", "baseline_log")}
        self.run_file_choices = {}
        self.summary_items = {}
        self.pending_views = []
        self.ipc_overview = None
        self.run_checks = {"当前": [], "基准": []}
        self.check_items = {}
        self.check_query = tk.StringVar()
        self.check_filter = tk.StringVar(value="全部")
        self.check_counts = tk.StringVar(value="加载目录后自动检查主仿真日志")
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
        self.font_size = tk.StringVar(value="12")
        self.applied_font_size = 12
        self.body_font = tkfont.nametofont("TkDefaultFont", root=root)
        self.bold_font = self.body_font.copy()
        self.bold_font.configure(weight="bold")
        self.title_font = self.bold_font.copy()
        self._build()
        self.apply_font_size()
        self.font_size.trace_add("write", lambda *_: self.apply_font_size())
        for key in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            root.bind_all(key, lambda _: self.adjust_font_size(1))
        for key in ("<Control-minus>", "<Control-KP_Subtract>"):
            root.bind_all(key, lambda _: self.adjust_font_size(-1))
        root.bind_all("<Control-0>", lambda _: self.adjust_font_size(reset=True))
        self.module_search.trace_add("write", lambda *_: self.render_modules())
        self.check_query.trace_add("write", lambda *_: self.render_run_checks())
        self.check_filter.trace_add("write", lambda *_: self.render_run_checks())
        for variable in (self.keyword, self.path_filter, self.start_line, self.end_line, self.direction):
            variable.trace_add("write", self.schedule_query)
        self.page_size.trace_add("write", self.schedule_query)
        self.compare_mode.trace_add("write", lambda *_: self.rebuild_comparison())
        root.bind("<Control-o>", lambda _: self.choose_file())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_timer = root.after(50, self.poll)
        self.render_modules()

    def _build(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=28)
        style.configure("Treeview.Heading", padding=(5, 6))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        toolbar = ttk.Frame(self.root, padding=(16, 12))
        toolbar.grid(row=0, column=0, sticky="ew")
        fontbar = ttk.Frame(toolbar, padding=(0, 8, 0, 0))
        fontbar.pack(side="bottom", fill="x")
        self.build_font_controls(fontbar)
        self.details_button = ttk.Button(fontbar, text="收起目录详情", command=self.toggle_run_details, state="disabled")
        self.details_button.pack(side="right")
        ttk.Label(toolbar, text="性能计数器日志查看器", font=self.title_font).pack(side="left")
        self.open_button = ttk.Button(toolbar, text="打开当前日志…", command=self.choose_file)
        self.open_button.pack(side="right", padx=(12, 0))
        self.baseline_button = ttk.Button(toolbar, text="打开基准日志…", command=lambda: self.choose_file(True))
        self.baseline_button.pack(side="right", padx=(8, 0))
        self.remove_baseline_button = ttk.Button(toolbar, text="移除基准", command=self.remove_baseline, state="disabled")
        self.remove_baseline_button.pack(side="right", padx=(8, 0))
        ttk.Combobox(toolbar, textvariable=self.encoding, values=("UTF-8", "GB18030 / GBK"), state="readonly", width=16).pack(side="right")
        ttk.Label(toolbar, text="日志编码：").pack(side="right")
        ttk.Label(self.root, textvariable=self.files_text, padding=(16, 0, 16, 10), wraplength=1300).grid(row=1, column=0, sticky="ew")
        self.build_run_controls()

        panes = ttk.Panedwindow(self.root, orient="horizontal")
        panes.grid(row=3, column=0, sticky="nsew", padx=16)
        sidebar = ttk.Frame(panes, padding=(0, 0, 12, 0), width=380)
        sidebar.grid_propagate(False)
        content = ttk.Frame(panes)
        panes.add(sidebar, weight=0)
        panes.add(content, weight=1)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(0, weight=1)
        self.sidebar_tabs = ttk.Notebook(sidebar)
        self.sidebar_tabs.grid(row=0, column=0, sticky="nsew")
        module_tab = ttk.Frame(self.sidebar_tabs, padding=(0, 8, 0, 0))
        self.sidebar_tabs.add(module_tab, text="性能模块")
        self.build_status_panel()
        sidebar = module_tab
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(2, weight=1)
        ttk.Label(sidebar, text="模块", font=self.bold_font).grid(row=0, column=0, sticky="w", pady=(0, 8))
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
        ttk.Entry(ranges, textvariable=self.start_line, width=6).pack(side="left")
        ttk.Label(ranges, text=" 至 ").pack(side="left")
        ttk.Entry(ranges, textvariable=self.end_line, width=6).pack(side="left")
        ttk.Button(ranges, text="清除筛选", command=self.clear_filters).pack(side="left", padx=10)
        comparison_options = ranges
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
        ttk.Label(summary, textvariable=self.match_text, font=self.bold_font).grid(row=0, column=0, sticky="w")
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
        ttk.Label(self.root, textvariable=self.status, anchor="w", padding=(16, 12), wraplength=1300).grid(row=4, column=0, sticky="ew")
        ttk.Label(self.root, text="基准匹配：模块＋路径＋名称。差值＝当前−基准，幅度＝差值/|基准|。上升/下降不代表性能好坏；双击查看原文。", padding=(16, 0, 16, 12)).grid(row=5, column=0, sticky="w")

    def build_font_controls(self, parent):
        ttk.Label(parent, text="字号：").pack(side="left")
        ttk.Button(parent, text="−", width=3, command=lambda: self.adjust_font_size(-1)).pack(side="left")
        ttk.Combobox(parent, textvariable=self.font_size, values=tuple(str(n) for n in range(8, 25)),
                     state="readonly", width=4).pack(side="left", padx=6)
        ttk.Button(parent, text="＋", width=3, command=lambda: self.adjust_font_size(1)).pack(side="left")
        ttk.Button(parent, text="恢复默认", command=lambda: self.adjust_font_size(reset=True)).pack(side="left", padx=8)
        ttk.Label(parent, text="Ctrl + / Ctrl − 缩放，Ctrl 0 恢复默认；所有窗口同步。").pack(side="left")

    def build_status_panel(self):
        self.check_tab = ttk.Frame(self.sidebar_tabs, padding=(0, 8, 0, 0))
        self.sidebar_tabs.add(self.check_tab, text="仿真状态")
        self.check_tab.columnconfigure(0, weight=1)
        self.check_tab.rowconfigure(3, weight=1)
        ttk.Entry(self.check_tab, textvariable=self.check_query).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        controls = ttk.Frame(self.check_tab)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Combobox(controls, textvariable=self.check_filter, values=("全部", "PASS", "FAIL", "未判定"),
                     width=9, state="readonly").pack(side="left")
        self.check_refresh_button = ttk.Button(controls, text="重新检查", command=self.refresh_run_checks, state="disabled")
        self.check_refresh_button.pack(side="right")
        ttk.Label(self.check_tab, textvariable=self.check_counts, wraplength=320).grid(row=2, column=0, columnspan=2, sticky="w", pady=6)
        self.check_table = ttk.Treeview(self.check_tab, columns=("benchmark", "checkpoint", "result", "side", "log", "reason"),
                                       show="headings", selectmode="browse")
        for name, label, width in (("benchmark", "子项", 170), ("checkpoint", "检查点", 85), ("result", "状态", 80),
                                   ("side", "来源", 60), ("log", "主日志", 250), ("reason", "判断依据", 320)):
            self.check_table.heading(name, text=label)
            self.check_table.column(name, width=width, minwidth=55, stretch=False)
        for name, color in (("PASS", "#008000"), ("FAIL", "#cc0000"), ("未判定", "#000000")):
            self.check_table.tag_configure(name, foreground=color)
        self.check_table.grid(row=3, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(self.check_tab, orient="vertical", command=self.check_table.yview)
        vertical.grid(row=3, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(self.check_tab, orient="horizontal", command=self.check_table.xview)
        horizontal.grid(row=4, column=0, sticky="ew")
        self.check_table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.check_table.bind("<Double-1>", lambda _: self.show_simulation_log())
        self.check_table.bind("<Return>", lambda _: self.show_simulation_log())
        ttk.Button(self.check_tab, text="查看仿真源日志", command=self.show_simulation_log).grid(row=5, column=0, columnspan=2, sticky="ew", pady=6)

    def render_run_checks(self):
        all_rows = [{**row, "side": side} for side, rows in self.run_checks.items() for row in rows]
        words = self.check_query.get().casefold().split()
        selected = self.check_filter.get()
        rows = [row for row in all_rows if (selected == "全部" or row["result"] == selected) and
                all(word in " ".join(str(row[key]) for key in ("benchmark", "checkpoint", "path", "side", "reason")).casefold() for word in words)]
        self.check_table.delete(*self.check_table.get_children())
        self.check_items = {}
        for index, row in enumerate(rows):
            iid = str(index)
            self.check_items[iid] = row
            self.check_table.insert("", "end", iid=iid,
                values=(row["benchmark"], row["checkpoint"], row["result"], row["side"], row["path"].name if row["path"] else "（缺少主日志）", row["reason"]), tags=(row["result"],))
        counts = {name: sum(row["result"] == name for row in all_rows) for name in ("PASS", "FAIL", "未判定")}
        self.check_counts.set(f"显示 {len(rows)} / {len(all_rows)} · " + " · ".join(f"{name} {count}" for name, count in counts.items()))

    def refresh_run_checks(self):
        if self.loading or self.exporting or not self.run_mode:
            return
        roots = {side: run.root for side, run in (("当前", self.current_run), ("基准", self.baseline_run)) if run}
        encoding = "utf-8" if self.encoding.get() == "UTF-8" else "gb18030"
        self.set_loading(True)
        self.status.set("正在重新检查全部主仿真日志…")
        def success(results):
            self.run_checks = {"当前": [], "基准": [], **results}
            self.render_run_checks()
            self.set_loading(False)
            self.status.set("仿真状态检查完成。双击状态列表中的子项可查看主日志。")
            self.schedule_query()
        def failure(error):
            self.set_loading(False)
            self.status.set("检查失败，保留之前结果：" + str(error))
            self.schedule_query()
        self.submit(lambda: {side: check_run_status(RunDirectory.scan(path), encoding) for side, path in roots.items()}, success, failure)

    def show_simulation_log(self):
        selection = self.check_table.selection()
        if not selection or selection[0] not in self.check_items:
            return
        row = self.check_items[selection[0]]
        if row["path"] is None:
            self.status.set(row["reason"])
            return
        window = tk.Toplevel(self.root)
        window.title(f"{row['result']} · {row['benchmark']} / {row['checkpoint']} · {row['path'].name}")
        window.geometry("1150x650")
        ttk.Label(window, text=str(row["path"]), wraplength=1100, padding=8).pack(fill="x")
        area = ttk.Frame(window)
        area.pack(fill="both", expand=True)
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        text = tk.Text(area, wrap="none", padx=12, pady=12)
        text.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(area, orient="vertical", command=text.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(area, orient="horizontal", command=text.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        text.insert("1.0", "正在读取源文件…")
        text.configure(state="disabled")
        encoding = "utf-8" if self.encoding.get() == "UTF-8" else "gb18030"
        def success(contents):
            if not window.winfo_exists():
                return
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", ANSI.sub("", contents))
            if row["line"]:
                text.tag_add("match", f"{row['line']}.0", f"{row['line']}.end")
                text.tag_configure("match", background="#fff1ad", foreground="#000000")
                text.see(f"{row['line']}.0")
            text.configure(state="disabled")
        self.submit(lambda: row["path"].read_text(encoding=encoding, errors="replace"), success,
                    lambda error: success("读取失败：" + str(error)))
        return window

    def toggle_run_details(self):
        if not self.run_mode:
            return
        if self.run_details.winfo_manager():
            self.run_details.grid_remove()
            self.details_button.configure(text="展开目录详情")
        else:
            self.run_details.grid()
            self.details_button.configure(text="收起目录详情")

    def adjust_font_size(self, delta=0, reset=False):
        self.font_size.set(str(12 if reset else max(8, min(24, self.applied_font_size + delta))))
        return "break"

    def apply_font_size(self):
        try:
            size = int(self.font_size.get())
        except ValueError:
            return
        if not 8 <= size <= 24:
            return
        ratio = size / self.applied_font_size
        for name in tkfont.names(root=self.root):
            if name.startswith("Tk"):
                tkfont.nametofont(name, root=self.root).configure(size=size)
        self.bold_font.configure(size=size)
        self.title_font.configure(size=size + 5)
        style = ttk.Style(self.root)
        style.configure(".", font=self.body_font)
        style.configure("Treeview", font=self.body_font, rowheight=self.body_font.metrics("linespace") + 12)
        style.configure("Treeview.Heading", font=self.bold_font)
        self.root.option_add("*TCombobox*Listbox.font", self.body_font)
        # Preserve user-adjusted widths while making room for larger glyphs.
        def resize_tables(widget):
            if isinstance(widget, ttk.Treeview):
                for column in ("#0", *widget["columns"]):
                    width = widget.column(column, "width")
                    widget.column(column, width=max(widget.column(column, "minwidth"), round(width * ratio)))
            for child in widget.winfo_children():
                resize_tables(child)
        resize_tables(self.root)
        self.applied_font_size = size

    def build_run_controls(self):
        frame = ttk.Frame(self.root, padding=(16, 0, 16, 10))
        frame.grid(row=2, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)
        self.directory_buttons = []
        for row, (label, variable, baseline) in enumerate((("当前运行目录：", self.current_directory, False),
                                                        ("基准运行目录：", self.baseline_directory, True))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(frame, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", padx=8, pady=3)
            entry.bind("<Return>", lambda _, b=baseline: self.open_directory(baseline=b))
            for column, text, choose in ((2, "选择目录…", True), (3, "加载目录", False)):
                button = ttk.Button(frame, text=text, command=lambda b=baseline, c=choose: self.open_directory(baseline=b, choose=c))
                button.grid(row=row, column=column, padx=(4, 0))
                self.directory_buttons.append(button)
        self.run_details = ttk.Frame(frame, padding=(0, 8, 0, 0))
        self.run_details.grid(row=2, column=0, columnspan=4, sticky="ew")
        self.run_details.columnconfigure(0, weight=1)
        selectors = ttk.Frame(self.run_details)
        selectors.grid(row=0, column=0, sticky="ew")
        ttk.Label(selectors, text="SPEC 子项：").pack(side="left")
        self.benchmark_combo = ttk.Combobox(selectors, textvariable=self.benchmark, state="readonly", width=28)
        self.benchmark_combo.pack(side="left", padx=(0, 16))
        self.benchmark_combo.bind("<<ComboboxSelected>>", lambda _: self.select_benchmark())
        ttk.Label(selectors, text="checkpoint：").pack(side="left")
        self.checkpoint_combo = ttk.Combobox(selectors, textvariable=self.checkpoint, state="readonly", width=18)
        self.checkpoint_combo.pack(side="left", padx=(0, 16))
        self.checkpoint_combo.bind("<<ComboboxSelected>>", lambda _: self.select_checkpoint())
        self.ipc_overview_button = ttk.Button(selectors, text="全部 checkpoint 的 IPC 对比…", command=self.open_ipc_overview, state="disabled")
        self.ipc_overview_button.pack(side="right", padx=(12, 0))
        ttk.Label(selectors, textvariable=self.checkpoint_presence).pack(side="left")
        files = ttk.Frame(self.run_details, padding=(0, 6, 0, 6))
        files.grid(row=1, column=0, sticky="ew")
        files.columnconfigure(1, weight=1)
        files.columnconfigure(3, weight=1)
        self.run_file_combos = {}
        for name, label, row, column in (("current_perf", "当前 .perf：", 0, 0), ("baseline_perf", "基准 .perf：", 0, 2),
                                         ("current_log", "当前 .log：", 1, 0), ("baseline_log", "基准 .log：", 1, 2)):
            ttk.Label(files, text=label).grid(row=row, column=column, sticky="w", pady=2)
            combo = ttk.Combobox(files, textvariable=self.run_files[name], state="readonly", width=45)
            combo.grid(row=row, column=column + 1, sticky="ew", padx=(4, 12), pady=2)
            combo.bind("<<ComboboxSelected>>", lambda _: self.reload_checkpoint())
            self.run_file_combos[name] = combo
        columns = ("metric", "baseline_value", "value", "delta", "percent", "direction")
        self.summary_table = ttk.Treeview(self.run_details, columns=columns, show="headings", height=3)
        for name, title, width in zip(columns, ("主日志指标（最后一次）", "基准值", "当前值", "差值", "变化百分比", "状态"),
                                      (220, 160, 160, 150, 150, 100)):
            self.summary_table.heading(name, text=title)
            self.summary_table.column(name, width=width, minwidth=65, anchor="w" if name in ("metric", "direction") else "e")
        for name, color in (("上升", "#215ca5"), ("下降", "#ad5718"), ("新增", "#74439b"), ("缺失", "#74439b")):
            self.summary_table.tag_configure(name, foreground=color)
        self.summary_table.grid(row=2, column=0, sticky="ew")
        scroll = ttk.Scrollbar(self.run_details, orient="vertical", command=self.summary_table.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.summary_table.configure(yscrollcommand=scroll.set)
        self.summary_table.bind("<Double-1>", lambda _: self.show_summary_original())
        ttk.Label(self.run_details, text="目录按子项名＋checkpoint 名配对；主日志指标取最后一次输出，可双击查看来源。IPC 的统计区间以原日志为准。").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.run_details.grid_remove()

    def configure_table(self):
        self.table.delete(*self.table.get_children())
        self.rows = {}
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
            self.table.column(name, width=round(width * self.applied_font_size / 12), minwidth=60, stretch=False,
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
        self.poll_timer = self.root.after(50, self.poll)

    def choose_file(self, baseline=False):
        if self.loading or self.exporting:
            return
        filename = filedialog.askopenfilename(parent=self.root, title="打开基准日志" if baseline else "打开当前日志", filetypes=(("日志文件", "*.perf *.log *.txt *.out"), ("所有文件", "*")))
        if filename:
            self.load_file(filename, baseline)

    def open_directory(self, path=None, baseline=False, choose=False):
        if self.loading or self.exporting:
            return
        variable = self.baseline_directory if baseline else self.current_directory
        if choose:
            path = filedialog.askdirectory(parent=self.root, title="选择基准运行结果目录" if baseline else "选择当前运行结果目录")
            if not path:
                return
        path = path if path is not None else variable.get().strip()
        if not path:
            self.status.set("请先输入或选择包含各个 SPEC 子项的运行结果根目录。")
            return
        self.set_loading(True)
        self.status.set("正在扫描目录并检查仿真状态：" + str(path))
        encoding = "utf-8" if self.encoding.get() == "UTF-8" else "gb18030"
        def operation():
            run = RunDirectory.scan(path)
            return run, check_run_status(run, encoding)
        def success(result):
            run, checks = result
            self.run_checks["基准" if baseline else "当前"] = checks
            self.render_run_checks()
            self.sidebar_tabs.select(self.check_tab)
            if baseline:
                self.baseline_run = run
            else:
                self.current_run = run
            variable.set(str(run.root))
            self.run_mode = True
            self.run_details.grid()
            self.details_button.configure(text="收起目录详情")
            self.set_loading(False)
            self.render_run_navigation()
        def failure(error):
            self.set_loading(False)
            self.status.set("目录扫描失败：" + str(error))
            messagebox.showerror("目录扫描失败", str(error), parent=self.root)
            self.schedule_query()
        self.submit(operation, success, failure)

    def render_run_navigation(self):
        names = sorted({key[0] for key in paired_keys(self.current_run, self.baseline_run)})
        self.benchmark_combo.configure(values=names)
        if self.benchmark.get() not in names:
            self.benchmark.set(names[0] if names else "")
        self.select_benchmark()

    def select_benchmark(self):
        if self.loading or self.exporting:
            return
        checkpoints = [key[1] for key in paired_keys(self.current_run, self.baseline_run) if key[0] == self.benchmark.get()]
        self.checkpoint_combo.configure(values=checkpoints)
        if self.checkpoint.get() not in checkpoints:
            self.checkpoint.set(checkpoints[0] if checkpoints else "")
        self.select_checkpoint()

    def select_checkpoint(self):
        if self.loading or self.exporting:
            return
        key = self.benchmark.get(), self.checkpoint.get()
        current = self.current_run.checkpoints.get(key) if self.current_run else None
        baseline = self.baseline_run.checkpoints.get(key) if self.baseline_run else None
        if current is None and baseline is None:
            return
        descriptions = []
        for side, entry in (("current", current), ("baseline", baseline)):
            if side == "baseline" and not self.baseline_run:
                continue
            label = "当前" if side == "current" else "基准"
            descriptions.append(label + ("存在" if entry else "缺少 checkpoint"))
            for extension, paths in (("perf", entry.perf if entry else ()), ("log", entry.logs if entry else ())):
                name = side + "_" + extension
                choices = {path.name: path for path in paths}
                self.run_file_choices[name] = choices
                self.run_file_combos[name].configure(values=list(choices), state="readonly" if choices else "disabled")
                default = paths[-1] if extension == "perf" and paths else paths[0] if paths else None
                self.run_files[name].set(default.name if default else "（缺少 ." + extension + "）")
                if entry and not paths:
                    descriptions.append(label + "缺少 ." + extension)
                if len(paths) > 1:
                    descriptions.append(label + f"有 {len(paths)} 份 .{extension}，请确认选择")
        if not self.baseline_run:
            for name in ("baseline_perf", "baseline_log"):
                self.run_file_choices[name] = {}
                self.run_files[name].set("（未打开基准目录）")
                self.run_file_combos[name].configure(values=(), state="disabled")
        self.checkpoint_presence.set(" · ".join(descriptions))
        self.reload_checkpoint()

    def reload_checkpoint(self):
        if self.loading or self.exporting or not self.run_mode:
            return
        paths = {name: self.run_file_choices.get(name, {}).get(variable.get()) for name, variable in self.run_files.items()}
        comparing = self.baseline_run is not None
        encoding = "utf-8" if self.encoding.get() == "UTF-8" else "gb18030"
        mode = "last" if self.compare_mode.get() == "最后一次" else "occurrence"
        key = self.benchmark.get(), self.checkpoint.get()
        self.set_loading(True)
        self.status.set(f"正在读取 {key[0]} / {key[1]}…")
        def operation():
            result = load_checkpoint(paths["current_perf"], paths["baseline_perf"], paths["current_log"], paths["baseline_log"],
                                     comparing, encoding, mode)
            self.pending_views.append(result[:3])
            return result
        def success(result):
            self.pending_views.remove(result[:3])
            previous = self.store, self.baseline, self.comparison
            self.store, self.baseline, self.comparison, summaries = result
            self.source, self.baseline_source = paths["current_perf"], paths["baseline_perf"]
            self.info = self.store.info
            self.baseline_info = self.baseline.info if comparing else {"loaded": False}
            self.active_store = self.comparison if comparing else self.store
            self.table.delete(*self.table.get_children())
            self.rows = {}
            if self.module not in {item["name"] for item in self.active_store.info["modules"]}:
                self.module = ""
            self.direction.set("全部")
            self.update_file_labels()
            self.configure_table()
            self.summary_table.delete(*self.summary_table.get_children())
            self.summary_items = {}
            for index, row in enumerate(summaries):
                iid = "summary-" + str(index)
                self.summary_items[iid] = row
                self.summary_table.insert("", "end", iid=iid, values=[row[name] for name in self.summary_table["columns"]], tags=(row["direction"],))
            self.summary_sources = paths
            self.set_loading(False)
            self.direction_combo.configure(state="readonly" if comparing else "disabled")
            self.render_modules()
            missing = [name for name in ("IPC", "cycle", "instrcount") if not any(row["metric"].endswith(name) and row["current"] for row in summaries)]
            self.status.set(f"已打开 {key[0]} / {key[1]} · {self.active_store.info['records']:,} 条计数器" +
                            (" · 当前主日志未识别：" + "、".join(missing) if missing else "") +
                            (" · 多份文件请确认下拉选择" if any(len(v) > 1 for v in self.run_file_choices.values()) else ""))
            self.schedule_query()
            self.submit(lambda: [store.close() for store in previous], lambda _: None)
        def failure(error):
            self.set_loading(False)
            self.status.set("checkpoint 打开失败，保留之前的结果：" + str(error))
            messagebox.showerror("checkpoint 打开失败", str(error), parent=self.root)
            self.schedule_query()
        self.submit(operation, success, failure)

    def show_summary_original(self):
        selection = self.summary_table.selection()
        if not selection or selection[0] not in self.summary_items:
            return
        row = self.summary_items[selection[0]]
        pieces = []
        for label, name, side in (("基准", "baseline_log", "baseline"), ("当前", "current_log", "current")):
            value = row[side]
            path = self.summary_sources.get(name)
            pieces.append(f"{label}：{path or '缺少主日志'}\n" +
                          (f"行号 {value.line or '计算值'}\n{value.raw}" if value else "（未识别该指标）"))
        window = tk.Toplevel(self.root)
        window.title("主日志指标来源 · " + row["metric"])
        window.geometry("1100x350")
        text = tk.Text(window, wrap="word", padx=12, pady=12)
        text.pack(fill="both", expand=True)
        original = "\n\n".join(pieces)
        text.insert("1.0", ANSI.sub("", original))
        text.configure(state="disabled")
        ttk.Button(window, text="复制原文", command=lambda: self.copy(original)).pack(pady=8)

    def clear_run_mode(self):
        self.current_run = self.baseline_run = None
        self.run_mode = False
        self.current_directory.set("")
        self.baseline_directory.set("")
        self.run_details.grid_remove()
        self.run_checks = {"当前": [], "基准": []}
        self.render_run_checks()
        self.sidebar_tabs.select(0)

    def open_ipc_overview(self):
        if self.loading or self.exporting or not self.current_run or not self.baseline_run:
            self.status.set("请先加载当前和基准两个运行结果目录。")
            return
        if self.ipc_overview and not self.ipc_overview.closed:
            if (self.ipc_overview.current_root, self.ipc_overview.baseline_root) == (self.current_run.root, self.baseline_run.root):
                self.ipc_overview.window.lift()
                return self.ipc_overview
            self.ipc_overview.close()
        self.ipc_overview = IPCOverview(self)
        return self.ipc_overview

    def set_loading(self, loading):
        self.loading = loading
        self.query_id += 1
        for button in (self.open_button, self.baseline_button, *self.directory_buttons):
            button.configure(state="disabled" if loading or self.exporting else "normal")
        self.remove_baseline_button.configure(state="normal" if (self.baseline_info["loaded"] or self.baseline_run) and not loading and not self.exporting else "disabled")
        self.mode_combo.configure(state="readonly" if self.info["loaded"] and self.baseline_info["loaded"] and not loading and not self.exporting else "disabled")
        for combo in (self.benchmark_combo, self.checkpoint_combo):
            combo.configure(state="readonly" if self.run_mode and not loading and not self.exporting else "disabled")
        for name, combo in self.run_file_combos.items():
            combo.configure(state="readonly" if self.run_mode and self.run_file_choices.get(name) and not loading and not self.exporting else "disabled")
        self.ipc_overview_button.configure(state="normal" if self.current_run and self.baseline_run and not loading and not self.exporting else "disabled")
        self.details_button.configure(state="normal" if self.run_mode else "disabled")
        self.check_refresh_button.configure(state="normal" if self.run_mode and not loading and not self.exporting else "disabled")
        if loading:
            self.csv_button.configure(state="disabled")
            self.log_button.configure(state="disabled")

    def update_file_labels(self):
        self.files_text.set(f"当前：{self.source or '未打开'}    |    基准：{self.baseline_source or '未打开'}")

    def load_file(self, filename, baseline=False):
        if self.loading or self.exporting:
            return
        if Path(filename).expanduser().is_dir():
            self.open_directory(filename, baseline)
            return
        path = Path(filename)
        encoding = "utf-8" if self.encoding.get() == "UTF-8" else "gb18030"
        target = self.baseline if baseline else self.store
        self.set_loading(True)
        self.status.set("正在读取并解析：" + str(path))
        def operation():
            with path.open("rb") as stream:
                return target.load(stream, path.stat().st_size, path.name, encoding)
        def success(info):
            self.clear_run_mode()
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
        if self.run_mode:
            self.baseline_run = None
            self.baseline_directory.set("")
            self.run_checks["基准"] = []
            self.render_run_checks()
            if self.current_run:
                self.render_run_navigation()
                return
            self.clear_run_mode()
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
        if self.debounce is not None:
            self.root.after_cancel(self.debounce)
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
        sources = (self.source, self.baseline_source)
        if self.run_mode:
            sources += tuple(self.summary_sources.values())
        destination = Path(filename)
        if any(source and (destination.resolve() == source.resolve() or
                           (destination.exists() and source.exists() and destination.samefile(source))) for source in sources):
            messagebox.showerror("导出失败", "不能覆盖当前或基准的 .perf / .log 源文件，请选择其他文件名。", parent=self.root)
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
        for timer in (self.poll_timer, self.debounce):
            if timer is not None:
                self.root.after_cancel(timer)
        self.root.update_idletasks()
        self.root.destroy()
        def cleanup():
            self.executor.shutdown(wait=True)
            self.store.close()
            self.baseline.close()
            self.comparison.close()
            for view in self.pending_views:
                for store in view:
                    store.close()
        threading.Thread(target=cleanup, name="perf-cleanup").start()


class IPCOverview:
    """A snapshot of all main-log IPCs in the two selected run directories."""

    def __init__(self, app):
        self.app = app
        self.current_root, self.baseline_root = app.current_run.root, app.baseline_run.root
        self.encoding = "utf-8" if app.encoding.get() == "UTF-8" else "gb18030"
        self.rows, self.visible = [], []
        self.closed = self.loading = self.exporting = False
        self.sort_column, self.reverse = None, False
        self.window = tk.Toplevel(app.root)
        self.window.title("全部 checkpoint 的 IPC 对比")
        self.window.geometry("1450x740")
        self.window.minsize(900, 450)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(2, weight=1)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.query = tk.StringVar()
        self.direction = tk.StringVar(value="全部")
        self.status = tk.StringVar()
        self.counts = tk.StringVar()
        self.instr_enabled = tk.BooleanVar(value=False)
        self.instr_target = tk.StringVar(value="40000000")
        self.instr_tolerance = tk.StringVar(value="1")
        self.instr_hint = tk.StringVar()
        ttk.Label(self.window, text=f"当前：{self.current_root}\n基准：{self.baseline_root}", padding=12, wraplength=1350).grid(row=0, column=0, sticky="ew")
        filters = ttk.Frame(self.window, padding=(12, 0, 12, 10))
        filters.grid(row=1, column=0, sticky="ew")
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="搜索子项 / checkpoint / 日志：").grid(row=0, column=0, sticky="w")
        ttk.Entry(filters, textvariable=self.query).grid(row=0, column=1, sticky="ew", padx=(4, 16))
        ttk.Label(filters, text="变化：").grid(row=0, column=2)
        ttk.Combobox(filters, textvariable=self.direction, values=("全部", "上升", "下降", "不变", "新增", "缺失", "无法比较"),
                     width=12, state="readonly").grid(row=0, column=3, padx=(0, 12))
        self.refresh_button = ttk.Button(filters, text="重新提取 IPC", command=self.refresh)
        self.refresh_button.grid(row=0, column=4)
        self.export_button = ttk.Button(filters, text="导出筛选结果 CSV…", command=self.export, state="disabled")
        self.export_button.grid(row=0, column=5, padx=(12, 0))
        instr_controls = ttk.Frame(filters, padding=(0, 8, 0, 0))
        instr_controls.grid(row=1, column=0, columnspan=6, sticky="ew")
        ttk.Checkbutton(instr_controls, text="开启 instrcount 筛选", variable=self.instr_enabled).pack(side="left", padx=(0, 12))
        ttk.Label(instr_controls, text="目标值 A：").pack(side="left")
        self.instr_target_entry = ttk.Entry(instr_controls, textvariable=self.instr_target, width=14, state="disabled")
        self.instr_target_entry.pack(side="left", padx=(0, 12))
        ttk.Label(instr_controls, text="误差值 B：").pack(side="left")
        self.instr_tolerance_entry = ttk.Entry(instr_controls, textvariable=self.instr_tolerance, width=10, state="disabled")
        self.instr_tolerance_entry.pack(side="left", padx=(0, 12))
        ttk.Label(instr_controls, textvariable=self.instr_hint).pack(side="left")
        ttk.Label(filters, textvariable=self.counts).grid(row=2, column=0, columnspan=6, sticky="w", pady=(10, 0))
        frame = ttk.Frame(self.window, padding=(12, 0))
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.columns = IPC_FIELDS[:10]
        self.table = ttk.Treeview(frame, columns=self.columns, show="headings", selectmode="browse")
        for name, title, width in zip(self.columns, IPC_HEADERS, (190, 100, 230, 100, 120, 120, 120, 135, 100, 370)):
            self.table.heading(name, text=title, command=lambda n=name: self.sort_by(n))
            self.table.column(name, width=round(width * app.applied_font_size / 12), minwidth=70, stretch=False,
                              anchor="e" if name in ("baseline_value", "value", "delta", "percent") else "w")
        for name, color in (("上升", "#215ca5"), ("下降", "#ad5718"), ("新增", "#74439b"), ("缺失", "#74439b"), ("无法比较", "#777777")):
            self.table.tag_configure(name, foreground=color)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.bind("<Double-1>", lambda _: self.show_original())
        self.table.bind("<Return>", lambda _: self.show_original())
        footer = ttk.Frame(self.window, padding=12)
        footer.grid(row=3, column=0, sticky="ew")
        ttk.Button(footer, text="查看 IPC 来源", command=self.show_original).pack(side="right")
        ttk.Label(footer, textvariable=self.status, wraplength=1150).pack(side="left")
        ttk.Label(self.window, text="取每份主日志、每个 core 最后一次 IPC；双击查看来源，点击列头排序。变化百分比＝(当前−基准)/|基准|。不按 SimPoint 权重汇总。",
                  padding=(12, 0, 12, 12), wraplength=1350).grid(row=4, column=0, sticky="w")
        fontbar = ttk.Frame(self.window, padding=(12, 0, 12, 12))
        fontbar.grid(row=5, column=0, sticky="ew")
        app.build_font_controls(fontbar)
        self.query.trace_add("write", lambda *_: self.render())
        self.direction.trace_add("write", lambda *_: self.render())
        for variable in (self.instr_enabled, self.instr_target, self.instr_tolerance):
            variable.trace_add("write", lambda *_: self.render())
        self.refresh()

    def refresh(self):
        if self.closed or self.loading or self.exporting:
            return
        self.loading = True
        self.refresh_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.status.set("正在提取两个目录下全部主日志的 IPC…")
        def operation():
            return compare_run_ipc(RunDirectory.scan(self.current_root), RunDirectory.scan(self.baseline_root), self.encoding)
        def success(rows):
            if self.closed:
                return
            self.loading = False
            self.rows = rows
            self.refresh_button.configure(state="normal")
            self.status.set("提取完成。缺少日志或未识别 IPC 的条目均保留在表中；instrcount 不同会在备注提示。")
            self.render()
        def failure(error):
            if not self.closed:
                self.loading = False
                self.refresh_button.configure(state="normal")
                self.status.set("提取失败，保留上次结果：" + str(error))
                self.render()
        self.app.submit(operation, success, failure)

    def render(self):
        if self.closed:
            return
        enabled = self.instr_enabled.get()
        for entry in (self.instr_target_entry, self.instr_tolerance_entry):
            entry.configure(state="normal" if enabled else "disabled")
        rows = self.rows
        if enabled:
            try:
                rows, lower, upper = filter_ipc_instrcount(rows, self.instr_target.get(), self.instr_tolerance.get())
            except ValueError as error:
                self.instr_hint.set(str(error))
                self.visible = []
                self.table.delete(*self.table.get_children())
                self.counts.set("instrcount 筛选参数无效，请修正 A / B 后查看对比结果。")
                self.export_button.configure(state="disabled")
                return
            self.instr_hint.set(f"两边都须在 [{lower}, {upper}] 内（含边界）")
        else:
            self.instr_hint.set("关闭时按原规则比较 IPC")
        words = self.query.get().casefold().split()
        direction = self.direction.get()
        self.visible = [row for row in rows if (direction == "全部" or row["direction"] == direction) and
                        all(word in " ".join(str(row[name]) for name in IPC_FIELDS).casefold() for word in words)]
        column = self.sort_column
        if column in ("baseline_value", "value", "delta", "percent"):
            numeric = [(decimal_value(row[column]), row) for row in self.visible]
            valid = sorted(((value[0], row) for value, row in numeric if value is not None), key=lambda item: item[0], reverse=self.reverse)
            self.visible = [row for _, row in valid] + [row for value, row in numeric if value is None]
        elif column:
            self.visible.sort(key=lambda row: int(row[column]) if column == "checkpoint" else row[column].casefold(), reverse=self.reverse)
        self.table.delete(*self.table.get_children())
        for index, row in enumerate(self.visible):
            self.table.insert("", "end", iid=str(index), values=[row[name] for name in self.columns], tags=(row["direction"],))
        counts = {name: sum(row["direction"] == name for row in rows) for name in ("上升", "下降", "不变", "新增", "缺失", "无法比较")}
        self.counts.set(f"显示 {len(self.visible):,} / {len(self.rows):,} 条 IPC · " + " · ".join(f"{name} {count:,}" for name, count in counts.items()))
        self.export_button.configure(state="normal" if self.visible and not self.loading and not self.exporting else "disabled")

    def sort_by(self, column):
        self.reverse = not self.reverse if self.sort_column == column else False
        self.sort_column = column
        self.render()

    def show_original(self):
        selection = self.table.selection()
        if not selection:
            return
        row = self.visible[int(selection[0])]
        parts = [f"{row['benchmark']} / {row['checkpoint']} / {row['core']}\n{row['note']}"]
        for label, side in (("基准", "baseline"), ("当前", "current")):
            parts.append(f"{label}主日志：{row[side + '_log'] or '缺少主日志'}")
            for name, key in (("IPC", side), ("instrcount", side + "_instr"), ("cycle", side + "_cycle")):
                value = row[key]
                parts.append(f"{name}：{value.value} · 行号 {value.line or '计算值'}\n{value.raw}" if value else name + "：（未识别）")
        original = "\n\n".join(parts)
        window = tk.Toplevel(self.window)
        window.title("IPC 来源 · " + row["benchmark"] + " / " + row["checkpoint"])
        window.geometry("1100x550")
        text = tk.Text(window, wrap="word", padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", ANSI.sub("", original))
        text.configure(state="disabled")
        ttk.Button(window, text="复制原文", command=lambda: self.app.copy(original)).pack(pady=8)

    def export(self):
        if self.loading or self.exporting or not self.visible or self.closed:
            return
        destination = filedialog.asksaveasfilename(parent=self.window, title="导出全部筛选后的 IPC 对比", initialfile="ipc-comparison.csv",
                                                  defaultextension=".csv", filetypes=(("CSV 文件", "*.csv"),))
        if not destination:
            return
        rows = list(self.visible)
        sources = [row[name] for row in self.rows for name in ("current_log", "baseline_log")]
        self.exporting = True
        self.refresh_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        def complete(message):
            if not self.closed:
                self.exporting = False
                self.refresh_button.configure(state="normal")
                self.status.set(message)
                self.render()
        self.app.submit(lambda: export_ipc_comparison(rows, destination, sources),
                        lambda count: complete(f"已导出 {count:,} 条 IPC：{destination}"),
                        lambda error: complete("导出失败：" + str(error)))

    def close(self):
        self.closed = True
        self.window.destroy()


def main():
    parser = argparse.ArgumentParser(description="独立桌面性能计数器日志查看器，适用于 Ubuntu / ETX 远程桌面")
    parser.add_argument("log", nargs="?", help="可选：启动时打开的日志文件或运行结果根目录")
    parser.add_argument("--encoding", choices=("utf-8", "gb18030"), default="utf-8")
    args = parser.parse_args()
    try:
        root = tk.Tk()
    except tk.TclError as error:
        parser.exit(1, f"无法打开图形窗口：{error}\n请在 ETX 或服务器桌面会话中启动，使用该会话的 DISPLAY 环境变量。\n")
    app = PerfViewerApp(root)
    app.encoding.set("UTF-8" if args.encoding == "utf-8" else "GB18030 / GBK")
    if args.log:
        root.after(100, lambda: app.open_directory(args.log) if Path(args.log).expanduser().is_dir() else app.load_file(args.log))
    root.mainloop()


if __name__ == "__main__":
    main()
