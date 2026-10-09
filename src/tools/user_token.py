"""工具侧用户 token 解析公共件: 当前 run 提问者的下游 token(aud=gateway), fail-closed。

RunContext.user_id tag → 数字 uid → ``web.sso_tokens`` 取下游 token(SSO 刷新/交换)。
uid 解析失败或无托管 token 一律返回 ``("", USER_TOKEN_HINT)``, 普通用户不得回退服务身份。

管理员例外（``is_admin_run``）: 管理员/系统任务（归属 admin 账号, role=admin）由市场工具
（market_execute / market_search）兜底: 拿不到用户令牌时以服务令牌（平台身份）执行;
个人凭据缺失（市场 403 + ``X-Market-Error-Code: user_credentials_missing``）时以服务
令牌重试一次（个人优先、缺才平台）。

注意: 内部含同步阻塞路径(SSO 刷新/交换 + SQLite + 锁), 调用方须经
``asyncio.to_thread`` 执行, 避免卡住全服事件循环。
"""

import logging

logger = logging.getLogger("agent.tools")


def is_admin_run() -> bool:
    """当前 run 身份是否为管理员（rbac 角色 admin）。

    系统定时任务默认归属 admin 账号（role=admin）；管理员调用市场工具时：
    无用户令牌直接以服务令牌（平台身份）执行；个人凭据缺失时由市场工具以服务
    令牌重试一次。无 run 上下文/非管理员返回 False。
    """
    from agent.core import current_run

    rc = current_run()
    return (getattr(rc, "role", "") or "") == "admin" or (
        getattr(rc, "user_role", "") or ""
    ) == "admin"


def resolve_user_token_or_hint(force: bool = False) -> tuple[str, str | None]:
    """解析当前 run 提问者的用户下游 token(aud=gateway), fail-closed。

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

    try:
        uid = uid_from_tag(getattr(current_run(), "user_id", "") or "")
        if not uid:
            return "", USER_TOKEN_HINT
        if force:
            token = get_downstream_token(int(uid), "gateway", force=True)
        else:
            token = require_user_token(int(uid), "gateway")
        if not token:
            return "", USER_TOKEN_HINT
        return token, None
    except UserTokenUnavailable:
        return "", USER_TOKEN_HINT
    except Exception as e:  # noqa: BLE001
        logger.warning(f"工具用户 token 解析失败(已 fail-closed): {e}")
        return "", USER_TOKEN_HINT
