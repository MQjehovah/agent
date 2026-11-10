"""工具侧用户 token 解析公共件: 当前 run 提问者的下游 token, fail-closed。

RunContext.user_id tag → 数字 uid → ``web.sso_tokens`` 取下游 token(SSO 刷新/交换)。
uid 解析失败或无托管 token 一律返回 ``("", USER_TOKEN_HINT)``。

市场工具使用 ``audience="market"``，让市场把 token 解析成自己的用户身份；知识库等
其它资源仍用默认 ``gateway``。**真实用户不得回退服务身份**；只有没有数字 uid 的系统
run（``cli:admin`` / ``scheduler:admin`` 等）才允许调用方用 ``MARKET_SERVICE_TOKEN``。
凭据选择（个人优先、market 管理员平台兜底）全部由市场侧判断，agent 不再按自身角色重试。

注意: 内部含同步阻塞路径(SSO 刷新/交换 + SQLite + 锁), 调用方须经
``asyncio.to_thread`` 执行, 避免卡住全服事件循环。
"""

import logging

logger = logging.getLogger("agent.tools")


def is_system_run() -> bool:
    """当前 run 是否为显式系统身份（cli/系统定时，且没有数字 uid）。

    只有这类 run 允许调用方回退 ``MARKET_SERVICE_TOKEN``（平台身份）；真实用户
    （``web:7`` / ``dingtalk:7`` 等带数字 uid 的 tag）一律不得回退服务身份。
    空 tag、malformed tag（如 ``web:abc``）与非系统渠道一律 fail-closed。
    """
    from agent.core import current_run
    from agent.user_profile import uid_from_tag

    tag = str(getattr(current_run(), "user_id", "") or "").strip()
    if not tag or uid_from_tag(tag):
        return False
    channel = tag.split(":", 1)[0]
    return channel in ("cli", "scheduler", "system")


def resolve_user_token_or_hint(
    force: bool = False, audience: str = "gateway"
) -> tuple[str, str | None]:
    """解析当前 run 提问者的用户下游 token, fail-closed。

    uid 由 ``RunContext.user_id`` tag 经 ``user_profile.uid_from_tag`` 解析。
    返回 ``(token, None)``; uid 解析失败或无托管 token(含空串)时返回
    ``("", USER_TOKEN_HINT)``; 其它意外异常同样 fail-closed 并记 WARNING, 不外漏。
    ``force=True`` 跳过进程内缓存强换一次(下游 401 后重试通道, 与 web 代理一致)。
    调用方一律 fail-closed, 不得回退服务令牌/服务账号身份。
    """
    from agent.core import current_run
    from agent.user_profile import uid_from_tag
    from web.sso_tokens import (
        USER_TOKEN_HINT,
        UserTokenUnavailable,
        get_downstream_token,
        require_user_token,
    )

    aud = (audience or "gateway").strip() or "gateway"
    try:
        uid = uid_from_tag(getattr(current_run(), "user_id", "") or "")
        if not uid:
            return "", USER_TOKEN_HINT
        token = (
            get_downstream_token(int(uid), aud, force=True)
            if force
            else require_user_token(int(uid), aud)
        )
        if not token:
            return "", USER_TOKEN_HINT
        return token, None
    except UserTokenUnavailable:
        return "", USER_TOKEN_HINT
    except Exception as e:  # noqa: BLE001
        logger.warning(f"工具用户 token 解析失败(已 fail-closed): {e}")
        return "", USER_TOKEN_HINT
