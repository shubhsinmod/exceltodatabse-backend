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

@router.post("/import/upload", response_model=UploadResponse)
async def upload_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Only Excel files are supported")
    return await ImportService.process_upload(file, db)

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
