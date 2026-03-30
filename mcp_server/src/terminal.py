"""
Terminal MCP Server - WebSocket Terminal
设备终端交互 MCP 服务 (rtty协议)
"""
import os
import json
import logging
import asyncio
import nest_asyncio
from typing import Optional
from dataclasses import dataclass, field
import websockets
from mcp.server.fastmcp import FastMCP
from rich.logging import RichHandler
from rich.console import Console

nest_asyncio.apply()

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("terminal-mcp")

mcp = FastMCP("Terminal MCP Server")

WS_BASE_URL = os.getenv("WS_BASE_URL", "wss://dev.xzrobot.com:10000")
DEFAULT_USERNAME = os.getenv("TERM_USERNAME", "xzrobot")
DEFAULT_PASSWORD = os.getenv("TERM_PASSWORD", "xzyz2022!")

LoginErrorOffline = 0x01
LoginErrorBusy = 0x02


@dataclass
class TerminalSession:
    sn: str
    ws: Optional[websockets.WebSocketClientProtocol] = None
    sid: str = ""
    output_buffer: list = field(default_factory=list)
    is_connected: bool = False
    is_logged_in: bool = False
    cols: int = 80
    rows: int = 24
    unack: int = 0


sessions: dict[str, TerminalSession] = {}


async def _send_winsize(session: TerminalSession):
    msg = {"type": "winsize", "cols": session.cols, "rows": session.rows}
    await session.ws.send(json.dumps(msg))
    logger.info(f"发送窗口大小: {session.cols}x{session.rows}")


async def _wait_login(session: TerminalSession, timeout: float = 10.0):
    try:
        message = await asyncio.wait_for(session.ws.recv(), timeout=timeout)
        if isinstance(message, str):
            msg = json.loads(message)
            if msg.get("type") == "login":
                if msg.get("err") == LoginErrorOffline:
                    session.is_connected = False
                    raise Exception("设备离线")
                elif msg.get("err") == LoginErrorBusy:
                    session.is_connected = False
                    raise Exception("会话已满")
                
                session.sid = msg.get("sid", "")
                session.is_logged_in = True
                logger.info(f"终端登录成功: {session.sn}, sid={session.sid}")
                
                await _send_winsize(session)
            else:
                raise Exception(f"未收到login消息: {msg}")
        else:
            raise Exception("未收到login消息（收到二进制数据）")
    except asyncio.TimeoutError:
        raise Exception("等待login超时")


async def _auto_login(session: TerminalSession, username: str, password: str, timeout: float = 5.0):
    logger.info(f"自动登录: {username}")
    
    outputs = await _receive_output(session.sn, timeout=timeout)
    
    login_prompt_found = False
    for o in outputs:
        if o["type"] == "output":
            text = o["data"].lower()
            if "login" in text or "username" in text or "user" in text:
                login_prompt_found = True
                break
    
    await _send_term_data(session, username + "\n")
    await asyncio.sleep(0.5)
    
    outputs = await _receive_output(session.sn, timeout=timeout)
    
    password_prompt_found = False
    for o in outputs:
        if o["type"] == "output":
            text = o["data"].lower()
            if "password" in text or "passwd" in text:
                password_prompt_found = True
                break
    
    await _send_term_data(session, password + "\n")
    await asyncio.sleep(1.0)
    
    outputs = await _receive_output(session.sn, timeout=timeout)
    
    login_success = False
    for o in outputs:
        if o["type"] == "output":
            text = o["data"]
            if "$" in text or "#" in text or "~" in text or "welcome" in text.lower():
                login_success = True
                break
    
    logger.info(f"登录结果: success={login_success}")
    return {"success": login_success, "outputs": outputs}


async def _connect_ws(sn: str, cols: int = 80, rows: int = 24, username: str = None, password: str = None) -> TerminalSession:
    if sn in sessions and sessions[sn].is_logged_in:
        return sessions[sn]
    
    url = f"{WS_BASE_URL}/connect/{sn}"
    logger.info(f"连接终端: {url}")
    
    try:
        ws = await websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5
        )
        session = TerminalSession(sn=sn, ws=ws, is_connected=True, cols=cols, rows=rows)
        sessions[sn] = session
        
        await _wait_login(session)
        
        if not session.is_logged_in:
            raise Exception("登录失败")
        
        if username and password:
            await _auto_login(session, username, password)
        
        return session
    except Exception as e:
        logger.error(f"终端连接失败: {e}")
        if sn in sessions:
            sessions[sn].is_connected = False
        raise


async def _disconnect_ws(sn: str):
    if sn in sessions and sessions[sn].ws:
        try:
            await sessions[sn].ws.close()
        except:
            pass
        sessions[sn].is_connected = False
        sessions[sn].is_logged_in = False
        sessions[sn].ws = None
        logger.info(f"终端已断开: {sn}")


async def _send_term_data(session: TerminalSession, data: str):
    buf = bytearray([0]) + data.encode('utf-8')
    await session.ws.send(bytes(buf))


async def _receive_output(sn: str, timeout: float = 2.0) -> list:
    if sn not in sessions or not sessions[sn].is_connected:
        return []
    
    session = sessions[sn]
    outputs = []
    
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    session.ws.recv(),
                    timeout=timeout
                )
                
                if isinstance(message, str):
                    msg = json.loads(message)
                    outputs.append({"type": "control", "data": msg})
                    session.output_buffer.append(msg)
                    
                    if msg.get("type") == "sendfile":
                        outputs.append({"type": "file_download", "name": msg.get("name")})
                    elif msg.get("type") == "recvfile":
                        outputs.append({"type": "file_upload_request"})
                else:
                    data = message
                    session.unack += len(data)
                    
                    text = data.decode('utf-8', errors='replace')
                    outputs.append({"type": "output", "data": text})
                    session.output_buffer.append(text)
                    
                    if session.unack > 4 * 1024:
                        ack_msg = {"type": "ack", "ack": session.unack}
                        await session.ws.send(json.dumps(ack_msg))
                        session.unack = 0
            except asyncio.TimeoutError:
                break
    except websockets.exceptions.ConnectionClosed:
        session.is_connected = False
        session.is_logged_in = False
        outputs.append({"type": "error", "data": "连接已断开"})
    
    return outputs


@mcp.tool()
def connect_terminal(sn: str, cols: int = 80, rows: int = 24, username: str = DEFAULT_USERNAME, password: str = DEFAULT_PASSWORD, base_url: str = None):
    """连接设备终端并自动登录
    
    参数:
    - sn: 设备编码
    - cols: 终端列数（默认80）
    - rows: 终端行数（默认24）
    - username: 登录用户名（默认xzrobot）
    - password: 登录密码（默认xzyz2022!）
    - base_url: WebSocket基础URL（可选，默认为 wss://dev.xzrobot.com:10000）
    """
    global WS_BASE_URL
    if base_url:
        WS_BASE_URL = base_url.rstrip("/")
    
    async def _connect():
        try:
            session = await _connect_ws(sn, cols, rows, username, password)
            return {
                "success": True,
                "sn": sn,
                "sid": session.sid,
                "cols": cols,
                "rows": rows,
                "username": username,
                "message": "终端连接并登录成功"
            }
        except Exception as e:
            return {"success": False, "sn": sn, "error": str(e)}
    
    return asyncio.get_event_loop().run_until_complete(_connect())


@mcp.tool()
def disconnect_terminal(sn: str):
    """断开设备终端连接
    
    参数:
    - sn: 设备编码
    """
    async def _disconnect():
        await _disconnect_ws(sn)
        if sn in sessions:
            del sessions[sn]
        return {"success": True, "sn": sn, "message": "终端已断开"}
    
    if sn not in sessions:
        return {"success": True, "sn": sn, "message": "终端未连接"}
    
    return asyncio.get_event_loop().run_until_complete(_disconnect())


@mcp.tool()
def send_command(sn: str, command: str, wait_output: bool = True, timeout: float = 2.0):
    """发送命令到终端
    
    参数:
    - sn: 设备编码
    - command: 要发送的命令（会自动添加换行符）
    - wait_output: 是否等待输出（默认True）
    - timeout: 等待输出的超时时间（秒，默认2.0）
    """
    if sn not in sessions or not sessions[sn].is_logged_in:
        return {"success": False, "error": "终端未连接，请先调用 connect_terminal"}
    
    async def _send():
        try:
            session = sessions[sn]
            
            await _send_term_data(session, command + "\n")
            logger.info(f"发送命令: {command}")
            
            if wait_output:
                outputs = await _receive_output(sn, timeout)
                output_texts = []
                for o in outputs:
                    if o["type"] == "output":
                        output_texts.append(o["data"])
                    elif o["type"] == "control":
                        output_texts.append(f"[控制消息: {o['data']}]")
                
                return {
                    "success": True,
                    "sn": sn,
                    "command": command,
                    "outputs": output_texts,
                    "raw_outputs": outputs
                }
            
            return {"success": True, "sn": sn, "command": command}
        except websockets.exceptions.ConnectionClosed:
            sessions[sn].is_connected = False
            sessions[sn].is_logged_in = False
            return {"success": False, "error": "连接已断开"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    return asyncio.get_event_loop().run_until_complete(_send())


@mcp.tool()
def send_raw(sn: str, data: str):
    """发送原始数据到终端（不添加换行符）
    
    参数:
    - sn: 设备编码
    - data: 原始数据字符串
    """
    if sn not in sessions or not sessions[sn].is_logged_in:
        return {"success": False, "error": "终端未连接"}
    
    async def _send():
        try:
            session = sessions[sn]
            await _send_term_data(session, data)
            return {"success": True, "sn": sn, "data": data}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    return asyncio.get_event_loop().run_until_complete(_send())


@mcp.tool()
def receive_output(sn: str, timeout: float = 2.0):
    """接收终端输出
    
    参数:
    - sn: 设备编码
    - timeout: 等待输出的超时时间（秒，默认2.0）
    """
    if sn not in sessions or not sessions[sn].is_connected:
        return {"success": False, "error": "终端未连接"}
    
    async def _receive():
        outputs = await _receive_output(sn, timeout)
        return {
            "success": True,
            "sn": sn,
            "outputs": outputs
        }
    
    return asyncio.get_event_loop().run_until_complete(_receive())


@mcp.tool()
def resize_terminal(sn: str, cols: int, rows: int):
    """调整终端窗口大小
    
    参数:
    - sn: 设备编码
    - cols: 列数
    - rows: 行数
    """
    if sn not in sessions or not sessions[sn].is_logged_in:
        return {"success": False, "error": "终端未连接"}
    
    async def _resize():
        session = sessions[sn]
        session.cols = cols
        session.rows = rows
        await _send_winsize(session)
        return {"success": True, "sn": sn, "cols": cols, "rows": rows}
    
    return asyncio.get_event_loop().run_until_complete(_resize())


@mcp.tool()
def get_session_status(sn: str = None):
    """获取终端会话状态
    
    参数:
    - sn: 设备编码（可选，不传则返回所有会话）
    """
    if sn:
        if sn in sessions:
            session = sessions[sn]
            return {
                "sn": sn,
                "sid": session.sid,
                "is_connected": session.is_connected,
                "is_logged_in": session.is_logged_in,
                "cols": session.cols,
                "rows": session.rows,
                "buffer_size": len(session.output_buffer)
            }
        return {"sn": sn, "is_connected": False, "is_logged_in": False}
    
    all_sessions = []
    for ssn, session in sessions.items():
        all_sessions.append({
            "sn": ssn,
            "sid": session.sid,
            "is_connected": session.is_connected,
            "is_logged_in": session.is_logged_in,
            "cols": session.cols,
            "rows": session.rows,
            "buffer_size": len(session.output_buffer)
        })
    return {"sessions": all_sessions}


@mcp.tool()
def clear_buffer(sn: str):
    """清空终端输出缓冲区
    
    参数:
    - sn: 设备编码
    """
    if sn in sessions:
        sessions[sn].output_buffer = []
        sessions[sn].unack = 0
        return {"success": True, "sn": sn, "message": "缓冲区已清空"}
    return {"success": False, "error": "会话不存在"}


@mcp.tool()
def get_buffer(sn: str, lines: int = 100):
    """获取终端输出缓冲区内容
    
    参数:
    - sn: 设备编码
    - lines: 获取最后N行（默认100）
    """
    if sn not in sessions:
        return {"success": False, "error": "会话不存在"}
    
    session = sessions[sn]
    buffer = session.output_buffer[-lines:] if lines > 0 else session.output_buffer
    
    return {
        "success": True,
        "sn": sn,
        "total_lines": len(session.output_buffer),
        "returned_lines": len(buffer),
        "buffer": buffer
    }


@mcp.tool()
def interactive_session(sn: str, commands: list, delay: float = 0.5):
    """交互式会话 - 发送多个命令并收集输出
    
    参数:
    - sn: 设备编码
    - commands: 命令列表
    - delay: 命令之间的延迟（秒，默认0.5）
    """
    if sn not in sessions or not sessions[sn].is_logged_in:
        return {"success": False, "error": "终端未连接，请先调用 connect_terminal"}
    
    async def _interactive():
        results = []
        
        for cmd in commands:
            try:
                await _send_term_data(sessions[sn], cmd + "\n")
                logger.info(f"发送命令: {cmd}")
                await asyncio.sleep(delay)
                outputs = await _receive_output(sn, timeout=1.0)
                
                output_texts = []
                for o in outputs:
                    if o["type"] == "output":
                        output_texts.append(o["data"])
                
                results.append({
                    "command": cmd,
                    "outputs": output_texts,
                    "success": True
                })
            except Exception as e:
                results.append({
                    "command": cmd,
                    "outputs": [],
                    "success": False,
                    "error": str(e)
                })
        
        return {
            "success": True,
            "sn": sn,
            "total_commands": len(commands),
            "results": results
        }
    
    return asyncio.get_event_loop().run_until_complete(_interactive())


@mcp.tool()
def set_ws_base_url(base_url: str):
    """设置WebSocket基础URL
    
    参数:
    - base_url: 基础URL，如 wss://dev.xzrobot.com:10000
    """
    global WS_BASE_URL
    WS_BASE_URL = base_url.rstrip("/")
    logger.info(f"WebSocket基础URL已设置: {WS_BASE_URL}")
    return {"success": True, "base_url": WS_BASE_URL}


if __name__ == "__main__":
    logger.info("启动 Terminal MCP Server")
    mcp.run()