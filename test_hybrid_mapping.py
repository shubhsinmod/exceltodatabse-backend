import json
from app.services.schema_parser import parse_sql_schema
from app.services.llm_service import LLMMappingService

schema_sql = """
CREATE TABLE [dbo].[Customer] (
    [Customer_ID] BIGINT IDENTITY(1,1) NOT NULL,
    [Customer_Name] VARCHAR(100) NOT NULL,
    [Address] VARCHAR(500) NULL,
    [Mobile_Number] VARCHAR(50) NULL,
    CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([Customer_ID] ASC)
);
CREATE TABLE [dbo].[Orders] (
    [Order_ID] BIGINT IDENTITY(1,1) NOT NULL,
    [Customer_ID] BIGINT NOT NULL,
    [Order_Date] DATETIME NULL,
    [Total_Amount] DECIMAL(18,2) NULL,
    CONSTRAINT [PK_Orders] PRIMARY KEY CLUSTERED ([Order_ID] ASC)
);
"""

schema = parse_sql_schema(schema_sql)

print("=" * 60)
print("TEST 1: Obvious & Normalized Headers (Should have 0 Groq calls)")
print("=" * 60)

excel_cols_1 = ["Customer_Name", "customer name", "Address", "mobile number"]
sample_rows_1 = [
    {"Customer_Name": "John Doe", "customer name": "John Doe", "Address": "123 Main St", "mobile number": "9876543210"}
]

res1 = LLMMappingService.get_semantic_mapping(schema, excel_cols_1, sample_rows_1)
print(f"Source: {res1['source']}")
print(f"Groq Calls: {res1['llm_usage']['groq_calls']}")
print(f"Total Tokens: {res1['llm_usage']['total_tokens']}")
print(f"Statistics: {json.dumps(res1['statistics'], indent=2)}")
for m in res1["mappings"]:
    print(f"  {m['excel_column']} -> {m['target_table']}.{m['target_column']} (conf: {m['confidence']}, method: {m.get('method')})")

assert res1["llm_usage"]["groq_calls"] == 0, "Standard matches must make 0 Groq calls!"
assert res1["statistics"]["standard_matches"] == 4, "All 4 columns should be standard matches!"
print("\n>>> TEST 1 PASSED: 0 Groq calls, 100% standard matches!\n")

print("=" * 60)
print("TEST 2: Ambiguous Header (e.g. 'Party Details' or 'Contact Info')")
print("=" * 60)

excel_cols_2 = ["Customer_Name", "Party Details"]
sample_rows_2 = [
    {"Customer_Name": "John Doe", "Party Details": "9876543210"}
]

res2 = LLMMappingService.get_semantic_mapping(schema, excel_cols_2, sample_rows_2)
print(f"Source: {res2['source']}")
print(f"Groq Calls: {res2['llm_usage']['groq_calls']}")
print(f"Total Tokens: {res2['llm_usage']['total_tokens']}")
print(f"Token Details: {json.dumps(res2['llm_usage'], indent=2)}")
print(f"Statistics: {json.dumps(res2['statistics'], indent=2)}")
for m in res2["mappings"]:
    print(f"  {m['excel_column']} -> {m['target_table']}.{m['target_column']} (conf: {m['confidence']}, method: {m.get('method')})")

print("\n>>> TEST 2 PASSED!\n")

print("=" * 60)
print("TEST 3: Cache Verification (Re-query same ambiguous header)")
print("=" * 60)

calls_before = res2["llm_usage"]["groq_calls"]
res3 = LLMMappingService.get_semantic_mapping(schema, excel_cols_2, sample_rows_2)
print(f"Groq Calls on repeat: {res3['llm_usage']['groq_calls']} (Should be 0 due to cache)")
assert res3["llm_usage"]["groq_calls"] == 0, "Repeat ambiguous query must use cache!"
print("\n>>> TEST 3 PASSED: Cache hit verified!\n")
