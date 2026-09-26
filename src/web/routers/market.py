"""能力市场 + 云端托管安装 Router(P4) + 管理端本地 MCP 配置。

市场调用矩阵(agent 不持有用户 SSO token, 统一服务令牌 + 按需 X-Act-As-Sub):
- 浏览/详情: 服务令牌 `GET /api/capabilities`、`GET /api/capabilities/{id}`
  (详情 schema `CapabilityOut.components` 已含 plugin 已发布子能力, 无需额外辅助调用);
- 已加入清单: act-as `GET /api/my/capabilities?scope=added`(name 集合);
- 加入/移出: act-as `POST /api/my/capabilities` / `DELETE /api/my/capabilities/{id}`。

安装是用户级「本地各记」(`capability_installations`), 仅支持 `type=mcp` 且
`distribution != local`; 校验通过后写表并刷新该用户已存在 worker 的平台工具集。
管理端本地 MCP 配置读写全局 `config/mcp_servers.json`(白名单字段), 写后 reload。
"""

import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from mcps.platform import PlatformMCPConfig
from web import security

logger = logging.getLogger("agent.web.market")

# 本地 MCP 条目白名单(按 config/mcp_servers.json 现有结构与 manager 读取字段)
_LOCAL_MCP_FIELDS = (
    "name", "enabled", "command", "args", "env", "url", "transport", "cwd",
    "description", "type", "timeout_seconds", "connect_timeout_seconds",
    "max_reconnect_attempts", "max_concurrency", "risk_overrides",
)


class MarketUnavailableError(Exception):
    """市场未配置(MARKET_BASE_URL / MARKET_SERVICE_TOKEN 缺失)。"""


class MarketUpstreamError(Exception):
    """市场请求失败(网络/超时等)。"""


async def _market_request(method: str, path: str, *, act_as: str = "",
                          params: dict | None = None, json_body: Any = None):
    """代理市场 HTTP; 返回 (status_code, payload)。

    市场未配置抛 MarketUnavailable; 网络异常抛 MarketUpstreamError。
    """
    cfg = PlatformMCPConfig.from_env()
    if not cfg.enabled:
        raise MarketUnavailableError()
    headers = {"Authorization": f"Bearer {cfg.service_token}"}
    if act_as:
        headers["X-Act-As-Sub"] = act_as
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout) as client:
            resp = await client.request(method, f"{cfg.base_url}{path}",
                                        headers=headers, params=params, json=json_body)
    except httpx.HTTPError as e:
        raise MarketUpstreamError(f"能力市场请求失败: {e}") from e
    try:
        payload: Any = resp.json()
    except Exception:
        payload = {"error": (resp.text or "")[:300]}
    return resp.status_code, payload


def _market_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, MarketUnavailableError):
        return JSONResponse({"error": "能力市场未配置"}, status_code=503)
    return JSONResponse({"error": str(exc)}, status_code=502)


def _forward(status: int, payload: Any) -> JSONResponse:
    """透传市场状态与文案(含 403 等业务错误)。"""
    body = payload if isinstance(payload, dict) else {"result": payload}
    return JSONResponse(body, status_code=status)


def _authz_or_401(request: Request):
    try:
        return security.get_authz(request), None
    except Exception:
        return None, JSONResponse({"error": "Unauthorized"}, status_code=401)


def build_market_router(server) -> APIRouter:
    """构建市场/安装/本地 MCP 路由; server 提供 refresh_user_platform(uid)。"""
    router = APIRouter()

    def _uid(u: dict) -> int:
        try:
            return int(u.get("uid") or 0)
        except (TypeError, ValueError):
            return 0

    def _act_as(uid: int) -> str:
        from web.security import resolve_market_act_as
        return resolve_market_act_as(f"web:{uid}")

    def _require_act_as(uid: int):
        """用户级市场调用必须具备市场身份: 解析失败/服务身份(uid<=0)一律 fail-closed 403。

        禁止静默降级为服务令牌身份(否则会串到服务账号的市场视角)。
        """
        act_as = _act_as(uid) if uid > 0 else ""
        if not act_as:
            return "", JSONResponse(
                {"error": "无法解析市场用户身份，请确认账号已同步到市场（工号）"},
                status_code=403)
        return act_as, None

    def _storage():
        from storage.storage import get_storage
        return get_storage()

    def _installed_names(uid: int) -> set[str]:
        storage = _storage()
        if storage is None or uid <= 0:
            return set()
        try:
            return storage.installed_capability_names(uid, enabled_only=False)
        except Exception as e:
            logger.warning(f"读取本地安装失败(按空集处理): {e}")
            return set()

    async def _joined_names(act_as: str) -> set[str]:
        status, payload = await _market_request(
            "GET", "/api/my/capabilities", act_as=act_as,
            params={"scope": "added"})
        if status >= 400 or not isinstance(payload, list):
            logger.warning(f"读取市场已加入清单失败(status={status}), joined 按空集")
            return set()
        return {str(i.get("name")) for i in payload
                if isinstance(i, dict) and i.get("name")}

    async def _refresh(uid: int) -> bool:
        fn = getattr(server, "refresh_user_platform", None)
        if not callable(fn):
            return False
        try:
            return bool(await fn(uid))
        except Exception as e:
            logger.warning(f"刷新用户平台安装失败(忽略): {e}")
            return False

    # ---------------- 市场浏览/详情/加入/移出 ----------------

    @router.get("/api/market/categories")
    async def market_categories(request: Request):
        """代理市场分类表(act-as 身份): {type: [category...]}，供前端分类筛选。"""
        u, denied = _authz_or_401(request)
        if denied:
            return denied
        uid = _uid(u)
        act_as, act_denied = _require_act_as(uid)
        if act_denied:
            return act_denied
        try:
            status, payload = await _market_request("GET", "/api/meta/categories", act_as=act_as)
        except (MarketUnavailableError, MarketUpstreamError) as e:
            return _market_error(e)
        if status >= 400:
            return _forward(status, payload)
        return payload if isinstance(payload, dict) else {}

    @router.get("/api/market/capabilities")
    async def market_capabilities(
        request: Request,
        page: int = Query(1, ge=1),
        page_size: int = Query(24, ge=1, le=100),
        q: str = "",
        type: str = "",
        category: str = "",
    ):
        """代理市场货架浏览(act-as 身份, fail-closed), 并 merge joined / installed。"""
        u, denied = _authz_or_401(request)
        if denied:
            return denied
        uid = _uid(u)
        act_as, act_denied = _require_act_as(uid)
        if act_denied:
            return act_denied
        try:
            status, payload = await _market_request(
                "GET", "/api/capabilities", act_as=act_as,
                params={"page": page, "page_size": page_size,
                        "q": q, "type": type, "category": category})
        except (MarketUnavailableError, MarketUpstreamError) as e:
            return _market_error(e)
        if status >= 400:
            return _forward(status, payload)
        items = payload.get("items") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            items = []
        try:
            joined = await _joined_names(act_as)
        except (MarketUnavailableError, MarketUpstreamError) as e:
            logger.warning(f"joined 查询失败(按空集处理): {e}")
            joined = set()
        installed = _installed_names(uid)
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            item["joined"] = name in joined
            item["installed"] = name in installed
        total = payload.get("total", len(items)) if isinstance(payload, dict) else len(items)
        result: dict[str, Any] = {"items": items, "total": total,
                                  "page": page, "page_size": page_size}
        if isinstance(payload, dict) and "runtime" in payload:
            result["runtime"] = payload["runtime"]  # 市场 runtime 契约原样透传
        return result

    @router.get("/api/market/capabilities/{cap_id}")
    async def market_capability_detail(cap_id: str, request: Request):
        """代理市场能力详情(act-as 身份, fail-closed) + joined/installed。

        plugin 组件清单: 市场详情 `components`(与 input_schema.components 同源)
        已包含, 无需再调 `/api/my/capabilities?include_components=1`。
        """
        u, denied = _authz_or_401(request)
        if denied:
            return denied
        uid = _uid(u)
        act_as, act_denied = _require_act_as(uid)
        if act_denied:
            return act_denied
        try:
            status, payload = await _market_request(
                "GET", f"/api/capabilities/{cap_id}", act_as=act_as)
        except (MarketUnavailableError, MarketUpstreamError) as e:
            return _market_error(e)
        if status >= 400:
            return _forward(status, payload)
        if isinstance(payload, dict):
            name = str(payload.get("name") or "")
            try:
                payload["joined"] = name in await _joined_names(act_as)
            except (MarketUnavailableError, MarketUpstreamError) as e:
                logger.warning(f"joined 查询失败(按空集处理): {e}")
                payload["joined"] = False
            payload["installed"] = name in _installed_names(uid)
        return payload

    @router.post("/api/market/capabilities/{cap_id}/join")
    async def market_capability_join(cap_id: str, request: Request):
        """act-as 代理市场加入(透传市场错误与文案)。"""
        u, denied = _authz_or_401(request)
        if denied:
            return denied
        act_as, act_denied = _require_act_as(_uid(u))
        if act_denied:
            return act_denied
        try:
            status, payload = await _market_request(
                "POST", "/api/my/capabilities", act_as=act_as,
                json_body={"capability_id": cap_id})
        except (MarketUnavailableError, MarketUpstreamError) as e:
            return _market_error(e)
        return _forward(status, payload)

    @router.post("/api/market/capabilities/{cap_id}/leave")
    async def market_capability_leave(cap_id: str, request: Request):
        """act-as 代理市场移出(透传市场错误与文案)。"""
        u, denied = _authz_or_401(request)
        if denied:
            return denied
        act_as, act_denied = _require_act_as(_uid(u))
        if act_denied:
            return act_denied
        try:
            status, payload = await _market_request(
                "DELETE", f"/api/my/capabilities/{cap_id}", act_as=act_as)
        except (MarketUnavailableError, MarketUpstreamError) as e:
            return _market_error(e)
        return _forward(status, payload)

    # ---------------- 用户级云端托管安装 ----------------

    @router.get("/api/market/installations")
    async def list_installations(request: Request):
        """本地安装列表(本地表; 仍需市场身份, fail-closed 防服务身份串用)。"""
        u, denied = _authz_or_401(request)
        if denied:
            return denied
        uid = _uid(u)
        _, act_denied = _require_act_as(uid)
        if act_denied:
            return act_denied
        storage = _storage()
        rows = storage.list_installations(uid) if storage is not None else []
        return {"installations": rows}

    @router.post("/api/market/installations")
    async def install_capability(request: Request):
        """安装到云端托管: 校验 mcp + 已加入 + runtime.cloud, 写表并刷新 worker。"""
        u, denied = _authz_or_401(request)
        if denied:
            return denied
        uid = _uid(u)
        act_as, act_denied = _require_act_as(uid)
        if act_denied:
            return act_denied
        try:
            data = await request.json()
        except Exception:
            data = {}
        capability_id = str((data or {}).get("capability_id") or "").strip()
        if not capability_id:
            return JSONResponse({"error": "capability_id 必填"}, status_code=400)

        try:
            status, payload = await _market_request(
                "GET", f"/api/capabilities/{capability_id}", act_as=act_as)
        except (MarketUnavailableError, MarketUpstreamError) as e:
            return _market_error(e)
        if status >= 400:
            return _forward(status, payload)
        detail = payload if isinstance(payload, dict) else {}
        cap_type = str(detail.get("type") or "").strip().lower()
        distribution = str(detail.get("distribution") or "both").strip().lower()
        if cap_type != "mcp":
            return JSONResponse({"error": "仅支持安装 type=mcp 的云端托管能力"},
                                status_code=422)
        runtime = detail.get("runtime")
        cloud = runtime.get("cloud") if isinstance(runtime, dict) else None
        if cloud is not None and not cloud:
            return JSONResponse(
                {"error": "该能力不支持云端托管（runtime.cloud=false）"},
                status_code=422)
        if cloud is None and distribution == "local":
            return JSONResponse(
                {"error": "该能力为本地分发(local), 不支持云端托管安装"},
                status_code=422)
        name = str(detail.get("name") or (data or {}).get("capability_name") or capability_id)
        try:
            joined = await _joined_names(act_as)
        except (MarketUnavailableError, MarketUpstreamError) as e:
            return _market_error(e)
        if name not in joined:
            return JSONResponse({"error": "请先在市场加入该能力"}, status_code=403)

        kind = str((data or {}).get("kind") or cap_type).strip() or cap_type
        storage = _storage()
        if storage is None:
            return JSONResponse({"error": "storage unavailable"}, status_code=500)
        storage.upsert_installation(uid, capability_id, name, kind)
        refreshed = await _refresh(uid)
        return {"success": True, "refreshed": refreshed,
                "installation": {"capability_id": capability_id,
                                 "capability_name": name, "kind": kind,
                                 "enabled": True}}

    @router.patch("/api/market/installations/{capability_id}")
    async def set_installation_enabled(capability_id: str, request: Request):
        """启停已安装能力(不存在 404); 写后刷新该用户 worker。"""
        u, denied = _authz_or_401(request)
        if denied:
            return denied
        uid = _uid(u)
        _, act_denied = _require_act_as(uid)
        if act_denied:
            return act_denied
        try:
            data = await request.json()
        except Exception:
            data = {}
        enabled = (data or {}).get("enabled")
        if not isinstance(enabled, bool):
            return JSONResponse({"error": "enabled 需为布尔值"}, status_code=422)
        storage = _storage()
        okay = bool(storage and storage.set_installation_enabled(uid, capability_id, enabled))
        if not okay:
            return JSONResponse({"error": "未安装该能力"}, status_code=404)
        refreshed = await _refresh(uid)
        return {"success": True, "capability_id": capability_id,
                "enabled": enabled, "refreshed": refreshed}

    @router.delete("/api/market/installations/{capability_id}")
    async def remove_installation(capability_id: str, request: Request):
        """卸载(不存在 404); 写后刷新该用户 worker。"""
        u, denied = _authz_or_401(request)
        if denied:
            return denied
        uid = _uid(u)
        _, act_denied = _require_act_as(uid)
        if act_denied:
            return act_denied
        storage = _storage()
        okay = bool(storage and storage.remove_installation(uid, capability_id))
        if not okay:
            return JSONResponse({"error": "未安装该能力"}, status_code=404)
        refreshed = await _refresh(uid)
        return {"success": True, "capability_id": capability_id, "refreshed": refreshed}

    # ---------------- 管理端: 全局本地 MCP 配置 ----------------

    def _admin_or_403(request: Request):
        try:
            u = security.get_authz(request)
        except Exception:
            return None, JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not security.has_permission(u, "admin.mcp_local"):
            return None, JSONResponse({"error": "Forbidden"}, status_code=403)
        return u, None

    def _local_mcp_path() -> str:
        agent = getattr(server, "agent", None)
        config_dir = str(getattr(agent, "config_dir", "") or "")
        if not config_dir:
            raise RuntimeError("Agent 未初始化")
        return os.path.join(config_dir, "mcp_servers.json")

    def _read_local_mcp() -> list[dict]:
        path = _local_mcp_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    def _write_local_mcp(entries: list[dict]) -> None:
        path = _local_mcp_path()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _sanitize_local_mcp(entry: dict) -> dict:
        return {k: entry[k] for k in _LOCAL_MCP_FIELDS if k in entry}

    async def _reload_local_mcp() -> dict:
        agent = getattr(server, "agent", None)
        manager = getattr(agent, "mcp", None) if agent is not None else None
        if manager is None:
            return {}
        try:
            results = await manager.reload_all()
            return results if isinstance(results, dict) else {}
        except Exception as e:
            logger.warning(f"本地 MCP 重载失败(忽略): {e}")
            return {}

    @router.get("/api/admin/local-mcp")
    async def local_mcp_list(request: Request):
        _, denied = _admin_or_403(request)
        if denied:
            return denied
        try:
            return {"servers": _read_local_mcp()}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @router.get("/api/admin/local-mcp/{name}")
    async def local_mcp_get(name: str, request: Request):
        _, denied = _admin_or_403(request)
        if denied:
            return denied
        try:
            entry = next((e for e in _read_local_mcp() if e.get("name") == name), None)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        if entry is None:
            return JSONResponse({"error": f"未找到本地 MCP: {name}"}, status_code=404)
        return {"server": entry}

    @router.put("/api/admin/local-mcp/{name}")
    async def local_mcp_upsert(name: str, request: Request):
        """新增/覆盖本地 MCP 条目(仅白名单字段; 名称以路径为准), 写后重载。"""
        _, denied = _admin_or_403(request)
        if denied:
            return denied
        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict) or not data:
            return JSONResponse({"error": "请求体需为 JSON 对象"}, status_code=422)
        entry = _sanitize_local_mcp(data)
        entry["name"] = name
        try:
            entries = _read_local_mcp()
            replaced = False
            for i, existing in enumerate(entries):
                if existing.get("name") == name:
                    entries[i] = entry
                    replaced = True
                    break
            if not replaced:
                entries.append(entry)
            _write_local_mcp(entries)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        reload_result = await _reload_local_mcp()
        return {"success": True, "server": entry, "reload": reload_result}

    @router.delete("/api/admin/local-mcp/{name}")
    async def local_mcp_delete(name: str, request: Request):
        """删除本地 MCP 条目, 写后重载。"""
        _, denied = _admin_or_403(request)
        if denied:
            return denied
        try:
            entries = _read_local_mcp()
            remaining = [e for e in entries if e.get("name") != name]
            if len(remaining) == len(entries):
                return JSONResponse({"error": f"未找到本地 MCP: {name}"}, status_code=404)
            _write_local_mcp(remaining)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        reload_result = await _reload_local_mcp()
        return {"success": True, "reload": reload_result}

    return router
