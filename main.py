from smolagents import OpenAIModel, GradioUI, MessageRole
import config
from utils import load_yaml
from sql_tools import all_sql_tools, FinalAnswerTool
from agent import SqlAgent

model_id = config.model_id
base_url = config.base_url
api_key = "api"
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
    "extra_body": {
        "chat_template_kwargs": {"enable_thinking": True}
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
