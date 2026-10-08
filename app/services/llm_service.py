"""
llm_service.py
--------------
Dynamic, Generic SQL-Column-First Semantic Mapping Engine with Groq LLM Fallback.

CRITICAL ARCHITECTURAL RULES (ZERO HARDCODING):
  1. ZERO HARDCODED:
     - table names
     - column names
     - Excel column names
     - Excel -> SQL mappings
     - primary-key or foreign-key values
     - business field names or aliases
     - special cases or entity names
  2. SQL-COLUMN-FIRST APPROACH:
     For every selected SQL table:
       For each SQL column:
         Find compatible Excel source columns.
     NOT:
       For each Excel column: force it into a SQL column.
  3. NO FORCED MATCHING:
     If a SQL column has no suitable Excel source:
       source = None
       action = DATABASE_GENERATED (if IDENTITY/AUTO_INCREMENT)
              | DEFAULT            (if DEFAULT constraint present)
              | NULL               (if NULLABLE)
              | VALIDATION_ERROR   (if NOT NULL and cannot be generated)
  4. EXCEL COLUMNS CAN REMAIN UNUSED:
     If an Excel column does not belong to the selected SQL table:
       action = NOT_USED
  5. GENERIC SQL COLUMNS:
     If SQL contains generic columns (e.g. Column1, Column2, Comments),
     never use them as fallback sinks. High-confidence evidence required.
  6. DYNAMIC VALUE PATTERN & TYPE INFERENCE:
     Infers types strictly from actual sample values (regex/parsing):
     string, integer, decimal, date, datetime, boolean, identifier,
     phone-like, email-like, numeric-string.
  7. GROQ LLM REASONING:
     Used strictly for semantic reasoning on ambiguous candidates.
     Prompts ask: "Does any candidate genuinely match? Answer MATCH or NO_MATCH."
     Never instructed to "choose the closest column".
"""

import json
import logging
import urllib.request
import urllib.error
import re
import difflib
import hashlib
import time
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any, Optional, Tuple
from app.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Dynamic Value Pattern & Type Inference Engine (Inspects ACTUAL Values)
# ─────────────────────────────────────────────────────────────────────────────

class DynamicValueAnalyzer:
    """
    Analyzes actual sample data values from Excel columns.
    Infer types, value patterns, lengths, and distributions with ZERO column name knowledge.
    """

    EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')
    PHONE_PATTERN = re.compile(r'^\+?[0-9\s\-()]{7,20}$')
    DIGITS_ONLY_PATTERN = re.compile(r'^\d+$')
    BOOLEAN_VALUES = frozenset({'true', 'false', '1', '0', 'yes', 'no', 't', 'f', 'y', 'n'})
    GENERIC_SQL_COL_PATTERN = re.compile(r'^(col|column|field|attr|attribute|temp|extra|misc|custom)?_?\d+$', re.IGNORECASE)

    @classmethod
    def analyze_column_values(cls, values: list) -> dict:
        """
        Inspects actual values and returns semantic characteristics.
        """
        cleaned_values = []
        for v in values:
            if v is not None:
                s = str(v).strip()
                if s != "" and s.lower() != "null" and s.lower() != "nan":
                    cleaned_values.append(s)

        if not cleaned_values:
            return {
                "inferred_type": "string",
                "sample_values": [],
                "max_length": 0,
                "min_length": 0,
                "is_numeric": False,
                "is_integer": False,
                "is_decimal": False,
                "is_date": False,
                "is_datetime": False,
                "is_boolean": False,
                "is_email_like": False,
                "is_phone_like": False,
                "is_numeric_string": False,
                "uniqueness_ratio": 0.0,
            }

        total_count = len(cleaned_values)
        lengths = [len(v) for v in cleaned_values]
        max_len = max(lengths)
        min_len = min(lengths)
        unique_vals = set(cleaned_values)
        uniqueness_ratio = len(unique_vals) / total_count

        # Check boolean
        bool_match_count = sum(1 for v in cleaned_values if v.lower() in cls.BOOLEAN_VALUES)
        is_boolean = (bool_match_count == total_count and len(unique_vals) <= 2)

        # Check integer
        int_match_count = 0
        for v in cleaned_values:
            try:
                # Accept pure digits or signed integer
                if cls.DIGITS_ONLY_PATTERN.match(v) or (v.startswith('-') and cls.DIGITS_ONLY_PATTERN.match(v[1:])):
                    int(v)
                    int_match_count += 1
            except ValueError:
                pass
        is_integer = (int_match_count == total_count)

        # Check decimal / float
        float_match_count = 0
        has_decimal_point = False
        for v in cleaned_values:
            try:
                float(v)
                float_match_count += 1
                if '.' in v:
                    has_decimal_point = True
            except ValueError:
                pass
        is_decimal = (float_match_count == total_count and has_decimal_point)
        is_numeric = (is_integer or is_decimal or (float_match_count == total_count))

        # Check date / datetime
        date_count = 0
        has_time_component = False
        for v in cleaned_values:
            parsed = cls._try_parse_datetime(v)
            if parsed:
                date_count += 1
                if parsed.hour != 0 or parsed.minute != 0 or parsed.second != 0:
                    has_time_component = True
        is_date = (date_count == total_count and not has_time_component)
        is_datetime = (date_count == total_count and has_time_component)

        # Check email
        email_count = sum(1 for v in cleaned_values if cls.EMAIL_PATTERN.match(v))
        is_email_like = (email_count == total_count)

        # Check phone
        phone_count = sum(1 for v in cleaned_values if cls.PHONE_PATTERN.match(v) and len(re.sub(r'\D', '', v)) >= 7)
        is_phone_like = (phone_count == total_count and not is_numeric)

        # Check numeric string (e.g., zip codes, leading zeros, fixed length codes)
        is_numeric_string = (
            cls.DIGITS_ONLY_PATTERN.match(cleaned_values[0]) is not None and
            all(cls.DIGITS_ONLY_PATTERN.match(v) for v in cleaned_values) and
            (any(v.startswith('0') for v in cleaned_values if len(v) > 1) or (max_len == min_len and max_len in (5, 6, 9)))
        )

        # Infer Primary Type
        if is_boolean:
            inferred_type = "boolean"
        elif is_datetime:
            inferred_type = "datetime"
        elif is_date:
            inferred_type = "date"
        elif is_email_like:
            inferred_type = "email-like"
        elif is_phone_like:
            inferred_type = "phone-like"
        elif is_numeric_string:
            inferred_type = "numeric-string"
        elif is_integer:
            inferred_type = "integer"
        elif is_decimal:
            inferred_type = "decimal"
        else:
            inferred_type = "string"

        # Unique sample values (up to 4)
        sample_vals = []
        for v in cleaned_values:
            if v not in sample_vals:
                sample_vals.append(v)
            if len(sample_vals) >= 4:
                break

        return {
            "inferred_type": inferred_type,
            "sample_values": sample_vals,
            "max_length": max_len,
            "min_length": min_len,
            "is_numeric": is_numeric,
            "is_integer": is_integer,
            "is_decimal": is_decimal,
            "is_date": is_date,
            "is_datetime": is_datetime,
            "is_boolean": is_boolean,
            "is_email_like": is_email_like,
            "is_phone_like": is_phone_like,
            "is_numeric_string": is_numeric_string,
            "uniqueness_ratio": round(uniqueness_ratio, 2),
        }

    @staticmethod
    def _try_parse_datetime(v: str) -> Optional[datetime]:
        """Try parsing common date and datetime formats generically."""
        if len(v) < 6:
            return None
        # Common ISO and locale formats
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%m/%d/%Y",
            "%d-%m-%Y",
            "%Y/%m/%d",
            "%d/%m/%Y %H:%M:%S",
            "%m/%d/%Y %H:%M:%S",
        ]
        # Clean fractional seconds or timezone if present
        cleaned = re.sub(r'\.\d+', '', v).replace('Z', '')
        for fmt in formats:
            try:
                return datetime.strptime(cleaned, fmt)
            except (ValueError, TypeError):
                continue
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Dynamic SQL Type & Semantic Compatibility Evaluator
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_token(name: str) -> str:
    """Lowercase and strip whitespace, underscores, hyphens, and dots."""
    if not name:
        return ""
    return re.sub(r'[\s_\-\.]+', '', str(name).lower())


def _tokenize_words(s: str) -> list:
    """Split column names into lowercase alphanumeric word tokens."""
    if not s:
        return []
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', str(s))
    tokens = re.split(r'[\s_\-\.]+', s.lower())
    return [t for t in tokens if t]


class DynamicTypeCompatibility:
    """
    Evaluates compatibility between a SQL column metadata definition
    and an Excel column's actual inferred characteristics.
    ZERO hardcoding of column or table names.
    """

    @classmethod
    def check_compatibility(cls, sql_col_meta: dict, excel_val_meta: dict) -> Tuple[bool, str]:
        """
        Returns (is_compatible, reason).
        Checks:
          - SQL generic data type vs. Excel inferred type
          - Length constraints
          - Role constraints (e.g. numeric PK/identity cannot accept non-numeric text)
        """
        sql_type = (sql_col_meta.get("data_type") or "string").lower()
        excel_type = excel_val_meta.get("inferred_type", "string")
        is_pk = sql_col_meta.get("is_pk", False)
        is_identity = sql_col_meta.get("is_identity", False) or sql_col_meta.get("auto_increment", False)
        sql_length = sql_col_meta.get("length")

        # ── 1. Role Constraints: Numeric PK / Identity cannot accept non-numeric text ──
        if (is_pk or is_identity) and sql_type == "integer":
            if not excel_val_meta.get("is_integer", False):
                return False, "SQL column is integer Primary Key / Identity but Excel values are not integers"

        # ── 2. Strict Type Incompatibility Matrix ──
        # If SQL expects integer:
        if sql_type == "integer":
            if excel_type in ("date", "datetime", "email-like", "phone-like") or (excel_type == "string" and not excel_val_meta.get("is_integer", False)):
                return False, f"SQL expects integer but Excel values are {excel_type}"

        # If SQL expects float/decimal/numeric:
        if sql_type == "float":
            if excel_type in ("date", "datetime", "email-like", "phone-like") or (excel_type == "string" and not excel_val_meta.get("is_numeric", False)):
                return False, f"SQL expects numeric/decimal but Excel values are {excel_type}"

        # If SQL expects date/datetime:
        if sql_type == "date":
            if excel_type in ("integer", "decimal", "email-like", "phone-like", "boolean"):
                return False, f"SQL expects date/datetime but Excel values are {excel_type}"

        # If SQL expects boolean:
        if sql_type == "boolean":
            if not excel_val_meta.get("is_boolean", False) and excel_type not in ("integer", "boolean"):
                return False, f"SQL expects boolean but Excel values are {excel_type}"

        # ── 3. Length Constraints ──
        # If SQL varchar length is defined and small (e.g. char(2)), and Excel samples are significantly longer
        if sql_length and sql_length > 0 and sql_length <= 5:
            if excel_val_meta.get("min_length", 0) > sql_length:
                return False, f"Excel values length ({excel_val_meta.get('min_length')}) exceed SQL column length ({sql_length})"

        return True, "Compatible"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Dynamic Semantic Matching & Evidence Scoring
# ─────────────────────────────────────────────────────────────────────────────

class DynamicSemanticScorer:
    """
    Computes a generic match score between a SQL column and an Excel candidate.
    Uses:
      - Name similarity (Exact, Normalized, Token Jaccard, SequenceMatcher)
      - Table context alignment (Table name prefix/suffix)
      - Pattern alignment (e.g. SQL column mentions 'mail' and values are email-like)
      - Generic SQL column protection (Column1, Column2 cannot be sinks)
    """

    @classmethod
    def score_candidate(
        cls,
        sql_table: str,
        sql_col: str,
        sql_col_meta: dict,
        excel_col: str,
        excel_val_meta: dict,
    ) -> Tuple[float, str]:
        """
        Returns (score, reason) where score is between 0.0 and 1.0.
        """
        # Step 1: Type compatibility
        is_compat, compat_reason = DynamicTypeCompatibility.check_compatibility(sql_col_meta, excel_val_meta)
        if not is_compat:
            return 0.0, compat_reason

        norm_sql = _normalize_token(sql_col)
        norm_ex = _normalize_token(excel_col)
        sql_tokens = set(_tokenize_words(sql_col))
        ex_tokens = set(_tokenize_words(excel_col))
        tbl_tokens = set(_tokenize_words(sql_table))

        # ── Protection: Generic SQL Columns (e.g. Column1, Column2, Comments) ──
        # Must require exact or normalized name match; cannot act as generic text sinks.
        is_generic_sql = bool(DynamicValueAnalyzer.GENERIC_SQL_COL_PATTERN.match(sql_col))
        if is_generic_sql:
            if norm_sql == norm_ex or (norm_ex in norm_sql and len(norm_ex) >= len(norm_sql) - 1):
                return 0.98, "Direct match to generic column name"
            return 0.0, "Generic SQL column placeholder requires direct name match"

        # ── 1. Exact Name Match (case-insensitive) ──
        if sql_col.strip().lower() == excel_col.strip().lower():
            return 1.0, "Exact column name match"

        # ── 2. Normalized Name Match (stripped separators) ──
        if norm_sql == norm_ex:
            return 0.98, "Normalized column name match"

        # ── 3. Table-Prefixed or Table-Compound Match ──
        # e.g., SQL col: "Customer_Name" vs Excel col: "Name" in table "Customer"
        # or SQL col: "ID" vs Excel col: "CustomerID" in table "Customer"
        norm_tbl = _normalize_token(sql_table)
        if norm_tbl + norm_sql == norm_ex or norm_sql == norm_tbl + norm_ex:
            return 0.96, "Compound table-prefixed match"

        # If Excel column is pure column name and SQL column is Table_Column
        if ex_tokens and ex_tokens.issubset(sql_tokens) and (sql_tokens - ex_tokens).issubset(tbl_tokens):
            return 0.95, "Entity-scoped column match"

        # ── 4. Token Overlap & Sequence Similarity ──
        token_overlap = len(sql_tokens & ex_tokens)
        token_total = len(sql_tokens | ex_tokens)
        jaccard = (token_overlap / token_total) if token_total > 0 else 0.0

        seq_score = difflib.SequenceMatcher(None, norm_sql, norm_ex).ratio()

        base_score = max(seq_score, (seq_score * 0.5 + jaccard * 0.5))

        # Check for substantial shared root token (length >= 4)
        has_shared_significant_token = any(
            t1 == t2 or (len(t1) >= 4 and len(t2) >= 4 and (t1 in t2 or t2 in t1))
            for t1 in sql_tokens for t2 in ex_tokens
        )
        if has_shared_significant_token:
            base_score = max(base_score, 0.72)

        # ── 5. Pattern-Specific Positive Evidence ──
        # If SQL column contains words relating to detected value pattern
        excel_type = excel_val_meta.get("inferred_type", "")
        pattern_bonus = 0.0

        if excel_type == "email-like" and any("mail" in t for t in sql_tokens):
            pattern_bonus = 0.20
        elif excel_type == "phone-like" and any(t in ("phone", "mobile", "contact", "cell", "tel") for t in sql_tokens):
            pattern_bonus = 0.20
        elif (excel_type in ("date", "datetime")) and any(t in ("date", "time", "created", "modified", "timestamp") for t in sql_tokens):
            pattern_bonus = 0.15
        elif excel_type == "boolean" and any(t in ("is", "has", "active", "flag", "enabled") for t in sql_tokens):
            pattern_bonus = 0.15

        final_score = min(base_score + pattern_bonus, 0.95)

        # Short tokens (length <= 2, e.g. "GE"): require exact match to prevent accidental substring match
        if len(sql_col) <= 2 and norm_sql != norm_ex:
            return 0.0, "Short identifier requires exact match"

        reason = "Semantic similarity and value compatibility"
        if final_score >= 0.80:
            reason = "High semantic & token similarity"
        elif final_score >= 0.60:
            reason = "Moderate token and pattern alignment"

        return round(final_score, 2), reason


# ─────────────────────────────────────────────────────────────────────────────
# 4. LLM Service with Groq (Strict SQL-Column-First Reasoning)
# ─────────────────────────────────────────────────────────────────────────────

class LLMMappingService:
    """
    Groq LLM Service operating in strict SQL-Column-First mode.
    Asks: "Is any candidate a valid source for this SQL column? Answer MATCH or NO_MATCH."
    Never asks the LLM to guess or pick the closest column.
    """

    _llm_cache = {}

    @classmethod
    def call_groq_chat_completion(cls, messages: list, max_tokens: int = 4096) -> Tuple[str, dict]:
        """Calls Groq API via standard urllib with JSON object response format."""
        headers = {
            "Authorization": f"Bearer {settings.GROQ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "ExcelToDb-Dynamic-Engine/2.0",
        }
        payload = {
            "model": settings.GROQ_MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{settings.GROQ_BASE_URL}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )

        start_time = time.time()
        with urllib.request.urlopen(req, timeout=30) as response:
            duration = time.time() - start_time
            res_json = json.loads(response.read().decode("utf-8"))
            usage = res_json.get("usage", {})
            token_usage = {
                "model": res_json.get("model", settings.GROQ_MODEL),
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "request_duration": round(duration, 3),
                "groq_calls": 1,
            }
            content = res_json["choices"][0]["message"]["content"]
            return content, token_usage

    @classmethod
    def query_llm_for_sql_columns(
        cls,
        unresolved_sql_items: list,
    ) -> Tuple[dict, dict]:
        """
        Sends ambiguous SQL column candidates to Groq LLM.
        Each item contains:
          {
            "table": str,
            "sql_column": str,
            "sql_type": str,
            "role": str,
            "candidates": [ { "excel_column": str, "inferred_type": str, "sample_values": list } ]
          }
        Returns: ({ (table, sql_column): { "matched_excel_column": str | None, "confidence": float, "reason": str } }, token_usage)
        """
        if not unresolved_sql_items or not settings.GROQ_API_KEY:
            return {}, {"groq_calls": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "request_duration": 0}

        # Compact prompt construction
        system_prompt = (
            "You are an expert SQL Server database migration assistant.\n"
            "You will be given SQL columns from selected database tables along with candidate Excel columns and their actual sample values.\n"
            "For each SQL column, determine if any candidate Excel column genuinely represents the exact same data.\n\n"
            "STRICT RULES:\n"
            "1. Output ONLY a valid JSON object matching the requested schema.\n"
            "2. If a candidate genuinely matches, return status: 'MATCH' with the matched 'excel_column' and confidence (0.70 - 1.0).\n"
            "3. If NONE of the candidates genuinely match the SQL column semantics, return status: 'NO_MATCH' with excel_column: null.\n"
            "4. NEVER guess or pick the 'closest' column if it does not represent the same data.\n"
            "5. Never map non-numeric text to numeric ID/PK columns.\n"
        )

        query_payload = []
        for item in unresolved_sql_items:
            query_payload.append({
                "table": item["table"],
                "sql_column": item["sql_column"],
                "sql_type": item["sql_type"],
                "role": item["role"],
                "candidates": [
                    {
                        "excel_column": c["excel_column"],
                        "inferred_type": c["inferred_type"],
                        "sample_values": c["sample_values"][:3]
                    }
                    for c in item["candidates"]
                ]
            })

        user_prompt = (
            f"Evaluate the following SQL columns and candidate Excel columns:\n"
            f"{json.dumps(query_payload, indent=2)}\n\n"
            f"Respond with JSON format:\n"
            f"{{\n"
            f'  "decisions": [\n'
            f'    {{"table": "...", "sql_column": "...", "status": "MATCH" | "NO_MATCH", "excel_column": "..." | null, "confidence": 0.85, "reason": "..."}}\n'
            f'  ]\n'
            f"}}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        try:
            content, usage = cls.call_groq_chat_completion(messages)
            parsed = json.loads(content)
            decisions_map = {}
            for d in parsed.get("decisions", []):
                tbl = str(d.get("table", "")).strip()
                col = str(d.get("sql_column", "")).strip()
                status = str(d.get("status", "NO_MATCH")).upper()
                raw_conf = d.get("confidence")
                try:
                    conf = float(raw_conf) if raw_conf is not None else 0.0
                except (ValueError, TypeError):
                    conf = 0.0
                reason = d.get("reason", "LLM decision")

                if status == "MATCH" and ex_col:
                    decisions_map[(tbl.lower(), col.lower())] = {
                        "matched_excel_column": str(ex_col).strip(),
                        "confidence": conf,
                        "reason": reason
                    }
                else:
                    decisions_map[(tbl.lower(), col.lower())] = {
                        "matched_excel_column": None,
                        "confidence": 0.0,
                        "reason": reason or "No genuine match found"
                    }
            return decisions_map, usage
        except Exception as e:
            logger.warning(f"Groq LLM query failed or skipped: {e}")
            return {}, {"groq_calls": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "request_duration": 0}

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Master Orchestrator: SQL-Column-First Mapping Engine
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_semantic_mapping(
        cls,
        schema_info: list,
        excel_columns: list,
        sample_rows: list,
        selected_tables: list = None,
    ) -> dict:
        """
        Executes pure SQL-COLUMN-FIRST mapping against SELECTED TABLES ONLY.

        Pipeline:
          1. Filter schema_info to selected_tables.
          2. Dynamically analyze actual sample values for every Excel column.
          3. For each selected SQL table:
               For each SQL column:
                 Evaluate & score candidate Excel columns.
                 Determine best candidate or determine unmapped action:
                 DATABASE_GENERATED | DEFAULT | NULL | VALIDATION_ERROR.
          4. Call Groq LLM only for ambiguous candidates (asking MATCH / NO_MATCH).
          5. Enforce 1-to-1 dynamic conflict resolution (one SQL target per Excel source).
          6. Record unassigned Excel columns as NOT_USED.
          7. Return structured mapping result.
        """
        # ── Step 0: Filter schema to selected_tables ──────────────────────────
        all_schema_tables = {t["table_name"].lower(): t["table_name"] for t in schema_info}

        if selected_tables is not None:
            selected_clean = [st.strip() for st in selected_tables if str(st).strip()]
            if not selected_clean:
                raise ValueError("Please select at least one target table.")
            for st in selected_clean:
                if st.lower() not in all_schema_tables:
                    raise ValueError(f"Selected table '{st}' was not found in the uploaded schema.")
            selected_set = {st.lower() for st in selected_clean}
            filtered_schema = [t for t in schema_info if t["table_name"].lower() in selected_set]
        else:
            selected_clean = [t["table_name"] for t in schema_info]
            filtered_schema = list(schema_info)

        if not filtered_schema:
            raise ValueError("No matching tables found in schema for the requested selection.")

        # ── Step 1: Dynamic Analysis of Actual Excel Column Values ────────────
        excel_val_profiles = {}
        for col_name in excel_columns:
            col_vals = [r.get(col_name) for r in sample_rows] if sample_rows else []
            excel_val_profiles[col_name] = DynamicValueAnalyzer.analyze_column_values(col_vals)

        # ── Step 2: SQL-Column-First Candidate Scoring ────────────────────────
        AUTO_ACCEPT_THRESHOLD = getattr(settings, "AUTO_ACCEPT_CONFIDENCE", 0.70)
        unresolved_for_llm = []
        table_column_plans = {}  # { (table_name, col_name): plan_dict }

        for t in filtered_schema:
            tbl_name = t["table_name"]
            declared_fks = t.get("foreign_keys", [])
            fk_map = {}
            for fk in declared_fks:
                c_name = (fk.get("child_column") or fk.get("column") or "").lower()
                p_tbl = fk.get("parent_table") or fk.get("referenced_table")
                p_col = fk.get("parent_column") or fk.get("referenced_column")
                if c_name and p_tbl and p_col:
                    fk_map[c_name] = f"FK (References {p_tbl}.{p_col})"

            for col in t["columns"]:
                c_name = col["name"]
                c_low = c_name.lower()
                is_pk = col.get("is_pk", False)
                is_id = col.get("is_identity", False) or col.get("auto_increment", False)
                has_def = col.get("has_default", False)
                nullable = col.get("nullable", True)
                sql_type = col.get("sql_type") or col.get("data_type") or "string"
                role = "PK" if is_pk else (fk_map.get(c_low, "Normal"))

                sql_meta = {
                    "table": tbl_name,
                    "column": c_name,
                    "sql_type": sql_type,
                    "data_type": col.get("data_type", "string"),
                    "length": col.get("length"),
                    "precision": col.get("precision"),
                    "scale": col.get("scale"),
                    "is_pk": is_pk,
                    "is_identity": is_id,
                    "has_default": has_def,
                    "nullable": nullable,
                    "role": role,
                }

                # Score all Excel columns as potential sources for this SQL column
                candidates = []
                for ex_col in excel_columns:
                    ex_meta = excel_val_profiles[ex_col]
                    score, reason = DynamicSemanticScorer.score_candidate(
                        sql_table=tbl_name,
                        sql_col=c_name,
                        sql_col_meta=sql_meta,
                        excel_col=ex_col,
                        excel_val_meta=ex_meta
                    )
                    if score >= 0.30:
                        candidates.append({
                            "excel_column": ex_col,
                            "score": score,
                            "reason": reason,
                            "inferred_type": ex_meta["inferred_type"],
                            "sample_values": ex_meta["sample_values"]
                        })

                candidates.sort(key=lambda x: x["score"], reverse=True)

                plan_key = (tbl_name, c_name)
                # Check for standard unambiguous match
                if candidates and candidates[0]["score"] >= AUTO_ACCEPT_THRESHOLD:
                    top = candidates[0]
                    # Check if unambiguous
                    is_unambiguous = (len(candidates) == 1 or (top["score"] - candidates[1]["score"] >= 0.10))
                    if is_unambiguous:
                        table_column_plans[plan_key] = {
                            "table": tbl_name,
                            "sql_column": c_name,
                            "sql_type": sql_type,
                            "role": role,
                            "excel_source": top["excel_column"],
                            "action": "IMPORT",
                            "confidence": int(top["score"] * 100),
                            "method": "STANDARD",
                            "reason": top["reason"],
                            "candidates": candidates,
                            "sql_meta": sql_meta
                        }
                        continue

                # Ambiguous candidate with score >= 0.50 -> queue for LLM reasoning
                if candidates and candidates[0]["score"] >= 0.50:
                    unresolved_for_llm.append({
                        "table": tbl_name,
                        "sql_column": c_name,
                        "sql_type": sql_type,
                        "role": role,
                        "candidates": candidates[:3],
                        "sql_meta": sql_meta
                    })
                    table_column_plans[plan_key] = {
                        "table": tbl_name,
                        "sql_column": c_name,
                        "sql_type": sql_type,
                        "role": role,
                        "excel_source": None,
                        "action": "PENDING_LLM",
                        "confidence": int(candidates[0]["score"] * 100),
                        "method": "PENDING",
                        "reason": "Ambiguous candidates; awaiting semantic resolution",
                        "candidates": candidates,
                        "sql_meta": sql_meta
                    }
                else:
                    # No candidate passed threshold -> determine fallback action from schema
                    action = cls._determine_unmapped_action(sql_meta)
                    table_column_plans[plan_key] = {
                        "table": tbl_name,
                        "sql_column": c_name,
                        "sql_type": sql_type,
                        "role": role,
                        "excel_source": None,
                        "action": action,
                        "confidence": 100 if action in ("DATABASE_GENERATED", "DEFAULT", "NULL") else 0,
                        "method": "SCHEMA_DEFAULT" if action != "VALIDATION_ERROR" else "NONE",
                        "reason": f"No compatible Excel source; action determined by schema ({action})",
                        "candidates": candidates,
                        "sql_meta": sql_meta
                    }

        # ── Step 3: LLM Semantic Reasoning for Ambiguous Candidates ──────────
        llm_usage = {"groq_calls": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "request_duration": 0}
        if unresolved_for_llm:
            llm_decisions, llm_usage = cls.query_llm_for_sql_columns(unresolved_for_llm)
            for item in unresolved_for_llm:
                tbl_name = item["table"]
                c_name = item["sql_column"]
                plan_key = (tbl_name, c_name)
                decision = llm_decisions.get((tbl_name.lower(), c_name.lower()))

                if decision and decision["matched_excel_column"]:
                    matched_col = decision["matched_excel_column"]
                    # Validate that matched_col exists in candidate list
                    valid_cand = next((c for c in item["candidates"] if c["excel_column"].lower() == matched_col.lower()), None)
                    if valid_cand:
                        table_column_plans[plan_key].update({
                            "excel_source": valid_cand["excel_column"],
                            "action": "IMPORT",
                            "confidence": int(decision["confidence"] * 100) if decision["confidence"] > 0 else int(valid_cand["score"] * 100),
                            "method": "LLM",
                            "reason": decision.get("reason", "LLM verified semantic match")
                        })
                        continue

                # If LLM returned NO_MATCH or failed, assign schema fallback action
                action = cls._determine_unmapped_action(item["sql_meta"])
                table_column_plans[plan_key].update({
                    "excel_source": None,
                    "action": action,
                    "confidence": 100 if action in ("DATABASE_GENERATED", "DEFAULT", "NULL") else 0,
                    "method": "SCHEMA_DEFAULT" if action != "VALIDATION_ERROR" else "NONE",
                    "reason": f"No genuine match verified by LLM; schema action ({action})"
                })

        # ── Step 4: Strict 1-to-1 Conflict Resolution ────────────────────────
        # Ensure that one Excel source column is not accidentally assigned to multiple SQL columns.
        excel_usage = defaultdict(list)
        for plan_key, plan in table_column_plans.items():
            if plan["action"] == "IMPORT" and plan["excel_source"]:
                excel_usage[plan["excel_source"]].append(plan_key)

        mapping_conflicts = []
        for ex_col, plan_keys in excel_usage.items():
            if len(plan_keys) > 1:
                # Multiple SQL columns targeting the same Excel column
                # Select the one with the highest confidence / score
                sorted_plans = sorted(plan_keys, key=lambda pk: table_column_plans[pk]["confidence"], reverse=True)
                winner = sorted_plans[0]
                conflicting_targets = [f"[{table_column_plans[pk]['table']}].[{table_column_plans[pk]['sql_column']}]" for pk in plan_keys]

                conflict_msg = f"Excel column '{ex_col}' was matched to multiple SQL columns ({', '.join(conflicting_targets)})."
                mapping_conflicts.append({
                    "excel_column": ex_col,
                    "conflicting_targets": conflicting_targets,
                    "message": conflict_msg,
                })

                # Demote the other targets to conflict / schema fallback
                for loser in sorted_plans[1:]:
                    loser_meta = table_column_plans[loser]["sql_meta"]
                    action = cls._determine_unmapped_action(loser_meta)
                    table_column_plans[loser].update({
                        "excel_source": None,
                        "action": action,
                        "has_conflict": True,
                        "conflict_message": f"Conflict with {winner[0]}.{winner[1]}; reverted to {action}",
                        "method": "CONFLICT"
                    })

        # Also check reverse: did two Excel columns resolve to the exact same SQL column?
        # Handled naturally by SQL-column-first iteration (each SQL column selects at most one source).

        # ── Step 5: Identify Unused Excel Columns (action: NOT_USED) ──────────
        used_excel_cols = {
            plan["excel_source"] for plan in table_column_plans.values() 
            if plan["action"] == "IMPORT" and plan["excel_source"]
        }

        unused_excel_columns = []
        for ex_col in excel_columns:
            if ex_col not in used_excel_cols:
                profile = excel_val_profiles[ex_col]
                unused_excel_columns.append({
                    "excel_column": ex_col,
                    "action": "NOT_USED",
                    "inferred_type": profile["inferred_type"],
                    "sample_values": profile["sample_values"]
                })

        # ── Step 6: Assemble Structured Resolved Mappings ─────────────────────
        # Structure by table: { table_name: [ columns ] }
        tables_structured = {}
        all_resolved_list = []
        for plan_key, plan in table_column_plans.items():
            tbl_name = plan["table"]
            if tbl_name not in tables_structured:
                tables_structured[tbl_name] = []
            tables_structured[tbl_name].append(plan)
            all_resolved_list.append(plan)

        # Build legacy-compatible mappings array for UI review table
        # Shows Excel columns and their assigned target (or None/NOT_USED)
        legacy_mappings = []
        for ex_col in excel_columns:
            matched_plan = next((p for p in all_resolved_list if p.get("excel_source") == ex_col), None)
            ex_meta = excel_val_profiles[ex_col]

            if matched_plan:
                legacy_mappings.append({
                    "excel_column": ex_col,
                    "target_table": matched_plan["table"],
                    "target_column": matched_plan["sql_column"],
                    "action": matched_plan["action"],
                    "confidence": matched_plan["confidence"],
                    "method": matched_plan["method"],
                    "role": matched_plan["role"],
                    "reason": matched_plan["reason"],
                    "inferred_type": ex_meta["inferred_type"],
                    "sample_values": ex_meta["sample_values"],
                    "has_conflict": matched_plan.get("has_conflict", False),
                    "candidates": matched_plan.get("candidates", [])
                })
            else:
                legacy_mappings.append({
                    "excel_column": ex_col,
                    "target_table": "",
                    "target_column": "",
                    "action": "NOT_USED",
                    "confidence": 0,
                    "method": "UNRESOLVED",
                    "role": "-",
                    "reason": "Not mapped to any SQL column in selected table(s)",
                    "inferred_type": ex_meta["inferred_type"],
                    "sample_values": ex_meta["sample_values"],
                    "has_conflict": False,
                    "candidates": []
                })

        mapping_source = "STANDARD" if llm_usage["groq_calls"] == 0 else "HYBRID"

        statistics = {
            "total_excel_columns": len(excel_columns),
            "mapped_excel_columns": len(used_excel_cols),
            "unused_excel_columns": len(unused_excel_columns),
            "sql_columns_imported": sum(1 for p in all_resolved_list if p["action"] == "IMPORT"),
            "sql_columns_generated": sum(1 for p in all_resolved_list if p["action"] == "DATABASE_GENERATED"),
            "sql_columns_default": sum(1 for p in all_resolved_list if p["action"] == "DEFAULT"),
            "sql_columns_null": sum(1 for p in all_resolved_list if p["action"] == "NULL"),
            "sql_columns_validation_error": sum(1 for p in all_resolved_list if p["action"] == "VALIDATION_ERROR"),
            "conflicts": len(mapping_conflicts),
            "llm_calls": llm_usage.get("groq_calls", 0),
            "total_llm_tokens": llm_usage.get("total_tokens", 0)
        }

        return {
            "source": mapping_source,
            "table_mappings": tables_structured,
            "all_sql_columns": all_resolved_list,
            "unused_excel_columns": unused_excel_columns,
            "mappings": legacy_mappings,
            "mapping_conflicts": mapping_conflicts,
            "selected_tables": [t["table_name"] for t in filtered_schema],
            "llm_usage": llm_usage,
            "statistics": statistics
        }

    @staticmethod
    def _determine_unmapped_action(sql_meta: dict) -> str:
        """
        Determines the dynamic database action for a SQL column when no Excel source is mapped.
        Rule:
          - If IDENTITY / AUTO_INCREMENT -> DATABASE_GENERATED
          - Else if has DEFAULT constraint -> DEFAULT
          - Else if NULLABLE -> NULL
          - Else -> VALIDATION_ERROR (Required field missing)
        """
        if sql_meta.get("is_identity", False):
            return "DATABASE_GENERATED"
        if sql_meta.get("has_default", False):
            return "DEFAULT"
        if sql_meta.get("nullable", True):
            return "NULL"
        return "VALIDATION_ERROR"
