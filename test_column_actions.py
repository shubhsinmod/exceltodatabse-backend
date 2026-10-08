"""
test_column_actions.py
-----------------------
Unit tests verifying user-controlled Action options per target SQL column:
1. Excel (Mapped from Excel column)
2. NULL (Emits literal SQL NULL when allowed)
3. Custom Value (Fixed value applied to every row)
4. Database Default (Emits SQL DEFAULT)
5. Database Generated / Identity (Omitted from INSERT)
"""

import unittest
import os
import sys

# Ensure backend path is configured
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.schema_parser import parse_sql_schema
from app.services.validation_engine import DeterministicValidationEngine
from app.services.sql_generator import DeterministicSQLGenerator


class TestColumnActions(unittest.TestCase):

    def test_user_action_options_sql_generation(self):
        """
        Test user actions:
        - Order_Outlet_ID -> Custom Value -> 1001
        - Order_Status -> Custom Value -> Success
        - Tax -> Custom Value -> 18.50
        - Order_DateTime -> Excel -> order_time
        - Customer_ID -> NULL
        - Quantity -> NULL
        - Order_ID -> Identity / Database Generated
        """
        schema_text = """
        CREATE TABLE [dbo].[Order] (
            [Order_ID] INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            [Customer_ID] INT NULL,
            [Quantity] INT NULL,
            [Order_DateTime] DATETIME NOT NULL,
            [Order_Outlet_ID] INT NOT NULL,
            [Tax] DECIMAL(10,2) NULL,
            [Order_Status] VARCHAR(50) NOT NULL
        );
        """
        parsed = parse_sql_schema(schema_text)

        # 2 Excel rows with different dates
        raw_rows = [
            {"order_time": "2026-06-30 00:00:00"},
            {"order_time": "2026-06-30 00:02:00"}
        ]

        table_mappings = {
            "order": {
                "order_time": "Order_DateTime"
            }
        }

        # User-configured actions per column:
        column_actions = [
            {"table": "Order", "sql_column": "Order_ID", "action": "DATABASE_GENERATED"},
            {"table": "Order", "sql_column": "Customer_ID", "action": "NULL"},
            {"table": "Order", "sql_column": "Quantity", "action": "NULL"},
            {"table": "Order", "sql_column": "Order_DateTime", "action": "EXCEL", "excel_source": "order_time", "raw_data_key": "order_time"},
            {"table": "Order", "sql_column": "Order_Outlet_ID", "action": "CUSTOM_VALUE", "custom_value": "1001"},
            {"table": "Order", "sql_column": "Tax", "action": "CUSTOM_VALUE", "custom_value": "18.50"},
            {"table": "Order", "sql_column": "Order_Status", "action": "CUSTOM_VALUE", "custom_value": "Success"},
        ]

        # 1. Validation Engine recognizes custom values for NOT NULL columns
        val_res = DeterministicValidationEngine.validate_dataset(
            raw_rows=raw_rows,
            schema_info=parsed,
            table_mappings=table_mappings,
            column_actions=column_actions
        )
        self.assertIn("Order", val_res["valid_tables"])
        self.assertEqual(len(val_res["table_data"]["Order"]["valid_rows"]), 2)

        # 2. SQL Generation produces exact multi-row INSERT with user-selected actions
        sql = DeterministicSQLGenerator.generate_sql(
            validated_result=val_res,
            schema_info=parsed,
            table_mappings=table_mappings,
            dialect="tsql",
            column_actions=column_actions
        )

        # A. Identity column omitted
        self.assertNotIn("[Order_ID]", sql)

        # B. All other columns present in INSERT in schema order
        expected_cols = (
            "INSERT INTO [dbo].[Order]\n"
            "(\n"
            "    [Customer_ID],\n"
            "    [Quantity],\n"
            "    [Order_DateTime],\n"
            "    [Order_Outlet_ID],\n"
            "    [Tax],\n"
            "    [Order_Status]\n"
            ")"
        )
        self.assertIn(expected_cols, sql)

        # C. Custom values applied across both rows
        # Row 1: NULL, NULL, '2026-06-30', 1001, 18.50, 'Success'
        # Row 2: NULL, NULL, '2026-06-30 00:02:00', 1001, 18.50, 'Success'
        self.assertIn("1001", sql)
        self.assertIn("18.50", sql)
        self.assertIn("'Success'", sql)
        self.assertIn("'2026-06-30'", sql)
        self.assertIn("'2026-06-30 00:02:00'", sql)

        # Both rows contain 1001
        self.assertEqual(sql.count("    1001"), 2)
        # Both rows contain 'Success'
        self.assertEqual(sql.count("    'Success'"), 2)

        # D. Clean SQL without validation error block
        self.assertNotIn("VALIDATION ERRORS", sql)

    def test_database_default_action(self):
        """Test action DEFAULT produces SQL keyword DEFAULT."""
        schema_text = """
        CREATE TABLE [dbo].[AuditLog] (
            [Log_ID] INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            [Action_Name] VARCHAR(100) NOT NULL,
            [Log_Date] DATETIME NOT NULL DEFAULT (getdate())
        );
        """
        parsed = parse_sql_schema(schema_text)
        raw_rows = [{"action": "LOGIN"}]
        table_mappings = {"auditlog": {"action": "Action_Name"}}
        column_actions = [
            {"table": "AuditLog", "sql_column": "Log_ID", "action": "DATABASE_GENERATED"},
            {"table": "AuditLog", "sql_column": "Action_Name", "action": "EXCEL", "excel_source": "action", "raw_data_key": "action"},
            {"table": "AuditLog", "sql_column": "Log_Date", "action": "DEFAULT"},
        ]

        val_res = DeterministicValidationEngine.validate_dataset(
            raw_rows=raw_rows,
            schema_info=parsed,
            table_mappings=table_mappings,
            column_actions=column_actions
        )
        self.assertIn("AuditLog", val_res["valid_tables"])

        sql = DeterministicSQLGenerator.generate_sql(
            validated_result=val_res,
            schema_info=parsed,
            table_mappings=table_mappings,
            dialect="tsql",
            column_actions=column_actions
        )

        self.assertIn("DEFAULT", sql)
        self.assertIn("'LOGIN'", sql)
        self.assertIn("[Action_Name]", sql)
        self.assertIn("[Log_Date]", sql)

    def test_user_override_auto_mapping_with_custom_value_69(self):
        """
        Verify Requirement 7: User selection MUST override automatic mapping.
        If automatic mapping previously suggested:
            Order_Outlet_ID -> Excel column: OutletID
        And the user changes:
            Action: Custom Value
            Value: 69
        The final generated SQL MUST use 69.
        """
        schema_text = """
        CREATE TABLE [dbo].[Orders] (
            [Order_ID] BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            [Order_Date] DATE NOT NULL,
            [Order_Outlet_ID] INT NOT NULL,
            [Notes] VARCHAR(100) NULL,
            [Created_At] DATETIME NOT NULL DEFAULT (getdate())
        );
        """
        parsed = parse_sql_schema(schema_text)
        raw_rows = [
            {"date": "2026-06-30", "outlet_id": "999"},
            {"date": "2026-07-01", "outlet_id": "888"}
        ]
        # Previous auto-match had outlet_id mapped to Order_Outlet_ID
        table_mappings = {"orders": {"date": "Order_Date", "outlet_id": "Order_Outlet_ID"}}

        # User overrides Order_Outlet_ID to CUSTOM_VALUE 69:
        column_actions = [
            {"table": "Orders", "sql_column": "Order_ID", "action": "DATABASE_GENERATED"},
            {"table": "Orders", "sql_column": "Order_Date", "action": "EXCEL", "excel_column": "date", "raw_data_key": "date"},
            {"table": "Orders", "sql_column": "Order_Outlet_ID", "action": "CUSTOM_VALUE", "custom_value": "69", "excel_column": None},
            {"table": "Orders", "sql_column": "Notes", "action": "NULL"},
            {"table": "Orders", "sql_column": "Created_At", "action": "DATABASE_DEFAULT"},
        ]

        val_res = DeterministicValidationEngine.validate_dataset(
            raw_rows=raw_rows,
            schema_info=parsed,
            table_mappings=table_mappings,
            column_actions=column_actions
        )
        self.assertIn("Orders", val_res["valid_tables"])

        sql = DeterministicSQLGenerator.generate_sql(
            validated_result=val_res,
            schema_info=parsed,
            table_mappings=table_mappings,
            dialect="tsql",
            column_actions=column_actions
        )

        # 1. Identity Order_ID omitted
        self.assertNotIn("[Order_ID]", sql)

        # 2. Both rows have 69 instead of 999 or 888
        self.assertNotIn("999", sql)
        self.assertNotIn("888", sql)
        self.assertEqual(sql.count("    69"), 2)

        # 3. Mapped date column present
        self.assertIn("'2026-06-30'", sql)
        self.assertIn("'2026-07-01'", sql)

        # 4. Null column is NULL
        self.assertEqual(sql.count("    NULL"), 2)

        # 5. Database Default column is DEFAULT
        self.assertEqual(sql.count("    DEFAULT"), 2)


if __name__ == "__main__":
    unittest.main()

