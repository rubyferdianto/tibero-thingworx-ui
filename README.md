# Tibero SQL Viewer (via ThingWorx)

A small DBeaver-style web SQL editor for **Tibero**. It never connects to the database directly: every
query goes through a **ThingWorx REST service**. Only `SELECT` is allowed.

- SQL editor on top (CodeMirror), **Ctrl+Enter** runs the selection or the statement under the cursor
  (statements are separated by `;` or a blank line)
- Object browser on the left: owners → tables → columns (`DBA_TABLES`, `DBA_TAB_COLS`)
- Result grid below; the divider between editor and grid (and the sidebar edge) can be dragged
- Context-aware suggestions: after `FROM`/`JOIN` → tables, after `SELECT`/`WHERE`/`ON`/… → columns
  of the tables in the statement, `alias.` → that table's columns, `OWNER.` → that owner's tables
  (also on **Ctrl+Space**). Column suggestions work even if you write `SELECT` before `FROM`.
- No login. Result grid supports sorting (click a header) and CSV export.

## Right-click menu on the result grid

- **Go to end (load all rows)** – loads every remaining page (instead of one page per scroll) and jumps to the bottom.
  Press Esc to stop; the rows loaded so far stay.
- **Export all rows to CSV** / **Export all rows as SQL INSERT…** (also the *Export CSV* button) – the server re-runs the
  statement, fetches **all** pages (not just what the grid shows) and writes the file to `EXPORT_DIR` (default
  `C:\temp`, created if missing; it's the machine running the app). Progress is shown in the status bar, Esc cancels
  (the partial file is deleted), *Show in folder* opens the result in Explorer.
  - SQL export asks for the table name (pre-filled from the `FROM` clause) and writes
    `INSERT INTO OWNER.TABLE (COLS…) VALUES (…);` per row. Strings are quoted/escaped, NULL stays `NULL`.
  - `DATE` / `TIMESTAMP` columns are converted from epoch ms to real dates: `YYYY-MM-DD HH24:MI:SS` in CSV,
    `TO_DATE(…)` / `TO_TIMESTAMP(…)` in SQL. The column types come from `DBA_TAB_COLS` for the tables in your query,
    and the timezone offset between ThingWorx and the database is detected automatically
    (`TWX_TZ_OFFSET_HOURS` forces it). Computed date expressions have no known type – use `TO_CHAR` for those.
  - Like paging, an export re-runs the query per 500-row page: add an `ORDER BY` on a unique key, and expect big tables
    to take a while (use Esc to cancel).

## Run

```powershell
pip install -r requirements.txt
# edit .env  (copy of .env.example; .env is git-ignored)
python app.py          # http://127.0.0.1:5000
```

## ThingWorx side

Same concept as `dashboard_log_inv_mgmt/config/thingworx.py`. The app calls

```
POST {THINGWORX_URL}/Thingworx/Things/{THING_NAME}/Services/{SERVICE_NAME}      (default RunQueryAndGetJSON)
appKey: {THINGWORX_APPKEY}
{"query": "<sql>"}
```

and reads the JSON string in `result` (a list of row objects, or a `{"status": ...}` object).

- **Fallback Things:** if `THING_NAME` fails (e.g. the DB user lacks grants on a table), the query is retried on
  `HALOD_FALLBACK_THING_NAME` / `HALO_FALLBACK_THING_NAME` (comma separated, merged, in order). If all fail, every
  Thing's error is shown. Network/auth errors are not retried.
- **Row cap / paging:** the service returns at most 500 rows per call, so further rows are fetched with `ROWNUM` paging.
  Each page re-runs the query, so add an `ORDER BY` on a unique key to big result sets.
- **Column order:** the service doesn't guarantee it and omits NULLs. The app restores the order from the SELECT list
  (and keeps all-NULL columns). With `SELECT *` the order is whatever the service returns.
- `TWX_HOST` / `APP_KEY` are accepted as alternatives to `THINGWORX_URL` / `THINGWORX_APPKEY`.

## Read-only protection

1. The app rejects anything that is not a single `SELECT`/`WITH … SELECT` (no `INSERT/UPDATE/DELETE/MERGE/DDL`,
   `FOR UPDATE`, `INTO`, `DBMS_*`/`UTL_*` packages, or multiple statements). Comments and string literals are
   ignored when checking. See [sql_guard.py](sql_guard.py) and `python -m unittest discover -s tests`.
2. This is a best-effort filter, not a security boundary. **Also make the ThingWorx database user read-only**
   (grant `SELECT` only) so the database itself enforces it.

## Notes

- **Infinite scroll, no row limit:** `MAX_ROWS` (default 500) is the *page size*. The grid shows the first page; when you
  scroll to the end the next `MAX_ROWS` rows are fetched automatically, and so on until the result is exhausted
  ("all rows loaded" in the status bar). Sorting and CSV export apply to the rows loaded so far.
- `DATE` / `TIMESTAMP` columns arrive from the service as epoch milliseconds. Use `TO_CHAR(col, 'YYYY-MM-DD HH24:MI:SS')`
  in your SQL to see them formatted.
- The object list is read from `DBA_TABLES`, so the ThingWorx DB user needs access to the `DBA_*` views.
  Use `INCLUDE_OWNERS` / `EXCLUDE_OWNERS` in `.env` to limit what is listed, `INCLUDE_VIEWS=true` to add views.
- Table/column metadata is cached for 5 minutes on the server; the ↻ button reloads it.
