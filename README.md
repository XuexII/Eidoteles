# 1. 设计

# 1. SafetyLayer
- 作用:
  - 在`handle_with_engine_inner`中，用户输入首先经过两层安全检查：
    - 检测是否包含系统文件访问路径、SQL 注入模式、加密私钥等;
    - 检测是否包含 API 密钥（sk-...）、GitHub token（ghp_...）等敏感信息
  - 在EffectBridgeAdapter中，对工具的调用结果进行清理。具体为`.sanitize_tool_output`