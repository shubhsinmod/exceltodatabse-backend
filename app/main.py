from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import router as api_router
from app.database import engine, Base
import os
from app.config import settings

# Create upload dir if not exists
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Excel Importer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")

@app.get("/")
def root():
    return {"message": "Excel Importer API is running"}
