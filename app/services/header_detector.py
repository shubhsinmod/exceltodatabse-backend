import pandas as pd
import numpy as np

class HeaderDetector:
    @staticmethod
    def detect_header_row(df: pd.DataFrame, max_search_rows: int = 20) -> dict:
        best_score = -1000
        best_row_idx = -1
        
        search_limit = min(max_search_rows, len(df))
        if search_limit == 0:
            return {"header_row": -1, "data_start_row": 0, "confidence": 0.0}

        for i in range(search_limit):
            row = df.iloc[i]
            
            non_empty_count = row.notna().sum()
            if non_empty_count == 0:
                continue
            
            # String characteristics
            string_count = sum(1 for val in row if isinstance(val, str) and len(val.strip()) > 0)
            
            # Penalize long text (descriptions)
            long_text_count = sum(1 for val in row if isinstance(val, str) and len(str(val)) > 50)
            
            # Heavily penalize numeric values (data rows have numbers, headers usually don't)
            numeric_count = sum(1 for val in row if isinstance(val, (int, float)) and pd.notna(val) and not isinstance(val, bool))
            
            # Next rows consistency
            next_rows_consistency = 0
            if i + 1 < len(df):
                next_row = df.iloc[i+1]
                next_non_empty = next_row.notna().sum()
                if abs(next_non_empty - non_empty_count) <= 2:
                    next_rows_consistency = 1
            
            # Score formula
            score = (string_count * 2) - (long_text_count * 5) - (numeric_count * 10) + (non_empty_count * 1) + (next_rows_consistency * 5)
            
            if score > best_score:
                best_score = score
                best_row_idx = i
                
        max_possible = (len(df.columns) * 3) + 5
        confidence = min(max(best_score / max_possible, 0.0), 1.0)
        
        # If the best score is very poor (likely negative due to numeric penalties), assume no header exists
        if best_score < (len(df.columns) * 0.5):
            return {
                "header_row": -1,
                "data_start_row": 0,
                "confidence": round(confidence, 2)
            }
        
        return {
            "header_row": best_row_idx,
            "data_start_row": best_row_idx + 1,
            "confidence": round(confidence, 2)
        }
