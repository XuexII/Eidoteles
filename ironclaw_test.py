from  channels.channel import IncomingMessage


"""
message处理步骤:
1. 接受用户消息;
2. 处理因需要用户干预(批准、授权)而暂停的任务;
3. 检查用户的输入是否合法，比如是否有操作系统文件的内容等等;
    3.1 此处可以考虑设计成`拒识模块`
4. 加载/创建 Project;
5. 处理并保存消息附件(如图片);
6. 向用户发送状态——"正在处理"

"""
# ----------Step1: 接受用户消息
# msg = IncomingMessage(
#     channel="gateway",
#     user_id="",
#     content="你是谁",
#     thread_id=None
# )

from gate.store import PendingGateStore
from gate.persistence import FileGatePersistence
pending_gates = PendingGateStore(
            FileGatePersistence.with_default_path()
        )
