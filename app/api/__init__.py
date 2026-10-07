from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.api_schemas import (
    UploadResponse, PreviewResponse, ExecuteRequest, ExecuteResponse, 
    ImportHistoryResponse, ErrorsResponse
)
from app.services.import_service import ImportService
from typing import List
from uuid import UUID

router = APIRouter()

from app.services.schema_parser import parse_sql_schema

@router.post("/import/upload", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.lower().endswith(('.xlsx', '.xls', '.csv', '.json')):
        raise HTTPException(status_code=400, detail="Unsupported file format")
    return await ImportService.process_upload(file, db)

@router.post("/import/schema")
async def upload_schema(file: UploadFile = File(...)):
    if not file.filename.endswith((".sql", ".json", ".txt")):
        raise HTTPException(status_code=400, detail="Invalid file type")
    content = await file.read()
    is_json = file.filename.endswith(".json")
@router.post("/import/{import_id}/auto_match")
async def auto_match_endpoint(import_id: UUID, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.endswith((".sql", ".json", ".txt")):
        raise HTTPException(status_code=400, detail="Invalid file type")
    content = await file.read()
    is_json = file.filename.endswith(".json")
    schema_info = parse_sql_schema(content.decode("utf-8"), is_json)
    
    try:
        # Save schema info somewhere if needed, but for now we'll just return it and the matches
        match_results = ImportService.auto_match_schema(import_id, schema_info, db)
        return {"status": "SUCCESS", "schema_info": schema_info, "mapping_results": match_results}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))

from pydantic import BaseModel
class GenerateRequest(BaseModel):
    schema_info: list
    matched_columns: list

@router.post("/import/{import_id}/generate_sql")
async def generate_sql_endpoint(import_id: UUID, request: GenerateRequest, db: Session = Depends(get_db)):
    try:
        sql_script = ImportService.generate_sql_from_matches(import_id, request.matched_columns, request.schema_info, db)
        return {"status": "SUCCESS", "sql_script": sql_script}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/import/{import_id}/preview", response_model=PreviewResponse)
def get_preview(import_id: UUID, db: Session = Depends(get_db)):
    preview = ImportService.get_preview(import_id, db)
    if not preview:
        raise HTTPException(status_code=404, detail="Import not found")
    return preview

@router.post("/import/{import_id}/execute", response_model=ExecuteResponse)
def execute_import(import_id: UUID, request: ExecuteRequest, db: Session = Depends(get_db)):
    return ImportService.execute_import(import_id, request.mappings, db)

@router.get("/import/history", response_model=List[ImportHistoryResponse])
def get_history(db: Session = Depends(get_db)):
    return ImportService.get_history(db)

@router.get("/import/{import_id}", response_model=ImportHistoryResponse)
def get_import_status(import_id: UUID, db: Session = Depends(get_db)):
    record = ImportService.get_import_record(import_id, db)
    if not record:
        raise HTTPException(status_code=404, detail="Import not found")
    return record

@router.get("/import/{import_id}/errors", response_model=ErrorsResponse)
def get_errors(import_id: UUID, db: Session = Depends(get_db)):
    return ImportService.get_errors(import_id, db)
