"""
负责在数据流向 LLM 之前进行多阶段安全检查，防止提示注入攻击、数据泄露和恶意内容

具体逻辑:
- 用户输入检查: 在`handle_with_engine_inner`中，用户输入首先经过两层安全检查：
    - 检测是否包含系统文件访问路径、SQL 注入模式、加密私钥等;
    - 检测是否包含 API 密钥（sk-...）、GitHub token（ghp_...）等敏感信息
- 工具输出检查: 在EffectBridgeAdapter中，调用.sanitize_tool_output
"""

from dataclasses import dataclass
from typing import List, Optional

from .leak_detector import LeakDetector
from .policy import Policy, PolicyRule
from .sanitizer import Sanitizer, SanitizedOutput
from .validator import Validator, ValidationResult


@dataclass
class SafetyConfig:
    max_output_length: int
    injection_check_enabled: bool


@dataclass
class SafetyLayer:
    sanitizer: Sanitizer
    validator: Validator
    policy: Policy
    leak_detector: LeakDetector
    config: SafetyConfig

    def validate_input(self, content: str) -> ValidationResult:
        """
        检测是否包含系统文件访问路径（如 /etc/passwd）、SQL 注入模式、加密私钥等
        """

        return self.validator.validate(content)

    def check_policy(self, content: str) -> List[PolicyRule]:
        """
        检查内容是否违反任何策略规则。
        """

        return self.policy.check(content)

    def scan_inbound_for_secrets(self, content: str) -> Optional[str]:
        """
        扫描用户输入中是否包含泄漏的密钥（API 密钥、令牌等）。

        如果输入包含看起来像密钥的内容，则返回 `警告消息`，
        以便调用者可以提前拒绝消息，而不是将其发送到 LLM
        （LLM 可能会回显它并触发出站阻止循环）。
        """
        warning = (
            "您的消息似乎包含一个密钥（API 密钥、令牌或凭证）。"
            "出于安全考虑，它未被发送到 AI。请移除密钥后重试。"
            "要存储凭证，请使用设置表单或 `ironclaw config set <name> <value>`。"
        )
        try:
            cleaned = self.leak_detector.scan_and_clean(content)
            if cleaned != content:
                return warning
        except Exception:
            return warning

        # 干净的输入
        return None

    def sanitize_tool_output(self, tool_name: str, output: str) -> SanitizedOutput:
        """
        在工具输出到达 LLM 之前对其进行清理。
        """

        # 运行一次清理：如果启用了注入检查或策略要求，则执行。
        if self.config.injection_check_enabled:
            return self.sanitizer.sanitize(output)
        else:
            return SanitizedOutput(content=output)
