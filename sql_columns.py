"""Work out the column names/order of a SELECT from its text.

The ThingWorx JSON service returns row objects: column order is not guaranteed and columns that are
NULL are omitted. When the select list is simple (no `*`) we use it to restore the order and to keep
columns that were NULL in every row. Anything we can't understand -> None (caller keeps service order).
"""
import re

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
_NOT_ALIAS = {"AND", "OR", "NOT", "IN", "IS", "LIKE", "BETWEEN", "THEN", "ELSE", "WHEN", "CASE", "NULL", "END",
              "ASC", "DESC", "OVER", "FROM", "SELECT", "DISTINCT", "BY", "ON", "AS", "ALL"}
_NOT_ALIAS_PREV = _NOT_ALIAS - {"END", "NULL"}      # `CASE ... END flag`, `NULL flag` do take an alias


def _tokens(sql):
    """Yield (kind, text, depth): kind is word | quoted | str | num | sym. Comments are dropped."""
    i, n, depth = 0, len(sql), 0
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            yield "str", sql[i:j + 1], depth
            i = j + 1
        elif c == '"':
            j = sql.find('"', i + 1)
            j = n - 1 if j < 0 else j
            yield "quoted", sql[i + 1:j], depth
            i = j + 1
        elif c.isdigit():
            m = re.compile(r"[\d.]+").match(sql, i)
            yield "num", m.group(0), depth
            i = m.end()
        else:
            m = _WORD.match(sql, i)
            if m:
                yield "word", m.group(0), depth
                i = m.end()
            else:
                if c == "(":
                    yield "sym", c, depth
                    depth += 1
                elif c == ")":
                    depth = max(0, depth - 1)
                    yield "sym", c, depth
                else:
                    yield "sym", c, depth
                i += 1


def select_list_sources(sql):
    """[(output_name, source_column_or_None)] for the first top-level SELECT, or None if it can't be determined.

    source_column is set when the item is a plain column (optionally qualified / aliased), so an alias can be
    traced back to the table column it came from.
    """
    toks = list(_tokens(sql))
    start = next((i for i, t in enumerate(toks) if t[0] == "word" and t[1].upper() == "SELECT" and t[2] == 0), None)
    if start is None:
        return None

    items, cur = [], []
    for kind, text, depth in toks[start + 1:]:
        if depth == 0 and kind == "word" and text.upper() == "FROM":
            break
        if depth == 0 and kind == "sym" and text == ",":
            items.append(cur)
            cur = []
        else:
            cur.append((kind, text))
    else:
        return None            # no FROM (e.g. SELECT 1) -> not worth guessing
    items.append(cur)

    # drop a leading DISTINCT / UNIQUE / ALL
    if items and items[0] and items[0][0][0] == "word" and items[0][0][1].upper() in ("DISTINCT", "UNIQUE", "ALL"):
        items[0] = items[0][1:]

    out = []
    for it in items:
        if not it:
            return None
        if it[-1] == ("sym", "*"):
            return None        # SELECT * / t.* - unknown columns
        name, alias_len = None, 0
        last_kind, last = it[-1]
        if last_kind in ("word", "quoted") and not (last_kind == "word" and last.upper() in _NOT_ALIAS):
            if len(it) == 1:
                name = last
            else:
                prev_kind, prev = it[-2]
                if prev_kind == "sym" and prev == ".":
                    name = last                                        # t.col
                elif prev_kind == "word" and prev.upper() == "AS":
                    name, alias_len = last, 2                          # expr AS alias
                elif prev_kind in ("word", "quoted", "str", "num") and not (prev_kind == "word" and prev.upper() in _NOT_ALIAS_PREV):
                    name, alias_len = last, 1                          # expr alias
                elif prev_kind == "sym" and prev == ")":
                    name, alias_len = last, 1                          # func(x) alias
        if name is None:
            return None
        out.append((name if last_kind == "quoted" else name.upper(), _source_column(it[:len(it) - alias_len])))
    return out


def _source_column(body):
    """`col`, `t.col`, `o.t.col` -> 'COL'; anything else (expressions, functions) -> None."""
    if not body or len(body) % 2 == 0:
        return None
    for i, (kind, text) in enumerate(body):
        if i % 2 == 0 and kind not in ("word", "quoted"):
            return None
        if i % 2 == 1 and (kind, text) != ("sym", "."):
            return None
    kind, text = body[-1]
    return text if kind == "quoted" else text.upper()


def select_list_names(sql):
    """Return the output column names of the first top-level SELECT, or None if they can't be determined."""
    parsed = select_list_sources(sql)
    return [n for n, _ in parsed] if parsed else None


def order_columns(sql, keys):
    """Order/complete `keys` (row-object keys seen in the result) using the SELECT list when possible."""
    keys = list(dict.fromkeys(keys))
    names = select_list_names(sql)
    if not names:
        return keys
    by_upper = {k.upper(): k for k in keys}
    ordered, used = [], set()
    for n in names:
        k = by_upper.get(n.upper(), n)
        if k not in used:
            ordered.append(k)
            used.add(k)
    ordered.extend(k for k in keys if k not in used)       # anything we failed to parse stays visible
    return ordered
