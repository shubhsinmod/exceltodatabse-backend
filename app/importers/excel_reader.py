import pandas as pd
import numpy as np
from typing import List, Dict, Any, Tuple
import os

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
    def parse_excel(file_path: str) -> List[Dict[str, Any]]:
        sheets_info = []
        xl = pd.ExcelFile(file_path)
        
        for sheet_name in xl.sheet_names:
            df = xl.parse(sheet_name)
            
            # Clean dataframe
            df = df.dropna(how='all') # Drop completely empty rows
            df = df.dropna(axis=1, how='all') # Drop completely empty columns
            
            # Convert NaNs to None for JSON serialization
            df = df.replace({np.nan: None})
            
            # Get sample data
            sample_df = df.head(20)
            sample_data = sample_df.to_dict(orient="records")
            
            # Infer types
            inferred_types = {str(col): ExcelReader._map_dtype(df[col].dtype) for col in df.columns}
            
            sheets_info.append({
                "sheet_name": sheet_name,
                "rows_count": len(df),
                "columns": [str(c) for c in df.columns],
                "sample_data": sample_data,
                "inferred_types": inferred_types
            })
            
        return sheets_info

    @staticmethod
    def read_sheet_data(file_path: str, sheet_name: str) -> List[Dict[str, Any]]:
        df = pd.read_excel(file_path, sheet_name=sheet_name)
        df = df.dropna(how='all').dropna(axis=1, how='all')
        df = df.replace({np.nan: None})
        
        # Convert date columns to ISO format string
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].dt.strftime('%Y-%m-%dT%H:%M:%S')
                
        return df.to_dict(orient="records")
