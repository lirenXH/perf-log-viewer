"""Browse SPEC result directories and read simulator summaries without networking."""

import io
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
