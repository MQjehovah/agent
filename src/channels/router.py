import contextlib
import logging
import uuid
from typing import Any

from channels.run_registry import register_run, unregister_run
from tools.ask_user import reset_ask_user_mode, set_ask_user_mode

logger = logging.getLogger("agent.channels")

# 需要按用户隔离工作区的非 web 单聊渠道(群共享根不隔离)。
ISOLATED_CHANNELS = ("dingtalk", "feishu")


class MessageRouter:
    """统一消息路由：所有渠道通过此路由器调用 agent.run()

    职责：
    - 标准化 session_id 格式 ({channel}:{unique_id})
    - 非交互渠道自动设 ask_user_mode=auto
    - 一致的用户身份传递（含部门/显式角色: web 渠道由 server 认证处传入 rbac 值）
    - 运行中登记：除 cli（本机交互，不属线上会话）外，route 前后对会话做
      开始/结束登记，使「运行中」API 对钉钉/飞书/webhook/定时等渠道同样可见。
    - **非 web 单聊按用户隔离**：注入 worker 池后，钉钉/飞书单聊按 uid 取独立
      worker(工作区 workspace/users/u_{uid})执行，避免多用户共享根 workspace；
      dingtalk_group 群共享根保持共享语义(root 执行)。

    注：技能可见性按用户部门/显式角色过滤（fail-closed）。web 渠道由 server 认证处
    传入 department/role；钉钉/定时等渠道已有的显式 role 照常生效；未解析身份的渠道
    （飞书/webhook 等）department 为空、role 缺省，受限技能对其不可见也不可执行。
    """

    def __init__(self, agent, worker_pool=None):
        self.agent = agent
        # 可选: 用户级 Worker 池(由 WebServer 创建后经 set_worker_pool 注入)
        self.worker_pool = worker_pool

    def set_worker_pool(self, pool) -> None:
        """注入用户级 Worker 池: 非 web 单聊按 uid 隔离执行(工作区/上下文)。"""
        self.worker_pool = pool

    @staticmethod
    def format_session_id(channel: str, *parts: str) -> str:
        parts = [p for p in parts if p]
        return f"{channel}:" + ":".join(parts)

    @staticmethod
    def _isolation_uid(channel: str, user_id: str, group_context: bool) -> str:
        """可隔离的非 web 单聊返回 uid(数字字符串); 群共享/未解析身份返回空。

        仅钉钉/飞书单聊隔离；群共享根(dingtalk_group)整群一个上下文, 不隔离;
        user_id 形如 ``{channel}:{uid}``(uid=rbac 用户 id), 非数字(旧格式)不隔离。
        """
        if group_context or channel not in ISOLATED_CHANNELS:
            return ""
        uid = user_id.split(":", 1)[1] if ":" in user_id else user_id
        return uid if uid.isdigit() and uid != "0" else ""

    @staticmethod
    def _apply_run_scope(root, worker) -> dict:
        """把 root 的 run 作用域状态(确认回调/权限模式)临时同步到 worker。

        钉钉确认卡片由插件在 route 前装配到 root(on_confirm/权限模式), 实际 run 落在
        worker 上时必须同步, 否则写操作确认失效。返回原值供 finally 恢复。
        """
        saved: dict[str, Any] = {}
        try:
            saved["on_confirm"] = worker.on_confirm
            worker.on_confirm = getattr(root, "on_confirm", None)
        except Exception:
            pass
        root_pc = getattr(root, "_permission_config", None)
        worker_pc = getattr(worker, "_permission_config", None)
        if root_pc is not None and worker_pc is not None:
            try:
                saved["mode"] = worker_pc.mode
                worker_pc.mode = root_pc.mode
            except Exception:
                saved.pop("mode", None)
        return saved

    @staticmethod
    def _restore_run_scope(worker, saved: dict) -> None:
        if "on_confirm" in saved:
            with contextlib.suppress(Exception):
                worker.on_confirm = saved["on_confirm"]
        if "mode" in saved:
            with contextlib.suppress(Exception):
                worker._permission_config.mode = saved["mode"]

    async def _acquire_isolated(self, channel: str, user_id: str, user_name: str,
                                group_context: bool):
        """取用户级 worker; 返回 (run_agent, release_fn|None)。

        不可隔离(群/非目标渠道/无 uid)、池未启用或分配失败(容量饱和等)时回退共享实例。
        """
        pool = self.worker_pool
        uid = self._isolation_uid(channel, user_id, group_context)
        if pool is None or not getattr(pool, "enabled", False) or not uid:
            return self.agent, None
        tag = f"{channel}:{uid}"
        try:
            worker = await pool.acquire(tag, uid, user_name)
        except Exception as e:  # noqa: BLE001 — 含 PoolBusyError; 回退共享实例并告警
            logger.warning(
                f"非 web 渠道按用户隔离 worker 分配失败({tag}): {e!r}; 回退共享实例")
            return self.agent, None
        return worker, (lambda: pool.release(tag))

    async def route(
        self,
        content: str,
        channel: str = "cli",
        session_id: str = "",
        user_id: str = "",
        user_name: str = "",
        group_context: bool = False,
        return_result: bool = False,
        user_department: str = "",
        user_role: str = "",
        **kwargs,
    ) -> Any:
        if not session_id:
            session_id = self.format_session_id(channel, uuid.uuid4().hex[:8])

        if not user_id:
            user_id = f"{channel}:admin"
        if not user_name:
            user_name = "管理员" if channel == "cli" else channel

        # 非 web 单聊: 取该用户独立 worker(工作区隔离); 群/未解析身份回退 root
        run_agent, release = await self._acquire_isolated(
            channel, user_id, user_name, group_context)
        saved_scope = (self._apply_run_scope(self.agent, run_agent)
                       if run_agent is not self.agent else None)

        tracked = channel != "cli"
        if tracked:
            model = ""
            try:
                client = getattr(run_agent, "client", None) or getattr(self.agent, "client", None)
                model = getattr(client, "model", "") or ""
            except Exception:
                model = ""
            register_run(session_id, channel, user_id, model=model)
        try:
            if channel == "cli":
                return await run_agent.run(
                    content, session_id=session_id,
                    user_id=user_id, user_name=user_name,
                    group_context=group_context,
                    user_department=user_department,
                    user_role=user_role,
                    **kwargs,
                )
            else:
                token = set_ask_user_mode("auto")
                try:
                    result = await run_agent.run(
                        content, session_id=session_id,
                        user_id=user_id, user_name=user_name,
                        group_context=group_context,
                        user_department=user_department,
                        user_role=user_role,
                        **kwargs,
                    )
                    # 渠道层需要结果元信息(如钉钉群敏感标记改道)时返回 AgentResult；
                    # 默认保持既有行为: 解包为最终回复文本字符串。
                    if return_result:
                        return result
                    return result.result if hasattr(result, "result") else str(result)
                finally:
                    reset_ask_user_mode(token)
        finally:
            if saved_scope is not None:
                self._restore_run_scope(run_agent, saved_scope)
            if release is not None:
                release()
            if tracked:
                unregister_run(session_id)
