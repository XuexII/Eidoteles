"""
引擎的核心类型定义。
所有数据结构都位于此处。不包含异步、不包含 I/O——仅包含类型和验证逻辑。

- capability.py
    - 表示权限和能力单元
- conversation.py
    - 对话界面——UI 层，与执行层分离
- error.py
    - 定义引擎错误类型
- event.py
     - 用于事件溯源
- memory.py
    - 表示持久化知识对象
- message.py
    - 表示线程中的消息
- mission.py
    - 表示长期目标和触发器
- project.py
    - 表示上下文容器，组织相关的线程、任务和内存文档
- provenance.py
    - 用于数据来源追踪
- step.py
    - 表示单次执行轮次
- thread.py
    - 表示有状态执行单元
"""
