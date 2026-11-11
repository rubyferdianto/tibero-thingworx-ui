"""Tibero SQL viewer (read-only) that talks to the database through the ThingWorx REST API."""
import os
import re
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from exporter import Exporter, guess_table, valid_table_name  # noqa: E402
from sql_guard import SqlNotAllowed, validate_select  # noqa: E402
from thingworx_client import ThingWorxClient, ThingWorxError  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="/static")
tw = ThingWorxClient()

MAX_ROWS = int(os.getenv("MAX_ROWS", "500") or 500)      # page size of the result grid
INCLUDE_VIEWS = os.getenv("INCLUDE_VIEWS", "false").strip().lower() in ("1", "true", "yes", "on")
IDENT = re.compile(r"^[A-Za-z0-9_$#]+$")


def _owner_list(name: str) -> list:
    items = [x.strip().upper() for x in os.getenv(name, "").split(",") if x.strip()]
    for x in items:
        if not IDENT.match(x):
            raise RuntimeError(f"Invalid identifier in {name}: {x!r}")
    return items


INCLUDE_OWNERS = _owner_list("INCLUDE_OWNERS")
EXCLUDE_OWNERS = _owner_list("EXCLUDE_OWNERS")


# ---- tiny TTL cache for metadata so the tree/autocomplete stay fast ----------
_cache, _cache_lock, CACHE_TTL = {}, threading.Lock(), 300


def cached(key, producer, refresh=False):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not refresh and now - hit[0] < CACHE_TTL:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _in_list(values):
    return ", ".join("'" + v + "'" for v in values)


def _rows_as_dicts(result):
    names = [c["name"].upper() for c in result["columns"]]
    return [dict(zip(names, r)) for r in result["rows"]]


# ---- routes ------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify(maxRows=MAX_ROWS, configured=tw.configured(), thing=tw.thing, service=tw.service,
                   fallbacks=tw.things[1:], exportDir=EXPORT_DIR)


@app.get("/api/ping")
def ping():
    try:
        tw.run_sql("SELECT 1 AS OK FROM DUAL")
        return jsonify(ok=True)
    except ThingWorxError as e:
        return jsonify(ok=False, error=str(e)), 502


@app.get("/api/objects")
def objects():
    """List tables (and optionally views) from DBA_TABLES / DBA_VIEWS."""
    if INCLUDE_OWNERS:
        owner_filter = f"OWNER IN ({_in_list(INCLUDE_OWNERS)})"
    elif EXCLUDE_OWNERS:
        owner_filter = f"OWNER NOT IN ({_in_list(EXCLUDE_OWNERS)})"
    else:
        owner_filter = "1 = 1"

    sql = f"SELECT OWNER, TABLE_NAME AS OBJECT_NAME, 'TABLE' AS OBJECT_TYPE FROM DBA_TABLES WHERE {owner_filter}"
    if INCLUDE_VIEWS:
        sql += f" UNION ALL SELECT OWNER, VIEW_NAME, 'VIEW' FROM DBA_VIEWS WHERE {owner_filter}"
    sql += " ORDER BY 1, 2"

    def produce():
        res = tw.run_sql(sql)
        return [{"owner": r["OWNER"], "name": r["OBJECT_NAME"], "type": r["OBJECT_TYPE"]} for r in _rows_as_dicts(res)]

    try:
        return jsonify(objects=cached("objects", produce, request.args.get("refresh") == "1"))
    except ThingWorxError as e:
        return jsonify(error=str(e)), 502


@app.get("/api/columns")
def columns():
    """Columns of one table from DBA_TAB_COLS. owner is optional (any owner is matched)."""
    owner = (request.args.get("owner") or "").strip().upper()
    table = (request.args.get("table") or "").strip().upper()
    if not IDENT.match(table) or (owner and not IDENT.match(owner)):
        return jsonify(error="Invalid owner or table name."), 400
    try:
        return jsonify(columns=table_columns(owner, table, request.args.get("refresh") == "1"))
    except ThingWorxError as e:
        return jsonify(error=str(e)), 502


def table_columns(owner, table, refresh=False):
    """Columns of a table from DBA_TAB_COLS (cached). owner may be '' / None = any owner."""
    owner = (owner or "").upper()
    table = table.upper()
    if not IDENT.match(table) or (owner and not IDENT.match(owner)):
        raise ThingWorxError("Invalid owner or table name.")
    where = f"TABLE_NAME = '{table}'" + (f" AND OWNER = '{owner}'" if owner else "")
    sql = (
        "SELECT OWNER, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE, COLUMN_ID "
        f"FROM DBA_TAB_COLS WHERE {where} ORDER BY OWNER, COLUMN_ID"
    )

    def produce():
        out = []
        for r in _rows_as_dicts(tw.run_sql(sql)):
            if str(r["COLUMN_NAME"]).startswith(("SYS_NC", "SYS_STU")):  # hidden system columns
                continue
            out.append({
                "owner": r["OWNER"],
                "name": r["COLUMN_NAME"],
                "type": _format_type(r),
                "nullable": r.get("NULLABLE") == "Y",
            })
        return out

    return cached(f"cols:{owner}:{table}", produce, refresh)


def _format_type(r):
    t = str(r.get("DATA_TYPE") or "")
    if t in ("NUMBER", "FLOAT") and r.get("DATA_PRECISION") not in (None, ""):
        scale = r.get("DATA_SCALE")
        return f"{t}({int(r['DATA_PRECISION'])}" + (f",{int(scale)})" if scale not in (None, "", 0) else ")")
    if t in ("VARCHAR2", "VARCHAR", "NVARCHAR2", "CHAR", "NCHAR", "RAW") and r.get("DATA_LENGTH") not in (None, ""):
        return f"{t}({int(r['DATA_LENGTH'])})"
    return t


@app.post("/api/query")
def query():
    payload = request.get_json(silent=True) or {}
    try:
        sql = validate_select(payload.get("sql", ""))
    except SqlNotAllowed as e:
        return jsonify(error=str(e)), 400

    # MAX_ROWS is the page size: the grid asks for the next page (offset) when the user scrolls to the end.
    try:
        offset = max(0, int(payload.get("offset") or 0))
    except (TypeError, ValueError):
        return jsonify(error="Invalid offset."), 400
    cols = payload.get("columns")
    if not (isinstance(cols, list) and all(isinstance(c, str) for c in cols)):
        cols = None

    # One extra row tells us whether more exist. If a page is as big as the service's per-call cap that extra row
    # would cost a second call per page, so a full page then just counts as "maybe more" (the next page may be empty).
    fetch = MAX_ROWS + 1 if MAX_ROWS < tw.page_size else MAX_ROWS

    started = time.perf_counter()
    try:
        result = tw.run_sql(sql, limit=fetch, offset=offset, columns=cols)
    except ThingWorxError as e:
        return jsonify(error=str(e)), 502

    rows = result["rows"]
    return jsonify(
        columns=result["columns"],
        rows=rows[:MAX_ROWS],
        rowCount=min(len(rows), MAX_ROWS),
        offset=offset,
        hasMore=len(rows) >= fetch,
        pageSize=MAX_ROWS,
        elapsedMs=round((time.perf_counter() - started) * 1000),
    )


# ---- full-result exports (CSV / SQL INSERT) ----------------------------------------
EXPORT_DIR = os.getenv("EXPORT_DIR", "").strip().strip('"') or r"C:\temp"


def detect_date_offset():
    """Seconds to add to ThingWorx's epoch-ms DATE values to get the database's wall-clock time.

    The service turns DATE/TIMESTAMP into epoch ms using its own server timezone, so the DB clock reading
    differs from the UTC reading by a fixed offset. Measured once from SYSDATE vs TO_CHAR(SYSDATE); override
    with TWX_TZ_OFFSET_HOURS in .env.
    """
    forced = os.getenv("TWX_TZ_OFFSET_HOURS", "").strip()
    if forced:
        return int(float(forced) * 3600)

    def produce():
        try:
            r = tw.run_sql("SELECT SYSDATE AS D, TO_CHAR(SYSDATE, 'YYYY-MM-DD HH24:MI:SS') AS S FROM DUAL")
            ms, text = r["rows"][0]
            from datetime import datetime, timezone
            utc_reading = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)
            diff = (datetime.strptime(text, "%Y-%m-%d %H:%M:%S") - utc_reading).total_seconds()
            return int(round(diff / 900.0) * 900)         # whole quarter hours
        except Exception:                                 # noqa: BLE001
            return 0
    return cached("date-offset", produce)


exporter = Exporter(tw, EXPORT_DIR, table_columns, detect_date_offset)


@app.post("/api/export")
def export_start():
    p = request.get_json(silent=True) or {}
    fmt = p.get("format")
    if fmt not in ("csv", "sql"):
        return jsonify(error="format must be 'csv' or 'sql'."), 400
    try:
        sql = validate_select(p.get("sql", ""))
    except SqlNotAllowed as e:
        return jsonify(error=str(e)), 400
    table = (p.get("table") or "").strip()
    if fmt == "sql" and not valid_table_name(table):
        return jsonify(error="Enter a valid table name for the INSERT statements, e.g. OWNER.TABLE."), 400
    cols = p.get("columns")
    if not (isinstance(cols, list) and cols and all(isinstance(c, str) for c in cols)):
        cols = None
    try:
        return jsonify(exporter.start(sql, fmt, cols, table).as_dict())
    except RuntimeError as e:
        return jsonify(error=str(e)), 409


@app.get("/api/export/guess")
def export_guess():
    return jsonify(table=guess_table(request.args.get("sql", "")))


@app.get("/api/export/<job_id>")
def export_status(job_id):
    job = exporter.get(job_id)
    return (jsonify(job.as_dict()) if job else (jsonify(error="Unknown export."), 404))


@app.post("/api/export/<job_id>/cancel")
def export_cancel(job_id):
    job = exporter.get(job_id)
    if not job:
        return jsonify(error="Unknown export."), 404
    job.cancel.set()
    return jsonify(job.as_dict())


@app.post("/api/export/<job_id>/reveal")
def export_reveal(job_id):
    """Show the finished file in Windows Explorer (the app runs on the same machine as the browser)."""
    job = exporter.get(job_id)
    if not job or job.status != "done" or not os.path.isfile(job.path):
        return jsonify(error="No finished export file."), 404
    if os.name == "nt":
        import subprocess
        subprocess.Popen(["explorer", "/select,", os.path.normpath(job.path)])
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "5000")),
        debug=False,
    )
