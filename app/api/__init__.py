from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.api_schemas import (
    UploadResponse, PreviewResponse, ExecuteRequest, ExecuteResponse, 
    ImportHistoryResponse, ErrorsResponse
)
from app.services.import_service import ImportService
from typing import List, Optional, Dict, Any
from uuid import UUID

router = APIRouter()

from app.services.schema_parser import parse_sql_schema

@router.post("/import/upload", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.lower().endswith(('.xlsx', '.xls', '.csv', '.json')):
        raise HTTPException(status_code=400, detail="Unsupported file format")
    return await ImportService.process_upload(file, db)

from pydantic import BaseModel

class TableMatchRequest(BaseModel):
    selected_tables: List[str]
    schema_info: Optional[List[Dict[str, Any]]] = None

class GenerateRequest(BaseModel):
    schema_info: list
    matched_columns: Optional[list] = None
    column_mappings: Optional[dict] = None
    table_plans: Optional[list] = None
    selected_tables: Optional[List[str]] = None
    include_identity: bool = False

@router.post("/import/schema")
@router.post("/import/{import_id}/schema")
async def upload_schema(import_id: Optional[UUID] = None, file: UploadFile = File(...)):
    if not file.filename.endswith((".sql", ".json", ".txt")):
        raise HTTPException(status_code=400, detail="Invalid file type. Please upload a .sql, .txt, or .json schema file.")
    try:
        content = await file.read()
        is_json = file.filename.endswith('.json')
        schema_info = parse_sql_schema(content, is_json)
        
        if import_id:
            ImportService.cache_schema(import_id, schema_info)

        table_list = [
            {
                "table_name": t["table_name"],
                "schema": t.get("schema", "dbo"),
                "column_count": t.get("column_count", len(t["columns"])),
                "primary_key": t.get("primary_key"),
                "has_identity": t.get("has_identity", False),
                "identity_columns": t.get("identity_columns", []),
                "fk_count": t.get("fk_count", len(t.get("foreign_keys", []))),
                "declared_fks": t.get("declared_fks", []),
                "related_tables": t.get("related_tables", []),
            }
            for t in schema_info
        ]

        return {
            "status": "SUCCESS",
            "schema_info": schema_info,
            "tables": table_list,
            "total_tables": len(table_list),
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"Unable to parse the SQL schema. Please verify that the uploaded file contains valid CREATE TABLE statements. ({str(e)})")


@router.post("/import/{import_id}/auto_match")
async def auto_match_endpoint(
    import_id: UUID,
    request: Request,
    db: Session = Depends(get_db)
):
    try:
        schema_info = None
        selected_tables = None

        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            body = await request.json()
            selected_tables = body.get("selected_tables")
            schema_info = body.get("schema_info")
        elif "multipart/form-data" in ct:
            form = await request.form()
            file = form.get("file")
            if file:
                content = await file.read()
                is_json = file.filename.endswith(".json") if hasattr(file, "filename") and file.filename else False
                schema_info = parse_sql_schema(content, is_json)
                ImportService.cache_schema(import_id, schema_info)
            st_raw = form.get("selected_tables")
            if st_raw:
                import json
                try:
                    selected_tables = json.loads(st_raw) if isinstance(st_raw, str) else st_raw
                except Exception:
                    selected_tables = [s.strip() for s in str(st_raw).split(",") if s.strip()]

        if not schema_info:
            schema_info = ImportService.get_cached_schema(import_id)

        if not schema_info:
            raise HTTPException(status_code=400, detail="No SQL schema found. Please upload a schema file first.")

        # Validate selected_tables if provided
        all_tables = {t["table_name"].lower(): t["table_name"] for t in schema_info}
        if selected_tables is not None:
            if not selected_tables or len(selected_tables) == 0:
                raise HTTPException(status_code=400, detail="Please select at least one target table.")
            for st in selected_tables:
                if st.strip().lower() not in all_tables:
                    raise HTTPException(status_code=400, detail=f"Selected table '{st}' was not found in the uploaded schema.")

        match_results = ImportService.auto_match_schema(
            import_id=import_id,
            schema_info=schema_info,
            db=db,
            selected_tables=selected_tables
        )
        return {
            "status": "SUCCESS",
            "schema_info": schema_info,
            "mapping_results": match_results,
            "selected_tables": selected_tables
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/import/{import_id}/generate_sql")
async def generate_sql_endpoint(import_id: UUID, request: GenerateRequest, db: Session = Depends(get_db)):
    try:
        matched = request.matched_columns or []
        if not matched and request.table_plans:
            matched = [
                {
                    "table": p["table"],
                    "column": p.get("sql_column", p.get("column")),
                    "excel_column": p.get("excel_source") or p.get("excel_column"),
                    "raw_data_key": p.get("raw_data_key", p.get("excel_source") or p.get("excel_column"))
                }
                for p in request.table_plans
                if str(p.get("action") or "").upper() in ("IMPORT", "EXCEL") and (p.get("excel_source") or p.get("excel_column"))
            ]

        result = ImportService.generate_sql_for_selected_tables(
            import_id=import_id,
            matched=matched,
            schema_info=request.schema_info,
            db=db,
            include_identity=request.include_identity,
            selected_tables=request.selected_tables,
            table_plans=request.table_plans,
        )
        if isinstance(result, dict):
            return {
                "status": "SUCCESS",
                "sql_script": result["sql_script"],
                "validation_report": result.get("validation_report", {}),
            }
        return {"status": "SUCCESS", "sql_script": result}
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
