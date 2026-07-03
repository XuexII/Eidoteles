from typing import Optional, List, Set
import logging

logger = logging.getLogger(__name__)


async def reconcile_dynamic_tool_lease(
        thread: Thread,
        effects: EffectExecutor,
        leases: LeaseManager,
        store: Optional[Store] = None,
        lease_planner: LeasePlanner = None,
) -> None:
    """协调动态工具租约

    租约协调仅读取 `available_actions`；此处不进行工具执行，
    因此惰性控制器就足够了
    """
    if lease_planner is None:
        lease_planner = LeasePlanner()

    active_leases = await leases.active_for_thread(thread.id)
    # 租约协调仅读取 `available_actions`；此处不进行工具执行，
    # 因此惰性控制器就足够了
    context = thread_execution_context(
        thread,
        StepId(),
        None,
        CancellingGateController(),
    )
    actions = await effects.available_actions(active_leases, context)
    if not actions:
        return

    # 构建能力注册表
    capabilities = CapabilityRegistry()
    capabilities.register(Capability(
        name="tools",
        description="可用工具",
        actions=actions,
        knowledge=[],
        policies=[],
    ))

    # 查找 "tools" 的授予计划
    grant = None
    for plan in lease_planner.plan_for_thread(thread.thread_type, capabilities):
        if plan.capability_name == "tools":
            grant = plan
            break

    if grant is None:
        return

    if isinstance(grant.granted_actions, All):
        return

    desired_actions: Set[str] = set(grant.granted_actions.actions)
    if not desired_actions:
        return

    # 检查是否已存在 "tools" 租约
    existing = None
    for lease in active_leases:
        if lease.capability_name == "tools":
            existing = lease
            break

    if existing is not None:
        # 已有租约 — 合并动作
        if isinstance(existing.granted_actions, All):
            return

        merged = set(existing.granted_actions.actions)
        before = len(merged)
        merged.update(desired_actions)
        if len(merged) == before:
            return

        merged_actions = sorted(merged)
        updated = await leases.update_granted_actions(
            existing.id, Specific(merged_actions),
        )
        if store is not None:
            await store.save_lease(updated)
        return

    # 创建新租约
    actions_list = sorted(desired_actions)
    lease = await leases.grant(
        thread.id,
        "tools",
        Specific(actions_list),
        None,
        None,
    )
    if store is not None:
        await store.save_lease(lease)
    if lease.id not in thread.capability_leases:
        thread.capability_leases.append(lease.id)