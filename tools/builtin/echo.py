import time
from tools.tool import Tool, ToolOutput

class EchoTool:
    """简单的回显工具，用于测试。"""

    def name(self) -> str:
        return "echo"

    def description(self) -> str:
        return "回显输入消息。用于测试工具执行。"

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "要回显的消息",
                }
            },
            "required": ["message"],
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行回显操作。

        Args:
            params: 包含 `message` 字段的参数字典
            ctx: 作业上下文

        Returns:
            包含回显消息的工具输出
        """
        start = time.monotonic()
        message = require_str(params, "message")
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.text(message, duration_ms)

    def requires_sanitization(self) -> bool:
        # 内部工具，无外部数据
        return False