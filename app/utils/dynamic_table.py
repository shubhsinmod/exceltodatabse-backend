from sqlalchemy import Table, Column, String, Integer, Float, Boolean, DateTime, MetaData
from sqlalchemy.engine import Engine

def get_sqlalchemy_type(type_str: str):
    if type_str == "integer":
        return Integer
    elif type_str == "float":
        return Float
    elif type_str == "date":
        return DateTime
    elif type_str == "boolean":
        return Boolean
    else:
        return String

class DynamicTableManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.metadata = MetaData()

    def create_table_if_not_exists(self, table_name: str, columns_mapping: list):
        self.metadata.reflect(bind=self.engine)
        
        if table_name in self.metadata.tables:
            table = self.metadata.tables[table_name]
            existing_cols = {c.name for c in table.columns}
            
            from sqlalchemy import text
            with self.engine.begin() as conn:
                for col_map in columns_mapping:
                    if col_map.target_column not in existing_cols:
                        col_type = get_sqlalchemy_type(col_map.target_type)()
                        type_str = str(col_type.compile(self.engine.dialect))
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_map.target_column} {type_str}"))
            
            # Clear and re-reflect to get the updated schema
            self.metadata.clear()
            self.metadata.reflect(bind=self.engine)
            return self.metadata.tables[table_name]

        cols = []
        has_pk = False
        for col_map in columns_mapping:
            is_pk = col_map.is_primary_key
            if is_pk:
                has_pk = True
            
            cols.append(Column(
                col_map.target_column, 
                get_sqlalchemy_type(col_map.target_type), 
                primary_key=is_pk
            ))
            
        # Add a default auto-increment ID if no PK is defined in mapping
        if not has_pk:
            cols.insert(0, Column("id", Integer, primary_key=True, autoincrement=True))

        table = Table(table_name, self.metadata, *cols)
        table.create(self.engine)
        return table
