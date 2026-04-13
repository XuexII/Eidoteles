from agent import Agent
from llm import LLM

model_name = "model"
base_url = "http://1484537100187225.cn-hangzhou.pai-eas.aliyuncs.com/api/predict/xp_list_select_test/v1"
api_key = "api"
default_headers = {"Content-Type": "application/json",
                   "Authorization": "Njk1ZWU5NTA2Yzk1NjJhMTRmOTgxNmE5OWNiOWJiY2JhNDQ3MjJlMQ=="}

gen_args = {
    "max_tokens": 512,
    "temperature": 0.1,
    "top_p": 0.1,
    "n": 1,
    "extra_body": {
        "chat_template_kwargs": {"enable_thinking": True}
    }
}

llm = LLM(
    model_name,
    api_key,
    base_url,
    gen_kwargs=gen_args,
    default_headers=default_headers

)

agent = Agent(llm, )
