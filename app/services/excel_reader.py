import pandas as pd
import numpy as np
import os
import uuid
from typing import List, Dict, Any
from app.config import settings
from app.services.workbook_inspector import WorkbookInspector
from app.services.table_detector import TableDetector
from app.services.data_cleaner import DataCleaner

class ExcelReader:
    @staticmethod
    def _map_dtype(dtype) -> str:
        if pd.api.types.is_integer_dtype(dtype):
            return "integer"
        elif pd.api.types.is_float_dtype(dtype):
            return "float"
        elif pd.api.types.is_datetime64_any_dtype(dtype):
            return "date"
        elif pd.api.types.is_bool_dtype(dtype):
            return "boolean"
        else:
            return "string"

    @staticmethod
    def process_and_cache_workbook(file_path: str, import_id: uuid.UUID) -> dict:
        """
        Reads workbook, extracts tables, cleans them, caches them as parquet,
        and returns the clean intermediate representation metadata.
        """
        inspection_result = WorkbookInspector.inspect(file_path)
        
        file_ext = os.path.splitext(file_path)[1].lower()
        
        cache_dir = os.path.join(settings.UPLOAD_DIR, str(import_id))
        os.makedirs(cache_dir, exist_ok=True)
        
        tables_metadata = []
        
        # Determine sheets and dataframes based on file extension
        sheet_dataframes = {}
        if file_ext in ['.xlsx', '.xls']:
            xl = pd.ExcelFile(file_path)
            for sheet_name in xl.sheet_names:
                sheet_dataframes[sheet_name] = xl.parse(sheet_name, header=None)
        elif file_ext == '.csv':
            sheet_dataframes["CSV Data"] = pd.read_csv(file_path, header=None, on_bad_lines='skip')
        elif file_ext == '.json':
            sheet_dataframes["JSON Data"] = pd.read_json(file_path)
        else:
            sheet_dataframes["Data"] = pd.read_csv(file_path, header=None, on_bad_lines='skip')
            
        for sheet_name, df in sheet_dataframes.items():
            # Detect multiple tables in the sheet/dataframe
            detected_tables = TableDetector.detect_tables(df, sheet_name)
            
            for table_info in detected_tables:
                # Clean the data
                clean_table = DataCleaner.clean_table_data(table_info)
                
                if not clean_table or clean_table["row_count"] == 0:
                    continue
                    
                data_df = clean_table.pop("data_df") # Remove from metadata
                
                # Inferred types from the clean data
                inferred_types = {col: ExcelReader._map_dtype(data_df[col].dtype) for col in data_df.columns}
                clean_table["inferred_types"] = inferred_types
                
                # Sample data
                sample_df = data_df.head(20).replace({np.nan: None})
                
                # Convert datetime for json
                for col in sample_df.columns:
                    if pd.api.types.is_datetime64_any_dtype(sample_df[col]):
                        sample_df[col] = sample_df[col].dt.strftime('%Y-%m-%dT%H:%M:%S')
                
                clean_table["sample_data"] = sample_df.to_dict(orient="records")
                clean_table["sheet_name"] = sheet_name
                
                # Save as parquet cache
                parquet_path = os.path.join(cache_dir, f"{clean_table['table_name']}.parquet")
                # Ensure string column names for parquet
                data_df.columns = data_df.columns.astype(str)
                data_df.to_parquet(parquet_path)
                
                tables_metadata.append(clean_table)
                
        return {
            "inspection": inspection_result,
            "tables": tables_metadata
        }

    @staticmethod
    def read_cached_table(import_id: uuid.UUID, table_name: str) -> List[Dict[str, Any]]:
        parquet_path = os.path.join(settings.UPLOAD_DIR, str(import_id), f"{table_name}.parquet")
        if not os.path.exists(parquet_path):
            return []
            
        df = pd.read_parquet(parquet_path)
        df = df.replace({np.nan: None})
        
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].dt.strftime('%Y-%m-%dT%H:%M:%S')
                
        return df.to_dict(orient="records")
