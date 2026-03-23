import os
from dataclasses import dataclass
from datetime import datetime
from datetime import time
from typing import Optional, Union

from config.elpers import optional_env, parse_bool_env, parse_option_env, parse_optional_env
from error import ConfigError
from settings import Settings


@dataclass
class HeartbeatConfig:
    """心跳配置"""
    # 是否启用心跳检测
    enabled: bool = False
    # 心跳检测间隔（秒）（当未设置 fire_at 时使用）
    interval_secs: int = 1800
    # 心跳检测结果通知通道
    notify_channel: Optional[str] = None
    # 心跳检测结果通知用户 ID。
    notify_user: Optional[str] = None
    # 触发的固定时间（HH:MM，24 小时制）。设置后，interval_secs 将被忽略。
    fire_at: Optional[time] = None        # 使用 datetime.time 类型
    # 静默期开始的小时数（0-23）
    quiet_hours_start: Optional[int] = None
    # 静默期结束的小时数（0-23）
    quiet_hours_end: Optional[int] = None
    # fire_at 和静默期评估的时区（IANA 名称）如 "Asia/Shanghai"
    timezone: Optional[str] = None

    @classmethod
    def default(cls):

        return cls()

    @classmethod
    def resolve(cls, settings: Settings) -> Union['HeartbeatConfig', ConfigError]:
        fire_at_str = os.getenv("HEARTBEAT_FIRE_AT", settings.heartbeat.fire_atsettings.heartbeat.fire_at)
        try:
            fire_at = datetime.strptime(fire_at_str, "%H:%M").time()
        except ValueError as e:
            raise ConfigError.InvalidValue(key="HEARTBEAT_FIRE_AT", message=str("must be HH:MM (24h), e.g. '14:00': {e}"))

        config = cls(
            enabled=parse_bool_env("HEARTBEAT_ENABLED", settings.heartbeat.enabled),
            interval_secs = parse_optional_env(
            "HEARTBEAT_INTERVAL_SECS",
            settings.heartbeat.interval_secs,
        ),
            notify_channel = optional_env("HEARTBEAT_NOTIFY_CHANNEL") or settings.heartbeat.notify_user,
            fire_at = fire_at,
            quiet_hours_start = parse_optional_env("HEARTBEAT_QUIET_START") or settings.heartbeat.quiet_hours_start,
            quiet_hours_end = parse_optional_env("HEARTBEAT_QUIET_END") or settings.heartbeat.quiet_hours_end,
            timezone = optional_env("HEARTBEAT_TIMEZONE") or settings.heartbeat.timezone
        )

        return config
