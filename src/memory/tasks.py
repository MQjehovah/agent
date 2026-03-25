import logging
from datetime import datetime

logger = logging.getLogger("agent.memory")


def create_archive_task_func(memory_dir: str, llm_client=None):
    """
    创建每日归档任务函数
    
    Args:
        memory_dir: 记忆目录路径
        llm_client: LLM客户端（可选）
    
    Returns:
        可执行的归档任务函数
    """
    from .archiver import MemoryArchiver
    
    async def archive_task():
        archiver = MemoryArchiver(memory_dir, llm_client)
        archiver.archive_daily_to_long_term(days_threshold=1)
        archiver.cleanup_old_sessions(retention_days=7)
        logger.info("Daily archive task completed")
    
    return archive_task


def get_archive_schedule_config():
    """
    获取归档任务的调度配置
    
    Returns:
        定时任务配置字典
    """
    return {
        "name": "每日记忆归档",
        "task": "__archive_memory__",
        "cron": "0 0 * * *",
        "enabled": True,
        "description": "每天凌晨执行，将昨日的每日记忆归档到长期记忆"
    }