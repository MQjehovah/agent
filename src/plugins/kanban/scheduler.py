import asyncio
import logging

logger = logging.getLogger("plugin.kanban.scheduler")


class KanbanScheduler:
    def __init__(
        self,
        board,
        agent,
        llm_client,
        poll_interval: int = 30,
        max_concurrent: int = 3,
        auto_assign: bool = True,
    ):
        self.board = board
        self.agent = agent
        self.llm_client = llm_client
        self.poll_interval = poll_interval
        self.max_concurrent = max_concurrent
        self.auto_assign = auto_assign
        self._running = False
        self._task: asyncio.Task | None = None
        self._running_jobs: dict[str, asyncio.Task] = {}

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "看板调度器已启动 (poll=%ds, max_concurrent=%d)",
            self.poll_interval,
            self.max_concurrent,
        )

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        for job_task in self._running_jobs.values():
            job_task.cancel()
        self._running_jobs.clear()
        logger.info("看板调度器已停止")

    async def _loop(self):
        await self._poll_once()
        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            try:
                await self._poll_once()
            except Exception:
                logger.exception("看板调度轮询异常")

    async def _poll_once(self):
        self._cleanup_finished_jobs()
        available = self.max_concurrent - len(self._running_jobs)
        if available <= 0:
            return

        due_tasks = self.board.get_due_tasks()
        if not due_tasks:
            return

        logger.info(
            "看板: %d 个任务待分配, %d 个执行槽位",
            len(due_tasks),
            available,
        )

        for task in due_tasks[:available]:
            assignee = "self"
            if self.auto_assign:
                assignee = await self._assign_task(task)
            await self._dispatch(task, assignee)

    async def _assign_task(self, task) -> str:
        subagent_names = []
        if self.agent.subagent_manager:
            subagent_names = self.agent.subagent_manager.list_templates()

        agents_list = ", ".join(subagent_names) + ", self(主agent自己执行)"
        prompt = (
            f"可用执行者: [{agents_list}]\n\n"
            f"任务标题: {task.title}\n"
            f"任务描述: {task.description or '(无)'}\n\n"
            f"请判断此任务应该分配给哪个执行者。只返回执行者名称，不要任何其他内容。"
        )

        try:
            response = await self.llm_client.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "你是任务分配助手。根据任务内容和可用执行者的能力匹配最合适的执行者。",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                stream=False,
                use_cache=False,
            )
            assignee = response.choices[0].message.content.strip()
            valid_names = set(subagent_names) | {"self"}
            if assignee not in valid_names:
                for name in subagent_names:
                    if name in assignee:
                        assignee = name
                        break
                else:
                    assignee = "self"
            logger.info("LLM 分配: '%s' → %s", task.title, assignee)
            return assignee
        except Exception:
            logger.exception("LLM 分配失败，使用 self")
            return "self"

    async def _dispatch(self, task, assignee: str):
        self.board.mark_started(task.id, assignee)
        job_task = asyncio.create_task(self._run_task(task, assignee))
        self._running_jobs[task.id] = job_task

    async def _run_task(self, task, assignee: str):
        task_desc = (
            f"{task.title}\n{task.description}" if task.description else task.title
        )
        try:
            agent_failed = False
            if assignee == "self":
                result = await self.agent.run(task_desc)
                result_text = (
                    result.result if hasattr(result, "result") else str(result)
                )
                if hasattr(result, "status") and result.status != "completed":
                    agent_failed = True
                if result_text and result_text.startswith("思考出错"):
                    agent_failed = True
            elif self.agent.subagent_manager:
                result = await self.agent.subagent_manager.run_subagent(
                    task=task_desc,
                    template=assignee,
                )
                result_text = (
                    result.result if hasattr(result, "result") else str(result)
                )
                if hasattr(result, "status") and result.status != "completed":
                    agent_failed = True
            else:
                result_text = f"子代理 {assignee} 不可用"
                self.board.mark_failed(task.id, result_text)
                return

            if agent_failed:
                self.board.mark_failed(task.id, result_text[:500])
                logger.error("看板任务失败: '%s' by %s", task.title, assignee)
            else:
                preview = result_text[:500] if result_text else "（无输出）"
                self.board.mark_completed(task.id, preview)
                logger.info("看板任务完成: '%s' by %s", task.title, assignee)
        except asyncio.CancelledError:
            self.board.mark_failed(task.id, "任务被取消")
        except Exception as e:
            logger.exception("看板任务失败: '%s'", task.title)
            self.board.mark_failed(task.id, str(e)[:500])
        finally:
            self._running_jobs.pop(task.id, None)

    def _cleanup_finished_jobs(self):
        done_ids = [
            tid for tid, jt in self._running_jobs.items() if jt.done()
        ]
        for tid in done_ids:
            self._running_jobs.pop(tid, None)

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "poll_interval": self.poll_interval,
            "max_concurrent": self.max_concurrent,
            "active_jobs": len(self._running_jobs),
            "active_task_ids": list(self._running_jobs.keys()),
        }
