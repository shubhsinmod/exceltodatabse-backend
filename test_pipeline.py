import asyncio
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.services.import_service import ImportService
from app.services.schema_parser import parse_sql_schema
import uuid
import pandas as pd

import_id_str = "fc66a6b1-b308-4892-b333-302274a4e0e7"

sql_script_schema = """
CREATE TABLE [dbo].[Customer](
    [Customer_ID] [bigint] IDENTITY(1,1) NOT NULL,
    [Customer_Name] [varchar](100) NOT NULL,
    CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([Customer_ID] ASC)
)
GO
CREATE TABLE [dbo].[Orders](
    [Order_ID] [bigint] IDENTITY(1,1) NOT NULL,
    [Customer_ID] [bigint] NULL,
    CONSTRAINT [PK_Orders] PRIMARY KEY CLUSTERED ([Order_ID] ASC)
)
GO
ALTER TABLE [dbo].[Orders] WITH CHECK ADD CONSTRAINT [FK_Orders_Customer] FOREIGN KEY([Customer_ID]) REFERENCES [dbo].[Customer] ([Customer_ID])
"""

def mock_read(*args, **kwargs):
    df = pd.DataFrame([
        {"customerid": 1, "customername": "Rahul", "orderid": 101},
        {"customerid": 2, "customername": "Amit", "orderid": 102},
    ])
    return df.to_dict(orient="records")

from app.services.excel_reader import ExcelReader
ExcelReader.read_cached_table = mock_read

db = SessionLocal()
try:
    uid = uuid.UUID(import_id_str)
    schema_info = parse_sql_schema(sql_script_schema)
    
    matched = [
        {"excel_column": "Customer ID", "raw_data_key": "customerid", "table": "Customer", "column": "Customer_ID"},
        {"excel_column": "Customer Name", "raw_data_key": "customername", "table": "Customer", "column": "Customer_Name"},
        {"excel_column": "Order ID", "raw_data_key": "orderid", "table": "Orders", "column": "Order_ID"},
        {"excel_column": "Customer ID", "raw_data_key": "customerid", "table": "Orders", "column": "Customer_ID"}
    ]
    
    sql = ImportService.generate_sql_from_matches(uid, matched, schema_info, db, include_identity=True)
    print("INCLUDE IDENTITY=TRUE")
    print(sql)
    
    sql2 = ImportService.generate_sql_from_matches(uid, matched, schema_info, db, include_identity=False)
    print("INCLUDE IDENTITY=FALSE")
    print(sql2)
finally:
    db.close()
