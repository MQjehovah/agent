"""后台/流式终端工具 — 长任务（dev server、watch、交互命令）的启动与增量读取。

与 `shell` 的区别：`shell` 同步捕获一次性输出；本工具把命令作为**后台进程**管理，
支持增量读取 stdout/stderr、向 stdin 写入、停止，适合需要持续观察/交互的场景。

会话生命周期：进程与读取任务挂在主事件循环上（`run_on_main_loop=True`），跨多次
工具调用保持存活；进程退出后仍可从缓冲区读取剩余输出与退出码。

operation:
- start : 启动后台命令, 返回 session_id
- read  : 读取增量输出(默认读取后清空缓冲) + 运行状态/退出码
- write : 向 stdin 写入(交互式命令)
- stop  : 终止进程
- list  : 列出当前会话
"""
import asyncio
import json
import logging
import os
import uuid

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


class _TerminalSession:
    def __init__(self, sid: str, command: str, proc: asyncio.subprocess.Process):
        self.id = sid
        self.command = command
        self.proc = proc
        self.buffer: list[str] = []
        self.exit_code: int | None = None
        self._pumps: list[asyncio.Task] = []

    @property
    def running(self) -> bool:
        return self.exit_code is None and self.proc.returncode is None

    def drain(self) -> str:
        out = "".join(self.buffer)
        self.buffer.clear()
        return out


_SESSIONS: dict[str, _TerminalSession] = {}


async def _pump(stream, session: _TerminalSession, tag: str):
    """持续读取子进程输出，带前缀聚合到会话缓冲。"""
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            session.buffer.append(f"[{tag}] {text}" if tag else text)
    except Exception as e:  # noqa: BLE001
        session.buffer.append(f"[{tag}] <读取结束: {e}>")
    finally:
        # 进程仍在时由 wait 收尾；退出后确保 exit_code 落地
        if session.proc.returncode is not None and session.exit_code is None:
            session.exit_code = session.proc.returncode


def stop_all_sessions() -> int:
    """进程/会话清理（供 Agent 收尾调用）；返回终止的会话数。"""
    n = 0
    for session in list(_SESSIONS.values()):
        try:
            if session.proc.returncode is None:
                session.proc.kill()
                n += 1
        except Exception:  # noqa: BLE001
            pass
    _SESSIONS.clear()
    return n


class TerminalTool(BuiltinTool):
    """后台/流式终端：启动、增量读取、写入、停止。"""

    # 进程需常驻主事件循环（跨调用存活），不走工具线程池的临时循环
    run_on_main_loop = True

    @property
    def name(self) -> str:
        return "terminal"

    @property
    def description(self) -> str:
        return (
            "后台/流式终端：把命令作为后台进程运行，支持持续读取输出、写入 stdin、停止。"
            "适合 dev server / watch / 交互式命令等长任务（一次性命令用 `shell`）。"
            "process 生命周期与当前 agent 进程一致，直到 stop 或进程退出。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["start", "read", "write", "stop", "list"],
                    "description": "操作类型",
                },
                "command": {"type": "string", "description": "operation=start 时执行的命令"},
                "session_id": {"type": "string", "description": "会话 ID（read/write/stop 用）"},
                "input": {"type": "string", "description": "operation=write 时写入 stdin 的内容（自动补换行）"},
                "cwd": {"type": "string", "description": "工作目录（operation=start）"},
                "env": {"type": "object", "description": "额外环境变量（operation=start）"},
                "clear": {"type": "boolean", "description": "read 后是否清空缓冲（默认 true）", "default": True},
                "wait": {"type": "number", "description": "read 前等待秒数（给长任务产出时间，默认 0）", "default": 0},
            },
            "required": ["operation"],
        }

    async def execute(self, operation: str = "", command: str = "", session_id: str = "",
                      input: str = "", cwd: str = "", env: dict | None = None,
                      clear: bool = True, wait: float = 0, **kwargs) -> str:
        op = (operation or "").strip().lower()
        try:
            if op == "start":
                return await self._start(command, cwd, env)
            if op == "read":
                return await self._read(session_id, clear, wait)
            if op == "write":
                return await self._write(session_id, input)
            if op == "stop":
                return await self._stop(session_id)
            if op == "list":
                return json.dumps({
                    "success": True,
                    "sessions": [
                        {"id": s.id, "command": s.command, "running": s.running,
                         "exit_code": s.exit_code, "buffered_chars": sum(len(x) for x in s.buffer)}
                        for s in _SESSIONS.values()
                    ],
                }, ensure_ascii=False)
            return self._error(f"未知 operation: {operation!r}（可选 start/read/write/stop/list）")
        except Exception as e:  # noqa: BLE001
            return self._error(f"terminal 执行失败: {e}")

    async def _start(self, command: str, cwd: str, env: dict | None) -> str:
        command = (command or "").strip()
        if not command:
            return self._error("operation=start 需要 command")
        workdir = cwd or self.workspace or os.getcwd()
        full_env = dict(os.environ)
        if env:
            full_env.update({str(k): str(v) for k, v in env.items()})
        proc = await asyncio.create_subprocess_shell(
            command, cwd=workdir, env=full_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        sid = uuid.uuid4().hex[:8]
        session = _TerminalSession(sid, command, proc)
        _SESSIONS[sid] = session
        session._pumps = [
            asyncio.create_task(_pump(proc.stdout, session, "stdout")),
            asyncio.create_task(_pump(proc.stderr, session, "stderr")),
        ]

        async def _reap():
            code = await proc.wait()
            session.exit_code = code
            session.buffer.append(f"\n[exit] {code}\n")

        asyncio.create_task(_reap())
        return json.dumps({
            "success": True, "session_id": sid, "command": command, "cwd": workdir,
            "hint": "用 terminal(operation=read, session_id=...) 读取输出; operation=stop 终止",
        }, ensure_ascii=False)

    async def _read(self, session_id: str, clear: bool, wait: float) -> str:
        session = _SESSIONS.get((session_id or "").strip())
        if session is None:
            return self._error(f"会话不存在: {session_id}")
        if wait and wait > 0:
            await asyncio.sleep(min(wait, 30))
        output = session.drain() if clear else "".join(session.buffer)
        if session.proc.returncode is not None and session.exit_code is None:
            session.exit_code = session.proc.returncode
        return json.dumps({
            "success": True,
            "session_id": session.id,
            "running": session.running,
            "exit_code": session.exit_code,
            "output": output,
        }, ensure_ascii=False)

    async def _write(self, session_id: str, data: str) -> str:
        session = _SESSIONS.get((session_id or "").strip())
        if session is None:
            return self._error(f"会话不存在: {session_id}")
        if not session.running or session.proc.stdin is None:
            return self._error("进程已结束，无法写入 stdin")
        payload = data if data.endswith("\n") else data + "\n"
        session.proc.stdin.write(payload.encode("utf-8"))
        await session.proc.stdin.drain()
        return json.dumps({"success": True, "session_id": session.id, "wrote": len(payload)},
                          ensure_ascii=False)

    async def _stop(self, session_id: str) -> str:
        session = _SESSIONS.pop((session_id or "").strip(), None)
        if session is None:
            return self._error(f"会话不存在: {session_id}")
        for task in session._pumps:
            task.cancel()
        try:
            if session.proc.returncode is None:
                session.proc.terminate()
                try:
                    await asyncio.wait_for(session.proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    session.proc.kill()
        except ProcessLookupError:
            pass
        return json.dumps({"success": True, "session_id": session.id,
                           "exit_code": session.proc.returncode}, ensure_ascii=False)

    @staticmethod
    def _error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)
