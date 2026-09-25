"""知识库(RAG)代理 Router。

Web 端(员工端)只持有 agent 凭据、无权直连 RAG;本路由用 agent 侧 RAG_* 配置登录取 token,
代理由此暴露只读接口给前端:

- GET /api/knowledge/status          —— RAG 是否已配置
- GET /api/knowledge/wiki            —— 知识库目录(Wiki 索引)
- GET /api/knowledge/wiki/{page_id}  —— 单页(Wiki 页面 + 正文 + 来源)
- GET /api/knowledge/search?q=&top_k= —— 混合检索(穿透 RAG /api/search)

仅登录用户可访问(get_authz);未配置 RAG 时返回 503。
"""

import logging
import time

import requests
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from web.security import get_authz

logger = logging.getLogger("agent.web.knowledge")


class _RagClient:
    """RAG 客户端:懒加载配置 + token 缓存/续期。"""

    def __init__(self) -> None:
        self._base = ""
        self._username = ""
        self._password = ""
        self._token = ""
        self._expires = 0.0
        self._loaded = False

    def configure(self) -> str:
        from settings import get_settings

        s = get_settings()
        self._base = (s.env_str("rag.base_url", "RAG_BASE_URL", "") or "").rstrip("/")
        self._username = s.env_str("rag.username", "RAG_USERNAME", "") or ""
        self._password = s.env_str("rag.password", "RAG_PASSWORD", "") or ""
        token = s.env_str("rag.token", "RAG_TOKEN", "") or ""
        if token:
            self._token = token
            self._expires = time.time() + 86400 * 30
        self._loaded = True
        return self._base

    def enabled(self) -> bool:
        if not self._loaded:
            self.configure()
        return bool(self._base)

    def _ensure_token(self) -> bool:
        if self._token and time.time() < self._expires:
            return True
        if not (self._username and self._password):
            return bool(self._token)
        try:
            resp = requests.post(
                f"{self._base}/api/auth/login",
                json={"username": self._username, "password": self._password},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            self._token = resp.json()["token"]
            self._expires = time.time() + 86400
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("[知识库代理] 登录失败: %s", exc)
            return False

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"}

    def request(self, method: str, path: str, *, params=None, json_body=None):
        """返回 (response, error)；401 自动重登重试一次。"""
        if not self._ensure_token():
            return None, "rag 认证失败，请检查 RAG_USERNAME/RAG_PASSWORD"
        try:
            resp = requests.request(
                method, f"{self._base}{path}", headers=self._headers(),
                params=params, json=json_body, timeout=30,
            )
            if resp.status_code == 401 and self._username:
                self._token = ""
                self._expires = 0.0
                if self._ensure_token():
                    resp = requests.request(
                        method, f"{self._base}{path}", headers=self._headers(),
                        params=params, json=json_body, timeout=30,
                    )
            return resp, None
        except Exception as exc:  # noqa: BLE001
            logger.error("[知识库代理] 请求失败 %s %s: %s", method, path, exc)
            return None, f"无法连接知识库服务: {self._base}"


def build_knowledge_router(server) -> APIRouter:  # noqa: ARG001 (与其它 router 签名一致)
    router = APIRouter()
    client = _RagClient()

    def _guard(request: Request):
        """未登录返回 401 响应, 否则返回 None。"""
        try:
            get_authz(request)
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    def _unavailable():
        return JSONResponse({"error": "RAG 知识库未配置 (RAG_BASE_URL)"}, status_code=503)

    @router.get("/api/knowledge/status")
    async def knowledge_status(request: Request):
        if (bad := _guard(request)) is not None:
            return bad
        return {"enabled": client.enabled()}

    @router.get("/api/knowledge/wiki")
    async def knowledge_wiki(request: Request):
        if (bad := _guard(request)) is not None:
            return bad
        if not client.enabled():
            return _unavailable()
        resp, err = client.request("GET", "/api/wiki")
        if resp is None:
            return JSONResponse({"error": err}, status_code=502)
        return JSONResponse(resp.json(), status_code=resp.status_code)

    @router.get("/api/knowledge/wiki/{page_id}")
    async def knowledge_wiki_page(request: Request, page_id: str):
        if (bad := _guard(request)) is not None:
            return bad
        if not client.enabled():
            return _unavailable()
        resp, err = client.request("GET", f"/api/wiki/{page_id}")
        if resp is None:
            return JSONResponse({"error": err}, status_code=502)
        return JSONResponse(resp.json(), status_code=resp.status_code)

    @router.get("/api/knowledge/search")
    async def knowledge_search(request: Request, q: str = Query(""), top_k: int = Query(5)):
        if (bad := _guard(request)) is not None:
            return bad
        if not client.enabled():
            return _unavailable()
        query = (q or "").strip()
        if not query:
            return JSONResponse({"results": [], "total": 0}, status_code=200)
        resp, err = client.request(
            "POST", "/api/search", json_body={"query": query, "top_k": max(1, min(int(top_k or 5), 20))}
        )
        if resp is None:
            return JSONResponse({"error": err}, status_code=502)
        return JSONResponse(resp.json(), status_code=resp.status_code)

    return router
