import pandas as pd
from smolagents.tools import Tool
from sqlalchemy import create_engine, text


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

    def forward(self, sql: str) -> str:

        # 检查是否是select语句
        if not sql.lower().startswith("select"):
            return f"仅支持执行SQL的SELECT语句，但你的SQL是: {sql}"

        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql))
                # 将结果转为 DataFrame
                result = pd.DataFrame(result.fetchall(), columns=result.keys())
        except Exception as e:
            result = f"执行SQL[{sql}]时报错: \n{e}"

        return result

{
    "name": "generate_chart",
    "description": "根据输入的二维数据表生成合适的图表，支持柱状图、折线图、饼图、散点图等常见类型",
    "arguments": {
        "type": "object",
        "properties": {
            "data_file": {
                "type": "string",
                "description": "要生成图表数据的文件路径，必须是xlsx文件",
            },
            "chart_type": {
                "type": "string",
                "description": "图表类型，bar为柱状图，line为折线图，pie为饼图，scatter为散点图，table为表格。",
                "enum": ["bar", "line", "pie", "scatter", "table"],
            },
            "x_axis_field": {
                "type": "string",
                "description": "用作X轴（或分类维度）的字段名，饼图对应扇区名称字段。",
            },
            "y_axis_field": {
                "type": "string",
                "description": "用作Y轴（或数值）的字段名，饼图对应数值字段。",
            },
            "title": {
            "type": "string",
            "description": "图表标题，默认为空。"
        }
        },
        "required": ["data", "chart_type", "x_axis_field", "y_axis_field"]
    }
}

class FinalAnswerTool(Tool):
    name = "final_answer"
    description = "将最终的分析报告发送给用户"
    inputs = {
        "report": {"type": "string", "description": "最终发送给用户的分析报告"}}
    output_type = "string"

    def forward(self, report: str) -> str:
        return report

all_sql_tools = [
    ExecuteSQL("sqlite:///./datasets/example.db"),
]

if __name__ == '__main__':
    database_url = "sqlite:///./datasets/example.db"

    tool = ExecuteSQL(database_url)

    sql = "SELECT * FROM employees"
    sql = "INSERT * FROM employees"
    res = tool(sql)
    print(res)
