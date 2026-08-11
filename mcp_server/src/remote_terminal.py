"""
Terminal MCP Server - WebSocket Terminal
设备终端交互 MCP 服务 (rtty协议)

包含终端数据流解析器，支持：
- ANSI 转义序列过滤
- 命令回显分离
- 提示符识别
- 错误检测
"""
import os
import re
import json
import time
import logging
import asyncio
from difflib import SequenceMatcher
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import websockets
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

logger = logging.getLogger("terminal-mcp")

mcp = FastMCP("Terminal MCP Server")

# ============================================================================
# 配置常量
# ============================================================================

WS_BASE_URL = os.getenv("WS_BASE_URL", "wss://dev.xzrobot.com:10000")
DEFAULT_USERNAME = os.getenv("TERM_USERNAME", "xzrobot")
# T810 现场常用 titan@810；SC50 等仍可能是 xzyz2022!，可用 TERM_PASSWORD 覆盖
DEFAULT_PASSWORD = os.getenv("TERM_PASSWORD", "titan@810")
# 默认加宽，避免长 grep 在 80 列换行插入 \\r 污染回显（可用 TERM_COLS 覆盖）
DEFAULT_COLS = int(os.getenv("TERM_COLS", "256"))
DEFAULT_ROWS = int(os.getenv("TERM_ROWS", "40"))
# 空闲自动断开（秒）；<=0 关闭。默认 4 分钟，降低「会话已满」占坑
IDLE_TTL_SECONDS = float(os.getenv("TERM_IDLE_TTL", "240"))
# 会话已满时退避重试
BUSY_RETRY_COUNT = int(os.getenv("TERM_BUSY_RETRIES", "3"))
BUSY_RETRY_BASE = float(os.getenv("TERM_BUSY_RETRY_BASE", "1.0"))
# 命令完成：收包轮询间隔；见 prompt 立即返回
CMD_RECV_POLL = float(os.getenv("TERM_CMD_RECV_POLL", "0.4"))
DEFAULT_CMD_TIMEOUT = float(os.getenv("TERM_CMD_TIMEOUT", "30.0"))
LOGIN_STEP_TIMEOUT = float(os.getenv("TERM_LOGIN_STEP_TIMEOUT", "8.0"))
# interactive_session 整批墙钟预算（须 < MCP call_tool 60s）；可用 TERM_SESSION_WALL 覆盖
SESSION_WALL_BUDGET = float(os.getenv("TERM_SESSION_WALL", "50.0"))
# 单次 interactive 建议命令条数上限（超出仍执行但按墙钟截断）
SESSION_CMD_SOFT_CAP = int(os.getenv("TERM_SESSION_CMD_CAP", "4"))

LoginErrorOffline = 0x01
LoginErrorBusy = 0x02


class SessionBusyError(Exception):
    """rtty 返回会话已满"""


class DeviceOfflineError(Exception):
    """设备离线"""


# ============================================================================
# 终端数据流解析器
# ============================================================================

class OutputType(Enum):
    """输出类型"""
    COMMAND_ECHO = "command_echo"      # 命令回显（用户输入）
    COMMAND_OUTPUT = "command_output"  # 命令输出
    PROMPT = "prompt"                  # 提示符
    CONTROL = "control"                # 控制序列
    ERROR = "error"                    # 错误信息
    UNKNOWN = "unknown"                # 未知


@dataclass
class ParsedOutput:
    """解析后的输出"""
    type: OutputType
    content: str
    raw: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "content": self.content,
            "raw": self.raw,
            "timestamp": self.timestamp
        }


@dataclass
class CommandResult:
    """命令执行结果"""
    command: str
    output: str
    success: bool = True
    error: str = ""
    raw_output: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "output": self.output,
            "success": self.success,
            "error": self.error,
            "raw_output": self.raw_output
        }


class ANSIStripper:
    """ANSI 转义序列过滤器"""

    # ANSI 转义序列正则（不含 \r/\b：须先按光标语义处理，不能直接删除）
    ANSI_PATTERN = re.compile(
        r'\x1B(?:'
        r'[\[(][0-9;]*[a-zA-Z]'  # CSI 序列: ESC[...字母
        r'|][0-9;]*[a-zA-Z]'     # OSC 序列
        r'|[()][AB012]'          # 字符集选择
        r'|[78]'                 # 保存/恢复光标
        r'|[DM]'                 # 删除行/移动光标
        r')|'
        r'\x07'                  # BEL
        r'|\x1B[=>]'             # 键盘模式
        r'|\x00'                 # 空字符
    )

    @classmethod
    def strip(cls, text: str) -> str:
        """移除 ANSI 转义序列"""
        return cls.ANSI_PATTERN.sub('', text)

    @classmethod
    def apply_cursor_controls(cls, text: str) -> str:
        """按终端语义处理 \\r（行首覆盖）与退格，避免 log\\rgs → loggs 这类拼接污染。

        窄终端换行常在回显中插入裸 \\r；若直接删掉 \\r 会把换行两侧字符拼成假路径。
        """
        text = text.replace("\r\n", "\n").replace("\r\x00", "\n")
        out_lines: List[str] = []
        for line in text.split("\n"):
            buf: List[str] = []
            col = 0
            for ch in line:
                if ch == "\r":
                    col = 0
                elif ch in ("\x08", "\x7f"):
                    if col > 0:
                        col -= 1
                        if col < len(buf):
                            buf.pop(col)
                    elif buf:
                        buf.pop()
                else:
                    if col < len(buf):
                        buf[col] = ch
                    else:
                        buf.append(ch)
                    col += 1
            out_lines.append("".join(buf))
        return "\n".join(out_lines)

    @classmethod
    def clean_for_display(cls, text: str) -> str:
        """清理文本用于显示"""
        text = cls.strip(text)
        text = cls.apply_cursor_controls(text)
        # 移除其余控制字符（保留 \n \t）
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        return text.strip()


class TerminalParser:
    """终端数据流解析器"""

    # 常见提示符模式
    PROMPT_PATTERNS = [
        r'^\[[\w@\-]+\][\w\$#]\s*$',           # [user@host]$
        r'^[\w\-]+@[\w\-]+:~?[\/\w]*[\$#]\s*$', # user@host:~$
        r'^[\w\-]+[\$#]\s*$',                   # user$
        r'^root@[\w\-]+:.*[\$#]\s*$',           # root@host:#
        r'^\$\s*$',                              # $
        r'^#\s*$',                               # #
        r'^>\s*$',                               # >
        r'^.*[@\$#]\s*$',                        # 通用模式
    ]

    # 命令行编辑字符
    EDIT_CHARS = {'\x7f', '\x08', '\x1b'}  # DEL, BS, ESC

    def __init__(self, prompt_pattern: str = None):
        """
        初始化解析器

        Args:
            prompt_pattern: 自定义提示符正则模式
        """
        self.prompt_pattern = prompt_pattern
        self.buffer: List[str] = []
        self.last_command: str = ""
        self.pending_output: List[str] = []
        self._last_output_time: float = 0

    def _normalize_for_echo_match(self, text: str) -> str:
        """去掉空白与常见噪声，便于比对被 \\r 污染的回显。"""
        return re.sub(r"\s+", "", text.strip())

    def _is_command_echo(self, line: str, command: str) -> bool:
        """判断一行是否为命令回显（含窄终端换行导致的残缺/错位回显）。"""
        cmd = command.strip()
        stripped = line.strip()
        if not cmd or not stripped:
            return False

        if stripped == cmd or stripped.startswith(cmd):
            return True

        # 真实 grep 命中行通常以「行号:」开头，勿当回显丢掉
        if re.match(r"^\d+:", stripped):
            return False

        cmd_first = cmd.split()[0]
        n_line = self._normalize_for_echo_match(stripped)
        n_cmd = self._normalize_for_echo_match(cmd)
        if len(n_line) < 8:
            return False

        # 完整或以同一命令词开头
        if stripped.startswith(cmd_first) and (
            cmd in stripped
            or n_cmd in n_line
            or n_line in n_cmd
            or SequenceMatcher(None, n_line, n_cmd).ratio() >= 0.75
        ):
            return True

        # 高相似：loggs / xzrobott_driver2 / 202608066 等污染回显
        if SequenceMatcher(None, n_line, n_cmd).ratio() >= 0.82:
            return True

        # 换行截断后的片段仍是命令的子串（足够长才认）
        if len(n_line) >= 24 and (n_line in n_cmd or n_cmd in n_line):
            return True

        # \\r 覆盖后行首被吃掉，但仍带管道/重定向等 shell 痕迹
        shell_markers = ("2>/dev/null", "| head", "| tail", "| grep", " |head", " |tail")
        if any(m in stripped for m in shell_markers) and SequenceMatcher(None, n_line, n_cmd).ratio() >= 0.45:
            return True

        # 以 grep/ls/head/cat 等开头且与命令共享长路径片段
        if stripped.startswith(cmd_first) and len(n_cmd) >= 30:
            paths = re.findall(r"/[A-Za-z0-9_./\-]+", cmd)
            for p in sorted(paths, key=len, reverse=True)[:3]:
                pn = self._normalize_for_echo_match(p)
                if len(pn) >= 18 and SequenceMatcher(None, n_line, n_cmd).ratio() >= 0.55:
                    overlap = sum(1 for c in pn if c in n_line)
                    if overlap / len(pn) >= 0.85:
                        return True

        return False

    def _is_prompt(self, text: str) -> bool:
        """检查文本是否为提示符"""
        clean = ANSIStripper.clean_for_display(text).strip()

        if self.prompt_pattern:
            return bool(re.match(self.prompt_pattern, clean))

        for pattern in self.PROMPT_PATTERNS:
            if re.match(pattern, clean, re.MULTILINE):
                return True

        return False

    def _extract_command_echo(self, output: str, command: str) -> Tuple[str, str]:
        """
        从输出中分离命令回显

        Returns:
            (command_echo, remaining_output)
        """
        clean_output = ANSIStripper.clean_for_display(output)
        clean_command = command.strip()

        # 查找命令回显
        lines = clean_output.split('\n')
        echo_lines = []
        remaining_lines = []

        found_echo = False
        for line in lines:
            clean_line = line.strip()
            if not found_echo and self._is_command_echo(clean_line, clean_command):
                echo_lines.append(line)
                found_echo = True
                continue
            remaining_lines.append(line)

        return '\n'.join(echo_lines), '\n'.join(remaining_lines)

    def parse_chunk(self, chunk: str, expect_command: str = None) -> List[ParsedOutput]:
        """
        解析单个数据块

        Args:
            chunk: 原始数据块
            expect_command: 期望的命令（用于识别回显）

        Returns:
            解析后的输出列表
        """
        results = []

        # 处理控制消息
        if chunk.startswith('{') and chunk.endswith('}'):
            try:
                msg = json.loads(chunk)
                results.append(ParsedOutput(
                    type=OutputType.CONTROL,
                    content=json.dumps(msg),
                    raw=chunk
                ))
                return results
            except json.JSONDecodeError:
                pass

        # 清理 ANSI 序列用于分析
        clean = ANSIStripper.clean_for_display(chunk)

        # 按行分割
        lines = chunk.split('\n')

        for line in lines:
            if not line.strip():
                continue

            clean_line = ANSIStripper.clean_for_display(line)

            # 检查是否为提示符
            if self._is_prompt(clean_line):
                results.append(ParsedOutput(
                    type=OutputType.PROMPT,
                    content=clean_line.strip(),
                    raw=line
                ))
            # 检查是否为命令回显
            elif expect_command and self._is_command_echo(clean_line, expect_command):
                results.append(ParsedOutput(
                    type=OutputType.COMMAND_ECHO,
                    content=clean_line.strip(),
                    raw=line
                ))
            # 检查是否为错误
            elif any(kw in clean_line.lower() for kw in ['error', 'failed', 'not found', 'permission denied', 'no such']):
                results.append(ParsedOutput(
                    type=OutputType.ERROR,
                    content=clean_line.strip(),
                    raw=line
                ))
            # 普通输出
            else:
                results.append(ParsedOutput(
                    type=OutputType.COMMAND_OUTPUT,
                    content=clean_line.strip(),
                    raw=line
                ))

        return results

    def parse_command_response(self, outputs: List[str], command: str) -> CommandResult:
        """
        解析完整的命令响应

        Args:
            outputs: 原始输出列表
            command: 执行的命令

        Returns:
            命令执行结果
        """
        # 合并所有输出
        full_output = ''.join(outputs)

        # 清理输出
        clean_output = ANSIStripper.clean_for_display(full_output)

        # 分行处理
        lines = clean_output.split('\n')

        # 过滤并分类
        result_lines = []
        prompt_found = False
        has_error = False
        error_msg = ""

        for line in lines:
            stripped = line.strip()

            # 跳过空行
            if not stripped:
                continue

            # 检查是否为提示符
            if self._is_prompt(stripped):
                prompt_found = True
                continue

            # 检查是否为命令回显（含 \\r 污染后的假路径）
            if self._is_command_echo(stripped, command):
                continue

            # 检查错误
            if any(kw in stripped.lower() for kw in ['error:', 'failed:', 'not found', 'permission denied', 'no such file']):
                has_error = True
                error_msg = stripped

            result_lines.append(stripped)

        return CommandResult(
            command=command,
            output='\n'.join(result_lines),
            success=not has_error,
            error=error_msg,
            raw_output=outputs
        )


class InteractiveTerminalSession:
    """
    交互式终端会话管理器

    维护会话状态，支持命令-响应配对
    """

    def __init__(self, prompt_pattern: str = None):
        self.parser = TerminalParser(prompt_pattern)
        self.command_history: List[Dict[str, Any]] = []
        self.output_buffer: List[str] = []
        self._current_command: str = ""
        self._command_sent_time: float = 0

    def start_command(self, command: str):
        """
        记录开始执行命令

        Args:
            command: 要执行的命令
        """
        self._current_command = command
        self._command_sent_time = time.time()
        self.output_buffer = []

    def collect_output(self, output: str):
        """
        收集命令输出

        Args:
            output: 输出数据
        """
        self.output_buffer.append(output)

    def finish_command(self, timeout: float = 0.5) -> CommandResult:
        """
        完成命令执行，解析结果

        Args:
            timeout: 等待额外输出的超时时间

        Returns:
            命令执行结果
        """
        result = self.parser.parse_command_response(
            self.output_buffer,
            self._current_command
        )

        # 记录历史
        self.command_history.append({
            "command": self._current_command,
            "output": result.output,
            "success": result.success,
            "timestamp": self._command_sent_time
        })

        # 重置状态
        self._current_command = ""
        self.output_buffer = []

        return result

    def get_last_command_result(self) -> Optional[CommandResult]:
        """获取最后一条命令的结果"""
        if not self.command_history:
            return None

        last = self.command_history[-1]
        return CommandResult(
            command=last["command"],
            output=last["output"],
            success=last["success"]
        )

    def parse_streaming_output(self, chunk: str) -> List[ParsedOutput]:
        """
        解析流式输出

        Args:
            chunk: 数据块

        Returns:
            解析结果列表
        """
        return self.parser.parse_chunk(chunk, self._current_command)


def parse_terminal_output(
    outputs: List[str],
    command: str = None,
    strip_ansi: bool = True
) -> Dict[str, Any]:
    """
    便捷函数：解析终端输出

    Args:
        outputs: 原始输出列表
        command: 执行的命令（可选）
        strip_ansi: 是否移除 ANSI 序列

    Returns:
        解析结果字典
    """
    parser = TerminalParser()

    if command:
        result = parser.parse_command_response(outputs, command)
        return result.to_dict()

    # 没有命令信息，只做基本解析
    all_parsed = []
    for output in outputs:
        parsed = parser.parse_chunk(output)
        all_parsed.extend([p.to_dict() for p in parsed])

    return {
        "parsed": all_parsed,
        "raw": outputs
    }


# ============================================================================
# 终端会话管理
# ============================================================================

@dataclass
class TerminalSession:
    sn: str
    ws: Any = None
    sid: str = ""
    output_buffer: list = field(default_factory=list)
    is_connected: bool = False
    is_logged_in: bool = False
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS
    unack: int = 0
    parser: TerminalParser = field(default_factory=TerminalParser)
    interactive: InteractiveTerminalSession = field(default_factory=InteractiveTerminalSession)
    username: str = DEFAULT_USERNAME
    password: str = DEFAULT_PASSWORD
    last_activity: float = field(default_factory=time.time)
    base_url: str = ""


sessions: dict[str, TerminalSession] = {}
_session_locks: dict[str, asyncio.Lock] = {}
_idle_tasks: dict[str, asyncio.Task] = {}


# ============================================================================
# 内部函数
# ============================================================================

def _get_sn_lock(sn: str) -> asyncio.Lock:
    lock = _session_locks.get(sn)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[sn] = lock
    return lock


def _ws_is_open(ws: Any) -> bool:
    if ws is None:
        return False
    state = getattr(ws, "state", None)
    if state is not None:
        name = getattr(state, "name", str(state))
        return name == "OPEN" or str(state).endswith("OPEN")
    return bool(getattr(ws, "open", False))


def _session_alive(session: Optional[TerminalSession]) -> bool:
    return bool(
        session
        and session.is_connected
        and session.is_logged_in
        and _ws_is_open(session.ws)
    )


def _cancel_idle_task(sn: str):
    task = _idle_tasks.pop(sn, None)
    if task and not task.done():
        task.cancel()


def _touch_session(session: TerminalSession):
    session.last_activity = time.time()
    _schedule_idle_disconnect(session.sn)


def _schedule_idle_disconnect(sn: str):
    _cancel_idle_task(sn)
    if IDLE_TTL_SECONDS <= 0:
        return

    async def _idle_watch():
        try:
            await asyncio.sleep(IDLE_TTL_SECONDS)
            session = sessions.get(sn)
            if not session:
                return
            idle_for = time.time() - session.last_activity
            if idle_for >= IDLE_TTL_SECONDS - 0.05:
                logger.info(f"空闲超时断开终端: {sn} (idle={idle_for:.0f}s)")
                await _purge_session(sn)
        except asyncio.CancelledError:
            return

    _idle_tasks[sn] = asyncio.create_task(_idle_watch())


async def _purge_session(sn: str):
    """关闭并移除会话，避免僵死 is_logged_in。"""
    _cancel_idle_task(sn)
    session = sessions.pop(sn, None)
    if not session:
        return
    session.is_connected = False
    session.is_logged_in = False
    ws = session.ws
    session.ws = None
    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass
    logger.info(f"终端已断开: {sn}")


def _text_ends_with_prompt(parser: TerminalParser, text: str) -> bool:
    clean = ANSIStripper.clean_for_display(text)
    lines = [ln.strip() for ln in clean.split("\n") if ln.strip()]
    if not lines:
        return False
    last = lines[-1]
    lower = last.lower()
    if "password" in lower or lower.startswith("login") or "username" in lower:
        return False
    return parser._is_prompt(last)


def _is_login_prompt_text(text: str) -> bool:
    lower = ANSIStripper.clean_for_display(text).lower()
    return any(k in lower for k in ("login:", "username:", "user name:"))


def _is_password_prompt_text(text: str) -> bool:
    lower = ANSIStripper.clean_for_display(text).lower()
    return "password" in lower


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
                    raise DeviceOfflineError("设备离线")
                elif msg.get("err") == LoginErrorBusy:
                    session.is_connected = False
                    raise SessionBusyError("会话已满")

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


async def _process_ws_message(session: TerminalSession, message: Any) -> List[Dict[str, Any]]:
    """处理单条 WS 消息，返回标准化 outputs 片段。"""
    outputs: List[Dict[str, Any]] = []
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
        text = data.decode("utf-8", errors="replace")
        outputs.append({"type": "output", "data": text})
        session.output_buffer.append(text)
        if session.unack > 4 * 1024:
            ack_msg = {"type": "ack", "ack": session.unack}
            await session.ws.send(json.dumps(ack_msg))
            session.unack = 0
    return outputs


async def _receive_until(
    sn: str,
    *,
    overall_timeout: float,
    poll_timeout: float = CMD_RECV_POLL,
    stop_when=None,
) -> List[Dict[str, Any]]:
    """持续收包直到 stop_when(accumulated_output_text) 为真，或 overall_timeout。

    stop_when 为 None 时退化为「idle poll_timeout 无数据则停」（兼容旧行为）。
    """
    if sn not in sessions or not sessions[sn].is_connected or not _ws_is_open(sessions[sn].ws):
        return []

    session = sessions[sn]
    outputs: List[Dict[str, Any]] = []
    accumulated = ""
    deadline = time.time() + overall_timeout
    got_data = False

    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            wait = min(poll_timeout, remaining)
            try:
                message = await asyncio.wait_for(session.ws.recv(), timeout=wait)
            except asyncio.TimeoutError:
                if stop_when is None:
                    break
                continue

            chunk = await _process_ws_message(session, message)
            outputs.extend(chunk)
            for o in chunk:
                if o["type"] == "output":
                    accumulated += o["data"]
                    got_data = True
            _touch_session(session)

            if stop_when is not None and got_data and stop_when(accumulated):
                break
    except websockets.exceptions.ConnectionClosed:
        session.is_connected = False
        session.is_logged_in = False
        outputs.append({"type": "error", "data": "连接已断开"})

    return outputs


async def _receive_output(sn: str, timeout: float = 2.0) -> list:
    """兼容：在 timeout 内持续收包，idle 则停。"""
    return await _receive_until(sn, overall_timeout=timeout, poll_timeout=timeout, stop_when=None)


async def _auto_login(session: TerminalSession, username: str, password: str, timeout: float = LOGIN_STEP_TIMEOUT):
    """提示符驱动自动登录，无固定长 sleep。"""
    logger.info(f"自动登录: {username}")
    sn = session.sn
    parser = session.parser

    def shell_ready(text: str) -> bool:
        return _text_ends_with_prompt(parser, text)

    def login_or_shell(text: str) -> bool:
        return _is_login_prompt_text(text) or shell_ready(text)

    def password_ready(text: str) -> bool:
        return _is_password_prompt_text(text) or shell_ready(text)

    outputs = await _receive_until(
        sn, overall_timeout=timeout, poll_timeout=CMD_RECV_POLL, stop_when=login_or_shell
    )
    initial = "".join(o["data"] for o in outputs if o["type"] == "output")
    if shell_ready(initial):
        logger.info("登录结果: success=True (已在 shell)")
        return {"success": True, "outputs": outputs}

    await _send_term_data(session, username + "\n")
    _touch_session(session)

    outputs = await _receive_until(
        sn, overall_timeout=timeout, poll_timeout=CMD_RECV_POLL, stop_when=password_ready
    )
    mid = "".join(o["data"] for o in outputs if o["type"] == "output")
    if shell_ready(mid):
        logger.info("登录结果: success=True (无需密码)")
        return {"success": True, "outputs": outputs}

    await _send_term_data(session, password + "\n")
    _touch_session(session)

    outputs = await _receive_until(
        sn, overall_timeout=timeout, poll_timeout=CMD_RECV_POLL, stop_when=shell_ready
    )
    final = "".join(o["data"] for o in outputs if o["type"] == "output")
    login_success = shell_ready(final)
    if not login_success and final:
        clean = ANSIStripper.clean_for_display(final)
        if "welcome" in clean.lower() and ("$" in clean or "#" in clean):
            login_success = True

    logger.info(f"登录结果: success={login_success}")
    return {"success": login_success, "outputs": outputs}


async def _connect_ws_once(
    sn: str,
    cols: int,
    rows: int,
    username: Optional[str],
    password: Optional[str],
    base_url: Optional[str] = None,
) -> TerminalSession:
    url_base = (base_url or WS_BASE_URL).rstrip("/")
    url = f"{url_base}/connect/{sn}"
    logger.info(f"连接终端: {url}")

    ws = await websockets.connect(
        url,
        ping_interval=20,
        ping_timeout=10,
        close_timeout=5,
    )
    session = TerminalSession(
        sn=sn,
        ws=ws,
        is_connected=True,
        cols=cols,
        rows=rows,
        username=username or DEFAULT_USERNAME,
        password=password or DEFAULT_PASSWORD,
        base_url=url_base,
    )
    sessions[sn] = session

    try:
        await _wait_login(session)
        if not session.is_logged_in:
            raise Exception("登录失败")
        if username and password:
            await _auto_login(session, username, password)
        _touch_session(session)
        return session
    except Exception:
        await _purge_session(sn)
        raise


async def _connect_ws(
    sn: str,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
    username: str = None,
    password: str = None,
    base_url: str = None,
) -> TerminalSession:
    existing = sessions.get(sn)
    if _session_alive(existing):
        _touch_session(existing)
        return existing
    if existing is not None:
        logger.info(f"复用失败，清理僵死会话后重连: {sn}")
        await _purge_session(sn)

    last_err: Optional[Exception] = None
    for attempt in range(max(1, BUSY_RETRY_COUNT)):
        try:
            return await _connect_ws_once(sn, cols, rows, username, password, base_url)
        except SessionBusyError as e:
            last_err = e
            delay = BUSY_RETRY_BASE * (2 ** attempt)
            logger.warning(f"会话已满，{delay:.1f}s 后重试 ({attempt + 1}/{BUSY_RETRY_COUNT}): {sn}")
            await _purge_session(sn)
            await asyncio.sleep(delay)
        except DeviceOfflineError:
            await _purge_session(sn)
            raise
        except Exception as e:
            logger.error(f"终端连接失败: {e}")
            await _purge_session(sn)
            raise

    raise last_err or SessionBusyError("会话已满")


async def _disconnect_ws(sn: str):
    await _purge_session(sn)


async def _send_term_data(session: TerminalSession, data: str):
    buf = bytearray([0]) + data.encode("utf-8")
    await session.ws.send(bytes(buf))


async def _ensure_session(
    sn: str,
    *,
    auto_reconnect: bool = True,
) -> Optional[TerminalSession]:
    """确保会话可用；断线时用缓存凭据重连一次。"""
    session = sessions.get(sn)
    if _session_alive(session):
        return session
    if not auto_reconnect or session is None:
        return None
    username = session.username
    password = session.password
    cols = session.cols
    rows = session.rows
    base_url = session.base_url or None
    logger.info(f"会话失效，尝试自动重连: {sn}")
    await _purge_session(sn)
    try:
        return await _connect_ws(sn, cols, rows, username, password, base_url)
    except Exception as e:
        logger.error(f"自动重连失败: {e}")
        return None


async def _run_command(
    sn: str,
    command: str,
    overall_timeout: float = DEFAULT_CMD_TIMEOUT,
) -> Tuple[List[str], CommandResult]:
    """发送命令并等到 shell 提示符（或 overall_timeout）。"""
    session = sessions[sn]
    session.interactive.start_command(command)
    await _send_term_data(session, command + "\n")
    logger.info(f"发送命令: {command}")
    _touch_session(session)

    def done(text: str) -> bool:
        return _text_ends_with_prompt(session.parser, text)

    outputs = await _receive_until(
        sn,
        overall_timeout=overall_timeout,
        poll_timeout=CMD_RECV_POLL,
        stop_when=done,
    )
    raw_texts = [o["data"] for o in outputs if o["type"] == "output"]
    result = session.interactive.parser.parse_command_response(raw_texts, command)
    session.interactive.command_history.append({
        "command": command,
        "output": result.output,
        "success": result.success,
        "timestamp": time.time(),
    })
    session.interactive._current_command = ""
    session.interactive.output_buffer = []
    _touch_session(session)
    return raw_texts, result


# ============================================================================
# MCP 工具函数
# ============================================================================

@mcp.tool()
async def connect_terminal(sn: str, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS, username: str = DEFAULT_USERNAME, password: str = DEFAULT_PASSWORD, base_url: str = None):
    """连接设备终端并自动登录

    参数:
    - sn: 设备编码
    - cols: 终端列数（默认256，避免长命令换行插入 \\r 污染回显；可用 TERM_COLS）
    - rows: 终端行数（默认40；可用 TERM_ROWS）
    - username: 登录用户名（默认xzrobot）
    - password: 登录密码（默认 titan@810，可用 TERM_PASSWORD 覆盖）
    - base_url: WebSocket基础URL（可选，默认为 wss://dev.xzrobot.com:10000）
    """
    async with _get_sn_lock(sn):
        try:
            session = await _connect_ws(sn, cols, rows, username, password, base_url)
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


@mcp.tool()
async def disconnect_terminal(sn: str):
    """断开设备终端连接

    参数:
    - sn: 设备编码
    """
    async with _get_sn_lock(sn):
        if sn not in sessions:
            return {"success": True, "sn": sn, "message": "终端未连接"}
        await _disconnect_ws(sn)
        return {"success": True, "sn": sn, "message": "终端已断开"}


@mcp.tool()
async def send_command(sn: str, command: str, wait_output: bool = True, timeout: float = DEFAULT_CMD_TIMEOUT, parse_output: bool = True):
    """发送命令到终端并解析响应

    参数:
    - sn: 设备编码
    - command: 要发送的命令（会自动添加换行符）。大日志请用 `grep … | head -n 20`；禁止裸 `tail`/`cat` 冒充时间窗证据
    - wait_output: 是否等待输出（默认True）
    - timeout: 等待命令完成的总超时（秒，默认见提示符即返回；可用 TERM_CMD_TIMEOUT）
    - parse_output: 是否解析输出结构（默认True）

    返回:
    - success: 是否成功
    - command: 发送的命令
    - output: 清理后的命令输出（已移除命令回显、提示符、ANSI序列）
    - raw_outputs: 原始输出列表
    - parsed: 解析后的结构化输出（当parse_output=True时）
    """
    async with _get_sn_lock(sn):
        session = await _ensure_session(sn, auto_reconnect=True)
        if not session:
            return {"success": False, "error": "终端未连接，请先调用 connect_terminal"}

        try:
            if not wait_output:
                await _send_term_data(session, command + "\n")
                _touch_session(session)
                return {"success": True, "sn": sn, "command": command}

            raw_texts, result = await _run_command(sn, command, overall_timeout=timeout)
            if parse_output:
                return {
                    "success": True,
                    "sn": sn,
                    "command": command,
                    "output": result.output,
                    "command_success": result.success,
                    "error": result.error if not result.success else "",
                    "raw_outputs": raw_texts,
                    "parsed": {
                        "lines": result.output.split("\n") if result.output else [],
                        "line_count": len(result.output.split("\n")) if result.output else 0,
                    },
                }
            return {
                "success": True,
                "sn": sn,
                "command": command,
                "output": result.output,
                "raw_outputs": raw_texts,
            }
        except websockets.exceptions.ConnectionClosed:
            dead = sessions.get(sn)
            if dead:
                dead.is_connected = False
                dead.is_logged_in = False
                dead.ws = None
            return {"success": False, "error": "连接已断开"}
        except Exception as e:
            return {"success": False, "error": str(e)}


@mcp.tool()
async def send_raw(sn: str, data: str):
    """发送原始数据到终端（不添加换行符）

    参数:
    - sn: 设备编码
    - data: 原始数据字符串
    """
    async with _get_sn_lock(sn):
        if not _session_alive(sessions.get(sn)):
            return {"success": False, "error": "终端未连接"}
        try:
            session = sessions[sn]
            await _send_term_data(session, data)
            _touch_session(session)
            return {"success": True, "sn": sn, "data": data}
        except Exception as e:
            return {"success": False, "error": str(e)}


@mcp.tool()
async def receive_output(sn: str, timeout: float = 2.0):
    """接收终端输出

    参数:
    - sn: 设备编码
    - timeout: 等待输出的超时时间（秒，默认2.0）
    """
    async with _get_sn_lock(sn):
        if sn not in sessions or not sessions[sn].is_connected:
            return {"success": False, "error": "终端未连接"}
        outputs = await _receive_output(sn, timeout)
        if sn in sessions:
            _touch_session(sessions[sn])
        return {"success": True, "sn": sn, "outputs": outputs}


@mcp.tool()
async def resize_terminal(sn: str, cols: int, rows: int):
    """调整终端窗口大小

    参数:
    - sn: 设备编码
    - cols: 列数
    - rows: 行数
    """
    async with _get_sn_lock(sn):
        if not _session_alive(sessions.get(sn)):
            return {"success": False, "error": "终端未连接"}
        session = sessions[sn]
        session.cols = cols
        session.rows = rows
        await _send_winsize(session)
        _touch_session(session)
        return {"success": True, "sn": sn, "cols": cols, "rows": rows}


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
                "is_connected": session.is_connected and _ws_is_open(session.ws),
                "is_logged_in": session.is_logged_in and _ws_is_open(session.ws),
                "cols": session.cols,
                "rows": session.rows,
                "buffer_size": len(session.output_buffer),
                "last_activity": session.last_activity,
                "idle_ttl": IDLE_TTL_SECONDS,
            }
        return {"sn": sn, "is_connected": False, "is_logged_in": False}

    all_sessions = []
    for ssn, session in sessions.items():
        all_sessions.append({
            "sn": ssn,
            "sid": session.sid,
            "is_connected": session.is_connected and _ws_is_open(session.ws),
            "is_logged_in": session.is_logged_in and _ws_is_open(session.ws),
            "cols": session.cols,
            "rows": session.rows,
            "buffer_size": len(session.output_buffer),
            "last_activity": session.last_activity,
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
async def interactive_session(sn: str, commands: list, delay: float = 0.0, parse_outputs: bool = True, timeout: float = DEFAULT_CMD_TIMEOUT):
    """交互式会话 - 发送多个命令并收集解析后的输出

    参数:
    - sn: 设备编码
    - commands: 命令列表（建议 ≤4 条；大日志须 `grep … | head -n 20`，禁止裸 tail/cat/无 head 全日 grep）
    - delay: 命令之间的额外延迟（秒，默认0；完成检测已按提示符，一般无需延迟）
    - parse_outputs: 是否解析输出结构（默认True）
    - timeout: 单条命令超时上限（秒）；整批另受墙钟预算 TERM_SESSION_WALL（默认50s，对齐 MCP 60s）约束

    返回:
    - success: 整体是否成功
    - results: 每条命令的执行结果列表，包含:
        - command: 命令
        - output: 清理后的输出
        - success: 命令是否成功
        - error: 错误信息（如有）
    - wall_budget_seconds: 本批墙钟预算
    - truncated: 是否因墙钟预算未跑完全部命令
    """
    async with _get_sn_lock(sn):
        session = await _ensure_session(sn, auto_reconnect=True)
        if not session:
            return {"success": False, "error": "终端未连接，请先调用 connect_terminal"}

        cmd_list = list(commands or [])
        results = []
        wall = max(1.0, SESSION_WALL_BUDGET)
        deadline = time.time() + wall
        truncated = False
        for idx, cmd in enumerate(cmd_list):
            remaining = deadline - time.time()
            if remaining <= 0.5:
                truncated = True
                for skipped in cmd_list[idx:]:
                    results.append({
                        "command": skipped,
                        "output": "",
                        "success": False,
                        "error": f"interactive_session 墙钟预算 {wall}s 已用尽，未执行",
                    })
                break
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                per_timeout = min(float(timeout), remaining)
                raw_texts, result = await _run_command(
                    sn, cmd, overall_timeout=per_timeout
                )
                if parse_outputs:
                    results.append({
                        "command": cmd,
                        "output": result.output,
                        "success": result.success,
                        "error": result.error if not result.success else "",
                    })
                else:
                    results.append({
                        "command": cmd,
                        "output": "\n".join(raw_texts),
                        "success": True,
                    })
            except Exception as e:
                results.append({
                    "command": cmd,
                    "output": "",
                    "success": False,
                    "error": str(e),
                })

        if len(cmd_list) > SESSION_CMD_SOFT_CAP and not truncated:
            logger.info(
                "interactive_session 命令数 %s 超过建议上限 %s（仍已执行；请拆批）",
                len(cmd_list),
                SESSION_CMD_SOFT_CAP,
            )

        return {
            "success": True,
            "sn": sn,
            "total_commands": len(cmd_list),
            "results": results,
            "wall_budget_seconds": wall,
            "truncated": truncated,
        }


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


@mcp.tool()
def strip_ansi(text: str):
    """移除文本中的ANSI转义序列

    参数:
    - text: 包含ANSI序列的文本

    返回:
    - 清理后的纯文本
    """
    cleaned = ANSIStripper.clean_for_display(text)
    return {
        "success": True,
        "original_length": len(text),
        "cleaned_length": len(cleaned),
        "cleaned_text": cleaned
    }


@mcp.tool()
def parse_output(outputs: list, command: str = None):
    """解析终端输出列表

    参数:
    - outputs: 终端输出字符串列表
    - command: 相关命令（可选，用于分离命令回显）

    返回:
    - 解析后的结构化输出
    """
    result = parse_terminal_output(outputs, command)
    return {
        "success": True,
        "result": result
    }


@mcp.tool()
async def execute_with_retry(sn: str, command: str, max_retries: int = 3, retry_delay: float = 1.0, timeout: float = DEFAULT_CMD_TIMEOUT):
    """执行命令并支持失败重试

    参数:
    - sn: 设备编码
    - command: 要执行的命令
    - max_retries: 最大重试次数（默认3）
    - retry_delay: 重试延迟（秒，默认1.0）
    - timeout: 每次执行的超时时间（秒）

    返回:
    - 命令执行结果
    """
    async with _get_sn_lock(sn):
        session = await _ensure_session(sn, auto_reconnect=True)
        if not session:
            return {"success": False, "error": "终端未连接，请先调用 connect_terminal"}

        last_error = None
        for attempt in range(max_retries):
            try:
                raw_texts, result = await _run_command(sn, command, overall_timeout=timeout)
                if result.success or attempt == max_retries - 1:
                    return {
                        "success": True,
                        "sn": sn,
                        "command": command,
                        "output": result.output,
                        "command_success": result.success,
                        "error": result.error if not result.success else "",
                        "attempts": attempt + 1,
                        "raw_outputs": raw_texts,
                    }
                logger.warning(f"命令执行失败，准备重试: {result.error}")
                await asyncio.sleep(retry_delay)
            except Exception as e:
                last_error = str(e)
                logger.error(f"命令执行异常 (尝试 {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)

        return {
            "success": False,
            "sn": sn,
            "command": command,
            "error": last_error or "命令执行失败",
            "attempts": max_retries,
        }


@mcp.tool()
async def wait_for_prompt(sn: str, timeout: float = 5.0):
    """等待终端提示符出现

    参数:
    - sn: 设备编码
    - timeout: 超时时间（秒，默认5.0）

    返回:
    - 是否成功等到提示符
    """
    async with _get_sn_lock(sn):
        if sn not in sessions or not sessions[sn].is_connected:
            return {"success": False, "error": "终端未连接"}

        session = sessions[sn]

        def done(text: str) -> bool:
            return _text_ends_with_prompt(session.parser, text)

        outputs = await _receive_until(
            sn, overall_timeout=timeout, poll_timeout=CMD_RECV_POLL, stop_when=done
        )
        accumulated = "".join(o["data"] for o in outputs if o["type"] == "output")
        if done(accumulated):
            clean = ANSIStripper.clean_for_display(accumulated)
            lines = [ln.strip() for ln in clean.split("\n") if ln.strip()]
            return {"success": True, "sn": sn, "prompt": lines[-1] if lines else clean.strip()}
        return {"success": False, "error": "等待提示符超时"}


if __name__ == "__main__":
    logger.info("启动 Terminal MCP Server")
    mcp.run()
