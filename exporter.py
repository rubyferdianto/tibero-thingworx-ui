"""Export the FULL result of a SELECT (all pages, not just what the grid shows) to a CSV or SQL INSERT file.

Runs in a background thread so the browser can show progress and cancel. Files are written on the machine
running this app, into EXPORT_DIR (default C:\\temp).
"""
import csv
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from sql_columns import select_list_sources
from sql_guard import _blank

_FROM = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w$#]*)(?:\.([A-Za-z_][\w$#]*))?", re.I)
_PLAIN_IDENT = re.compile(r"^[A-Z][A-Z0-9_$#]*$")
_TABLE_NAME = re.compile(r"^[A-Za-z_][\w$#]*(\.[A-Za-z_][\w$#]*)?$")


class Cancelled(Exception):
    pass


def parse_tables(sql):
    """[(owner_or_None, table)] referenced after FROM / JOIN (simple refs only)."""
    out = []
    for m in _FROM.finditer(_blank(sql)):
        a, b = m.group(1), m.group(2)
        out.append((a.upper(), b.upper()) if b else (None, a.upper()))
    return out


def guess_table(sql):
    refs = parse_tables(sql)
    if not refs:
        return ""
    o, t = refs[0]
    return f"{o}.{t}" if o else t


def valid_table_name(name):
    return bool(_TABLE_NAME.match(name or ""))


def ident(name):
    return name if _PLAIN_IDENT.match(name) else '"' + name.replace('"', '""') + '"'


def sql_literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


class Job:
    def __init__(self, fmt, path, table):
        self.id = uuid.uuid4().hex[:12]
        self.fmt, self.path, self.table = fmt, path, table
        self.status = "running"        # running | done | error | cancelled
        self.rows = 0
        self.error = ""
        self.started = time.time()
        self.finished = None
        self.cancel = threading.Event()

    def as_dict(self):
        end = self.finished or time.time()
        return {"id": self.id, "format": self.fmt, "status": self.status, "rows": self.rows, "path": self.path,
                "error": self.error, "elapsedMs": round((end - self.started) * 1000)}


class Exporter:
    """client: ThingWorxClient; columns_of(owner, table) -> [{'name','type'}]; date_offset() -> seconds."""

    def __init__(self, client, export_dir, columns_of, date_offset, page_size=None):
        self.client = client
        self.dir = export_dir
        self.columns_of = columns_of
        self.date_offset = date_offset
        self.page_size = page_size or client.page_size
        self.jobs = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------ job control
    def start(self, sql, fmt, columns=None, table=""):
        with self.lock:
            if any(j.status == "running" for j in self.jobs.values()):
                raise RuntimeError("Another export is still running - wait for it or cancel it first.")
            base = re.sub(r"[^A-Za-z0-9_.-]", "_", (table or guess_table(sql) or "query")).lower()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self.dir, f"{base}_{stamp}.{'csv' if fmt == 'csv' else 'sql'}")
            job = Job(fmt, path, table)
            self.jobs[job.id] = job
            for old in sorted(self.jobs.values(), key=lambda j: j.started)[:-20]:      # keep the last 20
                if old.status != "running":
                    del self.jobs[old.id]
        threading.Thread(target=self._run, args=(job, sql, columns), daemon=True).start()
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    # ----------------------------------------------------------------- worker
    def _date_columns(self, sql, names):
        """{column_index: 'DATE' | 'TIMESTAMP'} for output columns that are DATE/TIMESTAMP table columns (best effort)."""
        try:
            sources = dict(select_list_sources(sql) or [])
            types = {}
            for owner, table in parse_tables(sql)[:6]:
                for c in self.columns_of(owner, table):
                    types.setdefault(c["name"].upper(), (c.get("type") or "").upper())
            found = {}
            for i, n in enumerate(names):
                t = types.get(sources.get(n.upper()) or n.upper(), "")
                if t.startswith("TIMESTAMP"):
                    found[i] = "TIMESTAMP"
                elif t == "DATE":
                    found[i] = "DATE"
            return found
        except Exception:                      # metadata is only a nicety - never fail the export for it
            return {}

    def _run(self, job, sql, columns):
        status, error = "error", ""
        try:
            os.makedirs(self.dir, exist_ok=True)
            enc = "utf-8-sig" if job.fmt == "csv" else "utf-8"
            offset = 0
            cols = list(columns) if columns else None
            date_cols, shift = {}, None
            with open(job.path, "w", encoding=enc, newline="") as f:
                writer = csv.writer(f) if job.fmt == "csv" else None
                head_done = False
                while True:
                    if job.cancel.is_set():
                        raise Cancelled()
                    res = self.client.run_sql(sql, limit=self.page_size, offset=offset, columns=cols)
                    rows = res["rows"]
                    if cols is None:
                        cols = [c["name"] for c in res["columns"]]
                    if not head_done:
                        head_done = True
                        date_cols = self._date_columns(sql, cols)
                        shift = timedelta(seconds=self.date_offset()) if date_cols else None
                        self._header(job, f, writer, sql, cols)
                    for row in rows:
                        self._write_row(job, f, writer, cols, row, date_cols, shift)
                    f.flush()
                    job.rows += len(rows)
                    offset += len(rows)
                    if len(rows) < self.page_size:
                        break
            status = "done"
        except Cancelled:
            status = "cancelled"
        except Exception as e:                  # noqa: BLE001 - report anything to the UI
            status, error = "error", str(e)
        finally:
            if status != "done":
                try:
                    os.remove(job.path)         # never leave a half-written export behind
                except OSError:
                    pass
                job.path = ""
            job.error = error
            job.finished = time.time()
            job.status = status                 # last: the UI treats any non-running status as final

    # ---------------------------------------------------------------- writers
    @staticmethod
    def _stamp(ms, shift, with_fraction):
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None) + shift
        s = dt.strftime("%Y-%m-%d %H:%M:%S")
        return s + f".{dt.microsecond // 1000:03d}" if with_fraction else s

    def _header(self, job, f, writer, sql, cols):
        if job.fmt == "csv":
            writer.writerow(cols)
        else:
            one_line = " ".join(sql.split())
            f.write(f"-- Exported {datetime.now():%Y-%m-%d %H:%M:%S} by Tibero SQL Viewer\n")
            f.write(f"-- Query: {one_line[:300]}\n\n")

    def _write_row(self, job, f, writer, cols, row, date_cols, shift):
        vals = []
        for i, v in enumerate(row):
            kind = date_cols.get(i)
            if kind and isinstance(v, (int, float)) and not isinstance(v, bool):
                if job.fmt == "csv":
                    v = self._stamp(v, shift, kind == "TIMESTAMP" and v % 1000 != 0)
                elif kind == "DATE":
                    v = ("DATE", self._stamp(v, shift, False))
                else:
                    v = ("TIMESTAMP", self._stamp(v, shift, True))
            vals.append(v)
        if job.fmt == "csv":
            writer.writerow(["" if v is None else v for v in vals])
            return
        parts = []
        for v in vals:
            if isinstance(v, tuple):
                kind, s = v
                parts.append(f"TO_DATE('{s}', 'YYYY-MM-DD HH24:MI:SS')" if kind == "DATE"
                             else f"TO_TIMESTAMP('{s}', 'YYYY-MM-DD HH24:MI:SS.FF3')")
            else:
                parts.append(sql_literal(v))
        f.write(f"INSERT INTO {job.table} ({', '.join(ident(c) for c in cols)}) VALUES ({', '.join(parts)});\n")
