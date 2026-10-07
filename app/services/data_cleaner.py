import pandas as pd
from typing import Dict, Any

class DataCleaner:
    @staticmethod
    def clean_table_data(table_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Clean the extracted table block using the detected header info.
        Remove unnamed empty columns, blank rows, and formatting artifacts.
        """
        block_df = table_info["block_df"]
        header_info = table_info["header_info"]
        
        header_row_idx = header_info["header_row"]
        data_start_idx = header_info["data_start_row"]
        
        if len(block_df) <= data_start_idx:
            return None
            
        data_df = block_df.iloc[data_start_idx:].copy()
        
        if header_row_idx == -1:
            # No header detected, use generic names
            raw_headers = [f"Column_{i+1}" for i in range(len(block_df.columns))]
        else:
            raw_headers = block_df.iloc[header_row_idx].fillna("Unnamed").astype(str).tolist()
            
        valid_columns = []
        clean_headers = []
        normalized_headers = []
        
        for col_idx, col_name in enumerate(raw_headers):
            # Check if column is completely empty in data rows
            if data_df.iloc[:, col_idx].notna().any():
                valid_columns.append(col_idx)
                
                clean_name = str(col_name).strip()
                if "Unnamed" in clean_name and not data_df.iloc[:, col_idx].notna().any():
                     continue 
                
                if "Unnamed" in clean_name:
                    clean_name = f"Column_{col_idx+1}"
                    
                clean_headers.append(clean_name)
                
                normalized = clean_name.lower().replace(" ", "_").replace(r"[^\w\s]", "")
                normalized_headers.append(normalized)
                
        clean_data_df = data_df.iloc[:, valid_columns].copy()
        clean_data_df.columns = normalized_headers
        
        # Remove completely empty rows within the data
        clean_data_df = clean_data_df.dropna(how='all')
        
        return {
            "table_name": table_info["table_name"],
            "original_start_row": table_info["original_start_row"],
            "header_row_local": header_row_idx,
            "headers": clean_headers,
            "normalized_headers": normalized_headers,
            "row_count": len(clean_data_df),
            "data_df": clean_data_df,
            "ignored_rows": list(range(0, data_start_idx))
        }
