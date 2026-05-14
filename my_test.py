import logging
logging.basicConfig(level="INFO")
# 假设已有变量 iteration, text
text = "模型输出"
logging.info(
    "LLM text response",
    extra={
        "iteration": 1,
        "len": len(text),
        "has_suggestions": "<suggestions>" in text,
        "response": text,   # Python 中直接输出字符串
    }
)