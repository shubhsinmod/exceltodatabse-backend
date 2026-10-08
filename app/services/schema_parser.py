"""
schema_parser.py
----------------
Deterministic database schema parser.
Supports T-SQL (SQL Server), MySQL, and PostgreSQL dialects.

Extracts dynamically for every table:
  - table name & schema name
  - columns:
      name, sql_type, data_type, length, precision, scale,
      nullable, is_identity, auto_increment, has_default, default_value, is_pk, is_unique
  - primary keys (single and composite)
  - foreign keys (child_table, child_column, parent_table, parent_column)
  - constraints

Zero hardcoded tables, columns, or assumptions.
"""

import re
import json


def _detect_encoding(raw_bytes: bytes) -> str:
    """Detect BOM-based encoding; fall back to utf-8."""
    if raw_bytes[:3] == b'\xef\xbb\xbf':
        return 'utf-8-sig'
    if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return 'utf-16'
    return 'utf-8'


def _strip_identifier(name: str) -> str:
    """Remove square brackets, backticks, double-quotes and surrounding whitespace."""
    if not name:
        return ""
    name = name.strip()
    # Handle bracketed or quoted tokens
    name = name.strip('[]`"\'').strip()
    return name


def _clean_column_token(raw_token: str) -> str:
    """
    Extract clean column name from a constraint token which may include:
    [ColumnName] ASC, `ColumnName` DESC, ColumnName, etc.
    """
    if not raw_token:
        return ""
    token = raw_token.strip()
    # Split by whitespace first to isolate the column token from ASC/DESC/NULL etc.
    parts = token.split()
    if parts:
        token = parts[0]
    return _strip_identifier(token)


# ─────────────────────────────────────────────────────────────────────────────
# SQL type mapping (generic categories)
# ─────────────────────────────────────────────────────────────────────────────

_INTEGER_TYPES = frozenset({'int', 'bigint', 'smallint', 'tinyint', 'integer', 'serial', 'bigserial', 'smallserial'})
_FLOAT_TYPES = frozenset({'decimal', 'numeric', 'float', 'real', 'money', 'smallmoney', 'double', 'double precision'})
_DATE_TYPES = frozenset({'date', 'datetime', 'datetime2', 'smalldatetime', 'time', 'datetimeoffset', 'timestamp', 'timestamptz'})
_BOOLEAN_TYPES = frozenset({'bit', 'boolean', 'bool'})


def _sql_type_to_generic(raw: str) -> str:
    t = raw.lower().strip()
    if t in _INTEGER_TYPES:
        return 'integer'
    if t in _FLOAT_TYPES:
        return 'float'
    if t in _DATE_TYPES:
        return 'date'
    if t in _BOOLEAN_TYPES:
        return 'boolean'
    return 'string'


# ─────────────────────────────────────────────────────────────────────────────
# Balanced-paren extractor
# ─────────────────────────────────────────────────────────────────────────────

def _extract_balanced(text: str, start: int) -> str:
    depth = 0
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
        if ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())
    return parts


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT constraint extraction
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_RE = re.compile(
    r'\bDEFAULT\s+'
    r'('
    r'  \(\s*\(\s*([^()]+?)\s*\)\s*\)'   # (( value ))
    r'  |'
    r'  \(\s*([^()]*?)\s*\)'              # ( value )
    r'  |'
    r'  ([^\s,;)]+)'                      # bare value
    r')',
    re.IGNORECASE | re.VERBOSE
)


def _extract_default(col_def_text: str):
    m = _DEFAULT_RE.search(col_def_text)
    if not m:
        return False, None
    val = m.group(2) or m.group(3) or m.group(4)
    if val is not None:
        val = val.strip()
    return True, val


def _extract_type_size(size_str: str | None, generic_type: str) -> dict:
    length = precision = scale = None
    if not size_str:
        return {"length": length, "precision": precision, "scale": scale}
    size_str = size_str.strip()
    if size_str.upper() in ('MAX', ''):
        return {"length": -1, "precision": precision, "scale": scale}
    parts = [p.strip() for p in size_str.split(',')]
    if generic_type == 'float' and len(parts) == 2:
        try:
            precision = int(parts[0])
        except ValueError:
            pass
        try:
            scale = int(parts[1])
        except ValueError:
            pass
    else:
        try:
            length = int(parts[0])
        except ValueError:
            pass
    return {"length": length, "precision": precision, "scale": scale}


_SKIP_KEYWORDS = frozenset({
    'CONSTRAINT', 'PRIMARY', 'FOREIGN', 'UNIQUE', 'INDEX',
    'CHECK', 'ALTER', 'CREATE', 'GO', 'SET', 'USE',
})

_COL_DEF_RE = re.compile(
    r'^([\[`"]?[\w\s]+?[\]`"]?)\s+'  # column name
    r'([\[`"]?\w+[\]`"]?)'          # data type
    r'(?:\s*\(([^)]*)\))?'          # optional size / precision
    r'(.*)',                        # rest
    re.IGNORECASE | re.DOTALL
)


def _parse_column_stmt(stmt: str) -> tuple | None:
    stmt = stmt.strip()
    if not stmt:
        return None

    upper = stmt.upper().lstrip()
    first_token = re.split(r'\s', upper)[0].strip('[]`"')
    if first_token in _SKIP_KEYWORDS:
        return None
    if re.match(r'(CONSTRAINT\s+\S+\s+)?(PRIMARY|FOREIGN|UNIQUE|CHECK)\s', upper):
        return None

    m = _COL_DEF_RE.match(stmt)
    if not m:
        return None

    col_name_raw = _strip_identifier(m.group(1))
    sql_type_raw = _strip_identifier(m.group(2))
    size_str = m.group(3)
    rest = m.group(4)

    if col_name_raw.upper() in _SKIP_KEYWORDS or not col_name_raw:
        return None

    generic_type = _sql_type_to_generic(sql_type_raw)
    size_info = _extract_type_size(size_str, generic_type)
    rest_upper = rest.upper()

    is_identity = bool(re.search(r'\bIDENTITY\b', rest_upper))
    is_auto_inc = bool(re.search(r'\bAUTO_INCREMENT\b|\bSERIAL\b', rest_upper)) or is_identity
    is_pk_inline = bool(re.search(r'\bPRIMARY\s+KEY\b', rest_upper))
    is_unique_inline = bool(re.search(r'\bUNIQUE\b', rest_upper))
    nullable = not bool(re.search(r'\bNOT\s+NULL\b', rest_upper))
    has_default, default_value = _extract_default(rest)

    return {
        "name": col_name_raw,
        "sql_type": sql_type_raw,
        "data_type": generic_type,
        "length": size_info["length"],
        "precision": size_info["precision"],
        "scale": size_info["scale"],
        "nullable": nullable,
        "is_identity": is_identity,
        "auto_increment": is_auto_inc,
        "has_default": has_default,
        "default_value": default_value,
        "is_pk": is_pk_inline,
        "is_unique": is_unique_inline,
    }, is_pk_inline


def _parse_table_body(body: str, current_table_name: str) -> tuple:
    columns = []
    pks = []
    fks = []

    for stmt in _split_top_level(body):
        stmt = stmt.strip()
        if not stmt:
            continue

        upper = stmt.upper().lstrip()

        # ── PRIMARY KEY constraint (inline / named) ────────────────────────
        # Supports:
        #   PRIMARY KEY (col1, col2)
        #   CONSTRAINT [PK_Name] PRIMARY KEY (col1)
        #   CONSTRAINT [PK_Name] PRIMARY KEY CLUSTERED ([col1] ASC)
        if re.search(r'\bPRIMARY\s+KEY\b', upper):
            pk_m = re.search(r'\bPRIMARY\s+KEY(?:\s+CLUSTERED|\s+NONCLUSTERED)?\s*\(([^)]+)\)', stmt, re.IGNORECASE)
            if pk_m:
                for c in pk_m.group(1).split(','):
                    col_tok = _clean_column_token(c)
                    if col_tok:
                        pks.append(col_tok.lower())
                continue

        # ── FOREIGN KEY constraint ─────────────────────────────────────────
        # Supports:
        #   FOREIGN KEY (child_col) REFERENCES parent_table (parent_col)
        #   CONSTRAINT fk_name FOREIGN KEY (child_col) REFERENCES parent_table(parent_col)
        if re.search(r'\bFOREIGN\s+KEY\b', upper):
            fk_m = re.search(
                r'FOREIGN\s+KEY\s*\(([^)]+)\)\s*REFERENCES\s+([\[`"]?[\w\s.]+?[\]`"]?)\s*\(([^)]+)\)',
                stmt, re.IGNORECASE
            )
            if fk_m:
                child_cols = [_clean_column_token(c) for c in fk_m.group(1).split(',')]
                ref_table_raw = _strip_identifier(fk_m.group(2))
                parent_cols = [_clean_column_token(c) for c in fk_m.group(3).split(',')]

                ref_t = ref_table_raw.split('.')[-1].strip('[]`"')

                for c_col, p_col in zip(child_cols, parent_cols):
                    fks.append({
                        "child_table": current_table_name,
                        "child_column": c_col,
                        "parent_table": ref_t,
                        "parent_column": p_col,
                        # Also keep backward compatible keys:
                        "column": c_col,
                        "referenced_table": ref_t,
                        "referenced_column": p_col
                    })
                continue

        if re.match(r'(CONSTRAINT\s+\S+\s+)?(UNIQUE|CHECK|INDEX)', upper):
            continue

        result = _parse_column_stmt(stmt)
        if result is None:
            continue

        col_dict, is_pk_inline = result
        if is_pk_inline:
            pks.append(col_dict["name"].lower())

        columns.append(col_dict)

    return columns, pks, fks


def parse_sql_schema(content, is_json: bool = False) -> list:
    """
    Parse a SQL schema script deterministically and return list of table metadata dicts.
    Accepts string or raw bytes.
    """
    if isinstance(content, (bytes, bytearray)):
        enc = _detect_encoding(bytes(content))
        content = content.decode(enc, errors='ignore')

    if is_json:
        try:
            return json.loads(content)
        except Exception:
            return []

    # Strip block comments
    content = re.sub(r'/\*.*?\*/', ' ', content, flags=re.DOTALL)
    # Strip line comments
    content = re.sub(r'--[^\n]*', ' ', content)
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    tables: dict = {}

    table_header_re = re.compile(
        r'CREATE\s+TABLE\s+'
        r'((?:[\[`"]?[^\]`"\s,()]+[\]`"]?\s*\.\s*)?'   # optional schema.
        r'[\[`"]?[^\]`"\s,()]+[\]`"]?)'                 # table name
        r'\s*\(',
        re.IGNORECASE
    )

    for m in table_header_re.finditer(content):
        full_name = _strip_identifier(m.group(1))

        if '.' in full_name:
            parts = full_name.split('.', 1)
            schema = _strip_identifier(parts[0]) or 'dbo'
            table = _strip_identifier(parts[1])
        else:
            schema = 'dbo'
            table = full_name

        table_key = f"{schema}.{table}".lower()

        body = _extract_balanced(content, m.end() - 1)
        columns, pks, fks = _parse_table_body(body, current_table_name=table)

        tables[table_key] = {
            "schema": schema,
            "table_name": table,
            "columns": columns,
            "pks": pks,
            "fks": fks,
        }

    # ── ALTER TABLE … FOREIGN KEY ────────────────────────────────────────────
    alter_fk_re = re.compile(
        r'ALTER\s+TABLE\s+((?:[\[`"]?[^\]`"\s,()]+[\]`"]?\s*\.\s*)?[\[`"]?[^\]`"\s,()]+[\]`"]?)'
        r'\s+(?:WITH\s+(?:CHECK|NOCHECK)\s+)?'
        r'ADD\s+(?:CONSTRAINT\s+[\[`"]?[^\]`"\s,()]+[\]`"]?\s+)?'
        r'FOREIGN\s+KEY\s*\(([^)]+)\)\s*'
        r'REFERENCES\s+((?:[\[`"]?[^\]`"\s,()]+[\]`"]?\s*\.\s*)?[\[`"]?[^\]`"\s,()]+[\]`"]?)\s*\(([^)]+)\)',
        re.IGNORECASE
    )
    for m in alter_fk_re.finditer(content):
        src_table = _strip_identifier(m.group(1))
        src_cols = [_clean_column_token(c) for c in m.group(2).split(',')]
        ref_table = _strip_identifier(m.group(3))
        ref_cols = [_clean_column_token(c) for c in m.group(4).split(',')]

        if '.' not in src_table:
            src_table = f"dbo.{src_table}"
        if '.' not in ref_table:
            ref_table = f"dbo.{ref_table}"

        tk = src_table.lower()
        if tk in tables:
            src_t_short = tables[tk]["table_name"]
            ref_t_short = ref_table.split('.')[-1]
            for c_col, p_col in zip(src_cols, ref_cols):
                tables[tk]["fks"].append({
                    "child_table": src_t_short,
                    "child_column": c_col,
                    "parent_table": ref_t_short,
                    "parent_column": p_col,
                    "column": c_col,
                    "referenced_table": ref_t_short,
                    "referenced_column": p_col
                })

    # ── ALTER TABLE … PRIMARY KEY ────────────────────────────────────────────
    alter_pk_re = re.compile(
        r'ALTER\s+TABLE\s+((?:[\[`"]?[^\]`"\s,()]+[\]`"]?\s*\.\s*)?[\[`"]?[^\]`"\s,()]+[\]`"]?)'
        r'\s+ADD\s+(?:CONSTRAINT\s+[\[`"]?[^\]`"\s,()]+[\]`"]?\s+)?'
        r'PRIMARY\s+KEY(?:\s+CLUSTERED|\s+NONCLUSTERED)?\s*\(([^)]+)\)',
        re.IGNORECASE
    )
    for m in alter_pk_re.finditer(content):
        tname = _strip_identifier(m.group(1))
        if '.' not in tname:
            tname = f"dbo.{tname}"
        tk = tname.lower()
        if tk in tables:
            for c in m.group(2).split(','):
                tok = _clean_column_token(c)
                if tok:
                    tables[tk]["pks"].append(tok.lower())

    # ── Mark is_pk on each column ────────────────────────────────────────────
    for t in tables.values():
        pk_set = {p.lower() for p in t["pks"]}
        for col in t["columns"]:
            if col["name"].lower() in pk_set:
                col["is_pk"] = True

    # ── Calculate declared relationships across schema ───────────────────────
    declared_relationships = {}
    for t in tables.values():
        t_name = t["table_name"]
        declared_relationships[t_name.lower()] = set()

    for t in tables.values():
        t_name = t["table_name"]
        for fk in t["fks"]:
            parent = fk.get("parent_table") or fk.get("referenced_table")
            if parent:
                declared_relationships[t_name.lower()].add(parent)
                if parent.lower() in declared_relationships:
                    declared_relationships[parent.lower()].add(t_name)

    result = []
    for t in tables.values():
        col_casing = {c["name"].lower(): c["name"] for c in t["columns"]}
        pk_list = list(dict.fromkeys(col_casing.get(p.lower(), p) for p in t["pks"]))
        pk_display = ", ".join(pk_list) if pk_list else None
        id_cols = [c["name"] for c in t["columns"] if c.get("is_identity") or c.get("auto_increment")]
        rel_tables = sorted(list(declared_relationships.get(t["table_name"].lower(), set())))

        result.append({
            "schema": t["schema"],
            "table_name": t["table_name"],
            "columns": t["columns"],
            "primary_keys": pk_list,
            "primary_key": pk_display,
            "foreign_keys": t["fks"],
            "column_count": len(t["columns"]),
            "has_identity": len(id_cols) > 0,
            "identity_columns": id_cols,
            "fk_count": len(t["fks"]),
            "declared_fks": t["fks"],
            "related_tables": rel_tables,
        })

    if not result:
        raise Exception(
            "No database tables were detected in the uploaded SQL schema file. "
            "Please make sure the file contains CREATE TABLE statements."
        )

    return result
