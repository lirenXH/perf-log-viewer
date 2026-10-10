"""Exact counter comparisons backed by a local SQLite database."""

import csv
import os
import re
import sqlite3
import tempfile
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from perf_viewer import LogStore, NUMBER


FIELDS = ("line", "baseline_line", "module", "path", "metric", "occurrence", "value", "baseline_value",
          "delta", "percent", "direction", "raw", "baseline_raw", "search")
CSV_FIELDS = ("module", "path", "metric", "occurrence", "baseline_value", "value", "delta", "percent",
              "direction", "baseline_line", "line", "baseline_raw", "raw")
CSV_HEADERS = ("模块", "层级路径", "计数器名称", "出现次序", "基准值", "当前值", "差值", "变化百分比",
               "变化方向", "基准行号", "当前行号", "基准原文", "当前原文")


def decimal_value(text):
    if text is None or not re.fullmatch(NUMBER, text.strip()):
        return None
    text = text.strip()
    unit = "%" if text.endswith("%") else ""
    numeric = text.rstrip("%").strip().replace(",", "")
    if numeric.lstrip("+-").lower().startswith("0x"):
        value = Decimal(int(numeric, 16))
    else:
        value = Decimal(numeric)
    return value, unit


def display_decimal(value, signed=False):
    if value and (value.adjusted() > 80 or value.adjusted() < -12):
        text = format(value, "E")
    else:
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
    return ("+" if signed and value > 0 else "") + text


def compare_values(current, baseline):
    if current is None:
        return "—", "—", "缺失"
    if baseline is None:
        return "—", "—", "新增"
    c, b = decimal_value(current), decimal_value(baseline)
    if c is None or b is None:
        return "—", "—", "非数值"
    if c[1] != b[1]:
        return "—", "—", "单位不同"
    with localcontext() as context:
        # Integer counters up to uint64 and decimal inputs never pass through float.
        context.prec = max(80, len(c[0].as_tuple().digits), len(b[0].as_tuple().digits),
                           min(4096, abs(c[0].adjusted() - b[0].adjusted()) + 32))
        delta = c[0] - b[0]
        direction = "上升" if delta > 0 else "下降" if delta < 0 else "不变"
        delta_text = display_decimal(delta, signed=True) + (" 个百分点" if c[1] == "%" else "")
        if b[0] == 0:
            percent = "—（基准为0）"
        else:
            change = delta / abs(b[0]) * 100
            if abs(change) >= Decimal("1e50"):
                percent = display_decimal(change, signed=True) + "%"
            else:
                rounded = change.quantize(Decimal("0.01"))
                if rounded == 0:
                    rounded = abs(rounded)
                percent = ("+" if rounded > 0 else "") + format(rounded, ".2f") + "%"
        return delta_text, percent, direction


class ComparisonStore(LogStore):
    def build(self, current, baseline, mode="occurrence"):
        if mode not in ("occurrence", "last"):
            raise ValueError("不支持的重复计数器匹配方式")
        fd, filename = tempfile.mkstemp(suffix=".sqlite", dir=self.directory.name)
        os.close(fd)
        connection = sqlite3.connect(filename, check_same_thread=False)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            with current.lock, baseline.lock:
                if current.connection is None or baseline.connection is None:
                    raise ValueError("请先打开当前日志和基准日志")
                for alias, store in (("current_db", current), ("baseline_db", baseline)):
                    database = store.connection.execute("PRAGMA database_list").fetchone()[2]
                    connection.execute("ATTACH DATABASE ? AS " + alias, (database,))
                order = "DESC" if mode == "last" else "ASC"
                for name, alias in (("current_rank", "current_db"), ("baseline_rank", "baseline_db")):
                    sql = ("SELECT *, ROW_NUMBER() OVER (PARTITION BY module, path, metric ORDER BY line " + order +
                           ") AS occurrence FROM " + alias + ".counters")
                    if mode == "last":
                        sql = "SELECT * FROM (" + sql + ") WHERE occurrence = 1"
                    connection.execute("CREATE TABLE " + name + " AS " + sql)
                    connection.execute("CREATE UNIQUE INDEX " + name + "_keys ON " + name + "(module, path, metric, occurrence)")
                connection.execute("CREATE TABLE comparisons (line INTEGER, baseline_line INTEGER, module TEXT, path TEXT, metric TEXT, occurrence INTEGER, value TEXT, baseline_value TEXT, delta TEXT, percent TEXT, direction TEXT, raw TEXT, baseline_raw TEXT, search TEXT)")
                key = "c.module = b.module AND c.path = b.path AND c.metric = b.metric AND c.occurrence = b.occurrence"
                joined = connection.execute(
                    "SELECT c.line, b.line, c.module, c.path, c.metric, c.occurrence, c.value, b.value, c.raw, b.raw "
                    "FROM current_rank c LEFT JOIN baseline_rank b ON " + key +
                    " UNION ALL SELECT NULL, b.line, b.module, b.path, b.metric, b.occurrence, NULL, b.value, NULL, b.raw "
                    "FROM baseline_rank b LEFT JOIN current_rank c ON " + key + " WHERE c.line IS NULL")
                batch, records = [], 0
                for line, baseline_line, module, path, metric, occurrence, value, baseline_value, raw, baseline_raw in joined:
                    try:
                        delta, percent, direction = compare_values(value, baseline_value)
                    except (InvalidOperation, OverflowError, ValueError):
                        delta, percent, direction = "—", "—", "非数值"
                    search = " ".join(str(x or "") for x in (module, path, metric, value, baseline_value, delta,
                                                             percent, direction, raw, baseline_raw)).casefold()
                    batch.append((line, baseline_line, module, path, metric, occurrence, value, baseline_value,
                                  delta, percent, direction, raw, baseline_raw, search))
                    records += 1
                    if len(batch) >= 2000:
                        connection.executemany("INSERT INTO comparisons VALUES (" + ",".join(["?"] * 14) + ")", batch)
                        batch.clear()
                if batch:
                    connection.executemany("INSERT INTO comparisons VALUES (" + ",".join(["?"] * 14) + ")", batch)
                for name in ("current_rank", "baseline_rank"):
                    connection.execute("DROP TABLE " + name)
                connection.execute("CREATE INDEX comparison_module ON comparisons(module)")
                connection.commit()
                connection.execute("DETACH DATABASE current_db")
                connection.execute("DETACH DATABASE baseline_db")
            modules = [{"name": row[0], "count": row[1]} for row in connection.execute(
                "SELECT module, COUNT(*) FROM comparisons GROUP BY module ORDER BY module")]
            directions = dict(connection.execute("SELECT direction, COUNT(*) FROM comparisons GROUP BY direction"))
            with self.lock:
                previous = self.connection
                previous_path = previous.execute("PRAGMA database_list").fetchone()[2] if previous else None
                self.connection = connection
                self.serial += 1
                self.info = {"loaded": True, "records": records, "modules": modules, "serial": self.serial,
                             "mode": mode, "directions": directions}
                if previous:
                    previous.close()
                    Path(previous_path).unlink(missing_ok=True)
                return dict(self.info)
        except Exception:
            connection.close()
            Path(filename).unlink(missing_ok=True)
            raise

    @staticmethod
    def filters(params):
        where, values = LogStore.filters(params)
        direction = params.get("direction", [""])[0]
        if direction:
            where += (" AND " if where else " WHERE ")
            if direction == "有变化":
                where += "direction IN ('上升', '下降', '新增', '缺失')"
            elif direction == "无法比较":
                where += "direction IN ('非数值', '单位不同')"
            else:
                where += "direction = ?"
                values.append(direction)
        return where, values

    def query(self, params):
        where, values = self.filters(params)
        page = max(1, int(params.get("page", ["1"])[0]))
        limit = min(500, max(1, int(params.get("limit", ["100"])[0])))
        with self.lock:
            if self.connection is None:
                return {"rows": [], "total": 0, "page": 1, "pages": 1, "serial": 0}
            total = self.connection.execute("SELECT COUNT(*) FROM comparisons" + where, values).fetchone()[0]
            pages = max(1, (total + limit - 1) // limit)
            page = min(page, pages)
            rows = self.connection.execute("SELECT * FROM comparisons" + where +
                " ORDER BY COALESCE(line, baseline_line), module, path, metric, occurrence LIMIT ? OFFSET ?",
                values + [limit, (page - 1) * limit]).fetchall()
            return {"rows": [dict(zip(FIELDS, row)) for row in rows], "total": total,
                    "page": page, "pages": pages, "serial": self.serial}

    def iter_comparisons(self, params):
        where, values = self.filters(params)
        with self.lock:
            if self.connection is None:
                raise ValueError("请先打开基准日志")
            for row in self.connection.execute("SELECT * FROM comparisons" + where +
                    " ORDER BY COALESCE(line, baseline_line), module, path, metric, occurrence", values):
                yield dict(zip(FIELDS, row))


def export_comparison(store, params, destination, sources=()):
    target = Path(destination)
    for source in sources:
        if source is not None:
            source = Path(source)
            if target.resolve() == source.resolve() or (target.exists() and source.exists() and target.samefile(source)):
                raise ValueError("不能覆盖当前日志或基准日志，请选择其他导出文件名")
    store.filters(params)
    count = 0
    with target.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(CSV_HEADERS)
        for row in store.iter_comparisons(params):
            writer.writerow([row[name] for name in CSV_FIELDS])
            count += 1
    return count
