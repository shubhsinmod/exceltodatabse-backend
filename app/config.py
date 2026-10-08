from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./restaurant.db"
    UPLOAD_DIR: str = "uploads"
    
    # LLM Settings (Defaults to Groq openai/gpt-oss-120b, loaded from .env)
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    
    # Payload limits & sample parameters
    LLM_MAX_PAYLOAD_BYTES: int = 100000
    LLM_SAMPLE_VALUES_PER_COLUMN: int = 3
    LLM_MAX_CANDIDATES_PER_COL: int = 6
    
    # Confidence thresholds
    AUTO_ACCEPT_CONFIDENCE: float = 0.90
    WARNING_CONFIDENCE: float = 0.70
    
    # SQL Dialect: 'tsql' (SQL Server) | 'mysql' | 'postgres'
    SQL_DIALECT: str = "tsql"
    
    # SQL Insert Batch Size (Default: 1000, SQL Server maximum for multi-row VALUES)
    SQL_INSERT_BATCH_SIZE: int = 1000
    
    # Enable LLM Cache
    ENABLE_LLM_CACHE: bool = True
    
    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
