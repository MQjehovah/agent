"""
配置模块 — 统一配置系统的薄封装。
所有配置从 src/settings.py 的 Settings 单例读取（加载自 config/config.json）。
"""
import logging
from typing import Any

logger = logging.getLogger("agent.config")


def get_config_value(key: str, default: Any = None, converter: type = str) -> Any:
    """从 Settings 获取配置值（兼容旧接口）"""
    from settings import get_settings
    settings = get_settings()
    value = settings._data.get(key)
    if value is None:
        # 尝试点号路径
        value = settings.get(key)
    if value is None:
        return default
    try:
        return converter(value)
    except (ValueError, TypeError):
        return default


class Config:
    """配置常量 — 从 Settings 取值，保留旧 class-variable 访问方式"""

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

    # 上下文滑动窗口配置
    SLIDING_WINDOW_SIZE: int = 40
    SLIDING_WINDOW_SUMMARY_MAX: int = 6000

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
        """从 Settings 加载配置（替代原来的环境变量加载）"""
        from settings import get_settings
        s = get_settings()

        cls.SESSION_TTL_SECONDS = int(s.get("session.ttl_seconds", 3600))
        cls.MAX_SESSIONS = int(s.get("session.max_sessions", 100))
        cls.CACHE_MAX_SIZE = int(s.get("cache.max_size", 1000))
        cls.CACHE_TTL_SECONDS = int(s.get("cache.ttl_seconds", 3600))
        cls.LLM_TIMEOUT = float(s.get("llm.timeout", 300))
        cls.LLM_CONNECT_TIMEOUT = float(s.get("llm.connect_timeout", 30))
        cls.LOG_LEVEL = str(s.get("logging.level", "INFO"))
        cls.AUTONOMOUS_DISCOVERY_INTERVAL = int(s.get("autonomous.discovery_interval", 1800))
        cls.SLIDING_WINDOW_SIZE = int(s.get("context.sliding_window_size", 40))
        cls.SLIDING_WINDOW_SUMMARY_MAX = int(s.get("context.sliding_window_summary_max", 6000))
        cls.SEARXNG_URL = s.get("tools.search.searxng_url", "")
        cls.SEARXNG_ENGINES = s.get("tools.search.searxng_engines", "google,bing,duckduckgo")
        cls.SEARXNG_TIMEOUT = int(s.get("tools.search.searxng_timeout", 10))
        cls.TAVILY_API_KEY = s.get("tools.search.tavily_api_key", "")
        cls.SERPER_API_KEY = s.get("tools.search.serper_api_key", "")
        cls.BING_SEARCH_API_KEY = s.get("tools.search.bing_search_api_key", "")
        cls.SEARCH_BACKENDS = s.get("tools.search.backends", "")

def validate_config() -> bool:
    """启动时的配置校验（从 Settings 读取，不再依赖环境变量）"""
    from settings import get_settings
    s = get_settings()

    ok = True
    # LLM 端点必须配置
    eps = s.llm_endpoints
    if not eps:
        logger.error("LLM 端点未配置: 请在 config/config.json 的 llm.endpoints 中配置")
        ok = False
    else:
        for i, ep in enumerate(eps):
            missing = [k for k in ("model", "base_url", "api_key") if not ep.get(k)]
            if missing:
                logger.error(f"LLM 端点 #{i + 1} 缺少必填字段: {missing}")
                ok = False

    if not ok:
        logger.error("配置验证失败，请检查 config/config.json")
        logger.info("参考 config/config.example.json 创建配置文件")
        return False

    logger.info("读取配置成功")
    return True
