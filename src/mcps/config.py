"""MCP 配置目录读写（统一布局：`<config_dir>/mcps/<name>/server.json`）。

每个 MCP 一个目录，目录名即 server 名；`server.json` 为单条服务端配置（含治理字段）。
替换旧的单文件 `<config_dir>/mcp_servers.json`（数组）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil

logger = logging.getLogger("agent")

SERVER_FILE = "server.json"


def mcps_dir(config_dir: str) -> str:
    """MCP 目录根：<config_dir>/mcps"""
    return os.path.join(config_dir, "mcps")


def load_mcp_dir(mcps_dir_path: str) -> list[dict]:
    """扫描 <mcps>/<name>/server.json，返回配置列表（注入 name=目录名，目录名为准）。"""
    out: list[dict] = []
    if not os.path.isdir(mcps_dir_path):
        return out
    for name in sorted(os.listdir(mcps_dir_path)):
        entry_dir = os.path.join(mcps_dir_path, name)
        server_file = os.path.join(entry_dir, SERVER_FILE)
        if not os.path.isdir(entry_dir) or not os.path.isfile(server_file):
            continue
        try:
            with open(server_file, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"读取 MCP 配置 {server_file} 失败(跳过): {e}")
            continue
        if not isinstance(cfg, dict):
            continue
        cfg["name"] = name
        out.append(cfg)
    return out


def load_mcp_config(config_dir: str) -> list[dict]:
    """读 <config_dir>/mcps/ 下全部 server 配置。"""
    return load_mcp_dir(mcps_dir(config_dir))


def _valid_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and os.sep not in name and "/" not in name


def write_mcp_dir(config_dir: str, entries: list[dict]) -> None:
    """整目录覆盖：每个 entry 按 name 落 `mcps/<name>/server.json`，删除不在集合内的目录。"""
    root = mcps_dir(config_dir)
    os.makedirs(root, exist_ok=True)
    keep: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not _valid_name(name):
            continue
        keep.add(name)
        entry_dir = os.path.join(root, name)
        os.makedirs(entry_dir, exist_ok=True)
        data = {k: v for k, v in entry.items() if k != "name"}
        tmp = os.path.join(entry_dir, f".{SERVER_FILE}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(entry_dir, SERVER_FILE))
    # 清理已移除的 server 目录
    for name in os.listdir(root):
        if name in keep:
            continue
        target = os.path.join(root, name)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)


def delete_mcp_server(config_dir: str, name: str) -> None:
    """删除单个 server 目录。"""
    if not _valid_name(name):
        return
    target = os.path.join(mcps_dir(config_dir), name)
    if os.path.isdir(target):
        shutil.rmtree(target, ignore_errors=True)
