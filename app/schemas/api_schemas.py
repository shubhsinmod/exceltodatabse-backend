from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from datetime import datetime
from uuid import UUID

class UploadResponse(BaseModel):
    import_id: UUID
    file_name: str
    sheets: List[str]

class TablePreview(BaseModel):
    sheet_name: str
    table_name: str
    row_count: int
    headers: List[str]
    normalized_headers: List[str]
    sample_data: List[Dict[str, Any]]
    inferred_types: Dict[str, str]
    header_row_local: int
    ignored_rows: List[int]

class PreviewResponse(BaseModel):
    import_id: UUID
    file_name: str
    inspection: Dict[str, Any]
    tables: List[TablePreview]

class ColumnMapping(BaseModel):
    excel_column: str
    target_column: str
    target_type: str # string, integer, float, date, boolean
    is_primary_key: bool = False
    include: bool = True

class TableMapping(BaseModel):
    source_table_name: str
    target_table: str
    columns: List[ColumnMapping]

class ExecuteRequest(BaseModel):
    mappings: List[TableMapping]

class ExecuteResponse(BaseModel):
    import_id: UUID
    status: str
    message: str

class ImportHistoryResponse(BaseModel):
    id: UUID
    file_name: str
    upload_date: datetime
    total_records: int
    successful_records: int
    failed_records: int
    duplicate_records: int
    status: str
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class StagingError(BaseModel):
    row_index: int
    sheet_name: str
    raw_data: Dict[str, Any]
    errors: Dict[str, Any]

class ErrorsResponse(BaseModel):
    import_id: UUID
    errors: List[StagingError]
