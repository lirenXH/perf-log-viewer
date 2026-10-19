"""Browse SPEC result directories and read simulator summaries without networking."""

import io
import csv
import re
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path

from perf_compare import ComparisonStore, compare_values, display_decimal
from perf_viewer import ANSI, PERF, LogStore


def checkpoint_sort(name):
    return (int(name), name)


@dataclass(frozen=True)
class CheckpointFiles:
    directory: Path
    perf: tuple
    logs: tuple


@dataclass(frozen=True)
class RunDirectory:
    root: Path
    checkpoints: dict

    @classmethod
    def scan(cls, root):
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("运行结果目录不存在：" + str(root))
        entries = {}
        for benchmark in sorted(root.iterdir()):
            if not benchmark.is_dir():
                continue
            for checkpoint in benchmark.iterdir():
                if not checkpoint.is_dir() or not checkpoint.name.isascii() or not checkpoint.name.isdigit():
                    continue
                files = sorted(path for path in checkpoint.iterdir() if path.is_file())
                perf = tuple(path for path in files if path.suffix.lower() == ".perf")
                logs = tuple(path for path in files if path.suffix.lower() == ".log"
                             and not path.name.lower().endswith("_gzip_check.log"))
                entries[benchmark.name, checkpoint.name] = CheckpointFiles(checkpoint, perf, logs)
        if not entries:
            raise ValueError("未找到“子项/数字 checkpoint”目录。请选择包含各个 SPEC 子项的运行结果根目录。")
        return cls(root, entries)


def paired_keys(current=None, baseline=None):
    keys = set(current.checkpoints if current else ()) | set(baseline.checkpoints if baseline else ())
    return sorted(keys, key=lambda key: (key[0], checkpoint_sort(key[1])))


# Both compact aliases and the stock XiangShan trap summary are accepted.
SUMMARY_NUMBER = r"[-+]?(?:\d{1,3}(?:[, '\u00a0]\d{3})+|\d+)(?:\.\d*)?(?:[eE][-+]?\d+)?|[-+]?\.\d+(?:[eE][-+]?\d+)?"
ALIASES = {
    "IPC": r"IPC",
    "cycle": r"cycle(?:\s*[_-]?\s*(?:cnt|count))?|cycles|total\s+(?:guest\s+)?cycles",
    "instrcount": r"instr(?:\s*[_-]?\s*(?:cnt|count))|instructions|total\s+(?:guest\s+)?instructions",
    "Guest cycle spent": r"Guest\s+cycle\s+spent",
    "Host time spent (ms)": r"Host\s+time\s+spent",
}
PATTERNS = {name: re.compile(r"(?<![\w.])(?:" + aliases + r")\s*(?:[=:]\s*|\s+)(" + SUMMARY_NUMBER +
                            (r")(?:\s*ms)?(?![\w.])" if name == "Host time spent (ms)" else r")(?![\w.])"), re.I)
            for name, aliases in ALIASES.items()}
CORE = re.compile(r"\b(?:core|cpu)\s*(?:\[\s*)?(\d+)\s*\]?\s*:", re.I)
VISIBLE_ANSI = re.compile(r"\^\[\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class SummaryValue:
    value: str
    line: int
    raw: str
    derived: bool = False


def read_summary(path, encoding="utf-8"):
    """Keep the last explicit value per metric/core; never sum dumps or PERF values."""
    values = {}
    if path is None:
        return values
    core = ""
    with Path(path).open(encoding=encoding, errors="replace") as stream:
        for number, raw in enumerate(stream, 1):
            clean = VISIBLE_ANSI.sub("", ANSI.sub("", raw)).lstrip("\ufeff").strip()
            if PERF.search(clean):
                continue
            match = CORE.search(clean)
            if match:
                core = "Core " + match.group(1)
            for metric, pattern in PATTERNS.items():
                for match in pattern.finditer(clean):
                    text = re.sub(r"[, '\u00a0]", "", match.group(1))
                    values[core, metric] = SummaryValue(text, number, raw.rstrip("\r\n"))
    for core in {key[0] for key in values}:
        instructions, cycles = values.get((core, "instrcount")), values.get((core, "cycle"))
        if (core, "IPC") not in values and instructions and cycles and Decimal(cycles.value) > 0:
            with localcontext() as context:
                context.prec = 80
                ipc = Decimal(instructions.value) / Decimal(cycles.value)
                values[core, "IPC"] = SummaryValue(display_decimal(ipc.quantize(Decimal("0.000001"))),
                    0, f"由 instrcount / cycle 计算\n{instructions.raw}\n{cycles.raw}", True)
    return values


def summary_rows(current, baseline, comparing=False):
    cores = sorted({key[0] for key in current} | {key[0] for key in baseline}) or [""]
    rows = []
    for core in cores:
        metrics = ("IPC", "cycle", "instrcount") + tuple(name for name in ("Guest cycle spent", "Host time spent (ms)")
                                                         if (core, name) in current or (core, name) in baseline)
        for metric in metrics:
            c, b = current.get((core, metric)), baseline.get((core, metric))
            delta, percent, direction = compare_values(c.value if c else None, b.value if b else None)
            if not c and not b:
                direction = "未识别"
            rows.append({"metric": (core + " · " if core else "") + metric,
                         "value": c.value if c else "—", "baseline_value": b.value if b else "—",
                         "delta": delta if comparing else "—", "percent": percent if comparing else "—",
                         "direction": direction if comparing else ("已读取" if c else "未识别"),
                         "current": c, "baseline": b})
    return rows


def load_checkpoint(current_perf, baseline_perf, current_log, baseline_log, comparing=False,
                    encoding="utf-8", mode="occurrence"):
    """Build a fresh view so a failed read cannot mix two checkpoint selections."""
    current, baseline, comparison = LogStore(), LogStore(), ComparisonStore()
    try:
        for store, path in ((current, current_perf), (baseline, baseline_perf)):
            if path:
                with path.open("rb") as stream:
                    store.load(stream, path.stat().st_size, path.name, encoding)
            else:
                store.load(io.BytesIO(b""), 0, "（缺少 .perf）", encoding)
        if comparing:
            comparison.build(current, baseline, mode)
        summaries = summary_rows(read_summary(current_log, encoding), read_summary(baseline_log, encoding), comparing)
        return current, baseline, comparison, summaries
    except Exception:
        for store in (current, baseline, comparison):
            store.close()
        raise


def compare_run_ipc(current, baseline, encoding="utf-8"):
    """Read all main logs; retain unmatched files and failed reads as visible rows."""
    rows = []
    for benchmark, checkpoint in paired_keys(current, baseline):
        c_entry = current.checkpoints.get((benchmark, checkpoint))
        b_entry = baseline.checkpoints.get((benchmark, checkpoint))
        c_logs, b_logs = c_entry.logs if c_entry else (), b_entry.logs if b_entry else ()
        if len(c_logs) <= 1 and len(b_logs) <= 1:
            pairs = [("默认主日志", c_logs[0] if c_logs else None, b_logs[0] if b_logs else None)]
        else:
            c_names, b_names = {p.name: p for p in c_logs}, {p.name: p for p in b_logs}
            pairs = [(name, c_names.get(name), b_names.get(name)) for name in sorted(c_names.keys() | b_names.keys())]
        for name, c_path, b_path in pairs:
            data, errors = {}, {}
            for side, path in (("current", c_path), ("baseline", b_path)):
                try:
                    data[side] = read_summary(path, encoding)
                except OSError as error:
                    data[side] = {}
                    errors[side] = str(error)
            c_data, b_data = data["current"], data["baseline"]
            cores = sorted({key[0] for key in c_data} | {key[0] for key in b_data}) or [""]
            for core in cores:
                c, b = c_data.get((core, "IPC")), b_data.get((core, "IPC"))
                notes = []
                for label, side, path, value in (("当前", "current", c_path, c), ("基准", "baseline", b_path, b)):
                    if side in errors:
                        notes.append(label + "读取失败：" + errors[side])
                    elif path is None:
                        notes.append(label + "缺少主日志")
                    elif value is None:
                        notes.append(label + "未识别该 core 的 IPC")
                    elif value.derived:
                        notes.append(label + "IPC 由 instrcount/cycle 计算")
                delta, percent, direction = compare_values(c.value if c else None, b.value if b else None)
                if (c_path and c is None) or (b_path and b is None) or (c is None and b is None):
                    delta, percent, direction = "—", "—", "无法比较"
                c_instr, b_instr = c_data.get((core, "instrcount")), b_data.get((core, "instrcount"))
                if c_instr and b_instr and Decimal(c_instr.value) != Decimal(b_instr.value):
                    notes.append("两边 instrcount 不同")
                if len(c_logs) > 1 or len(b_logs) > 1:
                    notes.append("多份日志按文件名配对")
                rows.append({"benchmark": benchmark, "checkpoint": checkpoint, "log": name, "core": core or "（未标注）",
                             "baseline_value": b.value if b else "—", "value": c.value if c else "—",
                             "delta": delta, "percent": percent, "direction": direction, "note": "；".join(notes),
                             "current_log": c_path, "baseline_log": b_path, "current": c, "baseline": b,
                             "current_instr": c_instr, "baseline_instr": b_instr,
                             "current_cycle": c_data.get((core, "cycle")), "baseline_cycle": b_data.get((core, "cycle"))})
    return rows


IPC_FIELDS = ("benchmark", "checkpoint", "log", "core", "baseline_value", "value", "delta", "percent", "direction", "note",
              "baseline_log", "current_log")
IPC_HEADERS = ("子项", "checkpoint", "日志配对", "core", "基准 IPC", "当前 IPC", "差值", "变化百分比", "方向", "备注",
               "基准主日志", "当前主日志")


def export_ipc_comparison(rows, destination, sources=()):
    rows = list(rows)
    target = Path(destination)
    paths = [row[name] for row in rows for name in ("current_log", "baseline_log")] + list(sources)
    for source in paths:
        if source and (target.resolve() == source.resolve() or (target.exists() and source.exists() and target.samefile(source))):
            raise ValueError("不能覆盖 IPC 对比的主日志，请选择其他导出文件名")
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(IPC_HEADERS)
        for row in rows:
            writer.writerow([row[name] for name in IPC_FIELDS])
    return len(rows)
