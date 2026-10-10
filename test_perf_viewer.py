import io
import csv
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from perf_viewer import Handler, LogStore, UNLABELLED, parse_counter


class ParserTests(unittest.TestCase):
    def test_sample_zero_and_path(self):
        row = parse_counter("[PERF] TOP.SapphireCore.core.l2Cache.l2PfMonitor: [L2 SMS Prefetcher Monitor] Useful prefetch,   0", 17)
        self.assertEqual(row[:5], (17, "L2 SMS Prefetcher Monitor", "TOP.SapphireCore.core.l2Cache.l2PfMonitor", "Useful prefetch", "0"))

    def test_ansi_and_unlabelled_xiangshan(self):
        row = parse_counter("\x1b[32m[PERF ][time=123] 9527: commitInstr, 20000000\x1b[0m", 2)
        self.assertEqual(row[1:5], (UNLABELLED, "", "commitInstr", "20000000"))

    def test_numeric_formats_and_commas_in_name(self):
        for number in ("-12", "1.25", "2.1e+6", "0x123", "1,234,567", "12.5%"):
            row = parse_counter("[PERF] [模块] Request, accepted, " + number, 1)
            self.assertEqual(row[3:5], ("Request, accepted", number))

    def test_unrecognised_value_kept(self):
        self.assertIsNone(parse_counter("[INFO] Linux ready", 1))
        self.assertEqual(parse_counter("[PERF] [模块] not numeric", 2)[3:5], ("not numeric", ""))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = LogStore()
        self.addCleanup(self.store.close)
        self.log = ("Boot\r\n"
                    "[PERF] TOP.core.l2: [SMS] Useful prefetch, 0\r\n"
                    "Warmup finished\r\n"
                    "[PERF] TOP.core.l2: [SMS] Useful prefetch, 42\r\n"
                    "[PERF] TOP.core.rob: [ROB] commitInstr, 20000000\r\n").encode()
        self.info = self.store.load(io.BytesIO(self.log), len(self.log), "sample.log")

    def test_duplicates_zero_and_line_numbers(self):
        self.assertEqual((self.info["total_lines"], self.info["records"]), (5, 3))
        result = self.store.query({"module": ["SMS"]})
        self.assertEqual([(row["line"], row["value"]) for row in result["rows"]], [(2, "0"), (4, "42")])

    def test_combined_search_range_and_pagination(self):
        result = self.store.query({"q": ['"USEFUL PREFETCH" 42'], "path": ["CORE.L2"], "start": ["3"]})
        self.assertEqual([row["line"] for row in result["rows"]], [4])
        result = self.store.query({"limit": ["1"], "page": ["2"]})
        self.assertEqual((result["total"], result["pages"], result["rows"][0]["line"]), (3, 3, 4))

    def test_literal_search_and_failed_import_preserves_old_log(self):
        self.assertEqual(self.store.query({"q": ["%' OR 1=1"]})["total"], 0)
        with self.assertRaises(ValueError):
            self.store.load(io.BytesIO(b"partial"), 100, "bad.log")
        self.assertEqual(self.store.query({})["total"], 3)

    def test_gbk_and_final_line_without_newline(self):
        data = "[PERF] [缓存] 命中, 0".encode("gb18030")
        info = self.store.load(io.BytesIO(data), len(data), "gbk.log", "gb18030")
        self.assertEqual((info["total_lines"], info["records"]), (1, 1))
        self.assertEqual(self.store.query({"q": ["命中"]})["rows"][0]["value"], "0")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.store = LogStore()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.server.store.close()

    def test_upload_filter_and_export_all_matching_rows(self):
        data = ("Ignored\n[PERF] TOP.core.l2: [SMS] Useful prefetch, 0\n"
                "[PERF] TOP.core.l2: [SMS] Useful prefetch, 42\n"
                "[PERF] TOP.core.rob: [ROB] commitInstr, 20000000\n").encode()
        request = Request(self.base + "/api/load", data=data,
                          headers={"X-File-Name": "uploaded.log", "Content-Type": "application/octet-stream"})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["records"], 3)
        query = urlencode({"module": "SMS", "q": '"useful prefetch"', "limit": 1})
        with urlopen(self.base + "/api/rows?" + query) as response:
            result = json.load(response)
            self.assertEqual((result["total"], len(result["rows"])), (2, 1))
        with urlopen(self.base + "/api/export?" + query + "&format=csv") as response:
            rows = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
            self.assertEqual([row[4] for row in rows[1:]], ["0", "42"])
        with urlopen(self.base + "/api/export?" + query + "&format=raw") as response:
            self.assertEqual(len(response.read().decode().splitlines()), 2)
        with urlopen(self.base + "/") as response:
            self.assertIn("打开日志文件", response.read().decode())


if __name__ == "__main__":
    unittest.main()
