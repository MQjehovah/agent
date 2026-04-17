import os
import json
import logging
from typing import Optional, Dict, Any, Callable, Awaitable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("agent.scheduler")


class SchedulerManager:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._started = False
        self._task_executor: Optional[Callable[[str], Awaitable[Any]]] = None

    def set_executor(self, executor: Callable[[str], Awaitable[Any]]):
        self._task_executor = executor

    def load_schedules(self) -> list:
        if not os.path.exists(self.config_path):
            logger.warning(f"未找到配置文件: {self.config_path}")
            return []

        with open(self.config_path, encoding="utf-8") as f:
            schedules = json.load(f)

        enabled_schedules = [s for s in schedules if s.get("enabled", True)]
        logger.info(f"已加载 {len(enabled_schedules)} 个定时任务")
        return enabled_schedules

    async def _execute_task(self, schedule: Dict):
        name = schedule.get("name", "未命名任务")
        task = schedule.get("task", "")

        logger.info(f"⏰ 触发定时任务: {name}")
        logger.info(f"   任务内容: {task}")

        if not self._task_executor:
            logger.error("未设置任务执行器")
            return

        try:
            result = await self._task_executor(task)
            logger.info(f"✓ 定时任务完成: {name}")
            logger.debug(f"结果: {result}")
        except asyncio.CancelledError:
            logger.info(f"定时任务被取消: {name}")
        except Exception as e:
            logger.error(f"✗ 定时任务失败: {name}, 错误: {e}", exc_info=True)

    def start(self):
        self.scheduler = AsyncIOScheduler()
        schedules = self.load_schedules()

        for schedule in schedules:
            name = schedule.get("name", "未命名")
            cron = schedule.get("cron", "")

            try:
                trigger = CronTrigger.from_crontab(cron)
                self.scheduler.add_job(
                    self._execute_task,
                    trigger=trigger,
                    args=[schedule],
                    name=name
                )
                logger.debug(f"注册定时任务: {name} ({cron})")
            except Exception as e:
                logger.error(f"✗ 注册定时任务失败: {name}, 错误: {e}")

        if self.scheduler.get_jobs():
            self.scheduler.start()
            self._started = True
            logger.info(f"定时任务调度器已启动，共 {len(self.scheduler.get_jobs())} 个任务")
            for job in self.scheduler.get_jobs():
                logger.info(f"  - {job.name}: 下次执行 {job.next_run_time}")
        else:
            logger.warning("没有可执行的定时任务")

    def stop(self):
        if self.scheduler and self._started:
            self.scheduler.shutdown()
            self._started = False
            logger.info("定时任务调度器已停止")