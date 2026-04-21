"""
配置验证模块

在启动时验证必要的配置是否完整
"""
import os
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger("agent.config")


class ConfigValidator:
    """配置验证器"""

    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate_required(self, key: str, description: str = "") -> bool:
        """验证必需的配置项"""
        value = os.getenv(key)
        if not value:
            self.errors.append(f"缺少必需配置: {key}" + (f" ({description})" if description else ""))
            return False
        return True

    def validate_optional(self, key: str, default: str = None, description: str = "") -> Optional[str]:
        """验证可选配置项"""
        value = os.getenv(key)
        if not value:
            msg = f"使用默认配置: {key}={default}" + (f" ({description})" if description else "")
            self.warnings.append(msg)
            return default
        return value

    def validate_url(self, key: str, description: str = "") -> bool:
        """验证 URL 格式配置"""
        value = os.getenv(key)
        if not value:
            self.errors.append(f"缺少必需配置: {key}" + (f" ({description})" if description else ""))
            return False
        if not value.startswith(("http://", "https://", "ws://", "wss://")):
            self.errors.append(f"配置 {key} 不是有效的 URL: {value}")
            return False
        return True

    def get_report(self) -> Dict[str, Any]:
        """获取验证报告"""
        return {
            "valid": len(self.errors) == 0,
            "errors": self.errors,
            "warnings": self.warnings
        }


def validate_config() -> bool:
    """
    验证系统配置

    Returns:
        配置是否有效
    """
    validator = ConfigValidator()

    # 必需配置
    validator.validate_required("OPENAI_API_KEY", "LLM API 密钥")

    # 可选配置（带默认值）
    validator.validate_optional("OPENAI_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1", "LLM API 地址")
    validator.validate_optional("DEVICE_API_BASE_URL", "https://bms-cn.rosiwit.com", "设备运维 API 地址")
    validator.validate_optional("SESSION_TTL_SECONDS", "3600", "会话过期时间")
    validator.validate_optional("MAX_SESSIONS", "100", "最大会话数")
    validator.validate_optional("CACHE_MAX_SIZE", "1000", "缓存大小")
    validator.validate_optional("CACHE_TTL_SECONDS", "3600", "缓存过期时间")
    validator.validate_optional("LLM_TIMEOUT", "300", "LLM 请求超时（秒）")
    validator.validate_optional("LLM_CONNECT_TIMEOUT", "30", "LLM 连接超时（秒）")
    validator.validate_optional("LOG_LEVEL", "INFO", "日志级别")

    report = validator.get_report()

    # 输出警告
    for warning in report["warnings"]:
        logger.warning(warning)

    # 输出错误
    for error in report["errors"]:
        logger.error(error)

    if not report["valid"]:
        logger.error("配置验证失败，请检查 .env 文件或环境变量")
        logger.info(f"请复制 .env.example 为 .env 并填入实际配置值")
        return False

    logger.info("读取配置成功")
    return True


def get_config_value(key: str, default: Any = None, converter: type = str) -> Any:
    """
    获取配置值

    Args:
        key: 配置键名
        default: 默认值
        converter: 类型转换器

    Returns:
        配置值
    """
    value = os.getenv(key)
    if value is None:
        return default

    try:
        return converter(value)
    except (ValueError, TypeError):
        logger.warning(f"配置 {key} 值 '{value}' 无法转换为 {converter.__name__}，使用默认值: {default}")
        return default


# 配置常量
class Config:
    """配置常量"""

    # 会话配置
    SESSION_TTL_SECONDS: int = 3600
    MAX_SESSIONS: int = 100

    # 缓存配置
    CACHE_MAX_SIZE: int = 1000
    CACHE_TTL_SECONDS: int = 3600

    # LLM 配置
    LLM_TIMEOUT: float = 300.0
    LLM_CONNECT_TIMEOUT: float = 30.0

    # 日志级别
    LOG_LEVEL: str = "INFO"

    @classmethod
    def load_from_env(cls):
        """从环境变量加载配置"""
        cls.SESSION_TTL_SECONDS = get_config_value("SESSION_TTL_SECONDS", 3600, int)
        cls.MAX_SESSIONS = get_config_value("MAX_SESSIONS", 100, int)
        cls.CACHE_MAX_SIZE = get_config_value("CACHE_MAX_SIZE", 1000, int)
        cls.CACHE_TTL_SECONDS = get_config_value("CACHE_TTL_SECONDS", 3600, int)
        cls.LLM_TIMEOUT = get_config_value("LLM_TIMEOUT", 300, float)
        cls.LLM_CONNECT_TIMEOUT = get_config_value("LLM_CONNECT_TIMEOUT", 30, float)
        cls.LOG_LEVEL = get_config_value("LOG_LEVEL", "INFO", str)