import json
import logging
import time
from typing import Any

import requests

from tools import BuiltinTool

logger = logging.getLogger("agent.tools.retrieval")


class RetrievalTool(BuiltinTool):
    def __init__(self):
        self._base_url = ""
        self._username = ""
        self._password = ""
        self._token = ""
        self._token_expires_at = 0.0

    def configure(self, base_url: str, username: str = "", password: str = "", token: str = ""):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = token
        if token:
            self._token_expires_at = time.time() + 86400 * 30

    def _ensure_token(self) -> bool:
        if self._token and time.time() < self._token_expires_at:
            return True
        if not self._username or not self._password:
            return bool(self._token)
        try:
            resp = requests.post(
                f"{self._base_url}/api/auth/login",
                json={"username": self._username, "password": self._password},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["token"]
            self._token_expires_at = time.time() + 86400
            logger.info("[知识库检索] 登录成功，已获取 Token")
            return True
        except Exception as e:
            logger.error(f"[知识库检索] 登录失败: {e}")
            return False

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

        if not self._ensure_token():
            return json.dumps({"success": False, "error": "RAG 知识库认证失败，请检查 RAG_USERNAME/RAG_PASSWORD"}, ensure_ascii=False)

        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            }
            payload = {"query": query, "top_k": top_k}

            logger.info(f"[知识库检索] query={query[:80]}, top_k={top_k}")

            resp = requests.post(
                f"{self._base_url}/api/search",
                json=payload,
                headers=headers,
                timeout=30,
            )

            if resp.status_code == 401 and self._username:
                self._token = ""
                self._token_expires_at = 0.0
                if self._ensure_token():
                    headers["Authorization"] = f"Bearer {self._token}"
                    resp = requests.post(
                        f"{self._base_url}/api/search",
                        json=payload,
                        headers=headers,
                        timeout=30,
                    )

            resp.raise_for_status()
            data = resp.json()

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

        except requests.exceptions.Timeout:
            logger.error("[知识库检索] 请求超时")
            return json.dumps({"success": False, "error": "知识库检索超时"}, ensure_ascii=False)
        except requests.exceptions.ConnectionError:
            logger.error("[知识库检索] 连接失败")
            return json.dumps({"success": False, "error": f"无法连接知识库服务: {self._base_url}"}, ensure_ascii=False)
        except requests.exceptions.HTTPError as e:
            logger.error(f"[知识库检索] HTTP 错误: {e}")
            return json.dumps(
                {"success": False, "error": f"知识库返回错误: {e.response.status_code}"},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[知识库检索] 异常: {e}")
            return json.dumps({"success": False, "error": f"检索失败: {e}"}, ensure_ascii=False)
