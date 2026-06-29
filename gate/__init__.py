# 统一执行门控——应用层待处理状态管理。
#
# 引擎 crate（`ironclaw_engine::gate`）定义了 [`ExecutionGate`] 特质和评估管道。
# 本模块拥有**待处理状态存储**，该存储将门控暂停与面向用户的解析流程连接起来。
#
# [`ExecutionGate`]: ironclaw_engine::ExecutionGate