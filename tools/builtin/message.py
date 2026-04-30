from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# 用于向频道发送消息的工具。
class MessageTool(BaseModel):
    channel_manager: ChannelManager
    extension_manager: Optional[ExtensionManager] = None
    # 当前对话的默认频道（每轮设置一次）。
    # 使用 std::sync::RwLock，因为 requires_approval() 是同步的，并在异步上下文中被调用。
    default_channel: Optional[str]
    # 当前对话的默认目标（user_id 或 group_id），每轮设置一次。
    default_target: Optional[str]
    # 用于附件路径验证的基础目录（沙盒）。
    base_dir: Path