"""
Device Remote Operations MCP Server (rosiwit_cloud_remote)

rosiwit-cloud 云端运维（远程控制）：影子/录包/倒退/回桩/重启/重定位等。
鉴权：rosiwit_cloud_auth。统一 RemoteForm：{deviceId,productId,id,param}。
工单 CRUD 见 rosiwit_cloud_ticket；WebSocket 终端见 remote_terminal。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from mcp.server.fastmcp import FastMCP
from rich.console import Console
from rich.logging import RichHandler

import rosiwit_cloud_auth as cloud

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)],
)

logger = logging.getLogger("device-ops-mcp")

mcp = FastMCP("Device Operations MCP Server")

# ==================== rosiwit-cloud ====================
DEVICE_SHADOW_PATH = os.getenv("DEVICE_SHADOW_PATH", "/rosiwit-cloud/device/shadow")
REMOTE_PREFIX = os.getenv("REMOTE_API_PREFIX", "/rosiwit-cloud/remote")

_BAG_TIME_RE = re.compile(
    r"(?P<kind>all|task)_(?P<ts>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})_(?P<seq>\d+)",
    re.IGNORECASE,
)
_BAG_SLICE_SECONDS = 120
_TZ_CN = timezone(timedelta(hours=8))

_ROBOT_MODE_NAME = {
    "ROBOT_MODE_IDLE": "空闲",
    "ROBOT_MODE_TASK": "任务中",
    "ROBOT_MODE_PAUSE": "暂停状态",
    "ROBOT_MODE_FAULT": "错误发生",
    "ROBOT_MODE_MAP": "建图状态",
    "ROBOT_MODE_OTA": "OTA状态",
    "ROBOT_MODE_FACTORY": "工厂模式",
}
_CONTROL_MODE_NAME = {
    "CONTROL_MODE_MANUAL": "手动模式",
    "CONTROL_MODE_AUTO": "自动模式",
    0: "手动模式",
    1: "自动模式",
    "0": "手动模式",
    "1": "自动模式",
}
_SUPPLY_STATE_NAME = {
    0: "空闲",
    1: "前往工作站",
    2: "加排水中",
    3: "仅充电过程中",
    4: "退桩过程中",
    5: "手动补给",
    6: "等待补给外设全部关闭",
}
_SUPPLY_ENGAGED = {1, 2, 3}



def _remote_path(suffix: str) -> str:
    return f"{REMOTE_PREFIX.rstrip('/')}/{suffix.lstrip('/')}"


def _parse_param(param: Any) -> dict:
    if param is None or param == "":
        return {}
    if isinstance(param, dict):
        return param
    if isinstance(param, str):
        text = param.strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
            return loaded if isinstance(loaded, dict) else {"value": loaded}
        except json.JSONDecodeError:
            return {"raw": text}
    return {"value": param}


def _ok_result(result: Any, **extra: Any) -> dict:
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result, **extra}
    ok = bool(result.get("success") or result.get("returnCode") == 200)
    out = {
        "success": ok,
        "returnCode": result.get("returnCode"),
        "returnMsg": result.get("returnMsg"),
        "data": result.get("data"),
        "error": None if ok else (result.get("returnMsg") or result.get("error")),
    }
    out.update(extra)
    return out


def _remote_post(
    suffix: str,
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Optional[dict] = None,
) -> dict:
    """POST RemoteForm to /rosiwit-cloud/remote/..."""
    did = str(device_id or "").strip()
    pid = str(product_id or "").strip()
    if not did:
        return {"success": False, "error": "device_id 不能为空"}
    if not pid:
        return {"success": False, "error": "product_id 不能为空"}
    url = f"{cloud.get_base_url()}{_remote_path(suffix)}"
    body = {
        "id": int(req_id or 0),
        "deviceId": did,
        "productId": pid,
        "param": param if isinstance(param, dict) else {},
    }
    try:
        resp = cloud.request_with_reauth(
            "POST", url, headers=cloud.auth_headers(json_body=True), json=body
        )
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": False, "error": "empty response"}
    except requests.exceptions.RequestException as e:
        logger.error("远程控制失败 %s deviceId=%s: %s", suffix, did, e)
        return {"success": False, "error": str(e)}


def _remote_get(suffix: str, device_id: str, product_id: str, **extra_params: Any) -> dict:
    did = str(device_id or "").strip()
    pid = str(product_id or "").strip()
    if not did:
        return {"success": False, "error": "device_id 不能为空"}
    if not pid:
        return {"success": False, "error": "product_id 不能为空"}
    url = f"{cloud.get_base_url()}{_remote_path(suffix)}"
    params = {"deviceId": did, "productId": pid}
    for k, v in extra_params.items():
        if v is not None:
            params[k] = v
    try:
        resp = cloud.request_with_reauth(
            "GET", url, headers=cloud.auth_headers(), params=params
        )
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": False, "error": "empty response"}
    except requests.exceptions.RequestException as e:
        logger.error("远程查询失败 %s deviceId=%s: %s", suffix, did, e)
        return {"success": False, "error": str(e)}


def _extract_upload_url(result: Any) -> Optional[str]:
    if isinstance(result, str):
        text = result.strip()
        return text if text.startswith("http://") or text.startswith("https://") else None
    if not isinstance(result, dict):
        return None
    candidates: list[Any] = [
        result.get("url"),
        result.get("fileUrl"),
        result.get("ossUrl"),
        result.get("file_url"),
        result.get("oss_url"),
    ]
    data = result.get("data")
    if isinstance(data, str):
        candidates.append(data)
    elif isinstance(data, dict):
        candidates.extend(
            [
                data.get("url"),
                data.get("fileUrl"),
                data.get("ossUrl"),
                data.get("file_url"),
                data.get("oss_url"),
                data.get("filePath"),
                data.get("path"),
            ]
        )
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str) and item.startswith("http"):
                candidates.append(item)
            elif isinstance(item, dict):
                candidates.extend([item.get("url"), item.get("fileUrl"), item.get("ossUrl")])
    for item in candidates:
        if isinstance(item, str):
            text = item.strip()
            if text.startswith("http://") or text.startswith("https://"):
                return text
    return None


def _cloud_request_shadow(device_id: str, product_id: str) -> dict:
    url = f"{cloud.get_base_url()}{DEVICE_SHADOW_PATH}"
    params = {"deviceId": device_id, "productId": product_id}
    try:
        resp = cloud.request_with_reauth("GET", url, headers=cloud.auth_headers(), params=params)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": False, "error": "empty response"}
    except requests.exceptions.RequestException as e:
        logger.error("设备影子请求失败 deviceId=%s productId=%s: %s", device_id, product_id, e)
        return {"success": False, "error": str(e)}


def _cloud_request_bag_list(device_id: str, product_id: str, current: int = 1, size: int = 20) -> dict:
    return _remote_post(
        "bag/list",
        device_id,
        product_id,
        param={"pageNo": int(current), "pageSize": int(size)},
    )

def _cloud_request_backward(device_id: str, product_id: str) -> dict:
    return _remote_post("device/backward", device_id, product_id)


def _cloud_request_station_back(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Optional[dict] = None,
) -> dict:
    return _remote_post("station/back", device_id, product_id, req_id=req_id, param=param)


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y"):
            return True
        if lowered in ("false", "0", "no", "n"):
            return False
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _enum_name(mapping: dict, value: Any) -> Optional[str]:
    if value is None:
        return None
    if value in mapping:
        return mapping[value]
    if isinstance(value, str):
        key = value.strip()
        if key in mapping:
            return mapping[key]
        upper = key.upper()
        if upper in mapping:
            return mapping[upper]
    try:
        as_int = int(value)
        if as_int in mapping:
            return mapping[as_int]
    except (TypeError, ValueError):
        pass
    return None


def _summarize_fault(f: dict) -> dict:
    return {
        "code": f.get("code"),
        "name": f.get("name"),
        "level": f.get("level"),
        "module": f.get("module"),
        "fault_type": f.get("fault_type"),
        "fault_desc": f.get("fault_desc") or f.get("content") or "",
        "recovery_strategy": f.get("recovery_strategy"),
        "happenTime": f.get("happenTime"),
        "create_time": f.get("create_time") or f.get("createTime"),
        "update_time": f.get("update_time") or f.get("updateTime"),
    }


def _summarize_shadow(data: dict) -> dict:
    robot = data.get("cleanRobot") if isinstance(data.get("cleanRobot"), dict) else {}
    props = data.get("properties") if isinstance(data.get("properties"), dict) else {}
    faults_src = props.get("fault") or robot.get("currentFaults") or []
    faults = [_summarize_fault(f) for f in faults_src if isinstance(f, dict)]
    position = props.get("robot_position")
    if not isinstance(position, list) or len(position) < 2:
        position = [robot.get("x"), robot.get("y"), robot.get("theta")]
    battery = props.get("bms_soc")
    if battery is None:
        battery = robot.get("battery")
    robot_mode = (
        props.get("robotMode")
        or props.get("robot_mode")
        or robot.get("robotMode")
        or robot.get("state")
    )
    control_mode = props.get("control_mode")
    if control_mode is None:
        control_mode = props.get("controlMode") or robot.get("manual")
    supply_state = _as_int(
        props.get("supplyState")
        if props.get("supplyState") is not None
        else props.get("supply_state")
        if props.get("supply_state") is not None
        else robot.get("supplyState")
    )
    supply_engaged = supply_state in _SUPPLY_ENGAGED if supply_state is not None else None
    return {
        "deviceId": data.get("deviceId"),
        "productId": data.get("productId"),
        "isOnline": data.get("isOnline"),
        "lastMessageTime": data.get("lastMessageTime"),
        "battery": battery,
        "is_charge": props.get("is_charge", robot.get("charge")),
        "dock": props.get("dock", robot.get("dock")),
        "supplyState": supply_state,
        "supplyStateName": _enum_name(_SUPPLY_STATE_NAME, supply_state),
        "supplyEngaged": supply_engaged,
        "locate": props.get("locate", robot.get("locate")),
        "state": robot.get("state"),
        "robotMode": robot_mode,
        "robotModeName": _enum_name(_ROBOT_MODE_NAME, robot_mode),
        "robot_state": props.get("robot_state"),
        "control_mode": control_mode,
        "controlModeName": _enum_name(_CONTROL_MODE_NAME, control_mode),
        "clean_mode": props.get("clean_mode"),
        "map_name": props.get("map_name") or robot.get("currentMap"),
        "position": position,
        "water": robot.get("water"),
        "sewage": robot.get("sewage"),
        "waterSupply": robot.get("waterSupply"),
        "taskPercent": robot.get("taskPercent"),
        "taskPause": robot.get("taskPause"),
        "taskMode": robot.get("taskMode"),
        "crash": _as_bool(props.get("crash")),
        "fall": _as_bool(props.get("fall")),
        "enmergency": _as_bool(props.get("enmergency")),
        "cpu_used_percent": props.get("cpu_used_percent"),
        "memory_used_percent": props.get("memory_used_percent"),
        "disk_space_used_percent": props.get("disk_space_used_percent"),
        "free_disk_space": props.get("free_disk_space"),
        "hasFault": len(faults) > 0,
        "faultCount": len(faults),
        "faults": faults,
    }


def _parse_bag_start(file_name: str) -> Optional[datetime]:
    m = _BAG_TIME_RE.search(file_name or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group("ts"), "%Y-%m-%d-%H-%M-%S").replace(tzinfo=_TZ_CN)
    except ValueError:
        return None


def _parse_happen_time(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("/", "-")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ_CN)
        return dt.astimezone(_TZ_CN)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d-%H-%M-%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=_TZ_CN)
        except ValueError:
            continue
    return None


def _summarize_bag(rec: dict) -> dict:
    name = rec.get("file_name") or ""
    start = _parse_bag_start(name)
    kind = None
    m = _BAG_TIME_RE.search(name)
    if m:
        kind = m.group("kind").lower()
    end = start + timedelta(seconds=_BAG_SLICE_SECONDS) if start else None
    return {
        "file_name": name,
        "file_path": rec.get("file_path"),
        "file_size": rec.get("file_size"),
        "modify_time": rec.get("modify_time"),
        "kind": kind,
        "slice_start": start.strftime("%Y-%m-%d %H:%M:%S") if start else None,
        "slice_end": end.strftime("%Y-%m-%d %H:%M:%S") if end else None,
        "is_active": str(name).endswith(".active"),
    }


@mcp.tool()
def set_cloud_token(token: str):
    """手动设置 rosiwit-cloud API token。"""
    cloud.set_token(token)
    if not cloud.get_token():
        return {"success": False, "error": "token 为空"}
    logger.info("已手动设置 cloud token")
    return {"success": True, "message": "token 已设置"}


@mcp.tool()
def get_device_shadow(device_id: str, product_id: str):
    """获取设备实时状态（rosiwit-cloud 设备影子）。

    810 对桩看 supplyState∈{1,2,3} 或 supplyEngaged，不要单依赖 is_charge。
    is_charge=true 且 dock=false 一般表示手动充电（非工作站对桩）。
    """
    did = str(device_id or "").strip()
    pid = str(product_id or "").strip()
    if not did:
        return {"success": False, "error": "device_id 不能为空"}
    if not pid:
        return {"success": False, "error": "product_id 不能为空"}
    logger.info("获取设备影子: deviceId=%s productId=%s", did, pid)
    result = _cloud_request_shadow(did, pid)
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}
    if result.get("success") is False and result.get("data") is None:
        return {
            "success": False,
            "returnCode": result.get("returnCode"),
            "returnMsg": result.get("returnMsg") or result.get("error"),
            "hint": "检查 TICKET_API_BASE_URL / 账号，或 set_cloud_token",
        }
    data = result.get("data")
    if not isinstance(data, dict):
        return {
            "success": False,
            "returnCode": result.get("returnCode"),
            "returnMsg": result.get("returnMsg", "data 为空或非对象"),
            "raw": result,
        }
    summary = _summarize_shadow(data)
    summary["success"] = True
    summary["returnCode"] = result.get("returnCode", 200)
    return summary


@mcp.tool()
def list_device_bags(device_id: str, product_id: str, current: int = 1, size: int = 20):
    """云端录包列表。"""
    did = str(device_id or "").strip()
    pid = str(product_id or "").strip()
    if not did or not pid:
        return {"success": False, "error": "device_id/product_id 不能为空"}
    try:
        page = max(1, int(current))
        page_size = max(1, min(int(size), 100))
    except (TypeError, ValueError):
        return {"success": False, "error": "current/size 无效"}
    result = _cloud_request_bag_list(did, pid, page, page_size)
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}
    ok = bool(result.get("success") or result.get("returnCode") == 200)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    records = data.get("records") or []
    bags = [_summarize_bag(r) for r in records if isinstance(r, dict)]
    return {
        "success": ok,
        "deviceId": did,
        "productId": pid,
        "current": data.get("current", page),
        "pages": data.get("pages"),
        "size": data.get("size", page_size),
        "total": data.get("total"),
        "bags": bags,
        "returnCode": result.get("returnCode"),
        "returnMsg": result.get("returnMsg"),
        "error": None if ok else (result.get("returnMsg") or result.get("error")),
    }


@mcp.tool()
def device_backward(device_id: str, product_id: str):
    """云端倒退（碰撞脱困）。"""
    did = str(device_id or "").strip()
    pid = str(product_id or "").strip()
    if not did or not pid:
        return {"success": False, "error": "device_id/product_id 不能为空"}
    logger.info("云端倒退: deviceId=%s productId=%s", did, pid)
    result = _cloud_request_backward(did, pid)
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}
    ok = bool(result.get("success") or result.get("returnCode") == 200)
    return {
        "success": ok,
        "deviceId": did,
        "productId": pid,
        "returnCode": result.get("returnCode"),
        "returnMsg": result.get("returnMsg"),
        "data": result.get("data"),
        "error": None if ok else (result.get("returnMsg") or result.get("error")),
    }


@mcp.tool()
def device_back_to_station(device_id: str, product_id: str, req_id: int = 0):
    """云端回桩。

    先 get_device_shadow 看 supplyState；1/2/3 已对桩则不必下发；下发后再确认 supplyState。
    """
    did = str(device_id or "").strip()
    pid = str(product_id or "").strip()
    if not did or not pid:
        return {"success": False, "error": "device_id/product_id 不能为空"}
    try:
        rid = int(req_id or 0)
    except (TypeError, ValueError):
        return {"success": False, "error": "req_id 无效"}
    logger.info("云端回桩: deviceId=%s productId=%s id=%s", did, pid, rid)
    result = _cloud_request_station_back(did, pid, rid, {})
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}
    ok = bool(result.get("success") or result.get("returnCode") == 200)
    return {
        "success": ok,
        "deviceId": did,
        "productId": pid,
        "id": rid,
        "returnCode": result.get("returnCode"),
        "returnMsg": result.get("returnMsg"),
        "data": result.get("data"),
        "error": None if ok else (result.get("returnMsg") or result.get("error")),
    }


@mcp.tool()
def find_bags_near_time(
    device_id: str,
    product_id: str,
    happen_time: str,
    window_minutes: int = 5,
    max_pages: int = 10,
    prefer_kind: str = "all",
):
    """按时间匹配录包切片。"""
    did = str(device_id or "").strip()
    pid = str(product_id or "").strip()
    if not did or not pid:
        return {"success": False, "error": "device_id/product_id 不能为空"}
    target = _parse_happen_time(happen_time)
    if not target:
        return {"success": False, "error": f"无法解析 happen_time: {happen_time}"}
    try:
        window = max(0, int(window_minutes))
        pages = max(1, min(int(max_pages), 30))
    except (TypeError, ValueError):
        return {"success": False, "error": "window_minutes/max_pages 无效"}
    kind_pref = (prefer_kind or "all").strip().lower()
    if kind_pref not in ("all", "task", "any"):
        kind_pref = "all"

    all_bags: list = []
    seen: set = set()
    page_meta: dict = {}
    for page in range(1, pages + 1):
        result = _cloud_request_bag_list(did, pid, page, 50)
        if not isinstance(result, dict) or not (
            result.get("success") or result.get("returnCode") == 200
        ):
            if page == 1:
                return {
                    "success": False,
                    "error": (result or {}).get("returnMsg")
                    or (result or {}).get("error")
                    or "录包列表请求失败",
                    "raw": result,
                }
            break
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        page_meta = {
            "current": data.get("current"),
            "pages": data.get("pages"),
            "size": data.get("size"),
            "total": data.get("total"),
        }
        records = data.get("records") or []
        if not records:
            break
        new_count = 0
        for rec in records:
            if not isinstance(rec, dict):
                continue
            bag = _summarize_bag(rec)
            key = bag.get("file_path") or bag.get("file_name") or ""
            if not key or key in seen:
                continue
            seen.add(key)
            all_bags.append(bag)
            new_count += 1
        if new_count == 0:
            break
        try:
            if data.get("pages") is not None and page >= int(data.get("pages")):
                break
        except (TypeError, ValueError):
            pass

    timed = []
    for bag in all_bags:
        start = _parse_bag_start(bag.get("file_name") or "")
        if not start:
            continue
        end = start + timedelta(seconds=_BAG_SLICE_SECONDS)
        if start <= target <= end:
            distance, covers = 0.0, True
        elif target < start:
            distance, covers = (start - target).total_seconds(), False
        else:
            distance, covers = (target - end).total_seconds(), False
        timed.append({**bag, "covers_fault": covers, "distance_seconds": int(distance)})

    if not timed:
        return {
            "success": True,
            "deviceId": did,
            "productId": pid,
            "happen_time": target.strftime("%Y-%m-%d %H:%M:%S%z"),
            "matched": [],
            "matched_count": 0,
            "scanned": len(all_bags),
            "page_meta": page_meta,
            "hint": "未解析到任何带时间戳的录包文件名",
        }

    starts = [s for s in (_parse_bag_start(b["file_name"]) for b in timed) if s]
    available_newest = max(starts) if starts else None
    available_oldest = min(starts) if starts else None
    window_sec = window * 60
    candidates = [
        b for b in timed if b["covers_fault"] or abs(b["distance_seconds"]) <= window_sec
    ]

    def _sort_key(b):
        kind_rank = 0 if kind_pref == "any" or b.get("kind") == kind_pref else 1
        return (kind_rank, 1 if b.get("is_active") else 0, abs(b["distance_seconds"]), b.get("file_name") or "")

    candidates.sort(key=_sort_key)
    by_start = {
        _parse_bag_start(b["file_name"]): b
        for b in timed
        if _parse_bag_start(b["file_name"])
    }
    ordered_starts = sorted(by_start.keys())
    expanded = []
    seen_names: set = set()

    def _add(bag):
        if not bag:
            return
        name = bag.get("file_name") or ""
        if name in seen_names:
            return
        if bag.get("is_active") and not bag.get("covers_fault"):
            return
        seen_names.add(name)
        expanded.append(bag)

    for bag in candidates[:5]:
        _add(bag)
        st = _parse_bag_start(bag.get("file_name") or "")
        if not st or st not in ordered_starts:
            continue
        idx = ordered_starts.index(st)
        if idx > 0:
            _add(by_start[ordered_starts[idx - 1]])
        if idx + 1 < len(ordered_starts):
            _add(by_start[ordered_starts[idx + 1]])

    out_of_range = False
    hint = None
    if available_oldest and available_newest:
        bag_span_end = available_newest + timedelta(seconds=_BAG_SLICE_SECONDS)
        if target < available_oldest or target > bag_span_end:
            out_of_range = True
            hint = (
                f"故障时间不在当前可列出的录包范围内"
                f"（约 {available_oldest.strftime('%Y-%m-%d %H:%M:%S')}"
                f" ~ {bag_span_end.strftime('%Y-%m-%d %H:%M:%S')}）。"
            )
    if not expanded and not out_of_range:
        nearest = sorted(timed, key=lambda b: (b.get("is_active"), abs(b["distance_seconds"])))
        for bag in nearest[:3]:
            _add(bag)
        hint = hint or "窗口内无精确覆盖切片，已返回时间最近的录包"

    return {
        "success": True,
        "deviceId": did,
        "productId": pid,
        "happen_time": target.strftime("%Y-%m-%d %H:%M:%S%z"),
        "window_minutes": window,
        "matched": expanded,
        "matched_count": len(expanded),
        "scanned": len(all_bags),
        "available_newest": available_newest.strftime("%Y-%m-%d %H:%M:%S")
        if available_newest
        else None,
        "available_oldest": available_oldest.strftime("%Y-%m-%d %H:%M:%S")
        if available_oldest
        else None,
        "out_of_range": out_of_range,
        "page_meta": page_meta,
        "hint": hint,
        "note": "当前无 bag 内容解析接口；本工具仅按文件名时间定位故障附近切片。",
    }



# ==================== 老 FAE 能力对应的云端接口 ====================

def _extract_bag_name(result: Any, file_path: str = "") -> str:
    """从上传响应或路径提取录包文件名。"""
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            for key in ("bag_name", "file_name", "fileName", "name"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip().split("/")[-1]
        for key in ("bag_name", "file_name", "fileName"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().split("/")[-1]
    path = (file_path or "").strip().replace("\\", "/")
    if path:
        return path.split("/")[-1]
    return ""


@mcp.tool()
def upload_bag_file(
    device_id: str,
    product_id: str,
    file_path: str = "",
    timestamp: int = 0,
    param: Any = None,
    req_id: int = 0,
):
    """上传录包文件（POST /remote/bag/upload）。对应原 FAE upload_bag_file。

    param 默认含 filePath / timeStamp；也可直接传完整 param。
    成功时尽量返回顶层 url 便于 create_ticket_attachment。
    若 bag/upload 无 url，自动再试一次 bag/uploadList。
    url 仍为空时 attach_ready=false：禁止把「已定位录包」写成已挂附件。
    """
    p = _parse_param(param)
    if file_path and "filePath" not in p and "file_path" not in p:
        p["filePath"] = file_path
    if timestamp and "timeStamp" not in p and "timestamp" not in p:
        p["timeStamp"] = int(timestamp)
    resolved_path = str(p.get("filePath") or p.get("file_path") or file_path or "").strip()
    logger.info("上传录包: device=%s file=%s", device_id, resolved_path)
    result = _remote_post("bag/upload", device_id, product_id, req_id=req_id, param=p)
    out = _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())
    out["url"] = _extract_upload_url(result)
    out["bag_name"] = _extract_bag_name(result, resolved_path)
    out["fallback"] = None

    if out["success"] and not out["url"] and resolved_path:
        list_param = {
            "filePath": resolved_path,
            "filePaths": [resolved_path],
            "files": [resolved_path],
        }
        logger.info("upload 无 url，回退 uploadList: %s", resolved_path)
        result2 = _remote_post(
            "bag/uploadList", device_id, product_id, req_id=req_id, param=list_param
        )
        url2 = _extract_upload_url(result2)
        if url2:
            out["url"] = url2
            out["fallback"] = "uploadList"
            if isinstance(result2, dict) and result2.get("data") is not None:
                out["data"] = result2.get("data")
            name2 = _extract_bag_name(result2, resolved_path)
            if name2:
                out["bag_name"] = name2
        else:
            out["uploadList"] = {
                "returnCode": result2.get("returnCode") if isinstance(result2, dict) else None,
                "returnMsg": result2.get("returnMsg") if isinstance(result2, dict) else None,
                "data": result2.get("data") if isinstance(result2, dict) else None,
            }

    out["attach_ready"] = bool(out.get("url"))
    if out["success"] and not out["url"]:
        bag = out.get("bag_name") or resolved_path or "(unknown)"
        out["hint"] = (
            f"上传接口成功但未返回可挂附件 url（bag={bag}）。"
            f"禁止在评论「附件已挂载」中列入该录包；"
            f"须取得 url 后 create_ticket_attachment，或评论写明"
            f"「录包上传未返回 url，请人工挂载 {bag}」。"
        )
    return out


@mcp.tool()
def upload_bag_list(
    device_id: str,
    product_id: str,
    param: Any = None,
    req_id: int = 0,
    file_path: str = "",
):
    """批量上传故障录包（POST /remote/bag/uploadList）。

    可传 file_path 或 param.filePath / filePaths。成功时尽量返回顶层 url。
    """
    p = _parse_param(param)
    if file_path and "filePath" not in p and "file_path" not in p:
        p["filePath"] = file_path
        p.setdefault("filePaths", [file_path])
    result = _remote_post(
        "bag/uploadList", device_id, product_id, req_id=req_id, param=p
    )
    out = _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())
    out["url"] = _extract_upload_url(result)
    path = str(p.get("filePath") or file_path or "").strip()
    out["bag_name"] = _extract_bag_name(result, path)
    out["attach_ready"] = bool(out.get("url"))
    if out["success"] and not out["url"]:
        out["hint"] = (
            "uploadList 成功但未解析到 url；禁止把已定位录包写成已挂附件，"
            "评论须写请人工挂载或取得 url 后再 create_ticket_attachment。"
        )
    return out


@mcp.tool()
def soft_restart(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """机器重启（POST /remote/device/restart）。对应原 FAE soft_restart。"""
    logger.info("云端重启: %s / %s", device_id, product_id)
    result = _remote_post(
        "device/restart", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def factory_reset(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """设备恢复出厂设置（POST /remote/device/reset）。对应原 FAE factory_reset。慎用。"""
    result = _remote_post(
        "device/reset", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def set_control_mode(
    device_id: str,
    product_id: str,
    mode: int = 0,
    req_id: int = 0,
    param: Any = None,
):
    """切换手动/自动（POST /remote/device/manual）。对应原 FAE set_control_mode。

    mode: 0=手动, 1=自动（写入 param.mode，除非 param 已提供）。
    """
    p = _parse_param(param)
    if "mode" not in p:
        p["mode"] = int(mode)
    result = _remote_post("device/manual", device_id, product_id, req_id=req_id, param=p)
    return _ok_result(
        result,
        deviceId=str(device_id).strip(),
        productId=str(product_id).strip(),
        mode=p.get("mode"),
    )


@mcp.tool()
def relocate(
    device_id: str,
    product_id: str,
    position: Any = None,
    req_id: int = 0,
    param: Any = None,
):
    """地图重定位（POST /remote/map/relocation）。对应原 FAE relocate。

    position 可为 [x,y,theta] 或写入 param。
    """
    p = _parse_param(param)
    if position is not None and "position" not in p:
        if isinstance(position, str):
            try:
                position = json.loads(position)
            except json.JSONDecodeError:
                pass
        p["position"] = position
    result = _remote_post("map/relocation", device_id, product_id, req_id=req_id, param=p)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def station_relocation(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """工作站重定位（POST /remote/station/relocation）。"""
    result = _remote_post(
        "station/relocation",
        device_id,
        product_id,
        req_id=req_id,
        param=_parse_param(param),
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def station_dock(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """手动补给（POST /remote/station/dock）。"""
    result = _remote_post(
        "station/dock", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def get_clean_info(device_id: str, product_id: str):
    """获取清洁组件信息（GET /remote/device/cleanInfo）。对应原 FAE get_clean_info。"""
    result = _remote_get("device/cleanInfo", device_id, product_id)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def device_clean(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """设备手动清洗控制（POST /remote/device/clean）。"""
    result = _remote_post(
        "device/clean", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def get_camera_image(
    device_id: str,
    product_id: str,
    camera: str = "前",
    req_id: int = 0,
    param: Any = None,
    upload_oss: bool = True,
):
    """获取机器摄像头实时图片（POST /remote/device/camera/image）。

    camera 传中文方位即可（云端解码为设备侧 1/2/3/4）：
    - SC50/SW50：前、下（对应 1、2）
    - T810：前、后、左、右（对应 1、2、3、4）
    也可传数字字符串 \"1\"~\"4\"。

    接口通常返回 data.base64（JPEG）。默认 upload_oss=True：上传到
    /rosiwit-cloud/file/upload，返回顶层 url，便于 create_ticket_attachment。
    响应中不回传完整 base64，避免撑爆上下文。
    """
    import base64
    from datetime import datetime

    p = _parse_param(param)
    cam = (camera or "").strip() or "前"
    digit_to_cn = {"1": "前", "2": "下", "3": "左", "4": "右"}
    pid = str(product_id or "").upper()
    if cam in ("2",) and any(x in pid for x in ("TITAN", "T810", "810")):
        cam = "后"
    elif cam in digit_to_cn:
        cam = digit_to_cn[cam]
    aliases = {"前视": "前", "下视": "下", "后视": "后", "左视": "左", "右视": "右"}
    cam = aliases.get(cam, cam)
    if "camera" not in p:
        p["camera"] = cam
    logger.info("获取相机图: device=%s product=%s camera=%s", device_id, product_id, p.get("camera"))
    result = _remote_post(
        "device/camera/image",
        device_id,
        product_id,
        req_id=req_id,
        param=p,
    )
    out = _ok_result(
        result,
        deviceId=str(device_id).strip(),
        productId=str(product_id).strip(),
        camera=p.get("camera"),
    )
    out["url"] = _extract_upload_url(result)

    data = result.get("data") if isinstance(result, dict) else None
    b64 = None
    if isinstance(data, dict):
        b64 = data.get("base64") or data.get("imageBase64") or data.get("img")
    elif isinstance(data, str) and len(data) > 100:
        b64 = data

    if out["success"] and not out["url"] and upload_oss and b64:
        try:
            raw = str(b64).strip()
            if "," in raw and raw.lower().startswith("data:"):
                raw = raw.split(",", 1)[1]
            content = base64.b64decode(raw)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"camera_{device_id}_{p.get('camera')}_{ts}.jpg"
            up = cloud.upload_file_bytes(content, fname, content_type="image/jpeg")
            if up.get("success") and up.get("url"):
                out["url"] = up["url"]
                out["upload"] = {"name": up.get("name"), "success": True}
            else:
                out["upload"] = {
                    "success": False,
                    "error": up.get("error") or up.get("returnMsg"),
                }
                out["hint"] = "相机图已取到但 OSS 上传失败，无法直接挂附件"
        except Exception as e:
            logger.error("相机图 OSS 上传异常: %s", e)
            out["upload"] = {"success": False, "error": str(e)}
            out["hint"] = "相机图 base64 解码或上传失败"

    # 压缩 data：去掉巨型 base64，只保留元信息
    if isinstance(data, dict):
        slim = {k: v for k, v in data.items() if k not in ("base64", "imageBase64", "img")}
        if b64:
            slim["has_base64"] = True
            slim["base64_len"] = len(str(b64))
        out["data"] = slim
    elif b64:
        out["data"] = {"has_base64": True, "base64_len": len(str(b64))}

    if out["success"] and not out["url"]:
        out["hint"] = out.get("hint") or "获取成功但未得到可挂附件 url"
    return out


@mcp.tool()
def get_point_cloud(device_id: str, product_id: str):
    """激光雷达点云（GET /remote/point_cloud）。对应原 FAE get_point_cloud。"""
    result = _remote_get("point_cloud", device_id, product_id)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def start_factory_mode(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """开启工程模式（POST /remote/device/factory/switch）。param 可含 enable/mode 等。"""
    p = _parse_param(param)
    if "enable" not in p and "mode" not in p and "switch" not in p:
        p["enable"] = True
    result = _remote_post("device/factory/switch", device_id, product_id, req_id=req_id, param=p)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def stop_factory_mode(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """退出工程模式（POST /remote/device/factory/switch）。"""
    p = _parse_param(param)
    if "enable" not in p and "mode" not in p and "switch" not in p:
        p["enable"] = False
    result = _remote_post("device/factory/switch", device_id, product_id, req_id=req_id, param=p)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def get_factory_params(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """获取工程模式参数（POST /remote/device/factory/setting/get）。"""
    result = _remote_post(
        "device/factory/setting/get",
        device_id,
        product_id,
        req_id=req_id,
        param=_parse_param(param),
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def set_factory_params(
    device_id: str,
    product_id: str,
    param: Any = None,
    req_id: int = 0,
):
    """设置工程模式参数（POST /remote/device/factory/setting/set）。业务字段放 param。"""
    result = _remote_post(
        "device/factory/setting/set",
        device_id,
        product_id,
        req_id=req_id,
        param=_parse_param(param),
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def reset_factory_params(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """工程模式参数重置（POST /remote/device/factory/setting/reset）。"""
    result = _remote_post(
        "device/factory/setting/reset",
        device_id,
        product_id,
        req_id=req_id,
        param=_parse_param(param),
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def factory_control(
    device_id: str,
    product_id: str,
    param: Any = None,
    req_id: int = 0,
):
    """工程模式控制 sw50/gt（POST /remote/device/factory/control）。"""
    result = _remote_post(
        "device/factory/control",
        device_id,
        product_id,
        req_id=req_id,
        param=_parse_param(param),
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def get_pending_task(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """获取断点续扫任务（POST /remote/task/pending/list）。对应原 FAE get_pending_task。"""
    result = _remote_post(
        "task/pending/list",
        device_id,
        product_id,
        req_id=req_id,
        param=_parse_param(param),
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def resume_pending_task(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """开始断点续扫（POST /remote/task/pending/resume）。对应原 FAE resume_pending_task。"""
    result = _remote_post(
        "task/pending/resume",
        device_id,
        product_id,
        req_id=req_id,
        param=_parse_param(param),
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def plan_path(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """路径规划（POST /remote/path/plan）。对应原 FAE plan_path。"""
    result = _remote_post(
        "path/plan", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def list_schedules(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """定时任务列表（POST /remote/schedule/list）。"""
    result = _remote_post(
        "schedule/list", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def create_schedule(
    device_id: str,
    product_id: str,
    param: Any = None,
    req_id: int = 0,
):
    """定时任务创建（POST /remote/schedule/create）。"""
    result = _remote_post(
        "schedule/create", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def update_schedule(
    device_id: str,
    product_id: str,
    param: Any = None,
    req_id: int = 0,
):
    """定时任务更新（POST /remote/schedule/update）。"""
    result = _remote_post(
        "schedule/update", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def delete_schedule(
    device_id: str,
    product_id: str,
    param: Any = None,
    req_id: int = 0,
):
    """定时任务删除（POST /remote/schedule/delete）。"""
    result = _remote_post(
        "schedule/delete", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def execute_terminal(
    device_id: str,
    product_id: str,
    command: str = "",
    req_id: int = 0,
    param: Any = None,
):
    """向机器执行命令（POST /remote/device/terminal/execute）。

    与 WebSocket remote_terminal 互补；command 写入 param.command（除非 param 已含）。
    """
    p = _parse_param(param)
    if command and "command" not in p and "cmd" not in p:
        p["command"] = command
    result = _remote_post(
        "device/terminal/execute", device_id, product_id, req_id=req_id, param=p
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def get_device_setting(
    device_id: str,
    product_id: str,
):
    """获取系统参数（GET /remote/device/setting/get）。"""
    result = _remote_get("device/setting/get", device_id, product_id)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def set_device_setting(
    device_id: str,
    product_id: str,
    param: Any = None,
    req_id: int = 0,
):
    """设置系统参数（POST /remote/device/setting/set）。"""
    result = _remote_post(
        "device/setting/set",
        device_id,
        product_id,
        req_id=req_id,
        param=_parse_param(param),
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def list_consumables(
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """消耗品寿命列表（POST /remote/consumable/list）。"""
    result = _remote_post(
        "consumable/list", device_id, product_id, req_id=req_id, param=_parse_param(param)
    )
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def remote_action(
    action: str,
    device_id: str,
    product_id: str,
    req_id: int = 0,
    param: Any = None,
):
    """通用远程控制：action 为 /remote/ 后路径，如 device/garbage/switch、map/list、task/control。

    用于尚未单独封装的接口；优先使用具名工具。
    """
    suffix = str(action or "").strip().lstrip("/")
    if not suffix or ".." in suffix:
        return {"success": False, "error": "action 无效"}
    result = _remote_post(suffix, device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(
        result,
        action=suffix,
        deviceId=str(device_id).strip(),
        productId=str(product_id).strip(),
    )


# ==================== 补充具名工具：覆盖全部 /remote/* 端点 ====================
# 说明：以下工具与上面的具名工具风格一致，走 rosiwit_cloud_auth 鉴权 + RemoteForm 信封。
# 未明确业务字段的接口统一以 param(JSON/dict) 透传设备端参数。


@mcp.tool()
def device_initiate(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """设备初始化（POST /remote/device/initiate）。"""
    result = _remote_post("device/initiate", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def factory_calib_camera(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """相机标定（POST /remote/device/factory/calib/camera）。"""
    result = _remote_post("device/factory/calib/camera", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def factory_test(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """调试测试 SW50 GT（POST /remote/device/factory/test）。"""
    result = _remote_post("device/factory/test", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def camera_image(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """机器摄像头图片获取（旧接口，POST /remote/camera/image）。新版本见 get_camera_image。"""
    result = _remote_post("camera/image", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


# ---------- 地图 ----------

@mcp.tool()
def map_list(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图列表（POST /remote/map/list）。param: {pageNo, pageSize}"""
    result = _remote_post("map/list", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_detail(device_id: str, product_id: str, map_id: int):
    """地图详情（GET /remote/map/{id}）。map_id=地图云端数据库ID。"""
    result = _remote_get(f"map/{int(map_id)}", device_id, product_id)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip(), id=int(map_id))


@mcp.tool()
def map_save(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图保存（POST /remote/map/save）。"""
    result = _remote_post("map/save", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_update(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图更新（POST /remote/map/update）。param: id/aliasId/map_name/floor/building/origin/resolution/update_time"""
    result = _remote_post("map/update", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_image_update(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图图片更新（橡皮擦，POST /remote/map/image/update）。param: id/aliasId/map_data/type"""
    result = _remote_post("map/image/update", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_image_rotate(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图图片旋转（POST /remote/map/image/rotate）。"""
    result = _remote_post("map/image/rotate", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_delete(device_id: str, product_id: str, map_id: Optional[int] = None, req_id: int = 0, param: Any = None):
    """地图删除（POST /remote/map/delete）。param: id(地图云端ID)，或传 map_id。"""
    p = _parse_param(param)
    if map_id is not None and "id" not in p:
        p["id"] = int(map_id)
    result = _remote_post("map/delete", device_id, product_id, req_id=req_id, param=p)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_copy(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """复制地图（POST /remote/map/copy）。"""
    result = _remote_post("map/copy", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_switch(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """默认地图切换（POST /remote/map/switch）。"""
    result = _remote_post("map/switch", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_cover(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图覆盖物信息（POST /remote/map/cover）。"""
    result = _remote_post("map/cover", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_cover_create(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图覆盖物创建（POST /remote/map/cover/create）。"""
    result = _remote_post("map/cover/create", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_cover_update(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图覆盖物更新（POST /remote/map/cover/update）。"""
    result = _remote_post("map/cover/update", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_cover_update_all(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图覆盖物全量更新（POST /remote/map/cover/update/all）。param: {map_id, data}"""
    result = _remote_post("map/cover/update/all", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def map_cover_delete(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """地图覆盖物删除（POST /remote/map/cover/delete）。"""
    result = _remote_post("map/cover/delete", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


# ---------- 路径 ----------

@mcp.tool()
def path_list(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """路径列表（POST /remote/path/list）。param: {pageNo, pageSize}"""
    result = _remote_post("path/list", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def path_detail(device_id: str, product_id: str, path_id: int):
    """路径详情-查云端（GET /remote/path/detail/{id}）。path_id=路径云端数据库ID。"""
    result = _remote_get(f"path/detail/{int(path_id)}", device_id, product_id)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip(), id=int(path_id))


@mcp.tool()
def path_update(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """路径更新（POST /remote/path/update）。"""
    result = _remote_post("path/update", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def path_delete(device_id: str, product_id: str, path_id: Optional[int] = None, req_id: int = 0, param: Any = None):
    """路径删除（POST /remote/path/delete）。param: id(路径云端ID)，或传 path_id。"""
    p = _parse_param(param)
    if path_id is not None and "id" not in p:
        p["id"] = int(path_id)
    result = _remote_post("path/delete", device_id, product_id, req_id=req_id, param=p)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def path_record(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """路径记录（POST /remote/path/record）。param: name/map_id/type(path_type)/area/aliasId/id"""
    result = _remote_post("path/record", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


# ---------- 任务 ----------

@mcp.tool()
def task_list(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """任务列表（POST /remote/task/list）。param: {pageNo, pageSize}"""
    result = _remote_post("task/list", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def task_create(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """任务创建（POST /remote/task/create）。"""
    result = _remote_post("task/create", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def task_update(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """任务更新（POST /remote/task/update）。"""
    result = _remote_post("task/update", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def task_delete(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """任务删除（POST /remote/task/delete）。param: id(任务云端ID)"""
    result = _remote_post("task/delete", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def task_control(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """任务控制-开始/暂停/停止（POST /remote/task/control）。"""
    result = _remote_post("task/control", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def task_start_general(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """开始普通任务（POST /remote/task/start_general_task）。"""
    result = _remote_post("task/start_general_task", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def task_report_list(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """任务报告列表（POST /remote/task/report/list）。param: {pageNo, pageSize}"""
    result = _remote_post("task/report/list", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


# ---------- 消耗品 / 垃圾 ----------

@mcp.tool()
def consumable_reset(device_id: str, product_id: str, type: Optional[int] = None, req_id: int = 0, param: Any = None):
    """重置消耗品寿命（POST /remote/device/consumable/reset）。param: type(消耗品类型)，或传 type。"""
    p = _parse_param(param)
    if type is not None and "type" not in p:
        p["type"] = int(type)
    result = _remote_post("device/consumable/reset", device_id, product_id, req_id=req_id, param=p)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def consumable_set(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """设置消耗品寿命（POST /remote/device/consumable/set）。param: 消耗品类型/寿命等。"""
    result = _remote_post("device/consumable/set", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def garbage_switch(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """控制倒垃圾（POST /remote/device/garbage/switch）。"""
    result = _remote_post("device/garbage/switch", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


# ---------- 视频 ----------

@mcp.tool()
def video_control(device_id: str, product_id: str, flag: Optional[bool] = None, req_id: int = 0, param: Any = None):
    """设备视频控制（POST /remote/video/control）。param: flag(开关)，或传 flag。"""
    p = _parse_param(param)
    if flag is not None and "flag" not in p:
        p["flag"] = bool(flag)
    result = _remote_post("video/control", device_id, product_id, req_id=req_id, param=p)
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


@mcp.tool()
def video_heartbeat(device_id: str, product_id: str, req_id: int = 0, param: Any = None):
    """设备视频心跳（POST /remote/video/heartbeat）。"""
    result = _remote_post("video/heartbeat", device_id, product_id, req_id=req_id, param=_parse_param(param))
    return _ok_result(result, deviceId=str(device_id).strip(), productId=str(product_id).strip())


if __name__ == "__main__":
    mcp.run()
