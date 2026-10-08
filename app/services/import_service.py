import os
import shutil
import uuid
from datetime import datetime
from collections import defaultdict
from fastapi import UploadFile
from sqlalchemy.orm import Session
from app.config import settings
from app.models.domain import ImportHistory, StagingRecord
from app.schemas.api_schemas import UploadResponse, PreviewResponse, TableMapping, ExecuteResponse
from app.services.excel_reader import ExcelReader
from app.services.llm_service import LLMMappingService
from app.services.validation_engine import DeterministicValidationEngine
from app.services.sql_generator import DeterministicSQLGenerator
import traceback
import json
import pandas as pd


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
            status="PENDING",
        )
        db.add(history)
        db.commit()

        return UploadResponse(
            import_id=import_id,
            file_name=file.filename,
            sheets=[s["sheet_name"] for s in result["inspection"]["sheets"]],
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
            tables=metadata["tables"],
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Auto-match: LLM Semantic Mapping with Fallback
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def cache_schema(import_id: uuid.UUID, schema_info: list):
        cache_dir = os.path.join(settings.UPLOAD_DIR, str(import_id))
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "schema.json"), "w") as f:
            json.dump(schema_info, f)

    @staticmethod
    def get_cached_schema(import_id: uuid.UUID):
        cache_path = os.path.join(settings.UPLOAD_DIR, str(import_id), "schema.json")
        if os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                return json.load(f)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Auto-match: LLM Semantic Mapping with Table Selection & Conflict Detection
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def auto_match_schema(
        import_id: uuid.UUID,
        schema_info: list,
        db: Session,
        selected_tables: list = None,
    ) -> dict:
        preview = ImportService.get_preview(import_id, db)
        if not preview or not preview.tables:
            raise Exception("Preview not found or no tables extracted")

        ex_table = preview.tables[0]
        excel_headers = ex_table.headers
        sample_rows = ex_table.sample_data or []

        # Cache parsed schema for quick retrieval
        ImportService.cache_schema(import_id, schema_info)

        # Header to normalized key lookup
        header_to_key = {}
        for idx, h in enumerate(ex_table.headers):
            header_to_key[h] = ex_table.normalized_headers[idx]

        # Call LLM semantic mapping service against SELECTED TABLES ONLY in SQL-Column-First order
        llm_result = LLMMappingService.get_semantic_mapping(
            schema_info=schema_info,
            excel_columns=excel_headers,
            sample_rows=sample_rows,
            selected_tables=selected_tables,
        )

        return {
            "status": "SUCCESS",
            "source": llm_result.get("source", "STANDARD"),
            "table_mappings": llm_result.get("table_mappings", {}),
            "all_sql_columns": llm_result.get("all_sql_columns", []),
            "unused_excel_columns": llm_result.get("unused_excel_columns", []),
            "mappings": llm_result.get("mappings", []),
            "mapping_conflicts": llm_result.get("mapping_conflicts", []),
            "selected_tables": llm_result.get("selected_tables", []),
            "llm_usage": llm_result.get("llm_usage", {}),
            "statistics": llm_result.get("statistics", {}),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Schema-aware SQL Generation with Deterministic Validation
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def generate_sql_for_selected_tables(
        import_id: uuid.UUID,
        matched: list,
        schema_info: list,
        db: Session,
        include_identity: bool = False,
        selected_tables: list = None,
        table_plans: list = None,
    ) -> dict:
        preview = ImportService.get_preview(import_id, db)
        if not preview or not preview.tables:
            raise Exception("Preview not found or no tables extracted")

        source_table = preview.tables[0].table_name
        raw_data = ExcelReader.read_cached_table(import_id, source_table)

        # Filter schema to selected_tables if specified
        if selected_tables:
            sel_set = {s.lower() for s in selected_tables}
            schema_info = [t for t in schema_info if t["table_name"].lower() in sel_set]

        # Check for duplicate target conflicts before generating SQL
        target_check = defaultdict(list)
        for m in matched:
            t_key = (m["table"].lower(), m["column"].lower())
            target_check[t_key].append(m.get("excel_column", m.get("raw_data_key")))

        conflicts = []
        for (tbl, col), ex_cols in target_check.items():
            if len(ex_cols) > 1:
                conflicts.append(f"Multiple Excel columns ({', '.join(ex_cols)}) map to target [{tbl}].[{col}]")

        if conflicts:
            raise Exception(f"Cannot generate SQL with unresolved mapping conflicts: {'; '.join(conflicts)}")

        # Structure confirmed mappings
        table_mappings = defaultdict(dict)
        mapped_excel_cols = set()
        header_to_key = {}
        for idx, h in enumerate(preview.tables[0].headers):
            if idx < len(preview.tables[0].normalized_headers):
                header_to_key[h] = preview.tables[0].normalized_headers[idx]

        column_actions = {}
        if table_plans:
            for p in table_plans:
                t_k = str(p.get("table", "")).lower()
                c_k = str(p.get("sql_column", p.get("column", ""))).lower()
                if t_k and c_k:
                    raw_action = str(p.get("action", "")).upper()
                    if raw_action == "DEFAULT":
                        action = "DATABASE_DEFAULT"
                    elif raw_action == "IMPORT":
                        action = "EXCEL"
                    else:
                        action = raw_action

                    ex_col = p.get("excel_column") or p.get("excel_source")
                    raw_k = p.get("raw_data_key") or (header_to_key.get(ex_col, ex_col) if ex_col else None)
                    c_val = p.get("custom_value")

                    column_actions[(t_k, c_k)] = {
                        "table": p.get("table"),
                        "sql_column": p.get("sql_column", p.get("column")),
                        "action": action,
                        "excel_column": ex_col if action == "EXCEL" else None,
                        "raw_data_key": raw_k if action == "EXCEL" else None,
                        "custom_value": c_val if action in ("CUSTOM_VALUE", "CUSTOM") else None,
                    }
                    if action == "EXCEL" and ex_col and raw_k:
                        table_mappings[t_k][raw_k] = p.get("sql_column", p.get("column"))
                        mapped_excel_cols.add(ex_col)

        for m in matched:
            t_name = m["table"].lower()
            col_target = str(m.get("column", "")).lower()
            # User selection in table_plans MUST override automatic/previous matches
            if (t_name, col_target) in column_actions and column_actions[(t_name, col_target)]["action"] != "EXCEL":
                continue
            ex_col = m.get("excel_column")
            raw_key = m.get("raw_data_key") or header_to_key.get(ex_col, ex_col)
            table_mappings[t_name][raw_key] = m["column"]
            if ex_col:
                mapped_excel_cols.add(ex_col)

        unmapped = [h for h in preview.tables[0].headers if h not in mapped_excel_cols]

        # ── Step 3: Deterministic Validation Engine (Selected Tables Only) ───
        validation_result = DeterministicValidationEngine.validate_dataset(
            raw_rows=raw_data,
            schema_info=schema_info,
            table_mappings=table_mappings,
            column_actions=column_actions,
        )

        # ── Step 4: Deterministic SQL Generator (Selected Tables Only) ───────
        sql_script = DeterministicSQLGenerator.generate_sql(
            validated_result=validation_result,
            schema_info=schema_info,
            table_mappings=table_mappings,
            dialect=settings.SQL_DIALECT,
            use_transaction=True,
            unmapped_columns=unmapped,
            batch_size=settings.SQL_INSERT_BATCH_SIZE,
            column_actions=column_actions,
        )

        return {
            "sql_script": sql_script,
            "validation_report": {
                "valid_tables": validation_result["valid_tables"],
                "invalid_tables": validation_result["invalid_tables"],
                "overall_stats": validation_result["overall_stats"],
            },
        }

    # Backward compatibility alias
    generate_sql_from_matches = generate_sql_for_selected_tables

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
            StagingRecord.status == "ERROR",
        ).all()

        return {
            "import_id": import_id,
            "errors": [
                {
                    "row_index": e.row_index,
                    "sheet_name": e.sheet_name,
                    "raw_data": e.raw_data,
                    "errors": e.errors,
                }
                for e in errors
            ],
        }
