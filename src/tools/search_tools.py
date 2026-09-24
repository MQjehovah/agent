"""search_tools 内置工具 — 渐进披露入口(检索远程工具并激活进当前对话)。

与 ask_user 等同级注册进内置工具表(kind 语义=只读, 无需确认); 执行时:
检索当前可用远程工具 → 把命中项写进对话根激活集 → 返回文本清单。
激活状态读写见 `agent.tool_search.ToolActivationStore`。
"""

import logging
from collections.abc import Callable, Iterable
from typing import Any

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

# search_tools 输出中「已激活」承诺的固定文案(与 agent.tool_search.format_search_result 一致)
_ACTIVATED_NOTICE = "（已激活，下一轮对话可直接调用）"


class SearchToolsTool(BuiltinTool):
    """在已启用连接器的远程工具中按关键词搜索, 命中即激活。"""

    def __init__(self, entries_provider: Callable[[], Iterable[Any]] | None = None,
                 activator: Callable[[list[str]], set[str]] | None = None):
        # 注入式依赖: entries_provider 返回当前可用远程工具条目; activator 激活并返回最新集合
        self._entries_provider = entries_provider
        self._activator = activator

    def configure(self, entries_provider: Callable[[], Iterable[Any]],
                  activator: Callable[[list[str]], set[str]]) -> None:
        """由 Agent 装配(initialize 后调用): 注入检索源与激活动作。"""
        self._entries_provider = entries_provider
        self._activator = activator

    @property
    def name(self) -> str:
        return "search_tools"

    @property
    def description(self) -> str:
        return (
            "在已启用连接器的远程工具（MCP / 平台市场能力 / 插件）中按关键词搜索。"
            "命中的工具会立即激活，下一轮对话即可直接调用；"
            "当需要远程能力（远程终端/设备/市场等）而当前工具列表里没有时，先用本工具搜索。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词（工具名/功能描述/连接器名，空格分隔多个词）"
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数（1-20），缺省 8"
                }
            },
            "required": ["query"]
        }

    async def execute(self, **kwargs) -> str:
        from agent.tool_search import search_remote_tools

        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "参数 query 必须为非空字符串"

        provider = self._entries_provider
        if provider is None:
            return "工具搜索不可用: 未配置远程工具源"
        try:
            entries = list(provider() or [])
        except Exception as e:  # noqa: BLE001 — 检索源异常以文本返回给模型
            return f"工具搜索失败: {e}"

        hits, text = search_remote_tools(entries, query, limit=kwargs.get("limit"))
        if not hits:
            return text

        activated: set[str] = set()
        if self._activator is not None:
            try:
                activated = set(self._activator([hit.name for hit in hits]) or set())
            except Exception as e:  # noqa: BLE001 — 激活失败不影响本次检索结果
                logger.warning(f"工具搜索激活失败(忽略): {e}")
        if activated:
            logger.info(f"工具搜索激活 {len(activated)} 个远程工具: {', '.join(sorted(activated))}")
            return text
        # 无对话根/激活不可用: 不给出「已激活」的错误承诺
        return text.replace(_ACTIVATED_NOTICE, "（当前会话未能激活，如需直接调用请重试）")
