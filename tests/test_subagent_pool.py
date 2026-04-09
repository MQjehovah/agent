import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from subagent_manager import SubagentManager, SubagentTask, TaskStatus


@pytest.fixture
def manager(tmp_path):
    """创建测试用的 SubagentManager"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    config = workspace / "subagent_pool.json"
    config.write_text('{"max_concurrency": 2, "queue_size": 10}')
    
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    
    manager = SubagentManager(str(agents_dir), workspace=str(workspace))
    manager._client = AsyncMock()
    manager._parent_agent = MagicMock()
    
    return manager


@pytest.mark.asyncio
async def test_pool_initialization(manager):
    """测试并发池初始化"""
    stats = manager.get_pool_stats()
    assert stats["max_concurrency"] == 2
    assert stats["active"] == 0
    assert stats["pending"] == 0


@pytest.mark.asyncio
async def test_concurrent_task_submission(manager):
    """测试并发任务提交"""
    task_id = await manager.run_subagent_concurrent(
        task="test task",
        template="test",
        priority=1
    )
    
    assert task_id.startswith("task_")
    
    status = manager.get_task_status(task_id)
    assert status is not None
    assert status["status"] in ["pending", "running"]


@pytest.mark.asyncio
async def test_task_status_tracking(manager):
    """测试任务状态追踪"""
    task_id = await manager.run_subagent_concurrent(
        task="status test",
        template="test"
    )
    
    status = manager.get_task_status(task_id)
    assert "task_id" in status
    assert "status" in status
    assert "created_at" in status


@pytest.mark.asyncio
async def test_priority_queue(manager):
    """测试优先级队列"""
    low_id = await manager.run_subagent_concurrent(
        task="low priority",
        template="test",
        priority=-1
    )
    
    high_id = await manager.run_subagent_concurrent(
        task="high priority",
        template="test",
        priority=1
    )
    
    high_status = manager.get_task_status(high_id)
    low_status = manager.get_task_status(low_id)
    
    assert high_status["priority"] == 1
    assert low_status["priority"] == -1


@pytest.mark.asyncio
async def test_get_all_tasks(manager):
    """测试获取所有任务"""
    for i in range(3):
        await manager.run_subagent_concurrent(
            task=f"task {i}",
            template="test"
        )
    
    all_tasks = manager.get_all_tasks()
    assert len(all_tasks) == 3
    
    running_tasks = manager.get_all_tasks(status_filter=TaskStatus.RUNNING)
    assert isinstance(running_tasks, list)


@pytest.mark.asyncio
async def test_pool_stats_after_submission(manager):
    """测试提交任务后的池统计"""
    for i in range(5):
        await manager.run_subagent_concurrent(
            task=f"task {i}",
            template="test"
        )
    
    stats = manager.get_pool_stats()
    assert stats["total"] == 5
    assert stats["active"] <= 2
