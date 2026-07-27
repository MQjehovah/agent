"""
响应缓存模块

用于缓存 LLM 响应，减少重复请求
"""
import hashlib
import json
import time
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

from config import Config


@dataclass
class CacheEntry:
    """缓存条目"""
    response: Any
    created_at: float
    ttl: float
    hits: int = 0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl


class ResponseCache:
    """响应缓存管理器"""

    def __init__(self, max_size: int = 1000, default_ttl: float = 3600):
        """
        初始化缓存

        Args:
            max_size: 最大缓存条目数
            default_ttl: 默认过期时间（秒）
        """
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: Dict[str, CacheEntry] = {}

    def _generate_key(self, messages: list, tools: list, model: str) -> str:
        """生成缓存键"""
        content = json.dumps({
            "model": model,
            "messages": messages,
            "tools": tools or []
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(content.encode()).hexdigest()

    def get(self, messages: list, tools: list, model: str) -> Optional[Any]:
        """
        获取缓存响应

        Args:
            messages: 消息列表
            tools: 工具列表
            model: 模型名称

        Returns:
            缓存的响应，不存在或已过期返回 None
        """
        key = self._generate_key(messages, tools, model)
        entry = self._cache.get(key)

        if entry is None:
            return None

        if entry.is_expired():
            del self._cache[key]
            return None

        entry.hits += 1
        return entry.response

    def set(self, messages: list, tools: list, model: str, response: Any, ttl: Optional[float] = None):
        """
        设置缓存

        Args:
            messages: 消息列表
            tools: 工具列表
            model: 模型名称
            response: 响应内容
            ttl: 过期时间（秒），None 使用默认值
        """
        # 清理过期条目
        self._cleanup_expired()

        # 检查容量
        if len(self._cache) >= self.max_size:
            self._evict_lru()

        key = self._generate_key(messages, tools, model)
        self._cache[key] = CacheEntry(
            response=response,
            created_at=time.time(),
            ttl=ttl or self.default_ttl
        )

    def _cleanup_expired(self):
        """清理过期条目"""
        expired = [k for k, v in self._cache.items() if v.is_expired()]
        for key in expired:
            del self._cache[key]

    def _evict_lru(self):
        """淘汰最少使用的条目"""
        if not self._cache:
            return

        # 找到命中次数最少的条目
        lru_key = min(self._cache.keys(), key=lambda k: self._cache[k].hits)
        del self._cache[lru_key]

    def clear(self):
        """清空缓存"""
        self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        total_hits = sum(e.hits for e in self._cache.values())
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "total_hits": total_hits,
            "entries": [
                {
                    "key": k[:8] + "...",
                    "hits": e.hits,
                    "age": time.time() - e.created_at
                }
                for k, e in list(self._cache.items())[:10]
            ]
        }


# 全局缓存实例
_cache_instance: Optional[ResponseCache] = None


def get_cache() -> ResponseCache:
    """获取全局缓存实例"""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = ResponseCache(
            max_size=Config.CACHE_MAX_SIZE,
            default_ttl=Config.CACHE_TTL_SECONDS
        )
    return _cache_instance


def init_cache(max_size: int = None, default_ttl: float = None) -> ResponseCache:
    """初始化全局缓存"""
    global _cache_instance
    _cache_instance = ResponseCache(
        max_size or Config.CACHE_MAX_SIZE,
        default_ttl or Config.CACHE_TTL_SECONDS
    )
    return _cache_instance