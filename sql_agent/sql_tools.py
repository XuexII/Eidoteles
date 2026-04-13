from tools import Tool
import sqlite3
from dotenv import load_dotenv
import os


class ExecuteSql(Tool):
    name = "execute_sql"
    description = "执行一条SQL查询，并返回查询的结果"
    inputs = {
        'sql': {
            'type': 'string',
            'description': '要查询的SELECT语句'
        }
    }
    required = ["sql"]

    def __init__(self):

        load_dotenv()
        self.db_url = os.getenv("DB_URL", None)


    def forward(self, sql: str):
        # 安全检查：禁止非SELECT语句
        if sql and not sql.upper().startswith("SELECT"):
            return "仅支持SELECT查询，生成的非查询语句"

        try:
            conn = sqlite3.connect(self.db_url)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            result = cursor.fetchall()
            return result
        except Exception as e:
            return f"执行sql查询过程报错: {e}"

