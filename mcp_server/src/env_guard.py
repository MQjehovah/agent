"""mcp_server 侧的密钥分级守卫（自包含实现）。

历史上这里转发 agent 的 `src/utils/env_guard.py`，但 MCP server 以独立子进程运行
（cwd=/app，`python mcp_server/src/xxx.py`），并不保证 agent 的 `src/` 在 sys.path/
镜像内存在 —— 曾导致生产 `remote_terminal` 启动即 ModuleNotFoundError。
因此这里保留一份自包含实现，语义与 `src/utils/env_guard.py` 完全一致：
生产（APP_ENV=production/prod）下缺失/弱值直接拒绝启动，开发打印告警。
**修改任一份时请同步另一份（弱值清单必须一致）。**
"""
import logging
import os

WEAK_VALUES = {
    "change-me-in-production",
    "dev-secret-change-me-please-32-bytes-minimum",
    "default-secret",
    "default-key",
    "xzyz2022!",
    "admin123",
    "123456",
    "change-me",
    "gateway-secret",
    "agent-secret",
    "your-secret-key",
}

_PRODUCTION_NAMES = {"production", "prod"}

logger = logging.getLogger(__name__)


def is_production() -> bool:
    return os.environ.get("APP_ENV", "development").strip().lower() in _PRODUCTION_NAMES


def require_secret(name: str, value: str | None, weak_values: set[str] = WEAK_VALUES) -> str | None:
    """校验秘密类配置：生产拒绝弱值/空值，开发打印告警后放行。"""
    normalized = "" if value is None else str(value).strip()
    bad = (normalized == "") or (normalized in weak_values)
    if not bad:
        return value
    if is_production():
        raise RuntimeError(f"环境变量 {name} 未配置或仍为不安全的默认值,请参考 .env.example 设置")
    logger.warning("%s 使用默认/弱值;生产环境(APP_ENV=production)将拒绝启动", name)
    return value
