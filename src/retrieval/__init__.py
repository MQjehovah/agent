"""知识库检索工具(用户身份): knowledge_search — 按当前提问者的用户 token 调 RAG /api/search。

所有渠道的知识检索都逐请求解析当前 run 提问者(RunContext.user_id tag)的数字 uid, 经
``require_user_token(uid, "gateway")`` 取下游 token(Bearer); RAG 按用户视角过滤可见性。
无托管 token / uid 不可解析一律 fail-closed 返回统一引导文案(USER_TOKEN_HINT), 不再使用
服务账号(RAG_USERNAME/RAG_PASSWORD)登录。下游 401 时经 force 通道强换一次重试, 仍 401 才
引导重新登录(与 web 知识代理一致)。

tokens 解析含同步阻塞(SSO 刷新/交换 + SQLite), 经 ``asyncio.to_thread`` 执行, 不卡事件循环。
"""

import asyncio
import json
import logging
from typing import Any

import httpx

from tools import BuiltinTool
from tools.user_token import resolve_user_token_or_hint

logger = logging.getLogger("agent.tools.retrieval")


class RetrievalTool(BuiltinTool):
    def __init__(self):
        self._base_url = ""

    def configure(self, base_url: str):
        self._base_url = (base_url or "").rstrip("/")

    @property
    def name(self) -> str:
        return "knowledge_search"

    @property
    def description(self) -> str:
        return (
            "从知识库中检索与查询相关的文档片段。"
            "当需要查找任何类型的知识、文档、资料时使用。"
            "返回最相关的文档片段列表，包含标题、内容摘要和相关度分数。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索查询内容，用自然语言描述你想查找的信息"
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量，默认5",
                    "default": 5
                }
            },
            "required": ["query"]
        }

    async def execute(self, **kwargs) -> str:
        query = kwargs.get("query", "")
        top_k = kwargs.get("top_k", 5)

        if not query:
            return json.dumps({"success": False, "error": "缺少 query 参数"}, ensure_ascii=False)

        if not self._base_url:
            return json.dumps({"success": False, "error": "RAG 知识库未配置 (RAG_BASE_URL)"}, ensure_ascii=False)

        # 用户身份调用: 无托管 token 一律 fail-closed(不得回退服务账号), 并引导登录;
        # token 解析含同步阻塞(SSO 刷新/交换 + SQLite), 经线程池执行避免卡事件循环
        user_token, hint = await asyncio.to_thread(resolve_user_token_or_hint)
        if hint:
            return json.dumps({"success": False, "error": hint}, ensure_ascii=False)

        payload = {"query": query, "top_k": top_k}

        logger.info(f"[知识库检索] query={query[:80]}, top_k={top_k}")

        async def _post(token: str):
            async with httpx.AsyncClient(timeout=30) as client:
                return await client.post(
                    f"{self._base_url}/api/search", json=payload,
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {token}"},
                )

        try:
            resp = await _post(user_token)
            if resp.status_code == 401:
                # 与 web 代理一致: 下游 401 → 强换一次(force)后重试; 仍失败才引导登录
                logger.info("[知识库检索] 用户 token 被 RAG 拒绝(401), 强换重试一次")
                fresh, hint = await asyncio.to_thread(resolve_user_token_or_hint, force=True)
                if hint:
                    return json.dumps({"success": False, "error": hint}, ensure_ascii=False)
                resp = await _post(fresh)
        except httpx.TimeoutException:
            logger.error("[知识库检索] 请求超时")
            return json.dumps({"success": False, "error": "知识库检索超时"}, ensure_ascii=False)
        except httpx.HTTPError as e:
            logger.error(f"[知识库检索] 连接失败: {e}")
            return json.dumps({"success": False, "error": f"无法连接知识库服务: {self._base_url}"}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[知识库检索] 异常: {e}")
            return json.dumps({"success": False, "error": f"检索失败: {e}"}, ensure_ascii=False)

        if resp.status_code == 401:
            from web.sso_tokens import USER_TOKEN_HINT

            logger.info("[知识库检索] 强换后仍 401, 引导重新登录授权")
            return json.dumps({"success": False, "error": USER_TOKEN_HINT}, ensure_ascii=False)
        if resp.status_code >= 400:
            logger.error(f"[知识库检索] HTTP 错误: {resp.status_code}")
            return json.dumps(
                {"success": False, "error": f"知识库返回错误: {resp.status_code}"},
                ensure_ascii=False,
            )
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[知识库检索] 响应非 JSON: {e}")
            return json.dumps({"success": False, "error": "知识库返回非 JSON 响应"}, ensure_ascii=False)

        results = data.get("results", [])
        graph_expanded = data.get("graph_expanded", 0)

        if not results:
            return json.dumps({
                "success": True,
                "query": query,
                "count": 0,
                "message": "未找到相关文档",
            }, ensure_ascii=False)

        formatted = []
        for item in results:
            if isinstance(item, dict):
                formatted.append({
                    "title": item.get("title", ""),
                    "content": item.get("content", ""),
                    "score": item.get("score", 0),
                    "source": item.get("source", ""),
                })

        logger.info(f"[知识库检索] 返回 {len(formatted)} 条结果 (图谱扩展: {graph_expanded})")

        return json.dumps({
            "success": True,
            "query": query,
            "count": len(formatted),
            "graph_expanded": graph_expanded,
            "results": formatted,
        }, ensure_ascii=False)
