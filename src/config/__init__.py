"""
配置模块

包含配置验证和子代理并发池配置
"""
from .validator import ConfigValidator, validate_config, get_config_value, Config
from .subagent_pool import SubagentPoolConfig

__all__ = [
    "ConfigValidator",
    "validate_config",
    "get_config_value",
    "Config",
    "SubagentPoolConfig",
]
