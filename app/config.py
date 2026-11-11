"""环境配置：IMAP 连接参数与 SQLite 存储路径。

所有连接参数均来自环境变量，不在代码或请求体中硬编码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数，实际值: {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ImapSettings:
    """一次采集使用的 IMAP 连接参数（来自环境变量）。"""

    host: str
    port: int
    username: str
    password: str
    mailbox: str
    use_ssl: bool
    connect_timeout_seconds: int
    # 单次采集调用最多处理多少封邮件，防止一次请求无限运行。
    batch_max_messages: int

    @classmethod
    def from_env(cls) -> "ImapSettings":
        return cls(
            host=_env("IMAP_HOST"),
            port=_env_int("IMAP_PORT", 993),
            username=_env("IMAP_USERNAME"),
            password=os.environ.get("IMAP_PASSWORD", ""),
            mailbox=_env("IMAP_MAILBOX", "INBOX"),
            use_ssl=_env_bool("IMAP_USE_SSL", True),
            connect_timeout_seconds=_env_int("IMAP_CONNECT_TIMEOUT", 30),
            batch_max_messages=_env_int("IMAP_BATCH_MAX_MESSAGES", 200),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("IMAP_HOST", self.host),
                ("IMAP_USERNAME", self.username),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"缺少必要环境变量: {', '.join(missing)}")


def database_path() -> str:
    return _env("COLLECT_DB_PATH", "/data/collect.db")


# 采集心跳（秒）与锁过期判定（秒）。锁过期只用于进程崩溃后的恢复，
# 正常运行时靠显式释放，绝不在两进程同时活跃时互抢。
LOCK_HEARTBEAT_SECONDS = 10
LOCK_STALE_SECONDS = _env_int("COLLECT_LOCK_STALE_SECONDS", 300)
