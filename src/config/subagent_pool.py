"""
子代理并发池配置模块

控制子代理并发池的最大并发数、队列大小、超时等参数
"""
import json
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SubagentPoolConfig:
    """子代理并发池配置"""
    max_concurrency: int = 3
    queue_size: int = 50
    timeout: int = 300
    enable_priority: bool = True

    @classmethod
    def load(cls, workspace: str) -> "SubagentPoolConfig":
        """
        从工作目录加载配置文件

        Args:
            workspace: 工作目录路径

        Returns:
            SubagentPoolConfig 实例
        """
        config_file = os.path.join(workspace, "subagent_pool.json")
        if os.path.exists(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return cls(**{k: v for k, v in data.items() if hasattr(cls, k)})
            except Exception:
                pass
        return cls()
