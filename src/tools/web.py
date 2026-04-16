import json
import logging
import re
from typing import Dict, Any

import httpx

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


class WebSearchTool(BuiltinTool):
    """网络搜索工具 — 搜索互联网获取最新信息"""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return """搜索互联网获取最新信息。返回搜索结果列表，包含标题、摘要和链接。

使用场景：
- 查询技术文档和最新API信息
- 搜索解决方案和最佳实践
- 获取最新的新闻和动态
- 查找开源项目和库的使用方法"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词"
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数",
                    "default": 10
                }
            },
            "required": ["query"]
        }

    async def execute(self, **kwargs) -> str:
        query = kwargs.get("query", "")
        max_results = kwargs.get("max_results", 10)

        if not query:
            return json.dumps({"success": False, "error": "搜索关键词不能为空"}, ensure_ascii=False)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={
                        "q": query,
                        "format": "json",
                        "no_html": 1,
                        "skip_disambig": 1,
                    }
                )
                data = resp.json()

                results = []
                abstract = data.get("Abstract")
                if abstract:
                    results.append({
                        "title": data.get("Heading", ""),
                        "snippet": abstract,
                        "url": data.get("AbstractURL", ""),
                    })

                for topic in (data.get("RelatedTopics") or [])[:max_results]:
                    if isinstance(topic, dict) and "Text" in topic:
                        results.append({
                            "title": topic.get("Text", "")[:80],
                            "snippet": topic.get("Text", ""),
                            "url": topic.get("FirstURL", ""),
                        })

                return json.dumps({
                    "success": True,
                    "query": query,
                    "count": len(results),
                    "results": results[:max_results]
                }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Web搜索失败: {e}")
            return json.dumps({"success": False, "error": f"搜索失败: {e}"}, ensure_ascii=False)


class WebFetchTool(BuiltinTool):
    """网页内容获取工具 — 获取指定 URL 的网页内容"""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return """获取指定URL的网页内容，返回纯文本格式。可用于获取文档页面、API说明、网页内容等。

使用场景：
- 获取在线文档内容
- 读取网页中的信息
- 获取API响应数据"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要获取的网页URL"
                },
                "max_length": {
                    "type": "integer",
                    "description": "返回内容的最大字符数",
                    "default": 10000
                }
            },
            "required": ["url"]
        }

    async def execute(self, **kwargs) -> str:
        url = kwargs.get("url", "")
        max_length = kwargs.get("max_length", 10000)

        if not url:
            return json.dumps({"success": False, "error": "URL不能为空"}, ensure_ascii=False)

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AgentBot/1.0)"
                })
                resp.raise_for_status()

                content = resp.text
                if "<html" in content.lower():
                    content = self._html_to_text(content)

                if len(content) > max_length:
                    content = content[:max_length] + "\n... [内容已截断]"

                return json.dumps({
                    "success": True,
                    "url": url,
                    "status_code": resp.status_code,
                    "content_length": len(content),
                    "content": content
                }, ensure_ascii=False)

        except httpx.HTTPError as e:
            return json.dumps({"success": False, "error": f"HTTP请求失败: {e}"}, ensure_ascii=False)

    @staticmethod
    def _html_to_text(html: str) -> str:
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
        html = re.sub(r'<[^>]+>', ' ', html)
        html = re.sub(r'\s+', ' ', html).strip()
        return html
