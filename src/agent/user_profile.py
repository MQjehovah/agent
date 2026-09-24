"""当前用户画像解析公共件 — 系统提示注入(agent.core)与 whoami 工具共用。

数据来源: 当前 run 的 RunContext(渠道身份) + rbac_users(工号/显示名/部门兜底)
+ rbac_user_identities(钉钉 userId 等平台绑定)。解析失败一律静默回退(字段为空),
不影响 run。
"""
import logging

logger = logging.getLogger("agent.user_profile")


def uid_from_tag(user_id: str) -> str:
    """归属 tag({channel}:{uid}) → 数字 uid; 非数字/缺失返回空串。"""
    raw = str(user_id or "").strip()
    uid = raw.split(":", 1)[1] if ":" in raw else raw
    return uid if uid.isdigit() else ""


def channel_from_tag(user_id: str) -> str:
    """归属 tag({channel}:{uid}) → 渠道前缀; 无 ':'/缺失返回空串。"""
    raw = str(user_id or "").strip()
    return raw.split(":", 1)[0] if ":" in raw else ""


def flat_text(value: str) -> str:
    """展平空白/换行: 画像字段单行展示, 防止换行注入提示词。"""
    return " ".join(str(value or "").split())


def _resolve_rbac(rbac=None):
    """取 RBACManager: 优先入参, 否则由进程内 storage 新建; 不可用返回 None(静默)。"""
    if rbac is not None:
        return rbac
    try:
        from security.rbac import RBACManager
        from storage.storage import get_storage
        storage = get_storage()
        if not storage:
            return None
        return RBACManager(storage)
    except Exception as e:
        logger.debug(f"用户画像: 初始化 RBAC 失败(忽略): {e}")
        return None


def resolve_rbac_user(uid: str, rbac=None) -> dict | None:
    """按数字 uid 查 rbac_users(name=工号 / display_name / department / role)。

    rbac 缺省时回退进程内 storage 新建 RBACManager; 非数字/查不到/异常 → None(静默)。
    """
    if not uid or not str(uid).isdigit():
        return None
    try:
        manager = _resolve_rbac(rbac)
        return manager.get_user(int(uid)) if manager else None
    except Exception as e:
        logger.debug(f"用户画像: 解析 uid={uid} 的 rbac 用户失败(忽略): {e}")
        return None


def resolve_dingtalk_uid(user_id, rbac=None) -> str:
    """rbac 用户 id → 绑定的钉钉 platform_uid(rbac_user_identities, platform='dingtalk')。

    未绑定/非数字/查询异常 → 空串(静默, 不影响 run)。
    """
    if user_id in (None, "", 0, "0"):
        return ""
    try:
        manager = _resolve_rbac(rbac)
        if not manager:
            return ""
        for identity in manager.list_user_identities(int(user_id)):
            if str(identity.get("platform") or "").strip().lower() == "dingtalk":
                return flat_text(identity.get("platform_uid"))
    except Exception as e:
        logger.debug(f"用户画像: 解析 user_id={user_id} 的钉钉绑定失败(忽略): {e}")
    return ""


def resolve_user_profile(ctx, rbac=None) -> dict:
    """RunContext → 用户画像字段(全部字符串, 缺失为空)。

    - name: ctx.user_name(渠道显示名)优先, rbac.display_name 兜底;
    - employee_id: rbac_users.name(= 工号);
    - department: ctx.user_department 优先, rbac.department 兜底;
    - user_role: 渠道显式角色(技能/身份判定唯一角色源); role: 权限判定哨兵值;
    - dingtalk: rbac_user_identities 中 platform='dingtalk' 的 platform_uid(未绑定为空);
    - uid/channel: 由 user_id tag 解析; group_context 原样带出(调用方决定是否使用)。
    """
    uid = uid_from_tag(getattr(ctx, "user_id", ""))
    user = (resolve_rbac_user(uid, rbac=rbac) if uid else None) or {}
    dingtalk = resolve_dingtalk_uid(user.get("id"), rbac=rbac) if uid else ""
    return {
        "user_id": str(getattr(ctx, "user_id", "") or ""),
        "uid": uid,
        "channel": channel_from_tag(getattr(ctx, "user_id", "")),
        "name": flat_text(getattr(ctx, "user_name", "")) or flat_text(user.get("display_name")),
        "employee_id": flat_text(user.get("name")),
        "department": (flat_text(getattr(ctx, "user_department", ""))
                       or flat_text(user.get("department"))),
        "user_role": flat_text(getattr(ctx, "user_role", "")),
        "role": flat_text(getattr(ctx, "role", "")),
        "dingtalk": dingtalk,
        "group_context": bool(getattr(ctx, "group_context", False)),
    }
