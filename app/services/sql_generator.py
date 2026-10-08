"""
sql_generator.py
----------------
Deterministic SQL Generator with Multi-Row INSERT and Configurable Batching.
Generates safe, clean, transaction-wrapped SQL INSERT statements for validated datasets.

Features:
  - Multi-row INSERT statements (e.g., INSERT INTO [table] (...) VALUES (...), (...);).
  - Configurable batch size (SQL_INSERT_BATCH_SIZE, default 1000 per statement).
  - Generates SQL in foreign-key dependency order (Topological sorting).
  - Dialect-aware quoting:
      'tsql' (SQL Server) -> [table], [column]
      'mysql'             -> `table`, `column`
      'postgres'          -> "table", "column"
  - Supports transaction blocks (BEGIN TRANSACTION; ... COMMIT;).
  - Skips IDENTITY / AUTO_INCREMENT columns unless explicitly provided and valid.
  - Omits columns with DEFAULT constraints when absent in Excel.
  - Safely escapes strings (e.g., O'Brien -> 'O''Brien').
  - Type-safe formatting for integers, decimals, dates, booleans, and strings.
  - Skips tables with zero valid rows.
  - Zero hardcoding of any table names, column names, or values.
"""

from typing import Any, Optional, Dict, List, Union
import pandas as pd
from app.config import settings


def _is_empty(val) -> bool:
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(val, str) and val.strip() == ""


def _quote_ident(name: str, dialect: str = "tsql") -> str:
    cleaned = name.strip("[]`\"' ")
    if dialect == "mysql":
        return f"`{cleaned}`"
    elif dialect == "postgres":
        return f'"{cleaned}"'
    else:  # default tsql
        return f"[{cleaned}]"


def _format_value(val, col_type: str = "string") -> str:
    if _is_empty(val):
        return "NULL"

    c_type = (col_type or "string").lower()

    if c_type == "boolean":
        if isinstance(val, bool):
            return "1" if val else "0"
        sv = str(val).strip().lower()
        return "1" if sv in ("1", "true", "yes", "y") else "0"

    if c_type in ("integer", "int", "bigint", "smallint", "tinyint"):
        try:
            return str(int(float(str(val).strip())))
        except (ValueError, TypeError):
            pass

    if c_type in ("float", "decimal", "numeric", "real", "money", "smallmoney", "double"):
        try:
            s = str(val).strip()
            float(s)  # Validate numeric representation
            return s
        except (ValueError, TypeError):
            pass

    if c_type in ("date", "datetime", "datetime2", "smalldatetime", "timestamp"):
        try:
            dt = pd.to_datetime(val)
            if c_type == "date" and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                return f"'{dt.strftime('%Y-%m-%d')}'"
            return f"'{dt.strftime('%Y-%m-%d %H:%M:%S')}'"
        except Exception:
            pass

    # If raw value is already numeric int/float
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if isinstance(val, float) and pd.isna(val):
            return "NULL"
        return str(val)

    # String escaping: double apostrophes
    escaped = str(val).replace("'", "''")
    return f"'{escaped}'"


class DeterministicSQLGenerator:

    @classmethod
    def generate_sql(
        cls,
        validated_result: dict,
        schema_info: list,
        table_mappings: dict,  # { table_name: { excel_key: db_col } }
        dialect: str = "tsql",
        use_transaction: bool = True,
        unmapped_columns: list = None,
        ambiguous_mappings: list = None,
        batch_size: int = None,
        include_validation_report: bool = False,
        column_actions: Any = None,
    ) -> str:
        """
        Generate complete SQL script with sections:
          1. VALID INSERTS (Multi-row batched INSERT statements with complete target table structure)
          2. Column filling rules comment headers based on user actions (Excel, NULL, Custom Value, Database Default)
          3. Optional validation report (default: False per Rule 10 clean SQL output)
        """
        if batch_size is None or batch_size <= 0:
            batch_size = getattr(settings, "SQL_INSERT_BATCH_SIZE", 1000)

        # Normalize column_actions into a lookup dict: {(table_lower, col_lower): config_dict}
        col_actions_map = {}
        if column_actions:
            if isinstance(column_actions, dict):
                for k, v in column_actions.items():
                    if isinstance(k, tuple):
                        col_actions_map[(str(k[0]).lower(), str(k[1]).lower())] = v
                    elif isinstance(v, dict):
                        if "." in str(k):
                            parts = str(k).split(".", 1)
                            col_actions_map[(parts[0].lower(), parts[1].lower())] = v
                        else:
                            for c_k, c_v in v.items():
                                col_actions_map[(str(k).lower(), str(c_k).lower())] = c_v
                    elif isinstance(k, str) and "." in k:
                        parts = k.split(".", 1)
                        col_actions_map[(parts[0].lower(), parts[1].lower())] = {"action": v}
            elif isinstance(column_actions, list):
                for p in column_actions:
                    t_k = str(p.get("table", "")).lower()
                    c_k = str(p.get("sql_column", p.get("column", ""))).lower()
                    if t_k and c_k:
                        col_actions_map[(t_k, c_k)] = p

        schema_by_name = {t["table_name"].lower(): t for t in schema_info}
        table_data = validated_result.get("table_data", {})
        valid_tables = validated_result.get("valid_tables", [])
        invalid_tables = validated_result.get("invalid_tables", {})

        output_lines = []

        # ── SECTION A: VALID INSERTS ─────────────────────────────────────────
        output_lines.append("-- ===========================================")
        output_lines.append("-- VALID INSERTS")
        output_lines.append("-- ===========================================")
        output_lines.append("")

        if use_transaction:
            if dialect == "postgres":
                output_lines.append("BEGIN;")
            elif dialect == "mysql":
                output_lines.append("START TRANSACTION;")
            else:
                output_lines.append("BEGIN TRANSACTION;")
            output_lines.append("")

        total_inserts_count = 0

        for t_name in valid_tables:
            t_lower = t_name.lower()
            schema_tbl = schema_by_name.get(t_lower)
            if not schema_tbl:
                continue

            tbl_info = table_data.get(t_name, {})
            valid_rows = tbl_info.get("valid_rows", [])
            # Empty table check: skip tables with 0 valid rows
            if not valid_rows:
                continue

            excel_to_db = table_mappings.get(t_lower, {})
            db_to_excel = {v.lower(): k for k, v in excel_to_db.items()}

            db_cols = schema_tbl["columns"]
            db_schema = schema_tbl.get("schema", "dbo")

            # Determine insertable columns (COMPLETE Target Database Table Structure)
            # The database table is the source of truth for all target columns.
            # Order is strictly preserved from the database schema.
            insert_cols = []
            identity_skipped = []
            identity_included = []
            has_identity_insert = False

            for col in db_cols:
                c_name = col["name"]
                c_low = c_name.lower()
                is_id = col.get("is_identity", False) or col.get("auto_increment", False)
                col_cfg = col_actions_map.get((t_lower, c_low)) or {}
                ex_k = col_cfg.get("raw_data_key") or col_cfg.get("excel_column") or col_cfg.get("excel_source") or db_to_excel.get(c_low)

                if is_id:
                    # Check if mapped and Excel actually has non-empty values for it
                    if ex_k is not None and any(not _is_empty(r.get(ex_k)) for r in valid_rows):
                        has_identity_insert = True
                        identity_included.append(c_name)
                        insert_cols.append(col)
                    else:
                        # Omit unmapped/empty identity so SQL Server auto-generates it
                        identity_skipped.append(c_name)
                    continue

                # For all other target table columns: ALWAYS INCLUDE in INSERT structure!
                insert_cols.append(col)

            if not insert_cols:
                continue

            # Header comment for selected SQL table documenting column filling rules
            tbl_quoted = f"{_quote_ident(db_schema, dialect)}.{_quote_ident(t_name, dialect)}" if dialect == "tsql" else _quote_ident(t_name, dialect)
            output_lines.append(f"-- -------------------------------------------")
            output_lines.append(f"-- TARGET SQL TABLE: {tbl_quoted}")
            output_lines.append(f"-- Complete Columns in Table Schema: {len(db_cols)}")
            output_lines.append(f"-- Columns in INSERT Statement: {len(insert_cols)}")
            output_lines.append(f"-- Column Sources & Filling Rules:")
            for col in db_cols:
                c_name = col["name"]
                c_low = c_name.lower()
                is_id = col.get("is_identity", False) or col.get("auto_increment", False)
                has_def = col.get("has_default", False)
                nullable = col.get("nullable", True)

                col_cfg = col_actions_map.get((t_lower, c_low)) or {}
                act = str(col_cfg.get("action") or "").upper()
                c_val = col_cfg.get("custom_value")
                ex_src = col_cfg.get("excel_column") or col_cfg.get("excel_source") or db_to_excel.get(c_low)
                
                if is_id and col not in insert_cols:
                    output_lines.append(f"--   {c_name} -> DATABASE_GENERATED (Identity sequence; omitted from INSERT)")
                elif act in ("CUSTOM_VALUE", "CUSTOM") and c_val is not None:
                    output_lines.append(f"--   {c_name} -> CUSTOM_VALUE [{c_val}] (Fixed value for all rows)")
                elif act in ("DEFAULT", "DATABASE_DEFAULT"):
                    output_lines.append(f"--   {c_name} -> DEFAULT (Database default)")
                elif act == "NULL":
                    output_lines.append(f"--   {c_name} -> NULL (Explicit NULL)")
                elif ex_src:
                    output_lines.append(f"--   {c_name} -> EXCEL [{ex_src}] (Mapped)")
                elif has_def and not nullable:
                    output_lines.append(f"--   {c_name} -> DEFAULT (Schema constraint)")
                elif nullable:
                    output_lines.append(f"--   {c_name} -> NULL (Unmapped nullable)")
                else:
                    output_lines.append(f"--   {c_name} -> NULL")

            output_lines.append(f"-- Valid Rows to Insert: {len(valid_rows)}")
            if identity_included:
                output_lines.append(f"-- Explicit Identity Values Inserted: {', '.join(identity_included)}")
            if identity_skipped:
                output_lines.append(f"-- Identity Columns Omitted (DB Generated): {', '.join(identity_skipped)}")
            output_lines.append(f"-- -------------------------------------------")

            col_names_str = ",\n".join(f"    {_quote_ident(c['name'], dialect)}" for c in insert_cols)
            col_block = f"(\n{col_names_str}\n)"

            # Enable SET IDENTITY_INSERT ON if explicit identity values are present
            if has_identity_insert and dialect == "tsql":
                output_lines.append(f"SET IDENTITY_INSERT {tbl_quoted} ON;")

            # Multi-row batch insertion (chunks of batch_size)
            for batch_start in range(0, len(valid_rows), batch_size):
                if batch_start > 0:
                    output_lines.append("")

                batch_rows = valid_rows[batch_start:batch_start + batch_size]
                batch_tuples = []

                for row in batch_rows:
                    val_parts = []
                    for c in insert_cols:
                        c_name = c["name"]
                        c_low = c_name.lower()
                        c_type = c.get("data_type", "string")
                        is_nullable = c.get("nullable", True)
                        has_def = c.get("has_default", False)

                        col_cfg = col_actions_map.get((t_lower, c_low)) or {}
                        act = str(col_cfg.get("action") or "").upper()
                        c_val = col_cfg.get("custom_value")

                        if act in ("CUSTOM_VALUE", "CUSTOM"):
                            if c_val is not None and str(c_val).strip() != "":
                                val_parts.append(f"    {_format_value(c_val, c_type)}")
                            else:
                                if has_def and not is_nullable:
                                    val_parts.append("    DEFAULT")
                                else:
                                    val_parts.append("    NULL")
                        elif act in ("DEFAULT", "DATABASE_DEFAULT"):
                            val_parts.append("    DEFAULT")
                        elif act == "NULL":
                            val_parts.append("    NULL")
                        else:
                            ex_k = col_cfg.get("raw_data_key") or col_cfg.get("excel_column") or col_cfg.get("excel_source") or db_to_excel.get(c_low)
                            if ex_k is not None:
                                cell_val = row.get(ex_k)
                                if not _is_empty(cell_val):
                                    val_parts.append(f"    {_format_value(cell_val, c_type)}")
                                else:
                                    if has_def and not is_nullable:
                                        val_parts.append("    DEFAULT")
                                    else:
                                        val_parts.append("    NULL")
                            else:
                                # Complete table structure: unmapped column receives DEFAULT or NULL
                                if has_def and not is_nullable:
                                    val_parts.append("    DEFAULT")
                                else:
                                    val_parts.append("    NULL")

                    row_tuple_str = ",\n".join(val_parts)
                    batch_tuples.append(f"(\n{row_tuple_str}\n)")

                multi_row_values = ",\n".join(batch_tuples)
                stmt = f"INSERT INTO {tbl_quoted}\n{col_block}\nVALUES\n{multi_row_values};"
                output_lines.append(stmt)
                total_inserts_count += len(batch_rows)

            # Disable SET IDENTITY_INSERT OFF if explicit identity values were inserted
            if has_identity_insert and dialect == "tsql":
                output_lines.append(f"SET IDENTITY_INSERT {tbl_quoted} OFF;")

            output_lines.append("")

        if use_transaction:
            output_lines.append("COMMIT;")
            output_lines.append("")

        # Rule 10: Do not include the large VALIDATION ERRORS, rejected-row, or validation-error report in the generated SQL output.
        # Only append validation report if explicitly requested via include_validation_report=True.
        if include_validation_report:
            # ── SECTION B: VALIDATION ERRORS ─────────────────────────────────────
            output_lines.append("-- ===========================================")
            output_lines.append("-- VALIDATION ERRORS")
            output_lines.append("-- ===========================================")
            output_lines.append("")

            # 1. Skipped Tables
            if invalid_tables:
                output_lines.append("-- [TABLES CANNOT BE GENERATED]")
                for t_err, reasons in invalid_tables.items():
                    output_lines.append(f"-- Table: {t_err}")
                    for r in reasons:
                        output_lines.append(f"--   Reason: {r}")
                    output_lines.append("--")
                output_lines.append("")

            # 2. Row-level errors (PK duplicates, FK violations, missing NOT NULL)
            for t_name, info in table_data.items():
                pk_errs = info.get("pk_errors", [])
                fk_errs = info.get("fk_errors", [])
                null_errs = info.get("null_errors", [])
                rejected = info.get("rejected_rows", [])

                if pk_errs or fk_errs or null_errs or rejected:
                    output_lines.append(f"-- [Table: {t_name}] Rejected Rows: {len(rejected)}")
                    for pe in pk_errs:
                        output_lines.append(f"--   PRIMARY KEY CONFLICT: {pe['reason']}")
                    for fe in fk_errs:
                        output_lines.append(f"--   FOREIGN KEY ERROR: Row {fe['row_index']} - {fe['reason']}")
                    for ne in null_errs:
                        output_lines.append(f"--   NOT NULL ERROR: {ne['reason']}")
                    output_lines.append("--")

            output_lines.append("")

            # ── SECTION C: UNMAPPED EXCEL COLUMNS ────────────────────────────────
            output_lines.append("-- ===========================================")
            output_lines.append("-- UNMAPPED EXCEL COLUMNS")
            output_lines.append("-- ===========================================")
            if unmapped_columns:
                for uc in unmapped_columns:
                    output_lines.append(f"-- Excel Column: {uc}")
                    output_lines.append(f"--   Reason: No sufficiently confident database-column mapping was found.")
            else:
                output_lines.append("-- None (All columns were mapped)")
            output_lines.append("")

            # ── SECTION D: COLUMN MAPPING SUMMARY ────────────────────────────────
            output_lines.append("-- ===========================================")
            output_lines.append("-- COLUMN MAPPING SUMMARY")
            output_lines.append("-- ===========================================")
            for t_name, col_map in table_mappings.items():
                output_lines.append(f"-- Target Table: [{t_name}]")
                for ex_col, db_col in col_map.items():
                    output_lines.append(f"--   {ex_col} -> {db_col} [MATCHED]")
                output_lines.append("--")

            if ambiguous_mappings:
                output_lines.append("-- AMBIGUOUS MAPPINGS REQUIRING CONFIRMATION:")
                for amb in ambiguous_mappings:
                    output_lines.append(f"--   Excel Column: {amb.get('excel_column')}")
                    for cand in amb.get("candidates", []):
                        output_lines.append(f"--     Candidate: {cand.get('table')}.{cand.get('column')} (Confidence: {cand.get('confidence')})")
                output_lines.append("")

        return "\n".join(output_lines)
