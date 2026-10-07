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
        
        file_ext = os.path.splitext(file_path)[1].lower()
        sheets = []
        
        try:
            if file_ext in ['.xlsx', '.xls']:
                xl = pd.ExcelFile(file_path)
                for sheet_name in xl.sheet_names:
                    df_full = xl.parse(sheet_name)
                    sheets.append({
                        "sheet_name": sheet_name,
                        "rows": len(df_full),
                        "columns": len(df_full.columns),
                        "is_empty": len(df_full) == 0
                    })
            elif file_ext == '.csv':
                # Quick read for CSV
                df_full = pd.read_csv(file_path, on_bad_lines='skip')
                sheets.append({
                    "sheet_name": "CSV Data",
                    "rows": len(df_full),
                    "columns": len(df_full.columns),
                    "is_empty": len(df_full) == 0
                })
            elif file_ext == '.json':
                df_full = pd.read_json(file_path)
                sheets.append({
                    "sheet_name": "JSON Data",
                    "rows": len(df_full),
                    "columns": len(df_full.columns),
                    "is_empty": len(df_full) == 0
                })
            else:
                # Try generic read (fallback to CSV)
                df_full = pd.read_csv(file_path, on_bad_lines='skip')
                sheets.append({
                    "sheet_name": "Data",
                    "rows": len(df_full),
                    "columns": len(df_full.columns),
                    "is_empty": len(df_full) == 0
                })
        except Exception as e:
            print(f"Error inspecting file: {e}")
            
        return {
            "file_name": file_name,
            "file_size_bytes": file_size,
            "total_sheets": len(sheets),
            "sheets": sheets
        }
