"""知识库(RAG)代理 Router。

Web 端(员工端)只持有 agent 凭据、无权直连 RAG; 本路由逐请求按当前用户做 SSO token
交换(RFC 8693, 受众默认 gateway, 以用户身份访问, 权限/可见性随用户), 无 SSO 会话
(本地密码登录/服务身份)一律 fail-closed 503 + 统一引导登录文案, 不再回退服务账号。

- GET /api/knowledge/status          —— RAG 是否已配置
- GET /api/knowledge/spaces          —— 空间列表(卡片入口)
- GET /api/knowledge/wiki?space_id=  —— 知识库目录(Wiki 索引; space_id 空=全部, default=默认空间)
- GET /api/knowledge/wiki/{page_id}  —— 单页(Wiki 页面 + 正文 + 来源)
- GET /api/knowledge/search?q=&top_k= —— 混合检索(穿透 RAG /api/search)

仅登录用户可访问(get_authz);未配置 RAG 时返回 503。
"""

import logging

import requests
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from web.security import get_authz
from web.sso_tokens import USER_TOKEN_HINT

logger = logging.getLogger("agent.web.knowledge")


def _direct_request(method: str, url: str, token: str, *, params=None, json_body=None):
    """以用户 OBO Bearer token 直连 RAG; 网络异常返回 None(由调用方报错)。"""
    try:
        return requests.request(
            method, url,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            params=params, json=json_body, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[知识库代理] 用户轨请求失败 %s %s: %s", method, url, exc)
        return None


class _RagConfig:
    """RAG 连接配置(base_url): 懒加载; 不再保存服务账号凭据(用户 token 轨)。"""

    def __init__(self) -> None:
        self._base = ""
        self._loaded = False

    def configure(self) -> str:
        from settings import get_settings

        s = get_settings()
        self._base = (s.env_str("rag.base_url", "RAG_BASE_URL", "") or "").rstrip("/")
        self._loaded = True
        return self._base

    def enabled(self) -> bool:
        if not self._loaded:
            self.configure()
        return bool(self._base)


def build_knowledge_router(server) -> APIRouter:  # noqa: ARG001 (与其它 router 签名一致)
    router = APIRouter()
    client = _RagConfig()

    def _authz(request: Request):
        """取当前用户鉴权信息(请求内缓存, 避免重复 DB 解析)。"""
        cached = getattr(request.state, "kb_authz", None)
        if cached is None:
            cached = get_authz(request)
            request.state.kb_authz = cached
        return cached

    def _guard(request: Request):
        """未登录返回 401 响应, 否则返回 None。"""
        try:
            _authz(request)
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    def _rag_call(request: Request, method: str, path: str, *, params=None, json_body=None):
        """以当前用户的 SSO 交换 token 直连 RAG; 401 强制重换重试一次。

        无用户 token(无 SSO 会话/交换失败)一律 fail-closed: 返回 (None, 引导文案, 503),
        不回退服务账号。返回 (response, error, status_code)。
        """
        base = client.configure()
        try:
            uid = int((_authz(request) or {}).get("uid") or 0)
        except Exception:  # noqa: BLE001
            uid = 0
        if not (uid and base):
            return None, USER_TOKEN_HINT, 503
        from web.sso_tokens import get_downstream_token

        for attempt in (0, 1):
            token = get_downstream_token(uid, force=bool(attempt))
            if not token:
                return None, USER_TOKEN_HINT, 503
            resp = _direct_request(
                method, f"{base}{path}", token, params=params, json_body=json_body
            )
            if resp is not None and resp.status_code != 401:
                return resp, None, 200
            if resp is None:
                return None, f"无法连接知识库服务: {base}", 502
            logger.info("[知识库代理] 用户 %s 下游令牌失效(401), 重换重试", uid)
        return None, USER_TOKEN_HINT, 503

    def _unavailable():
        return JSONResponse({"error": "RAG 知识库未配置 (RAG_BASE_URL)"}, status_code=503)

    @router.get("/api/knowledge/status")
    async def knowledge_status(request: Request):
        if (bad := _guard(request)) is not None:
            return bad
        return {"enabled": client.enabled()}

    @router.get("/api/knowledge/spaces")
    async def knowledge_spaces(request: Request):
        if (bad := _guard(request)) is not None:
            return bad
        if not client.enabled():
            return _unavailable()
        resp, err, code = _rag_call(request, "GET", "/api/wiki/spaces")
        if resp is None:
            return JSONResponse({"error": err}, status_code=code)
        return JSONResponse(resp.json(), status_code=resp.status_code)

    @router.get("/api/knowledge/wiki")
    async def knowledge_wiki(request: Request, space_id: str | None = Query(None)):
        if (bad := _guard(request)) is not None:
            return bad
        if not client.enabled():
            return _unavailable()
        params = {"space_id": space_id} if space_id else None
        resp, err, code = _rag_call(request, "GET", "/api/wiki", params=params)
        if resp is None:
            return JSONResponse({"error": err}, status_code=code)
        return JSONResponse(resp.json(), status_code=resp.status_code)

    @router.get("/api/knowledge/wiki/{page_id}")
    async def knowledge_wiki_page(request: Request, page_id: str):
        if (bad := _guard(request)) is not None:
            return bad
        if not client.enabled():
            return _unavailable()
        resp, err, code = _rag_call(request, "GET", f"/api/wiki/{page_id}")
        if resp is None:
            return JSONResponse({"error": err}, status_code=code)
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
        resp, err, code = _rag_call(
            request, "POST", "/api/search",
            json_body={"query": query, "top_k": max(1, min(int(top_k or 5), 20))},
        )
        if resp is None:
            return JSONResponse({"error": err}, status_code=code)
        return JSONResponse(resp.json(), status_code=resp.status_code)

    return router
