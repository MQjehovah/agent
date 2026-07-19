"""
统一配置系统。单例，加载 config/config.json，所有设置有硬编码默认值，用户只需覆写差异字段。
"""
import json
import logging
import os as _os
from typing import Any

logger = logging.getLogger("agent.settings")

# 硬编码默认值（所有配置的基础）
_DEFAULTS = {
    "llm": {
        "endpoints": [],
        "timeout": 300,
        "connect_timeout": 30,
    },
    "session": {
        "ttl_seconds": 3600,
        "max_sessions": 100,
    },
    "cache": {
        "max_size": 1000,
        "ttl_seconds": 3600,
    },
    "logging": {
        "level": "INFO",
    },
    "autonomous": {
        "discovery_interval": 1800,
    },
    "context": {
        "sliding_window_size": 40,
        "sliding_window_summary_max": 6000,
    },
    "tools": {
        "search": {
            "backends": "",
            "tavily_api_key": "",
            "serper_api_key": "",
            "bing_search_api_key": "",
            "searxng_url": "",
            "searxng_engines": "google,bing,duckduckgo",
            "searxng_timeout": 10,
        },
    },
    "rag": {
        "base_url": "",
        "username": "",
        "password": "",
        "token": "",
    },
    "learning": {
        "enabled": False,
        "per_round": False,
        "auto_create": False,
    },
    "memory": {
        "injection_limit": 24,
        "per_category_limit": 3,
        "keyword_filter": True,
    },
    "cost": {
        "pricing": {},
    },
    "observability": {
        "jsonl_path": "",
    },
}

_settings_instance: "Settings | None" = None


def get_settings() -> "Settings":
    if _settings_instance is None:
        raise RuntimeError("Settings 未初始化，请先调用 init_settings(config_dir)")
    return _settings_instance


def init_settings(config_dir: str) -> "Settings":
    global _settings_instance
    _settings_instance = Settings(config_dir)
    return _settings_instance


class Settings:
    def __init__(self, config_dir: str):
        self._config_dir = config_dir
        self._data = self._deep_merge(_DEFAULTS, self._load_user_config())

    def _load_user_config(self) -> dict:
        path = _os.path.join(self._config_dir, "config.json")
        if not _os.path.isfile(path):
            logger.info(f"未找到 {path}，使用默认配置")
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"解析 config.json 失败: {e}，使用默认配置")
            return {}

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """递归合并两个 dict，override 覆盖 base"""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = Settings._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def get(self, path: str, default: Any = None) -> Any:
        """用点号路径访问配置值，如 get("llm.endpoints")"""
        node = self._data
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    # ---- 常用配置的快捷属性 ----
    @property
    def llm_endpoints(self) -> list:
        return self.get("llm.endpoints", [])

    @property
    def llm_timeout(self) -> float:
        return float(self.get("llm.timeout", 300))

    @property
    def llm_connect_timeout(self) -> float:
        return float(self.get("llm.connect_timeout", 30))

    # ---- 环境变量兼容（用于尚未迁移的模块）----
    def _env_get(self, path: str, env_key: str, default: Any = None) -> Any:
        """优先 Settings，回退环境变量，再回退默认值"""
        from_settings = self.get(path)
        if from_settings is not None:
            return from_settings
        env_val = _os.getenv(env_key, "")
        if env_val:
            logger.debug(f"配置 {path} 使用环境变量 {env_key}={env_val}（建议迁移到 config.json）")
        return env_val if env_val else default

    def env_str(self, path: str, env_key: str, default: str = "") -> str:
        return self._env_get(path, env_key, default)

    def env_int(self, path: str, env_key: str, default: int = 0) -> int:
        try:
            return int(self._env_get(path, env_key, default))
        except (ValueError, TypeError):
            return default

    def env_float(self, path: str, env_key: str, default: float = 0.0) -> float:
        try:
            return float(self._env_get(path, env_key, default))
        except (ValueError, TypeError):
            return default

    def report(self) -> str:
        """生成配置摘要（隐藏敏感值）"""
        safe = self._data.copy()
        if "llm" in safe and "endpoints" in safe["llm"]:
            safe["llm"]["endpoints"] = [
                {**ep, "api_key": "***"} for ep in safe["llm"]["endpoints"]
            ]
        if "tools" in safe and "search" in safe["tools"]:
            search = safe["tools"]["search"]
            for k in list(search):
                if "key" in k and search[k]:
                    search[k] = "***"
        return json.dumps(safe, indent=2, ensure_ascii=False)


def validate_config() -> bool:
    """启动时校验配置"""
    import logging
    logger = logging.getLogger("agent.config")
    eps = get_settings().llm_endpoints
    if not eps:
        logger.error("LLM 端点未配置: 请在 config/config.json 的 llm.endpoints 中配置")
        return False
    for i, ep in enumerate(eps):
        missing = [k for k in ("model", "base_url", "api_key") if not ep.get(k)]
        if missing:
            logger.error(f"LLM 端点 #{i + 1} 缺少必填字段: {missing}")
            return False
    logger.info("读取配置成功")
    return True
