"""渐进披露(工具搜索)纯逻辑 + 会话级激活状态。

对齐 dashboard 已实现的模式(见 dashboard/electron/main/kernel/tool-search.ts):
连接器/远程工具装多后不再每轮把全部远程工具定义塞给模型, 而是先发内置工具 +
`search_tools`, 由模型按需检索, 命中后把远程工具「激活」进本对话(会话内粘住、
重启保留)。全部判定/检索/文案为纯函数, 便于离线单测; 激活持久化优先走
`session_meta`(见 storage.get_active_tools/set_active_tools), 不可用时进程内内存兜底。
"""

import logging
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger("agent.tool_search")

# search_tools 注册名(内置工具通道, 与 ask_user 同级)
SEARCH_TOOLS_NAME = "search_tools"
# auto 模式默认阈值: 远程工具数 > 40 时启用渐进披露
DEFAULT_TOOL_SEARCH_THRESHOLD = 40
# search_tools 的 limit 缺省值与夹紧范围(与 dashboard 一致)
SEARCH_TOOLS_DEFAULT_LIMIT = 8
SEARCH_TOOLS_MAX_LIMIT = 20

ENV_TOOL_SEARCH = "AGENT_TOOL_SEARCH"
ENV_TOOL_SEARCH_THRESHOLD = "AGENT_TOOL_SEARCH_THRESHOLD"

# 远程工具来源(可渐进): MCP server / 插件 / 市场远程; 核心来源恒注入
REMOTE_SOURCES = frozenset({"mcp", "plugin", "market"})
CORE_SOURCES = frozenset({"builtin", "skill"})


class ToolSearchMode(str, Enum):
    """`AGENT_TOOL_SEARCH` 取值: auto(默认)/always/off。"""

    AUTO = "auto"
    ALWAYS = "always"
    OFF = "off"


def parse_tool_search_mode(value: Any) -> ToolSearchMode:
    """解析模式字符串; 非法值告警并回退 auto(缺省即默认)。"""
    raw = getattr(value, "value", value)  # 兼容直接传入 ToolSearchMode 成员(str Enum)
    text = str(raw or "").strip().lower()
    if text in (ToolSearchMode.AUTO.value, ToolSearchMode.ALWAYS.value, ToolSearchMode.OFF.value):
        return ToolSearchMode(text)
    if text:
        logger.warning(
            f"{ENV_TOOL_SEARCH} 非法值 {raw!r}, 回退 auto(工具多时渐进)")
    return ToolSearchMode.AUTO


def parse_tool_search_threshold(value: Any, default: int = DEFAULT_TOOL_SEARCH_THRESHOLD) -> int:
    """解析阈值; 缺省/非法回退 default(40), 负值视为非法。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        return default
    try:
        number = int(text)
    except (TypeError, ValueError):
        logger.warning(f"{ENV_TOOL_SEARCH_THRESHOLD} 非法值 {value!r}, 回退默认 {default}")
        return default
    if number < 0:
        logger.warning(f"{ENV_TOOL_SEARCH_THRESHOLD} 非法值 {value!r}, 回退默认 {default}")
        return default
    return number


def tool_search_mode_from_env(env: Mapping[str, str] | None = None) -> ToolSearchMode:
    """从环境变量读取模式(缺省 auto)。"""
    source = os.environ if env is None else env
    return parse_tool_search_mode(source.get(ENV_TOOL_SEARCH))


def tool_search_threshold_from_env(env: Mapping[str, str] | None = None) -> int:
    """从环境变量读取阈值(缺省 40)。"""
    source = os.environ if env is None else env
    return parse_tool_search_threshold(source.get(ENV_TOOL_SEARCH_THRESHOLD))


def should_use_progressive(mode: ToolSearchMode | str, remote_count: int,
                           threshold: int = DEFAULT_TOOL_SEARCH_THRESHOLD) -> bool:
    """是否启用渐进披露: off 恒否; always 恒是; auto 仅当远程工具数 > 阈值(边界 40/41)。"""
    normalized = mode if isinstance(mode, ToolSearchMode) else parse_tool_search_mode(mode)
    if normalized is ToolSearchMode.OFF:
        return False
    if normalized is ToolSearchMode.ALWAYS:
        return True
    return int(remote_count) > int(threshold)


def is_remote_tool(name: str, source_hint: str | None = None) -> bool:
    """远程工具判定(可渐进): 来源优先(builtin/skill→核心, mcp/plugin/market→远程)。

    来源缺失/未知时按暴露名兜底: 平台轨 `platform__*`、MCP 重名前缀 `mcp__*`、
    市场远程 `market:*` 一律视为远程; 其余按核心处理。
    """
    hint = str(source_hint or "").strip().lower()
    if hint:
        return hint in REMOTE_SOURCES
    text = str(name or "")
    return text.startswith("platform__") or text.startswith("mcp__") or text.startswith("market:")


@dataclass(frozen=True)
class ToolSearchEntry:
    """检索条目: 远程工具名 + 描述 + 来源(连接器/插件名)。"""

    name: str
    description: str = ""
    source: str = ""


@dataclass(frozen=True)
class ToolSearchHit:
    """检索命中: score 用于排序与测试断言(名称精确 100 / 名称包含 10 / 来源 5 / 描述 2)。"""

    name: str
    description: str
    source: str
    score: int


def clamp_limit(limit: Any, default: int = SEARCH_TOOLS_DEFAULT_LIMIT,
                max_limit: int = SEARCH_TOOLS_MAX_LIMIT) -> int:
    """limit 夹紧: 非数值/缺省取 default, 结果恒在 1..max_limit。"""
    try:
        number = int(limit)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, max_limit))


def search_remote_tools(entries: Iterable[ToolSearchEntry], query: str,
                        limit: Any = SEARCH_TOOLS_DEFAULT_LIMIT,
                        max: int = SEARCH_TOOLS_MAX_LIMIT) -> tuple[list[ToolSearchHit], str]:
    """远程工具检索(纯函数)。

    query 按空白分词, 每词在「名称 / 来源 / 描述」中做不区分大小写的子串匹配并计分
    (名称精确 +100、名称包含 +10、来源包含 +5、描述包含 +2), 至少命中一词才入选;
    按分数降序、同分按名称升序稳定排序; 返回 (命中列表, 文案), 未命中/空查询给
    可行动提示。
    """
    terms = [t for t in str(query or "").lower().split() if t]
    candidates = list(entries or [])
    if not terms:
        return [], "请提供搜索关键词（工具名/功能描述/连接器名，空格分隔多个词）。"
    hits: list[ToolSearchHit] = []
    for entry in candidates:
        name = str(getattr(entry, "name", "") or "")
        if not name:
            continue
        lowered_name = name.lower()
        source = str(getattr(entry, "source", "") or "").lower()
        description = str(getattr(entry, "description", "") or "").lower()
        score = 0
        for term in terms:
            if lowered_name == term:
                score += 100
            elif term in lowered_name:
                score += 10
            if source and term in source:
                score += 5
            if term in description:
                score += 2
        if score > 0:
            hits.append(ToolSearchHit(
                name=name,
                description=str(getattr(entry, "description", "") or ""),
                source=str(getattr(entry, "source", "") or ""),
                score=score,
            ))
    hits.sort(key=lambda hit: (-hit.score, hit.name))
    hits = hits[:clamp_limit(limit, max_limit=max)]
    if not hits:
        return [], (f"未找到匹配「{query}」的远程工具（当前可用 {len(candidates)} 个）。"
                    "可换关键词重试，或直接用现有工具完成任务。")
    return hits, format_search_result(hits)


def format_search_result(hits: Iterable[ToolSearchHit]) -> str:
    """search_tools 命中输出: 逐条「名称 —— [连接器 x] 描述」, 并说明已激活。"""
    items = list(hits or [])
    lines = [f"找到 {len(items)} 个匹配的远程工具（已激活，下一轮对话可直接调用）："]
    for hit in items:
        tag = f"[连接器 {hit.source}] " if hit.source else ""
        desc = " ".join(str(hit.description or "").split()) or "(无描述)"
        lines.append(f"{hit.name} —— {tag}{desc}")
    return "\n".join(lines)


def progressive_hint(active_names: Iterable[str]) -> str:
    """渐进模式下追加到系统提示末尾的说明(文案与 dashboard 一致)。"""
    names = [str(name).strip() for name in (active_names or []) if str(name).strip()]
    listing = "、".join(names) if names else "无"
    return (
        "连接器的更多工具需先用 `search_tools` 搜索；已激活："
        + listing
        + "。需要远程能力（远程终端/设备/市场等）而当前工具列表没有时，先搜索再调用。"
    )


class ToolActivationStore:
    """会话激活集: session_meta 持久化优先, 不可用时进程内内存兜底。

    - 键为「对话根」(`RunContext.conversation_id`, web/钉钉/子代理一致);
    - load: 进程内缓存命中直接返回; 未命中读 `storage.get_active_tools`(JSON 数组),
      storage 缺失/读取异常 → 返回空集且不缓存(下次可重试);
    - activate: 合并去重后先更新进程内缓存, 再写穿 storage(失败仅告警, 内存已生效);
    - 仅 `search_tools` 增加; 远程工具暂不可用时注入前过滤, 不清理持久化
      (server 恢复后自动重新可用)。
    """

    _lock = threading.Lock()
    _cache: dict[str, frozenset[str]] = {}

    @staticmethod
    def _normalize(conversation_id: Any) -> str:
        return str(conversation_id or "").strip()

    @classmethod
    def load(cls, conversation_id: Any, storage: Any = None) -> frozenset[str]:
        """读取该对话已激活的远程工具名集合。"""
        cid = cls._normalize(conversation_id)
        if not cid:
            return frozenset()
        with cls._lock:
            cached = cls._cache.get(cid)
        if cached is not None:
            return cached
        if storage is None or not hasattr(storage, "get_active_tools"):
            return frozenset()
        try:
            raw = storage.get_active_tools(cid)
        except Exception as e:  # noqa: BLE001 — 存储异常回退内存, 不阻断对话
            logger.warning(f"读取会话激活工具失败(内存兜底): {e}")
            return frozenset()
        names = frozenset(str(n) for n in (raw or []) if str(n).strip())
        with cls._lock:
            cls._cache[cid] = names
        return names

    @classmethod
    def activate(cls, conversation_id: Any, names: Iterable[str], storage: Any = None) -> frozenset[str]:
        """把命中的工具名并入激活集并持久化, 返回最新激活集。"""
        cid = cls._normalize(conversation_id)
        added = {str(name).strip() for name in (names or []) if str(name).strip()}
        if not cid:
            return frozenset()
        if not added:
            return cls.load(cid, storage)
        merged = set(cls.load(cid, storage)) | added
        value = frozenset(merged)
        with cls._lock:
            cls._cache[cid] = value
        if storage is not None and hasattr(storage, "set_active_tools"):
            try:
                storage.set_active_tools(cid, sorted(merged))
            except Exception as e:  # noqa: BLE001 — 持久化失败内存仍生效
                logger.warning(f"持久化会话激活工具失败(内存已生效): {e}")
        return value

    @classmethod
    def reset_cache(cls, conversation_id: Any = None) -> None:
        """清空进程内缓存(测试/运维用); 不传 conversation_id 清全部。"""
        with cls._lock:
            if conversation_id is None:
                cls._cache.clear()
            else:
                cls._cache.pop(cls._normalize(conversation_id), None)
