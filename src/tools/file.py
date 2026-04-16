import os
import json
import logging
from typing import Dict, Any

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB 安全限制


class FileTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "file_operation"

    @property
    def description(self) -> str:
        return "文件操作工具。支持读取、写入、追加、删除文件内容，以及检查文件是否存在、列出目录内容。读取支持分段读取和行号显示。"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "write", "append", "delete", "exists", "list"],
                    "description": "操作类型: read-读取文件, write-写入文件(覆盖), append-追加内容, delete-删除文件, exists-检查文件是否存在, list-列出目录内容"
                },
                "path": {
                    "type": "string",
                    "description": "文件或目录的绝对路径"
                },
                "content": {
                    "type": "string",
                    "description": "要写入或追加的内容(仅用于write和append操作)"
                },
                "encoding": {
                    "type": "string",
                    "default": "utf-8",
                    "description": "文件编码，默认utf-8"
                },
                "offset": {
                    "type": "integer",
                    "description": "读取起始行号（0-based），默认从文件开头",
                    "default": 0
                },
                "limit": {
                    "type": "integer",
                    "description": "读取的行数，默认读取全部"
                }
            },
            "required": ["operation", "path"]
        }

    async def execute(self, operation: str, path: str, content: str = None,
                      encoding: str = "utf-8", offset: int = 0, limit: int = None) -> str:
        try:
            if operation == "read":
                return self._read_file(path, encoding, offset, limit)
            elif operation == "write":
                return self._write_file(path, content, encoding)
            elif operation == "append":
                return self._append_file(path, content, encoding)
            elif operation == "delete":
                return self._delete_file(path)
            elif operation == "exists":
                return self._file_exists(path)
            elif operation == "list":
                return self._list_directory(path)
            else:
                return json.dumps({"success": False, "error": f"未知操作: {operation}"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    def _read_file(self, path: str, encoding: str, offset: int = 0, limit: int = None) -> str:
        if not os.path.exists(path):
            return json.dumps({"success": False, "error": f"文件不存在: {path}"}, ensure_ascii=False)

        if os.path.isdir(path):
            return json.dumps({"success": False, "error": f"路径是目录，不是文件: {path}"}, ensure_ascii=False)

        file_size = os.path.getsize(path)
        if file_size > MAX_FILE_SIZE:
            return json.dumps({
                "success": False,
                "error": f"文件过大 ({file_size // 1024 // 1024}MB)，请使用 offset/limit 分段读取"
            }, ensure_ascii=False)

        with open(path, "r", encoding=encoding, errors="replace") as f:
            lines = f.readlines()

        # 分段读取
        if limit is not None:
            end = offset + limit
        else:
            end = len(lines)
        selected_lines = lines[offset:end]

        # 带行号输出
        numbered = "\n".join(
            f"{offset + i + 1:6d}\t{line.rstrip()}"
            for i, line in enumerate(selected_lines)
        )

        showing_end = min(end, len(lines))
        return json.dumps({
            "success": True,
            "path": path,
            "total_lines": len(lines),
            "showing": f"{offset + 1}-{showing_end}",
            "content": numbered
        }, ensure_ascii=False)

    def _write_file(self, path: str, content: str, encoding: str) -> str:
        if content is None:
            return json.dumps({"success": False, "error": "缺少要写入的内容"}, ensure_ascii=False)

        dir_path = os.path.dirname(path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)

        with open(path, "w", encoding=encoding) as f:
            f.write(content)

        result = {
            "success": True,
            "path": path,
            "message": f"文件已成功写入: {path}",
            "size": len(content)
        }
        return json.dumps(result, ensure_ascii=False)

    def _append_file(self, path: str, content: str, encoding: str) -> str:
        if content is None:
            return json.dumps({"success": False, "error": "缺少要追加的内容"}, ensure_ascii=False)

        dir_path = os.path.dirname(path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)

        with open(path, "a", encoding=encoding) as f:
            f.write(content)

        result = {
            "success": True,
            "path": path,
            "message": f"内容已成功追加到: {path}",
            "appended_size": len(content)
        }
        return json.dumps(result, ensure_ascii=False)

    def _delete_file(self, path: str) -> str:
        if not os.path.exists(path):
            return json.dumps({"success": False, "error": f"文件不存在: {path}"}, ensure_ascii=False)

        if os.path.isdir(path):
            import shutil
            shutil.rmtree(path)
            result = {
                "success": True,
                "path": path,
                "message": f"目录已删除: {path}"
            }
        else:
            os.remove(path)
            result = {
                "success": True,
                "path": path,
                "message": f"文件已删除: {path}"
            }
        return json.dumps(result, ensure_ascii=False)

    def _file_exists(self, path: str) -> str:
        exists = os.path.exists(path)
        is_dir = os.path.isdir(path) if exists else False
        size = os.path.getsize(path) if exists and not is_dir else None
        result = {
            "success": True,
            "path": path,
            "exists": exists,
            "is_directory": is_dir,
            "is_file": exists and not is_dir,
            "size": size
        }
        return json.dumps(result, ensure_ascii=False)

    def _list_directory(self, path: str) -> str:
        if not os.path.exists(path):
            return json.dumps({"success": False, "error": f"目录不存在: {path}"}, ensure_ascii=False)

        if not os.path.isdir(path):
            return json.dumps({"success": False, "error": f"路径不是目录: {path}"}, ensure_ascii=False)

        items = []
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            try:
                is_dir = os.path.isdir(item_path)
                items.append({
                    "name": item,
                    "is_directory": is_dir,
                    "is_file": not is_dir,
                    "size": os.path.getsize(item_path) if not is_dir else None
                })
            except (OSError, PermissionError):
                items.append({"name": item, "is_directory": False, "is_file": False, "size": None})

        result = {
            "success": True,
            "path": path,
            "count": len(items),
            "items": items
        }
        return json.dumps(result, ensure_ascii=False)
