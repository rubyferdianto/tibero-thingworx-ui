import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sql_columns import order_columns, select_list_names  # noqa: E402
from thingworx_client import ThingWorxClient, ThingWorxError  # noqa: E402


class SelectListTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(select_list_names("select a, b.c, d as dd, e ee from t"), ["A", "C", "DD", "EE"])

    def test_functions_and_case(self):
        sql = "SELECT NVL(x, 0) total, COUNT(*) AS cnt, CASE WHEN a=1 THEN 'x' ELSE 'y' END flag FROM t"
        self.assertEqual(select_list_names(sql), ["TOTAL", "CNT", "FLAG"])

    def test_distinct_quoted_cte_comments(self):
        sql = 'WITH x AS (SELECT 1 a, 2 b FROM dual) SELECT DISTINCT /* hi */ a, b AS "My Col" -- c\n FROM x'
        self.assertEqual(select_list_names(sql), ["A", "My Col"])

    def test_unknown(self):
        self.assertIsNone(select_list_names("SELECT * FROM t"))
        self.assertIsNone(select_list_names("SELECT t.* FROM t"))
        self.assertIsNone(select_list_names("SELECT COUNT(*) FROM t"))        # no alias
        self.assertIsNone(select_list_names("SELECT a + b FROM t"))
        self.assertIsNone(select_list_names("SELECT 1 FROM"))

    def test_order_and_null_columns(self):
        # service order scrambled and column B was NULL in every row (omitted)
        self.assertEqual(order_columns("SELECT a, b, c FROM t", ["C", "A"]), ["A", "B", "C"])
        # unparsable -> keep service order; unmatched keys are never dropped
        self.assertEqual(order_columns("SELECT * FROM t", ["Z", "A"]), ["Z", "A"])
        self.assertEqual(order_columns("SELECT a FROM t", ["A", "EXTRA"]), ["A", "EXTRA"])


def client(**env):
    base = {"THINGWORX_URL": "http://h", "THINGWORX_APPKEY": "k", "SERVICE_NAME": "RunQueryAndGetJSON",
            "THING_NAME": '"Primary"', "HALOD_FALLBACK_THING_NAME": "Fb1", "HALO_FALLBACK_THING_NAME": "Fb1, Fb2",
            "THINGWORX_THING": "legacy", "TWX_MAX_ROWS": "5"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=True):
        return ThingWorxClient()


class ParseTests(unittest.TestCase):
    def test_json_in_result(self):
        w = {"dataShape": {}, "rows": [{"result": json.dumps([{"A": 1}, {"A": 2, "B": None}])}]}
        self.assertEqual(ThingWorxClient._parse(w), [{"A": 1}, {"A": 2, "B": None}])

    def test_status_objects(self):
        self.assertEqual(ThingWorxClient._parse({"rows": [{"result": '{"status":"SUCCESS","rowsReturned":0}'}]}), [])
        self.assertEqual(ThingWorxClient._parse({"rows": [{"result": ""}]}), [])
        with self.assertRaisesRegex(ThingWorxError, "DATABASE_ERROR: ORA-00942"):
            ThingWorxClient._parse({"rows": [{"result": '{"status":"DATABASE_ERROR","error":"ORA-00942"}'}]})

    def test_plain_infotable(self):
        w = {"dataShape": {"fieldDefinitions": {"A": {"name": "A", "baseType": "NUMBER"}}}, "rows": [{"A": 1}]}
        self.assertEqual(ThingWorxClient._parse(w), [{"A": 1}])


class ConfigTests(unittest.TestCase):
    def test_things_order_and_dedupe(self):
        c = client()
        self.assertEqual(c.things, ["Primary", "Fb1", "Fb2"])      # THING_NAME wins over THINGWORX_THING; quotes stripped
        self.assertTrue(c.configured())

    def test_legacy_names_and_twx_host(self):
        c = client(THING_NAME="", SERVICE_NAME="", THINGWORX_SERVICE="Svc", THINGWORX_URL="", TWX_HOST="http://x/", APP_KEY="a", THINGWORX_APPKEY="")
        self.assertEqual((c.things[0], c.service, c.host, c.app_key), ("legacy", "Svc", "http://x", "a"))


class RunSqlTests(unittest.TestCase):
    def test_fallback_and_combined_error(self):
        c = client()
        calls = []

        def post(thing, sql):
            calls.append(thing)
            if thing == "Fb1":
                return [{"A": 1}]
            raise ThingWorxError(f"{thing} no grant")

        with mock.patch.object(c, "_post", side_effect=post):
            self.assertEqual(c.run_sql("SELECT a FROM t;")["rows"], [[1]])
        self.assertEqual(calls, ["Primary", "Fb1"])

        with mock.patch.object(c, "_post", side_effect=ThingWorxError("boom")):
            with self.assertRaisesRegex(ThingWorxError, r"\[Primary\] boom\n\[Fb1\] boom\n\[Fb2\] boom"):
                c.run_sql("SELECT a FROM t")

    def test_fatal_error_skips_fallbacks(self):
        c = client()
        with mock.patch.object(c, "_post", side_effect=ThingWorxError("down", fatal=True)) as m:
            with self.assertRaises(ThingWorxError):
                c.run_sql("SELECT 1 FROM dual")
        self.assertEqual(m.call_count, 1)

    def test_paging(self):
        c = client()                                   # page size 5
        data = [{"N": i} for i in range(1, 13)]        # 12 rows available
        sqls = []

        def post(thing, sql):
            sqls.append(sql)
            m = re.search(r"ROWNUM <= (\d+)\) WHERE twx_rn > (\d+)", sql)
            if not m:
                return [dict(r) for r in data[:5]]    # the service cap
            hi, lo = int(m.group(1)), int(m.group(2))
            return [dict(r, TWX_RN=i) for i, r in enumerate(data[lo:hi], lo + 1)]

        with mock.patch.object(c, "_post", side_effect=post):
            full = c.run_sql("SELECT n FROM t ORDER BY n")
            self.assertEqual([r[0] for r in full["rows"]], list(range(1, 13)))
            self.assertEqual([x["name"] for x in full["columns"]], ["N"])           # TWX_RN removed
            limited = c.run_sql("SELECT n FROM t ORDER BY n", limit=8)
            self.assertEqual(len(limited["rows"]), 8)
            self.assertIn("ROWNUM <= 8", sqls[-1])

    def test_offset_pages_and_pinned_columns(self):
        c = client()                                   # page size 5 -> sub-pages of 5 for a 12-row request
        data = [{"A": i, "B": i * 2} for i in range(1, 31)]
        sqls = []

        def post(thing, sql):
            sqls.append(sql)
            m = re.search(r"ROWNUM <= (\d+)\) WHERE twx_rn > (\d+)", sql)
            hi, lo = int(m.group(1)), int(m.group(2))
            return [dict(reversed(list(dict(r, TWX_RN=i).items()))) for i, r in enumerate(data[:hi], 1)][lo:]

        with mock.patch.object(c, "_post", side_effect=post):
            res = c.run_sql("SELECT a, b FROM t ORDER BY a", limit=12, offset=10, columns=["B", "A"])
        self.assertEqual([r[1] for r in res["rows"]], list(range(11, 23)))          # rows 11..22
        self.assertEqual([x["name"] for x in res["columns"]], ["B", "A"])           # order of page 1 is kept
        self.assertEqual(len(sqls), 3)                                              # 5 + 5 + 2 rows
        self.assertTrue(all("twx_rn >" in s for s in sqls))                         # never the plain query for offset>0

    def test_not_configured(self):
        with self.assertRaises(ThingWorxError):
            client(THINGWORX_APPKEY="", APP_KEY="").run_sql("SELECT 1 FROM dual")


if __name__ == "__main__":
    unittest.main()
