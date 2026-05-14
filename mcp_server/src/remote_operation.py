"""
Device Remote Operations MCP Server
设备远程运维 MCP 服务器
"""
import os
import json
import logging
from typing import Optional, List, Dict, Any
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
    base_url: str = os.getenv("DEVICE_API_BASE_URL", "https://bms-cn.rosiwit.com")
    username: Optional[str] = os.getenv("DEVICE_API_USERNAME","")
    password: Optional[str] = os.getenv("DEVICE_API_PASSWORD","")
    token: Optional[str] = None
    timeout: int = 30


api_config = APIConfig()


def _auto_login():
    if not api_config.username or not api_config.password:
        logger.warning("DEVICE_API_USERNAME 或 DEVICE_API_PASSWORD 未配置，自动登录跳过")
        return
    if api_config.token:
        return
    url = f"{api_config.base_url}/xz_robot_common/user/login"
    try:
        resp = requests.post(
            url,
            json={"username": api_config.username, "password": api_config.password, "clientType": "WEB"},
            headers={"Content-Type": "application/json"},
            timeout=api_config.timeout,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("success") or result.get("returnCode") == 200:
            data = result.get("data", {})
            token = data.get("token") if isinstance(data, dict) else None
        else:
            token = None
        if token:
            api_config.token = token
            logger.info(f"自动登录成功 (user={api_config.username})")
        else:
            logger.warning(f"自动登录失败: {result.get('returnMsg', '未知错误')}")
    except Exception as e:
        logger.warning(f"自动登录异常: {e}")


_auto_login()

def _get_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_config.token:
        headers["token"] = api_config.token
    return headers


def _post(endpoint: str, data: Dict = None) -> Dict[str, Any]:
    url = f"{api_config.base_url}/xz_sc50/fae{endpoint}"
    try:
        response = requests.post(
            url,
            json=data or {},
            headers=_get_headers(),
            timeout=api_config.timeout
        )
        if response.status_code == 403 and api_config.username:
            api_config.token = None
            _auto_login()
            if api_config.token:
                response = requests.post(
                    url,
                    json=data or {},
                    headers=_get_headers(),
                    timeout=api_config.timeout
                )
        response.raise_for_status()
        return response.json() if response.text else {"success": True}
    except requests.exceptions.RequestException as e:
        logger.error(f"API请求失败: {endpoint}, 错误: {e}")
        return {"success": False, "error": str(e)}


# ==================== 设备信息查询 ====================

@mcp.tool()
def get_device_detail(sn: str):
    """获取设备详情
    
    参数:
    - sn: 设备编码
    
    返回字段包括: 连接状态(connect)、定位状态(locate)、故障信息(faultDTOList)、电量(battery)等
    """
    logger.info(f"获取设备详情: {sn}")
    result = _post("/detail", {"sn": sn})
    
    if result.get("sn"):
        detail = {
            "sn": result.get("sn"),
            "name": result.get("name"),
            "connect": result.get("connect"),
            "locate": result.get("locate"),
            "hasFault": result.get("hasFault"),
            "runState": result.get("runState"),
            "runStateName": result.get("runStateName"),
            "battery": result.get("battery"),
            "position": result.get("position"),
            "faultDTOList": result.get("faultDTOList", []),
            "dock": result.get("dock"),
            "isPause": result.get("isPause")
        }
        logger.info(f"设备状态: connect={detail['connect']}, locate={detail['locate']}, hasFault={detail['hasFault']}")
        return detail
    
    return result


@mcp.tool()
def get_real_time_state(sn: str):
    """获取设备实时数据
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"获取设备实时数据: {sn}")
    return _post("/device/real_time_state", {"sn": sn})


@mcp.tool()
def get_clean_info(sn: str):
    """获取设备清洁组件信息
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"获取清洁组件信息: {sn}")
    return _post("/clean_info", {"sn": sn})


@mcp.tool()
def get_camera_info(sn: str, camera_id: int = 0):
    """获取摄像头信息
    
    参数:
    - sn: 设备编码
    - camera_id: 摄像头ID
    """
    logger.info(f"获取摄像头信息: {sn}, camera_id={camera_id}")
    return _post("/camera_information", {"sn": sn, "cameraId": camera_id})


@mcp.tool()
def get_chassis_info(sn: str):
    """获取底盘底层数据
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"获取底盘底层数据: {sn}")
    return _post("/driver/chassis/info", {"sn": sn})


# ==================== 故障诊断与恢复 ====================

# @mcp.tool()
# def fault_diagnose(sn: str):
#     """故障诊断
    
#     参数:
#     - sn: 设备编码
#     """
#     logger.info(f"故障诊断: {sn}")
#     return _post("/fault_diagnose", {"sn": sn})


# @mcp.tool()
# def soft_restart(sn: str):
#     """软重启设备
    
#     参数:
#     - sn: 设备编码
#     """
#     logger.info(f"软重启设备: {sn}")
#     result = _post("/soft_restart", {"sn": sn})
#     logger.info(f"软重启指令已发送: {sn}")
#     return result


# @mcp.tool()
# def relocate(sn: str, position: List[float]):
#     """设备重定位
    
#     参数:
#     - sn: 设备编码
#     - position: 位姿信息 [x, y, theta]
#     """
#     logger.info(f"重定位设备: {sn}, position={position}")
#     result = _post("/relocate", {"sn": sn, "position": position})
#     logger.info(f"重定位指令已发送: {sn}")
#     return result


# @mcp.tool()
# def factory_reset(sn: str):
#     """重置设备高级工程模式参数
    
#     参数:
#     - sn: 设备编码
#     """
#     logger.info(f"重置设备参数: {sn}")
#     return _post("/factory_reset", {"sn": sn})


# ==================== 设备控制 ====================

@mcp.tool()
def move_robot(sn: str, mode: int):
    """控制设备移动
    
    参数:
    - sn: 设备编码
    - mode: 移动模式
        - 0: 停止
        - 1: 前进
        - 2: 右旋带前进
        - 3: 右旋转
        - 4: 右旋带后退
        - 5: 后退
        - 6: 左旋带后退
        - 7: 左旋转
        - 8: 左旋带前进
    """
    mode_names = {
        0: "停止", 1: "前进", 2: "右旋带前进", 3: "右旋转",
        4: "右旋带后退", 5: "后退", 6: "左旋带后退", 7: "左旋转", 8: "左旋带前进"
    }
    logger.info(f"控制设备移动: {sn}, mode={mode}({mode_names.get(mode, '未知')})")
    return _post("/move", {"sn": sn, "mode": mode})


@mcp.tool()
def stop_robot(sn: str):
    """停止设备移动
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"停止设备: {sn}")
    return move_robot(sn, 0)


@mcp.tool()
def backward(sn: str):
    """设备倒退
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"设备倒退: {sn}")
    return _post("/backward", {"sn": sn})


@mcp.tool()
def forward_charge(sn: str):
    """设备前往充电站
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"设备前往充电站: {sn}")
    return _post("/forward_charge", {"sn": sn})


@mcp.tool()
def set_control_mode(sn: str, mode: int):
    """切换设备手自动模式
    
    参数:
    - sn: 设备编码
    - mode: 0-切手动, 1-切自动
    """
    mode_name = "手动" if mode == 0 else "自动"
    logger.info(f"切换设备模式: {sn} -> {mode_name}")
    return _post("/control_mode", {"sn": sn, "mode": mode})


# ==================== 工程模式 ====================

@mcp.tool()
def start_factory_mode(sn: str):
    """开启高级工程模式
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"开启工程模式: {sn}")
    return _post("/factory_mode_start", {"sn": sn})


@mcp.tool()
def stop_factory_mode(sn: str):
    """退出高级工程模式
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"退出工程模式: {sn}")
    return _post("/factory_mode_stop", {"sn": sn})


@mcp.tool()
def get_factory_params(sn: str):
    """获取高级工程模式参数
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"获取工程模式参数: {sn}")
    return _post("/factory_get", {"sn": sn})


@mcp.tool()
def set_factory_params(sn: str, params: Dict[str, Any]):
    """设置高级工程模式参数
    
    参数:
    - sn: 设备编码
    - params: 参数字典
    """
    logger.info(f"设置工程模式参数: {sn}")
    return _post("/factory_set", {"sn": sn, **params})


# ==================== 地图与点云 ====================

@mcp.tool()
def get_cost_map(sn: str):
    """获取感知地图
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"获取感知地图: {sn}")
    return _post("/cost_map", {"sn": sn})


@mcp.tool()
def get_point_cloud(sn: str):
    """获取点云数据
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"获取点云数据: {sn}")
    return _post("/point_cloud", {"sn": sn})


# ==================== 任务管理 ====================

@mcp.tool()
def get_pending_task(sn: str):
    """获取断点续扫任务
    
    参数:
    - sn: 设备编码
    """
    logger.info(f"获取断点续扫任务: {sn}")
    return _post("/pending_task_get", {"sn": sn})


@mcp.tool()
def resume_pending_task():
    """开始断点续扫任务"""
    logger.info("开始断点续扫任务")
    return _post("/pending_task_resume")


@mcp.tool()
def send_task_info():
    """发送任务数据"""
    logger.info("发送任务数据")
    return _post("/send_task_info")


@mcp.tool()
def plan_path():
    """路径规划"""
    logger.info("执行路径规划")
    return _post("/path/plan")


# ==================== OTA升级 ====================

@mcp.tool()
def start_ota(
    sn_list: List[str],
    name: str,
    version: str,
    url: str,
    md5: str,
    task_id: str,
    ota_type: str = "",
    mode: str = "",
    description: str = "",
    timestamp: str = ""
):
    """开始OTA升级
    
    参数:
    - sn_list: 设备编码列表
    - name: 任务名称
    - version: OTA版本号
    - url: OTA版本包下载地址
    - md5: MD5加密字符串
    - task_id: OTA任务ID
    - ota_type: 升级类型（可选）
    - mode: 升级模式（可选）
    - description: 版本描述（可选）
    - timestamp: 时间戳（可选）
    """
    logger.info(f"开始OTA升级: {name} v{version}, 设备: {sn_list}")
    data = {
        "snList": sn_list,
        "name": name,
        "type": ota_type,
        "mode": mode,
        "description": description,
        "version": version,
        "md5": md5,
        "taskId": task_id,
        "url": url,
        "timestamp": timestamp
    }
    return _post("/ota/start", data)


# ==================== 录包文件 ====================

@mcp.tool()
def get_bag_files(sn: str, page_no: int = 1, page_size: int = 10, from_time: str = None, to_time: str = None):
    """获取录包文件分页数据
    
    参数:
    - sn: 设备编码
    - page_no: 页码
    - page_size: 每页数量
    - from_time: 开始时间（可选）
    - to_time: 结束时间（可选）
    """
    logger.info(f"获取录包文件: {sn}, page={page_no}")
    data = {"sn": sn, "pageNo": page_no, "pageSize": page_size}
    if from_time:
        data["fromTime"] = from_time
    if to_time:
        data["toTime"] = to_time
    return _post("/bag/page", data)


@mcp.tool()
def upload_bag_file(sn: str, file_path: str, timestamp: int = None):
    """上传录包文件
    
    参数:
    - sn: 设备编码
    - file_path: 文件路径
    - timestamp: 时间戳（可选）
    """
    logger.info(f"上传录包文件: {sn}, file={file_path}")
    data = {"sn": sn, "filePath": file_path}
    if timestamp:
        data["timeStamp"] = timestamp
    return _post("/upload_bag", data)


# ==================== 定时任务 ====================

@mcp.tool()
def update_timer_task():
    """更新定时任务"""
    logger.info("更新定时任务")
    return _post("/timer_task/update")


@mcp.tool()
def delete_timer_task():
    """删除定时任务"""
    logger.info("删除定时任务")
    return _post("/timer_task/delete")


if __name__ == "__main__":
    logger.info("启动 Device Operations MCP Server")
    mcp.run()