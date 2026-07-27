import asyncio
import json
import logging
import os
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from plugins.base import BasePlugin

logger = logging.getLogger("plugin.scheduler")


class SchedulerPlugin(BasePlugin):
    name = "scheduler"
    description = "定时任务插件，基于cron表达式调度任务执行"
    version = "1.0.0"

    def _load_config(self):
        config_file = self.config_path
        if not config_file:
            config_file = os.path.join(
                self.config_dir or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "plugins", "schedules.json"
            )

        self.schedules: list[dict] = []
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._started = False
        self._agent_executor = None

        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as f:
                    data = json.load(f)
                if not data.get("enabled", True):
                    self.enabled = False
                    return
                all_schedules = data.get("schedules", data) if isinstance(data, dict) else data
                self.schedules = [s for s in all_schedules if s.get("enabled", True)]
                logger.info(f"已加载 {len(self.schedules)} 个定时任务")
            except Exception as e:
                logger.error(f"加载定时任务配置失败: {e}")
        else:
            logger.warning(f"定时任务配置文件不存在: {config_file}")

        self.enabled = bool(self.schedules)

    def start(self):
        if not self.schedules:
            return

        self.scheduler = AsyncIOScheduler()

        for schedule in self.schedules:
            name = schedule.get("name", "未命名")
            cron = schedule.get("cron", "")
            try:
                trigger = CronTrigger.from_crontab(cron)
                self.scheduler.add_job(
                    self._execute_task,
                    trigger=trigger,
                    args=[schedule],
                    name=name,
                )
                logger.debug(f"注册定时任务: {name} ({cron})")
            except Exception as e:
                logger.error(f"注册定时任务失败: {name}, 错误: {e}")

        if self.scheduler.get_jobs():
            self.scheduler.start()
            self._started = True
            logger.info(f"定时任务调度器已启动，共 {len(self.scheduler.get_jobs())} 个任务")
            for job in self.scheduler.get_jobs():
                logger.info(f"  - {job.name}: 下次执行 {job.next_run_time}")

    def stop(self):
        if self.scheduler and self._started:
            self.scheduler.shutdown()
            self._started = False
            logger.info("定时任务调度器已停止")

    async def _execute_task(self, schedule: dict):
        name = schedule.get("name", "未命名任务")
        task = schedule.get("task", "")

        logger.info(f"⏰ 触发定时任务: {name}")
        logger.info(f"   任务内容: {task}")

        if not self._agent_executor:
            logger.error("未注册 agent 执行器")
            return

        try:
            result = await self._agent_executor(task)
            logger.info(f"✓ 定时任务完成: {name}")
            logger.debug(f"结果: {result}")
        except asyncio.CancelledError:
            logger.info(f"定时任务被取消: {name}")
        except Exception as e:
            logger.error(f"✗ 定时任务失败: {name}, 错误: {e}", exc_info=True)


plugin = SchedulerPlugin
