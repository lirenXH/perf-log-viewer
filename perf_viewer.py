#!/usr/bin/env python3
"""A dependency-free, local performance-counter log viewer."""

import argparse
import codecs
import csv
import io
import json
import re
import sqlite3
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
PERF = re.compile(r"\[\s*PERF\s*\]", re.IGNORECASE)
HIERARCHY = re.compile(r"\b((?:TOP|top)\.[\w.$\[\]-]+)\s*:\s*")
MODULE = re.compile(r"^\s*\[([^\]]+)\]\s*")
NUMBER = r"[-+]?(?:0[xX][0-9a-fA-F]+|(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)(?:\s*%)?"
COUNTER = re.compile(r"^(.*?)\s*,\s*(" + NUMBER + r")\s*$")
UNLABELLED = "（未标注模块）"


def parse_counter(raw, line_number):
    """Keep each PERF record separately; never sum repeated counter dumps."""
    clean = ANSI.sub("", raw).lstrip("\ufeff")
    perf = PERF.search(clean)
    if not perf:
        return None
    payload = clean[perf.end():].strip()
    hierarchy = HIERARCHY.search(payload)
    path = hierarchy.group(1) if hierarchy else ""
    if hierarchy:
        payload = payload[hierarchy.end():]
    else:
        payload = re.sub(r"^\s*\[time\s*=[^\]]*\]\s*", "", payload)
        payload = re.sub(r"^\s*\d+\s*:\s*", "", payload)
    match = MODULE.match(payload)
    module = match.group(1).strip() if match else UNLABELLED
    if match:
        payload = payload[match.end():]
    counter = COUNTER.match(payload.strip())
    metric, value = (counter.group(1).strip(), counter.group(2).strip()) if counter else (payload.strip(), "")
    search = " ".join((module, path, metric, value, clean)).casefold()
    return (line_number, module, path, metric, value, raw, search)


class LogStore:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="perf-log-viewer-")
        self.lock = threading.RLock()
        self.connection = None
        self.info = {"loaded": False, "filename": "", "total_lines": 0, "records": 0, "modules": []}
        self.serial = 0

    def load(self, stream, length, filename, encoding="utf-8"):
        if encoding not in ("utf-8", "gb18030"):
            raise ValueError("仅支持 UTF-8 或 GB18030 编码")
        # Import into a new database; a failed upload leaves the previous log intact.
        fd, database_path = tempfile.mkstemp(suffix=".sqlite", dir=self.directory.name)
        import os
        os.close(fd)
        connection = sqlite3.connect(database_path, check_same_thread=False)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("CREATE TABLE counters (line INTEGER, module TEXT, path TEXT, metric TEXT, value TEXT, raw TEXT, search TEXT)")
            decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            pending, batch, line_number, records, replacements = "", [], 0, 0, 0

            def consume(line):
                nonlocal line_number, records, replacements
                line_number += 1
                line = line.rstrip("\r")
                replacements += line.count("\ufffd")
                row = parse_counter(line, line_number)
                if row is not None:
                    batch.append(row)
                    records += 1
                if len(batch) >= 2000:
                    connection.executemany("INSERT INTO counters VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
                    batch.clear()

            remaining = length
            while remaining > 0:
                data = stream.read(min(1024 * 1024, remaining))
                if not data:
                    raise ValueError("文件上传不完整，请重新打开文件")
                remaining -= len(data)
                pending += decoder.decode(data)
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    consume(line)
            pending += decoder.decode(b"", final=True)
            if pending:
                consume(pending)
            if batch:
                connection.executemany("INSERT INTO counters VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
            connection.execute("CREATE INDEX module_index ON counters(module, line)")
            connection.execute("CREATE INDEX line_index ON counters(line)")
            connection.commit()
            modules = [{"name": row[0], "count": row[1]} for row in connection.execute(
                "SELECT module, COUNT(*) FROM counters GROUP BY module ORDER BY module")]
            with self.lock:
                previous = self.connection
                previous_path = previous.execute("PRAGMA database_list").fetchone()[2] if previous else None
                self.connection = connection
                self.serial += 1
                self.info = {"loaded": True, "filename": Path(filename).name, "total_lines": line_number,
                             "records": records, "modules": modules, "replacement_chars": replacements,
                             "serial": self.serial}
                if previous:
                    previous.close()
                    Path(previous_path).unlink(missing_ok=True)
                return dict(self.info)
        except Exception:
            connection.close()
            Path(database_path).unlink(missing_ok=True)
            raise

    @staticmethod
    def filters(params):
        clauses, values = [], []
        module = params.get("module", [""])[0]
        if module:
            clauses.append("module = ?")
            values.append(module)
        # Space-separated words use AND. Quoted phrases are one search term.
        query = params.get("q", [""])[0]
        words = re.findall(r'"([^"]+)"|(\S+)', query)
        for quoted, word in words:
            clauses.append("instr(search, ?) > 0")
            values.append((quoted or word).casefold())
        path = params.get("path", [""])[0]
        if path:
            clauses.append("instr(lower(path), ?) > 0")
            values.append(path.lower())
        for name, operator in (("start", ">="), ("end", "<=")):
            raw = params.get(name, [""])[0].strip()
            if raw:
                number = int(raw)
                if number < 1:
                    raise ValueError("行号必须大于零")
                clauses.append("line " + operator + " ?")
                values.append(number)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", values

    def query(self, params):
        where, values = self.filters(params)
        page = max(1, int(params.get("page", ["1"])[0]))
        limit = min(500, max(1, int(params.get("limit", ["100"])[0])))
        with self.lock:
            if not self.connection:
                return {"rows": [], "total": 0, "page": 1, "pages": 1, "serial": 0}
            total = self.connection.execute("SELECT COUNT(*) FROM counters" + where, values).fetchone()[0]
            pages = max(1, (total + limit - 1) // limit)
            page = min(page, pages)
            rows = self.connection.execute(
                "SELECT line, module, path, metric, value, raw FROM counters" + where +
                " ORDER BY line LIMIT ? OFFSET ?", values + [limit, (page - 1) * limit]).fetchall()
            return {"rows": [dict(zip(("line", "module", "path", "metric", "value", "raw"), row)) for row in rows],
                    "total": total, "page": page, "pages": pages, "serial": self.serial}

    def iter_rows(self, params):
        """Stream all matching rows, without the display's pagination limit."""
        where, values = self.filters(params)
        with self.lock:
            if self.connection is None:
                raise ValueError("请先加载日志")
            rows = self.connection.execute(
                "SELECT line, module, path, metric, value, raw FROM counters" + where + " ORDER BY line", values)
            yield from rows

    def close(self):
        with self.lock:
            if self.connection:
                self.connection.close()
            self.directory.cleanup()


def export_to_file(store, params, destination, kind, source=None):
    """Export every match; refuse to overwrite the source, including hard links."""
    if kind not in ("csv", "log"):
        raise ValueError("不支持的导出格式")
    target = Path(destination)
    if source is not None:
        source = Path(source)
        if target.resolve() == source.resolve() or (target.exists() and source.exists() and target.samefile(source)):
            raise ValueError("导出路径不能是当前打开的原始日志，请选择其他文件名")
    store.filters(params)
    count = 0
    with target.open("w", encoding="utf-8-sig" if kind == "csv" else "utf-8", newline="") as output:
        if kind == "csv":
            writer = csv.writer(output)
            writer.writerow(["原始行号", "模块", "层级路径", "计数器名称", "数值", "原文"])
            for row in store.iter_rows(params):
                writer.writerow(row)
                count += 1
        else:
            for row in store.iter_rows(params):
                output.write(row[5] + "\n")
                count += 1
    return count


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>性能计数器日志查看器</title>
<style>
:root{color-scheme:light;--ink:#202b3e;--muted:#62718b;--blue:#235aca;--border:#dce3ee}
*{box-sizing:border-box}body{margin:0;font:14px system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;background:#f3f6fb;color:var(--ink)}
header{padding:24px 30px;background:white;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
h1{margin:0 0 6px;font-size:23px}p{margin:0;color:var(--muted)}button,.file-button{cursor:pointer;border:1px solid var(--border);border-radius:8px;background:white;color:var(--ink);padding:9px 13px;font:inherit}
button:hover,.file-button:hover{background:#eef3ff}button:disabled{opacity:.45;cursor:default}.primary{background:var(--blue);color:white;border-color:var(--blue)}.primary:hover{background:#194bac}
input,select{border:1px solid var(--border);border-radius:7px;padding:9px 10px;font:inherit;background:white;color:var(--ink);min-width:0}input:focus,select:focus,button:focus-visible{outline:2px solid #8eb4ff;outline-offset:2px}
.open-tools{display:flex;align-items:center;gap:10px}.workspace{display:grid;grid-template-columns:290px minmax(0,1fr);gap:18px;padding:20px 30px}
aside,.content{background:white;border:1px solid var(--border);border-radius:12px;overflow:hidden}aside{align-self:start;position:sticky;top:16px}.aside-head{padding:16px;border-bottom:1px solid var(--border)}h2{font-size:15px;margin:0 0 12px}.aside-head input{width:100%}
.module-list{max-height:65vh;overflow:auto;padding:8px}.module{display:flex;width:100%;text-align:left;justify-content:space-between;gap:8px;border:0;padding:11px 9px;align-items:start;margin:2px 0}.module span{overflow-wrap:anywhere}.module small{white-space:nowrap;color:var(--muted);padding-top:2px}.module.active{background:#eaf0ff;color:#1749ac;font-weight:600}
.filters{padding:18px;display:grid;grid-template-columns:minmax(180px,2fr) minmax(130px,1fr);gap:12px}.field{display:flex;flex-direction:column;gap:6px}.field label{font-size:12px;color:var(--muted)}.range-tools{grid-column:1/-1;display:flex;gap:12px;flex-wrap:wrap;align-items:end}.range-tools input{width:125px}.hint{font-size:12px;color:var(--muted);grid-column:1/-1}
.summary{padding:12px 18px;border-top:1px solid var(--border);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.selection{font-size:12px;color:var(--muted);margin-top:5px;overflow-wrap:anywhere}.exports{display:flex;gap:8px}
.table-wrap{overflow:auto;max-height:65vh}table{width:100%;border-collapse:collapse;text-align:left}th{position:sticky;top:0;background:#f7f9fd;z-index:1;color:var(--muted);font-size:12px}th,td{padding:11px 14px;border-bottom:1px solid #edf0f6;vertical-align:top}td.metric{min-width:250px;overflow-wrap:anywhere}td.value{text-align:right;font-family:ui-monospace,monospace;font-weight:600;white-space:nowrap}td.source{max-width:350px;font-size:12px;color:var(--muted);overflow-wrap:anywhere}td.line{white-space:nowrap;font-family:ui-monospace,monospace;color:var(--muted)}tr:hover{background:#f8faff}summary{cursor:pointer;color:var(--blue)}pre{font:12px ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-width:600px;margin:8px 0 0}
.pager{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;gap:12px}.empty{padding:55px 24px;text-align:center;color:var(--muted)}.status{padding:12px 30px;font-size:13px;overflow-wrap:anywhere}.status.error{color:#a42424;background:#fff0f0}.note{padding:0 30px 20px;font-size:12px;color:var(--muted)}
@media(max-width:850px){header{padding:18px}.workspace{padding:12px;grid-template-columns:1fr}aside{position:static}.module-list{max-height:180px}.filters{grid-template-columns:1fr}.status,.note{padding-left:18px;padding-right:18px}.open-tools{flex-wrap:wrap}}
</style></head><body>
<header><div><h1>性能计数器日志查看器</h1><p>按模块定位计数器，保留每一次输出及其原始行号。</p></div>
<div class="open-tools"><select id="encoding" aria-label="日志编码"><option value="utf-8">UTF-8</option><option value="gb18030">GB18030 / GBK</option></select><label class="file-button primary" for="file">打开日志文件</label><input id="file" type="file" hidden></div></header>
<div id="status" class="status" role="status" aria-live="polite">请选择本机的日志文件。文件只由本机服务处理。</div>
<main class="workspace"><aside><div class="aside-head"><h2>模块 <span id="module-count"></span></h2><input id="module-search" placeholder="筛选模块名称…" aria-label="筛选模块名称"></div><div id="modules" class="module-list"></div></aside>
<section class="content"><div class="filters"><div class="field"><label for="q">关键字搜索</label><input id="q" placeholder='例如：prefetch accepted，或 "Useful prefetch"'></div><div class="field"><label for="path">层级路径包含</label><input id="path" placeholder="例如：l2Cache.l2PfMonitor"></div>
<div class="hint">搜索不区分大小写；多个词同时满足；双引号用于短语。范围包括名称、模块、路径、数值和原文。</div>
<div class="range-tools"><div class="field"><label for="start">起始原始行号</label><input id="start" type="number" min="1" placeholder="不限"></div><div class="field"><label for="end">结束原始行号</label><input id="end" type="number" min="1" placeholder="不限"></div><button id="clear">清除筛选</button></div></div>
<div class="summary"><div><strong id="matches">尚未加载日志</strong><div id="selected" class="selection">全部模块</div></div><div class="exports"><button id="csv" disabled>导出 CSV</button><button id="raw" disabled>导出日志</button></div></div>
<div class="table-wrap"><table id="table" hidden><thead><tr><th>原始行号</th><th>计数器名称</th><th style="text-align:right">数值</th><th>模块 / 层级路径</th><th>原文</th></tr></thead><tbody id="rows"></tbody></table><div id="empty" class="empty">打开日志后，左侧会列出方括号内的模块名称。</div></div>
<div class="pager"><div><button id="previous" disabled>上一页</button> <span id="page">1 / 1</span> <button id="next" disabled>下一页</button></div><label>每页 <select id="limit"><option>100</option><option>200</option><option>500</option></select> 条</label></div></section></main>
<div class="note">只解析 [PERF] 行。[PERF] 是日志类型，其后的 [模块名] 是筛选标签；没有模块标签的行归入“未标注模块”。同名条目保留，数值不会自动求和。行号范围可用于隔离预热段或某一轮 dump。</div>
<script>
const $=id=>document.getElementById(id);let info={loaded:false,modules:[]},moduleName='',currentPage=1,pages=1,requestID=0,controller=null,timer=null;
const format=n=>Number(n).toLocaleString('zh-CN');
function status(message,error=false){$('status').textContent=message;$('status').className='status'+(error?' error':'');}
function params(){const p=new URLSearchParams({module:moduleName,q:$('q').value,path:$('path').value,start:$('start').value,end:$('end').value,page:currentPage,limit:$('limit').value});return p;}
function cell(row,text,className=''){const td=document.createElement('td');td.textContent=text;td.className=className;row.append(td);return td;}
function renderModules(){const needle=$('module-search').value.trim().toLocaleLowerCase();$('modules').replaceChildren();const all=[{name:'',label:'全部模块',count:info.records||0},...info.modules];for(const item of all){if(item.name&&!item.name.toLocaleLowerCase().includes(needle))continue;const b=document.createElement('button');b.className='module'+(moduleName===item.name?' active':'');b.setAttribute('aria-pressed',moduleName===item.name);const name=document.createElement('span'),count=document.createElement('small');name.textContent=item.label||item.name;count.textContent=format(item.count);b.append(name,count);b.onclick=()=>{moduleName=item.name;currentPage=1;renderModules();refresh();};$('modules').append(b);}$('module-count').textContent=info.loaded?'('+info.modules.length+')':'';}
async function refresh(){if(!info.loaded)return;const id=++requestID;if(controller)controller.abort();controller=new AbortController();try{const response=await fetch('/api/rows?'+params(),{signal:controller.signal});const data=await response.json();if(!response.ok)throw Error(data.error);if(id!==requestID||data.serial!==info.serial)return;currentPage=data.page;pages=data.pages;$('rows').replaceChildren();for(const item of data.rows){const tr=document.createElement('tr');cell(tr,item.line,'line');cell(tr,item.metric,'metric');cell(tr,item.value,'value');const source=cell(tr,'','source'),name=document.createElement('div'),path=document.createElement('div');name.textContent=item.module;path.textContent=item.path;source.append(name,path);const original=cell(tr,''),details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='查看';pre.textContent=item.raw;details.append(summary,pre);original.append(details);$('rows').append(tr);}$('table').hidden=!data.rows.length;$('empty').hidden=!!data.rows.length;$('empty').textContent=info.records?'没有匹配的计数器，请调整筛选条件。':'文件中未找到 [PERF] 行。';$('matches').textContent='匹配 '+format(data.total)+' / '+format(info.records)+' 条';$('selected').textContent=moduleName||'全部模块';$('page').textContent=currentPage+' / '+pages;$('previous').disabled=currentPage<=1;$('next').disabled=currentPage>=pages;$('csv').disabled=$('raw').disabled=!data.total;}catch(error){if(error.name!=='AbortError')status(error.message,true);}}
function schedule(){currentPage=1;if(controller)controller.abort();clearTimeout(timer);timer=setTimeout(refresh,200);}
function adopt(data){info=data;moduleName='';currentPage=1;for(const id of ['q','path','start','end','module-search'])$(id).value='';renderModules();status('已加载 '+data.filename+' · 原文件 '+format(data.total_lines)+' 行 · '+format(data.records)+' 条 PERF 记录'+(data.replacement_chars?' · 检测到无法解码字符，可尝试切换编码后重新打开':''));refresh();}
$('file').onchange=async event=>{const file=event.target.files[0];if(!file)return;++requestID;if(controller)controller.abort();$('file').disabled=true;status('正在读取并解析 '+file.name+'（'+(file.size/1048576).toFixed(1)+' MB）…');try{const response=await fetch('/api/load?encoding='+$('encoding').value,{method:'POST',headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(file.name)},body:file});const data=await response.json();if(!response.ok)throw Error(data.error);adopt(data);}catch(error){status('加载失败：'+error.message,true);}finally{$('file').disabled=false;event.target.value='';}};
for(const id of ['q','path','start','end'])$(id).oninput=schedule;$('module-search').oninput=renderModules;$('limit').onchange=()=>{currentPage=1;refresh();};$('clear').onclick=()=>{moduleName='';for(const id of ['q','path','start','end','module-search'])$(id).value='';currentPage=1;renderModules();refresh();};$('previous').onclick=()=>{currentPage--;refresh();};$('next').onclick=()=>{currentPage++;refresh();};
for(const kind of ['csv','raw'])$(kind).onclick=()=>{window.location.href='/api/export?'+params()+'&format='+kind;};
fetch('/api/info').then(r=>r.json()).then(data=>{if(data.loaded)adopt(data);else renderModules();}).catch(error=>status('无法连接本机服务：'+error.message,true));
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query, keep_blank_values=True)
        try:
            if url.path == "/":
                self.send_bytes(HTML.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/info":
                with self.server.store.lock:
                    self.send_json(self.server.store.info)
            elif url.path == "/api/rows":
                self.send_json(self.server.store.query(params))
            elif url.path == "/api/export":
                self.export(params)
            elif url.path == "/favicon.ico":
                self.send_bytes(b"", "image/x-icon", 204)
            else:
                self.send_json({"error": "路径不存在"}, 404)
        except (ValueError, sqlite3.Error) as error:
            self.send_json({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/load":
            self.send_json({"error": "路径不存在"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0:
                raise ValueError("缺少文件长度")
            encoding = parse_qs(url.query).get("encoding", ["utf-8"])[0]
            filename = unquote(self.headers.get("X-File-Name", "log.txt"))
            self.send_json(self.server.store.load(self.rfile, length, filename, encoding))
        except (ValueError, OSError, sqlite3.Error) as error:
            self.send_json({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def export(self, params):
        kind = params.get("format", ["csv"])[0]
        if kind not in ("csv", "raw"):
            raise ValueError("不支持的导出格式")
        where, values = self.server.store.filters(params)
        with self.server.store.lock:
            connection = self.server.store.connection
            if connection is None:
                raise ValueError("请先加载日志")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8" if kind == "csv" else "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="perf-filtered.' + ("csv" if kind == "csv" else "log") + '"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            rows = connection.execute("SELECT line, module, path, metric, value, raw FROM counters" + where + " ORDER BY line", values)
            if kind == "csv":
                self.wfile.write(codecs.BOM_UTF8)
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow(["原始行号", "模块", "层级路径", "计数器名称", "数值", "原文"])
                self.wfile.write(buffer.getvalue().encode())
                for row in rows:
                    buffer.seek(0)
                    buffer.truncate(0)
                    writer.writerow(row)
                    self.wfile.write(buffer.getvalue().encode())
            else:
                for row in rows:
                    self.wfile.write((row[5] + "\n").encode())

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="本地性能计数器日志查看器，无需第三方 Python 依赖")
    parser.add_argument("log", nargs="?", help="可选：启动时打开的日志路径")
    parser.add_argument("--port", type=int, default=8765, help="本机服务端口，默认 8765；0 表示自动选择")
    parser.add_argument("--encoding", choices=("utf-8", "gb18030"), default="utf-8")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务，不自动打开浏览器")
    args = parser.parse_args()
    store = LogStore()
    server = None
    try:
        if args.log:
            path = Path(args.log)
            with path.open("rb") as stream:
                store.load(stream, path.stat().st_size, path.name, args.encoding)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.store = store
        url = "http://127.0.0.1:" + str(server.server_port)
        print("日志查看器：" + url, flush=True)
        print("按 Ctrl+C 停止。原日志文件不会被修改。", flush=True)
        if not args.no_browser:
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    except (OSError, ValueError) as error:
        parser.exit(1, "启动失败：" + str(error) + "\n可尝试 --port 0 自动选择端口。\n")
    finally:
        if server:
            server.server_close()
        store.close()


if __name__ == "__main__":
    main()
