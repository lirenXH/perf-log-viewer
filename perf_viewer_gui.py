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


class PerfViewerApp:
    def __init__(self, root):
        self.root = root
        root.title("性能计数器日志查看器")
        root.geometry("1260x790")
        root.minsize(850, 550)
        self.store = LogStore()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="perf-log")
        self.completions = queue.Queue()
        self.closed = False
        self.info = {"loaded": False, "records": 0, "modules": []}
        self.source = None
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
        self.status = tk.StringVar(value="打开服务器上的日志文件，然后按模块和关键字筛选。")
        self.match_text = tk.StringVar(value="尚未加载日志")
        self.selected_text = tk.StringVar(value="全部模块")
        self.page_text = tk.StringVar(value="1 / 1")
        self._build()
        self.module_search.trace_add("write", lambda *_: self.render_modules())
        for variable in (self.keyword, self.path_filter, self.start_line, self.end_line):
            variable.trace_add("write", self.schedule_query)
        self.page_size.trace_add("write", self.schedule_query)
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
        self.root.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self.root, padding=(16, 12))
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="性能计数器日志查看器", font=("sans", 17, "bold")).pack(side="left")
        self.open_button = ttk.Button(toolbar, text="打开日志文件…", command=self.choose_file)
        self.open_button.pack(side="right", padx=(12, 0))
        ttk.Combobox(toolbar, textvariable=self.encoding, values=("UTF-8", "GB18030 / GBK"), state="readonly", width=16).pack(side="right")
        ttk.Label(toolbar, text="日志编码：").pack(side="right")

        panes = ttk.Panedwindow(self.root, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew", padx=16)
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
        ttk.Label(ranges, text="原始行号：").pack(side="left")
        ttk.Entry(ranges, textvariable=self.start_line, width=10).pack(side="left")
        ttk.Label(ranges, text=" 至 ").pack(side="left")
        ttk.Entry(ranges, textvariable=self.end_line, width=10).pack(side="left")
        ttk.Button(ranges, text="清除筛选", command=self.clear_filters).pack(side="left", padx=10)
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
        columns = ("line", "metric", "value", "module", "path")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for name, title, width in (("line", "原始行号", 85), ("metric", "计数器名称", 300), ("value", "数值", 130), ("module", "模块", 240), ("path", "层级路径", 460)):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, minwidth=60, anchor="e" if name in ("line", "value") else "w", stretch=False)
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
        ttk.Label(self.root, textvariable=self.status, anchor="w", padding=(16, 12), wraplength=1150).grid(row=2, column=0, sticky="ew")
        ttk.Label(self.root, text="保留零值和每轮重复条目；双击查看原始行。导出包含全部匹配结果，不仅当前页。", padding=(16, 0, 16, 12)).grid(row=3, column=0, sticky="w")

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

    def choose_file(self):
        if self.loading:
            return
        filename = filedialog.askopenfilename(parent=self.root, title="打开服务器上的性能计数器日志", filetypes=(("日志文件", "*.log *.txt *.out"), ("所有文件", "*")))
        if filename:
            self.load_file(filename)

    def load_file(self, filename):
        path = Path(filename)
        encoding = "utf-8" if self.encoding.get() == "UTF-8" else "gb18030"
        self.loading = True
        self.query_id += 1
        self.open_button.configure(state="disabled")
        self.csv_button.configure(state="disabled")
        self.log_button.configure(state="disabled")
        self.status.set("正在读取并解析：" + str(path))
        def operation():
            with path.open("rb") as stream:
                return self.store.load(stream, path.stat().st_size, path.name, encoding)
        def success(info):
            self.loading = False
            self.open_button.configure(state="normal")
            self.source = path
            self.info = info
            self.clear_filters()
            warning = "；存在无法解码字符，可切换编码后重新打开" if info["replacement_chars"] else ""
            self.status.set(f"已打开 {path} · 原文件 {info['total_lines']:,} 行 · {info['records']:,} 条 PERF 记录" + warning)
        def failure(error):
            self.loading = False
            self.open_button.configure(state="normal")
            self.status.set("打开失败：" + str(error))
            messagebox.showerror("打开失败", str(error), parent=self.root)
            self.schedule_query()
        self.submit(operation, success, failure)

    def render_modules(self):
        selected = self.module
        needle = self.module_search.get().casefold().strip()
        self.modules.delete(*self.modules.get_children())
        self.module_ids = {"all": ""}
        self.modules.insert("", "end", iid="all", text="全部模块", values=(f"{self.info['records']:,}",))
        chosen = "all" if not selected else None
        for index, module in enumerate(self.info["modules"]):
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
                "page": [str(self.page)], "limit": [self.page_size.get()]}

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
        self.render_modules()
        self.schedule_query()

    def refresh(self):
        self.debounce = None
        if not self.info["loaded"] or self.loading:
            return
        params = self.params()
        token = self.query_id
        def success(result):
            if token != self.query_id or result["serial"] != self.info["serial"]:
                return
            self.page, self.pages = result["page"], result["pages"]
            self.table.delete(*self.table.get_children())
            self.rows = {}
            for index, row in enumerate(result["rows"]):
                iid = "row-" + str(index)
                self.rows[iid] = row
                self.table.insert("", "end", iid=iid, values=(row["line"], row["metric"], row["value"], row["module"], row["path"]))
            self.match_text.set(f"匹配 {result['total']:,} / {self.info['records']:,} 条")
            self.selected_text.set(self.module or "全部模块")
            self.page_text.set(f"{self.page} / {self.pages}")
            self.previous.configure(state="normal" if self.page > 1 else "disabled")
            self.next.configure(state="normal" if self.page < self.pages else "disabled")
            state = "normal" if result["total"] and not self.exporting else "disabled"
            self.csv_button.configure(state=state)
            self.log_button.configure(state=state)
        def failure(error):
            if token == self.query_id:
                self.status.set("筛选条件无效：" + str(error))
                self.csv_button.configure(state="disabled")
                self.log_button.configure(state="disabled")
        self.submit(lambda: self.store.query(params), success, failure)

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
        window = tk.Toplevel(self.root)
        window.title(f"原始日志 · 第 {row['line']} 行")
        window.geometry("1000x260")
        text = tk.Text(window, wrap="word", padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", ANSI.sub("", row["raw"]))
        text.configure(state="disabled")
        ttk.Button(window, text="复制原始行", command=lambda: self.copy(row["raw"])).pack(pady=8)

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def export(self, kind):
        if self.loading or self.exporting or not self.info["loaded"]:
            return
        params = self.params()
        try:
            self.store.filters(params)
        except ValueError as error:
            messagebox.showerror("筛选条件无效", str(error), parent=self.root)
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title="导出全部匹配结果", initialfile="perf-filtered." + kind,
                                               defaultextension="." + kind, filetypes=((kind.upper() + " 文件", "*." + kind), ("所有文件", "*")))
        if not filename:
            return
        self.exporting = True
        self.csv_button.configure(state="disabled")
        self.log_button.configure(state="disabled")
        self.status.set("正在导出全部匹配结果：" + filename)
        source = self.source
        def success(count):
            self.exporting = False
            self.status.set(f"已导出 {count:,} 条：{filename}")
            self.refresh()
        def failure(error):
            self.exporting = False
            self.status.set("导出失败：" + str(error))
            messagebox.showerror("导出失败", str(error), parent=self.root)
            self.refresh()
        self.submit(lambda: export_to_file(self.store, params, filename, kind, source), success, failure)

    def close(self):
        self.closed = True
        self.root.destroy()
        def cleanup():
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.store.close()
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
