from agent import Agent
from llm import LLM

model_name = "model"
base_url = "***"
api_key = "api"
default_headers = {"Content-Type": "application/json",
                   "Authorization": "****"}

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
