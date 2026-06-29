# 引擎 v2 桥接层——将 `ironclaw_engine` 连接到现有基础设施。
#
# 策略 C：并行部署。当 `ENGINE_V2=true` 时，用户消息将通过引擎路由，而非现有的智能体循环。
# 当该标志关闭时，所有现有行为保持不变。

# v1和v2的区别
# 维度	v1 Agentic Loop	Engine v2 (handle_with_engine)
# 架构模型	Session/Thread/Turn + 多 Delegate	Thread/Step/Capability 五个原始类型
# 执行引擎	run_agentic_loop() + LoopDelegate	ExecutionLoop + Python Orchestrator
# 工具执行	每次 LLM 调用只能执行一个工具	CodeAct：LLM 编写 Python 代码组合工具
# Gate 模型	独立的批准和认证流程	统一的 gate 模型（暂停/恢复）
# 持久化	SQL 数据库（sessions、messages）	工作区文件系统（.system/engine/）
# 上下文管理	Token 窗口内的消息历史	RLM 模式：上下文作为 Python 变量
from .router import handle_with_engine