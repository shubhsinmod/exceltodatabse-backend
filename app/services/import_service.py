import os
import shutil
import uuid
from datetime import datetime
from fastapi import UploadFile
from sqlalchemy.orm import Session
from app.config import settings
from app.models.domain import ImportHistory, StagingRecord
from app.schemas.api_schemas import UploadResponse, PreviewResponse, TableMapping, ExecuteResponse
from app.services.excel_reader import ExcelReader
from app.utils.dynamic_table import DynamicTableManager
from app.database import engine
from sqlalchemy import insert
import traceback
import json
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Value formatting (generic, driven by schema data_type)
# ─────────────────────────────────────────────────────────────────────────────

def _is_empty(val) -> bool:
    """Return True if val is None, NaN, or empty string."""
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(val, str) and val.strip() == ""


def _format_sql_value(val, col_type: str) -> str:
    """
    Format a Python value as a safe SQL Server literal using the generic type
    extracted from the schema parser.  col_type is one of:
        'integer' | 'float' | 'date' | 'boolean' | 'string'

    Rules:
    - Empty / None / NaN  →  NULL  (never 'NULL' in quotes)
    - integer             →  123
    - float               →  123.45
    - boolean             →  1 or 0
    - date                →  'YYYY-MM-DD HH:MM:SS'  (SQL Server safe format)
    - string              →  'escaped text'
    - Single quotes in strings are doubled:  O'Reilly → 'O''Reilly'
    """
    if _is_empty(val):
        return "NULL"

    if col_type == "boolean":
        if isinstance(val, bool):
            return "1" if val else "0"
        sv = str(val).strip().lower()
        return "1" if sv in ("1", "true", "yes", "y") else "0"

    if col_type == "integer":
        try:
            return str(int(float(str(val).strip())))
        except (ValueError, TypeError):
            pass   # fall through to string

    if col_type == "float":
        try:
            return str(float(str(val).strip()))
        except (ValueError, TypeError):
            pass   # fall through to string

    if col_type == "date":
        try:
            dt = pd.to_datetime(val)
            return f"'{dt.strftime('%Y-%m-%d %H:%M:%S')}'"
        except Exception:
            pass   # fall through to string

    # String (default for 'string' type AND fallback for any type that failed above)
    escaped = str(val).replace("'", "''")
    return f"'{escaped}'"


def _normalize_col(name: str) -> str:
    """
    Normalise a column/header name for fuzzy matching:
    lowercase, remove spaces, underscores, hyphens.
    No hardcoded substitutions.
    """
    return name.lower().replace(" ", "").replace("_", "").replace("-", "")



# ─────────────────────────────────────────────────────────────────────────────
# Schema-aware SQL generation
# ─────────────────────────────────────────────────────────────────────────────

def _generate_schema_aware_inserts(
    raw_data: list,
    schema_table: dict,
    excel_to_db_map: dict,   # { raw_data_key: db_column_name }
) -> tuple:
    """
    Generate schema-aware INSERT statements for ONE DB table.

    Rules (purely schema-driven, zero hardcoding):
      - IDENTITY columns are ALWAYS excluded from INSERT.
      - For every non-IDENTITY DB column:
          * If mapped in excel_to_db_map:
              - Row has value -> use Excel value (type-safe formatted).
              - Row cell is empty:
                  - If nullable or has_default -> insert NULL.
                  - If NOT NULL and no DEFAULT -> row-level validation error (skip row).
          * If NOT in excel_to_db_map:
              - If has_default -> omit column completely from INSERT so SQL Server default applies.
              - If nullable (and no default) -> include in INSERT with NULL.
              - If NOT NULL and no default -> table-level validation error (skip entire table).

    Returns
    -------
    (sql_lines: list[str], table_errors: list[str], summary: dict)
    """
    db_schema   = schema_table.get("schema", "dbo")
    db_table    = schema_table["table_name"]
    db_cols_def = schema_table["columns"]   # ordered list of col dicts from parser

    # ── build lookup structures ───────────────────────────────────────────────
    # excel_mapped_lower : db_col_name_lower → excel raw_data_key
    excel_mapped_lower = {}
    for excel_key, db_col_name in excel_to_db_map.items():
        excel_mapped_lower[db_col_name.lower()] = excel_key

    # ── step 1: classify DB columns ──────────────────────────────────────────
    insert_cols        = []   # columns included in INSERT
    identity_excluded  = []   # columns skipped because IDENTITY
    default_omitted    = []   # columns omitted so SQL Server DEFAULT applies

    for col_def in db_cols_def:
        col_lower   = col_def["name"].lower()
        in_excel    = col_lower in excel_mapped_lower
        nullable    = col_def.get("nullable", True)
        has_default = col_def.get("has_default", False)
        is_identity = col_def.get("is_identity", False)

        if is_identity:
            identity_excluded.append(col_def["name"])
            continue

        if not in_excel and has_default:
            # Omit so DB default constraint applies automatically
            default_omitted.append(col_def["name"])
            continue

        insert_cols.append(col_def)

    if not insert_cols:
        return [], [], {
            "identity_excluded": identity_excluded,
            "default_omitted":   default_omitted,
        }

    # ── step 2: table-level validation ───────────────────────────────────────
    # Missing from Excel, NOT NULL, and has no DEFAULT -> table cannot be safely inserted
    table_level_missing = [
        col_def["name"]
        for col_def in insert_cols
        if col_def["name"].lower() not in excel_mapped_lower
        and not col_def.get("nullable", True)
        and not col_def.get("has_default", False)
    ]

    if table_level_missing:
        err = (
            f"Table [{db_schema}].[{db_table}] cannot be inserted.\n"
            f"Missing required database column(s):\n"
            + "\n".join(f"  - {c}" for c in table_level_missing)
            + f"\nReason:\nThese columns are NOT NULL and have no DEFAULT value."
        )
        return [], [err], {}

    # ── step 3: build summary sections ───────────────────────────────────────
    excel_mapped_cols = {}   # col_name → excel_key
    null_filled_cols  = {}   # col_name → "NULL"

    for col_def in insert_cols:
        col_lower = col_def["name"].lower()
        if col_lower in excel_mapped_lower:
            excel_mapped_cols[col_def["name"]] = excel_mapped_lower[col_lower]
        else:
            null_filled_cols[col_def["name"]] = "NULL"

    summary = {
        "excel_mapped":      excel_mapped_cols,
        "null_filled":       null_filled_cols,
        "identity_excluded": identity_excluded,
        "default_omitted":   default_omitted,
    }

    # ── step 4: header comment ────────────────────────────────────────────────
    sql_lines = [
        f"-- =========================================",
        f"-- Table            : [{db_schema}].[{db_table}]",
        f"-- From Excel       : {len(excel_mapped_cols)} column(s)",
        f"-- NULL-filled      : {len(null_filled_cols)} column(s)",
        f"-- IDENTITY skipped : {', '.join(identity_excluded) or 'none'}",
        f"-- DEFAULT omitted  : {', '.join(default_only_cols if 'default_only_cols' in locals() else default_omitted) or 'none'}",
        f"-- =========================================",
    ]

    # Pre-build column block
    col_names_parts = [f"    [{c['name']}]" for c in insert_cols]
    col_block = "(\n" + ",\n".join(col_names_parts) + "\n)"

    seen_rows: set = set()
    row_errors: list = []

    # ── step 5: generate per-row INSERT ──────────────────────────────────────
    for row_idx, row in enumerate(raw_data, start=1):
        values = []
        row_invalid = False
        row_error_cols = []

        for col_def in insert_cols:
            col_lower   = col_def["name"].lower()
            col_type    = col_def.get("data_type", "string")
            nullable    = col_def.get("nullable", True)
            has_default = col_def.get("has_default", False)

            excel_key = excel_mapped_lower.get(col_lower)

            if excel_key is not None:
                raw_val = row.get(excel_key)
                sql_val = _format_sql_value(raw_val, col_type)

                if sql_val == "NULL" and not nullable and not has_default:
                    row_invalid = True
                    row_error_cols.append(col_def["name"])
                else:
                    values.append(sql_val)
            else:
                values.append("NULL")

        if row_invalid:
            row_errors.append(
                f"  Row {row_idx}: cannot be inserted. Missing required value for NOT NULL column(s): "
                + ", ".join(row_error_cols)
            )
            continue

        dedup_key = tuple(values)
        if dedup_key in seen_rows:
            continue
        seen_rows.add(dedup_key)

        val_parts = [f"    {v}" for v in values]
        val_block = "(\n" + ",\n".join(val_parts) + "\n)"

        sql_lines.append(
            f"INSERT INTO [{db_schema}].[{db_table}]\n"
            f"{col_block}\n"
            f"VALUES\n"
            f"{val_block};"
        )

    if row_errors:
        sql_lines.append(f"-- Row-level validation errors for [{db_schema}].[{db_table}]:")
        for re_msg in row_errors:
            sql_lines.append(f"--{re_msg}")

    sql_lines.append("")
    return sql_lines, [], summary



# ─────────────────────────────────────────────────────────────────────────────
# ImportService
# ─────────────────────────────────────────────────────────────────────────────

class ImportService:

    @staticmethod
    async def process_upload(file: UploadFile, db: Session) -> UploadResponse:
        import_id = uuid.uuid4()

        file_ext = os.path.splitext(file.filename)[1]
        saved_filename = f"{import_id}{file_ext}"
        file_path = os.path.join(settings.UPLOAD_DIR, saved_filename)

        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        result = ExcelReader.process_and_cache_workbook(file_path, import_id)

        cache_dir = os.path.join(settings.UPLOAD_DIR, str(import_id))
        with open(os.path.join(cache_dir, "metadata.json"), "w") as f:
            json.dump(result, f)

        history = ImportHistory(
            id=import_id,
            file_name=file.filename,
            status="PENDING"
        )
        db.add(history)
        db.commit()

        return UploadResponse(
            import_id=import_id,
            file_name=file.filename,
            sheets=[s["sheet_name"] for s in result["inspection"]["sheets"]]
        )

    @staticmethod
    def get_preview(import_id: uuid.UUID, db: Session) -> PreviewResponse:
        history = db.query(ImportHistory).filter(ImportHistory.id == import_id).first()
        if not history:
            return None

        meta_path = os.path.join(settings.UPLOAD_DIR, str(import_id), "metadata.json")
        if not os.path.exists(meta_path):
            return None

        with open(meta_path, "r") as f:
            metadata = json.load(f)

        return PreviewResponse(
            import_id=import_id,
            file_name=history.file_name,
            inspection=metadata["inspection"],
            tables=metadata["tables"]
        )

    @staticmethod
    def execute_import(import_id: uuid.UUID, mappings: list, db: Session) -> ExecuteResponse:
        history = db.query(ImportHistory).filter(ImportHistory.id == import_id).first()
        if not history:
            return ExecuteResponse(import_id=import_id, status="FAILED", message="Import not found")

        history.status = "IMPORTING"
        history.started_at = datetime.utcnow()
        history.mapping_config = [m.model_dump() for m in mappings]
        db.commit()

        sql_statements = []

        try:
            for mapping in mappings:
                source_table = mapping.source_table_name
                target_table = mapping.target_table
                raw_data = ExcelReader.read_cached_table(import_id, source_table)
                sql_columns = [col_map.target_column for col_map in mapping.columns if col_map.include]
                if not sql_columns:
                    continue
                columns_str = ", ".join(sql_columns)
                for row in raw_data:
                    values = []
                    for col_map in mapping.columns:
                        if not col_map.include:
                            continue
                        val = row.get(col_map.excel_column)
                        if pd.isna(val) or val is None or val == "":
                            values.append("NULL")
                        elif isinstance(val, (int, float)):
                            values.append(str(val))
                        else:
                            escaped = str(val).replace("'", "''")
                            values.append(f"'{escaped}'")
                    values_str = ", ".join(values)
                    stmt = f"INSERT INTO {target_table} ({columns_str}) VALUES ({values_str});"
                    sql_statements.append(stmt)

            history.status = "COMPLETED"
            history.completed_at = datetime.utcnow()
            history.total_records = len(sql_statements)
            history.successful_records = len(sql_statements)
            history.failed_records = 0
            db.commit()

            return ExecuteResponse(
                import_id=import_id,
                status="SUCCESS",
                message="\n".join(sql_statements)
            )

        except Exception as e:
            traceback.print_exc()
            db.rollback()
            history.status = "FAILED"
            history.error_message = str(e)
            db.commit()
            return ExecuteResponse(import_id=import_id, status=history.status, message=str(e))

    # ─────────────────────────────────────────────────────────────────────────
    # Auto-match: match Excel columns to DB schema columns
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def auto_match_schema(import_id: uuid.UUID, schema_info: list, db: Session) -> dict:
        preview = ImportService.get_preview(import_id, db)
        if not preview or not preview.tables:
            raise Exception("Preview not found or no tables extracted")

        ex_table = preview.tables[0]

        # Flatten all DB columns
        db_columns = []
        for st in schema_info:
            t_name = st["table_name"]
            for col in st["columns"]:
                db_columns.append({
                    "schema":      st["schema"],
                    "table":       t_name,
                    "column":      col["name"],
                    "type":        col.get("data_type", "string"),
                    "is_pk":       col.get("is_pk", False),
                    "is_identity": col.get("is_identity", False),
                    "nullable":    col.get("nullable", True),
                })

        results = []

        for orig_idx, orig_header in enumerate(ex_table.headers):
            raw_data_key = ex_table.normalized_headers[orig_idx]
            norm_ex = _normalize_col(orig_header)

            candidates = []

            for db_col in db_columns:
                db_col_norm = _normalize_col(db_col["column"])
                t_norm      = _normalize_col(db_col["table"])

                score = 0

                # A. Exact normalized match
                if norm_ex == db_col_norm:
                    score = 80
                # B. table+column combo match
                elif norm_ex == t_norm + db_col_norm or t_norm + norm_ex == db_col_norm:
                    score = 90
                # C. Substring match (only meaningful tokens)
                elif len(norm_ex) > 3 and len(db_col_norm) > 3:
                    if db_col_norm in norm_ex or norm_ex in db_col_norm:
                        score = 40

                if score > 0:
                    if db_col["is_pk"] and score >= 80:
                        score = min(100, score + 10)

                    candidates.append({
                        "table":      db_col["table"],
                        "column":     db_col["column"],
                        "confidence": score,
                    })

            candidates.sort(key=lambda x: x["confidence"], reverse=True)
            best_match = candidates[0] if candidates and candidates[0]["confidence"] >= 70 else None

            results.append({
                "excel_column": orig_header,
                "raw_data_key": raw_data_key,
                "candidates":   candidates,
                "best_match":   best_match,
            })

        return {"mappings": results}

    # ─────────────────────────────────────────────────────────────────────────
    # Schema-aware SQL generation
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def generate_sql_from_matches(
        import_id:        uuid.UUID,
        matched:          list,
        schema_info:      list,
        db:               Session,
        include_identity: bool = False,
    ) -> str:
        """
        Generate schema-aware INSERT statements.

        `matched` is the user-confirmed list:
            [ { "excel_column": ..., "raw_data_key": ..., "table": ..., "column": ... } ]

        For every DB table that appears in `matched`:
        - Include ALL schema columns in the INSERT.
        - Use Excel data for matched columns.
        - Use NULL for nullable columns not in Excel.
        - Raise a validation error for NOT NULL / no-DEFAULT columns missing from Excel.
        - Exclude IDENTITY columns (unless include_identity=True).
        """
        preview = ImportService.get_preview(import_id, db)
        if not preview or not preview.tables:
            raise Exception("Preview not found or no tables extracted")

        source_table = preview.tables[0].table_name
        raw_data = ExcelReader.read_cached_table(import_id, source_table)

        # Group confirmed mappings by target table (case-insensitive)
        from collections import defaultdict
        table_to_excel_map = defaultdict(dict)  # table_lower → { raw_data_key: db_col_name }
        for m in matched:
            t_lower = m["table"].lower()
            table_to_excel_map[t_lower][m.get("raw_data_key", m["excel_column"])] = m["column"]

        # Topological sort (FK-aware ordering)
        table_names = [st["table_name"].lower() for st in schema_info]
        adj = defaultdict(list)
        in_degree = defaultdict(int)
        for t in table_names:
            in_degree[t] = 0
        for st in schema_info:
            t_lower = st["table_name"].lower()
            for fk in st.get("foreign_keys", []):
                ref_t = fk["referenced_table"].split(".")[-1].lower().strip("[]")
                if ref_t in table_names and ref_t != t_lower:
                    adj[ref_t].append(t_lower)
                    in_degree[t_lower] += 1

        queue = [t for t in table_names if in_degree[t] == 0]
        table_order = []
        while queue:
            curr = queue.pop(0)
            table_order.append(curr)
            for nxt in adj[curr]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)
        for t in table_names:
            if t not in table_order:
                table_order.append(t)

        # Schema lookup
        schema_by_lower = {st["table_name"].lower(): st for st in schema_info}

        all_sql = []
        all_errors = []

        for t_lower in table_order:
            if t_lower not in table_to_excel_map:
                continue  # no user mappings for this table → skip

            schema_table = schema_by_lower.get(t_lower)
            if not schema_table:
                continue

            excel_to_db_map = table_to_excel_map[t_lower]  # { raw_data_key: db_col_name }

            sql_lines, errors, summary = _generate_schema_aware_inserts(
                raw_data        = raw_data,
                schema_table    = schema_table,
                excel_to_db_map = excel_to_db_map,
            )

            if errors:
                all_errors.append((schema_table.get("schema", "dbo"), schema_table["table_name"], errors))
            if sql_lines:
                all_sql.append((schema_table["table_name"], sql_lines, summary))

        # ── assemble final output ────────────────────────────────────────────
        output_parts = []

        # Section A: Valid INSERT SQL
        if all_sql:
            output_parts.append("-- ===========================================")
            output_parts.append("-- VALID INSERTS")
            output_parts.append("-- ===========================================")
            output_parts.append("")
            for t_name, sql_lines, summary in all_sql:
                output_parts.extend(sql_lines)

        # Section B: Validation errors
        if all_errors:
            output_parts.append("-- ===========================================")
            output_parts.append("-- VALIDATION ERRORS (tables skipped)")
            output_parts.append("-- ===========================================")
            output_parts.append("")
            for db_sch, t_name, errs in all_errors:
                for e in errs:
                    for line in e.splitlines():
                        output_parts.append(f"-- {line}")
                output_parts.append("")

        # Section C: Mapping summary
        if all_sql:
            output_parts.append("-- ===========================================")
            output_parts.append("-- COLUMN MAPPING SUMMARY")
            output_parts.append("-- ===========================================")
            for t_name, _, summary in all_sql:
                output_parts.append(f"-- [{t_name}]")
                if summary.get("identity_excluded"):
                    output_parts.append("--   IDENTITY EXCLUDED:")
                    for ic in summary["identity_excluded"]:
                        output_parts.append(f"--     {ic} -> (auto-generated by SQL Server)")
                if summary.get("excel_mapped"):
                    output_parts.append("--   MAPPED FROM EXCEL:")
                    for db_col, ex_key in summary["excel_mapped"].items():
                        output_parts.append(f"--     {ex_key} -> {db_col}")
                if summary.get("null_filled"):
                    output_parts.append("--   NULL-FILLED (nullable, not in Excel):")
                    for db_col in summary["null_filled"]:
                        output_parts.append(f"--     {db_col} -> NULL")
                if summary.get("default_omitted"):
                    output_parts.append("--   DEFAULT OMITTED (applied by SQL Server):")
                    for db_col in summary["default_omitted"]:
                        output_parts.append(f"--     {db_col} -> DEFAULT")
                output_parts.append("")

        if not output_parts:
            raise Exception(
                "No SQL was generated. Check that your Excel columns are matched to a DB table."
            )

        return "\n".join(output_parts)

    @staticmethod
    def get_history(db: Session):
        return db.query(ImportHistory).order_by(ImportHistory.upload_date.desc()).all()

    @staticmethod
    def get_import_record(import_id: uuid.UUID, db: Session):
        return db.query(ImportHistory).filter(ImportHistory.id == import_id).first()

    @staticmethod
    def get_errors(import_id: uuid.UUID, db: Session):
        errors = db.query(StagingRecord).filter(
            StagingRecord.import_id == import_id,
            StagingRecord.status == "ERROR"
        ).all()

        return {
            "import_id": import_id,
            "errors": [
                {
                    "row_index":  e.row_index,
                    "sheet_name": e.sheet_name,
                    "raw_data":   e.raw_data,
                    "errors":     e.errors
                }
                for e in errors
            ]
        }
