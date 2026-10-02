"""能力市场共享工具: 市场配置读取(供 market_search / market_execute 复用)。

用户 token 解析公共件已抽到 ``tools/user_token.py``(知识检索工具等共用)。
"""

import logging
import os

logger = logging.getLogger("agent.tools")


def market_config() -> tuple[str, str, float]:
    """读取市场配置(base_url, service_token, timeout)。

    镜像 ``mcps.platform.PlatformMCPConfig.from_env`` 语义; 此处刻意不 import ``mcps``,
    以避免仅为读配置而引入 MCP SDK(mcps 包初始化会拉起 MCP 客户端依赖)。

    注: 返回的 service_token 仅作**启用门禁**判断, 市场调用不再使用(逐请求走用户 token)。
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
