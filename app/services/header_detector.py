import pandas as pd
import numpy as np

class HeaderDetector:
    @staticmethod
    def detect_header_row(df: pd.DataFrame, max_search_rows: int = 20) -> dict:
        """
        Detect the header row of a dataframe (which might contain metadata at the top).
        Returns a dict: {'header_row': index, 'data_start_row': index, 'confidence': float}
        """
        best_score = -1
        best_row_idx = 0
        
        search_limit = min(max_search_rows, len(df))
        if search_limit == 0:
            return {"header_row": 0, "data_start_row": 1, "confidence": 0.0}

        for i in range(search_limit):
            row = df.iloc[i]
            
            # 1. Non-empty ratio
            non_empty_count = row.notna().sum()
            if non_empty_count == 0:
                continue
            
            # 2. String characteristics
            string_count = sum(1 for val in row if isinstance(val, str) and len(val.strip()) > 0)
            
            # Penalize long text (descriptions)
            long_text_count = sum(1 for val in row if isinstance(val, str) and len(str(val)) > 50)
            
            # 3. Next rows consistency
            next_rows_consistency = 0
            if i + 1 < len(df):
                next_row = df.iloc[i+1]
                next_non_empty = next_row.notna().sum()
                # Data rows usually have similar or same number of columns filled
                if abs(next_non_empty - non_empty_count) <= 2:
                    next_rows_consistency = 1
            
            # Calculate score
            score = (string_count * 2) - (long_text_count * 5) + (non_empty_count * 1) + (next_rows_consistency * 5)
            
            if score > best_score:
                best_score = score
                best_row_idx = i
                
        # Calculate a pseudo-confidence
        max_possible = (len(df.columns) * 3) + 5
        confidence = min(max(best_score / max_possible, 0.1), 0.99)
        
        return {
            "header_row": best_row_idx,
            "data_start_row": best_row_idx + 1,
            "confidence": round(confidence, 2)
        }
