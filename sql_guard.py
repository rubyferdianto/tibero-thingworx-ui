"""Read-only guard: only a single SELECT (or WITH ... SELECT) statement may pass."""
import re

class SqlNotAllowed(ValueError):
    pass


# Comments and literals are blanked out before keyword checks so that e.g.
# WHERE note = 'please delete me' or -- update later don't trigger false positives.
_BLANKABLE = re.compile(
    r"""
      --[^\n]*                                              # line comment
    | /\*.*?(?:\*/|\Z)                                      # block comment (incl. hints)
    | (?<![\w$#])[qQ]'(?:\[.*?\]|\{.*?\}|\(.*?\)|<.*?>|(?P<d>[^\s\[\{\(<]).*?(?P=d))'   # q'[...]' literal
    | '(?:[^']|'')*(?:'|\Z)                                 # string literal
    | "[^"]*(?:"|\Z)                                        # quoted identifier
    """,
    re.S | re.X,
)

_FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT",
    "DROP", "ALTER", "CREATE", "TRUNCATE", "RENAME",
    "GRANT", "REVOKE", "COMMENT",
    "EXEC", "EXECUTE", "CALL", "BEGIN", "DECLARE",
    "COMMIT", "ROLLBACK", "SAVEPOINT", "SET",
    "LOCK", "INTO", "ANALYZE", "FLASHBACK", "PURGE",
}
_FORBIDDEN_PREFIXES = ("DBMS_", "UTL_")   # packages that can write files / run jobs / send mail

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")


def _blank(sql: str) -> str:
    def repl(m):
        t = m.group(0)
        return " " if t.startswith(("--", "/*")) else "''"
    return _BLANKABLE.sub(repl, sql)


def validate_select(sql: str) -> str:
    """Return the statement ready to send (trailing ';' removed) or raise SqlNotAllowed."""
    if not sql or not sql.strip():
        raise SqlNotAllowed("Query is empty.")

    skeleton = _blank(sql).strip()
    while skeleton.endswith(";"):
        skeleton = skeleton[:-1].rstrip()
    if ";" in skeleton:
        raise SqlNotAllowed("Only one statement can be executed at a time.")
    if not skeleton:
        raise SqlNotAllowed("Query is empty.")

    words = [w.upper() for w in _WORD.findall(skeleton)]
    if not words or words[0] not in ("SELECT", "WITH"):
        raise SqlNotAllowed("Only SELECT statements are allowed.")

    for w in words:
        if w in _FORBIDDEN:
            raise SqlNotAllowed(f"'{w}' is not allowed - this connection is read-only (SELECT only).")
        if w.startswith(_FORBIDDEN_PREFIXES):
            raise SqlNotAllowed(f"Package '{w}' is not allowed - this connection is read-only.")

    # Return the original text (comments intact) minus trailing semicolons/whitespace.
    cleaned = sql.strip()
    while cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    return cleaned
