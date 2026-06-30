"""
能力管理。
- registry.py: CapabilityRegistry — 存储已知能力及其动作
- lease.py: LeaseManager — 授予、验证和过期能力租约
- policy.py: PolicyEngine — 确定性效果级允许/拒绝/批准
- planner.py: LeasePlanner — 租约规划器
"""
from .lease import LeaseManager
from .policy import PolicyDecision, PolicyEngine
from .registry import CapabilityRegistry
