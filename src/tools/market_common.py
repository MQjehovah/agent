"""能力市场共享工具: 配置读取与用户 token 解析(供 market_search / market_runtime / market_delegate 复用)。"""

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


def resolve_user_token_or_hint() -> tuple[str, str | None]:
    """解析当前 run 提问者的用户下游 token(aud=gateway), fail-closed。

    uid 由 ``RunContext.user_id`` tag 经 ``user_profile.uid_from_tag`` 解析。
    返回 ``(token, None)``; uid 解析失败或市场无托管 token(含 uid <= 0)时返回
    ``("", USER_TOKEN_HINT)``; 其它意外异常同样 fail-closed 并记 WARNING, 不外漏。
    调用方一律 fail-closed, 不得回退服务令牌身份。

    注意: 内部含同步阻塞路径(SSO 刷新/交换 + SQLite + 锁)，工具内须经
    ``asyncio.to_thread`` 调用, 避免卡住全服事件循环。
    """
    from agent.core import current_run
    from agent.user_profile import uid_from_tag
    from web.sso_tokens import USER_TOKEN_HINT, UserTokenUnavailable, require_user_token

    try:
        uid = uid_from_tag(getattr(current_run(), "user_id", "") or "")
        if not uid:
            return "", USER_TOKEN_HINT
        return require_user_token(int(uid), "gateway"), None
    except UserTokenUnavailable:
        return "", USER_TOKEN_HINT
    except Exception as e:  # noqa: BLE001
        logger.warning(f"市场用户 token 解析失败(已 fail-closed): {e}")
        return "", USER_TOKEN_HINT
