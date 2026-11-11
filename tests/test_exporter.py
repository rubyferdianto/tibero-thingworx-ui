import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exporter import Exporter, guess_table, ident, parse_tables, sql_literal, valid_table_name  # noqa: E402
from sql_columns import select_list_sources  # noqa: E402

MS = 1663563513000          # 2022-09-19 04:58:33 UTC -> the DB shows 12:58:33 with the +8h service offset


class FakeClient:
    page_size = 3

    def __init__(self, rows, fail_at=None, delay=0):
        self.rows, self.fail_at, self.delay, self.calls = rows, fail_at, delay, []

    def run_sql(self, sql, limit=None, offset=0, columns=None):
        self.calls.append((offset, limit))
        if self.delay:
            time.sleep(self.delay)
        if self.fail_at is not None and offset >= self.fail_at:
            raise RuntimeError("boom")
        page = self.rows[offset:offset + limit]
        return {"columns": [{"name": c, "type": ""} for c in (columns or ["ID", "NAME", "CREATED"])], "rows": page}


def columns_of(owner, table):
    return [{"name": "ID", "type": "NUMBER(6)"}, {"name": "NAME", "type": "VARCHAR2(20)"},
            {"name": "REG_DT", "type": "DATE"}, {"name": "TS", "type": "TIMESTAMP(6)"}]


def wait(job, timeout=5):
    end = time.time() + timeout
    while job.status == "running" and time.time() < end:
        time.sleep(0.02)
    return job


class HelperTests(unittest.TestCase):
    def test_tables(self):
        self.assertEqual(parse_tables("SELECT * FROM hr.emp e JOIN dept d ON 1=1 -- from nothing"), [("HR", "EMP"), (None, "DEPT")])
        self.assertEqual(guess_table("select a from cinfo.addr where x = 'from y'"), "CINFO.ADDR")
        self.assertEqual(guess_table("SELECT 1"), "")

    def test_names_and_literals(self):
        self.assertTrue(valid_table_name("HR.EMP") and valid_table_name("emp"))
        self.assertFalse(valid_table_name("a b") or valid_table_name("a;drop") or valid_table_name("") or valid_table_name("a.b.c"))
        self.assertEqual(ident("COL_1"), "COL_1")
        self.assertEqual(ident("My Col"), '"My Col"')
        self.assertEqual([sql_literal(v) for v in (None, 5, 1.5, True, "it's")], ["NULL", "5", "1.5", "1", "'it''s'"])

    def test_alias_sources(self):
        self.assertEqual(select_list_sources("SELECT t.reg_dt AS created, id, nvl(x,0) y, a b FROM t"),
                         [("CREATED", "REG_DT"), ("ID", "ID"), ("Y", None), ("B", "A")])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def make(self, client):
        return Exporter(client, self.dir, columns_of, lambda: 8 * 3600)

    def read(self, job):
        with open(job.path, encoding="utf-8-sig" if job.fmt == "csv" else "utf-8", newline="") as f:
            return f.read()

    def test_csv_all_pages_and_dates(self):
        rows = [[i, f"n{i}", MS] for i in range(1, 8)]               # 7 rows = pages 3 + 3 + 1
        c = FakeClient(rows)
        job = wait(self.make(c).start("SELECT id, name, reg_dt AS created FROM x.t ORDER BY 1", "csv"))
        self.assertEqual(job.status, "done", job.error)
        self.assertEqual(job.rows, 7)
        self.assertEqual([call[0] for call in c.calls], [0, 3, 6])
        lines = self.read(job).splitlines()
        self.assertEqual(lines[0], "ID,NAME,CREATED")
        self.assertEqual(lines[1], "1,n1,2022-09-19 12:58:33")        # epoch -> DB wall clock (+8h)
        self.assertEqual(len(lines), 8)
        self.assertTrue(job.path.endswith(".csv") and os.path.basename(job.path).startswith("x.t_"))

    def test_sql_insert_with_dates_and_quoting(self):
        rows = [[1, "O'Brien", MS], [2, None, None]]
        job = wait(self.make(FakeClient(rows)).start("SELECT id, name, reg_dt AS created FROM x.t", "sql", table="X.T"))
        self.assertEqual(job.status, "done", job.error)
        text = self.read(job)
        self.assertIn("INSERT INTO X.T (ID, NAME, CREATED) VALUES (1, 'O''Brien', TO_DATE('2022-09-19 12:58:33', 'YYYY-MM-DD HH24:MI:SS'));", text)
        self.assertIn("INSERT INTO X.T (ID, NAME, CREATED) VALUES (2, NULL, NULL);", text)
        self.assertTrue(job.path.endswith(".sql"))

    def test_timestamp_keeps_milliseconds(self):
        job = wait(self.make(FakeClient([[1, "a", MS + 123]])).start("SELECT id, name, ts AS created FROM x.t", "sql", table="X.T"))
        self.assertIn("TO_TIMESTAMP('2022-09-19 12:58:33.123', 'YYYY-MM-DD HH24:MI:SS.FF3')", self.read(job))

    def test_empty_result_still_has_header(self):
        job = wait(self.make(FakeClient([])).start("SELECT id, name FROM x.t", "csv", columns=["ID", "NAME"]))
        self.assertEqual((job.status, job.rows), ("done", 0))
        self.assertEqual(self.read(job).strip(), "ID,NAME")

    def test_error_removes_partial_file(self):
        job = wait(self.make(FakeClient([[i, "n", 0] for i in range(10)], fail_at=3)).start("SELECT id FROM x.t", "csv"))
        self.assertEqual(job.status, "error")
        self.assertIn("boom", job.error)
        self.assertEqual(os.listdir(self.dir), [])

    def test_cancel_removes_file_and_second_job_blocked(self):
        ex = self.make(FakeClient([[i, "n", 0] for i in range(300)], delay=0.05))
        job = ex.start("SELECT id FROM x.t", "csv")
        with self.assertRaises(RuntimeError):
            ex.start("SELECT id FROM x.t", "csv")
        time.sleep(0.12)
        job.cancel.set()
        wait(job)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(os.listdir(self.dir), [])


if __name__ == "__main__":
    unittest.main()
