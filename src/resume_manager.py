"""
会话恢复与目标管理 — /resume + /goal pause/resume

设计思路（参考 grok-build）：
- /resume [id]: 从历史会话恢复上下文，注入之前的对话摘要
- /goal pause: 暂停当前自治任务（保存中间状态）
- /goal resume: 从暂停点恢复执行
- /goal status: 查看当前目标进度
- /goal clear: 放弃当前目标

用法:
    rm = ResumeManager(storage, session_manager)
    context = await rm.resume_session("session_abc123")

    gm = GoalManager(storage)
    await gm.pause_goal("goal_001")
    await gm.resume_goal("goal_001")
"""
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("agent.resume")


class ResumeManager:
    """会话恢复管理器"""

    def __init__(self, storage=None, session_manager=None):
        self.storage = storage
        self.session_manager = session_manager

    async def resume_session(self, session_id: str) -> str:
        """恢复历史会话，返回上下文字符串供注入 prompt

        从 storage 恢复 session 消息，生成摘要供新 session 使用。
        """
        if not self.storage or not session_id:
            return ""

        messages = self.storage.get_messages(session_id)
        if not messages:
            return f"[session {session_id[:8]} 无消息]"

        # 生成摘要
        summary = self._summarize_messages(messages)
        meta = {}
        if hasattr(self.storage, "get_session_meta"):
            try:
                meta = self.storage.get_session_meta(session_id) or {}
            except Exception:
                pass

        last_summary = meta.get("last_summary", "")
        if last_summary:
            context = (
                f"[恢复会话 {session_id[:8]}]\n"
                f"最后摘要: {last_summary}\n"
                f"最近消息: {summary}"
            )
        else:
            context = (
                f"[恢复会话 {session_id[:8]}]\n"
                f"消息数: {len(messages)}\n"
                f"摘要: {summary}"
            )

        logger.info(f"[resume] 恢复 session {session_id[:8]}, {len(messages)} 条消息")
        return context

    async def list_sessions(self, limit: int = 20) -> list[dict]:
        """列出可恢复的会话"""
        if not self.storage:
            return []
        try:
            return self.storage.list_recent_sessions(limit=limit) or []
        except Exception as e:
            logger.warning(f"[resume] 列会话失败: {e}")
            return []

    @staticmethod
    def _summarize_messages(messages: list[dict], max_lines: int = 15) -> str:
        """生成会话摘要"""
        lines = []
        for msg in messages[-max_lines:]:
            role = msg.get("role", "?")
            content = msg.get("content", "") or ""
            if role == "system":
                continue
            if len(content) > 100:
                content = content[:100] + "..."
            lines.append(f"  [{role}] {content}")
        return "\n".join(lines)


class GoalLifecycle:
    """目标生命周期管理 — /goal pause/resume/status/clear"""

    STORAGE_KEY = "goal_lifecycle"

    def __init__(self, storage=None):
        self.storage = storage
        self._current_goal: Optional[dict] = None
        self._goal_history: list[dict] = []
        self._load()

    # ── 核心接口 ──

    async def pause(self, goal_id: str) -> bool:
        """暂停目标

        保存当前进度：已完成的步骤、中间结果、上下文快照。
        """
        goal = await self._find_goal(goal_id)
        if not goal:
            logger.warning(f"[goal] 未找到目标: {goal_id}")
            return False

        goal["status"] = "paused"
        goal["paused_at"] = time.time()
        goal["resume_count"] = goal.get("resume_count", 0) + 1

        self._save()
        logger.info(f"[goal] ⏸️ 已暂停: {goal_id} ({goal.get('title', '')[:40]})")
        return True

    async def resume(self, goal_id: str) -> Optional[dict]:
        """恢复目标

        Returns:
            目标的上下文（包括已完成步骤、中间结果），供 Agent 继续执行
        """
        goal = await self._find_goal(goal_id)
        if not goal:
            return None

        goal["status"] = "running"
        goal["resumed_at"] = time.time()

        self._save()
        logger.info(f"[goal] ▶️ 已恢复: {goal_id}")

        # 返回目标上下文
        completed_steps = [s for s in goal.get("steps", []) if s.get("status") == "completed"]
        pending_steps = [s for s in goal.get("steps", []) if s.get("status") != "completed"]

        return {
            "goal_id": goal_id,
            "title": goal.get("title", ""),
            "description": goal.get("description", ""),
            "progress": f"{len(completed_steps)}/{len(goal.get('steps', []))} 步骤完成",
            "completed_steps": completed_steps,
            "next_step": pending_steps[0] if pending_steps else None,
            "intermediate_results": goal.get("intermediate_results", {}),
            "paused_at": goal.get("paused_at"),
            "elapsed_s": int(time.time() - goal.get("started_at", time.time())),
        }

    async def create_goal(self, title: str, description: str = "", steps: list[dict] = None) -> str:
        """创建新目标"""
        goal_id = f"goal_{int(time.time())}"
        goal = {
            "id": goal_id,
            "title": title,
            "description": description,
            "steps": steps or [],
            "status": "running",
            "started_at": time.time(),
            "intermediate_results": {},
            "resume_count": 0,
        }
        self._goal_history.append(goal)
        self._current_goal = goal
        self._save()
        logger.info(f"[goal] 🎯 新目标: {goal_id} - {title[:60]}")
        return goal_id

    def get_status(self, goal_id: str = "") -> dict:
        """获取目标状态"""
        if goal_id:
            goal = next((g for g in self._goal_history if g["id"] == goal_id), None)
        else:
            goal = self._current_goal

        if not goal:
            return {"status": "idle", "message": "当前无活跃目标"}

        steps = goal.get("steps", [])
        completed = sum(1 for s in steps if s.get("status") == "completed")
        failed = sum(1 for s in steps if s.get("status") == "failed")

        return {
            "goal_id": goal["id"],
            "title": goal["title"],
            "status": goal["status"],
            "progress": f"{completed}/{len(steps)} 步骤",
            "completed": completed,
            "failed": failed,
            "total": len(steps),
            "elapsed_s": int(time.time() - goal.get("started_at", time.time())),
            "resume_count": goal.get("resume_count", 0),
            "paused": goal["status"] == "paused",
        }

    async def clear(self, goal_id: str = "") -> bool:
        """清除目标"""
        if goal_id:
            self._goal_history = [g for g in self._goal_history if g["id"] != goal_id]
        else:
            self._goal_history = []
            self._current_goal = None
        self._save()
        logger.info(f"[goal] 🗑️ 已清除目标: {goal_id or '全部'}")
        return True

    def get_history(self, limit: int = 10) -> list[dict]:
        """获取目标历史"""
        return [
            {
                "id": g["id"],
                "title": g["title"][:60],
                "status": g["status"],
                "steps": len(g.get("steps", [])),
                "elapsed_s": int(time.time() - g.get("started_at", time.time())),
                "resume_count": g.get("resume_count", 0),
            }
            for g in reversed(self._goal_history[-limit:])
        ]

    async def update_step(self, goal_id: str, step_index: int, status: str, result: str = ""):
        """更新步骤状态"""
        goal = await self._find_goal(goal_id)
        if not goal:
            return
        steps = goal.get("steps", [])
        if 0 <= step_index < len(steps):
            steps[step_index]["status"] = status
            if result:
                steps[step_index]["result"] = result[:500]
            # 保存中间结果
            goal["intermediate_results"][f"step_{step_index}"] = result[:500]
            self._save()

    # ── 内部 ──

    async def _find_goal(self, goal_id: str) -> Optional[dict]:
        for g in self._goal_history:
            if g["id"] == goal_id:
                return g
        # 尝试从 storage 加载
        if self.storage:
            try:
                goals = self.storage.get_data(self.STORAGE_KEY, [])
                for g in goals:
                    if g["id"] == goal_id:
                        self._goal_history.append(g)
                        return g
            except Exception:
                pass
        return None

    def _save(self):
        if not self.storage:
            return
        try:
            self.storage.set_data(self.STORAGE_KEY, self._goal_history[-50:])
        except Exception as e:
            logger.warning(f"[goal] 保存目标状态失败: {e}")

    def _load(self):
        if not self.storage:
            return
        try:
            data = self.storage.get_data(self.STORAGE_KEY, [])
            if data:
                self._goal_history = data
                # 找出最后一个 running 目标
                running = [g for g in data if g["status"] in ("running", "paused")]
                if running:
                    self._current_goal = running[-1]
                logger.info(f"[goal] 加载了 {len(data)} 个目标")
        except Exception as e:
            logger.debug(f"[goal] 加载目标失败: {e}")
