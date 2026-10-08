import unittest
import os
import sys

# Add backend directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.schema_parser import parse_sql_schema
from app.services.llm_service import (
    LLMMappingService,
    DynamicTypeCompatibility,
    DynamicSemanticScorer,
    DynamicValueAnalyzer
)
from app.services.validation_engine import DeterministicValidationEngine
from app.services.sql_generator import DeterministicSQLGenerator

class TestTargetTableSelection(unittest.TestCase):

    def setUp(self):
        # Generic multi-table schema for testing
        self.sql_schema = """
        CREATE TABLE [dbo].[Customer] (
            [Customer_ID] INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            [Customer_Name] VARCHAR(100) NOT NULL,
            [Phone] VARCHAR(20) NULL
        );

        CREATE TABLE [dbo].[Bills] (
            [Bill_ID] INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            [Customer_ID] INT NOT NULL,
            [Bill_Number] VARCHAR(50) NOT NULL,
            [Total_Amount] DECIMAL(10,2) NOT NULL,
            [OtherCharges] DECIMAL(10,2) NULL,
            CONSTRAINT FK_Bills_Customer FOREIGN KEY (Customer_ID) REFERENCES Customer(Customer_ID)
        );

        CREATE TABLE [dbo].[Payment] (
            [Payment_ID] INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            [Bill_ID] INT NOT NULL,
            [Amount_Paid] DECIMAL(10,2) NOT NULL,
            [Payment_Method] VARCHAR(50) NULL,
            CONSTRAINT FK_Payment_Bills FOREIGN KEY (Bill_ID) REFERENCES Bills(Bill_ID)
        );
        """
        self.parsed_schema = parse_sql_schema(self.sql_schema)

    def test_schema_parser_metadata(self):
        """Verify schema parser extracts related_tables, declared_fks, identity, and PKs correctly."""
        table_map = {t["table_name"]: t for t in self.parsed_schema}
        self.assertIn("Customer", table_map)
        self.assertIn("Bills", table_map)
        self.assertIn("Payment", table_map)

        # Bills references Customer
        bills = table_map["Bills"]
        self.assertEqual(bills["primary_key"], "Bill_ID")
        self.assertTrue(bills["has_identity"])
        self.assertIn("Customer", bills["related_tables"])
        self.assertEqual(len(bills["declared_fks"]), 1)
        self.assertEqual(bills["declared_fks"][0]["parent_table"], "Customer")

    def test_single_target_table_selection(self):
        """Test 1: Only selected table (Bills) participates in mapping."""
        excel_cols = ["Bill_Number", "Customer_Name", "Total_Amount"]
        sample_rows = [
            {"Bill_Number": "B-101", "Customer_Name": "Alice", "Total_Amount": 150.0},
            {"Bill_Number": "B-102", "Customer_Name": "Bob", "Total_Amount": 200.0}
        ]
        
        selected_tables = ["Bills"]
        results = LLMMappingService.get_semantic_mapping(
            schema_info=self.parsed_schema,
            excel_columns=excel_cols,
            sample_rows=sample_rows,
            selected_tables=selected_tables
        )
        
        # Every matched table MUST strictly belong to selected_tables
        for m in results.get("mappings", []):
            if m.get("target_table"):
                self.assertIn(m["target_table"], selected_tables, f"Matched table {m['target_table']} was not in selected_tables")

    def test_multiple_target_tables_selection(self):
        """Test 2: Selected 'Bills' and 'Customer' both participate in mapping."""
        excel_cols = ["Bill_Number", "Customer_Name", "Total_Amount"]
        sample_rows = [
            {"Bill_Number": "B-101", "Customer_Name": "Alice", "Total_Amount": 150.0}
        ]
        
        selected_tables = ["Bills", "Customer"]
        results = LLMMappingService.get_semantic_mapping(
            schema_info=self.parsed_schema,
            excel_columns=excel_cols,
            sample_rows=sample_rows,
            selected_tables=selected_tables
        )
        
        matched_tables = {m["target_table"] for m in results.get("mappings", []) if m.get("target_table")}
        self.assertTrue(matched_tables.issubset(set(selected_tables)))
        self.assertIn("Customer", matched_tables)
        self.assertIn("Bills", matched_tables)
        # Payment was not selected and must not appear
        self.assertNotIn("Payment", matched_tables)

    def test_empty_or_invalid_table_selection(self):
        """Test 3 & 4: Empty selected_tables or invalid table name."""
        excel_cols = ["Bill_Number"]
        
        # Empty selected_tables raises ValueError
        with self.assertRaises(ValueError):
            LLMMappingService.get_semantic_mapping(
                schema_info=self.parsed_schema,
                excel_columns=excel_cols,
                sample_rows=[],
                selected_tables=[]
            )

        # Nonexistent table raises ValueError
        with self.assertRaises(ValueError):
            LLMMappingService.get_semantic_mapping(
                schema_info=self.parsed_schema,
                excel_columns=excel_cols,
                sample_rows=[],
                selected_tables=["NonExistentTable"]
            )

    def test_semantic_incompatibility_rules(self):
        """Test strict semantic filters: monetary values cannot map to ID/PK, text cannot map to numeric ID."""
        # 1. Monetary/decimal values cannot map to integer PK / identity
        pk_cand = {"column": "Bill_ID", "is_pk": True, "is_identity": True, "data_type": "integer"}
        money_profile = DynamicValueAnalyzer.analyze_column_values([10.50, 20.00, 35.75])
        is_compat, reason = DynamicTypeCompatibility.check_compatibility(pk_cand, money_profile)
        self.assertFalse(is_compat)

        # 2. Text column cannot map to integer ID / PK
        id_cand = {"column": "Customer_ID", "is_pk": True, "is_identity": True, "data_type": "integer"}
        text_profile = DynamicValueAnalyzer.analyze_column_values(["Alice", "Bob", "Charlie"])
        is_compat, reason = DynamicTypeCompatibility.check_compatibility(id_cand, text_profile)
        self.assertFalse(is_compat)

        # 3. Compatible integer to integer PK allowed
        int_profile = DynamicValueAnalyzer.analyze_column_values([1, 2, 3])
        is_compat, reason = DynamicTypeCompatibility.check_compatibility(id_cand, int_profile)
        self.assertTrue(is_compat)

    def test_identity_insert_handling(self):
        """Test explicit identity column in Excel produces SET IDENTITY_INSERT ON/OFF."""
        # Case A: Explicit Identity present
        valid_rows_with_id = [
            {"cust_id": 1, "cust_name": "Alice", "phone": "12345"},
            {"cust_id": 2, "cust_name": "Bob", "phone": "67890"}
        ]
        val_res_with_id = {
            "valid_tables": ["Customer"],
            "invalid_tables": {},
            "table_data": {
                "Customer": {
                    "valid_rows": valid_rows_with_id,
                    "rejected_rows": []
                }
            }
        }
        mappings_with_id = {
            "customer": {
                "cust_id": "Customer_ID",
                "cust_name": "Customer_Name",
                "phone": "Phone"
            }
        }
        sql_with_id = DeterministicSQLGenerator.generate_sql(
            validated_result=val_res_with_id,
            schema_info=self.parsed_schema,
            table_mappings=mappings_with_id,
            dialect="tsql"
        )
        self.assertIn("SET IDENTITY_INSERT [dbo].[Customer] ON;", sql_with_id)
        self.assertIn("SET IDENTITY_INSERT [dbo].[Customer] OFF;", sql_with_id)
        self.assertIn("[Customer_ID]", sql_with_id)

        # Case B: Identity column omitted (auto-generated by DB)
        valid_rows_without_id = [
            {"cust_name": "Alice", "phone": "12345"},
            {"cust_name": "Bob", "phone": "67890"}
        ]
        val_res_without_id = {
            "valid_tables": ["Customer"],
            "invalid_tables": {},
            "table_data": {
                "Customer": {
                    "valid_rows": valid_rows_without_id,
                    "rejected_rows": []
                }
            }
        }
        mappings_without_id = {
            "customer": {
                "cust_name": "Customer_Name",
                "phone": "Phone"
            }
        }
        sql_without_id = DeterministicSQLGenerator.generate_sql(
            validated_result=val_res_without_id,
            schema_info=self.parsed_schema,
            table_mappings=mappings_without_id,
            dialect="tsql"
        )
        self.assertNotIn("SET IDENTITY_INSERT", sql_without_id)
        self.assertNotIn("[Customer_ID]", sql_without_id)

    def test_topological_dependency_ordering(self):
        """Parent table (Customer) must be generated before Child table (Bills) and Grandchild (Payment)."""
        order = DeterministicValidationEngine.get_topological_order(self.parsed_schema)
        
        self.assertIn("Customer", order)
        self.assertIn("Bills", order)
        self.assertIn("Payment", order)
        
        cust_idx = order.index("Customer")
        bills_idx = order.index("Bills")
        pay_idx = order.index("Payment")
        
        self.assertLess(cust_idx, bills_idx, "Customer must precede Bills")
        self.assertLess(bills_idx, pay_idx, "Bills must precede Payment")

if __name__ == "__main__":
    unittest.main()
