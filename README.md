# Eidoteles

## 使用记忆

### 1. 会话恢复时重建消息
在handle_message的self.maybe_hydrate_thread中使用

### 2. 构建对话上下文
agent.session.Thread.messages

### 3. 压缩上下文时使用


## 存储记忆

### 1. 文档提取后保存
保存到文件中
agent_loop的store_extracted_documents

### 2. llm结果保存和工具执行结果保存
保存到数据库
在thread_ops的persist_assistant_response

### 3. Agent主动写入
通过调用工具src/tools/builtin/memory.rs写入文件
