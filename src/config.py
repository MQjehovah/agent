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
    validator.validate_optional("AUTONOMOUS_DISCOVERY_INTERVAL", "1800", "自主巡检间隔（秒）")

    searxng_url = os.getenv("SEARXNG_URL", "")
    if searxng_url:
        if not searxng_url.startswith(("http://", "https://")):
            validator.errors.append(f"SEARXNG_URL 不是有效的 URL: {searxng_url}")
        else:
            logger.info(f"SearXNG 搜索已启用: {searxng_url}")

    if os.getenv("TAVILY_API_KEY"):
        logger.info("Tavily 搜索已启用")
    if os.getenv("SERPER_API_KEY"):
        logger.info("Serper 搜索已启用")
    if os.getenv("BING_SEARCH_API_KEY"):
        logger.info("Bing Search API 已启用")

    if not any([searxng_url, os.getenv("TAVILY_API_KEY"),
                os.getenv("SERPER_API_KEY"), os.getenv("BING_SEARCH_API_KEY")]):
        logger.info("所有搜索 API 均未配置，搜索将使用 DuckDuckGo（稳定性较低）")

    validator.validate_optional("SEARCH_BACKENDS", "", "搜索后端优先级")
    validator.validate_optional("TAVILY_API_KEY", "", "Tavily API 密钥")
    validator.validate_optional("SERPER_API_KEY", "", "Serper API 密钥")
    validator.validate_optional("BING_SEARCH_API_KEY", "", "Bing Search API 密钥")
    validator.validate_optional("SEARXNG_URL", "", "SearXNG 实例地址")
    validator.validate_optional("SEARXNG_ENGINES", "google,bing,duckduckgo", "SearXNG 搜索引擎")
    validator.validate_optional("SEARXNG_TIMEOUT", "10", "SearXNG 搜索超时（秒）")

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

    # 自主模式配置
    AUTONOMOUS_DISCOVERY_INTERVAL: int = 1800

    # SearXNG 搜索配置
    SEARXNG_URL: str = ""
    SEARXNG_ENGINES: str = "google,bing,duckduckgo"
    SEARXNG_TIMEOUT: int = 10

    # 搜索 API 配置
    TAVILY_API_KEY: str = ""
    SERPER_API_KEY: str = ""
    BING_SEARCH_API_KEY: str = ""
    SEARCH_BACKENDS: str = ""

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
        cls.AUTONOMOUS_DISCOVERY_INTERVAL = get_config_value(
            "AUTONOMOUS_DISCOVERY_INTERVAL", 1800, int
        )
        cls.SEARXNG_URL = get_config_value("SEARXNG_URL", "", str)
        cls.SEARXNG_ENGINES = get_config_value("SEARXNG_ENGINES", "google,bing,duckduckgo", str)
        cls.SEARXNG_TIMEOUT = get_config_value("SEARXNG_TIMEOUT", 10, int)
        cls.TAVILY_API_KEY = get_config_value("TAVILY_API_KEY", "", str)
        cls.SERPER_API_KEY = get_config_value("SERPER_API_KEY", "", str)
        cls.BING_SEARCH_API_KEY = get_config_value("BING_SEARCH_API_KEY", "", str)
        cls.SEARCH_BACKENDS = get_config_value("SEARCH_BACKENDS", "", str)
