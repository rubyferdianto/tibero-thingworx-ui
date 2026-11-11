import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sql_guard import SqlNotAllowed, validate_select  # noqa: E402


class AllowedTests(unittest.TestCase):
    def ok(self, sql):
        return validate_select(sql)

    def test_plain_select(self):
        self.assertEqual(self.ok("select * from emp;"), "select * from emp")

    def test_with_cte(self):
        self.ok("WITH x AS (SELECT 1 a FROM dual) SELECT * FROM x")

    def test_keywords_inside_literals_and_comments(self):
        self.ok("SELECT 'delete me; drop table x' FROM dual")
        self.ok("SELECT 1 FROM dual -- update later; insert too")
        self.ok("SELECT /* delete */ 1 FROM dual")
        self.ok("SELECT q'[it's an update; ok]' FROM dual")
        self.ok('SELECT "UPDATE" FROM t')

    def test_column_names_containing_keywords(self):
        self.ok("SELECT create_date, update_user, delete_flag, insert_time FROM t")

    def test_leading_comment(self):
        self.ok("-- header\nSELECT 1 FROM dual")

    def test_dba_views(self):
        self.ok("SELECT OWNER, TABLE_NAME FROM DBA_TABLES ORDER BY 1, 2")


class BlockedTests(unittest.TestCase):
    def bad(self, sql):
        with self.assertRaises(SqlNotAllowed):
            validate_select(sql)

    def test_dml_ddl(self):
        for s in [
            "INSERT INTO t VALUES (1)",
            "update t set a=1",
            "DELETE FROM t",
            "MERGE INTO t USING s ON (1=1) WHEN MATCHED THEN UPDATE SET a=1",
            "DROP TABLE t",
            "TRUNCATE TABLE t",
            "CREATE TABLE t2 AS SELECT * FROM t",
            "ALTER SESSION SET x=1",
            "GRANT SELECT ON t TO u",
            "BEGIN NULL; END;",
            "CALL some_proc()",
        ]:
            with self.subTest(s=s):
                self.bad(s)

    def test_smuggled(self):
        self.bad("SELECT 1 FROM dual; DELETE FROM t")
        self.bad("SELECT 1 FROM dual;\nDROP TABLE t")
        self.bad("SELECT * FROM t FOR UPDATE")
        self.bad("SELECT * INTO backup FROM t")
        self.bad("SELECT DBMS_LOCK.SLEEP(10) FROM dual")
        self.bad("SELECT UTL_HTTP.REQUEST('http://x') FROM dual")
        self.bad("/* hi */ DELETE FROM t")

    def test_empty(self):
        self.bad("")
        self.bad("   ;  ")
        self.bad("-- only a comment")


if __name__ == "__main__":
    unittest.main()
