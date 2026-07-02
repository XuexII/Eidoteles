# 编译时内置至程序二进制文件的捆绑技能
#
# 构建脚本build.rs会收集所有skills目录下各子文件夹中的SKILL.md文件，
# 生成embedded_skills.json文件。本模块对该二进制数据进行反序列化，
# 并将原始的名称、内容键值对提供给技能注册表用于检索。


import json
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

# ── 嵌入式技能数据 ───────────────────────────────────────────

# 由构建过程从 `skills/*/SKILL.md` 生成的原始 JSON。
# 在实际部署中，此变量由构建脚本填充。
# 这里提供一个默认的空列表作为占位符
EMBEDDED_SKILLS_JSON = "[]"

# 跨调用缓存的已解析捆绑技能
_parsed_skills_cache: Optional[List[Tuple[str, str]]] = None


def parsed_skills() -> List[Tuple[str, str]]:
    """解析并缓存捆绑的技能

    Returns:
        包含 (skill_name, skill_md_content) 元组的列表
    """
    global _parsed_skills_cache

    if _parsed_skills_cache is not None:
        return _parsed_skills_cache

    try:
        entries = json.loads(EMBEDDED_SKILLS_JSON)
    except json.JSONDecodeError as e:
        logger.warning(f"解析嵌入式技能目录失败: {e}")
        _parsed_skills_cache = []
        return _parsed_skills_cache

    _parsed_skills_cache = [
        (entry["name"], entry["content"])
        for entry in entries
    ]
    return _parsed_skills_cache


def load_bundled_skills() -> List[Tuple[str, str]]:
    """加载编译到二进制文件中的所有捆绑技能（名称、内容）对

    返回 `(skill_name, skill_md_content)` 元组列表。
    这些由技能注册表以最低优先级发现源加载，信任级别为 `Trusted`
    （它们随应用程序一起发布）
    """
    return parsed_skills()
