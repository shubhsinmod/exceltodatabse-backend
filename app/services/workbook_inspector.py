import pandas as pd
import os

class WorkbookInspector:
    @staticmethod
    def inspect(file_path: str) -> dict:
        """
        Inspect the workbook and return metadata about sheets.
        """
        file_size = os.path.getsize(file_path)
        file_name = os.path.basename(file_path)
        
        xl = pd.ExcelFile(file_path)
        sheets = []
        
        for sheet_name in xl.sheet_names:
            df = xl.parse(sheet_name, nrows=0) # Just get columns info to check if empty
            # To get row counts quickly without full parse is tricky in pandas, 
            # we do a full parse for the inspection to get accurate counts 
            # (since pandas doesn't expose fast row counts without reading)
            # but we can cache it later.
            
            df_full = xl.parse(sheet_name)
            
            sheets.append({
                "sheet_name": sheet_name,
                "rows": len(df_full),
                "columns": len(df_full.columns),
                "is_empty": len(df_full) == 0
            })
            
        return {
            "file_name": file_name,
            "file_size_bytes": file_size,
            "total_sheets": len(sheets),
            "sheets": sheets
        }
