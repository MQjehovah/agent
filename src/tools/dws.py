"""钉钉 DWS 云端工具：只通过 market runtime 执行，不接触 keychain。

桌面端仍可直接用 shell 跑本机 `dws`；网页端使用这两个工具，由 market
按当前用户 workspace 执行，权限由统一 PermissionChecker 管理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from . import BuiltinTool
from .market_common import market_config
from .user_token import resolve_user_token_or_hint

logger = logging.getLogger("agent.tools.dws")

_DWS_CAPABILITY = "dingtalk-dws"
_DWS_TOOL_PATH = f"/api/runtime/tools/{_DWS_CAPABILITY}/invoke"
_DWS_SCHEMA_PATH = f"/api/capabilities/{_DWS_CAPABILITY}/auth/schema"
_DWS_WORKSPACE_PATH = f"/api/capabilities/{_DWS_CAPABILITY}/auth/workspace"
_DWS_FILES_PATH = f"/api/capabilities/{_DWS_CAPABILITY}/auth/files"


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


async def _auth_token() -> tuple[str, str | None]:
    return await asyncio.to_thread(resolve_user_token_or_hint, audience="market")


def _base_or_error() -> tuple[str, str | None]:
    base, _service_token, _timeout = market_config()
    if not base:
        return "", "能力市场未配置"
    return base.rstrip("/"), None


async def _workspace(base: str, token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            base + _DWS_WORKSPACE_PATH,
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return {"authorized": False, "page_url": "", "error": resp.text[:500]}
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"authorized": False, "page_url": "", "error": "市场返回非 JSON"}


class DwsSchemaTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "dws_schema"

    @property
    def description(self) -> str:
        return (
            "查询钉钉 DWS（工作台 CLI）的命令与参数。网页端使用 DWS 前先用它确认命令路径和参数；"
            "传 path（如 'calendar event list'）查具体命令，传 query 过滤服务名。"
            "只读，不需要用户确认。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "DWS 命令路径，如 calendar event list"},
                "query": {"type": "string", "description": "按服务名过滤，如 日历 / chat"},
            },
        }

    async def execute(self, path: str = "", query: str = "", **kwargs) -> str:
        base, err = _base_or_error()
        if err:
            return _json({"success": False, "error": err})
        token, hint = await _auth_token()
        if hint:
            return _json({"success": False, "error": hint})
        ws = await _workspace(base, token)
        if not ws.get("authorized"):
            return _json(
                {
                    "success": False,
                    "error": "dws_not_authorized",
                    "message": "钉钉 DWS 尚未授权，请先扫码登录",
                    "login_url": ws.get("page_url") or ws.get("login_url") or "",
                }
            )
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    base + _DWS_SCHEMA_PATH,
                    params={"path": path or "", "query": query or ""},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            return _json({"success": False, "error": f"市场请求失败: {type(exc).__name__}: {exc}"})
        if resp.status_code != 200:
            return _json({"success": False, "error": resp.text[:1000]})
        return _json({"success": True, "data": resp.json()})


class DwsTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "dws"

    @property
    def description(self) -> str:
        return (
            "执行钉钉 DWS（工作台 CLI）命令，使用当前用户的钉钉身份。"
            "读操作用 mode=read；普通写用 mode=write；删除/审批等破坏性操作用 mode=destructive。"
            "非 read 必须先向用户说明影响并征得同意，再传 confirm=true。"
            "文件上传/下载用 inputs/outputs（path 为当前工作区本地路径），不要传服务器绝对路径。"
            "网页端不要用 shell 执行 dws；桌面端仍可直接用 shell。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "DWS 子命令路径，如 chat message list、calendar event list",
                },
                "args": {"type": "object", "description": "具名参数，按 dws_schema 返回的 parameters"},
                "positional": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "位置参数（少数命令需要）",
                },
                "mode": {
                    "type": "string",
                    "enum": ["read", "write", "destructive"],
                    "description": "声明风险，必须不低于命令实际 effect",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "非 read 必须为 true；调用前必须先获得用户同意",
                },
                "profile": {"type": "string", "description": "多 profile 时指定 corpId:userId"},
                "inputs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "arg": {"type": "string"},
                            "path": {"type": "string"},
                            "name": {"type": "string"},
                        },
                    },
                    "description": "上传本地文件并替换命令参数：[{arg, path}]",
                },
                "outputs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "arg": {"type": "string"},
                            "name": {"type": "string"},
                            "path": {"type": "string"},
                        },
                    },
                    "description": "命令输出文件落点：[{arg, name, path}]",
                },
            },
            "required": ["command", "mode"],
        }

    def _local_path(self, raw: str) -> Path:
        path = Path(self.resolve_path(raw))
        if not self.is_path_allowed(str(path)):
            raise ValueError(f"文件路径不在当前工作区: {raw}")
        return path

    async def _upload(self, base: str, token: str, item: dict[str, Any]) -> dict[str, str]:
        arg = str(item.get("arg") or "").strip()
        raw = str(item.get("path") or "").strip()
        if not arg or not raw:
            raise ValueError("inputs 必须包含 arg 和 path")
        path = self._local_path(raw)
        if not path.is_file():
            raise ValueError(f"输入文件不存在: {raw}")
        name = str(item.get("name") or path.name)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                base + _DWS_FILES_PATH,
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (name, path.read_bytes(), content_type)},
            )
        if resp.status_code != 200:
            raise ValueError(f"上传文件失败: {resp.text[:500]}")
        data = resp.json()
        return {"arg": arg, "file_id": str(data["file_id"])}

    async def _download_outputs(
        self, base: str, token: str, result: dict[str, Any], outputs: list[dict[str, Any]]
    ) -> None:
        by_arg = {str(o.get("arg") or ""): o for o in outputs if isinstance(o, dict)}
        async with httpx.AsyncClient(timeout=120) as client:
            for row in result.get("output_files") or []:
                file_id = str(row.get("file_id") or "")
                if not file_id:
                    continue
                resp = await client.get(
                    f"{base}{_DWS_FILES_PATH}/{file_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code != 200:
                    row["download_error"] = resp.text[:300]
                    continue
                # 按 name 匹配 outputs；未显式给 path 时落到工作区 .dws/downloads/
                matched = next((o for o in by_arg.values() if str(o.get("name") or "") == row.get("name")), None)
                raw = str((matched or {}).get("path") or "").strip()
                target = self._local_path(raw) if raw else Path(self.workspace) / ".dws" / "downloads" / str(row.get("name") or file_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(resp.content)
                row["local_path"] = str(target)

    async def execute(
        self,
        command: str = "",
        args: dict | None = None,
        positional: list | None = None,
        mode: str = "read",
        confirm: bool = False,
        profile: str = "",
        inputs: list | None = None,
        outputs: list | None = None,
        **kwargs,
    ) -> str:
        command = (command or "").strip()
        mode = (mode or "read").strip().lower()
        if not command:
            return _json({"success": False, "error": "缺少 command"})
        if mode not in ("read", "write", "destructive"):
            return _json({"success": False, "error": "mode 必须是 read / write / destructive"})
        if mode != "read" and not confirm:
            return _json(
                {
                    "success": False,
                    "error": "dws_confirmation_required",
                    "message": "写操作需要用户确认；请先征得用户同意，再传 confirm=true",
                    "effect": mode,
                }
            )
        base, err = _base_or_error()
        if err:
            return _json({"success": False, "error": err})
        token, hint = await _auth_token()
        if hint:
            return _json({"success": False, "error": hint})
        ws = await _workspace(base, token)
        if not ws.get("authorized"):
            return _json(
                {
                    "success": False,
                    "error": "dws_not_authorized",
                    "message": "钉钉 DWS 尚未授权，请先扫码登录",
                    "login_url": ws.get("page_url") or ws.get("login_url") or "",
                }
            )

        uploads: list[dict[str, str]] = []
        try:
            for item in inputs or []:
                if isinstance(item, dict):
                    uploads.append(await self._upload(base, token, item))
        except (ValueError, OSError, httpx.HTTPError) as exc:
            return _json({"success": False, "error": str(exc)})

        params: dict[str, Any] = {
            "command": command,
            "args": args or {},
            "positional": positional or [],
            "mode": mode,
            "confirm": bool(confirm),
        }
        if profile:
            params["profile"] = profile
        if uploads:
            params["inputs"] = uploads
        if outputs:
            params["outputs"] = [
                {k: v for k, v in o.items() if k in ("arg", "name") and v}
                for o in outputs
                if isinstance(o, dict)
            ]

        try:
            async with httpx.AsyncClient(timeout=320) as client:
                resp = await client.post(
                    base + _DWS_TOOL_PATH,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"params": params},
                )
        except httpx.HTTPError as exc:
            return _json({"success": False, "error": f"市场请求失败: {type(exc).__name__}: {exc}"})

        if resp.status_code != 200:
            text = resp.text[:1500]
            try:
                detail = resp.json().get("detail") or resp.json().get("message") or text
            except Exception:  # noqa: BLE001
                detail = text
            return _json({"success": False, "error": detail, "status_code": resp.status_code})
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return _json({"success": False, "error": "市场返回非 JSON"})
        inner = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(inner, dict):
            return _json({"success": False, "error": "市场返回缺少 result"})
        output = inner.get("output") if isinstance(inner.get("output"), dict) else inner
        if output.get("success") and outputs:
            try:
                await self._download_outputs(base, token, output, outputs)
            except (ValueError, OSError, httpx.HTTPError) as exc:
                output["output_download_error"] = str(exc)
        return _json({"success": bool(output.get("success")), "result": output})
