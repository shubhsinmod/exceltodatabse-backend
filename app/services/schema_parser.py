"""
schema_parser.py
----------------
Parse a SQL Server schema script (SSMS-generated) into structured metadata.

Extracts for every table, dynamically:
  - table name & schema name
  - columns:
      name, data_type (generic), sql_type (raw), length, precision, scale,
      nullable, is_identity, has_default, default_value, is_pk, is_unique
  - primary keys
  - foreign keys (column, referenced_table, referenced_column)

Zero hardcoding of any table/column names.
"""

import re
import json


# ─────────────────────────────────────────────────────────────────────────────
# Encoding / text utilities
# ─────────────────────────────────────────────────────────────────────────────

def _detect_encoding(raw_bytes: bytes) -> str:
    """Detect BOM-based encoding; fall back to utf-8."""
    if raw_bytes[:3] == b'\xef\xbb\xbf':
        return 'utf-8-sig'
    if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return 'utf-16'
    return 'utf-8'


def _strip_identifier(name: str) -> str:
    """Remove square brackets, backticks, double-quotes and surrounding whitespace."""
    return name.strip().strip('[]`"').strip()


# ─────────────────────────────────────────────────────────────────────────────
# SQL type mapping  (generic categories for formatting logic)
# ─────────────────────────────────────────────────────────────────────────────

_INTEGER_TYPES  = frozenset({'int','bigint','smallint','tinyint','integer'})
_FLOAT_TYPES    = frozenset({'decimal','numeric','float','real','money',
                             'smallmoney','double','double precision'})
_DATE_TYPES     = frozenset({'date','datetime','datetime2','smalldatetime',
                             'time','datetimeoffset','timestamp'})
_BOOLEAN_TYPES  = frozenset({'bit','boolean','bool'})


def _sql_type_to_generic(raw: str) -> str:
    """Map a SQL Server type name to one of: integer | float | date | boolean | string."""
    t = raw.lower().strip()
    if t in _INTEGER_TYPES:  return 'integer'
    if t in _FLOAT_TYPES:    return 'float'
    if t in _DATE_TYPES:     return 'date'
    if t in _BOOLEAN_TYPES:  return 'boolean'
    return 'string'


# ─────────────────────────────────────────────────────────────────────────────
# Balanced-paren extractor
# ─────────────────────────────────────────────────────────────────────────────

def _extract_balanced(text: str, start: int) -> str:
    """
    Return the content between the parentheses that opens at `start`.
    `start` must be the index of the opening '('.
    """
    depth  = 0
    result = []
    for ch in text[start:]:
        if ch == '(':
            depth += 1
            if depth > 1:
                result.append(ch)
        elif ch == ')':
            depth -= 1
            if depth == 0:
                break
            result.append(ch)
        else:
            result.append(ch)
    return ''.join(result)


def _split_top_level(body: str) -> list:
    """Split by commas that are NOT inside nested parentheses."""
    depth, parts, current = 0, [], []
    for ch in body:
        if   ch == '(':                     depth += 1; current.append(ch)
        elif ch == ')':                     depth -= 1; current.append(ch)
        elif ch == ',' and depth == 0:      parts.append(''.join(current).strip()); current = []
        else:                               current.append(ch)
    if current:
        parts.append(''.join(current).strip())
    return parts


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT constraint extraction
# ─────────────────────────────────────────────────────────────────────────────

# Matches:  DEFAULT (value)   DEFAULT value   DEFAULT ((value))
_DEFAULT_RE = re.compile(
    r'\bDEFAULT\s*'
    r'('
    r'  \(\s*\(\s*([^()]+?)\s*\)\s*\)'   # (( value ))
    r'  |'
    r'  \(\s*([^()]*?)\s*\)'              # ( value )
    r'  |'
    r'  ([^\s,]+)'                         # bare value
    r')',
    re.IGNORECASE | re.VERBOSE
)


def _extract_default(col_def_text: str):
    """
    Extract the DEFAULT value from a column definition fragment.
    Returns (has_default: bool, default_value: str | None).
    default_value is the raw SQL expression (e.g. '1', 'getdate()', 'N/A').
    """
    m = _DEFAULT_RE.search(col_def_text)
    if not m:
        return False, None
    # pick the first non-None capturing group (group 2, 3, or 4)
    val = m.group(2) or m.group(3) or m.group(4)
    if val is not None:
        val = val.strip()
    return True, val


# ─────────────────────────────────────────────────────────────────────────────
# Type size extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_type_size(size_str: str | None, generic_type: str) -> dict:
    """
    Parse the optional size/precision from the parentheses after a type name.
    Returns a dict with keys length, precision, scale (each int or None).
    """
    length = precision = scale = None
    if not size_str:
        return {"length": length, "precision": precision, "scale": scale}
    size_str = size_str.strip()
    if size_str.upper() in ('MAX', ''):
        length = -1  # MAX
        return {"length": length, "precision": precision, "scale": scale}
    parts = [p.strip() for p in size_str.split(',')]
    if generic_type == 'float' and len(parts) == 2:
        try: precision = int(parts[0])
        except ValueError: pass
        try: scale = int(parts[1])
        except ValueError: pass
    else:
        try: length = int(parts[0])
        except ValueError: pass
    return {"length": length, "precision": precision, "scale": scale}


# ─────────────────────────────────────────────────────────────────────────────
# Column definition parser
# ─────────────────────────────────────────────────────────────────────────────

# SQL keywords that can appear as the first token of a line but are NOT columns
_SKIP_KEYWORDS = frozenset({
    'CONSTRAINT', 'PRIMARY', 'FOREIGN', 'UNIQUE', 'INDEX',
    'CHECK', 'ALTER', 'CREATE', 'GO', 'SET', 'USE',
})

# Column definition pattern:
#   [ColName] | ColName  followed by  [TypeName] | TypeName  optionally followed by (size)
_COL_DEF_RE = re.compile(
    r'^(\[?[\w\s]+?\]?)\s+'          # group 1: column name (may have spaces inside brackets)
    r'(\[?\w+\]?)'                   # group 2: data type
    r'(?:\s*\(([^)]*)\))?'           # group 3: optional size/precision
    r'(.*)',                          # group 4: rest (IDENTITY, NULL, DEFAULT, PRIMARY KEY...)
    re.IGNORECASE | re.DOTALL
)


def _parse_column_stmt(stmt: str) -> dict | None:
    """
    Parse a single column definition and return a column metadata dict, or None
    if the statement is a constraint/keyword line rather than a column.
    """
    stmt = stmt.strip()
    if not stmt:
        return None

    upper = stmt.upper().lstrip()

    # Skip constraint/keyword lines
    first_token = re.split(r'\s', upper)[0].strip('[]')
    if first_token in _SKIP_KEYWORDS:
        return None
    # Also skip lines starting with CONSTRAINT keyword pattern
    if re.match(r'(CONSTRAINT\s+\S+\s+)?(PRIMARY|FOREIGN|UNIQUE|CHECK)\s', upper):
        return None

    m = _COL_DEF_RE.match(stmt)
    if not m:
        return None

    col_name_raw = _strip_identifier(m.group(1))
    sql_type_raw = _strip_identifier(m.group(2))
    size_str     = m.group(3)   # may be None
    rest         = m.group(4)   # everything after type(size)

    # Safety: skip if name matches a keyword
    if col_name_raw.upper() in _SKIP_KEYWORDS:
        return None
    if not col_name_raw:
        return None

    generic_type = _sql_type_to_generic(sql_type_raw)
    size_info    = _extract_type_size(size_str, generic_type)

    rest_upper = rest.upper()

    is_identity   = bool(re.search(r'\bIDENTITY\b', rest_upper))
    is_pk_inline  = bool(re.search(r'\bPRIMARY\s+KEY\b', rest_upper))
    nullable      = not bool(re.search(r'\bNOT\s+NULL\b', rest_upper))
    has_default, default_value = _extract_default(rest)

    return {
        "name":          col_name_raw,
        "sql_type":      sql_type_raw,           # raw type as written (VARCHAR, INT …)
        "data_type":     generic_type,           # generic: string | integer | float | date | boolean
        "length":        size_info["length"],
        "precision":     size_info["precision"],
        "scale":         size_info["scale"],
        "nullable":      nullable,
        "is_identity":   is_identity,
        "has_default":   has_default,
        "default_value": default_value,          # raw SQL expression or None
        "is_pk":         False,                  # resolved later via PKs list
        "is_unique":     False,                  # resolved later
    }, is_pk_inline


# ─────────────────────────────────────────────────────────────────────────────
# Table body parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_table_body(body: str) -> tuple:
    """
    Parse all column definitions, inline PKs, and inline FKs from a CREATE TABLE body.
    Returns (columns: list, pks: list[str_lower], fks: list[dict]).
    """
    columns = []
    pks     = []
    fks     = []

    for stmt in _split_top_level(body):
        stmt = stmt.strip()
        if not stmt:
            continue

        upper = stmt.upper().lstrip()

        # ── inline PRIMARY KEY constraint ──────────────────────────────────
        if re.match(r'(CONSTRAINT\s+\S+\s+)?PRIMARY\s+KEY', upper):
            pk_m = re.search(r'\(([^)]+)\)', stmt)
            if pk_m:
                for c in pk_m.group(1).split(','):
                    tok = _strip_identifier(c).split()[0]
                    if tok:
                        pks.append(tok.lower())
            continue

        # ── inline FOREIGN KEY constraint ─────────────────────────────────
        if re.match(r'(CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY', upper):
            fk_m = re.search(
                r'FOREIGN\s+KEY\s*\(([^)]+)\)\s*REFERENCES\s+(\S+)\s*\(([^)]+)\)',
                stmt, re.IGNORECASE
            )
            if fk_m:
                fks.append({
                    "column":            _strip_identifier(fk_m.group(1)),
                    "referenced_table":  _strip_identifier(fk_m.group(2)),
                    "referenced_column": _strip_identifier(fk_m.group(3)),
                })
            continue

        # ── UNIQUE / CHECK / INDEX constraints ────────────────────────────
        if re.match(r'(CONSTRAINT\s+\S+\s+)?(UNIQUE|CHECK|INDEX)', upper):
            continue

        # ── column definition ─────────────────────────────────────────────
        result = _parse_column_stmt(stmt)
        if result is None:
            continue

        col_dict, is_pk_inline = result
        if is_pk_inline:
            pks.append(col_dict["name"].lower())

        columns.append(col_dict)

    return columns, pks, fks


# ─────────────────────────────────────────────────────────────────────────────
# Main public function
# ─────────────────────────────────────────────────────────────────────────────

def parse_sql_schema(content, is_json: bool = False) -> list:
    """
    Parse a SQL Server schema script and return a list of table metadata dicts.

    Accepts either a str or raw bytes (encoding is auto-detected from BOM).

    Each returned dict has the structure:
    {
        "schema":       str,          # e.g. "dbo"
        "table_name":   str,
        "columns": [
            {
                "name":          str,
                "sql_type":      str,   # raw SQL type
                "data_type":     str,   # integer|float|date|boolean|string
                "length":        int|None,
                "precision":     int|None,
                "scale":         int|None,
                "nullable":      bool,
                "is_identity":   bool,
                "has_default":   bool,
                "default_value": str|None,
                "is_pk":         bool,
                "is_unique":     bool,
            }, ...
        ],
        "foreign_keys": [
            {
                "column":            str,
                "referenced_table":  str,
                "referenced_column": str,
            }, ...
        ]
    }
    """
    # ── handle raw bytes ──────────────────────────────────────────────────────
    if isinstance(content, (bytes, bytearray)):
        enc = _detect_encoding(bytes(content))
        content = content.decode(enc, errors='ignore')

    # ── JSON schema ───────────────────────────────────────────────────────────
    if is_json:
        try:
            return json.loads(content)
        except Exception:
            return []

    # ── pre-process ──────────────────────────────────────────────────────────
    content = re.sub(r'/\*.*?\*/', ' ', content, flags=re.DOTALL)   # block comments
    content = re.sub(r'--[^\n]*', ' ', content)                      # line comments
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    tables: dict = {}   # key = "schema.table" (lowercase) → metadata

    # ── 1. CREATE TABLE blocks ────────────────────────────────────────────────
    table_header_re = re.compile(
        r'CREATE\s+TABLE\s+'
        r'((?:\[?[^\]\s,()]+\]?\s*\.\s*)?'   # optional schema.
        r'\[?[^\]\s,()]+\]?)'                  # table name
        r'\s*\(',
        re.IGNORECASE
    )

    for m in table_header_re.finditer(content):
        full_name = _strip_identifier(m.group(1))

        if '.' in full_name:
            parts  = full_name.split('.', 1)
            schema = _strip_identifier(parts[0]) or 'dbo'
            table  = _strip_identifier(parts[1])
        else:
            schema = 'dbo'
            table  = full_name

        table_key = f"{schema}.{table}".lower()

        body = _extract_balanced(content, m.end() - 1)
        columns, pks, fks = _parse_table_body(body)

        tables[table_key] = {
            "schema":     schema,
            "table_name": table,
            "columns":    columns,
            "pks":        pks,
            "fks":        fks,
        }

    # ── 2. ALTER TABLE … FOREIGN KEY ─────────────────────────────────────────
    fk_re = re.compile(
        r'ALTER\s+TABLE\s+((?:\[?[^\]\s,()]+\]?\s*\.\s*)?\[?[^\]\s,()]+\]?)'
        r'\s+(?:WITH\s+(?:CHECK|NOCHECK)\s+)?'
        r'ADD\s+(?:CONSTRAINT\s+\[?[^\]\s,()]+\]?\s+)?'
        r'FOREIGN\s+KEY\s*\(([^)]+)\)\s*'
        r'REFERENCES\s+((?:\[?[^\]\s,()]+\]?\s*\.\s*)?\[?[^\]\s,()]+\]?)\s*\(([^)]+)\)',
        re.IGNORECASE
    )
    for m in fk_re.finditer(content):
        src_table = _strip_identifier(m.group(1))
        src_col   = _strip_identifier(m.group(2))
        ref_table = _strip_identifier(m.group(3))
        ref_col   = _strip_identifier(m.group(4))
        if '.' not in src_table: src_table = f"dbo.{src_table}"
        if '.' not in ref_table: ref_table = f"dbo.{ref_table}"
        tk = src_table.lower()
        if tk in tables:
            tables[tk]["fks"].append({
                "column":            src_col,
                "referenced_table":  ref_table,
                "referenced_column": ref_col,
            })

    # ── 3. ALTER TABLE … PRIMARY KEY ─────────────────────────────────────────
    pk_re = re.compile(
        r'ALTER\s+TABLE\s+((?:\[?[^\]\s,()]+\]?\s*\.\s*)?\[?[^\]\s,()]+\]?)'
        r'\s+ADD\s+(?:CONSTRAINT\s+\[?[^\]\s,()]+\]?\s+)?'
        r'PRIMARY\s+KEY\s+\w*\s*\(([^)]+)\)',
        re.IGNORECASE
    )
    for m in pk_re.finditer(content):
        tname = _strip_identifier(m.group(1))
        if '.' not in tname: tname = f"dbo.{tname}"
        tk = tname.lower()
        if tk in tables:
            pk_cols = [
                _strip_identifier(c).split()[0].lower()
                for c in m.group(2).split(',')
            ]
            tables[tk]["pks"].extend(pk_cols)

    # ── 4. Resolve is_pk on each column ──────────────────────────────────────
    for t in tables.values():
        pk_set = {p.lower() for p in t["pks"]}
        for col in t["columns"]:
            if col["name"].lower() in pk_set:
                col["is_pk"] = True

    # ── 5. Build and return result ────────────────────────────────────────────
    result = []
    for t in tables.values():
        result.append({
            "schema":       t["schema"],
            "table_name":   t["table_name"],
            "columns":      t["columns"],
            "foreign_keys": t["fks"],
        })

    if not result:
        raise Exception(
            "No database tables were detected in the uploaded SQL schema file. "
            "Please make sure the file contains CREATE TABLE statements."
        )

    return result
