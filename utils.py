import re
import sys
from typing import Dict

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def load_yaml(path, encoding="utf-8") -> Dict:
    with open(path, "r", encoding=encoding) as f:
        return yaml.safe_load(f)


def load_toml(path) -> Dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def convert_mysql_date_to_sqlite(sql: str) -> str:
    """
        将 MySQL SQL 中的日期函数转换为 SQLite 等价写法。
        支持：
          - CURDATE()                 -> date('now')
          - CURDATE() - INTERVAL n MONTH -> date('now', '-n months')
          - DATE_FORMAT(expr, '%Y-%m-01') -> date(expr, 'start of month')
          - DATE_FORMAT(expr, '%Y-%m-%d') -> strftime('%Y-%m-%d', expr)
        """

    # 1. CURDATE() 直接替换
    sql = re.sub(r'\bCURDATE\(\)', "date('now')", sql)

    # 2. date('now') - INTERVAL n MONTH  → date('now', '-n months')
    sql = re.sub(
        r"date\('now'\)\s*-\s*INTERVAL\s+(\d+)\s+MONTH",
        r"date('now', '-\1 months')",
        sql
    )

    # 3. DATE_FORMAT(..., '%Y-%m-01') → date(..., 'start of month')
    #    使用非贪婪匹配第一个参数，直到遇到 ",'%Y-%m-01')"
    def replace_month_start(m):
        inner_expr = m.group(1)
        return f"date({inner_expr}, 'start of month')"

    sql = re.sub(
        r"DATE_FORMAT\((.+?),\s*'%Y-%m-01'\s*\)",
        replace_month_start,
        sql
    )

    # 4. 其他简单 DATE_FORMAT(expr, '%Y-%m-%d') → strftime('%Y-%m-%d', expr)
    #    (可按需扩展更多格式)
    sql = re.sub(
        r"DATE_FORMAT\((.+?),\s*'%Y-%m-%d'\s*\)",
        r"strftime('%Y-%m-%d', \1)",
        sql
    )

    return sql
