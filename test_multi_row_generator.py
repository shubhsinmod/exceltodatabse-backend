"""
test_multi_row_generator.py
---------------------------
Comprehensive tests for multi-row INSERT generation, batching, escaping,
and table dependency handling.
"""

import re
from app.services.sql_generator import DeterministicSQLGenerator, _format_value
from app.services.validation_engine import DeterministicValidationEngine
from app.services.schema_parser import parse_sql_schema
from app.config import settings

def test_value_formatting():
    print("Testing value formatting...")
    # 9. String containing apostrophe
    assert _format_value("John's Restaurant", "string") == "'John''s Restaurant'"
    # 10. NULL value
    assert _format_value(None, "string") == "NULL"
    assert _format_value("", "string") == "NULL"
    assert _format_value("   ", "string") == "NULL"
    # 11. Decimal values
    assert _format_value(100.50, "float") == "100.5"
    assert _format_value("250.75", "decimal") == "250.75"
    # Integer
    assert _format_value("42", "integer") == "42"
    # 12 & 13. Date & DateTime values
    date_val = _format_value("2026-06-30 14:30:00", "datetime")
    assert date_val == "'2026-06-30 14:30:00'"
    print("  -> Value formatting PASSED!")


def test_batching_scenarios():
    print("\nTesting batching scenarios (1, 10, 999, 1000, 1001, 4280 rows)...")
    schema_info = [
        {
            "schema": "dbo",
            "table_name": "Customer",
            "columns": [
                {"name": "Customer_ID", "data_type": "integer", "is_pk": True, "is_identity": True},
                {"name": "Customer_Name", "data_type": "string", "nullable": False},
                {"name": "Address", "data_type": "string", "nullable": True},
            ],
            "foreign_keys": []
        }
    ]
    table_mappings = {
        "customer": {
            "name": "Customer_Name",
            "addr": "Address"
        }
    }

    def generate_for_row_count(n):
        rows = [{"name": f"Customer {i}", "addr": f"Address {i}"} for i in range(1, n + 1)]
        val_result = {
            "valid_tables": ["Customer"],
            "invalid_tables": {},
            "table_data": {
                "Customer": {
                    "valid_rows": rows,
                    "rejected_rows": []
                }
            }
        }
        return DeterministicSQLGenerator.generate_sql(
            validated_result=val_result,
            schema_info=schema_info,
            table_mappings=table_mappings,
            dialect="tsql",
            use_transaction=True,
            batch_size=1000
        )

    # 1. Table with 1 row
    sql_1 = generate_for_row_count(1)
    insert_stmts_1 = [s for s in sql_1.split(";") if "INSERT INTO [dbo].[Customer]" in s]
    assert len(insert_stmts_1) == 1, f"Expected 1 INSERT statement for 1 row, got {len(insert_stmts_1)}"
    assert "[Customer_ID]" not in sql_1, "Identity column must be omitted from INSERT statements"
    assert "'Customer 1'" in sql_1
    print("  -> 1 row: 1 INSERT statement PASSED")

    # 2. Table with 10 rows
    sql_10 = generate_for_row_count(10)
    insert_stmts_10 = [s for s in sql_10.split(";") if "INSERT INTO [dbo].[Customer]" in s]
    assert len(insert_stmts_10) == 1, f"Expected 1 INSERT statement for 10 rows, got {len(insert_stmts_10)}"
    # Count tuple rows
    tuple_count_10 = sql_10.count("(\n    'Customer ")
    assert tuple_count_10 == 10, f"Expected 10 row tuples, got {tuple_count_10}"
    print("  -> 10 rows: 1 multi-row INSERT with 10 tuples PASSED")

    # 3. Table with 999 rows
    sql_999 = generate_for_row_count(999)
    insert_stmts_999 = [s for s in sql_999.split(";") if "INSERT INTO [dbo].[Customer]" in s]
    assert len(insert_stmts_999) == 1, f"Expected 1 INSERT statement for 999 rows, got {len(insert_stmts_999)}"
    tuple_count_999 = sql_999.count("(\n    'Customer ")
    assert tuple_count_999 == 999, f"Expected 999 row tuples, got {tuple_count_999}"
    print("  -> 999 rows: 1 multi-row INSERT with 999 tuples PASSED")

    # 4. Table with 1,000 rows
    sql_1000 = generate_for_row_count(1000)
    insert_stmts_1000 = [s for s in sql_1000.split(";") if "INSERT INTO [dbo].[Customer]" in s]
    assert len(insert_stmts_1000) == 1, f"Expected 1 INSERT statement for 1000 rows, got {len(insert_stmts_1000)}"
    tuple_count_1000 = sql_1000.count("(\n    'Customer ")
    assert tuple_count_1000 == 1000
    print("  -> 1,000 rows: 1 multi-row INSERT with 1,000 tuples PASSED")

    # 5. Table with 1,001 rows -> 2 batches (1000 + 1)
    sql_1001 = generate_for_row_count(1001)
    insert_stmts_1001 = [s for s in sql_1001.split(";") if "INSERT INTO [dbo].[Customer]" in s]
    assert len(insert_stmts_1001) == 2, f"Expected 2 INSERT statements for 1001 rows, got {len(insert_stmts_1001)}"
    tuple_count_1001 = sql_1001.count("(\n    'Customer ")
    assert tuple_count_1001 == 1001
    print("  -> 1,001 rows: 2 multi-row INSERT batches (1000 + 1) PASSED")

    # 6. Table with 4,280 rows -> 5 batches (1000, 1000, 1000, 1000, 280)
    sql_4280 = generate_for_row_count(4280)
    insert_stmts_4280 = [s for s in sql_4280.split(";") if "INSERT INTO [dbo].[Customer]" in s]
    assert len(insert_stmts_4280) == 5, f"Expected 5 INSERT statements for 4280 rows, got {len(insert_stmts_4280)}"
    tuple_count_4280 = sql_4280.count("(\n    'Customer ")
    assert tuple_count_4280 == 4280
    print("  -> 4,280 rows: 5 multi-row INSERT batches (1000*4 + 280) PASSED")


def test_complex_scenarios():
    print("\nTesting complex scenarios (Empty table, Foreign keys, Identity, Rejected rows)...")
    schema_sql = """
    CREATE TABLE [dbo].[Branch] (
        [Branch_ID] INT IDENTITY(1,1) NOT NULL,
        [Branch_Name] VARCHAR(100) NOT NULL,
        CONSTRAINT [PK_Branch] PRIMARY KEY CLUSTERED ([Branch_ID] ASC)
    );
    CREATE TABLE [dbo].[Customer] (
        [Customer_ID] BIGINT IDENTITY(1,1) NOT NULL,
        [Branch_ID] INT NOT NULL,
        [Customer_Name] VARCHAR(100) NOT NULL,
        [Notes] VARCHAR(500) NULL,
        CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([Customer_ID] ASC)
    );
    CREATE TABLE [dbo].[EmptyTable] (
        [ID] INT NOT NULL,
        [Data] VARCHAR(50) NULL,
        CONSTRAINT [PK_Empty] PRIMARY KEY CLUSTERED ([ID] ASC)
    );
    ALTER TABLE [dbo].[Customer] WITH CHECK ADD CONSTRAINT [FK_Customer_Branch] FOREIGN KEY([Branch_ID]) REFERENCES [dbo].[Branch] ([Branch_ID]);
    """
    schema = parse_sql_schema(schema_sql)

    # 14. Empty table: EmptyTable has 0 valid rows
    val_result = {
        "valid_tables": ["Branch", "Customer"], # EmptyTable not even valid, or valid with 0 rows
        "invalid_tables": {},
        "table_data": {
            "Branch": {
                "valid_rows": [
                    {"b_name": "Koramangala"},
                    {"b_name": "Indiranagar"},
                    {"b_name": "Whitefield"},
                ],
                "rejected_rows": []
            },
            "Customer": {
                "valid_rows": [
                    {"b_id": 1, "c_name": "John's Diner", "notes": None},
                    {"b_id": 2, "c_name": "Alice & Bob", "notes": "VIP"},
                ],
                # 7. Table containing rejected rows
                "rejected_rows": [
                    {"row_index": 3, "data": {"b_id": 999, "c_name": "Invalid FK"}, "errors": ["FK violation"]}
                ],
                "fk_errors": [
                    {"row_index": 3, "reason": "Branch_ID 999 does not exist in parent Branch"}
                ]
            },
            "EmptyTable": {
                "valid_rows": [],
                "rejected_rows": []
            }
        }
    }

    table_mappings = {
        "branch": {"b_name": "Branch_Name"},
        "customer": {"b_id": "Branch_ID", "c_name": "Customer_Name", "notes": "Notes"},
        "emptytable": {"id": "ID", "data": "Data"}
    }

    sql = DeterministicSQLGenerator.generate_sql(
        validated_result=val_result,
        schema_info=schema,
        table_mappings=table_mappings,
        dialect="tsql",
        use_transaction=True
    )

    # 15. Dependency ordering: Branch before Customer
    pos_branch = sql.find("INSERT INTO [dbo].[Branch]")
    pos_customer = sql.find("INSERT INTO [dbo].[Customer]")
    assert pos_branch != -1 and pos_customer != -1
    assert pos_branch < pos_customer, "Parent table [Branch] must be inserted BEFORE child table [Customer]"
    print("  -> Foreign key dependency ordering verified (Branch before Customer)")

    # 14. Empty table check
    assert "INSERT INTO [dbo].[EmptyTable]" not in sql, "Empty table must NOT generate any INSERT statement"
    print("  -> Empty table skipped successfully")

    # 8. IDENTITY column check
    assert "INSERT INTO [dbo].[Branch]\n(\n    [Branch_Name]\n)" in sql, "Branch INSERT must omit Branch_ID identity column"
    assert "[Customer_ID]" not in sql, "Customer_ID IDENTITY column omitted from INSERT column list"
    print("  -> IDENTITY columns omitted successfully")

    # 9. String escaping check
    assert "'John''s Diner'" in sql, "Apostrophe in 'John''s Diner' escaped properly"
    print("  -> String escaping ('John''s Diner') verified")

    # 10. NULL handling check
    assert "NULL" in sql, "NULL value properly emitted as NULL"
    assert "'NULL'" not in sql, "'NULL' string literal must not exist for null values"
    print("  -> NULL handling verified")

    # Transaction check
    assert sql.startswith("-- ===========================================\n-- VALID INSERTS\n-- ===========================================\n\nBEGIN TRANSACTION;")
    assert "COMMIT;" in sql
    print("  -> Transaction block (BEGIN TRANSACTION; ... COMMIT;) verified")

    # Rule 10: Clean SQL by default (validation errors omitted)
    assert "VALIDATION ERRORS" not in sql
    print("  -> Clean SQL output verified (no validation errors block by default)")

    # Optional validation report when include_validation_report=True
    sql_with_report = DeterministicSQLGenerator.generate_sql(
        validated_result=val_result,
        schema_info=schema,
        table_mappings=table_mappings,
        dialect="tsql",
        use_transaction=True,
        include_validation_report=True
    )
    assert "FOREIGN KEY ERROR: Row 3 - Branch_ID 999 does not exist in parent Branch" in sql_with_report
    print("  -> Optional validation error section verified when explicitly requested")

    print("\n>>> ALL TESTS PASSED SUCCESSFULLY! <<<\n")


if __name__ == "__main__":
    test_value_formatting()
    test_batching_scenarios()
    test_complex_scenarios()
