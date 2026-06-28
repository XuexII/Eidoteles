from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from ironclaw_host_api.runtime_policy import DeploymentMode, RuntimeProfile


# ── 注：以下类型在 Rust 代码中定义但未在此提供实现 ──
# DeploymentMode、RuntimeProfile、EffectiveRuntimePolicy、
# ConfigError、ResolveRequest、OrgPolicyConstraints、
# parse_optional_env、parse_bool_env_or_default、
# resolve、resolver_error
# 保持原名称不变，假设它们在外部已存在。


@dataclass
class RuntimeConfigOverrides:
    """
    CLI 提供的运行时策略覆盖。当 CLI 未指定值时，每个字段为 `None`；
    调用者随后可以回退到环境变量和默认值。
    CLI 层负责将用户输入解析为这些类型——
    `DeploymentMode` 和 `RuntimeProfile` 实现了针对其 snake_case 线名称的
    字符串解析。
    """
    deployment: Optional[DeploymentMode] = None
    profile: Optional[RuntimeProfile] = None
    yolo_disclosure_acknowledged: Optional[bool] = None


@dataclass
class RuntimeConfig:
    """经过解析的运行时配置，贯穿主机的其余部分。"""
    # 操作员在任何部署/策略收缩之前请求的内容
    deployment: DeploymentMode
    requested_profile: RuntimeProfile
    yolo_disclosure_acknowledged: bool
    # 由主机运行时规划器实际强制执行已解析策略
    effective_policy: EffectiveRuntimePolicy

    @classmethod
    def resolve_from(cls, overrides: RuntimeConfigOverrides) -> "RuntimeConfig":
        """
        从 CLI 覆盖 + 环境解析运行时配置。

        当解析器拒绝所请求的 `(deployment, profile)` 对时、
        当请求了 yolo 配置文件但未设置
        `IRONCLAW_YOLO_DISCLOSURE=true` / `--yolo-disclosure` 时，
        或当环境变量解析失败时，返回 `ConfigError`。
        """
        if overrides.deployment is not None:
            deployment = overrides.deployment
        else:
            deployment = parse_optional_env("IRONCLAW_DEPLOYMENT_MODE")
            if deployment is None:
                deployment = DeploymentMode.LocalSingleUser

        if overrides.profile is not None:
            requested_profile = overrides.profile
        else:
            requested_profile = parse_optional_env("IRONCLAW_RUNTIME_PROFILE")
            if requested_profile is None:
                requested_profile = RuntimeProfile.SecureDefault

        if overrides.yolo_disclosure_acknowledged is not None:
            yolo_disclosure_acknowledged = overrides.yolo_disclosure_acknowledged
        else:
            yolo_disclosure_acknowledged = parse_bool_env_or_default(
                "IRONCLAW_YOLO_DISCLOSURE", False
            )

        request = ResolveRequest(
            deployment=deployment,
            requested_profile=requested_profile,
            # 组织策略尚未从设置存储中获取。
            # 在还没有写入位置的情况下将其暴露出来会引起混淆；
            # 预留此字段，等待后续连接设置存储层。
            org_policy=OrgPolicyConstraints.default(),
            yolo_disclosure_acknowledged=yolo_disclosure_acknowledged,
        )

        effective_policy = resolve(request)
        if effective_policy is None:  # 模拟 Result 的错误处理
            raise resolver_error()

        return cls(
            deployment=deployment,
            requested_profile=requested_profile,
            yolo_disclosure_acknowledged=yolo_disclosure_acknowledged,
            effective_policy=effective_policy,
        )

    @staticmethod
    def safe_default() -> "RuntimeConfig":
        """
        用于测试 / `Config::for_testing` 的便捷方法。
        始终以最安全的默认值成功。
        """
        deployment = DeploymentMode.LocalSingleUser
        requested_profile = RuntimeProfile.SecureDefault
        # `(LocalSingleUser, SecureDefault, 默认 OrgPolicyConstraints, 无 yolo 披露)`
        # 在结构上保证可以解析——`SecureDefault` 在 `is_compatible` 中与部署无关，
        # 不是 yolo 类型，且空的 `OrgPolicyConstraints` 永远不会收缩。
        # 由 `ironclaw_runtime_policy::resolver::tests` 中的
        # `every_valid_deployment_profile_pair_resolves` 锁定。
        effective_policy = resolve(
            ResolveRequest.new(deployment, requested_profile)
        )  # 安全性：与部署无关的配置文件 + 空策略是全覆盖的
        return RuntimeConfig(
            deployment=deployment,
            requested_profile=requested_profile,
            yolo_disclosure_acknowledged=False,
            effective_policy=effective_policy,
        )
