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
        
        dt_manager = DynamicTableManager(engine)
        
        total_records = 0
        success_count = 0
        failed_count = 0
        
        try:
            for mapping in mappings:
                source_table = mapping.source_table_name
                target_table = mapping.target_table
                
                # Create target table
                table = dt_manager.create_table_if_not_exists(target_table, mapping.columns)
                
                # Load cached data
                raw_data = ExcelReader.read_cached_table(import_id, source_table)
                total_records += len(raw_data)
                
                # Batch size for bulk ops
                batch_size = 1000
                current_batch = []
                staging_batch = []
                
                for i, row in enumerate(raw_data):
                    # For staging, we should record the original sheet but we use source_table as sheet_name logic
                    staging_record = StagingRecord(
                        import_id=import_id,
                        sheet_name=source_table,
                        target_table=target_table,
                        row_index=i,
                        raw_data=row
                    )
                    
                    mapped_row = {}
                    is_valid = True
                    
                    for col_map in mapping.columns:
                        excel_val = row.get(col_map.excel_column)
                        mapped_row[col_map.target_column] = excel_val
                        
                    staging_record.mapped_data = mapped_row
                    staging_record.status = "VALID"
                    staging_record.is_valid = True
                    
                    staging_batch.append(staging_record)
                    current_batch.append(mapped_row)
                    
                    if len(current_batch) >= batch_size or i == len(raw_data) - 1:
                        # Save staging
                        db.bulk_save_objects(staging_batch)
                        db.commit()
                        
                        # Try bulk insert
                        try:
                            stmt = insert(table).values(current_batch)
                            with db.begin_nested():
                                db.execute(stmt)
                            success_count += len(current_batch)
                            
                            # Update staging status
                            db.query(StagingRecord).filter(
                                StagingRecord.import_id == import_id,
                                StagingRecord.sheet_name == source_table,
                                StagingRecord.row_index >= i - len(current_batch) + 1,
                                StagingRecord.row_index <= i
                            ).update({"status": "IMPORTED"})
                            db.commit()
                            
                        except Exception as e_bulk:
                            db.rollback()
                            # Fallback to row-by-row insert for this batch to isolate failures
                            for idx, m_row in enumerate(current_batch):
                                s_record = staging_batch[idx]
                                try:
                                    with db.begin_nested():
                                        stmt = insert(table).values(m_row)
                                        db.execute(stmt)
                                    s_record.status = "IMPORTED"
                                    success_count += 1
                                except Exception as e_row:
                                    s_record.status = "ERROR"
                                    s_record.is_valid = False
                                    s_record.errors = {"db_error": str(e_row)}
                                    failed_count += 1
                                    
                            db.bulk_save_objects(staging_batch) # update staging
                            db.commit()
                            
                        current_batch = []
                        staging_batch = []
                
            history.status = "COMPLETED"
            history.completed_at = datetime.utcnow()
            history.total_records = total_records
            history.successful_records = success_count
            history.failed_records = failed_count
            
        except Exception as e:
            traceback.print_exc()
            db.rollback()
            history.status = "FAILED"
            history.error_message = str(e)
            
        db.commit()
        return ExecuteResponse(import_id=import_id, status=history.status, message="Execution finished")

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
