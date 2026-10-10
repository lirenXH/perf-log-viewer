# 性能计数器日志查看器

适用于 Ubuntu 22.04 和 Python 3.10+。仅使用 Python 标准库，无需 pip、Node.js 或联网下载依赖。

## 启动

```bash
cd perf-log-viewer
python3 perf_viewer.py
```

浏览器通常会自动打开；也可以手动访问 http://127.0.0.1:8765 。点击“打开日志文件”选择日志。

启动时直接加载日志：

```bash
python3 perf_viewer.py /path/to/perf.log
```

体验自带示例：

```bash
python3 perf_viewer.py sample.log
```

端口冲突时使用 `--port 8766`，或 `--port 0` 自动分配端口。`--no-browser` 不自动打开浏览器。
按 Ctrl+C 停止服务。原日志不修改，临时 SQLite 索引在程序退出时清理。

## 在其他服务器上使用

只需要复制 `perf_viewer.py`；网页界面已包含在脚本中。服务器需要 Python 3.10 或更高版本。

在服务器上启动并加载服务器端日志：

```bash
python3 perf_viewer.py /path/to/perf.log --no-browser
```

在本机建立 SSH 隧道：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
```

然后在本机浏览器打开 http://127.0.0.1:8765 。服务仅监听服务器的本机回环地址。
网页“打开日志文件”选择的是浏览器所在机器的文件；服务器已有的日志可以用启动参数加载。

## 筛选与导出

- 左侧按 `[L2 SMS Prefetcher Monitor]` 等模块标签筛选，也支持搜索模块名称。
- 顶部关键字搜索覆盖模块、路径、计数器名称、值和原文，不区分大小写。
- 多个空格分隔的关键词按 AND 匹配，例如 `prefetch accepted`。
- 双引号表示连续短语，例如 `"Useful prefetch"`。
- “层级路径包含”能进一步区分同名模块所在的不同 bank 或实例。
- 原始行号范围可隔离预热段、某一轮 dump；边界行号包含在结果中。
- 导出 CSV 或原始日志行，导出的是所有匹配项，不只是当前页。
- 同名计数器的多次输出分别保留，不求和、不覆盖，零值也保留。
- UTF-8 是默认编码；旧 GBK 日志选择 GB18030 后重新打开。

## 日志格式

```text
[PERF] TOP.SapphireCore.core.l2CacheWrapper.l2Cache.l2PfMonitor: [L2 SMS Prefetcher Monitor] Useful prefetch, 0
```

解析成模块 `L2 SMS Prefetcher Monitor`、名称 `Useful prefetch`、数值 `0`、层级路径和原始行号。
支持 `[PERF ]` 空格、ANSI 终端颜色、CRLF、负数、小数、科学计数法、十六进制及千分位数值。
未带模块标签的 PERF 行归入“未标注模块”；未识别数值的 PERF 行仍然保留，值列留空。
其他日志行不展示，但计入原始行号。该工具不判断计数器的统计口径，也不自动计算 MPKI。

文件在本机 Python 服务中分块解析，使用 SQLite 存储索引，页面分页显示，无需把所有记录渲染到浏览器。
全日志关键字搜索需要扫描记录，超大文件的首次导入和搜索可能需要等待。

## 验证

```bash
python3 -m unittest -v
```
