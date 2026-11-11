"""Run SQL on Tibero through a ThingWorx service (REST) - same concept as dashboard_log_inv_mgmt/config/thingworx.py.

    POST {host}/Thingworx/Things/{THING_NAME}/Services/{SERVICE_NAME}
    headers: appKey          body: {"query": "<SQL>"}

The service (RunQueryAndGetJSON) answers with an INFOTABLE whose single row holds a JSON *string* in
`result`: a list of row objects, or a status object ({"status": "SUCCESS"} = no rows, anything else = error).
Characteristics handled here:
  * at most ~500 rows per call      -> further rows are fetched with ROWNUM paging
  * column order is not guaranteed  -> restored from the SELECT list when possible (sql_columns.py)
  * NULL values are omitted         -> filled with None
  * DATE/TIMESTAMP come as epoch ms -> shown as returned (use TO_CHAR in the SQL for formatted dates)
  * if the primary Thing fails (e.g. missing grants) the fallback Things are tried in order
"""
import json
import os
from datetime import datetime, timezone

import requests

from sql_columns import order_columns

DEFAULT_PAGE_SIZE = 500
HARD_ROW_CAP = 200_000          # safety net for "fetch everything" calls (metadata)


class ThingWorxError(Exception):
    """Service / database error. `fatal` errors (network, auth) are not retried on fallback Things."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


def _env(*names, default=""):
    for n in names:
        v = os.getenv(n)
        if v is not None and v.strip():
            return v.strip().strip('"').strip("'").strip()
    return default


def _bool(name, default):
    v = _env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


class ThingWorxClient:
    def __init__(self):
        self.host = _env("THINGWORX_URL", "TWX_HOST").rstrip("/")
        self.app_key = _env("THINGWORX_APPKEY", "APP_KEY")
        self.service = _env("SERVICE_NAME", "THINGWORX_SERVICE")
        self.param = _env("THINGWORX_QUERY_PARAM", default="query")
        self.verify = _bool("THINGWORX_VERIFY_SSL", True)
        self.timeout = float(_env("THINGWORX_TIMEOUT", "TWX_TIMEOUT", default="60"))
        self.page_size = int(_env("TWX_MAX_ROWS", default=str(DEFAULT_PAGE_SIZE)))

        primary = _env("THING_NAME", "THINGWORX_THING")
        fallbacks = []
        for var in ("HALOD_FALLBACK_THING_NAME", "HALO_FALLBACK_THING_NAME"):
            fallbacks += [t.strip().strip('"') for t in _env(var).split(",")]
        self.things = [t for t in dict.fromkeys([primary] + fallbacks) if t]    # ordered, de-duplicated
        self.thing = self.things[0] if self.things else ""
        self.session = requests.Session()

    def configured(self):
        return bool(self.host and self.app_key and self.service and self.things)

    # ---------------------------------------------------------------- one call
    def _post(self, thing, sql):
        """One service call on one Thing -> list of row dicts."""
        base = self.host if self.host.lower().endswith("/thingworx") else self.host + "/Thingworx"
        url = f"{base}/Things/{thing}/Services/{self.service}"
        try:
            resp = self.session.post(
                url,
                json={self.param: sql},
                headers={"appKey": self.app_key, "Accept": "application/json", "Content-Type": "application/json"},
                timeout=self.timeout,
                verify=self.verify,
            )
        except requests.exceptions.SSLError as e:
            raise ThingWorxError(f"SSL error talking to ThingWorx (THINGWORX_VERIFY_SSL=false for self-signed certs): {e}", fatal=True) from None
        except requests.exceptions.Timeout:
            raise ThingWorxError(f"ThingWorx did not answer within {self.timeout:g}s.", fatal=True) from None
        except requests.exceptions.RequestException as e:
            raise ThingWorxError(f"Cannot reach ThingWorx at {self.host}: {e}", fatal=True) from None

        if resp.status_code == 401:
            raise ThingWorxError("ThingWorx HTTP 401 Unauthorized - check the app key.", fatal=True)
        if resp.status_code != 200:
            detail = (resp.headers.get("x-thingworx-error-message") or resp.text or resp.reason or "").strip()
            raise ThingWorxError(f"HTTP {resp.status_code}: {detail[:500]}")

        try:
            wrapper = resp.json()
        except ValueError:
            raise ThingWorxError("ThingWorx returned a non-JSON response.") from None
        return self._parse(wrapper)

    @staticmethod
    def _parse(wrapper):
        raw = wrapper
        if isinstance(wrapper, dict):
            rows = wrapper.get("rows")
            if rows and isinstance(rows[0], dict) and "result" in rows[0]:
                raw = rows[0]["result"]
            elif rows is not None and "result" not in wrapper:
                return ThingWorxClient._infotable_rows(wrapper)      # plain INFOTABLE service
            elif "result" in wrapper:
                raw = wrapper["result"]

        if isinstance(raw, str):
            if not raw.strip():
                return []
            try:
                data = json.loads(raw)
            except ValueError:
                raise ThingWorxError(f"Service returned non-JSON result: {raw[:300]}") from None
        else:
            data = raw

        if isinstance(data, dict):
            status = str(data.get("status", "")).upper()
            if status == "SUCCESS":
                return []                                       # "0 records matched your criteria."
            if status or "error" in data:
                raise ThingWorxError(f"{data.get('status', 'ERROR')}: {data.get('error') or data.get('message') or data}")
            return [data]
        if isinstance(data, list):
            return data
        raise ThingWorxError(f"Unexpected service result: {str(data)[:300]}")

    @staticmethod
    def _infotable_rows(wrapper):
        defs = (wrapper.get("dataShape") or {}).get("fieldDefinitions") or {}
        types = {d.get("name"): d.get("baseType") for d in defs.values()}
        out = []
        for r in wrapper.get("rows") or []:
            row = {}
            for k, v in r.items():
                if types.get(k) == "DATETIME" and isinstance(v, (int, float)):
                    v = datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                row[k] = v
            out.append(row)
        return out

    # ----------------------------------------------------- primary + fallbacks
    def _post_with_fallback(self, sql):
        errors = []
        for thing in self.things:
            try:
                return self._post(thing, sql)
            except ThingWorxError as e:
                if e.fatal:
                    raise
                errors.append((thing, str(e)))
        if len(errors) == 1:
            raise ThingWorxError(errors[0][1])
        raise ThingWorxError("\n".join(f"[{t}] {m}" for t, m in errors))

    # --------------------------------------------------------------- public API
    def run_sql(self, sql, limit=None, offset=0, columns=None):
        """Execute SQL and return {'columns': [{'name','type'}], 'rows': [[...]]}.

        limit=None fetches everything (all pages); otherwise at most `limit` rows starting after `offset` rows.
        `columns` (names) pins the column order, e.g. the one from the first page when fetching a later page.
        Paging re-runs the query per page, so give long results an ORDER BY on a unique key.
        """
        if not self.configured():
            raise ThingWorxError("ThingWorx is not configured - check THINGWORX_URL, THINGWORX_APPKEY, THING_NAME, SERVICE_NAME in .env.", fatal=True)

        base = sql.strip().rstrip(";").rstrip()      # the service runs a single statement; a trailing ';' breaks it
        end = HARD_ROW_CAP if limit is None else offset + limit      # absolute row position we want to reach
        rows, pos = [], offset
        if offset == 0:
            rows = self._post_with_fallback(base)                    # plain query: the service returns up to its cap
            pos = len(rows)
            full_page = len(rows) >= self.page_size
        else:
            full_page = True

        while full_page and pos < end:
            hi = min(pos + self.page_size, end)
            # newlines guard against a trailing '--' comment in the original SQL
            paged = (f"SELECT * FROM (SELECT twx_q.*, ROWNUM AS twx_rn FROM (\n{base}\n) twx_q "
                     f"WHERE ROWNUM <= {hi}) WHERE twx_rn > {pos}")
            page = self._post_with_fallback(paged)
            for r in page:
                r.pop("TWX_RN", None)
                r.pop("twx_rn", None)
            rows.extend(page)
            full_page = len(page) >= hi - pos and len(page) > 0
            pos += len(page)

        if limit is not None:
            rows = rows[:limit]

        if columns:
            names = list(columns)
        else:
            seen = {}                               # NULLs are omitted per row: union of keys, first-seen order
            for r in rows:
                for k in r:
                    seen.setdefault(k, None)
            names = order_columns(base, list(seen))
        return {
            "columns": [{"name": n, "type": ""} for n in names],
            "rows": [[self._cell(r.get(n)) for n in names] for r in rows],
        }

    @staticmethod
    def _cell(v):
        return str(v) if isinstance(v, (dict, list)) else v
