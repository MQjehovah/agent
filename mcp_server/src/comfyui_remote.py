"""
ComfyUI Remote MCP Server - 远程 ComfyUI 调用

通过 ComfyUI 原生 HTTP API (http://host:port) 远程驱动其它机器上的 ComfyUI：
- 查询服务器状态 / 硬件 / 显存 / 队列
- 列出模型、搜索节点、查看节点输入定义
- 上传输入图片
- 提交工作流（API 格式或 UI 导出格式）、等待完成、取回输出文件与图片
- 中断任务、清空队列、释放显存

环境变量:
- COMFYUI_URL        目标 ComfyUI 地址，默认 http://192.168.31.34:8188
- COMFYUI_TIMEOUT    单次 HTTP 请求超时(秒)，默认 30
- COMFYUI_INLINE_BYTES 单张图片内联返回上限(字节)，默认 5MB
"""
import os
import io
import json
import time
import uuid
import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional, List, Dict, Union

import requests
from mcp.server.mcpserver import MCPServer, Image
from rich.logging import RichHandler
from rich.console import Console

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("comfyui-mcp")

mcp = MCPServer("ComfyUI Remote MCP Server")

# ============================================================================
# 配置常量
# ============================================================================

DEFAULT_SERVER = os.getenv("COMFYUI_URL", "http://192.168.31.34:8188")
HTTP_TIMEOUT = float(os.getenv("COMFYUI_TIMEOUT", "30"))
INLINE_LIMIT = int(os.getenv("COMFYUI_INLINE_BYTES", str(5 * 1024 * 1024)))

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}
TEXT_KEYS = ("text",)
FILE_KEYS = ("images", "gifs", "videos", "audio", "files", "video", "audio_file")

STATE: Dict[str, Any] = {
    "base_url": DEFAULT_SERVER.rstrip("/"),
    "client_id": uuid.uuid4().hex,
}

_object_info_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
OBJECT_INFO_TTL = 300.0
TEMP_DIR = Path(os.getenv("TEMP") or os.getenv("TMP") or ".") / "comfyui_mcp"


# ============================================================================
# HTTP 基础
# ============================================================================

class ComfyError(Exception):
    """ComfyUI 请求错误"""


def _base_url() -> str:
    return STATE["base_url"]


def _request(method: str, path: str, *, timeout: Optional[float] = None, **kwargs):
    url = _base_url() + path
    try:
        # requests.Session 非线程安全; v2 同步 handler 跑在 anyio worker 线程且可能并发,
        # 故不再全局复用 Session, 每次请求由 requests.request 自建独立 Session(短连接, 换取安全)。
        resp = requests.request(method, url, timeout=timeout or HTTP_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise ComfyError(f"无法连接 ComfyUI ({url}): {exc}") from exc
    if resp.status_code >= 400:
        detail = resp.text[:800]
        raise ComfyError(f"HTTP {resp.status_code} {url}: {detail}")
    return resp


def _get_json(path: str, **params):
    return _request("GET", path, params=params or None).json()


def _object_info(refresh: bool = False):
    now = time.time()
    if refresh or _object_info_cache["data"] is None or now - _object_info_cache["ts"] > OBJECT_INFO_TTL:
        data = _get_json("/object_info", timeout=120)
        _object_info_cache.update(data=data, ts=now)
    return _object_info_cache["data"]


def _norm_ext_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return {"jpg": "jpeg"}.get(suffix, suffix or "png")


# ============================================================================
# UI 导出格式 -> API 格式 转换
# ============================================================================

def _is_ui_graph(wf: Dict[str, Any]) -> bool:
    return isinstance(wf, dict) and isinstance(wf.get("nodes"), list)


def _widget_input_names(spec: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    inp = spec.get("input") or {}
    for section in ("required", "optional"):
        for name, item in (inp.get(section) or {}).items():
            if not isinstance(item, list) or not item:
                continue
            type_spec = item[0]
            opts = item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            if opts.get("forceInput"):
                continue
            if isinstance(type_spec, list) or type_spec in WIDGET_TYPES:
                names.append(name)
    return names


def _has_control_after_generate(spec: Dict[str, Any], name: str) -> bool:
    inp = spec.get("input") or {}
    for section in ("required", "optional"):
        item = (inp.get(section) or {}).get(name)
        if isinstance(item, list) and len(item) > 1 and isinstance(item[1], dict):
            return bool(item[1].get("control_after_generate"))
    return False


def _node_widget_names(spec: Dict[str, Any], node: Dict[str, Any]) -> List[str]:
    names = _widget_input_names(spec)
    converted = set()
    for slot in node.get("inputs") or []:
        widget = slot.get("widget")
        if isinstance(widget, dict) and widget.get("name"):
            converted.add(widget["name"])
        elif isinstance(widget, str):
            converted.add(widget)
        elif slot.get("link") is not None and slot.get("name") in names:
            converted.add(slot["name"])
    return [n for n in names if n not in converted]


def ui_graph_to_api(graph: Dict[str, Any]) -> tuple:
    """把 ComfyUI 前端导出的 UI 格式转换为 /prompt 需要的 API 格式"""
    info = _object_info()
    nodes = graph.get("nodes") or []
    links = graph.get("links") or []

    link_map: Dict[Any, list] = {}
    for link in links:
        if isinstance(link, list) and len(link) >= 5:
            link_map[link[0]] = [str(link[1]), int(link[2])]
        elif isinstance(link, dict) and "id" in link:
            link_map[link["id"]] = [str(link.get("origin_id")), int(link.get("origin_slot") or 0)]

    api: Dict[str, Any] = {}
    warnings: List[str] = []

    for node in nodes:
        node_id = str(node.get("id"))
        class_type = node.get("type") or node.get("class_type")
        mode = node.get("mode")
        if class_type in ("Note", "MarkdownNote"):
            continue
        if mode == 2:
            warnings.append(f"节点 {node_id} ({class_type}) 已静音，跳过")
            continue
        spec = info.get(class_type)
        if spec is None:
            warnings.append(f"节点 {node_id} 类型 {class_type} 在目标 ComfyUI 上不存在，已忽略")
            continue
        if mode == 4:
            warnings.append(f"节点 {node_id} ({class_type}) 处于 Bypass 模式，已按普通节点提交")

        inputs: Dict[str, Any] = {}
        for slot in node.get("inputs") or []:
            link_id = slot.get("link")
            if link_id is None or slot.get("name") is None:
                continue
            source = link_map.get(link_id)
            if source is None:
                continue
            inputs[slot["name"]] = [source[0], source[1]]

        values = node.get("widgets_values") or []
        cursor = 0
        for widget_name in _node_widget_names(spec, node):
            if cursor >= len(values):
                break
            inputs[widget_name] = values[cursor]
            cursor += 1
            if _has_control_after_generate(spec, widget_name):
                cursor += 1
        if cursor < len(values):
            warnings.append(
                f"节点 {node_id} ({class_type}) 有 {len(values) - cursor} 个控件值未能映射，"
                f"请确认节点版本一致（必要时改用 API 格式工作流）"
            )

        api[node_id] = {
            "class_type": class_type,
            "inputs": inputs,
            "_meta": {"title": node.get("title") or class_type},
        }

    for node_id, node in api.items():
        for name, value in list(node["inputs"].items()):
            if isinstance(value, str) and value in api and name in ("model", "clip", "vae"):
                node["inputs"][name] = [value, 0]

    return api, warnings


def _load_workflow(workflow: Optional[Union[dict, str]], workflow_file: Optional[str]) -> tuple:
    raw: Any = workflow
    if raw is None and workflow_file:
        path = Path(workflow_file).expanduser()
        if not path.exists():
            raise ComfyError(f"工作流文件不存在: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
    if raw is None:
        raise ComfyError("必须提供 workflow (dict) 或 workflow_file (路径)")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ComfyError("工作流必须是 JSON 对象")

    if _is_ui_graph(raw):
        api, warnings = ui_graph_to_api(raw)
        if not api:
            raise ComfyError("UI 格式工作流转换后为空，请改用 Export (API) 导出的工作流")
        return api, warnings

    for key, value in raw.items():
        if not isinstance(value, dict) or "class_type" not in value:
            raise ComfyError(
                f"无法识别工作流格式（节点 {key} 缺少 class_type）。"
                f"请使用 ComfyUI 的 Workflow -> Export (API) 导出的 JSON。"
            )
    return raw, []


# ============================================================================
# 校验 / 队列 / 历史 / 输出
# ============================================================================

def validate_api_workflow(api: Dict[str, Any]):
    info = _object_info()
    errors: List[str] = []
    node_ids = set(api.keys())
    for node_id, node in api.items():
        class_type = node.get("class_type")
        spec = info.get(class_type)
        if spec is None:
            errors.append(f"节点 {node_id}: 未知节点类型 {class_type}")
            continue
        declared = set((spec.get("input") or {}).get("required") or {})
        declared |= set((spec.get("input") or {}).get("optional") or {})
        for name, value in (node.get("inputs") or {}).items():
            if name not in declared:
                errors.append(f"节点 {node_id} ({class_type}): 输入 {name} 不存在")
            if isinstance(value, list) and len(value) == 2 and str(value[0]) not in node_ids:
                errors.append(f"节点 {node_id} ({class_type}): 输入 {name} 指向不存在的节点 {value[0]}")
    return {"valid": not errors, "errors": errors, "node_count": len(api)}


def _history_entry(prompt_id: str) -> Optional[Dict[str, Any]]:
    data = _get_json(f"/history/{prompt_id}")
    return data.get(prompt_id) if isinstance(data, dict) else None


def _queue_state(prompt_id: str) -> str:
    """running / pending / unknown"""
    queue = _get_json("/queue")
    for key, state in (("queue_running", "running"), ("queue_pending", "pending")):
        for item in queue.get(key) or []:
            if len(item) > 1 and str(item[1]) == prompt_id:
                return state
    return "unknown"


def _job_status(prompt_id: str):
    entry = _history_entry(prompt_id)
    if not entry:
        state = _queue_state(prompt_id)
        return {"prompt_id": prompt_id, "completed": False, "status": state}
    status = entry.get("status") or {}
    messages = status.get("messages") or []
    errors: List[str] = []
    for message in messages:
        if isinstance(message, list) and len(message) > 1 and message[0] in ("execution_error", "execution_interrupted"):
            errors.append(json.dumps(message[1], ensure_ascii=False)[:600])
    return {
        "prompt_id": prompt_id,
        "completed": bool(status.get("completed")),
        "status": status.get("status_str") or ("success" if status.get("completed") else "unknown"),
        "errors": errors,
        "outputs": (entry.get("outputs") or {}),
    }


def _iter_outputs(outputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for node_id, node_out in (outputs or {}).items():
        if not isinstance(node_out, dict):
            continue
        for key, values in node_out.items():
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, dict):
                    item = dict(value)
                    item["kind"] = key
                    item["node_id"] = node_id
                    items.append(item)
                elif isinstance(value, str) and key in TEXT_KEYS:
                    items.append({"kind": "text", "node_id": node_id, "text": value})
    return items


def _download_item(item: Dict[str, Any], out_dir: Path) -> Optional[Path]:
    filename = item.get("filename")
    if not filename:
        return None
    resp = _request(
        "GET",
        "/view",
        params={
            "filename": filename,
            "subfolder": item.get("subfolder") or "",
            "type": item.get("type") or "output",
        },
        timeout=180,
    )
    subfolder = Path(item.get("subfolder") or "")
    target = out_dir / subfolder / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(resp.content)
    return target


def _collect(prompt_id: str, out_dir: Optional[str], inline: bool, max_inline: int):
    entry = _history_entry(prompt_id)
    if not entry:
        raise ComfyError(f"没有找到 prompt_id={prompt_id} 的执行记录")
    items = _iter_outputs(entry.get("outputs") or {})
    if not items:
        return [{"prompt_id": prompt_id, "message": "该任务没有输出文件", "outputs": []}]

    saved_to = Path(out_dir).expanduser() if out_dir else None
    if saved_to is None and inline:
        saved_to = TEMP_DIR / prompt_id
    saved_to = saved_to.resolve() if saved_to else None

    results: List[Dict[str, Any]] = []
    images: List[Any] = []
    for item in items:
        record = {
            "node_id": item.get("node_id"),
            "kind": item.get("kind"),
            "filename": item.get("filename"),
            "type": item.get("type") or "output",
        }
        if item.get("kind") == "text":
            record["text"] = item.get("text")
        if saved_to and item.get("filename"):
            try:
                path = _download_item(item, saved_to)
                if path:
                    record["local_path"] = str(path)
                    if (
                        inline
                        and len(images) < max_inline
                        and path.suffix.lower() in IMAGE_EXTS
                        and path.stat().st_size <= INLINE_LIMIT
                    ):
                        images.append(Image(path=str(path), format=_norm_ext_format(path)))
            except ComfyError as exc:
                record["error"] = str(exc)
        results.append(record)

    summary = {"prompt_id": prompt_id, "file_count": len([r for r in results if r.get("filename")]), "outputs": results}
    return [summary] + images


# ============================================================================
# MCP 工具
# ============================================================================

@mcp.tool()
def set_comfyui_server(url: str):
    """切换目标 ComfyUI 服务器地址（默认 http://192.168.31.34:8188）。

    Args:
        url: ComfyUI 地址，如 http://192.168.31.34:8188
    """
    STATE["base_url"] = url.rstrip("/")
    _object_info_cache["data"] = None
    logger.info("切换 ComfyUI 服务器 -> %s", STATE["base_url"])
    return {"base_url": STATE["base_url"]}


@mcp.tool()
def get_comfyui_server():
    """获取当前配置的 ComfyUI 服务器地址。"""
    return {"base_url": STATE["base_url"]}


@mcp.tool()
def get_server_status():
    """查询 ComfyUI 服务器状态：版本、系统内存、显卡与显存、队列长度。

    调用任何耗时工作流前建议先调用本工具确认服务在线并查看显存余量。
    """
    stats = _get_json("/system_stats")
    queue = _get_json("/queue")
    system = stats.get("system") or {}
    devices = []
    for dev in stats.get("devices") or []:
        total = dev.get("vram_total") or 0
        free = dev.get("vram_free") or 0
        devices.append(
            {
                "name": dev.get("name"),
                "type": dev.get("type"),
                "vram_total_gb": round(total / 1024**3, 2) if total else None,
                "vram_free_gb": round(free / 1024**3, 2) if free else None,
            }
        )
    return {
        "base_url": STATE["base_url"],
        "comfyui_version": system.get("comfyui_version"),
        "os": system.get("os"),
        "python_version": system.get("python_version"),
        "pytorch_version": system.get("pytorch_version"),
        "ram_total_gb": round((system.get("ram_total") or 0) / 1024**3, 2),
        "ram_free_gb": round((system.get("ram_free") or 0) / 1024**3, 2),
        "devices": devices,
        "queue_running": len(queue.get("queue_running") or []),
        "queue_pending": len(queue.get("queue_pending") or []),
    }


@mcp.tool()
def list_models(folder: str = "checkpoints", keyword: str = ""):
    """列出远程 ComfyUI 磁盘上的模型文件。

    Args:
        folder: 模型类别（目录名），如 checkpoints / loras / vae / text_encoders /
            diffusion_models / controlnet / clip_vision / upscale_models /
            style_models / embeddings。传空字符串列出所有可用类别。
        keyword: 可选，按文件名过滤（不区分大小写）。
    """
    if not folder:
        folders = _get_json("/models")
        return {"folder": "(all)", "available_folders": folders}

    try:
        names = _get_json(f"/models/{folder}")
    except ComfyError as exc:
        available = _get_json("/models")
        return {"error": str(exc), "available_folders": available}
    if keyword:
        lower = keyword.lower()
        names = [n for n in names if lower in n.lower()]
    return {"folder": folder, "count": len(names), "models": names}


@mcp.tool()
def search_nodes(keyword: str = "", category: str = "", limit: int = 40):
    """搜索远程 ComfyUI 可用节点（含已安装的自定义节点）。

    Args:
        keyword: 关键词，匹配节点类名、显示名或描述，不区分大小写。
        category: 可选，按分类过滤，如 "image", "conditioning", "model"。
        limit: 最多返回数量，默认 40。
    """
    info = _object_info(refresh=True)
    keyword = (keyword or "").lower()
    category = (category or "").lower()
    found = []
    for class_type, spec in info.items():
        display = spec.get("display_name") or class_type
        cat = spec.get("category") or ""
        desc = spec.get("description") or ""
        if keyword and keyword not in class_type.lower() and keyword not in display.lower() and keyword not in desc.lower():
            continue
        if category and category not in cat.lower():
            continue
        outputs = [o if isinstance(o, str) else o[0] for o in (spec.get("output") or [])]
        found.append(
            {
                "class_type": class_type,
                "display_name": display,
                "category": cat,
                "outputs": outputs,
                "is_output_node": bool(spec.get("output_node")),
            }
        )
        if len(found) >= limit:
            break
    return {"keyword": keyword, "count": len(found), "nodes": found, "total_nodes": len(info)}


@mcp.tool()
def get_node_info(class_name: str):
    """获取指定节点的完整输入/输出定义，用于构造工作流。

    Args:
        class_name: 节点类名，如 "KSampler"、"CheckpointLoaderSimple"。
    """
    info = _object_info()
    spec = info.get(class_name)
    if spec is None:
        return {"error": f"节点 {class_name} 不存在", "hint": "用 search_nodes 搜索可用节点"}
    return {"class_type": class_name, **spec}


@mcp.tool()
def validate_workflow(workflow: Optional[dict] = None, workflow_file: Optional[str] = None):
    """在提交前校验工作流：节点类型是否存在、输入名是否合法、连线是否指向有效节点。

    Args:
        workflow: API 格式工作流 dict（也接受 UI 导出格式，会自动转换）。
        workflow_file: 工作流 JSON 文件路径，二选一。
    """
    api, warnings = _load_workflow(workflow, workflow_file)
    result = validate_api_workflow(api)
    result["warnings"] = warnings
    result["nodes"] = sorted(api.keys(), key=lambda x: (len(x), x))
    return result


@mcp.tool()
def upload_image(file_path: str, subfolder: str = "", overwrite: bool = True):
    """上传本地图片到远程 ComfyUI 的 input 目录，供 LoadImage 等节点使用。

    Args:
        file_path: 本地图片路径。
        subfolder: 上传到 input 目录下的子目录，可选。
        overwrite: 同名文件是否覆盖，默认 True。

    Returns:
        含 name（LoadImage 的 image 值）、subfolder、type。
    """
    path = Path(file_path).expanduser()
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        files = {"image": (path.name, fh, mime)}
        data = {"overwrite": "true" if overwrite else "false"}
        if subfolder:
            data["subfolder"] = subfolder
        resp = _request("POST", "/upload/image", files=files, data=data, timeout=300)
    result = resp.json()
    result["local_path"] = str(path)
    return result


@mcp.tool()
def run_workflow(
    workflow: Optional[dict] = None,
    workflow_file: Optional[str] = None,
    wait: bool = True,
    timeout: float = 600.0,
    download_dir: str = "",
    include_images: bool = True,
    max_inline_images: int = 6,
):
    """提交工作流到远程 ComfyUI 执行，可选等待完成并取回输出。

    Args:
        workflow: API 格式工作流 dict；也支持 ComfyUI 前端导出的 UI 格式（自动转换）。
        workflow_file: 工作流 JSON 文件路径，二选一。
        wait: True 则阻塞等待执行结束，False 立即返回 prompt_id。
        timeout: 等待超时秒数，默认 600。
        download_dir: 输出文件下载到本地的目录；留空则不下载（等待时仅返回输出清单）。
        include_images: 是否把输出图片内联返回给模型查看（便于确认生成效果），默认 True。
        max_inline_images: 最多内联多少张图片，默认 6。
    """
    api, warnings = _load_workflow(workflow, workflow_file)
    check = validate_api_workflow(api)
    if not check["valid"]:
        return [{"error": "工作流校验失败，未提交", "validation": check, "warnings": warnings}]

    resp = _request(
        "POST",
        "/prompt",
        json={"prompt": api, "client_id": STATE["client_id"]},
        timeout=300,
    ).json()
    if resp.get("node_errors"):
        return [{"error": "ComfyUI 拒绝该工作流", "node_errors": resp["node_errors"], "warnings": warnings}]
    prompt_id = resp.get("prompt_id")
    if not wait:
        return [{"prompt_id": prompt_id, "queued": True, "number": resp.get("number"), "warnings": warnings}]

    result = _wait_for(prompt_id, timeout)
    if result.get("status") != "success":
        return [{"prompt_id": prompt_id, "warnings": warnings, **result}]

    content = _collect(prompt_id, download_dir or None, include_images, max_inline_images)
    if warnings:
        content[0]["warnings"] = warnings
    content[0]["status"] = "success"
    return content


def _wait_for(prompt_id: str, timeout: float, interval: float = 1.5):
    deadline = time.time() + max(timeout, 0)
    while time.time() < deadline:
        entry = _history_entry(prompt_id)
        if entry and (entry.get("status") or {}).get("completed"):
            status = _job_status(prompt_id)
            return {"status": status["status"], "errors": status["errors"]}
        time.sleep(interval)
    state = _queue_state(prompt_id)
    return {"status": "timeout", "queue_state": state, "hint": f"仍在队列中({state})，可稍后用 get_job_status 查询"}


@mcp.tool()
def get_job_status(prompt_id: str):
    """查询某个 prompt_id 的执行状态与错误信息。

    Args:
        prompt_id: 提交工作流时返回的任务 ID。
    """
    status = _job_status(prompt_id)
    if not status.get("completed") and status.get("status") == "unknown":
        status["status"] = "not_found"
    status.pop("outputs", None)
    return status


@mcp.tool()
def wait_for_job(prompt_id: str, timeout: float = 600.0):
    """阻塞等待某个 prompt_id 执行结束。

    Args:
        prompt_id: 任务 ID。
        timeout: 超时秒数，默认 600。
    """
    return {"prompt_id": prompt_id, **_wait_for(prompt_id, timeout)}


@mcp.tool()
def get_outputs(
    prompt_id: str,
    download_dir: str = "",
    include_images: bool = True,
    max_inline_images: int = 6,
):
    """获取已完成任务的输出清单，可选下载到本地并内联图片。

    Args:
        prompt_id: 任务 ID。
        download_dir: 下载目录；留空时若 include_images 为 True 会下载到系统临时目录以便内联展示。
        include_images: 是否内联返回图片内容，默认 True。
        max_inline_images: 最多内联多少张图片，默认 6。
    """
    return _collect(prompt_id, download_dir or None, include_images, max_inline_images)


@mcp.tool()
def get_queue_status():
    """查看远程 ComfyUI 当前正在执行和排队中的任务。"""
    queue = _get_json("/queue")
    def summarize(items):
        out = []
        for item in items or []:
            if len(item) > 2:
                out.append({"number": item[0], "prompt_id": item[1], "node_count": len(item[2] or {})})
        return out
    return {
        "running": summarize(queue.get("queue_running")),
        "pending": summarize(queue.get("queue_pending")),
    }


@mcp.tool()
def interrupt():
    """中断远程 ComfyUI 当前正在执行的任务（不会清空后续排队任务）。"""
    _request("POST", "/interrupt")
    return {"interrupted": True}


@mcp.tool()
def clear_queue():
    """清空远程 ComfyUI 的等待队列。"""
    _request("POST", "/queue", json={"clear": True})
    return {"cleared": True}


@mcp.tool()
def free_memory(unload_models: bool = False, free_cached_memory: bool = True):
    """让远程 ComfyUI 释放显存。

    Args:
        unload_models: 是否卸载已加载的模型，默认 False。
        free_cached_memory: 是否释放缓存的显存，默认 True。
    """
    _request(
        "POST",
        "/free",
        json={"unload_models": unload_models, "free_memory": free_cached_memory},
    )
    return {"unload_models": unload_models, "free_memory": free_cached_memory}


@mcp.tool()
def generate_image(
    prompt: str,
    negative_prompt: str = "",
    checkpoint: str = "",
    width: int = 1024,
    height: int = 1024,
    steps: int = 20,
    cfg: float = 7.0,
    seed: int = 0,
    sampler: str = "euler",
    scheduler: str = "normal",
    batch_size: int = 1,
    filename_prefix: str = "mcp",
    timeout: float = 600.0,
    download_dir: str = "",
):
    """用内置的 SD 文生图工作流（CheckpointLoaderSimple + KSampler + SaveImage）快速出图。

    注意：需要目标 ComfyUI 上有可用的 SD/SDXL 类 checkpoint（用 list_models 查看）。
    复杂需求请改用 run_workflow 提交完整工作流。

    Args:
        prompt: 正向提示词。
        negative_prompt: 负向提示词。
        checkpoint: checkpoint 文件名；留空则用远程第一个可用 checkpoint。
        width/height: 图片尺寸，默认 1024x1024。
        steps/cfg/seed/sampler/scheduler: 采样参数；seed=0 表示随机。
        batch_size: 一次生成几张。
        filename_prefix: 输出文件前缀。
        timeout: 等待超时秒数。
        download_dir: 输出下载目录。
    """
    if not checkpoint:
        candidates = _get_json("/models/checkpoints")
        if not candidates:
            return [{"error": "远程没有可用的 checkpoint，请先用 list_models 确认或用 run_workflow 提交流程"}]
        checkpoint = candidates[0]
    if seed == 0:
        seed = int.from_bytes(os.urandom(6), "big")

    workflow = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": batch_size}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": filename_prefix}},
    }
    return run_workflow(
        workflow=workflow,
        wait=True,
        timeout=timeout,
        download_dir=download_dir,
        include_images=True,
        max_inline_images=6,
    )


if __name__ == "__main__":
    logger.info("ComfyUI Remote MCP Server -> %s", STATE["base_url"])
    mcp.run()
