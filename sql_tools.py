import pandas as pd
from smolagents.tools import Tool
from sqlalchemy import create_engine, text
from utils import convert_mysql_date_to_sqlite
from datetime import datetime
import json
from agent import SQLOutput
from dataclasses import dataclass
import matplotlib.pyplot as plt
from typing import Any


@dataclass
class ExecuteOutput(SQLOutput):
    sql: str
    path: str
    content: str

    def __str__(self):
        return self.content


@dataclass
class ChartOutput(SQLOutput):
    state: str
    fig: Any

    def __str__(self):
        return self.state


class ExecuteSQL(Tool):
    name = "execute_sql"
    description = "执行SQL的SELECT查询语句，并返回查询的结果。"
    inputs = {
        'sql': {
            'type': 'string',
            'description': '语法正确的SQL的SELECT查询语句'
        }
    }
    output_type = "string"

    def __init__(self, database_url, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine = create_engine(database_url, echo=False)

    def process(self, df: pd.DataFrame):

        return df

    def query_as_dataframe(self, sql: str) -> pd.DataFrame:
        """
        执行原生 SQL 查询，返回 pandas DataFrame

        参数:
            sql: SQL 语句，可用 :param 占位符
            params: 参数字典，安全传递（防注入）
        """
        with self.engine.connect() as conn:
            result = conn.execute(text(sql))
            # 将结果转为 DataFrame
            df = pd.DataFrame(result.fetchall(), columns=result.keys())

        df = self.process(df)
        return df

    def forward(self, sql: str) -> str | ExecuteOutput:

        # 检查是否是select语句
        if not sql.lower().startswith("select"):
            return f"仅支持执行SQL的SELECT语句，但你的SQL是: {sql}"

        try:
            df = self.query_as_dataframe(sql)
            now = datetime.now().strftime("%Y%m%d%H%M%S")
            save_path = f"cache/{now}.xlsx"
            df.to_excel(save_path, index=False)
            result = "\n".join([json.dumps(row, ensure_ascii=False) for row in df.to_dict("records")])
            result = f"查询结果如下:\n结果保存位置: {save_path}\n详细信息:\n{result}"
            result = ExecuteOutput(sql=sql, path=save_path, content=result)
        except Exception as e:
            result = f"执行SQL[{sql}]时报错: \n{e}"

        return result


class GenerateChart(Tool):
    name = "generate_chart"
    description = "根据输入的excel数据生成合适的图表，支持柱状图、折线图、饼图、散点图。执行成功后你将会收到: '执行成功'，否则你会收到报错信息。"
    inputs = {
        "path": {
            'type': 'string',
            'description': 'excel文件的路径'
        },
        'chart_type': {
            'type': 'string',
            'description': '图表类型，bar为柱状图，line为折线图，pie为饼图。'
        },
        "x_axis_field": {
            "type": "string",
            "description": "用作X轴（或分类维度）的字段名，饼图对应扇区名称字段。",
        },
        "y_axis_field": {
            "type": "string",
            "description": "用作Y轴（或数值）的字段名，饼图对应数值字段。",
        },
        "xlabel": {
            "type": "string",
            "description": "X轴的标签，默认为''",
            "nullable": True
        },
        "ylabel": {
            "type": "string",
            "description": "Y轴的标签，默认为''",
            "nullable": True
        },
        "title": {
            "type": "string",
            "description": "图表标题，默认为''",
            "nullable": True
        }
    }
    output_type = "string"

    def forward(self, path: str, chart_type, x_axis_field, y_axis_field, title="", xlabel="",
                ylabel="") -> str | ChartOutput:

        try:
            df = pd.read_excel(path)

            fig, ax = plt.subplots(figsize=(8, 5))

            if chart_type == "bar":
                ax.bar(df.iloc[:, 0], df.iloc[:, 1], color='skyblue')
                ax.set_xlabel(xlabel)
                ax.set_ylabel(ylabel)
                ax.set_title(title)
                plt.xticks(rotation=45, ha='right')  # 旋转x轴标签防止重叠

            elif chart_type == "line":
                ax.plot(df.iloc[:, 0], df.iloc[:, 1], marker='o', linestyle='-', color='orange')
                ax.set_xlabel(xlabel)
                ax.set_ylabel(ylabel)
                ax.set_title(title)
                plt.xticks(rotation=45, ha='right')
                plt.grid(True, linestyle='--', alpha=0.7)
            else:  # pie
                # 绘制饼图，自动计算百分比
                ax.pie(df.iloc[:, 1], labels=df.iloc[:, 0], autopct='%1.1f%%', startangle=90)
                ax.set_title(title)
                ax.axis('equal')  # 保证饼图为正圆

            plt.tight_layout()
            result = ChartOutput(state="执行成功", fig=fig)
        except Exception as e:
            result = f"generate_chart执行时报错: \n{e}"
        return result


class FinalAnswerTool(Tool):
    name = "final_answer"
    description = "将最终的分析报告发送给用户"
    inputs = {
        "report": {"type": "string", "description": "最终发送给用户的分析报告"}}
    output_type = "string"

    def forward(self, report: str) -> str:
        return report


all_sql_tools = [
    ExecuteSQL("mysql+pymysql://root:@127.0.0.1:3306/mydatabase"),
    GenerateChart()
]

