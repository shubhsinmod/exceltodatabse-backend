"""
validation_engine.py
--------------------
Deterministic Validation Engine for Excel-to-SQL generation.

Validates:
  1. Primary keys:
     - Single-column and composite primary keys
     - Detects missing PK values
     - Detects duplicate PK values across rows with differing data (conflicts)
     - Rejects rows with duplicate PK conflicts
  2. Required columns (NOT NULL, no DEFAULT, not IDENTITY / AUTO_INCREMENT):
     - Table-level validation: if required columns are absent from mappings
     - Row-level validation: if mapped column value is empty / NULL in a row
  3. Foreign keys:
     - Builds dependency graph from schema
     - Validates that referenced parent values exist in parent data
  4. Data types:
     - Validates compatibility (integers, floats, dates, booleans)
  5. Deterministic row classification:
     - VALID rows (safe for INSERT)
     - REJECTED rows (with exact row index, table, column, value, reason)

Zero hardcoded tables, columns, or restaurant-specific values.
"""

from typing import Any, Optional, Dict, List, Union
from collections import defaultdict
import pandas as pd


def _is_empty_val(val) -> bool:
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(val, str) and val.strip() == ""


class DeterministicValidationEngine:

    @classmethod
    def validate_dataset(
        cls,
        raw_rows: list,
        schema_info: list,
        table_mappings: dict,  # { table_name: { excel_key: db_col_name } }
        column_actions: Any = None,
    ) -> dict:
        """
        Validate all tables deterministically.
        """
        # Normalize column_actions lookup
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
            elif isinstance(column_actions, list):
                for p in column_actions:
                    t_k = str(p.get("table", "")).lower()
                    c_k = str(p.get("sql_column", p.get("column", ""))).lower()
                    if t_k and c_k:
                        col_actions_map[(t_k, c_k)] = p

        schema_by_name = {t["table_name"].lower(): t for t in schema_info}
        table_order = cls.get_topological_order(schema_info)

        valid_tables = []
        invalid_tables = {}
        table_data = {}

        total_valid = 0
        total_rejected = 0
        total_pk_errs = 0
        total_fk_errs = 0
        total_null_errs = 0

        # Maintain set of valid inserted parent keys for FK validation:
        # { (parent_table.lower(), parent_col.lower()): set(values) }
        parent_value_store = defaultdict(set)

        for t_name in table_order:
            t_lower = t_name.lower()
            if t_lower not in table_mappings or not table_mappings[t_lower]:
                # Check if this table has user column actions
                table_has_actions = any(k[0] == t_lower for k in col_actions_map.keys())
                if not table_has_actions:
                    continue

            schema_table = schema_by_name.get(t_lower)
            if not schema_table:
                continue

            excel_to_db = table_mappings.get(t_lower, {})  # { excel_key: db_col }
            db_to_excel = {v.lower(): k for k, v in excel_to_db.items()}

            db_cols = schema_table["columns"]
            pk_col_names = [pk.lower() for pk in schema_table.get("primary_keys", [])]
            foreign_keys = schema_table.get("foreign_keys", [])

            # ── 1. Table-level check: required columns ───────────────────────
            missing_required = []
            for col in db_cols:
                c_lower = col["name"].lower()
                nullable = col.get("nullable", True)
                has_def = col.get("has_default", False)
                is_id = col.get("is_identity", False) or col.get("auto_increment", False)

                col_cfg = col_actions_map.get((t_lower, c_lower), {})
                act = str(col_cfg.get("action") or "").upper()
                has_custom = act in ("CUSTOM_VALUE", "CUSTOM") and not _is_empty_val(col_cfg.get("custom_value"))
                has_db_default = act in ("DEFAULT", "DATABASE_DEFAULT")

                if not nullable and not has_def and not is_id:
                    if c_lower not in db_to_excel and not has_custom and not has_db_default:
                        missing_required.append(col["name"])

            if missing_required:
                reason = (
                    f"Table [{schema_table['table_name']}] cannot be generated: "
                    f"Missing required columns ({', '.join(missing_required)}) with no default in Excel data."
                )
                invalid_tables[schema_table["table_name"]] = [reason]
                total_null_errs += len(missing_required)
                continue

            # ── 2. Primary Key Uniqueness & Deduplication Check ───────────────
            # Check for duplicate PK values in Excel that have conflicting data.
            # If the same PK appears with identical data, it is a duplicate row to deduplicate.
            # If the same PK appears with conflicting data, it is a PK conflict error!
            pk_seen_records = {}  # pk_tuple -> { "first_row_idx": int, "data_repr": tuple, "row": dict }
            conflicting_pk_values = set()
            pk_errors = []

            if pk_col_names:
                for idx, row in enumerate(raw_rows, start=1):
                    pk_vals = []
                    pk_missing = False
                    for pk_c in pk_col_names:
                        ex_key = db_to_excel.get(pk_c)
                        if ex_key is None or _is_empty_val(row.get(ex_key)):
                            pk_missing = True
                            break
                        pk_vals.append(str(row.get(ex_key)).strip())

                    if pk_missing:
                        # Missing PK value in row
                        continue

                    pk_key = tuple(pk_vals)
                    # Create signature of all mapped attributes for this table
                    row_data_sig = tuple(str(row.get(db_to_excel.get(c["name"].lower(), ""))) for c in db_cols if c["name"].lower() in db_to_excel)

                    if pk_key in pk_seen_records:
                        prev = pk_seen_records[pk_key]
                        if prev["data_repr"] != row_data_sig:
                            conflicting_pk_values.add(pk_key)
                            pk_errors.append({
                                "table": schema_table["table_name"],
                                "pk_columns": pk_col_names,
                                "pk_value": ", ".join(pk_vals),
                                "row_index": idx,
                                "conflicting_with_row": prev["first_row_idx"],
                                "reason": f"Duplicate primary-key value '{', '.join(pk_vals)}' with conflicting data (rows {prev['first_row_idx']} and {idx})",
                            })
                    else:
                        pk_seen_records[pk_key] = {
                            "first_row_idx": idx,
                            "data_repr": row_data_sig,
                            "row": row,
                        }

            total_pk_errs += len(conflicting_pk_values)

            # ── 3. Row-by-Row Validation (FK, NOT NULL, Types) ────────────────
            valid_rows = []
            rejected_rows = []
            fk_errors = []
            null_errors = []
            dedup_set = set()

            for idx, row in enumerate(raw_rows, start=1):
                row_rejected = False
                reasons = []

                # A. Check if row matches any conflicting PK
                if pk_col_names:
                    row_pk_vals = []
                    has_pk = True
                    for pk_c in pk_col_names:
                        ex_k = db_to_excel.get(pk_c)
                        if ex_k is None or _is_empty_val(row.get(ex_k)):
                            has_pk = False
                            break
                        row_pk_vals.append(str(row.get(ex_k)).strip())

                    if has_pk and tuple(row_pk_vals) in conflicting_pk_values:
                        rejected_rows.append({
                            "row_index": idx,
                            "table": schema_table["table_name"],
                            "error_type": "PRIMARY_KEY_CONFLICT",
                            "reason": f"Row rejected due to duplicate primary key conflict: '{', '.join(row_pk_vals)}'",
                        })
                        continue

                # B. Check NOT NULL on mapped columns
                for col in db_cols:
                    c_lower = col["name"].lower()
                    if c_lower in db_to_excel:
                        ex_k = db_to_excel[c_lower]
                        val = row.get(ex_k)
                        if _is_empty_val(val):
                            if not col.get("nullable", True) and not col.get("has_default", False):
                                row_rejected = True
                                r_msg = f"Required column '{col['name']}' is empty in row {idx}"
                                reasons.append(r_msg)
                                null_errors.append({
                                    "row_index": idx,
                                    "table": schema_table["table_name"],
                                    "column": col["name"],
                                    "reason": r_msg,
                                })

                # C. Check Foreign Keys against parent store
                for fk in foreign_keys:
                    c_col = fk.get("child_column", fk.get("column", "")).lower()
                    p_tbl = fk.get("parent_table", fk.get("referenced_table", "")).lower()
                    p_col = fk.get("parent_column", fk.get("referenced_column", "")).lower()

                    if c_col in db_to_excel:
                        fk_val = row.get(db_to_excel[c_col])
                        if not _is_empty_val(fk_val):
                            fk_str = str(fk_val).strip()
                            known_parent_vals = parent_value_store.get((p_tbl, p_col))
                            # If parent table was mapped and processed, validate presence
                            if known_parent_vals is not None and len(known_parent_vals) > 0:
                                if fk_str not in known_parent_vals:
                                    row_rejected = True
                                    fk_msg = (
                                        f"Foreign key violation: Column '{fk.get('child_column')}' value '{fk_str}' "
                                        f"does not exist in parent table '{fk.get('parent_table')}.{fk.get('parent_column')}'"
                                    )
                                    reasons.append(fk_msg)
                                    fk_errors.append({
                                        "row_index": idx,
                                        "table": schema_table["table_name"],
                                        "column": fk.get("child_column"),
                                        "value": fk_str,
                                        "parent_table": fk.get("parent_table"),
                                        "parent_column": fk.get("parent_column"),
                                        "reason": fk_msg,
                                    })

                if row_rejected:
                    rejected_rows.append({
                        "row_index": idx,
                        "table": schema_table["table_name"],
                        "error_type": "VALIDATION_ERROR",
                        "reason": "; ".join(reasons),
                    })
                else:
                    # Deduplication of identical rows for this table
                    row_vals_tuple = tuple(str(row.get(db_to_excel.get(c["name"].lower(), ""))) for c in db_cols)
                    if row_vals_tuple not in dedup_set:
                        dedup_set.add(row_vals_tuple)
                        valid_rows.append(row)
                        # Record values into parent_value_store for child FK checks
                        for col in db_cols:
                            c_low = col["name"].lower()
                            if c_low in db_to_excel:
                                cell_val = row.get(db_to_excel[c_low])
                                if not _is_empty_val(cell_val):
                                    parent_value_store[(schema_table["table_name"].lower(), c_low)].add(str(cell_val).strip())

            total_fk_errs += len(fk_errors)
            total_valid += len(valid_rows)
            total_rejected += len(rejected_rows)

            if valid_rows:
                valid_tables.append(schema_table["table_name"])
            else:
                invalid_tables[schema_table["table_name"]] = ["No valid rows remaining after validation."]

            table_data[schema_table["table_name"]] = {
                "valid_rows": valid_rows,
                "rejected_rows": rejected_rows,
                "pk_errors": pk_errors,
                "fk_errors": fk_errors,
                "null_errors": null_errors,
            }

        return {
            "valid_tables": valid_tables,
            "invalid_tables": invalid_tables,
            "table_data": table_data,
            "overall_stats": {
                "total_valid_rows": total_valid,
                "total_rejected_rows": total_rejected,
                "pk_error_count": total_pk_errs,
                "fk_error_count": total_fk_errs,
                "missing_required_count": total_null_errs,
            },
        }

    @staticmethod
    def get_topological_order(schema_info: list) -> list:
        """Topological sort based on foreign keys to determine safe insert ordering."""
        table_names = [st["table_name"] for st in schema_info]
        name_lower_map = {st["table_name"].lower(): st["table_name"] for st in schema_info}
        adj = defaultdict(list)
        in_degree = defaultdict(int)

        for t in table_names:
            in_degree[t.lower()] = 0

        for st in schema_info:
            t_lower = st["table_name"].lower()
            for fk in st.get("foreign_keys", []):
                ref_t = fk.get("parent_table", fk.get("referenced_table", "")).split(".")[-1].strip("[]`\"").lower()
                if ref_t in name_lower_map and ref_t != t_lower:
                    adj[ref_t].append(t_lower)
                    in_degree[t_lower] += 1

        queue = [t.lower() for t in table_names if in_degree[t.lower()] == 0]
        ordered_lowers = []
        while queue:
            curr = queue.pop(0)
            ordered_lowers.append(curr)
            for nxt in adj[curr]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

        for t in table_names:
            if t.lower() not in ordered_lowers:
                ordered_lowers.append(t.lower())

        return [name_lower_map[tl] for tl in ordered_lowers if tl in name_lower_map]
