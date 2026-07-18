import json
import logging
import os

from plugins.base import BasePlugin

from .board import KanbanBoard
from .scheduler import KanbanScheduler

logger = logging.getLogger("plugin.kanban")


class KanbanPlugin(BasePlugin):
    name = "kanban"
    description = "Kanban 看板插件，自动监控看板任务并分配给子 agent 并发执行"
    version = "1.0.0"

    def _load_config(self):
        config_file = self.config_path
        if not config_file:
            config_file = os.path.join(
                self.config_dir or ".", "plugins", "kanban.json"
            )

        self._config = {
            "enabled": True,
            "poll_interval": 30,
            "max_concurrent": 3,
            "auto_assign": True,
            "columns": ["backlog", "todo", "in_progress", "done"],
        }

        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as f:
                    data = json.load(f)
                if not data.get("enabled", True):
                    self.enabled = False
                    return
                self._config.update(
                    {k: v for k, v in data.items() if k != "enabled"}
                )
                logger.debug("看板配置已加载: %s", config_file)
            except Exception as e:
                logger.error("加载看板配置失败: %s", e)

        self.enabled = True
        self.board: KanbanBoard | None = None
        self.scheduler: KanbanScheduler | None = None
        self._agent = None

    def start(self):
        config_dir = self.config_dir or "."
        panel_path = os.path.join(config_dir, "task_panel.json")

        from storage.storage import get_storage
        self.board = KanbanBoard(storage=get_storage())
        self.board.migrate_from_panel(panel_path)

        if self._agent and self.board:
            self.scheduler = KanbanScheduler(
                board=self.board,
                agent=self._agent,
                llm_client=self._agent.client,
                poll_interval=self._config["poll_interval"],
                max_concurrent=self._config["max_concurrent"],
                auto_assign=self._config["auto_assign"],
            )
            self.scheduler.start()

        logger.info("看板插件已启动")

    def stop(self):
        if self.scheduler:
            self.scheduler.stop()
        logger.info("看板插件已停止")

    def set_agent(self, agent):
        self._agent = agent

    def get_board(self) -> KanbanBoard | None:
        return self.board

    def get_tool_defs(self) -> list[dict]:
        if not self.enabled:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": "kanban_add",
                    "description": (
                        "在看板上创建一个新任务。"
                        "任务会进入 Backlog 或 Todo 列。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "任务标题",
                            },
                            "description": {
                                "type": "string",
                                "description": "任务详细描述",
                            },
                            "priority": {
                                "type": "integer",
                                "description": "优先级: 1=高 2=中 3=低",
                                "default": 3,
                            },
                            "column": {
                                "type": "string",
                                "description": "初始列: backlog/todo",
                                "default": "backlog",
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "标签列表",
                            },
                            "interval": {
                                "type": "integer",
                                "description": "重复间隔(秒)，null=一次性",
                            },
                        },
                        "required": ["title"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "kanban_list",
                    "description": (
                        "列出看板上的任务，支持按列、来源、执行者筛选。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "column": {
                                "type": "string",
                                "description": (
                                    "按列筛选: "
                                    "backlog/todo/in_progress/done"
                                ),
                            },
                            "source": {
                                "type": "string",
                                "description": (
                                    "按来源筛选: "
                                    "user/llm/event/scheduler"
                                ),
                            },
                            "assignee": {
                                "type": "string",
                                "description": "按执行者筛选",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "kanban_move",
                    "description": "移动任务到指定列。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "任务ID",
                            },
                            "column": {
                                "type": "string",
                                "description": (
                                    "目标列: "
                                    "backlog/todo/in_progress/done"
                                ),
                            },
                        },
                        "required": ["task_id", "column"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "kanban_assign",
                    "description": "为任务指定执行者。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "任务ID",
                            },
                            "assignee": {
                                "type": "string",
                                "description": (
                                    "执行者名称（子 agent 名或 self）"
                                ),
                            },
                        },
                        "required": ["task_id", "assignee"],
                    },
                },
            },
        ]

    async def execute_tool(self, name: str, args: dict) -> str:
        if not self.board:
            return json.dumps(
                {"error": "看板未初始化"}, ensure_ascii=False
            )

        if name == "kanban_add":
            task = self.board.add_task(
                title=args["title"],
                description=args.get("description", ""),
                priority=args.get("priority", 3),
                column=args.get("column", "backlog"),
                source="llm",
                tags=args.get("tags"),
                interval=args.get("interval"),
            )
            return json.dumps(
                {"success": True, "task": task.to_dict()},
                ensure_ascii=False,
            )

        if name == "kanban_list":
            tasks = self.board.list_tasks(
                column=args.get("column"),
                source=args.get("source"),
                assignee=args.get("assignee"),
            )
            return json.dumps(
                {
                    "success": True,
                    "count": len(tasks),
                    "tasks": [t.to_dict() for t in tasks],
                },
                ensure_ascii=False,
            )

        if name == "kanban_move":
            ok = self.board.move_task(args["task_id"], args["column"])
            return json.dumps({"success": ok}, ensure_ascii=False)

        if name == "kanban_assign":
            task = self.board.get_task(args["task_id"])
            if not task:
                return json.dumps(
                    {"error": "任务不存在"}, ensure_ascii=False
                )
            ok = self.board.move_task(
                task.id, task.column, assignee=args["assignee"]
            )
            return json.dumps({"success": ok}, ensure_ascii=False)

        return json.dumps(
            {"error": f"未知工具: {name}"}, ensure_ascii=False
        )


plugin = KanbanPlugin
