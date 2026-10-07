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

class ImportService:
    
    @staticmethod
    async def process_upload(file: UploadFile, db: Session) -> UploadResponse:
        import_id = uuid.uuid4()
        
        file_ext = os.path.splitext(file.filename)[1]
        saved_filename = f"{import_id}{file_ext}"
        file_path = os.path.join(settings.UPLOAD_DIR, saved_filename)
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        # Parse Excel, detect sheets, tables, clean and cache data
        result = ExcelReader.process_and_cache_workbook(file_path, import_id)
        
        # Save metadata to disk
        cache_dir = os.path.join(settings.UPLOAD_DIR, str(import_id))
        with open(os.path.join(cache_dir, "metadata.json"), "w") as f:
            json.dump(result, f)
        
        # Create history record
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
    def execute_import(import_id: uuid.UUID, mappings: list[TableMapping], db: Session) -> ExecuteResponse:
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
                
                # Load cached data
                raw_data = ExcelReader.read_cached_table(import_id, source_table)
                
                sql_columns = [col_map.target_column for col_map in mapping.columns if col_map.include]
                if not sql_columns:
                    continue
                    
                columns_str = ", ".join(sql_columns)
                
                for row in raw_data:
                    values = []
                    for col_map in mapping.columns:
                        if not col_map.include: continue
                        val = row.get(col_map.excel_column)
                        
                        import pandas as pd
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
            
            # Return the SQL string in the message field
            return ExecuteResponse(
                import_id=import_id, 
                status="SUCCESS", 
                message="\n".join(sql_statements)
            )
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            db.rollback()
            history.status = "FAILED"
            history.error_message = str(e)
            db.commit()
            return ExecuteResponse(import_id=import_id, status=history.status, message=str(e))




    @staticmethod
    def auto_match_schema(import_id: uuid.UUID, schema_info: list, db: Session) -> dict:
        preview = ImportService.get_preview(import_id, db)
        if not preview or not preview.tables:
            raise Exception("Preview not found or no tables extracted")
            
        ex_table = preview.tables[0] # Assuming single flat file table
        
        from collections import defaultdict
        db_columns = defaultdict(list)
        
        for st in schema_info:
            t_name = st["table_name"]
            for col in st["columns"]:
                c_name = col["name"].strip("`\"\'")
                norm_c = c_name.lower().replace("_", "").replace("-", "")
                db_columns[norm_c].append({"table": t_name, "column": c_name})
                
        aliases = {
            "qty": "quantity",
            "payment": "paymentmethod",
            "id": "id"
        }
        
        matched = []
        unmatched = []
        ambiguous = []
        
        for orig_idx, orig_header in enumerate(ex_table.headers):
            raw_data_key = ex_table.normalized_headers[orig_idx]
            norm_ex = orig_header.lower().replace(" ", "").replace("_", "").replace("-", "")
            if norm_ex in aliases:
                norm_ex = aliases[norm_ex]
                
            matches = db_columns.get(norm_ex, [])
            if not matches:
                for db_norm, db_list in db_columns.items():
                    for db_item in db_list:
                        t_norm = db_item["table"].lower().replace("_", "")
                        if norm_ex == t_norm + db_norm or norm_ex == db_norm:
                             matches.append(db_item)
            
            unique_matches = []
            seen = set()
            for m in matches:
                identifier = f"{m['table']}.{m['column']}"
                if identifier not in seen:
                    seen.add(identifier)
                    unique_matches.append(m)
            
            if unique_matches:
                # Map to ALL matched tables! No ambiguity!
                for um in unique_matches:
                    matched.append({
                        "excel_column": orig_header,
                        "raw_data_key": raw_data_key,
                        "table": um["table"],
                        "column": um["column"]
                    })
            else:
                unmatched.append({
                    "excel_column": orig_header
                })
                
        return {
            "matched": matched,
            "unmatched": unmatched,
            "ambiguous": ambiguous
        }
        
    @staticmethod
    def generate_sql_from_matches(import_id: uuid.UUID, matched: list, schema_info: list, db: Session) -> str:
        preview = ImportService.get_preview(import_id, db)
        source_table = preview.tables[0].table_name
        raw_data = ExcelReader.read_cached_table(import_id, source_table)
        
        from collections import defaultdict
        tables_to_insert = defaultdict(list)
        for m in matched:
            tables_to_insert[m["table"]].append((m.get("raw_data_key", m["excel_column"]), m["column"]))
            
        sql_statements = []
        table_order = [t["table_name"] for t in schema_info]
        
        for st in schema_info:
            t_name = st["table_name"]
            if t_name not in tables_to_insert:
                continue
                
            col_mappings = tables_to_insert[t_name]
            sql_columns = [m[1] for m in col_mappings]
            
            schema_cols = {c["name"].lower(): c for c in st["columns"]}
            pk_cols = [c["name"].lower() for c in st["columns"] if c.get("is_pk")]
            
            # Validation: Are non-auto-increment PKs missing from the mapping entirely?
            for pk in pk_cols:
                if not schema_cols[pk].get("is_auto"):
                    pk_mapped = any(target_col.lower() == pk for ex_col, target_col in col_mappings)
                    if not pk_mapped:
                        if t_name == "orders" or t_name == "order_item":
                            raise Exception("Order ID is required to populate the orders/order_item foreign-key relationship, but the source Excel does not contain a reliable Order ID.")
                        else:
                            raise Exception(f"Primary key '{pk}' is required for table '{t_name}' but is not mapped.")
                            
            sql_statements.append(f"-- =========================================")
            sql_statements.append(f"-- Inserts for table: {t_name}")
            sql_statements.append(f"-- =========================================")
            
            seen_pks = set()
            import pandas as pd
            
            for row in raw_data:
                pk_vals = []
                missing_pk = False
                
                for pk in pk_cols:
                    pk_val = None
                    for ex_col, target_col in col_mappings:
                        if target_col.lower() == pk:
                            pk_val = row.get(ex_col)
                            break
                    if pk_val is None or pd.isna(pk_val) or pk_val == "":
                        if not schema_cols[pk].get("is_auto"):
                            missing_pk = True
                    else:
                        pk_vals.append(str(pk_val))
                
                if missing_pk:
                    continue
                    
                row_values_for_dedup = []
                values = []
                insert_cols = []
                
                for ex_col, target_col in col_mappings:
                    sc = schema_cols.get(target_col.lower(), {})
                    val = row.get(ex_col)
                    
                    if sc.get("is_auto"):
                        continue
                        
                    insert_cols.append(target_col)
                    
                    if pd.isna(val) or val is None or val == "":
                        values.append("NULL")
                        row_values_for_dedup.append("NULL")
                    elif isinstance(val, (int, float)):
                        values.append(str(val))
                        row_values_for_dedup.append(str(val))
                    else:
                        escaped = str(val).replace("'", "''")
                        values.append(f"'{escaped}'")
                        row_values_for_dedup.append(f"'{escaped}'")
                        
                dedup_key = tuple(pk_vals) if pk_vals else tuple(row_values_for_dedup)
                
                if dedup_key in seen_pks:
                    continue
                    
                seen_pks.add(dedup_key)
                
                columns_str = ", ".join(insert_cols)
                values_str = ", ".join(values)
                stmt = f"INSERT INTO {t_name} ({columns_str}) VALUES ({values_str});"
                sql_statements.append(stmt)
                
            sql_statements.append("")
            
        return "\n".join(sql_statements)

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
                    "row_index": e.row_index,
                    "sheet_name": e.sheet_name,
                    "raw_data": e.raw_data,
                    "errors": e.errors
                } for e in errors
            ]
        }
