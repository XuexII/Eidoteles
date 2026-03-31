from pydantic import BaseModel, Field, ConfigDict

class HookEvent:

    class Inbound(BaseModel):
        """
        一条即将被处理的入站用户消息
        """
        pass
    class ToolCall(BaseModel):
        """
        一个即将被执行的工具调用。
        """
        pass
    class Outbound(BaseModel):
        """
        一个即将被发送的出站响应。
        """
        pass
    class SessionStart(BaseModel):
        """
        一个新会话已创建。
        """
        pass
    class SessionEnd(BaseModel):
        """
        一个会话已结束（被修剪）。
        """
        pass

    class ResponseTransform(BaseModel):
        """
        最终响应在轮次完成前正在被转换。
        """
        pass

