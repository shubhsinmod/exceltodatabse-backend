from sqlalchemy import Column, String, Integer, DateTime, JSON, ForeignKey, Boolean, Uuid
import uuid
from datetime import datetime
from app.database import Base

class ImportHistory(Base):
    __tablename__ = "import_history"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    file_name = Column(String, nullable=False)
    upload_date = Column(DateTime, default=datetime.utcnow)
    total_records = Column(Integer, default=0)
    successful_records = Column(Integer, default=0)
    failed_records = Column(Integer, default=0)
    duplicate_records = Column(Integer, default=0)
    status = Column(String, default="PENDING") # PENDING, VALIDATING, READY, IMPORTING, COMPLETED, FAILED
    error_message = Column(String, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    mapping_config = Column(JSON, nullable=True)

class StagingRecord(Base):
    __tablename__ = "staging_records"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    import_id = Column(Uuid, ForeignKey("import_history.id", ondelete="CASCADE"), index=True)
    sheet_name = Column(String, nullable=False, index=True)
    target_table = Column(String, nullable=True)
    row_index = Column(Integer, nullable=False)
    raw_data = Column(JSON, nullable=False)
    mapped_data = Column(JSON, nullable=True)
    is_valid = Column(Boolean, default=False)
    errors = Column(JSON, nullable=True)
    status = Column(String, default="PENDING") # PENDING, VALID, ERROR, IMPORTED
