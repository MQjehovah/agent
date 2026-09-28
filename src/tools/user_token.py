"""工具侧用户 token 解析公共件: 当前 run 提问者的下游 token(aud=gateway), fail-closed。

RunContext.user_id tag → 数字 uid → ``web.sso_tokens.require_user_token``(SSO 刷新/交换)。
uid 解析失败或无托管 token 一律返回 ``("", USER_TOKEN_HINT)``, 调用方不得回退服务身份。

注意: 内部含同步阻塞路径(SSO 刷新/交换 + SQLite + 锁), 调用方须经
``asyncio.to_thread`` 执行, 避免卡住全服事件循环。
"""

import logging

logger = logging.getLogger("agent.tools")


def resolve_user_token_or_hint() -> tuple[str, str | None]:
    """解析当前 run 提问者的用户下游 token(aud=gateway), fail-closed。

    uid 由 ``RunContext.user_id`` tag 经 ``user_profile.uid_from_tag`` 解析。
    返回 ``(token, None)``; uid 解析失败或无托管 token(含空串)时返回
    ``("", USER_TOKEN_HINT)``; 其它意外异常同样 fail-closed 并记 WARNING, 不外漏。
    调用方一律 fail-closed, 不得回退服务令牌/服务账号身份。
    """
    from agent.core import current_run
    from agent.user_profile import uid_from_tag
    from web.sso_tokens import USER_TOKEN_HINT, UserTokenUnavailable, require_user_token

    try:
        uid = uid_from_tag(getattr(current_run(), "user_id", "") or "")
        if not uid:
            return "", USER_TOKEN_HINT
        token = require_user_token(int(uid), "gateway")
        if not token:
            return "", USER_TOKEN_HINT
        return token, None
    except UserTokenUnavailable:
        return "", USER_TOKEN_HINT
    except Exception as e:  # noqa: BLE001
        logger.warning(f"工具用户 token 解析失败(已 fail-closed): {e}")
        return "", USER_TOKEN_HINT
