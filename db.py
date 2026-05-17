import os

import pandas as pd
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import (
    sessionmaker,
)
from tqdm import tqdm

# ============================================================
# 1. 配置数据库连接（这里用 SQLite，可替换为其他数据库）
# ============================================================
# SQLite 示例（文件型数据库，无需安装服务）
# DATABASE_URL = "sqlite:///./datasets/e-commerce.db"

# MySQL 示例（需先安装 pymysql：pip install pymysql）
# DATABASE_URL = "mysql+pymysql://用户名:密码@主机:端口/数据库名?charset=utf8mb4"
DATABASE_URL = "mysql+pymysql://root:@127.0.0.1:3306/mydatabase"


engine = create_engine(DATABASE_URL, echo=False)  # echo=True 可打印执行的 SQL
SessionLocal = sessionmaker(bind=engine)


# ============================================================
# 2. 从 Excel 导入数据到数据库表
# ============================================================
def import_excel_to_table(df, table_name: str, if_exists: str = "replace"):
    """
    将 Excel 文件导入为数据库表

    参数:
        excel_path: Excel 文件路径
        table_name: 目标表名
        if_exists: 表存在时的处理方式 {'fail', 'replace', 'append'}
    """
    # # 读取 Excel 所有列，pandas 会自动推断数据类型
    # df = pd.read_excel(excel_path)

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
    print(f"导入成功：表 [{table_name}]，共 {len(df)} 行")


date_col_map = {
    "user": ["register_date"],
    "products": ["launch_date"],
    "inventory": ["update_time"],
    "orders": ["order_date"],
    "reviews": ["review_date"]
}


def import_excel_to_table_batch(dir_path):
    dir_items = os.listdir(dir_path)
    for file in tqdm(dir_items, total=len(dir_items)):
        if file.startswith(".") or not file.endswith(".csv"):
            continue
        file_name, _ = os.path.splitext(file)
        file_path = os.path.join(dir_path, file)
        parse_dates = date_col_map.get(file_name, [])
        try:
            df = pd.read_csv(file_path, parse_dates=parse_dates)
        except Exception as e:
            df = pd.read_csv(file_path, parse_dates=parse_dates, encoding='gbk')
        import_excel_to_table(df, file_name)


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
    # # ----- 步骤1: 导入 Excel 到数据库 -----

    dir_path = "datasets"
    import_excel_to_table_batch(dir_path)

    # ----- 步骤2: 查看数据库中有哪些表 -----
    # list_tables()

    # ----- 步骤3: 执行查询 -----
    # 简单查询
    sql1 = """SELECT product_name, price
FROM products
WHERE product_id = '101';"""

    df1 = query_as_dataframe(sql1)
    print("\n所有员工：\n", df1)

    # 带参数查询（防止 SQL 注入）
    sql2 = """SELECT order_date, total_amount
FROM orders
WHERE order_date >= DATE_FORMAT(CURDATE() - INTERVAL 1 MONTH, '%Y-%m-01')
  AND order_date < DATE_FORMAT(CURDATE(), '%Y-%m-01');"""
    df2 = query_as_dataframe(sql2)
    print("\n技术部员工薪资：\n", df2)

    # 聚合查询
    sql3 = """SELECT 
    c.category_name,
    ROUND(AVG(r.rating), 2) AS avg_rating
FROM categories c
INNER JOIN products p ON c.category_id = p.category_id
INNER JOIN reviews r ON p.product_id = r.product_id
GROUP BY c.category_id, c.category_name;"""
    df3 = query_as_dataframe(sql3)
    print("\n各部门平均薪资：\n", df3)

    sql4 = """SELECT 
    u.city,
    SUM(total_amount) AS total_sales
FROM orders o
JOIN user u
ON o.user_id=u.user_id
WHERE u.city IN ('北京', '上海')
  AND order_date >= MAKEDATE(YEAR(CURDATE()), 1) + INTERVAL (QUARTER(CURDATE()) - 2) * 3 MONTH
  AND order_date < MAKEDATE(YEAR(CURDATE()), 1) + INTERVAL (QUARTER(CURDATE()) - 1) * 3 MONTH
GROUP BY u.city;"""

    df4 = query_as_dataframe(sql4)
    print("\nsq4：\n", df4)

    sql5 = """SELECT 
    p.product_name,
    SUM(i.stock_quantity) AS total_stock
FROM categories c
INNER JOIN products p ON c.category_id = p.category_id
INNER JOIN inventory i ON p.product_id = i.product_id
WHERE c.category_name in ('手机')
GROUP BY p.product_name;"""
    df5 = query_as_dataframe(sql5)
    print("\nsq5：\n", df5)

    sql6 = """SELECT order_date,SUM(total_amount) AS total_sales
    FROM orders
    WHERE order_date >= DATE_FORMAT(CURDATE() - INTERVAL 1 MONTH, '%Y-%m-01')
      AND order_date < DATE_FORMAT(CURDATE(), '%Y-%m-01')
    GROUP BY order_date
    ORDER BY order_date;"""

    df6 = query_as_dataframe(sql6)
    print("\nsq4：\n", df6)
