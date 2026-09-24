"""ERP 只读 MCP：业务工具封装常用接口，skill 不必再列 HTTP 路径。

保留 erp_request 作兜底；优先用下方命名工具。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from mcp.server.mcpserver import MCPServer
from rich.console import Console
from rich.logging import RichHandler

from hub_stop import filter_erp_result, maybe_block

console = Console(stderr=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)],
)
logger = logging.getLogger("mcp.erp")

mcp = MCPServer("ERP Read-only API")

BASE_URL = os.getenv("ERP_API_BASE_URL", "").rstrip("/")
ERP_TOKEN = os.getenv("ERP_TOKEN", "").strip()
ERP_USERNAME = os.getenv("ERP_USERNAME", "").strip()
ERP_PASSWORD = os.getenv("ERP_PASSWORD", "").strip()

_session_token = ERP_TOKEN

WRITE_FRAGMENTS = (
    "/execute",
    "/finish",
    "/close",
    "/import",
    "/export",
    "/pkgoutbound",
    "/addwmsinventory",
    "/attachment",
    "/passwd",
    "/create",
    "/state/",
    "/revoke",
)

READ_POST_MARKERS = (
    "/query",
    "/list",
    "/page",
    "/select",
    "/locations",
    "/login",
    "/geterppkgandrep",
    "/materialcode",
    "/getbybomidandname",
)

# 单据资源白名单（erp_query_order / erp_downstream 等）
ORDER_RESOURCES = frozenset(
    {
        "sale",
        "purchase",
        "deliver",
        "outbound",
        "inbound",
        "arrival",
        "aftersale",
        "manufacture",
        "pick",
        "detect",
        "erp",
    }
)

STAT_KINDS = {
    "overview": "/statistics/overview",
    "product_delivery": "/statistics/product-delivery-stats",
    "sales_revenue": "/statistics/sales-revenue",
    "fund_flow": "/statistics/fund-flow",
    "inventory": "/statistics/inventory-statistics",
    "aftersale_delivery": "/statistics/aftersale-delivery-stats",
    "banner": "/user/banner",
}


def _normalize_path(path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path.split("?")[0]


def _is_allowed(method: str, path: str) -> bool:
    method = method.upper()
    lowered = path.lower()
    if method in ("PUT", "DELETE", "PATCH"):
        return False
    if any(frag in lowered for frag in WRITE_FRAGMENTS):
        return False
    if method == "GET":
        return True
    if method == "POST":
        return any(marker in lowered for marker in READ_POST_MARKERS)
    return False


def _is_unfiltered_collection(method: str, path: str) -> bool:
    if method.upper() != "GET":
        return False
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    return parts[-1].lower() in {"list", "query", "page", "select"}


def _extract_token(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    token = data.get("token") or payload.get("token") or ""
    return str(token).strip()


def _ensure_token() -> str:
    global _session_token
    if _session_token:
        return _session_token
    if not ERP_USERNAME or not ERP_PASSWORD:
        raise RuntimeError("未配置 ERP_TOKEN，且缺少 ERP_USERNAME / ERP_PASSWORD，无法登录")
    resp = requests.post(
        urljoin(BASE_URL + "/", "user/login"),
        json={"userName": ERP_USERNAME, "password": ERP_PASSWORD},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    token = _extract_token(body)
    if not token:
        raise RuntimeError(f"登录成功但未返回 token: {body}")
    _session_token = token
    logger.info("ERP 登录成功，已缓存 Token")
    return _session_token


def _page(page: int = 1, page_size: int = 20) -> dict[str, int]:
    return {"pageCurrent": max(1, int(page or 1)), "pageSize": min(50, max(1, int(page_size or 20)))}


def _call(
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
    tool_name: str = "erp_request",
) -> dict[str, Any]:
    method = (method or "GET").upper()
    path = _normalize_path(path)
    stopped = maybe_block(
        tool_name,
        {"method": method, "path": path, "params": params, "body": body},
    )
    if stopped:
        return stopped
    if not _is_allowed(method, path):
        logger.warning("拒绝非只读 ERP 请求: %s %s", method, path)
        return {"success": False, "error": f"只允许只读接口，已拒绝 {method} {path}"}
    if _is_unfiltered_collection(method, path):
        return {
            "success": False,
            "path": path,
            "error": "禁止无筛选 GET 集合。请用命名工具（如 erp_query_order）或 POST query。",
        }
    try:
        token = _ensure_token()
        headers = {"Token": token}
        url = urljoin(BASE_URL + "/", path.lstrip("/"))
        logger.info("ERP %s %s via %s", method, path, tool_name)
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params or None,
            json=body if method == "POST" else None,
            timeout=30,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text[:2000]}
        if resp.status_code == 401 or (
            isinstance(payload, dict) and payload.get("code") in (20001, 20002)
        ):
            global _session_token
            _session_token = ""
            token = _ensure_token()
            resp = requests.request(
                method,
                url,
                headers={"Token": token},
                params=params or None,
                json=body if method == "POST" else None,
                timeout=30,
            )
            try:
                payload = resp.json()
            except ValueError:
                payload = {"raw": resp.text[:2000]}
        result = {
            "success": resp.ok and (not isinstance(payload, dict) or payload.get("success", True)),
            "status_code": resp.status_code,
            "path": path,
            "data": payload,
        }
        args = {"method": method, "path": path, "params": params, "body": body}
        try:
            return filter_erp_result(path, result, args)
        except Exception:
            logger.exception("ERP 结果过滤失败")
            return result
    except Exception as e:
        logger.exception("ERP 请求失败")
        return {"success": False, "error": str(e), "path": path}


def _norm_resource(resource: str) -> str:
    r = (resource or "").strip().lower().lstrip("/")
    return r.split("/")[0] if r else ""


# ---------- 鉴权 ----------


@mcp.tool()
def erp_login():
    """登录 ERP 并缓存 Token（也可用环境变量 ERP_TOKEN）。"""
    global _session_token
    if ERP_TOKEN:
        _session_token = ERP_TOKEN
        return {"success": True, "token_ready": True, "source": "ERP_TOKEN"}
    _session_token = ""
    token = _ensure_token()
    return {"success": True, "token_ready": bool(token), "source": "password"}


# ---------- 看板 ----------


@mcp.tool()
def erp_weekly_delivery():
    """本周发货短问：overview + 机型发货结构。金额为 0 但台数≠0 不是未发货。"""
    overview = _call("GET", "/statistics/overview", tool_name="erp_weekly_delivery")
    structure = _call(
        "GET", "/statistics/product-delivery-stats", tool_name="erp_weekly_delivery"
    )
    return {"success": True, "overview": overview, "product_delivery": structure}


@mcp.tool()
def erp_statistics(kind: str):
    """看板单项。kind=overview|product_delivery|sales_revenue|fund_flow|inventory|aftersale_delivery|banner。
    本周发货短问请用 erp_weekly_delivery，不要为短问调 fund_flow/sales_revenue。"""
    key = (kind or "").strip().lower().replace("-", "_")
    path = STAT_KINDS.get(key)
    if not path:
        return {
            "success": False,
            "error": f"未知 kind={kind}，可选：{', '.join(STAT_KINDS)}",
        }
    return _call("GET", path, tool_name="erp_statistics")


# ---------- 单据 ----------


@mcp.tool()
def erp_query_order(
    resource: str,
    code: str = "",
    order_code: str = "",
    material_code: str = "",
    state: str = "",
    type: str = "",
    page: int = 1,
    page_size: int = 20,
):
    """按资源查单据列表。resource=sale|purchase|deliver|outbound|inbound|arrival|aftersale|manufacture|pick|detect|erp。
    优先传 code（本单号）；上游关联传 order_code。"""
    res = _norm_resource(resource)
    if res not in ORDER_RESOURCES:
        return {"success": False, "error": f"不支持的 resource={resource}，可选：{sorted(ORDER_RESOURCES)}"}
    body: dict[str, Any] = {}
    if code:
        body["code"] = code.strip()
    if order_code:
        body["orderCode"] = order_code.strip()
    if material_code:
        body["materialCode"] = material_code.strip()
    if state:
        body["state"] = state.strip()
    if type:
        body["type"] = type.strip()
    if not body:
        return {"success": False, "error": "至少提供 code / order_code / material_code / state / type 之一"}
    return _call(
        "POST",
        f"/{res}/query",
        params=_page(page, page_size),
        body=body,
        tool_name="erp_query_order",
    )


@mcp.tool()
def erp_order_by_code(resource: str, code: str):
    """按编码取单张单据。resource 常用 sale|purchase；path=/resource/code/{code}。"""
    res = _norm_resource(resource)
    code = (code or "").strip()
    if res not in {"sale", "purchase", "deliver", "detect"} or not code:
        return {"success": False, "error": "需要 resource=sale|purchase|deliver|detect 且提供 code"}
    return _call("GET", f"/{res}/code/{code}", tool_name="erp_order_by_code")


@mcp.tool()
def erp_downstream(resource: str, upstream_code: str):
    """查下游单据：GET /{resource}/list/orderCode/{上游单号}。
    例：发货挂销售 → resource=deliver, upstream_code=XS…；出库挂发货 → resource=outbound, upstream_code=FH…"""
    res = _norm_resource(resource)
    code = (upstream_code or "").strip()
    if res not in ORDER_RESOURCES or not code:
        return {"success": False, "error": "需要合法 resource 与 upstream_code"}
    return _call(
        "GET",
        f"/{res}/list/orderCode/{code}",
        tool_name="erp_downstream",
    )


@mcp.tool()
def erp_order_materials(order_code: str):
    """当前单据物料行：GET /order/material/list/orderMaterial/{当前单据code}。"""
    code = (order_code or "").strip()
    if not code:
        return {"success": False, "error": "需要 order_code"}
    return _call(
        "GET",
        f"/order/material/list/orderMaterial/{code}",
        tool_name="erp_order_materials",
    )


# ---------- SN / 主数据 / 库存 ----------


@mcp.tool()
def erp_sn_lookup(sn: str):
    """序列号/裸编码：先查检测与出入库记录。有结果即停，不要再当销售单号查。"""
    sn = (sn or "").strip()
    if not sn:
        return {"success": False, "error": "需要 sn"}
    hit = _call("GET", f"/order/record/list/material/{sn}", tool_name="erp_sn_lookup")
    data = hit.get("data") if isinstance(hit, dict) else None
    empty = False
    if isinstance(data, list) and not data:
        empty = True
    if isinstance(data, dict):
        records = data.get("data") or data.get("records") or data.get("list") or data.get("rows")
        if records == [] or data.get("total") == 0:
            empty = True
    if empty or (isinstance(hit, dict) and not hit.get("success")):
        return _call(
            "POST",
            "/order/record/query",
            params=_page(1, 20),
            body={"materialSn": sn},
            tool_name="erp_sn_lookup",
        )
    return hit


@mcp.tool()
def erp_lookup_customer(code: str = "", name: str = "", page: int = 1, page_size: int = 20):
    """客户档案。传 code 或 name。"""
    body: dict[str, Any] = {}
    if code:
        body["code"] = code.strip()
    if name:
        body["name"] = name.strip()
    if not body:
        return {"success": False, "error": "需要 code 或 name"}
    return _call(
        "POST",
        "/customer/query",
        params=_page(page, page_size),
        body=body,
        tool_name="erp_lookup_customer",
    )


@mcp.tool()
def erp_lookup_material(code: str = "", name: str = "", page: int = 1, page_size: int = 20):
    """物料档案。不要把 material.quantity 当库存。"""
    body: dict[str, Any] = {}
    if code:
        body["code"] = code.strip()
    if name:
        body["name"] = name.strip()
    if not body:
        return {"success": False, "error": "需要 code 或 name"}
    return _call(
        "POST",
        "/material/query",
        params=_page(page, page_size),
        body=body,
        tool_name="erp_lookup_material",
    )


@mcp.tool()
def erp_lookup_supplier(code: str = "", name: str = "", page: int = 1, page_size: int = 20):
    """供应商档案（内部主数据，不是供应商门户）。"""
    body: dict[str, Any] = {}
    if code:
        body["code"] = code.strip()
    if name:
        body["name"] = name.strip()
    if not body:
        return {"success": False, "error": "需要 code 或 name"}
    return _call(
        "POST",
        "/supplier/query",
        params=_page(page, page_size),
        body=body,
        tool_name="erp_lookup_supplier",
    )


@mcp.tool()
def erp_pkg_stock(material_code: str, page: int = 1, page_size: int = 20):
    """物料箱码库存（WMS pkg）。只返回有数量的语境由 skill 过滤。"""
    code = (material_code or "").strip()
    if not code:
        return {"success": False, "error": "需要 material_code"}
    primary = _call(
        "POST",
        "/wmsInventory/pkg/materialCode",
        body={"materialCode": code},
        tool_name="erp_pkg_stock",
    )
    if isinstance(primary, dict) and primary.get("success"):
        return primary
    return _call(
        "POST",
        "/pkg/getErpPkgAndRep",
        params=_page(page, page_size),
        body={"materialCode": code},
        tool_name="erp_pkg_stock",
    )


@mcp.tool()
def erp_pkg_history(pkg_code: str, page: int = 1, page_size: int = 20):
    """箱码流转履历。"""
    code = (pkg_code or "").strip()
    if not code:
        return {"success": False, "error": "需要 pkg_code"}
    return _call(
        "POST",
        "/wmsInventoryHistory/page",
        params=_page(page, page_size),
        body={"pkgCode": code},
        tool_name="erp_pkg_history",
    )


# ---------- 生产 / 付款 / BOM ----------


@mcp.tool()
def erp_recent_picks(page: int = 1, page_size: int = 20, order_code: str = ""):
    """领料查询。没给 SC 时只看近期领料；有生产单号可传 order_code。"""
    body: dict[str, Any] = {}
    if order_code:
        body["orderCode"] = order_code.strip()
    return _call(
        "POST",
        "/pick/query",
        params=_page(page, page_size),
        body=body or {},
        tool_name="erp_recent_picks",
    )


@mcp.tool()
def erp_payment_query(
    code: str = "",
    state: str = "",
    page: int = 1,
    page_size: int = 20,
):
    """付款单列表（待审核等）。短问「有要付的款吗」只用本工具，不要再打资金看板。"""
    body: dict[str, Any] = {}
    if code:
        body["code"] = code.strip()
    if state:
        body["state"] = state.strip()
    return _call(
        "POST",
        "/erp/query",
        params=_page(page, page_size),
        body=body or {},
        tool_name="erp_payment_query",
    )


@mcp.tool()
def erp_bom_cost(material_code: str, page: int = 1, page_size: int = 20):
    """机型 BOM：先查 BOM 头，再取第一项树（若有 id）。"""
    code = (material_code or "").strip()
    if not code:
        return {"success": False, "error": "需要 material_code"}
    head = _call(
        "POST",
        "/bom/query",
        params=_page(page, page_size),
        body={"materialCode": code},
        tool_name="erp_bom_cost",
    )
    bom_id = ""
    data = head.get("data") if isinstance(head, dict) else None
    rows = None
    if isinstance(data, dict):
        rows = data.get("records") or data.get("list") or data.get("rows") or data.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        bom_id = str(rows[0].get("id") or rows[0].get("bomId") or "")
    if not bom_id:
        return head
    tree = _call("GET", f"/bom/item/tree/{bom_id}", tool_name="erp_bom_cost")
    return {"success": True, "bom_query": head, "bom_tree": tree}


# ---------- 兜底 ----------


@mcp.tool()
def erp_request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
):
    """兜底：仅当命名工具覆盖不到时用。优先 erp_weekly_delivery / erp_query_order / erp_sn_lookup 等。"""
    return _call(method, path, params=params, body=body, tool_name="erp_request")


if __name__ == "__main__":
    logger.info("启动 ERP API MCP，base=%s", BASE_URL or "(未配置)")
    mcp.run()
