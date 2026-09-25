"""能力市场共享工具: 配置读取与 subject 解析(供 market_runtime / market_search 复用)。"""

import os


def market_config() -> tuple[str, str, float]:
    """读取市场配置(base_url, service_token, timeout)。

    镜像 ``mcps.platform.PlatformMCPConfig.from_env`` 语义; 此处刻意不 import ``mcps``,
    以避免仅为读配置而引入 MCP SDK(mcps 包初始化会拉起 MCP 客户端依赖)。
    """
    base = os.environ.get("MARKET_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("MARKET_SERVICE_TOKEN", "").strip()
    try:
        timeout = float(os.environ.get("MARKET_PLATFORM_TIMEOUT", "60") or "60")
    except ValueError:
        timeout = 60.0
    if timeout <= 0:
        timeout = 60.0
    return base, token, timeout


def resolve_subject() -> str:
    """当前 run 的提问者(subject) → 市场用户名(= rbac 工号); 解析失败返回空串。"""
    try:
        from agent.core import current_run

        raw = getattr(current_run(), "user_id", "") or ""
    except Exception:
        raw = ""
    if not raw:
        return ""
    try:
        from web.security import resolve_market_act_as

        return resolve_market_act_as(raw)
    except Exception:
        return ""
