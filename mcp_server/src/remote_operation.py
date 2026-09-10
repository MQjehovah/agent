"""
Device Remote Operations MCP Server
设备远程运维 MCP 服务器

对接 rosiwit-cloud 设备远程控制接口集（/remote/*）。

要点（2026-09 接口改版后）:
- 基址: https://bms-cn.rosiwit.com/rosiwit-cloud
- 鉴权: 用户名/密码登录换取 JWT，之后所有请求带 ``Authorization: Bearer <token>``
- 入参: 统一信封 ``RemoteForm`` = ``{deviceId, productId, param, messageId?}``
  - ``deviceId`` 为设备序列号(如 1400486A)，``productId`` 为产品型号(如 XZ-SC50)
  - ``param`` 为设备端业务参数(键值对)，随设备功能指令下发；各接口可用键见各工具 docstring
"""
import os
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass
import requests
from mcp.server.fastmcp import FastMCP
from rich.logging import RichHandler
from rich.console import Console

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("device-ops-mcp")

mcp = FastMCP("Device Operations MCP Server")


@dataclass
class APIConfig:
    base_url: str = os.getenv("DEVICE_API_BASE_URL", "https://bms-cn.rosiwit.com/rosiwit-cloud")
    username: Optional[str] = os.getenv("DEVICE_API_USERNAME", "")
    password: Optional[str] = os.getenv("DEVICE_API_PASSWORD", "")
    token: Optional[str] = None
    timeout: int = int(os.getenv("DEVICE_API_TIMEOUT", "60"))


api_config = APIConfig()


def _login() -> None:
    """用户名/密码登录，换取并缓存 JWT。"""
    if not api_config.username or not api_config.password:
        logger.warning("DEVICE_API_USERNAME 或 DEVICE_API_PASSWORD 未配置，登录跳过")
        return
    url = f"{api_config.base_url}/auth/login"
    try:
        resp = requests.post(
            url,
            params={"userName": api_config.username,
                    "password": api_config.password,
                    "clientType": "WEB"},
            headers={"Content-Type": "application/json"},
            timeout=api_config.timeout,
        )
        resp.raise_for_status()
        result = resp.json()
        data = result.get("data") or {}
        token = None
        if isinstance(data, dict):
            token = (data.get("tokenInfo") or {}).get("token") or data.get("token")
        if token:
            api_config.token = token
            logger.info(f"登录成功 (user={api_config.username})")
        else:
            logger.warning(f"登录失败: {result.get('returnMsg', '未知错误')}")
    except Exception as e:
        logger.warning(f"登录异常: {e}")


def _headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept-Language": "zh_CN", "Accept-Timezone": "Asia/Shanghai"}
    if api_config.token:
        headers["Authorization"] = f"Bearer {api_config.token}"
    return headers


def _handle_response(resp: requests.Response) -> Dict[str, Any]:
    try:
        return resp.json() if resp.text else {"success": True}
    except Exception:
        return {"success": resp.ok, "status": resp.status_code, "text": resp.text[:500]}


def _request(method: str, path: str, form: Optional[Dict[str, Any]] = None,
             query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """统一请求：自动登录/401 重登一次。"""
    if not api_config.token:
        _login()
    url = f"{api_config.base_url}{path}"
    for attempt in range(2):
        try:
            if method == "GET":
                # 少数 GET 接口(如 /remote/device/setting/get)需要 body；统一带 json 兼容
                resp = requests.get(url, params=query, json=form,
                                    headers=_headers(), timeout=api_config.timeout)
            else:
                resp = requests.post(url, params=query, json=form or {},
                                     headers=_headers(), timeout=api_config.timeout)
            if resp.status_code in (401, 403) and attempt == 0 and api_config.username:
                logger.warning(f"鉴权失效，重新登录后重试: {path}")
                api_config.token = None
                _login()
                continue
            return _handle_response(resp)
        except requests.exceptions.RequestException as e:
            logger.error(f"API请求失败: {method} {path}, 错误: {e}")
            return {"success": False, "error": str(e)}
    return {"success": False, "error": "鉴权失败"}


def _form(device_id: str, product_id: str, param: Optional[Dict[str, Any]] = None,
          message_id: str = "") -> Dict[str, Any]:
    form: Dict[str, Any] = {"deviceId": device_id, "productId": product_id}
    if param:
        form["param"] = param
    if message_id:
        form["messageId"] = message_id
    return form


def _call(path: str, device_id: str, product_id: str,
          param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """POST /remote/* 统一信封调用。"""
    return _request("POST", path, form=_form(device_id, product_id, param))


# ==================== 设备查询（非 /remote，供状态诊断） ====================

@mcp.tool()
def device_shadow(device_id: str, product_id: str) -> Dict[str, Any]:
    """查询设备实时状态（推荐的状态诊断入口）。

    返回 cleanRobot: battery/charge/locate/dock/manual/state/currentFaults/x/y/theta 等，
    以及 isOnline / lastMessageTime。用于替代旧的 get_device_detail / get_real_time_state。
    """
    logger.info(f"查询设备实时状态: {device_id}")
    return _request("GET", "/device/shadow", query={"deviceId": device_id, "productId": product_id})


@mcp.tool()
def device_page(page_no: int = 1, page_size: int = 10, device_id: str = "",
                name: str = "", need_detail: bool = False) -> Dict[str, Any]:
    """分页查询设备。可按 deviceId/name 过滤；need_detail=True 返回更完整字段(含故障等)。"""
    logger.info(f"分页查询设备: page={page_no}, device_id={device_id}")
    body: Dict[str, Any] = {}
    if device_id:
        body["deviceIds"] = [device_id]
    if name:
        body["name"] = name
    if need_detail:
        body["needDetail"] = True
    return _request("POST", "/device/page", form=body,
                    query={"current": page_no, "size": page_size})


@mcp.tool()
def device_list() -> Dict[str, Any]:
    """查询当前用户授权范围内的全部设备列表（含 deviceId/productId/在线状态等）。"""
    logger.info("查询设备列表")
    return _request("GET", "/device/list")


# ==================== 设备管理 ====================

@mcp.tool()
def device_initiate(device_id: str, product_id: str) -> Dict[str, Any]:
    """设备初始化。deviceId=设备序列号, productId=产品型号。param: 无"""
    logger.info(f"设备初始化: {device_id}")
    return _call("/remote/device/initiate", device_id, product_id)


@mcp.tool()
def device_reset(device_id: str, product_id: str) -> Dict[str, Any]:
    """设备恢复出厂设置。param: 无"""
    logger.info(f"设备恢复出厂设置: {device_id}")
    return _call("/remote/device/reset", device_id, product_id)


@mcp.tool()
def device_setting_get(device_id: str, product_id: str) -> Dict[str, Any]:
    """获取系统参数。param: 无"""
    logger.info(f"获取系统参数: {device_id}")
    return _request("GET", "/remote/device/setting/get", form=_form(device_id, product_id))


@mcp.tool()
def device_setting_set(device_id: str, product_id: str,
                       param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """设置系统参数。param: 参数键值对，如 {"paramName": "值"}"""
    logger.info(f"设置系统参数: {device_id}, param={param}")
    return _call("/remote/device/setting/set", device_id, product_id, param)


@mcp.tool()
def device_factory_switch(device_id: str, product_id: str,
                          param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """工程模式切换。param: 参见设备端(如 {"enable": true})"""
    logger.info(f"工程模式切换: {device_id}, param={param}")
    return _call("/remote/device/factory/switch", device_id, product_id, param)


@mcp.tool()
def factory_setting_set(device_id: str, product_id: str,
                        param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """工程模式参数设置。param: 工程参数键值对"""
    logger.info(f"工程模式参数设置: {device_id}, param={param}")
    return _call("/remote/device/factory/setting/set", device_id, product_id, param)


@mcp.tool()
def factory_setting_get(device_id: str, product_id: str) -> Dict[str, Any]:
    """获取工程模式参数。param: 无"""
    logger.info(f"获取工程模式参数: {device_id}")
    return _call("/remote/device/factory/setting/get", device_id, product_id)


@mcp.tool()
def factory_setting_reset(device_id: str, product_id: str,
                          param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """工程模式参数重置。param: 参见设备端"""
    logger.info(f"工程模式参数重置: {device_id}")
    return _call("/remote/device/factory/setting/reset", device_id, product_id, param)


@mcp.tool()
def factory_control(device_id: str, product_id: str,
                    param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """工程模式控制 (sw50 gt)。param: 参见设备端"""
    logger.info(f"工程模式控制: {device_id}, param={param}")
    return _call("/remote/device/factory/control", device_id, product_id, param)


@mcp.tool()
def factory_calib_camera(device_id: str, product_id: str,
                         param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """相机标定。param: 参见设备端"""
    logger.info(f"相机标定: {device_id}")
    return _call("/remote/device/factory/calib/camera", device_id, product_id, param)


@mcp.tool()
def factory_test(device_id: str, product_id: str,
                 param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """调试测试 (SW50 GT)。param: 参见设备端"""
    logger.info(f"调试测试: {device_id}")
    return _call("/remote/device/factory/test", device_id, product_id, param)


@mcp.tool()
def device_restart(device_id: str, product_id: str, type: int = 0) -> Dict[str, Any]:
    """机器重启。param: type(重启类型，默认0)"""
    logger.info(f"机器重启: {device_id}, type={type}")
    return _call("/remote/device/restart", device_id, product_id, {"type": type})


@mcp.tool()
def robot_backward(device_id: str, product_id: str) -> Dict[str, Any]:
    """机器倒退。param: 无"""
    logger.info(f"机器倒退: {device_id}")
    return _call("/remote/device/backward", device_id, product_id)


@mcp.tool()
def robot_manual(device_id: str, product_id: str,
                 param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """切换手动/自动模式。param: 参见设备端(如 {"manual": true})"""
    logger.info(f"切换手动/自动模式: {device_id}, param={param}")
    return _call("/remote/device/manual", device_id, product_id, param)


@mcp.tool()
def device_clean(device_id: str, product_id: str,
                 param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """设备手动清洗控制。param: 参见设备端(如 {"mode": ...})"""
    logger.info(f"设备手动清洗控制: {device_id}, param={param}")
    return _call("/remote/device/clean", device_id, product_id, param)


@mcp.tool()
def garbage_switch(device_id: str, product_id: str,
                   param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """控制倒垃圾。param: 参见设备端"""
    logger.info(f"控制倒垃圾: {device_id}, param={param}")
    return _call("/remote/device/garbage/switch", device_id, product_id, param)


@mcp.tool()
def clean_info(device_id: str, product_id: str) -> Dict[str, Any]:
    """获取机器清洁组件信息。param: 无"""
    logger.info(f"获取清洁组件信息: {device_id}")
    return _request("GET", "/remote/device/cleanInfo", query={"deviceId": device_id, "productId": product_id})


@mcp.tool()
def device_terminal_execute(device_id: str, product_id: str, command: str) -> Dict[str, Any]:
    """向机器执行命令。param: command(多个指令用 && 连接)，如 "date && ls" """
    logger.info(f"向机器执行命令: {device_id}, command={command}")
    return _call("/remote/device/terminal/execute", device_id, product_id, {"command": command})


# ==================== 摄像头 / 点云 ====================

@mcp.tool()
def camera_image(device_id: str, product_id: str,
                 param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """机器摄像头图片获取。param: 参见设备端(如 {"cameraId": 0})"""
    logger.info(f"获取摄像头图片: {device_id}")
    return _call("/remote/camera/image", device_id, product_id, param)


@mcp.tool()
def device_camera_image(device_id: str, product_id: str,
                        param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """机器摄像头图片获取（新版本）。param: 参见设备端"""
    logger.info(f"获取摄像头图片(新): {device_id}")
    return _call("/remote/device/camera/image", device_id, product_id, param)


@mcp.tool()
def point_cloud(device_id: str, product_id: str) -> Dict[str, Any]:
    """激光雷达点云数据。param: 无"""
    logger.info(f"获取点云数据: {device_id}")
    return _request("GET", "/remote/point_cloud", query={"deviceId": device_id, "productId": product_id})


# ==================== 工作站 ====================

@mcp.tool()
def station_back(device_id: str, product_id: str) -> Dict[str, Any]:
    """回桩（返回充电点）。param: 无"""
    logger.info(f"回桩: {device_id}")
    return _call("/remote/station/back", device_id, product_id)


@mcp.tool()
def station_relocation(device_id: str, product_id: str,
                       param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """工作站重定位。param: 参见设备端"""
    logger.info(f"工作站重定位: {device_id}")
    return _call("/remote/station/relocation", device_id, product_id, param)


@mcp.tool()
def station_dock(device_id: str, product_id: str) -> Dict[str, Any]:
    """手动补给。param: 无"""
    logger.info(f"手动补给: {device_id}")
    return _call("/remote/station/dock", device_id, product_id)


# ==================== 地图 ====================

@mcp.tool()
def map_list(device_id: str, product_id: str,
             param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取设备地图列表。param: 分页 {pageNo, pageSize}"""
    logger.info(f"获取地图列表: {device_id}")
    return _call("/remote/map/list", device_id, product_id, param)


@mcp.tool()
def get_map_by_id(device_id: str, product_id: str, map_id: int) -> Dict[str, Any]:
    """根据云端ID获取地图详情。map_id=地图云端数据库ID"""
    logger.info(f"获取地图详情: {device_id}, id={map_id}")
    return _request("GET", f"/remote/map/{map_id}", query={"deviceId": device_id, "productId": product_id})


@mcp.tool()
def map_save(device_id: str, product_id: str,
             param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """地图保存。param: 参见设备端"""
    logger.info(f"地图保存: {device_id}")
    return _call("/remote/map/save", device_id, product_id, param)


@mcp.tool()
def map_update(device_id: str, product_id: str,
               param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """地图更新。param: id/aliasId/map_name/floor/building/origin/resolution/update_time"""
    logger.info(f"地图更新: {device_id}, param={param}")
    return _call("/remote/map/update", device_id, product_id, param)


@mcp.tool()
def map_image_update(device_id: str, product_id: str,
                     param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """地图图片更新（橡皮擦）。param: id/aliasId/map_data/type"""
    logger.info(f"地图图片更新: {device_id}")
    return _call("/remote/map/image/update", device_id, product_id, param)


@mcp.tool()
def map_image_rotate(device_id: str, product_id: str,
                     param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """地图图片旋转。param: 参见设备端"""
    logger.info(f"地图图片旋转: {device_id}")
    return _call("/remote/map/image/rotate", device_id, product_id, param)


@mcp.tool()
def map_delete(device_id: str, product_id: str,
               param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """地图删除。param: id(地图云端ID)"""
    logger.info(f"地图删除: {device_id}, param={param}")
    return _call("/remote/map/delete", device_id, product_id, param)


@mcp.tool()
def map_copy(device_id: str, product_id: str,
             param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """复制地图。param: 参见设备端"""
    logger.info(f"复制地图: {device_id}")
    return _call("/remote/map/copy", device_id, product_id, param)


@mcp.tool()
def map_relocation(device_id: str, product_id: str,
                   param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """地图重定位。param: 参见设备端(如 {"map_id": ..., "position": [x,y,theta]})"""
    logger.info(f"地图重定位: {device_id}")
    return _call("/remote/map/relocation", device_id, product_id, param)


@mcp.tool()
def map_switch(device_id: str, product_id: str,
               param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """默认地图切换。param: 参见设备端"""
    logger.info(f"默认地图切换: {device_id}")
    return _call("/remote/map/switch", device_id, product_id, param)


# ==================== 地图覆盖物 ====================

@mcp.tool()
def map_cover(device_id: str, product_id: str,
              param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取地图覆盖物信息。param: 参见设备端"""
    logger.info(f"获取地图覆盖物: {device_id}")
    return _call("/remote/map/cover", device_id, product_id, param)


@mcp.tool()
def map_cover_create(device_id: str, product_id: str,
                     param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """创建地图覆盖物。param: 参见设备端"""
    logger.info(f"创建地图覆盖物: {device_id}")
    return _call("/remote/map/cover/create", device_id, product_id, param)


@mcp.tool()
def map_cover_update(device_id: str, product_id: str,
                     param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """更新地图覆盖物。param: 参见设备端"""
    logger.info(f"更新地图覆盖物: {device_id}")
    return _call("/remote/map/cover/update", device_id, product_id, param)


@mcp.tool()
def map_cover_update_all(device_id: str, product_id: str,
                         param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """全量更新地图覆盖物。param: {map_id, data}"""
    logger.info(f"全量更新地图覆盖物: {device_id}, param={param}")
    return _call("/remote/map/cover/update/all", device_id, product_id, param)


@mcp.tool()
def map_cover_delete(device_id: str, product_id: str,
                     param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """删除地图覆盖物。param: 参见设备端"""
    logger.info(f"删除地图覆盖物: {device_id}")
    return _call("/remote/map/cover/delete", device_id, product_id, param)


# ==================== 路径 ====================

@mcp.tool()
def path_list(device_id: str, product_id: str,
              param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取路径列表。param: 分页 {pageNo, pageSize}"""
    logger.info(f"获取路径列表: {device_id}")
    return _call("/remote/path/list", device_id, product_id, param)


@mcp.tool()
def path_detail(device_id: str, product_id: str, path_id: int) -> Dict[str, Any]:
    """获取路径详情（查云端）。path_id=路径云端数据库ID"""
    logger.info(f"获取路径详情: {device_id}, id={path_id}")
    return _request("GET", f"/remote/path/detail/{path_id}", query={"deviceId": device_id, "productId": product_id})


@mcp.tool()
def path_update(device_id: str, product_id: str,
                param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """更新路径。param: 参见设备端"""
    logger.info(f"更新路径: {device_id}")
    return _call("/remote/path/update", device_id, product_id, param)


@mcp.tool()
def path_delete(device_id: str, product_id: str, path_id: int) -> Dict[str, Any]:
    """删除路径。param: id(路径云端ID)"""
    logger.info(f"删除路径: {device_id}, id={path_id}")
    return _call("/remote/path/delete", device_id, product_id, {"id": path_id})


@mcp.tool()
def path_plan(device_id: str, product_id: str, map_id: Optional[int] = None) -> Dict[str, Any]:
    """路径规划。param: map_id(可选)"""
    logger.info(f"路径规划: {device_id}, map_id={map_id}")
    param = {"map_id": map_id} if map_id is not None else None
    return _call("/remote/path/plan", device_id, product_id, param)


@mcp.tool()
def path_record(device_id: str, product_id: str,
                param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """路径记录。param: name/map_id/type(path_type)/area/aliasId/id"""
    logger.info(f"路径记录: {device_id}, param={param}")
    return _call("/remote/path/record", device_id, product_id, param)


# ==================== 任务 ====================

@mcp.tool()
def task_list(device_id: str, product_id: str,
              param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取任务列表。param: 分页 {pageNo, pageSize}"""
    logger.info(f"获取任务列表: {device_id}")
    return _call("/remote/task/list", device_id, product_id, param)


@mcp.tool()
def task_create(device_id: str, product_id: str,
                param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """创建任务。param: 参见设备端任务字段"""
    logger.info(f"创建任务: {device_id}, param={param}")
    return _call("/remote/task/create", device_id, product_id, param)


@mcp.tool()
def task_update(device_id: str, product_id: str,
                param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """更新任务。param: 参见设备端任务字段"""
    logger.info(f"更新任务: {device_id}, param={param}")
    return _call("/remote/task/update", device_id, product_id, param)


@mcp.tool()
def task_delete(device_id: str, product_id: str,
                param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """删除任务。param: id(任务云端ID)"""
    logger.info(f"删除任务: {device_id}, param={param}")
    return _call("/remote/task/delete", device_id, product_id, param)


@mcp.tool()
def task_control(device_id: str, product_id: str,
                 param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """任务控制（开始/暂停/停止）。param: 参见设备端(如 {"action": ...})"""
    logger.info(f"任务控制: {device_id}, param={param}")
    return _call("/remote/task/control", device_id, product_id, param)


@mcp.tool()
def start_general_task(device_id: str, product_id: str,
                       param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """开始普通任务。param: 参见设备端"""
    logger.info(f"开始普通任务: {device_id}, param={param}")
    return _call("/remote/task/start_general_task", device_id, product_id, param)


@mcp.tool()
def task_pending_list(device_id: str, product_id: str,
                      param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取断点续传任务列表。param: 参见设备端"""
    logger.info(f"获取断点续传任务: {device_id}")
    return _call("/remote/task/pending/list", device_id, product_id, param)


@mcp.tool()
def task_pending_resume(device_id: str, product_id: str,
                        param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """开始执行断点续扫任务。param: 参见设备端"""
    logger.info(f"开始断点续扫: {device_id}")
    return _call("/remote/task/pending/resume", device_id, product_id, param)


@mcp.tool()
def task_report_list(device_id: str, product_id: str,
                     param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取任务报告列表。param: 分页 {pageNo, pageSize}"""
    logger.info(f"获取任务报告列表: {device_id}")
    return _call("/remote/task/report/list", device_id, product_id, param)


# ==================== 定时任务 ====================

@mcp.tool()
def schedule_create(device_id: str, product_id: str,
                    param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """创建定时任务。param: task_id/start_time/stop_time/week_day/week_repeat/task_duration/enable"""
    logger.info(f"创建定时任务: {device_id}, param={param}")
    return _call("/remote/schedule/create", device_id, product_id, param)


@mcp.tool()
def schedule_update(device_id: str, product_id: str,
                    param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """更新定时任务。param: id/task_id/start_time/stop_time/week_day/week_repeat/task_duration/enable"""
    logger.info(f"更新定时任务: {device_id}, param={param}")
    return _call("/remote/schedule/update", device_id, product_id, param)


@mcp.tool()
def schedule_list(device_id: str, product_id: str,
                  param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取定时任务列表。param: 参见设备端"""
    logger.info(f"获取定时任务列表: {device_id}")
    return _call("/remote/schedule/list", device_id, product_id, param)


@mcp.tool()
def schedule_delete(device_id: str, product_id: str, schedule_id: int) -> Dict[str, Any]:
    """删除定时任务。param: id(定时任务云端ID)"""
    logger.info(f"删除定时任务: {device_id}, id={schedule_id}")
    return _call("/remote/schedule/delete", device_id, product_id, {"id": schedule_id})


# ==================== 消耗品 ====================

@mcp.tool()
def consumable_list(device_id: str, product_id: str,
                    param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取消耗品寿命列表。param: 参见设备端"""
    logger.info(f"获取消耗品列表: {device_id}")
    return _call("/remote/consumable/list", device_id, product_id, param)


@mcp.tool()
def consumable_reset(device_id: str, product_id: str, type: int) -> Dict[str, Any]:
    """重置消耗品寿命。param: type(消耗品类型)"""
    logger.info(f"重置消耗品寿命: {device_id}, type={type}")
    return _call("/remote/device/consumable/reset", device_id, product_id, {"type": type})


@mcp.tool()
def set_consumable(device_id: str, product_id: str,
                   param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """设置消耗品寿命。param: 参见设备端(消耗品类型/寿命等)"""
    logger.info(f"设置消耗品寿命: {device_id}, param={param}")
    return _call("/remote/device/consumable/set", device_id, product_id, param)


# ==================== 录包 ====================

@mcp.tool()
def bag_upload(device_id: str, product_id: str, file_path: str,
               timestamp: Optional[int] = None) -> Dict[str, Any]:
    """上传录包文件。param: filePath, timeStamp(可选)。注意: 上传消耗流量与网盘空间，勿随意批量上传"""
    logger.info(f"上传录包: {device_id}, file={file_path}")
    param: Dict[str, Any] = {"filePath": file_path}
    if timestamp is not None:
        param["timeStamp"] = timestamp
    return _call("/remote/bag/upload", device_id, product_id, param)


@mcp.tool()
def bag_list(device_id: str, product_id: str, page_no: int = 1, page_size: int = 10,
             param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取设备录包列表。param: pageNo, pageSize"""
    logger.info(f"获取录包列表: {device_id}, page={page_no}")
    p = {"pageNo": page_no, "pageSize": page_size}
    if param:
        p.update(param)
    return _call("/remote/bag/list", device_id, product_id, p)


@mcp.tool()
def bag_upload_list(device_id: str, product_id: str,
                    param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """批量上传故障录包。param: 参见设备端"""
    logger.info(f"批量上传故障录包: {device_id}")
    return _call("/remote/bag/uploadList", device_id, product_id, param)


# ==================== 视频 ====================

@mcp.tool()
def video_control(device_id: str, product_id: str, flag: bool) -> Dict[str, Any]:
    """设备视频控制。param: flag(开关)"""
    logger.info(f"设备视频控制: {device_id}, flag={flag}")
    return _call("/remote/video/control", device_id, product_id, {"flag": flag})


@mcp.tool()
def video_heartbeat(device_id: str, product_id: str,
                    param: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """设备视频心跳。param: 参见设备端"""
    logger.info(f"设备视频心跳: {device_id}")
    return _call("/remote/video/heartbeat", device_id, product_id, param)


if __name__ == "__main__":
    logger.info("启动 Device Operations MCP Server")
    mcp.run()
