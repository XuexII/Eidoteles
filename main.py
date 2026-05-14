from smolagents import OpenAIModel, GradioUI, MessageRole
import config
from utils import load_yaml
from sql_tools import all_sql_tools, FinalAnswerTool
from agent import SqlAgent
import os

model_id = config.model_id
base_url = config.base_url
api_key = config.api_key
headers = config.headers

dataset_info = [
    {
        "table": "employees",
        "description": "员工信息表",
        "column": {
            "员工编号": "员工的编号",
            "姓名": "员工姓名",
            "部门": "员工所在部门",
            "薪资": "员工每月的薪资"
        }
    }
]

gen_args = {
    "max_tokens": 10000,
    "temperature": 0.1,
    "top_p": 0.1,
    "n": 1,
    # "reasoning_effort": "low",
    "extra_body": {
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking": {"type": "disabled"}  # enabled
    }
}

prompt_templates = load_yaml("prompts/toolcalling_agent_v1.3.yaml")
tools = all_sql_tools

custom_role_conversions = {
    MessageRole.TOOL_RESPONSE: "tool"
}

model = OpenAIModel(
    model_id=model_id,
    api_base=base_url,
    api_key=api_key,
    client_kwargs={"default_headers": headers},
    custom_role_conversions=custom_role_conversions,
    **gen_args
)

agent = SqlAgent(
    tools=tools,
    model=model,
    prompt_templates=prompt_templates,
    planning_interval=100,  # 每多少步更新一次计划
    dataset_info=dataset_info
)

agent.tools["final_answer"] = FinalAnswerTool()

query = "帮我看一下我每个月公共要支付多少钱的工资给员工"

agent.run(query)



# import gradio as gr
#
# with gr.Blocks(title="电商Text2SQL智能分析助手",theme=gr.themes.Soft()) as demo:
#     gr.Markdown("# 电商数据分析助手")
#     gr.Markdown("用文字输入需求，获取SQL查询、图表与分析报告。")
#
#     with gr.Row():
#         with gr.Column(scale=3):
#             chatbot = gr.Chatbot(
#                 label="对话历史",
#                 height=285,
#                 avatar_images=(None, "")
#             )
#             with gr.Row():
#                 gr.Markdown(
#                     "<div style='line-height: 38px; font-weight: bold;'>输入问题</div>",
#                 )
#                 msg = gr.Textbox(placeholder="例如：上周销售额最高的5个商品是哪些？", scale=5, show_label=False)
#                 send_btn = gr.Button("发送", variant="primary", scale=5)
#
#         with gr.Column(scale=2):
#             with gr.Tabs():
#                 with gr.TabItem("图表"):
#                     plot_display = gr.Plot(label="可视化结果")
#                 with gr.TabItem("数据"):
#                     data_table = gr.Dataframe(label="查询结果", interactive=False)
#                 with gr.TabItem("分析报告"):
#                     report_display = gr.Markdown("等待查询结果...")
#
#             with gr.Accordion("查看生成的SQL", open=False):
#                 sql_display = gr.Code(label="SQL语句", language="sql", interactive=False)
#
#     demo.launch()