"""最小 LSP 客户端（stdio + JSON-RPC）——按文件语言懒启动语言服务器。

用途：给 agent 提供「编辑器级」能力：实时诊断、hover、跳转定义、查找引用、重命名。
- 语言服务器通过环境变量 `AGENT_LSP_SERVERS`（JSON: 语言→命令数组）配置，
  缺省用常见服务器（pyright-langserver / typescript-language-server / gopls / rust-analyzer）。
- 服务器未安装/启动失败 → `get_client` 返回 None，工具侧优雅降级并提示。
- 所有连接挂在**主事件循环**（工具 `run_on_main_loop=True`），按 (root, 语言) 复用。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil

logger = logging.getLogger("agent.lsp")

_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescriptreact",
    ".js": "javascript", ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust",
}

_DEFAULT_SERVERS = {
    "python": ["pyright-langserver", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
    "javascript": ["typescript-language-server", "--stdio"],
    "typescriptreact": ["typescript-language-server", "--stdio"],
    "javascriptreact": ["typescript-language-server", "--stdio"],
    "go": ["gopls"],
    "rust": ["rust-analyzer"],
}


def language_for(path: str) -> str:
    return _LANG_BY_EXT.get(os.path.splitext(path)[1].lower(), "")


def server_command(lang: str) -> list[str] | None:
    raw = os.environ.get("AGENT_LSP_SERVERS")
    if raw:
        try:
            cfg = json.loads(raw)
            if isinstance(cfg, dict) and isinstance(cfg.get(lang), list):
                return [str(x) for x in cfg[lang]]
        except Exception:  # noqa: BLE001
            logger.warning("AGENT_LSP_SERVERS 解析失败(忽略): %s", raw[:80])
    return _DEFAULT_SERVERS.get(lang)


def encode_message(obj: dict) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


def _file_uri(path: str) -> str:
    return "file:///" + os.path.abspath(path).replace("\\", "/").lstrip("/")


class LspClient:
    """单个语言服务器连接（stdio）。"""

    def __init__(self, command: list[str], root: str):
        self.command = command
        self.root = os.path.abspath(root)
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._diags: dict[str, list] = {}
        self._diag_events: dict[str, asyncio.Event] = {}
        self._open: set[str] = set()
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> bool:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.root,
            )
        except (FileNotFoundError, OSError) as e:
            logger.info("LSP 服务器启动失败 %s: %s", self.command[0], e)
            return False
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await self._request("initialize", {
                "processId": os.getpid(),
                "rootUri": _file_uri(self.root),
                "capabilities": {"textDocument": {"publishDiagnostics": {}}},
                "workspaceFolders": [{"uri": _file_uri(self.root), "name": os.path.basename(self.root)}],
            }, timeout=20)
            self._notify("initialized", {})
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("LSP initialize 失败 %s: %s", self.command[0], e)
            await self.stop()
            return False

    async def _read_loop(self):
        stream = self.proc.stdout
        try:
            while True:
                header = await stream.readuntil(b"\r\n\r\n")
                length = 0
                for line in header.decode("ascii", errors="replace").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                if length <= 0:
                    continue
                body = await stream.readexactly(length)
                try:
                    msg = json.loads(body.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                self._dispatch(msg)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP reader 结束: %s", e)

    def _dispatch(self, msg: dict):
        if "id" in msg and msg["id"] in self._pending:
            fut = self._pending.pop(msg["id"])
            if not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            return
        if msg.get("method") == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            uri = params.get("uri", "")
            self._diags[uri] = params.get("diagnostics", [])
            ev = self._diag_events.get(uri)
            if ev:
                ev.set()

    def _notify(self, method: str, params: dict):
        if self.proc and self.proc.stdin:
            self.proc.stdin.write(encode_message({"jsonrpc": "2.0", "method": method, "params": params}))

    async def _request(self, method: str, params: dict, timeout: float = 15):
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self.proc.stdin.write(encode_message(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout=timeout)

    async def did_open(self, path: str, text: str) -> str:
        uri = _file_uri(path)
        if uri not in self._open:
            ev = asyncio.Event()
            self._diag_events[uri] = ev
            self._notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": language_for(path), "version": 1, "text": text}})
            self._open.add(uri)
        return uri

    async def wait_diagnostics(self, uri: str, timeout: float = 8) -> list:
        ev = self._diag_events.get(uri)
        if ev:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), timeout=timeout)
        return self._diags.get(uri, [])

    async def hover(self, path: str, line: int, char: int):
        return await self._request("textDocument/hover", {
            "textDocument": {"uri": _file_uri(path)},
            "position": {"line": max(0, line - 1), "character": max(0, char - 1)},
        })

    async def definition(self, path: str, line: int, char: int):
        return await self._request("textDocument/definition", {
            "textDocument": {"uri": _file_uri(path)},
            "position": {"line": max(0, line - 1), "character": max(0, char - 1)},
        })

    async def references(self, path: str, line: int, char: int):
        return await self._request("textDocument/references", {
            "textDocument": {"uri": _file_uri(path)},
            "position": {"line": max(0, line - 1), "character": max(0, char - 1)},
            "context": {"includeDeclaration": True},
        })

    async def rename(self, path: str, line: int, char: int, new_name: str):
        return await self._request("textDocument/rename", {
            "textDocument": {"uri": _file_uri(path)},
            "position": {"line": max(0, line - 1), "character": max(0, char - 1)},
            "newName": new_name,
        }, timeout=30)

    async def stop(self):
        if self._reader_task:
            self._reader_task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
            except ProcessLookupError:
                pass


class LspManager:
    """按 (root, 语言) 复用语言服务器连接。"""

    def __init__(self):
        self._clients: dict[tuple[str, str], LspClient] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._failed: set[tuple[str, str]] = set()

    async def get_client(self, path: str) -> LspClient | None:
        lang = language_for(path)
        if not lang:
            return None
        cmd = server_command(lang)
        if not cmd:
            return None
        # 可执行文件不存在也直接判定不可用（避免反复 spawn 异常）
        exe = cmd[0]
        if os.path.sep not in exe and not shutil.which(exe):
            return None
        key = (os.path.dirname(os.path.abspath(path)), lang)
        if key in self._failed:
            return None
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            client = self._clients.get(key)
            if client is not None:
                return client
            client = LspClient(cmd, key[0])
            if not await client.start():
                self._failed.add(key)
                return None
            self._clients[key] = client
            return client

    async def shutdown(self):
        for client in list(self._clients.values()):
            await client.stop()
        self._clients.clear()
        self._failed.clear()


_MANAGER: LspManager | None = None


def get_lsp_manager() -> LspManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = LspManager()
    return _MANAGER


async def shutdown_all():
    global _MANAGER
    if _MANAGER is not None:
        await _MANAGER.shutdown()
