# 集中式扩展/工具凭证认证管理器
#
# 统一管理预检凭证校验逻辑与配置说明查询功能。
# 使用单一状态机整合分散在router.rs、effect_adapter.rs、extension_tools.rs中的各类认证逻辑。
#
# 三条识别链路：
# 1. **HTTP工具** — 共享凭证注册表 + 支持自动刷新的通用凭证解析机制
# 2. **WASM工具** — 复用上述链路（WASM工具注册宿主与凭证映射关系）
# 3. **扩展程序** — 调用扩展管理器的工具认证状态校验方法
from __future__ import annotations
from extensions.naming import canonicalize_extension_name
from extensions import (
    ConfigureResult,
    ExtensionError,
    InstalledExtension,
    LatentProviderAction,
ExtensionManager
)
from secrets import SecretsStore
from tools import ToolRegistry
from tools.builtin import extract_host_from_params, extract_path_from_params
from skills import SkillCredentialSpec, SkillRegistry

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum
import logging

logger = logging.getLogger(__name__)


# ── 认证检查结果 ─────────────────────────────────────────────

class AuthCheckResult:
    """认证检查结果"""
    pass


@dataclass
class NoAuthRequired(AuthCheckResult):
    """无需认证"""
    pass


@dataclass
class Ready(AuthCheckResult):
    """凭证就绪"""
    pass


@dataclass
class MissingCredentials(AuthCheckResult):
    """缺少凭证"""
    missing: List["MissingCredential"]


# ── 缺失凭证 ─────────────────────────────────────────────────

@dataclass
class MissingCredential:
    """缺失的凭证描述"""
    credential_name: Any  # CredentialName
    setup_instructions: Optional[str] = None
    auth_url: Optional[str] = None


# ── 工具就绪状态 ─────────────────────────────────────────────

class ToolReadiness:
    """工具就绪状态"""
    pass


@dataclass
class ToolReady(ToolReadiness):
    """工具就绪"""
    pass


@dataclass
class NeedsAuth(ToolReadiness):
    """需要认证"""
    credential_name: Any = None  # CredentialName
    instructions: Optional[str] = None
    auth_url: Optional[str] = None


@dataclass
class NeedsSetup(ToolReadiness):
    """需要设置"""
    message: str = ""


# ── 潜在动作执行 ─────────────────────────────────────────────

class LatentActionExecution:
    """潜在动作执行结果"""
    pass


@dataclass
class RetryRegisteredAction(LatentActionExecution):
    """重试已注册的动作"""
    resolved_action: str


@dataclass
class ProviderReady(LatentActionExecution):
    """提供者就绪"""
    provider_extension: str
    available_actions: List[str]


@dataclass
class NeedsAuthExecution(LatentActionExecution):
    """需要认证"""
    credential_name: Any  # CredentialName
    instructions: str
    auth_url: Optional[str] = None


@dataclass
class NeedsSetupExecution(LatentActionExecution):
    """需要设置"""
    message: str


# ── 潜在动作定义 ─────────────────────────────────────────────

@dataclass
class LatentActionDef:
    """潜在动作定义"""
    action_name: str
    description: str = ""
    parameters_schema: Optional[dict] = None


@dataclass
class LatentProviderAction:
    """潜在提供者动作"""
    provider_extension: str
    action_name: str


# ── 已安装扩展 ──────────────────────────────────────────────

@dataclass
class InstalledExtension:
    """已安装的扩展"""
    name: str
    kind: str = ""
    status: str = ""


# ── 配置结果 ─────────────────────────────────────────────────

@dataclass
class ConfigureResult:
    """配置结果"""
    message: str = ""
    activated: bool = False
    pairing_required: bool = False
    auth_url: Optional[str] = None
    onboarding_state: Optional[str] = None
    onboarding: Optional[dict] = None


# ── 技能凭证规范 ─────────────────────────────────────────────

@dataclass
class SkillCredentialSpec:
    """技能凭证规范"""
    name: str
    provider: str = ""
    setup_instructions: Optional[str] = None
    oauth: Optional[dict] = None

    def clone(self) -> "SkillCredentialSpec":
        return SkillCredentialSpec(
            name=self.name,
            provider=self.provider,
            setup_instructions=self.setup_instructions,
            oauth=dict(self.oauth) if self.oauth else None,
        )


# ── 认证管理器 ───────────────────────────────────────────────

@dataclass
class AuthManager:
    """扩展/工具凭证流的集中式认证状态

    为引擎、网关和扩展运行时提供预检凭证检查、设置指令查找和工具就绪查询
    """
    secrets_store: SecretsStore
    skill_registry: Optional[SkillRegistry] = None
    extension_manager: Optional[ExtensionManager] = None
    tools: Optional[ToolRegistry] = None

    def settings_store(self) -> Optional[Any]:
        """获取设置存储"""
        if self.tools is not None:
            db = getattr(self.tools, 'database', None)
            if db is not None:
                return db() if callable(db) else db

        if self.extension_manager is not None:
            return getattr(self.extension_manager, 'settings_store', lambda: None)()

        return None

    async def check_action_auth(
            self,
            action_name: str,
            parameters: dict,
            user_id: str,
            credential_registry: Any,  # SharedCredentialRegistry
    ) -> AuthCheckResult:
        """工具调用的预检凭证检查

        对于 `http` 工具（以及使用相同凭证注入路径的 WASM 工具），
        从参数中提取目标主机，查找已注册的凭证映射，
        并检查所需的密钥是否存在于存储中
        """
        is_http = action_name in ("http", "http_request")

        if is_http:
            return await self.check_http_auth(parameters, user_id, credential_registry)

        # 非 HTTP 路径：询问工具本身声明了哪些凭证
        if self.tools is None:
            return NoAuthRequired()

        tool = await self.tools.get(action_name)
        if tool is None:
            return NoAuthRequired()

        required = tool.required_credentials() if hasattr(tool, 'required_credentials') else []
        if not required:
            return NoAuthRequired()

        role_lookup = getattr(self.tools, 'role_lookup', None)
        if role_lookup is not None:
            role_lookup = role_lookup()

        missing = []
        for secret_name in required:
            oauth_refresh = getattr(credential_registry, 'oauth_refresh_for_secret', None)
            if oauth_refresh is not None:
                oauth_refresh = oauth_refresh(secret_name)

            try:
                await resolve_secret_for_runtime(
                    self.secrets_store,
                    user_id,
                    secret_name,
                    role_lookup,
                    oauth_refresh,
                    DefaultFallback.AdminOnly,
                )
            except Exception as error:
                if hasattr(error, 'requires_authentication') and error.requires_authentication():
                    missing.append(
                        await self.describe_missing_credential(secret_name, user_id)
                    )
                else:
                    logger.debug(
                        f"预检认证期间解析凭证失败: "
                        f"action={action_name}, secret={secret_name}, error={error}"
                    )
                    missing.append(MissingCredential(
                        credential_name=CredentialName.from_trusted(secret_name),
                        setup_instructions=None,
                        auth_url=None,
                    ))

        if not missing:
            return Ready()
        else:
            return MissingCredentials(missing=missing)

    async def check_http_auth(
            self,
            parameters: dict,
            user_id: str,
            credential_registry: Any,  # SharedCredentialRegistry
    ) -> AuthCheckResult:
        """通过提取主机并查询凭证注册表 + 密钥存储来检查 HTTP 工具凭证"""
        host = extract_host_from_params(parameters)
        if host is None:
            logger.debug("预检认证: 参数中无主机 — 跳过")
            return NoAuthRequired()

        path = extract_path_from_params(parameters) or "/"
        matched = credential_registry.find_for_url(host, path)
        logger.debug(f"预检认证: 凭证注册表查找 host={host}, matched_count={len(matched)}")

        if not matched:
            return NoAuthRequired()

        # 联合评估：每个*必需*的匹配映射都必须解析
        missing = []
        for mapping in matched:
            if getattr(mapping, 'optional', False):
                continue

            oauth_refresh = getattr(credential_registry, 'oauth_refresh_for_secret', None)
            if oauth_refresh is not None:
                oauth_refresh = oauth_refresh(mapping.secret_name)

            role_lookup = None
            if self.tools is not None:
                rl = getattr(self.tools, 'role_lookup', None)
                if rl is not None:
                    role_lookup = rl()

            try:
                await resolve_secret_for_runtime(
                    self.secrets_store,
                    user_id,
                    mapping.secret_name,
                    role_lookup,
                    oauth_refresh,
                    DefaultFallback.AdminOnly,
                )
            except Exception as error:
                if hasattr(error, 'requires_authentication') and error.requires_authentication():
                    missing.append(
                        await self.describe_missing_credential(mapping.secret_name, user_id)
                    )
                else:
                    logger.debug(
                        f"预检认证期间解析凭证失败: "
                        f"secret={mapping.secret_name}, error={error}"
                    )
                    missing.append(MissingCredential(
                        credential_name=CredentialName.from_trusted(mapping.secret_name),
                        setup_instructions=None,
                        auth_url=None,
                    ))

        if not missing:
            return Ready()
        else:
            return MissingCredentials(missing=missing)

    async def check_tool_readiness(self, tool_name: str, user_id: str) -> ToolReadiness:
        """检查工具（按名称）是否可以使用、需要认证或需要管理员设置

        由 `available_actions()` 用于过滤完全无法工作的工具
        """
        ext_mgr = self.extension_manager
        if ext_mgr is None:
            return ToolReady()

        ext_name = await self._resolve_extension_name(tool_name)
        if ext_name is None:
            return ToolReady()

        result = await ext_mgr.ensure_extension_ready(
            ext_name, user_id, "UseCapability",
        )
        return await self.readiness_from_extension_result(ext_name, user_id, result)

    async def prepare_tool_for_execution(self, tool_name: str, user_id: str) -> ToolReadiness:
        """准备扩展支持的能力以立即执行

        与 [`check_tool_readiness`] 不同，此路径可能将潜在的注册表支持的扩展
        提升到已安装状态，因为调用者正在处理具体的用户请求动作，
        而不仅仅是列出或过滤可用动作
        """
        ext_mgr = self.extension_manager
        if ext_mgr is None:
            return ToolReady()

        ext_name = await self._resolve_extension_name(tool_name)
        if ext_name is None:
            return ToolReady()

        result = await self._ensure_extension_ready_for_execution(ext_mgr, ext_name, user_id)
        return await self.readiness_from_extension_result(ext_name, user_id, result)

    async def _resolve_extension_name(self, tool_name: str) -> Optional[str]:
        """解析工具对应的扩展名称"""
        if self.tools is not None:
            provider_ext = await self.tools.provider_extension_for_tool(tool_name)
            if provider_ext is not None:
                return provider_ext

        try:
            return canonicalize_extension_name(tool_name)
        except Exception:
            return None

    @staticmethod
    async def _ensure_extension_ready_for_execution(
            ext_mgr: Any,
            extension_name: str,
            user_id: str,
    ) -> Any:
        """确保扩展准备好执行"""
        try:
            return await ext_mgr.ensure_extension_ready(
                extension_name, user_id, "UseCapability",
            )
        except Exception as e:
            if "NotInstalled" in str(e):
                logger.debug(
                    f"扩展未安装用于能力使用；通过显式激活路径重试: "
                    f"extension={extension_name}, user_id={user_id}"
                )
                return await ext_mgr.ensure_extension_ready(
                    extension_name, user_id, "ExplicitActivate",
                )
            raise

    async def readiness_from_extension_result(
            self,
            ext_name: str,
            user_id: str,
            result: Any,
    ) -> ToolReadiness:
        """从扩展结果构建工具就绪状态"""
        try:
            outcome_type = getattr(result, 'type', None) or str(result)
        except Exception:
            return ToolReady()

        if "Ready" in outcome_type:
            return ToolReady()
        elif "NeedsAuth" in outcome_type:
            credential_name = getattr(result, 'credential_name', None) or ext_name
            auth = getattr(result, 'auth', None)
            described = await self.describe_missing_credential(credential_name, user_id)

            instructions = described.setup_instructions
            if instructions is None and auth is not None:
                auth_status = getattr(auth, 'status', None)
                if "AwaitingAuthorization" in str(auth_status):
                    instructions = f"认证 '{getattr(auth, 'name', ext_name)}' 以完成设置。"
                elif "AwaitingToken" in str(auth_status):
                    instructions = getattr(auth_status, 'instructions', None) or instructions

            auth_url = None
            if auth is not None:
                auth_url = sanitize_auth_url(getattr(auth, 'auth_url', lambda: None)())
            if auth_url is None:
                auth_url = sanitize_auth_url(described.auth_url)

            return NeedsAuth(
                credential_name=described.credential_name,
                instructions=instructions,
                auth_url=auth_url,
            )
        elif "NeedsSetup" in outcome_type:
            return NeedsSetup(message=getattr(result, 'instructions', ''))
        else:
            logger.debug(
                f"扩展认证就绪探测失败；将工具视为就绪: "
                f"tool={ext_name}, user_id={user_id}, result={result}"
            )
            return ToolReady()

    async def resolve_extension_name_for_auth_flow(
            self,
            action_name: str,
            parameters: dict,
            credential_fallback: str,
            user_id: str,
    ) -> Any:
        """解析拥有动作的面向用户的扩展/频道名称

        这与后端凭证身份有意不同。
        对于扩展支持的认证流，我们希望 UI 和令牌提交路径操作已安装的扩展名称，
        而密钥仍以声明的凭证名称存储
        """
        return await resolve_auth_flow_extension_name(
            action_name,
            parameters,
            credential_fallback,
            user_id,
            self.tools,
            self.extension_manager,
        )

    async def latent_extension_actions(self) -> List[LatentActionDef]:
        """获取潜在的扩展动作"""
        if self.extension_manager is None:
            return []

        actions = await self.extension_manager.latent_provider_actions_default_user()
        return [
            LatentActionDef(
                action_name=a.action_name,
                description=getattr(a, 'description', ''),
                parameters_schema=getattr(a, 'parameters_schema', None),
            )
            for a in actions
        ]

    async def list_capability_extensions(self, user_id: str) -> List[InstalledExtension]:
        """列出能力扩展"""
        if self.extension_manager is None:
            return []

        extensions = await self.extension_manager.list(None, True, user_id)
        return [
            InstalledExtension(
                name=e.name,
                kind=getattr(e, 'kind', ''),
                status=getattr(e, 'status', ''),
            )
            for e in extensions
        ]

    async def latent_provider_actions(self, user_id: str) -> List[LatentProviderAction]:
        """获取用户范围的潜在提供者动作"""
        if self.extension_manager is None:
            return []

        actions = await self.extension_manager.latent_provider_actions(user_id)
        return [
            LatentProviderAction(
                provider_extension=a.provider_extension,
                action_name=a.action_name,
            )
            for a in actions
        ]

    async def notification_target_for_channel(self, name: str) -> Optional[str]:
        """获取频道路由的通知目标"""
        if self.extension_manager is None:
            return None
        return await self.extension_manager.notification_target_for_channel(name)

    async def execute_latent_extension_action(
            self,
            action_name: str,
            user_id: str,
    ) -> Optional[Any]:
        """执行潜在扩展动作"""
        ext_mgr = self.extension_manager
        if ext_mgr is None:
            return None

        latent = await ext_mgr.latent_provider_action(action_name, user_id)
        if latent is None:
            return None

        readiness = await self._ensure_extension_ready_for_execution(
            ext_mgr, latent.provider_extension, user_id,
        )

        if "Ready" in str(type(readiness)):
            available_actions = await ext_mgr.provider_action_names(latent.provider_extension)
            if latent.action_name in available_actions:
                return RetryRegisteredAction(resolved_action=latent.action_name)
            else:
                return ProviderReady(
                    provider_extension=latent.provider_extension,
                    available_actions=available_actions,
                )
        elif isinstance(readiness, NeedsAuth):
            return NeedsAuthExecution(
                credential_name=CredentialName.from_trusted(latent.provider_extension),
                instructions=readiness.instructions or "完成认证以继续。",
                auth_url=readiness.auth_url,
            )
        elif isinstance(readiness, NeedsSetup):
            return NeedsSetupExecution(message=readiness.message)
        else:
            return readiness

    async def describe_missing_credential(
            self,
            credential_name: str,
            user_id: str,
    ) -> MissingCredential:
        """描述缺失的凭证"""
        setup_instructions = self.get_setup_instructions(credential_name)
        auth_url = sanitize_auth_url(
            await self.start_skill_oauth_if_supported(credential_name, user_id)
        )

        if auth_url is not None:
            setup_instructions = setup_instructions or f"认证 '{credential_name}' 以继续。"

        return MissingCredential(
            credential_name=CredentialName.from_trusted(credential_name),
            setup_instructions=setup_instructions,
            auth_url=auth_url,
        )

    async def submit_auth_token(
            self,
            extension_name: str,
            token: str,
            user_id: str,
    ) -> ConfigureResult:
        """提交认证令牌"""
        trimmed = token.strip()
        if not trimmed:
            raise Exception("凭证不能为空。")

        # 尝试通过扩展管理器提交
        if self.extension_manager is not None:
            try:
                return await self.extension_manager.configure_token(extension_name, trimmed, user_id)
            except Exception as e:
                if "NotInstalled" not in str(e) and "not found" not in str(e):
                    raise

        # 回退到直接存储
        spec = self.get_credential_spec(extension_name)
        if spec is None:
            raise Exception(f"未安装 '{extension_name}'")

        # 深度防御：仅使用注册的凭证名称写入
        if spec.name != extension_name:
            raise Exception(
                f"凭证名称不匹配: 请求 '{extension_name}'，解析 '{spec.name}'"
            )

        params = {"name": spec.name, "value": trimmed}
        if spec.provider:
            params["provider"] = spec.provider

        await self.secrets_store.create(user_id, params)

        return ConfigureResult(
            message=f"凭证 '{spec.name}' 已存储。",
            activated=True,
        )

    async def start_skill_oauth_if_supported(
            self,
            credential_name: str,
            user_id: str,
    ) -> Optional[str]:
        """如果支持，启动技能 OAuth 流程"""
        spec = self.get_credential_spec(credential_name)
        if spec is None:
            return None

        oauth = spec.oauth
        if oauth is None:
            return None

        # 构建 OAuth 描述符并启动流程
        authorization_url = oauth.get("authorization_url", "")
        if not authorization_url:
            return None

        # 返回授权 URL（简化实现）
        return authorization_url

    def get_credential_spec(self, credential_name: str) -> Optional[SkillCredentialSpec]:
        """获取凭证规范"""
        if self.skill_registry is None:
            return None

        try:
            registry = self.skill_registry.read() if hasattr(self.skill_registry, 'read') else self.skill_registry
            skills = registry.skills() if callable(getattr(registry, 'skills', None)) else []

            for skill in skills:
                manifest = getattr(skill, 'manifest', None)
                if manifest is None:
                    continue
                credentials = getattr(manifest, 'credentials', [])
                for cred in credentials:
                    if getattr(cred, 'name', '') == credential_name:
                        return SkillCredentialSpec(
                            name=cred.name,
                            provider=getattr(cred, 'provider', ''),
                            setup_instructions=getattr(cred, 'setup_instructions', None),
                            oauth=getattr(cred, 'oauth', None),
                        )
        except Exception:
            pass

        return None

    def get_setup_instructions(self, credential_name: str) -> Optional[str]:
        """查找凭证的人类可读设置指令

        检查技能注册表中匹配的凭证规范以获取 `setup_instructions`。
        回退到通用提示
        """
        spec = self.get_credential_spec(credential_name)
        if spec is not None:
            return spec.setup_instructions
        return None

    def get_setup_instructions_or_default(self, credential_name: str) -> str:
        """获取设置指令，带回退默认消息"""
        return self.get_setup_instructions(credential_name) or f"提供你的 {credential_name} 令牌"