from dataclasses import dataclass, field
from typing import List, Dict, Optional
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# 注册技能
@dataclass
class SkillRegistry:
    # 所有技能
    skills: List[LoadedSkill] = field(default_factory=list)
    # 用户技能目录（~/.ironclaw/skills/）。此处的技能均为受信任技能。
    user_dir: Path
    # 注册表安装的技能目录（~/.ironclaw/installed_skills/）。此处的技能已安装。
    installed_dir: Optional[Path] = None
    # 可选的工作区技能目录。
    workspace_dir: Optional[Path] = None