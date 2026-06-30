# 受信内部写入作用域标记。
#
# 某些 Store 实现会对安全敏感文档（编排器代码、提示覆盖层）的 `save_memory_doc` 操作进行门控限制。
# 该门控会豁免*系统内部*写入——例如在项目引导期间植入编译好的编排器 v0——使其不受大语言模型写入规则的约束。
#
# 仅凭文档内容来区分“系统内部”与“大语言模型编写”是不安全的：大语言模型工具调用可以构造一个包含门控所检查的任何标记的有效载荷。
# 相反，受信写入的调用点在调用 `save_memory_doc` 之前会进入此任务本地作用域，而 Store 实现会读取 `is_trusted_internal_write_active()`。
# 该标志限定于当前异步任务，并且**不会**跨 `tokio::spawn` 传播，因此不可信代码无法继承它。
#
# ## 用法
#
# ```ignore
# use ironclaw_engine::runtime::internal_write::with_trusted_internal_writes;
#
# with_trusted_internal_writes(async {
#     store.save_memory_doc(&seed_doc).await?;
#     Ok(())
# }).await
# ```
#
# 在闭包内部，存储的门控会看到 `is_trusted_internal_write_active() == true`，
# 并允许写入，即使自我修改功能在其他情况下被禁用。