import logging
import math
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, create_engine, text, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    joinedload,
    mapped_column,
    relationship,
    sessionmaker,
)
import pandas as pd


# ============================================================
# 1. 配置数据库连接（这里用 SQLite，可替换为其他数据库）
# ============================================================
# SQLite 示例（文件型数据库，无需安装服务）
DATABASE_URL = "sqlite:///./datasets/example.db"

# MySQL 示例（需先安装 pymysql：pip install pymysql）
# DATABASE_URL = "mysql+pymysql://用户名:密码@主机:端口/数据库名?charset=utf8mb4"

# PostgreSQL 示例（需先安装 psycopg2）
# DATABASE_URL = "postgresql+psycopg2://用户名:密码@主机:端口/数据库名"

engine = create_engine(DATABASE_URL, echo=False)  # echo=True 可打印执行的 SQL
SessionLocal = sessionmaker(bind=engine)


# ============================================================
# 2. 从 Excel 导入数据到数据库表
# ============================================================
def import_excel_to_table(excel_path: str, table_name: str, if_exists: str = "replace"):
    """
    将 Excel 文件导入为数据库表

    参数:
        excel_path: Excel 文件路径
        table_name: 目标表名
        if_exists: 表存在时的处理方式 {'fail', 'replace', 'append'}
    """
    # 读取 Excel 所有列，pandas 会自动推断数据类型
    df = pd.read_excel(excel_path)

    # 可选：手动处理列名（去除空格、特殊符号等）
    df.columns = [col.strip().replace(" ", "_") for col in df.columns]

    # 写入数据库，index=False 不写入 DataFrame 的行索引
    with engine.begin() as conn:
        df.to_sql(
            name=table_name,
            con=conn,
            if_exists=if_exists,
            index=False,
            method="multi"  # 批量插入，提升性能
        )
    print(f"导入成功：{excel_path} -> 表 [{table_name}]，共 {len(df)} 行")


# ============================================================
# 3. 执行 SQL 查询并返回 DataFrame
# ============================================================
def query_as_dataframe(sql: str, params: dict = None) -> pd.DataFrame:
    """
    执行原生 SQL 查询，返回 pandas DataFrame

    参数:
        sql: SQL 语句，可用 :param 占位符
        params: 参数字典，安全传递（防注入）
    """
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        # 将结果转为 DataFrame
        df = pd.DataFrame(result.fetchall(), columns=result.keys())
    return df


# ============================================================
# 4. 列出数据库中所有表名（辅助查看）
# ============================================================
def list_tables():
    """打印当前数据库中的所有表"""
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print("数据库中的表:", tables)
    return tables


# ============================================================
# 5. 综合示例：从 Excel 导入 -> 执行 SQL 查询
# ============================================================
if __name__ == "__main__":
    # ----- 准备一个测试 Excel 文件（如果本地没有，可自动生成示例） -----
    sample_df = pd.DataFrame({
        "员工编号": [1001, 1002, 1003],
        "姓名": ["张三", "李四", "王五"],
        "部门": ["技术部", "市场部", "技术部"],
        "薪资": [15000, 12000, 18000]
    })
    excel_file = "datasets/sample_employees.xlsx"
    sample_df.to_excel(excel_file, index=False)
    print(f"已生成示例 Excel: {excel_file}")

    # ----- 步骤1: 导入 Excel 到数据库 -----
    import_excel_to_table(excel_file, table_name="employees", if_exists="replace")

    # ----- 步骤2: 查看数据库中有哪些表 -----
    list_tables()

    # ----- 步骤3: 执行查询 -----
    # 简单查询
    sql1 = "SELECT * FROM employees"
    df1 = query_as_dataframe(sql1)
    print("\n所有员工：\n", df1)

    # 带参数查询（防止 SQL 注入）
    sql2 = "SELECT 姓名, 薪资 FROM employees WHERE 部门 = :dept"
    df2 = query_as_dataframe(sql2, {"dept": "技术部"})
    print("\n技术部员工薪资：\n", df2)

    # 聚合查询
    sql3 = "SELECT 部门, AVG(薪资) as 平均薪资 FROM employees GROUP BY 部门"
    df3 = query_as_dataframe(sql3)
    print("\n各部门平均薪资：\n", df3)