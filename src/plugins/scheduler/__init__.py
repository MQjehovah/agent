import asyncio
import json
import logging
import os
import uuid
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from plugins.base import BasePlugin

logger = logging.getLogger("plugin.scheduler")


class SchedulerPlugin(BasePlugin):
    name = "scheduler"
    description = "定时任务插件，基于cron表达式调度任务执行，支持LLM动态创建与管理"
    version = "1.1.0"

    def _load_config(self):
        config_file = self.config_path
        if not config_file:
            config_file = os.path.join(
                self.config_dir or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "plugins", "schedules.json"
            )

        self.schedules: list[dict] = []
        self.scheduler: AsyncIOScheduler | None = None
        self._started = False
        self._agent_executor = None
        self._config_file = config_file

        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as f:
                    data = json.load(f)
                if not data.get("enabled", True):
                    self.enabled = False
                    return
                all_schedules = data.get("schedules", data) if isinstance(data, dict) else data
                # 静态任务打标 static=True，来自配置文件
                self.schedules = [
                    {**s, "static": True}
                    for s in all_schedules if s.get("enabled", True)
                ]
                logger.info(f"已加载 {len(self.schedules)} 个静态定时任务")
            except Exception as e:
                logger.error(f"加载定时任务配置失败: {e}")
        else:
            logger.warning(f"定时任务配置文件不存在: {config_file}")

        self.enabled = True

    # ── 数据库任务（LLM 动态创建） ─────────────────────────────

    def _db(self):
        from storage.storage import get_storage
        return get_storage()

    def list_db_tasks(self, user_id: str = "") -> list[dict]:
        """列出数据库定时任务。user_id 为空返回全部（管理员），否则只返回该用户。"""
        db = self._db()
        if not db:
            return []
        try:
            with db.get_connection() as conn:
                if user_id:
                    rows = conn.execute(
                        "SELECT * FROM scheduled_tasks WHERE user_id = ? ORDER BY created_at DESC",
                        (user_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM scheduled_tasks ORDER BY created_at DESC"
                    ).fetchall()
                cols = [d[0] for d in conn.execute(
                    "SELECT * FROM scheduled_tasks LIMIT 0").description]
                return [dict(zip(cols, r, strict=False)) for r in rows]
        except Exception as e:
            logger.error(f"查询定时任务失败: {e}")
            return []

    def create_db_task(self, name: str, cron: str, task: str, user_id: str = "",
                       user_name: str = "", session_id: str = "") -> dict:
        """创建数据库定时任务。"""
        now = datetime.now().isoformat(timespec="seconds")
        task_id = uuid.uuid4().hex[:12]
        rec = {
            "id": task_id,
            "name": name,
            "cron": cron,
            "task": task,
            "user_id": user_id,
            "user_name": user_name,
            "session_id": session_id,
            "enabled": 1,
            "last_run_at": None,
            "last_result": None,
            "last_error": None,
            "run_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        db = self._db()
        if db:
            try:
                with db.get_connection() as conn:
                    conn.execute(
                        """INSERT INTO scheduled_tasks
                           (id, name, cron, task, user_id, user_name, session_id,
                            enabled, last_run_at, last_result, last_error, run_count,
                            created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        tuple(rec[k] for k in (
                            "id", "name", "cron", "task", "user_id", "user_name",
                            "session_id", "enabled", "last_run_at", "last_result",
                            "last_error", "run_count", "created_at", "updated_at")),
                    )
                    conn.commit()
            except Exception as e:
                logger.error(f"创建定时任务失败: {e}")
                raise
        return rec

    def update_db_task(self, task_id: str, name: str = None, cron: str = None,
                       task: str = None, enabled: int = None) -> dict | None:
        """更新数据库定时任务。"""
        db = self._db()
        if not db:
            return None
        try:
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if not row:
                    return None
                cols = [d[0] for d in conn.execute(
                    "SELECT * FROM scheduled_tasks LIMIT 0").description]
                rec = dict(zip(cols, row, strict=False))
                updates = {}
                if name is not None:
                    updates["name"] = name
                if cron is not None:
                    updates["cron"] = cron
                if task is not None:
                    updates["task"] = task
                if enabled is not None:
                    updates["enabled"] = 1 if enabled else 0
                if not updates:
                    return rec
                updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE scheduled_tasks SET {set_clause} WHERE id = ?",
                    (*updates.values(), task_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
                ).fetchone()
                cols = [d[0] for d in conn.execute(
                    "SELECT * FROM scheduled_tasks LIMIT 0").description]
                return dict(zip(cols, row, strict=False))
        except Exception as e:
            logger.error(f"更新定时任务失败: {e}")
            return None

    def delete_db_task(self, task_id: str) -> bool:
        db = self._db()
        if not db:
            return False
        try:
            with db.get_connection() as conn:
                cur = conn.execute(
                    "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.error(f"删除定时任务失败: {e}")
            return False

    async def _push_result(self, schedule: dict, result: str):
        """定时任务执行完成后，将结果自动推回原会话渠道。

        定时任务由 cron 触发，没有 incoming_message，无法走钉钉/飞书的
        reply 链路。这里从 session_id 解析渠道前缀，调用对应插件的主动
        发送能力，把执行结果直接送回创建者的会话。
        """
        sid = schedule.get("session_id", "") or ""
        if not sid or not result:
            return
        channel = sid.split(":", 1)[0]
        pm = self.plugin_manager
        if not pm:
            return
        try:
            if channel == "dingtalk":
                dt = pm.get_plugin("dingtalk")
                if dt and hasattr(dt, "_send_text"):
                    uid = schedule.get("user_id", "") or sid
                    await dt._send_text(result[:2000], local_user_id=uid)
                    logger.info(f"定时任务结果已推送钉钉: {sid}")
            elif channel == "feishu":
                fs = pm.get_plugin("feishu")
                if fs and getattr(fs, "_client", None):
                    parts = sid.split(":")
                    chat_id = parts[1] if len(parts) > 1 else ""
                    if chat_id:
                        await fs._client.send_text_message(chat_id, result[:4000])
                        logger.info(f"定时任务结果已推送飞书: {sid}")
            elif channel == "web":
                ws = pm.get_plugin("web") or pm.get_plugin("webui")
                if ws and hasattr(ws, "push_to_user"):
                    await ws.push_to_user(sid, result[:4000])
        except Exception as e:
            logger.warning(f"定时任务结果推送失败: {channel}:{sid}, {e!r}")

    def record_run(self, task_id: str, ok: bool, result: str):
        db = self._db()
        if not db:
            return
        try:
            now = datetime.now().isoformat(timespec="seconds")
            with db.get_connection() as conn:
                if ok:
                    conn.execute(
                        """UPDATE scheduled_tasks
                           SET last_run_at = ?, last_result = ?, last_error = NULL,
                               run_count = run_count + 1
                           WHERE id = ?""",
                        (now, result[:2000], task_id),
                    )
                else:
                    conn.execute(
                        """UPDATE scheduled_tasks
                           SET last_run_at = ?, last_result = NULL, last_error = ?
                           WHERE id = ?""",
                        (now, result[:2000], task_id),
                    )
                conn.commit()
        except Exception as e:
            logger.error(f"记录定时任务运行状态失败: {e}")

    # ── 调度器 ────────────────────────────────────────────────

    def _ensure_schema(self):
        """旧库表迁移：补充新增列（session_id）。"""
        db = self._db()
        if not db:
            return
        try:
            with db.get_connection() as conn:
                cols = [d[1] for d in conn.execute(
                    "PRAGMA table_info(scheduled_tasks)")]
                if cols and "session_id" not in cols:
                    conn.execute(
                        "ALTER TABLE scheduled_tasks ADD COLUMN session_id TEXT DEFAULT ''")
                    conn.commit()
                    logger.info("已迁移 scheduled_tasks 表：新增 session_id 列")
        except Exception as e:
            logger.warning(f"scheduled_tasks 表迁移跳过: {e}")

    def _all_tasks(self) -> list[dict]:
        """合并静态 + 数据库任务。"""
        db_tasks = self.list_db_tasks()
        return self.schedules + db_tasks

    def start(self):
        self._ensure_schema()
        self.stop()
        self.scheduler = AsyncIOScheduler()
        registered = 0

        for schedule in self._all_tasks():
            name = schedule.get("name", "未命名")
            cron = schedule.get("cron", "")
            task_id = schedule.get("id", "")
            try:
                trigger = CronTrigger.from_crontab(cron)
                self.scheduler.add_job(
                    self._execute_task,
                    trigger=trigger,
                    args=[schedule],
                    name=f"{name}::{task_id}" if task_id else name,
                    id=task_id or None,
                )
                registered += 1
                logger.debug(f"注册定时任务: {name} ({cron})")
            except Exception as e:
                logger.error(f"注册定时任务失败: {name}, 错误: {e}")

        if registered:
            self.scheduler.start()
            self._started = True
            logger.info(f"定时任务调度器已启动，共 {registered} 个任务")
            for job in self.scheduler.get_jobs():
                logger.info(f"  - {job.name}: 下次执行 {job.next_run_time}")

    def stop(self):
        if self.scheduler and self._started:
            self.scheduler.shutdown()
            self._started = False
            logger.info("定时任务调度器已停止")
        self.scheduler = None

    def reload(self):
        """重新加载全部任务（增删改后调用）。"""
        logger.info("重新加载定时任务...")
        self.start()

    # ── 任务执行 ──────────────────────────────────────────────

    async def _execute_task(self, schedule: dict):
        name = schedule.get("name", "未命名任务")
        task = schedule.get("task", "")
        task_id = schedule.get("id", "")
        user_id = schedule.get("user_id", "")
        user_name = schedule.get("user_name", "")

        logger.info(f"⏰ 触发定时任务: {name}")
        logger.info(f"   任务内容: {task}")

        if not self._agent_executor:
            logger.error("未注册 agent 执行器")
            return

        try:
            # 以创建者身份 + 原会话执行：agent_executor 接受 user_id/user_name/schedule 恢复身份
            result = await self._agent_executor(
                task, user_id=user_id, user_name=user_name, schedule=schedule)
            ok = not (isinstance(result, str) and ("失败" in result[:200] or "错误" in result[:200]))
            if task_id:
                self.record_run(task_id, ok, str(result))
            logger.info(f"✓ 定时任务完成: {name}")
            logger.debug(f"结果: {result}")
            # 框架自动把结果推回原会话渠道（定时任务无 incoming_message，无法走 reply 链路）
            await self._push_result(schedule, str(result))
        except asyncio.CancelledError:
            if task_id:
                self.record_run(task_id, False, "任务被取消")
            logger.info(f"定时任务被取消: {name}")
        except Exception as e:
            if task_id:
                self.record_run(task_id, False, str(e))
            logger.error(f"✗ 定时任务失败: {name}, 错误: {e}", exc_info=True)

    # ── LLM 工具 ──────────────────────────────────────────────

    def get_tool_defs(self) -> list[dict]:
        if not self.enabled:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": "scheduler_create",
                    "description": (
                        "创建定时任务。用 cron 表达式（5字段: 分 时 日 月 周，"
                        "如 '0 9 * * *' 每天9点、'0 10 * * 0' 每周日10点）"
                        "周期性让 agent 执行一段任务内容。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "任务名称"},
                            "cron": {"type": "string", "description": "cron 表达式（5字段）"},
                            "task": {"type": "string", "description": "到点要执行的任务内容描述"},
                        },
                        "required": ["name", "cron", "task"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "scheduler_list",
                    "description": "列出定时任务（含 cron、启停状态、最近执行结果）。",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "scheduler_update",
                    "description": "更新定时任务的 cron 表达式、内容或启停状态。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string", "description": "任务ID"},
                            "name": {"type": "string", "description": "新名称"},
                            "cron": {"type": "string", "description": "新 cron 表达式"},
                            "task": {"type": "string", "description": "新任务内容"},
                            "enabled": {"type": "boolean", "description": "是否启用"},
                        },
                        "required": ["task_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "scheduler_delete",
                    "description": "删除定时任务。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string", "description": "任务ID"},
                        },
                        "required": ["task_id"],
                    },
                },
            },
        ]

    async def execute_tool(self, name: str, args: dict) -> str:
        # 用户隔离：_local_user_id 由 agent.executor 注入
        current_uid = args.get("_local_user_id", "")
        current_sid = args.get("_local_session_id", "")
        args.pop("_local_user_id", None)
        args.pop("_local_session_id", None)

        if name == "scheduler_create":
            cron = args.get("cron", "")
            try:
                CronTrigger.from_crontab(cron)
            except Exception as e:
                return json.dumps(
                    {"success": False, "error": f"cron 表达式无效: {e}"},
                    ensure_ascii=False,
                )
            rec = self.create_db_task(
                name=args.get("name", "未命名"),
                cron=cron,
                task=args.get("task", ""),
                user_id=current_uid,
                session_id=current_sid,
            )
            self.reload()
            return json.dumps({"success": True, "task": rec}, ensure_ascii=False)

        if name == "scheduler_list":
            tasks = self.list_db_tasks(user_id=current_uid)
            # 用户只看到自己的 + 无主的静态任务
            visible = self.schedules + tasks
            return json.dumps(
                {
                    "success": True,
                    "count": len(visible),
                    "tasks": [{
                        "id": t.get("id", ""),
                        "name": t.get("name", ""),
                        "cron": t.get("cron", ""),
                        "task": t.get("task", ""),
                        "enabled": bool(t.get("enabled", t.get("static", True))),
                        "static": t.get("static", False),
                        "user_id": t.get("user_id", ""),
                        "session_id": t.get("session_id", ""),
                        "last_run_at": t.get("last_run_at"),
                        "last_result": t.get("last_result"),
                        "last_error": t.get("last_error"),
                        "run_count": t.get("run_count", 0),
                    } for t in visible],
                },
                ensure_ascii=False,
            )

        if name == "scheduler_update":
            task_id = args.get("task_id", "")
            rec = self.update_db_task(
                task_id,
                name=args.get("name"),
                cron=args.get("cron"),
                task=args.get("task"),
                enabled=args.get("enabled") is not None and (1 if args["enabled"] else 0),
            )
            if rec is None:
                return json.dumps(
                    {"success": False, "error": "任务不存在或无权修改"},
                    ensure_ascii=False,
                )
            self.reload()
            return json.dumps({"success": True, "task": rec}, ensure_ascii=False)

        if name == "scheduler_delete":
            ok = self.delete_db_task(args.get("task_id", ""))
            if ok:
                self.reload()
            return json.dumps({"success": ok}, ensure_ascii=False)

        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


plugin = SchedulerPlugin
