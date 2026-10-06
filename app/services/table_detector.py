import pandas as pd
from typing import List, Dict, Any
from app.services.header_detector import HeaderDetector

class TableDetector:
    @staticmethod
    def detect_tables(df: pd.DataFrame, sheet_name: str) -> List[Dict[str, Any]]:
        """
        Detect multiple tables within a single dataframe based on empty rows separating data blocks.
        """
        tables = []
        
        # Find completely empty rows
        empty_rows = df.isnull().all(axis=1)
        
        # Find blocks of data
        blocks = []
        current_block = []
        
        for idx, is_empty in empty_rows.items():
            if not is_empty:
                current_block.append(idx)
            elif current_block:
                # If we hit an empty row and we have a current block, save it
                # Only save if it has at least 2 rows (header + data)
                if len(current_block) >= 2:
                    blocks.append(current_block)
                current_block = []
                
        # Add the last block
        if len(current_block) >= 2:
            blocks.append(current_block)
            
        # If no distinct blocks found, treat the whole sheet as one table
        if not blocks and len(df) > 0:
            blocks = [list(range(len(df)))]
            
        table_index = 1
        for block in blocks:
            # Extract block dataframe
            block_df = df.loc[block].reset_index(drop=True)
            
            # Detect header for this block
            header_info = HeaderDetector.detect_header_row(block_df)
            
            # Original start row in the main dataframe
            original_start_row = block[0]
            
            table_name = f"{sheet_name.lower().replace(' ', '_')}_{table_index}" if len(blocks) > 1 else sheet_name.lower().replace(' ', '_')
            
            tables.append({
                "table_name": table_name,
                "block_df": block_df,
                "header_info": header_info,
                "original_start_row": original_start_row
            })
            table_index += 1
            
        return tables
