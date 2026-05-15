from smolagents import OpenAIModel, GradioUI, MessageRole
import config
from utils import load_yaml
from sql_tools import all_sql_tools, FinalAnswerTool
from agent import SqlAgent
from gradio_ui import GradioUI

model_id = config.model_id
base_url = config.base_url
api_key = config.api_key
headers = config.headers

dataset_info = [
    {
        "table": "user",
        "description": "用户信息表",
        "column": {
            "user_id": "用户的唯一id",
            "username": "用户的姓名",
            "register_date": "用户注册时间",
            "user_level": "会员等级",
            "city": "所在城市"
        }
    },
    {
        "table": "categories",
        "description": "商品类目信息表",
        "column": {
            "category_id": "类目的唯一id",
            "category_name": "类目的具体名称"
        }
    },
    {
        "table": "products",
        "description": "商品信息表",
        "column": {
            "product_id": "商品的唯一id",
            "product_name": "商品名称",
            "category_id": "商品所属的类目id",
            "brand": "品牌名称",
            "price": "商品价格",
            "launch_date": "首次上架销售时间"
        }
    },
    {
        "table": "inventory",
        "description": "商品库存信息表",
        "column": {
            "product_id": "产品的唯一id",
            "warehouse_id": "库存id",
            "stock_quantity": "库存数量",
            "update_time": "更新时间"
        }
    },
    {
        "table": "orders",
        "description": "销售订单信息表",
        "column": {
            "order_id": "订单唯一id",
            "user_id": "下单用户id",
            "order_date": "下单时间",
            "total_amount": "订单总金额",
            "payment_method": "支付方式",
            "status": "订单状态。可选值['已完成', '未完成']"
        }
    },
    {
        "table": "order_items",
        "description": "销售订单明细表",
        "column": {
            "item_id": "明细唯一id",
            "order_id": "所属订单id",
            "product_id": "购买的商品id",
            "quantity": "购买数量",
            "unit_price": "购买时的单价",
            "discount": "购买时的折扣"
        }
    },
    {
        "table": "reviews",
        "description": "商品评价/评论表，存储用户对购买过的商品发表的使用感受、评分等。",
        "column": {
            "review_id": "评论唯一id",
            "user_id": "发表评论的用户id",
            "product_id": "被评价的商品id",
            "rating": "评分",
            "comment": "评论正文",
            "review_date": "评论发表时间"
        }
    },
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

query = "上个月总销售额是多少"

# agent.run(query)
GradioUI(agent).launch()