from schems.async_schems import RWLockList
from hooks.hook import Hook, HookContext, HookError, HookEvent, HookFailureMode, HookOutcome
from typing import Optional, List, Dict, Union



class HookRegistry:

    def __init__(self):
        self.hooks: RWLockList = RWLockList()


    async def register(self, hook: Hook):
        """
        以默认优先级（100）注册一个钩子
        """
        await self.register_with_priority(hook, 100)


    async def register_with_priority(self, hook: Hook, priority: int):
        """
        以指定优先级注册一个钩子

        优先级数字越小，执行顺序越靠前
        :param hook:
        :param priority:
        :return:
        """
        async with self.hooks.write:
            hook_name = hook.name

            # 在钩子列表中查找指定名称的钩子
            # 如果存在，替换为新的钩子和优先级（并记录警告）
            # 如果不存在，添加新钩子


    #     if let Some(existing) = hooks
    #         .iter_mut()
    #         .find(|entry| entry.hook.name() == hook_name)
    #     {
    #         tracing::warn!(
    #             hook = %hook_name,
    #             "Replacing existing hook registration with same name"
    #         );
    #         existing.hook = hook;
    #         existing.priority = priority;
    #     } else {
    #         hooks.push(HookEntry { hook, priority });
    #     }
    #
    #     hooks.sort_by_key(|e| e.priority);
    # }


    async def unregister(self, name: str) -> bool:
        """
        按名称注销一个钩子。若找到并成功移除则返回 true
        :param name:
        :return:
        """
        async with self.hooks.write:
            before = len(self.hooks)
            # 移除hook
            # hooks.retain(|e| e.hook.name() != name);
            return len(self.hooks) < before

    async def list(self) -> List[str]:
        """
        列出所有已注册的钩子名称（按优先级排序）
        :return:
        """
        async with self.hooks.read:
            return [entry.hook.name() for entry in self.hooks]

    /// Run all hooks matching the event's hook point.
    ///
    /// - Hooks run in priority order (lowest first).
    /// - `Reject` stops the chain immediately.
    /// - `Modify` chains the modification through subsequent hooks.
    /// - Timeout/error handling respects each hook's `failure_mode`.
    async def run(self, event: &HookEvent) -> Result<HookOutcome, HookError> {
        let point = event.hook_point();
        let ctx = HookContext::default();

        // Clone matching hooks and drop the read guard before executing.
        // Each hook can run up to its timeout, so holding the guard would
        // block concurrent register/unregister/run calls.
        let matching: Vec<Arc<dyn Hook>> = {
            let hooks = self.hooks.read().await;
            hooks
                .iter()
                .filter(|e| e.hook.hook_points().contains(&point))
                .map(|e| e.hook.clone())
                .collect()
        };

        if matching.is_empty() {
            return Ok(HookOutcome::ok());
        }

        let mut current_event = event.clone();

        for hook in &matching {
            let timeout = hook.timeout();

            let result = tokio::time::timeout(timeout, hook.execute(&current_event, &ctx)).await;

            match result {
                Ok(Ok(HookOutcome::Reject { reason })) => {
                    tracing::debug!(hook = hook.name(), "Hook rejected: {}", reason);
                    return Err(HookError::Rejected { reason });
                }
                Ok(Ok(HookOutcome::Continue {
                    modified: Some(value),
                })) => {
                    tracing::debug!(hook = hook.name(), "Hook modified content");
                    current_event.apply_modification(&value);
                }
                Ok(Ok(HookOutcome::Continue { modified: None })) => {
                    // No-op, continue chain
                }
                Ok(Err(err)) => match hook.failure_mode() {
                    HookFailureMode::FailOpen => {
                        tracing::warn!(hook = hook.name(), "Hook failed (fail-open): {}", err);
                    }
                    HookFailureMode::FailClosed => {
                        tracing::warn!(hook = hook.name(), "Hook failed (fail-closed): {}", err);
                        return Err(HookError::ExecutionFailed {
                            reason: format!("Hook '{}' failed: {}", hook.name(), err),
                        });
                    }
                },
                Err(_elapsed) => match hook.failure_mode() {
                    HookFailureMode::FailOpen => {
                        tracing::warn!(
                            hook = hook.name(),
                            "Hook timed out (fail-open) after {:?}",
                            timeout
                        );
                    }
                    HookFailureMode::FailClosed => {
                        tracing::warn!(
                            hook = hook.name(),
                            "Hook timed out (fail-closed) after {:?}",
                            timeout
                        );
                        return Err(HookError::Timeout { timeout });
                    }
                },
            }
        }

        // Determine final outcome by comparing with original event
        let modified = extract_content(&current_event);
        let original = extract_content(event);

        if modified != original {
            Ok(HookOutcome::modify(modified))
        } else {
            Ok(HookOutcome::ok())
        }
    }
