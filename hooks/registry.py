import asyncio
import json
import logging
from copy import deepcopy
from typing import List, Union

from hooks.hook import Hook, HookContext, HookError, HookEvent, HookFailureMode, HookOutcome
from schems.async_schems import RWLockList

logger = logging.getLogger(__name__)


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

    def __repr__(self):
        super().__repr__()

    async def run(self, event: HookEvent) -> Union[HookOutcome, HookError]:
        """
        运行所有匹配事件钩子点的钩子。

        - 钩子按优先级顺序运行（优先级数字最小的最先运行）。
        - `Reject` 会立即停止整个链的执行。
        - `Modify` 会将修改内容依次传递给后续钩子。
        - 超时/错误处理遵循每个钩子的 `failure_mode` 设置。
        """
        point = event.hook_point()
        ctx = HookContext.default()

        # 克隆匹配的钩子并在执行前释放读锁。
        # 每个钩子可能运行至其超时时间，若持有读锁则会阻塞并发的注册/注销/运行调用。
        matching = []
        async with  self.hooks.read:
            for entry in self.hooks:
                if point in entry.hook.hook_points():
                    matching.append(entry.hook)

        if len(matching) == 0:
            return HookOutcome.ok()

        current_event = deepcopy(event)
        for hook in matching:
            timeout = hook.timeout()

            try:
                result = await asyncio.wait_for(hook.execute(current_event, ctx), timeout=timeout)
                match result:
                    case HookOutcome.Reject.value(reason):
                        logger.debug(f"Hook {hook.name()} rejected: {reason}")
                        return HookError.Rejected(reason=reason)

                    case HookOutcome.Continue.value(modified=None):
                        # 无操作，继续执行链
                        pass
                    case HookOutcome.Continue.value(modified):
                        logger.debug(f"Hook {hook.name()} modified content")
                        current_event.apply_modification(modified)


            except asyncio.TimeoutError:

                # 对应 Rust: Err(_elapsed) -> 超时

                failure_mode = hook.failure_mode()

                match failure_mode:
                    case HookFailureMode.FailOpen:
                        logger.warning(f"钩子 {hook.name()} 在 {timeout} 秒后超时（采用故障开放策略）")
                    case HookFailureMode.FailClosed:
                        logger.warning(f"钩子 {hook.name()} 在 {timeout} 秒后超时（采用故障关闭策略）")
                        return HookError.Timeout(timeout=timeout)

            except Exception as err:
                failure_mode = hook.failure_mode()

                match failure_mode:
                    case HookFailureMode.FailOpen:
                        logger.warning(f"钩子 {hook.name()} 报错（采用故障开放策略): {err}")
                    case HookFailureMode.FailClosed:
                        logger.warning(f"钩子 {hook.name()} 报错（采用故障关闭策略): {err}")
                        return HookError.ExecutionFailed(reason=f"钩子 {hook.name()} 报错: {err}")

        # 通过对比原始事件来确定最终结果。
        modified = extract_content(current_event)
        original = extract_content(event)
        if modified != original:
            return HookOutcome.modify(modified=modified)
        return HookOutcome.ok()


def extract_content(event: HookEvent) -> str:
    """
    从钩子事件中提取主要内容字符串。
    :param event:
    :return:
    """
    match event:
        case HookEvent.Inbound.value(content) | HookEvent.Outbound.value(content=content):
            return content
        case HookEvent.ToolCall.value(parameters=parameters):
            return json.dumps(parameters, ensure_ascii=False)
        case HookEvent.ResponseTransform.value(response=response):
            return response
        case HookEvent.SessionStart.value(session_id=session_id) | HookEvent.SessionEnd.value(session_id=session_id):
            return session_id
