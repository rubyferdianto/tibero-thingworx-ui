/* Tibero SQL Viewer - front end (editor, autocomplete, object tree, result grid) */
(function () {
  "use strict";

  // ---------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k) { try { return localStorage.getItem("tsv." + k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem("tsv." + k, v); } catch (e) { /* ignore */ } },
  };
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  async function api(path, opts) {
    const r = await fetch(path, opts);
    let j = {};
    try { j = await r.json(); } catch (e) { /* non-json */ }
    if (!r.ok) throw new Error(j.error || `${r.status} ${r.statusText}`);
    return j;
  }

  // ------------------------------------------------------------ SQL scanning
  // Replace string literals / comments by `fill` (keeps length + newlines). `open` = text ends inside one.
  function mask(text, fill) {
    fill = fill || " ";
    const n = text.length;
    let out = "", i = 0, open = false;
    const blank = (s) => s.replace(/[^\n]/g, fill);
    while (i < n) {
      const c = text[i], d = text[i + 1];
      let j = -1, isOpen = false;
      if (c === "'") {
        j = i + 1;
        for (;;) {
          if (j >= n) { isOpen = true; break; }
          if (text[j] === "'") { if (text[j + 1] === "'") { j += 2; continue; } j++; break; }
          j++;
        }
      } else if (c === '"') {
        j = text.indexOf('"', i + 1);
        if (j < 0) { j = n; isOpen = true; } else j++;
      } else if (c === "-" && d === "-") {
        j = text.indexOf("\n", i);
        if (j < 0) { j = n; isOpen = true; }
      } else if (c === "/" && d === "*") {
        j = text.indexOf("*/", i + 2);
        if (j < 0) { j = n; isOpen = true; } else j += 2;
      }
      if (j >= 0) { out += blank(text.slice(i, j)); if (isOpen) open = true; i = j; }
      else { out += c; i++; }
    }
    return { text: out, open };
  }

  // Statements are separated by ';' or by a blank line (like DBeaver).
  function splitStatements(text) {
    const m = mask(text, "\x01").text;
    const out = [];
    const push = (rs, re) => {
      let s = rs, e = re;
      while (s < e && /\s/.test(text[s])) s++;
      while (e > s && /\s/.test(text[e - 1])) e--;
      out.push({ rawStart: rs, rawEnd: re, start: s, end: e, empty: e <= s });
    };
    const re = /;|\n[ \t\r]*\n/g;
    let start = 0, x;
    while ((x = re.exec(m))) { push(start, x.index); start = x.index + 1; }
    push(start, text.length);
    return out;
  }

  function containing(chunks, pos) {
    return chunks.find((c) => pos >= c.rawStart && pos <= c.rawEnd) || null;
  }

  function statementToRun(text, pos) {
    const chunks = splitStatements(text);
    const here = containing(chunks, pos);
    if (here && !here.empty) return here;
    const before = chunks.filter((c) => !c.empty && c.start < pos).pop();
    return before || chunks.find((c) => !c.empty) || null;
  }

  // ----------------------------------------------------------- schema model
  const state = {
    objects: null,            // [{owner,name,type}]
    byOwner: new Map(),       // OWNER -> [objects]
    byName: new Map(),        // NAME  -> [OWNER,...]
    colCache: new Map(),      // "OWNER.NAME" -> Promise<[{name,type,nullable}]>
    colLoaded: new Map(),     // "OWNER.NAME" -> [cols] once resolved (for fallbacks)
    activeTable: null,        // {owner,name} chosen in the tree
    result: null,
    sort: null,
    running: false,
    pageSize: 500,            // rows per page (MAX_ROWS)
    queryId: 0,               // bumps on every run; late responses of older queries are dropped
    loadingMore: false,
    loadingAll: false,        // right-click > "Go to end" is loading every remaining page
    stopAll: false,
    stmt: "",                 // statement that produced the current result (re-sent for further pages)
  };

  function indexObjects(list) {
    state.objects = list;
    state.byOwner = new Map();
    state.byName = new Map();
    for (const o of list) {
      if (!state.byOwner.has(o.owner)) state.byOwner.set(o.owner, []);
      state.byOwner.get(o.owner).push(o);
      if (!state.byName.has(o.name)) state.byName.set(o.name, []);
      state.byName.get(o.name).push(o.owner);
    }
  }

  function getColumns(owner, name) {
    const key = owner + "." + name;
    if (!state.colCache.has(key)) {
      const p = api(`/api/columns?owner=${encodeURIComponent(owner)}&table=${encodeURIComponent(name)}`)
        .then((j) => { state.colLoaded.set(key, j.columns); return j.columns; })
        .catch((e) => { state.colCache.delete(key); throw e; });
      state.colCache.set(key, p);
    }
    return state.colCache.get(key);
  }

  // Resolve a table reference {owner?, name} to known (owner,name) pairs
  function resolveRef(ref) {
    if (!state.objects) return [];
    const name = ref.name.toUpperCase();
    const owners = state.byName.get(name) || [];
    if (ref.owner) return owners.includes(ref.owner.toUpperCase()) ? [{ owner: ref.owner.toUpperCase(), name }] : [];
    return owners.slice(0, 3).map((o) => ({ owner: o, name }));
  }

  async function columnsForRefs(refs) {
    const pairs = refs.flatMap(resolveRef);
    const lists = await Promise.all(pairs.map((p) => getColumns(p.owner, p.name).then((cols) => cols.map((c) => ({ ...c, table: p.name }))).catch(() => [])));
    return lists.flat();
  }

  // ------------------------------------------------------------ autocomplete
  const KEYWORDS = ("SELECT DISTINCT FROM WHERE AND OR NOT IN EXISTS BETWEEN LIKE IS NULL AS ON JOIN INNER LEFT RIGHT FULL OUTER CROSS " +
    "GROUP BY ORDER HAVING UNION ALL MINUS INTERSECT CASE WHEN THEN ELSE END ASC DESC WITH CONNECT START PRIOR LEVEL FETCH FIRST ROWS ONLY OVER PARTITION").split(" ");
  const FUNCTIONS = ("COUNT SUM AVG MIN MAX NVL NVL2 DECODE COALESCE NULLIF TO_CHAR TO_DATE TO_NUMBER TO_TIMESTAMP SUBSTR INSTR LENGTH UPPER LOWER INITCAP " +
    "TRIM LTRIM RTRIM LPAD RPAD REPLACE CONCAT ROUND TRUNC CEIL FLOOR MOD ABS SYSDATE SYSTIMESTAMP ROWNUM ROWID ROW_NUMBER RANK DENSE_RANK LAG LEAD " +
    "LISTAGG ADD_MONTHS MONTHS_BETWEEN LAST_DAY NEXT_DAY EXTRACT CAST").split(" ");
  const NOT_ALIAS = new Set(("WHERE GROUP ORDER HAVING JOIN INNER LEFT RIGHT FULL CROSS OUTER ON UNION MINUS INTERSECT CONNECT START FETCH USING NATURAL AND OR SET FOR WINDOW LIMIT AS").split(" "));
  const COLUMN_CLAUSE = new Set("SELECT WHERE ON AND OR HAVING BY WHEN THEN ELSE CASE USING DISTINCT".split(" "));
  const TABLE_CLAUSE = new Set(["FROM", "JOIN"]);
  const MAX_HINTS = 200;

  // Tables referenced in FROM / JOIN of a (masked) statement, with aliases
  function parseTables(s) {
    const refs = [];
    const kw = /\b(FROM|JOIN)\s+/gi;
    let m;
    while ((m = kw.exec(s))) {
      let pos = kw.lastIndex;
      for (;;) {
        const t = /^([A-Za-z_][\w$#]*)(?:\.([A-Za-z_][\w$#]*))?(?:\s+(?:AS\s+)?([A-Za-z_][\w$#]*))?/i.exec(s.slice(pos));
        if (!t) break;
        const ref = t[2] ? { owner: t[1], name: t[2] } : { owner: null, name: t[1] };
        if (t[3] && !NOT_ALIAS.has(t[3].toUpperCase())) ref.alias = t[3];
        refs.push(ref);
        pos += t[0].length;
        const rest = /^\s*,\s*/.exec(s.slice(pos));
        if (!rest) break;
        pos += rest[0].length;
      }
    }
    return refs;
  }

  // What does the text before the cursor expect: a table, a column or just keywords?
  function clauseContext(beforeMasked) {
    const toks = beforeMasked.match(/[A-Za-z_][\w$#.]*|[(),=<>!+\-*\/|]|\d+/g) || [];
    const last = toks.length ? toks[toks.length - 1].toUpperCase() : "";
    for (let i = toks.length - 1; i >= 0; i--) {
      const t = toks[i].toUpperCase();
      if (TABLE_CLAUSE.has(t)) {
        const afterRef = i < toks.length - 1 && last !== "," && last !== "(";
        return { kind: afterRef ? "keyword" : "table" };
      }
      if (COLUMN_CLAUSE.has(t)) return { kind: "column" };
    }
    return { kind: "keyword" };
  }

  function rankFilter(items, prefix, key) {
    const p = prefix.toUpperCase();
    if (!p) return items;
    const starts = [], contains = [];
    for (const it of items) {
      const k = key(it).toUpperCase();
      if (k.startsWith(p)) starts.push(it);
      else if (p.length >= 2 && k.includes(p)) contains.push(it);
    }
    return starts.concat(contains);
  }

  const kwItem = (w, detail) => ({ text: w, displayText: w, detail, cls: "h-kw" });
  const hintRender = (elm, self, data) => {
    elm.className += " " + (data.cls || "");
    elm.innerHTML = `<span class="h-name">${esc(data.displayText)}</span><span class="h-detail">${esc(data.detail || "")}</span>`;
  };

  function tableItems(objs, qualified) {
    return objs.map((o) => ({
      text: qualified ? `${o.owner}.${o.name}` : o.name,
      displayText: o.name,
      detail: o.owner + (o.type === "VIEW" ? " · view" : ""),
      cls: "h-tbl",
      _key: `${o.owner}.${o.name}`,
      _name: o.name,
    }));
  }

  async function buildHints(cm) {
    const cur = cm.getCursor();
    const full = cm.getValue();
    const off = cm.indexFromPos(cur);
    const lineBefore = cm.getLine(cur.line).slice(0, cur.ch);
    const word = /[A-Za-z0-9_$#.]*$/.exec(lineBefore)[0];
    if (/^\d/.test(word)) return null;

    let qualifier = null, prefix = word;
    const dot = word.lastIndexOf(".");
    if (dot >= 0) { qualifier = word.slice(0, dot); prefix = word.slice(dot + 1); }
    const range = { from: { line: cur.line, ch: cur.ch - prefix.length }, to: cur };

    const chunk = containing(splitStatements(full), off);
    const stmtStart = chunk ? chunk.rawStart : off;
    const stmtEnd = chunk ? chunk.rawEnd : off;
    const upToCursor = mask(full.slice(stmtStart, off - word.length));
    if (mask(full.slice(stmtStart, off)).open) return null;       // inside a string or comment
    const stmtMasked = mask(full.slice(stmtStart, stmtEnd)).text;
    const refs = parseTables(stmtMasked);
    const ctx = clauseContext(upToCursor.text);
    let items = [];

    if (qualifier) {
      const q = qualifier.toUpperCase();
      let target = null;
      if (q.includes(".")) {
        const [o, t] = q.split(".");
        target = [{ owner: o, name: t }];
      } else {
        const hit = refs.find((r) => r.alias && r.alias.toUpperCase() === q) || refs.find((r) => r.name.toUpperCase() === q);
        if (hit) target = [hit];
        else if (state.byOwner.has(q)) {
          items = tableItems(rankFilter(state.byOwner.get(q), prefix, (o) => o.name), false);
        } else if (state.byName.has(q)) target = [{ owner: null, name: q }];
      }
      if (target) {
        const cols = await columnsForRefs(target);
        items = rankFilter(cols, prefix, (c) => c.name).map((c) => ({ text: c.name, displayText: c.name, detail: c.type, cls: "h-col" }));
        if (!prefix || "*".startsWith(prefix)) items.unshift({ text: "*", displayText: "*", detail: "all columns", cls: "h-kw" });
      }
    } else if (ctx.kind === "table") {
      if (!state.objects) return null;
      items = tableItems(rankFilter(state.objects, prefix, (o) => `${o.owner}.${o.name}`.startsWith(prefix.toUpperCase()) ? `${o.owner}.${o.name}` : o.name), true);
    } else if (ctx.kind === "column") {
      let cols = await columnsForRefs(refs);
      if (!cols.length && state.activeTable) {
        cols = await columnsForRefs([state.activeTable]);
      }
      if (!cols.length && !refs.length) {
        cols = [...state.colLoaded.entries()].flatMap(([k, v]) => v.map((c) => ({ ...c, table: k.split(".")[1] })));
      }
      const multi = new Set(cols.map((c) => c.table)).size > 1;
      items = rankFilter(cols, prefix, (c) => c.name).map((c) => ({
        text: c.name, displayText: c.name, detail: c.type + (multi ? " · " + c.table : ""), cls: "h-col",
      }));
      if (prefix) {
        items = items.concat(
          rankFilter(FUNCTIONS, prefix, (x) => x).map((f) => kwItem(f, "function")),
          rankFilter(KEYWORDS, prefix, (x) => x).map((k) => kwItem(k, "")));
      } else if (items.length) {
        items.unshift({ text: "*", displayText: "*", detail: "all columns", cls: "h-kw" });
      }
    } else if (prefix) {
      items = rankFilter(KEYWORDS, prefix, (x) => x).map((k) => kwItem(k, ""))
        .concat(rankFilter(FUNCTIONS, prefix, (x) => x).map((f) => kwItem(f, "function")));
    }

    if (!items.length) return null;
    // de-duplicate by displayed text + detail
    const seen = new Set();
    items = items.filter((i) => { const k = i.text + "|" + i.detail; if (seen.has(k)) return false; seen.add(k); return true; });
    items = items.slice(0, MAX_HINTS).map((i) => ({ ...i, render: hintRender }));
    return { list: items, from: range.from, to: range.to };
  }

  function hintFn(cm, callback) {
    buildHints(cm).then(callback, () => callback(null));
  }
  hintFn.async = true;

  // ----------------------------------------------------------------- editor
  const cm = CodeMirror($("editor"), {
    value: store.get("sql") || "-- Write a SELECT statement, put the cursor on it and press Ctrl+Enter\nSELECT * FROM DUAL;\n",
    mode: "text/x-plsql",
    lineNumbers: true,
    matchBrackets: true,
    indentUnit: 2,
    lineWrapping: false,
    autofocus: true,
    extraKeys: {
      "Ctrl-Enter": () => run(),
      "Cmd-Enter": () => run(),
      "Ctrl-Space": () => triggerHint(),
    },
  });

  function triggerHint() {
    cm.showHint({ hint: hintFn, completeSingle: false, closeCharacters: /[\s()\[\]{};:>,=<]/ });
  }

  cm.on("inputRead", (inst, change) => {
    if (change.origin === "paste" || change.text.length > 1) return;
    const t = change.text[0];
    if (t === " ") {
      // The space closes any open popup; wait for that, then pop up the list the keyword before it calls for.
      setTimeout(() => {
        if (inst.state.completionActive) return;
        const cur = inst.getCursor();
        const lb = inst.getLine(cur.line).slice(0, cur.ch).trimEnd();
        if (/(?:^|[^\w$#])(SELECT|DISTINCT|FROM|JOIN|WHERE|AND|OR|ON|BY|HAVING)$/i.test(lb) || /,$/.test(lb)) triggerHint();
      }, 30);
      return;
    }
    if (inst.state.completionActive) return;
    if (/^[A-Za-z0-9_$#.]$/.test(t)) triggerHint();
  });

  let saveTimer = null, runMark = null;
  cm.on("change", () => {
    if (runMark) { runMark.clear(); runMark = null; }
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => store.set("sql", cm.getValue()), 400);
  });

  function insertText(text) {
    cm.replaceSelection(text);
    cm.focus();
  }

  // ------------------------------------------------------------------ run
  async function run(forcedSql) {
    if (state.running) return;
    let sql;
    if (typeof forcedSql === "string") {
      sql = forcedSql;
    } else if (cm.somethingSelected()) {
      sql = cm.getSelection();
      markRunning(cm.getCursor("from"), cm.getCursor("to"));
    } else {
      const text = cm.getValue();
      const chunk = statementToRun(text, cm.indexFromPos(cm.getCursor()));
      if (!chunk) { setStatus("Nothing to run - no statement at the cursor.", "warn"); return; }
      sql = text.slice(chunk.start, chunk.end);
      markRunning(cm.posFromIndex(chunk.start), cm.posFromIndex(chunk.end));
    }

    state.running = true;
    state.loadingMore = false;
    ++state.queryId;                       // responses of an older query are ignored
    state.stmt = sql;
    $("btnRun").disabled = true;
    setStatus("Running…");
    try {
      const j = await postQuery({ sql });
      state.result = { columns: j.columns, rows: j.rows, hasMore: j.hasMore, elapsedMs: j.elapsedMs };
      state.pageSize = j.pageSize;
      state.sort = null;
      renderGrid();
      updateStatus();
      $("btnCsv").disabled = false;
      state.running = false;
      maybeLoadMore();                     // short page that doesn't fill the grid -> keep loading
    } catch (e) {
      state.result = null;
      $("btnCsv").disabled = true;
      $("grid").innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
      setStatus("Query failed", "err");
    } finally {
      state.running = false;
      $("btnRun").disabled = false;
    }
  }

  const postQuery = (body) => api("/api/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  // Infinite scroll: when the user reaches the end of the grid, fetch the next page (MAX_ROWS rows).
  async function loadMore() {
    const r = state.result;
    if (!r || !r.hasMore || state.loadingMore || state.running) return false;
    const qid = state.queryId;
    state.loadingMore = true;
    setStatus(`Loading rows ${(r.rows.length + 1).toLocaleString()}…`);
    let ok = false;
    try {
      const j = await postQuery({ sql: state.stmt, offset: r.rows.length, columns: r.columns.map((c) => c.name) });
      if (qid !== state.queryId) return false;
      const start = r.rows.length;
      for (const row of j.rows) r.rows.push(row);
      r.hasMore = j.hasMore;
      r.elapsedMs = j.elapsedMs;
      if (state.sort) renderGrid(); else appendRows(start, j.rows);
      updateStatus();
      ok = true;
    } catch (e) {
      if (qid === state.queryId) setStatus("Could not load more rows: " + e.message + " (scroll again to retry)", "err");
    } finally {
      if (qid === state.queryId) state.loadingMore = false;
    }
    if (ok && qid === state.queryId) maybeLoadMore();
    return ok;
  }

  // Right-click > "Go to end": keep loading pages until the whole result is in the grid, then jump to the bottom.
  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
  async function loadAll() {
    const r = state.result;
    if (!r) return;
    if (state.loadingAll) { state.stopAll = true; return; }
    const qid = state.queryId;
    state.loadingAll = true;
    state.stopAll = false;
    try {
      while (r.hasMore && !state.stopAll && qid === state.queryId) {
        if (state.loadingMore || state.running) { await sleep(100); continue; }
        const ok = await loadMore();
        if (!ok) break;
      }
    } finally {
      state.loadingAll = false;
      if (qid === state.queryId && state.result) updateStatus();
    }
    if (qid === state.queryId) { const g = $("grid"); g.scrollTop = g.scrollHeight; }
  }

  function maybeLoadMore() {
    const g = $("grid");
    if (state.result && state.result.hasMore && g.scrollTop + g.clientHeight >= g.scrollHeight - 300) loadMore();
  }
  $("grid").addEventListener("scroll", maybeLoadMore, { passive: true });

  function updateStatus() {
    const r = state.result;
    const n = r.rows.length;
    let t = `${n.toLocaleString()} row${n === 1 ? "" : "s"}`;
    if (state.loadingAll && r.hasMore) {
      t += " loaded · loading all rows… (Esc to stop)";
    } else if (r.hasMore) {
      t += " loaded · scroll down for more";
      if (!/\border\s+by\b/i.test(state.stmt)) t += " (tip: add ORDER BY so pages stay consistent)";
    } else if (n > state.pageSize) {
      t += " · all rows loaded";
    }
    setStatus(`${t} · ${r.elapsedMs} ms`);
  }

  function markRunning(from, to) {
    if (runMark) runMark.clear();
    runMark = cm.markText(from, to, { className: "running-stmt" });
  }

  function setStatus(text, cls) {
    $("statusText").textContent = text;
    $("statusbar").className = cls || "";
  }

  // ------------------------------------------------------------------ grid
  const isNumType = (t) => /^(NUMBER|INTEGER|LONG)$/.test(t || "");

  function renderGrid() {
    const r = state.result;
    if (!r) return;
    let rows = r.rows;
    if (state.sort) {
      const { col, dir } = state.sort;
      const numeric = isNumType(r.columns[col].type) || rows.every((x) => x[col] == null || typeof x[col] === "number");
      rows = rows.slice().sort((a, b) => {
        const x = a[col], y = b[col];
        if (x == null && y == null) return 0;
        if (x == null) return 1;           // NULLs always last
        if (y == null) return -1;
        const c = numeric ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
        return dir * c;
      });
    }
    const head = r.columns.map((c, i) => {
      const mark = state.sort && state.sort.col === i ? `<span class="sort">${state.sort.dir > 0 ? "▲" : "▼"}</span>` : "";
      return `<th data-i="${i}" title="${esc(c.name)}${c.type ? " (" + esc(c.type) + ")" : ""}">${esc(c.name)}${mark}</th>`;
    }).join("");
    const empty = r.rows.length ? "" : '<div class="placeholder">Query returned no rows.</div>';
    const g = $("grid");
    const top = g.scrollTop;
    g.innerHTML = `<table class="grid"><thead><tr><th class="rn">#</th>${head}</tr></thead><tbody>${rowsHtml(rows, 0)}</tbody></table>${empty}`;
    g.scrollTop = top;
  }

  function rowsHtml(rows, start) {
    const numCols = state.result.columns.map((c) => isNumType(c.type));
    return rows.map((row, n) =>
      `<tr><td class="rn">${(start + n + 1).toLocaleString()}</td>` + row.map((v, i) => {
        if (v === null || v === undefined) return '<td class="null">NULL</td>';
        const s = String(v);
        return `<td class="${numCols[i] || typeof v === "number" ? "num" : ""}" title="${esc(s.length > 60 ? s.slice(0, 500) : "")}">${esc(s)}</td>`;
      }).join("") + "</tr>").join("");
  }

  // Add freshly loaded rows to the existing table (no full re-render, keeps scroll position)
  function appendRows(start, rows) {
    const tbody = $("grid").querySelector("table.grid tbody");
    if (tbody) tbody.insertAdjacentHTML("beforeend", rowsHtml(rows, start));
  }

  $("grid").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-i]");
    if (!th || !state.result) return;
    const col = Number(th.dataset.i);
    const s = state.sort;
    state.sort = !s || s.col !== col ? { col, dir: 1 } : s.dir === 1 ? { col, dir: -1 } : null;
    renderGrid();
  });

  // ---------------------------------------------------------------- export
  // The server re-runs the statement and writes ALL rows (every page) to a file in EXPORT_DIR (default C:\temp).
  const exp = { job: null, timer: null };

  function guessTable() {
    const ref = parseTables(mask(state.stmt).text)[0];
    if (!ref) return "";
    const name = ref.name.toUpperCase();
    const owners = state.byName.get(name) || [];
    const owner = ref.owner ? ref.owner.toUpperCase() : owners.length === 1 ? owners[0] : "";
    return owner ? `${owner}.${name}` : name;
  }

  async function startExport(fmt) {
    const r = state.result;
    if (!r) return;
    if (exp.job && exp.job.status === "running") { setStatus("An export is already running (Esc cancels it).", "warn"); return; }
    let table = "";
    if (fmt === "sql") {
      const t = window.prompt("Table name for the INSERT statements (OWNER.TABLE):", guessTable());
      if (t === null) return;
      table = t.trim();
    }
    try {
      const job = await api("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sql: state.stmt, format: fmt, table, columns: r.columns.map((c) => c.name) }),
      });
      pollExport(job);
    } catch (e) {
      setStatus("Export failed: " + e.message, "err");
    }
  }

  function pollExport(job) {
    exp.job = job;
    $("btnReveal").hidden = true;
    clearTimeout(exp.timer);
    const label = job.format === "csv" ? "CSV" : "SQL INSERT";
    const tick = async () => {
      try {
        job = await api("/api/export/" + job.id);
      } catch (e) {
        setStatus("Lost track of the export: " + e.message, "err");
        return;
      }
      exp.job = job;
      if (job.status === "running") {
        setStatus(`Exporting ALL rows to ${label}… ${job.rows.toLocaleString()} rows written (Esc to cancel)`);
        exp.timer = setTimeout(tick, 400);
      } else if (job.status === "done") {
        setStatus(`Exported ${job.rows.toLocaleString()} rows to ${job.path}`);
        $("btnReveal").hidden = false;
      } else if (job.status === "cancelled") {
        setStatus("Export cancelled - no file was kept.", "warn");
      } else {
        setStatus("Export failed: " + job.error, "err");
      }
    };
    tick();
  }

  function cancelExport() {
    if (exp.job && exp.job.status === "running") api(`/api/export/${exp.job.id}/cancel`, { method: "POST" }).catch(() => {});
  }

  $("btnReveal").addEventListener("click", () => {
    if (exp.job && exp.job.status === "done") api(`/api/export/${exp.job.id}/reveal`, { method: "POST" }).catch((e) => setStatus(e.message, "err"));
  });

  // ------------------------------------------------------------ grid context menu
  const ctx = $("ctxMenu");
  const hideMenu = () => { ctx.hidden = true; };

  $("grid").addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const r = state.result;
    const exporting = !!(exp.job && exp.job.status === "running");
    const set = (act, text, disabled) => {
      const b = ctx.querySelector(`[data-act="${act}"]`);
      if (text) b.querySelector(".t").textContent = text;
      b.disabled = disabled;
    };
    set("end", state.loadingAll ? "Stop loading" : "Go to end (load all rows)", !r);
    set("csv", null, !r || exporting);
    set("sql", null, !r || exporting);
    ctx.hidden = false;
    ctx.style.left = Math.min(e.clientX, window.innerWidth - ctx.offsetWidth - 4) + "px";
    ctx.style.top = Math.min(e.clientY, window.innerHeight - ctx.offsetHeight - 4) + "px";
  });

  ctx.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b || b.disabled) return;
    hideMenu();
    const act = b.dataset.act;
    if (act === "end") loadAll();
    else startExport(act);
  });
  ctx.addEventListener("contextmenu", (e) => e.preventDefault());
  document.addEventListener("mousedown", (e) => { if (!ctx.contains(e.target)) hideMenu(); });
  window.addEventListener("resize", hideMenu);
  $("grid").addEventListener("scroll", hideMenu, { passive: true });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    hideMenu();
    cancelExport();
    if (state.loadingAll) state.stopAll = true;
  });

  // ------------------------------------------------------------------- tree
  const TREE_LIMIT = 400;
  let treeAbort = 0;

  function renderTree() {
    const tree = $("tree");
    tree.textContent = "";
    if (!state.objects) return;
    const f = $("filter").value.trim().toUpperCase();
    let shown = 0, truncated = false;

    for (const owner of [...state.byOwner.keys()].sort()) {
      let items = state.byOwner.get(owner);
      if (f) items = items.filter((t) => t.name.includes(f) || `${owner}.${t.name}`.includes(f));
      if (!items.length) continue;
      if (shown >= TREE_LIMIT) { truncated = true; break; }
      if (f && shown + items.length > TREE_LIMIT) { items = items.slice(0, TREE_LIMIT - shown); truncated = true; }
      shown += f ? items.length : 0;

      const wrap = el("div");
      const row = el("div", "node owner");
      row.style.setProperty("--lvl", 0);
      const arrow = el("span", "arrow", "▶");
      row.append(arrow, el("span", "ico", "🗄"), el("span", "label", owner), el("span", "meta", String(state.byOwner.get(owner).length)));
      const kids = el("div");
      kids.hidden = true;
      let built = false;
      const toggle = (open) => {
        kids.hidden = !open;
        arrow.textContent = open ? "▼" : "▶";
        if (open && !built) { built = true; items.forEach((t) => kids.appendChild(tableNode(t))); }
      };
      row.addEventListener("click", () => toggle(kids.hidden));
      wrap.append(row, kids);
      tree.appendChild(wrap);
      if (f) toggle(true);
    }
    if (!tree.childNodes.length) tree.appendChild(el("div", "msg", f ? "No tables match." : "No tables found."));
    if (truncated) tree.appendChild(el("div", "msg", `Showing first ${TREE_LIMIT} matches - refine the filter.`));
  }

  function tableNode(t) {
    const wrap = el("div");
    const row = el("div", "node");
    row.style.setProperty("--lvl", 1);
    const arrow = el("span", "arrow", "▶");
    const go = el("button", "go", "▶ SELECT");
    go.title = `SELECT * FROM ${t.owner}.${t.name}`;
    row.append(arrow, el("span", "ico", t.type === "VIEW" ? "◫" : "▦"), el("span", "label", t.name), go);
    row.title = `${t.owner}.${t.name}`;
    const kids = el("div");
    kids.hidden = true;
    let loaded = false;

    row.addEventListener("click", async () => {
      document.querySelectorAll(".node.active").forEach((n) => n.classList.remove("active"));
      row.classList.add("active");
      state.activeTable = { owner: t.owner, name: t.name };
      const open = kids.hidden;
      kids.hidden = !open;
      arrow.textContent = open ? "▼" : "▶";
      if (open && !loaded) {
        loaded = true;
        kids.appendChild(el("div", "msg", "loading…"));
        try {
          const cols = await getColumns(t.owner, t.name);
          kids.textContent = "";
          for (const c of cols) {
            const cr = el("div", "node");
            cr.style.setProperty("--lvl", 2);
            cr.append(el("span", "arrow"), el("span", "ico", "▪"), el("span", "label", c.name), el("span", "meta", c.type + (c.nullable ? "" : " · not null")));
            cr.addEventListener("dblclick", () => insertText(c.name));
            kids.appendChild(cr);
          }
          if (!cols.length) kids.appendChild(el("div", "msg", "no columns"));
        } catch (e) {
          loaded = false;
          kids.textContent = "";
          kids.appendChild(el("div", "msg err", e.message));
        }
      }
    });
    row.addEventListener("dblclick", () => insertText(`${t.owner}.${t.name}`));
    go.addEventListener("click", (e) => {
      e.stopPropagation();
      const stmt = `SELECT * FROM ${t.owner}.${t.name}`;
      const cur = cm.getValue().trimEnd();
      const text = (cur ? cur + "\n\n" : "") + stmt + ";\n";
      cm.setValue(text);
      cm.setCursor(cm.posFromIndex(text.length - 2));
      run();
    });
    wrap.append(row, kids);
    return wrap;
  }

  async function loadObjects(refresh) {
    const tree = $("tree");
    tree.textContent = "";
    tree.appendChild(el("div", "msg", "Loading tables…"));
    const ticket = ++treeAbort;
    try {
      const j = await api("/api/objects" + (refresh ? "?refresh=1" : ""));
      if (ticket !== treeAbort) return;
      if (refresh) { state.colCache.clear(); state.colLoaded.clear(); }
      indexObjects(j.objects);
      renderTree();
    } catch (e) {
      if (ticket !== treeAbort) return;
      tree.textContent = "";
      tree.appendChild(el("div", "msg err", "Could not load tables: " + e.message));
    }
  }

  let filterTimer = null;
  $("filter").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(renderTree, 150); });
  $("btnRefresh").addEventListener("click", () => loadObjects(true));
  $("btnRun").addEventListener("click", () => run());
  $("btnCsv").addEventListener("click", () => startExport("csv"));

  // -------------------------------------------------------------- splitters
  function makeSplitter(handle, bodyClass, onMove) {
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      handle.classList.add("dragging");
      document.body.classList.add(bodyClass);
      const move = (ev) => { onMove(ev); requestAnimationFrame(() => cm.refresh()); };
      const up = () => {
        handle.classList.remove("dragging");
        document.body.classList.remove(bodyClass);
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", up);
        cm.refresh();
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", up);
    });
  }

  const sidebar = $("sidebar"), editorPane = $("editorPane"), workspace = $("workspace");

  function setSidebarWidth(px) {
    const w = clamp(px, 160, Math.max(200, window.innerWidth * 0.6));
    sidebar.style.width = w + "px";
    store.set("sideW", w);
  }
  function setEditorHeight(px) {
    const h = clamp(px, 80, workspace.getBoundingClientRect().height - 60);
    editorPane.style.flex = `0 0 ${h}px`;
    store.set("editorH", h);
  }

  makeSplitter($("vsplit"), "dragging-v", (e) => setSidebarWidth(e.clientX - sidebar.getBoundingClientRect().left));
  makeSplitter($("hsplit"), "dragging-h", (e) => setEditorHeight(e.clientY - workspace.getBoundingClientRect().top));

  if (store.get("sideW")) sidebar.style.width = clamp(Number(store.get("sideW")), 160, window.innerWidth * 0.6) + "px";
  if (store.get("editorH")) setEditorHeight(Number(store.get("editorH")));
  window.addEventListener("resize", () => { if (store.get("editorH")) setEditorHeight(Number(store.get("editorH"))); cm.refresh(); });

  // ------------------------------------------------------------------ boot
  async function boot() {
    const conn = $("conn"), txt = $("connText");
    try {
      const cfg = await api("/api/config");
      state.pageSize = cfg.maxRows;
      if (cfg.exportDir) $("ctxDir").textContent = cfg.exportDir;
      if (!cfg.configured) {
        conn.className = "conn bad";
        txt.textContent = "not configured - edit .env";
        $("tree").appendChild(el("div", "msg err", "ThingWorx settings are missing in the .env file."));
        return;
      }
      txt.textContent = `${cfg.thing}.${cfg.service}`;
    } catch (e) {
      conn.className = "conn bad";
      txt.textContent = "server unreachable";
      return;
    }
    loadObjects(false);
    try {
      await api("/api/ping");
      conn.className = "conn ok";
      conn.title = "Connected to ThingWorx / Tibero";
    } catch (e) {
      conn.className = "conn bad";
      conn.title = e.message;
      txt.textContent += " - connection failed";
    }
  }
  boot();
})();
