# 由 ClawHub 公共注册表支持的运行时技能目录。
#
# 在运行时从 ClawHub API（`/api/v1/search`）获取技能列表，并将结果缓存在内存中。
# 没有编译时条目——目录始终与注册表保持同步。
#
# 配置：
# - `CLAWHUB_REGISTRY` 环境变量可覆盖默认的基础 URL