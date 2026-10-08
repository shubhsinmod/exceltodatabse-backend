import unittest
import os
import sys

# Ensure backend path is configured
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.schema_parser import parse_sql_schema
from app.services.llm_service import (
    DynamicValueAnalyzer,
    DynamicTypeCompatibility,
    DynamicSemanticScorer,
    LLMMappingService,
)
from app.services.validation_engine import DeterministicValidationEngine
from app.services.sql_generator import DeterministicSQLGenerator


class TestDynamicSQLFirstMapping(unittest.TestCase):

    def test_dynamic_value_analyzer(self):
        """Verify dynamic type inference strictly from actual sample values without column names."""
        # Email detection
        emails = ["user1@example.com", "admin@company.org", "test.account+tag@domain.co.in"]
        prof_email = DynamicValueAnalyzer.analyze_column_values(emails)
        self.assertEqual(prof_email["inferred_type"], "email-like")
        self.assertTrue(prof_email["is_email_like"])

        # Phone detection
        phones = ["+1 (555) 123-4567", "+91 9876543210", "123-456-7890"]
        prof_phone = DynamicValueAnalyzer.analyze_column_values(phones)
        self.assertEqual(prof_phone["inferred_type"], "phone-like")
        self.assertTrue(prof_phone["is_phone_like"])

        # Integer detection
        ints = ["101", "102", "103", "104"]
        prof_int = DynamicValueAnalyzer.analyze_column_values(ints)
        self.assertEqual(prof_int["inferred_type"], "integer")
        self.assertTrue(prof_int["is_integer"])

        # Decimal detection
        decimals = ["19.99", "150.50", "0.75", "1200.00"]
        prof_dec = DynamicValueAnalyzer.analyze_column_values(decimals)
        self.assertEqual(prof_dec["inferred_type"], "decimal")
        self.assertTrue(prof_dec["is_decimal"])

        # Date & Datetime detection
        dates = ["2026-06-30", "2026-07-01", "2026-07-02"]
        prof_date = DynamicValueAnalyzer.analyze_column_values(dates)
        self.assertEqual(prof_date["inferred_type"], "date")

        datetimes = ["2026-06-30 14:30:00", "2026-07-01 09:15:20"]
        prof_dt = DynamicValueAnalyzer.analyze_column_values(datetimes)
        self.assertEqual(prof_dt["inferred_type"], "datetime")

        # Boolean detection
        bools = ["True", "False", "True"]
        prof_bool = DynamicValueAnalyzer.analyze_column_values(bools)
        self.assertEqual(prof_bool["inferred_type"], "boolean")

    def test_dynamic_type_compatibility(self):
        """Verify type compatibility: text cannot map to numeric PKs or integers."""
        # Case A: Integer PK vs text name
        sql_pk_meta = {"data_type": "integer", "is_pk": True, "is_identity": True}
        text_profile = {"inferred_type": "string", "is_integer": False, "is_numeric": False}
        is_compat, reason = DynamicTypeCompatibility.check_compatibility(sql_pk_meta, text_profile)
        self.assertFalse(is_compat)

        # Case B: Integer PK vs integer values
        int_profile = {"inferred_type": "integer", "is_integer": True, "is_numeric": True}
        is_compat, reason = DynamicTypeCompatibility.check_compatibility(sql_pk_meta, int_profile)
        self.assertTrue(is_compat)

        # Case C: Decimal SQL column vs email-like
        sql_dec_meta = {"data_type": "float", "is_pk": False, "is_identity": False}
        email_profile = {"inferred_type": "email-like", "is_numeric": False}
        is_compat, reason = DynamicTypeCompatibility.check_compatibility(sql_dec_meta, email_profile)
        self.assertFalse(is_compat)

    def test_generic_sql_column_protection(self):
        """Generic SQL columns (Column1, Column2) cannot act as sinks for unrelated text without exact evidence."""
        sql_meta = {"data_type": "string", "is_pk": False, "is_identity": False, "nullable": True}
        text_profile = {"inferred_type": "string", "is_numeric": False, "min_length": 5, "max_length": 20}
        
        # Unrelated text column must be rejected (score = 0.0)
        score, reason = DynamicSemanticScorer.score_candidate("TestTable", "Column1", sql_meta, "Customer_Address", text_profile)
        self.assertEqual(score, 0.0)

        # Direct name match allowed
        score_exact, reason_exact = DynamicSemanticScorer.score_candidate("TestTable", "Column1", sql_meta, "Column1", text_profile)
        self.assertGreaterEqual(score_exact, 0.95)

    def test_customer_schema_sql_first_mapping(self):
        """Test with schema containing Customer & Bills tables."""
        customer_schema = """
        CREATE TABLE [dbo].[Customer] (
            [Customer_ID] INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            [Customer_Name] VARCHAR(100) NOT NULL,
            [Mobile_Number] VARCHAR(20) NULL,
            [Email_ID] VARCHAR(100) NULL,
            [Address] VARCHAR(255) NULL,
            [Date_Added] DATETIME NULL DEFAULT (getdate()),
            [Comments] VARCHAR(500) NULL,
            [Column1] VARCHAR(50) NULL,
            [Column2] VARCHAR(50) NULL
        );
        """
        parsed = parse_sql_schema(customer_schema)
        
        # Excel columns providing some fields but omitting others, and containing an extra unrelated column
        excel_cols = ["Customer_Name", "Mobile_Number", "Email_ID", "Unrelated_Extra_Notes"]
        sample_rows = [
            {
                "Customer_Name": "Alice Johnson",
                "Mobile_Number": "+1 555-0199",
                "Email_ID": "alice@example.com",
                "Unrelated_Extra_Notes": "Special handling requested"
            },
            {
                "Customer_Name": "Bob Smith",
                "Mobile_Number": "+1 555-0288",
                "Email_ID": "bob@example.com",
                "Unrelated_Extra_Notes": "VIP customer"
            }
        ]

        results = LLMMappingService.get_semantic_mapping(
            schema_info=parsed,
            excel_columns=excel_cols,
            sample_rows=sample_rows,
            selected_tables=["Customer"]
        )

        table_plans = results["table_mappings"]["Customer"]
        plan_dict = {p["sql_column"]: p for p in table_plans}

        # 1. Customer_Name mapped
        self.assertEqual(plan_dict["Customer_Name"]["action"], "IMPORT")
        self.assertEqual(plan_dict["Customer_Name"]["excel_source"], "Customer_Name")

        # 2. Mobile_Number mapped
        self.assertEqual(plan_dict["Mobile_Number"]["action"], "IMPORT")
        self.assertEqual(plan_dict["Mobile_Number"]["excel_source"], "Mobile_Number")

        # 3. Email_ID mapped
        self.assertEqual(plan_dict["Email_ID"]["action"], "IMPORT")
        self.assertEqual(plan_dict["Email_ID"]["excel_source"], "Email_ID")

        # 4. Customer_ID has IDENTITY constraint -> DATABASE_GENERATED
        self.assertEqual(plan_dict["Customer_ID"]["action"], "DATABASE_GENERATED")
        self.assertIsNone(plan_dict["Customer_ID"]["excel_source"])

        # 5. Date_Added has DEFAULT constraint -> DEFAULT
        self.assertEqual(plan_dict["Date_Added"]["action"], "DEFAULT")
        self.assertIsNone(plan_dict["Date_Added"]["excel_source"])

        # 6. Address & Comments are NULLABLE without source -> NULL
        self.assertEqual(plan_dict["Address"]["action"], "NULL")
        self.assertEqual(plan_dict["Comments"]["action"], "NULL")

        # 7. Generic columns Column1 & Column2 NOT matched to Unrelated_Extra_Notes
        self.assertIn(plan_dict["Column1"]["action"], ("NULL", "DEFAULT"))
        self.assertIsNone(plan_dict["Column1"]["excel_source"])

        # 8. Unrelated_Extra_Notes remains NOT_USED
        unused_names = [u["excel_column"] for u in results["unused_excel_columns"]]
        self.assertIn("Unrelated_Extra_Notes", unused_names)

    def test_completely_different_schema_portability(self):
        """
        Prove ZERO hardcoding by executing the exact same code against a completely
        different schema (e.g. Healthcare: Hospital / Patient / Ward / Medication).
        """
        hospital_schema = """
        CREATE TABLE [dbo].[Patient] (
            [Patient_UUID] INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            [Full_Legal_Name] NVARCHAR(120) NOT NULL,
            [Contact_Phone] VARCHAR(30) NULL,
            [Date_Of_Birth] DATE NOT NULL,
            [Insurance_Provider] VARCHAR(100) NULL,
            [Emergency_Contact] VARCHAR(100) NULL,
            [Record_Created_At] DATETIME NULL DEFAULT (getdate()),
            [Generic_Field_1] VARCHAR(50) NULL
        );
        """
        parsed_hospital = parse_sql_schema(hospital_schema)

        # Excel file with patient information
        excel_cols = ["Full_Legal_Name", "Contact_Phone", "Date_Of_Birth", "Dietary_Preferences"]
        sample_rows = [
            {
                "Full_Legal_Name": "Eleanor Rigby",
                "Contact_Phone": "+44 20 7946 0991",
                "Date_Of_Birth": "1985-04-12",
                "Dietary_Preferences": "Vegetarian"
            },
            {
                "Full_Legal_Name": "Jude Harrison",
                "Contact_Phone": "+44 20 7946 0992",
                "Date_Of_Birth": "1992-11-23",
                "Dietary_Preferences": "Gluten-Free"
            }
        ]

        results = LLMMappingService.get_semantic_mapping(
            schema_info=parsed_hospital,
            excel_columns=excel_cols,
            sample_rows=sample_rows,
            selected_tables=["Patient"]
        )

        patient_plans = results["table_mappings"]["Patient"]
        plan_dict = {p["sql_column"]: p for p in patient_plans}

        # 1. Full_Legal_Name mapped
        self.assertEqual(plan_dict["Full_Legal_Name"]["action"], "IMPORT")
        self.assertEqual(plan_dict["Full_Legal_Name"]["excel_source"], "Full_Legal_Name")

        # 2. Contact_Phone mapped
        self.assertEqual(plan_dict["Contact_Phone"]["action"], "IMPORT")
        self.assertEqual(plan_dict["Contact_Phone"]["excel_source"], "Contact_Phone")

        # 3. Date_Of_Birth mapped
        self.assertEqual(plan_dict["Date_Of_Birth"]["action"], "IMPORT")
        self.assertEqual(plan_dict["Date_Of_Birth"]["excel_source"], "Date_Of_Birth")

        # 4. Patient_UUID identity -> DATABASE_GENERATED
        self.assertEqual(plan_dict["Patient_UUID"]["action"], "DATABASE_GENERATED")

        # 5. Record_Created_At default -> DEFAULT
        self.assertEqual(plan_dict["Record_Created_At"]["action"], "DEFAULT")

        # 6. Insurance_Provider & Emergency_Contact nullable -> NULL
        self.assertEqual(plan_dict["Insurance_Provider"]["action"], "NULL")
        self.assertEqual(plan_dict["Emergency_Contact"]["action"], "NULL")

        # 7. Generic_Field_1 NOT matched to Dietary_Preferences
        self.assertEqual(plan_dict["Generic_Field_1"]["action"], "NULL")
        self.assertIsNone(plan_dict["Generic_Field_1"]["excel_source"])

        # 8. Dietary_Preferences remains NOT_USED
        unused_names = [u["excel_column"] for u in results["unused_excel_columns"]]
        self.assertIn("Dietary_Preferences", unused_names)

    def test_required_column_validation_error(self):
        """When a required NOT NULL column without default/identity has no source, action = VALIDATION_ERROR."""
        schema_text = """
        CREATE TABLE [dbo].[Inventory] (
            [Item_Code] VARCHAR(50) NOT NULL PRIMARY KEY,
            [Item_Description] VARCHAR(200) NOT NULL,
            [Unit_Cost] DECIMAL(10,2) NOT NULL,
            [Reorder_Level] INT NULL
        );
        """
        parsed = parse_sql_schema(schema_text)
        # Excel only provides Item_Code and Item_Description, omitting required Unit_Cost
        excel_cols = ["Item_Code", "Item_Description"]
        sample_rows = [{"Item_Code": "ITM-01", "Item_Description": "Widget A"}]

        results = LLMMappingService.get_semantic_mapping(
            schema_info=parsed,
            excel_columns=excel_cols,
            sample_rows=sample_rows,
            selected_tables=["Inventory"]
        )

        inv_plans = {p["sql_column"]: p for p in results["table_mappings"]["Inventory"]}
        self.assertEqual(inv_plans["Item_Code"]["action"], "IMPORT")
        self.assertEqual(inv_plans["Item_Description"]["action"], "IMPORT")
        # Unit_Cost is NOT NULL with no default and no identity -> VALIDATION_ERROR
        self.assertEqual(inv_plans["Unit_Cost"]["action"], "VALIDATION_ERROR")
        # Reorder_Level is nullable -> NULL
        self.assertEqual(inv_plans["Reorder_Level"]["action"], "NULL")

    def test_generate_sql_for_selected_table_filling_rules(self):
        """
        Verify SQL generation is driven by the selected SQL table, filling applicable
        columns from Excel/schema rules (IMPORT, DATABASE_GENERATED, DEFAULT, NULL).
        """
        schema_text = """
        CREATE TABLE [dbo].[Customer] (
            [Customer_ID] INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            [Customer_Name] VARCHAR(100) NOT NULL,
            [Mobile_Number] VARCHAR(20) NULL,
            [Date_Added] DATETIME NULL DEFAULT (getdate()),
            [Address] VARCHAR(255) NULL
        );
        """
        parsed = parse_sql_schema(schema_text)
        
        # Mappings where Customer_Name and Mobile_Number are filled from Excel,
        # Customer_ID is DATABASE_GENERATED, Date_Added is DEFAULT, Address is NULL.
        table_mappings = {
            "customer": {
                "customer_name": "Customer_Name",
                "mobile_number": "Mobile_Number"
            }
        }
        valid_rows = [
            {"customer_name": "Alice Johnson", "mobile_number": "555-1234"},
            {"customer_name": "Bob Smith", "mobile_number": "555-5678"}
        ]
        val_res = {
            "valid_tables": ["Customer"],
            "invalid_tables": {},
            "table_data": {
                "Customer": {
                    "valid_rows": valid_rows,
                    "rejected_rows": []
                }
            }
        }
        
        sql = DeterministicSQLGenerator.generate_sql(
            validated_result=val_res,
            schema_info=parsed,
            table_mappings=table_mappings,
            dialect="tsql"
        )
        
        # 1. Target table is selected
        self.assertIn("INSERT INTO [dbo].[Customer]", sql)
        # 2. All table columns (mapped and unmapped) are in the INSERT column list in schema order!
        self.assertIn("[Customer_Name]", sql)
        self.assertIn("[Mobile_Number]", sql)
        self.assertIn("[Date_Added]", sql)
        self.assertIn("[Address]", sql)
        # 3. Identity column (without explicit value) is omitted for DB to generate
        self.assertNotIn("[Customer_ID]", sql)
        # 4. Values are filled from Excel rows
        self.assertIn("'Alice Johnson'", sql)
        self.assertIn("'Bob Smith'", sql)
        # 5. Unmapped columns receive literal NULL in VALUES list
        self.assertIn("NULL", sql)
        # 6. Column filling rules are documented in SQL comment header
        self.assertIn("-- TARGET SQL TABLE: [dbo].[Customer]", sql)
        self.assertIn("--   Customer_Name -> EXCEL [customer_name] (Mapped)", sql)
        self.assertIn("--   Customer_ID -> DATABASE_GENERATED", sql)
        self.assertIn("--   Address -> NULL (Unmapped nullable)", sql)
        # 7. Rule 10: Validation error comments block omitted
        self.assertNotIn("VALIDATION ERRORS", sql)

    def test_complete_target_table_structure_10_columns(self):
        """
        Verify exact requirement:
        Target table has 10 columns, Excel contains data for only 3 columns.
        Generated SQL MUST contain:
        - All 10 insertable columns in schema order in the INSERT statement.
        - Actual Excel values for the 3 mapped columns.
        - Literal unquoted NULL for unmapped nullable columns.
        - Zero fake/default invented values.
        - NO large VALIDATION ERRORS block (Rule 10).
        """
        schema_text = """
        CREATE TABLE [dbo].[TargetTable] (
            [Column1] VARCHAR(50) NOT NULL,
            [Column2] VARCHAR(50) NOT NULL,
            [Column3] VARCHAR(50) NOT NULL,
            [Column4] VARCHAR(50) NULL,
            [Column5] VARCHAR(50) NULL,
            [Column6] VARCHAR(50) NULL,
            [Column7] VARCHAR(50) NULL,
            [Column8] VARCHAR(50) NULL,
            [Column9] VARCHAR(50) NULL,
            [Column10] VARCHAR(50) NULL
        );
        """
        parsed = parse_sql_schema(schema_text)
        table_mappings = {
            "targettable": {
                "col1": "Column1",
                "col2": "Column2",
                "col3": "Column3"
            }
        }
        valid_rows = [
            {"col1": "Excel Value 1", "col2": "Excel Value 2", "col3": "Excel Value 3"}
        ]
        val_res = {
            "valid_tables": ["TargetTable"],
            "invalid_tables": {},
            "table_data": {
                "TargetTable": {
                    "valid_rows": valid_rows,
                    "rejected_rows": []
                }
            }
        }

        sql = DeterministicSQLGenerator.generate_sql(
            validated_result=val_res,
            schema_info=parsed,
            table_mappings=table_mappings,
            dialect="tsql"
        )

        # INSERT statement has all 10 columns in exact schema order
        expected_insert_cols = (
            "INSERT INTO [dbo].[TargetTable]\n"
            "(\n"
            "    [Column1],\n"
            "    [Column2],\n"
            "    [Column3],\n"
            "    [Column4],\n"
            "    [Column5],\n"
            "    [Column6],\n"
            "    [Column7],\n"
            "    [Column8],\n"
            "    [Column9],\n"
            "    [Column10]\n"
            ")"
        )
        self.assertIn(expected_insert_cols, sql)

        # VALUES statement has mapped values and 7 NULLs in matching positions
        expected_values = (
            "VALUES\n"
            "(\n"
            "    'Excel Value 1',\n"
            "    'Excel Value 2',\n"
            "    'Excel Value 3',\n"
            "    NULL,\n"
            "    NULL,\n"
            "    NULL,\n"
            "    NULL,\n"
            "    NULL,\n"
            "    NULL,\n"
            "    NULL\n"
            ");"
        )
        self.assertIn(expected_values, sql)

        # Rule 10: Clean SQL without VALIDATION ERRORS block
        self.assertNotIn("VALIDATION ERRORS", sql)
        self.assertNotIn("TABLES CANNOT BE GENERATED", sql)


if __name__ == "__main__":
    unittest.main()
