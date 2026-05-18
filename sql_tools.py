import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
from smolagents.agent_types import handle_agent_input_types, handle_agent_output_types
from smolagents.tools import Tool
from sqlalchemy import create_engine, text

from agent import SQLOutput

plt.rcParams['font.sans-serif'] = ['STHeiti']  # 或 ['Hei']
plt.rcParams['axes.unicode_minus'] = False

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


class SQLTool(Tool):
    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        if not self.is_initialized:
            self.setup()

        # Handle the arguments might be passed as a single dictionary
        if len(args) == 1 and len(kwargs) == 0 and isinstance(args[0], dict):
            potential_kwargs = args[0]

            # If the dictionary keys match our input parameters, convert it to kwargs
            if all(key in self.inputs for key in potential_kwargs):
                args = ()
                kwargs = potential_kwargs

        if sanitize_inputs_outputs:
            args, kwargs = handle_agent_input_types(*args, **kwargs)
        outputs = self.forward(*args, **kwargs)
        if sanitize_inputs_outputs:
            outputs = handle_agent_output_types(outputs, self.output_type)
        return outputs


class ExecuteSQL(SQLTool):
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
            save_path = f"./cache/{now}.xlsx"
            os.makedirs("./cache", exist_ok=True)
            df.to_excel(save_path, index=False)
            result = df.to_string()
            result = f"查询结果如下:\n结果保存位置: {save_path}\n详细信息:\n{result}"
            result = ExecuteOutput(sql=sql, path=save_path, content=result)
        except Exception as e:
            result = f"执行SQL[{sql}]时报错: \n{e}"

        return result


class GenerateChart(SQLTool):
    name = "generate_chart"
    description = "根据输入的excel数据生成合适的图表并在前端展示图表，支持柱状图、折线图、饼图、散点图。"
    inputs = {
        "path": {
            'type': 'string',
            'description': 'SQL查询结果结果保存位置'
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

            x_items = df[x_axis_field]
            y_items = df[y_axis_field]

            if chart_type == "bar":
                ax.bar(x_items, y_items, color='skyblue')
                ax.set_xlabel(xlabel)
                ax.set_ylabel(ylabel)
                ax.set_title(title)
                plt.xticks(rotation=45, ha='right')  # 旋转x轴标签防止重叠

            elif chart_type == "line":
                ax.plot(x_items, y_items, marker='o', linestyle='-', color='orange')
                ax.set_xlabel(xlabel)
                ax.set_ylabel(ylabel)
                ax.set_title(title)
                plt.xticks(rotation=45, ha='right')
                plt.grid(True, linestyle='--', alpha=0.7)
            else:  # pie
                # 绘制饼图，自动计算百分比
                ax.pie(x_items, labels=y_items, autopct='%1.1f%%', startangle=90)
                ax.set_title(title)
                ax.axis('equal')  # 保证饼图为正圆

            plt.tight_layout()
            state = "图表已在前端展示，请继续执行下一步计划。"
            result = ChartOutput(state=state, fig=fig)
        except Exception as e:
            result = f"generate_chart执行时报错: \n{e}"
        return result


class FinalAnswerTool(SQLTool):
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

if __name__ == '__main__':
    sql = """SELECT 
    c.category_name,
    ROUND(AVG(r.rating), 2) AS avg_rating
FROM categories c
INNER JOIN products p ON c.category_id = p.category_id
INNER JOIN reviews r ON p.product_id = r.product_id
GROUP BY c.category_id, c.category_name;"""

    exc_sql = all_sql_tools[0]
    res = exc_sql(sql=sql)
    print(str(res))

    args = {"path": "./cache/20260518141051.xlsx", "chart_type": "bar", "x_axis_field": "category_name",
            "y_axis_field": "avg_rating", "xlabel": "商品类别", "ylabel": "平均评分", "title": "各商品平均评分"}
    all_sql_tools[1](**args)
