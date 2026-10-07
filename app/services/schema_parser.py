import sqlparse
import re
import json

def parse_sql_schema(content: str, is_json: bool = False):
    if is_json:
        try:
            return json.loads(content)
        except Exception:
            return []

    parsed = sqlparse.parse(content)
    tables = []
    
    for stmt in parsed:
        if stmt.get_type() == "CREATE":
            tokens = [t for t in stmt.tokens if not t.is_whitespace]
            table_name = None
            for i, token in enumerate(tokens):
                if token.value.upper() == "TABLE":
                    table_name = tokens[i+1].get_name()
                    break
            
            if table_name:
                cols_match = re.search(r'\((.*)\)', str(stmt), re.DOTALL)
                cols = []
                fks = []
                
                table_pks = []
                
                if cols_match:
                    raw_inner = cols_match.group(1)
                    paren_depth = 0
                    safe_chars = []
                    for c in raw_inner:
                        if c == '(': paren_depth += 1
                        elif c == ')': paren_depth -= 1
                        if c == ',' and paren_depth > 0:
                            safe_chars.append('__COMMA__')
                        else:
                            safe_chars.append(c)
                            
                    col_defs = "".join(safe_chars).split(',')
                    
                    for cdef in col_defs:
                        cdef = cdef.replace('__COMMA__', ',').strip()
                        cdef_upper = cdef.upper()
                        
                        if cdef_upper.startswith("PRIMARY KEY"):
                            pk_match = re.search(r'\((.*?)\)', cdef_upper)
                            if pk_match:
                                for pk_col in pk_match.group(1).split(','):
                                    table_pks.append(pk_col.strip().strip('`"').lower())
                        elif "FOREIGN KEY" in cdef_upper:
                            fk_match = re.search(r'REFERENCES\s+([a-zA-Z0-9_]+)', cdef_upper)
                            if fk_match:
                                fks.append(fk_match.group(1).lower())
                        elif "REFERENCES" in cdef_upper and not cdef_upper.startswith("CONSTRAINT"):
                            fk_match = re.search(r'REFERENCES\s+([a-zA-Z0-9_]+)', cdef_upper)
                            if fk_match:
                                fks.append(fk_match.group(1).lower())
                                
                        if cdef and not cdef_upper.startswith(('PRIMARY', 'FOREIGN', 'UNIQUE', 'CONSTRAINT')):
                            col_name = cdef.split()[0].strip('`"')
                            col_type = "string"
                            is_pk = "PRIMARY KEY" in cdef_upper or col_name.lower() in table_pks
                            is_auto = "AUTO_INCREMENT" in cdef_upper or "IDENTITY" in cdef_upper or "SERIAL" in cdef_upper
                            
                            if len(cdef.split()) > 1:
                                type_str = cdef.split()[1].upper()
                                if "INT" in type_str or "SERIAL" in type_str: col_type = "integer"
                                elif "FLOAT" in type_str or "DECIMAL" in type_str or "NUMERIC" in type_str: col_type = "float"
                                elif "DATE" in type_str or "TIME" in type_str: col_type = "date"
                                elif "BOOL" in type_str: col_type = "boolean"
                            
                            cols.append({
                                "name": col_name, 
                                "type": col_type,
                                "is_pk": is_pk,
                                "is_auto": is_auto
                            })
                
                for c in cols:
                    if c["name"].lower() in table_pks:
                        c["is_pk"] = True
                        
                if not cols:
                    cols = [{"name": "id", "type": "integer", "is_pk": True, "is_auto": True}]
                    
                tables.append({
                    "table_name": table_name.lower(),
                    "columns": cols,
                    "foreign_keys": fks
                })
                
    return tables
