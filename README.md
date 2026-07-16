# 1. 设计

# 1. SafetyLayer
- 作用:
  - 在`handle_with_engine_inner`中，用户输入首先经过两层安全检查：
    - 检测是否包含系统文件访问路径、SQL 注入模式、加密私钥等;
    - 检测是否包含 API 密钥（sk-...）、GitHub token（ghp_...）等敏感信息
  - 在EffectBridgeAdapter中，对工具的调用结果进行清理。具体为`.sanitize_tool_output`


# 2. 实现
## Thread
Thread代表一个完整的执行任务，具有自己的生命周期、状态机、能力租约和资源消耗跟踪。由ThreadManager管理(创建、执行)
## Project

## ThreadMessage
线程中对话消息的基本单元

## Provenance
标记每条数据的来源

## Step
每个Step代表一次完整的LLM调用及其相关的工具/代码执行

## Mission
任务代表一个持续进行的目标，它会定期生成线程以推进工作

## MemoryDoc
持久化知识单元，用于存储结构化的知识

## ThreadEvent
用于记录Thread执行过程中的完整追踪信息，支持重放、调试和反思
doing



# 完全被 V2 原语替代的组件

| V1 | v2 | 说明 |
|----|----|----|
| Session | Thread | 会话概念被 Thread 统一，Thread 支持父子树结构和生命周期管理 |
| Job | Thread | 作业概念被 Thread 替代，Thread 作为工作单元具有能力租约 |
| Routine | Thread + Mission | 定时任务被 Thread 和 Mission 替代，Mission 管理长期目标和触发器 |
|Agentic Loop|        Step          |       代理循环迭代被 Step 替代，Step 表示一次 LLM 调用及其工具/代码执行                                       |
|    Tool     |      Capability            |                                              |
|      Skill   |         Capability         |                                              |
|    Hook     |           Capability       |                                              |
|      Extension       |       Capability                     |                                              |
|      Workspace <br/>memory<br/>blobs       |     MemoryDoc                       |                     工作空间内存块被 MemoryDoc 替代，MemoryDoc 是结构化的持久知识单元                         |
|            Flat workspace namespace                                |      Project                               |                            平坦的工作空间命名空间被 Project 替代，Project 提供上下文作用域                                                             |

组件	V2 中的实现方式
Database	通过 HybridStore 适配器，将引擎状态持久化到工作空间 2026-03-20-engine-v2-architecture.md:341-342
LlmProvider	通过 LlmBridgeAdapter 包装，转换 ThreadMessage ↔ ChatMessage 2026-03-20-engine-v2-architecture.md:339-340
ToolRegistry	通过 EffectBridgeAdapter 包装，路由工具调用到 WASM 沙箱工具 2026-03-20-engine-v2-architecture.md:340-341
SafetyLayer	在 EffectBridgeAdapter 边界应用，引擎是纯编排 2026-03-20-engine-v2-architecture.md:493-494
ChannelManager	通过 ConversationManager 桥接，路由通道消息到线程 2026-03-20-engine-v2-architecture.md:322-325




# 流程
Agent.run()
Agent.handle_message()
Agent.process_user_input()  # thread_ops