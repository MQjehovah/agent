import os
import json
from typing import Dict, Any

from . import BuiltinTool


class FileTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "file_operation"

    @property
    def description(self) -> str:
        return "文件操作工具。支持读取、写入、追加、删除文件内容。可用于查看文件内容、创建新文件、修改现有文件等操作。"

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
                }
            },
            "required": ["operation", "path"]
        }

    async def execute(self, operation: str, path: str, content: str = None, encoding: str = "utf-8") -> str:
        try:
            if operation == "read":
                return self._read_file(path, encoding)
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

    def _read_file(self, path: str, encoding: str) -> str:
        if not os.path.exists(path):
            return json.dumps({"success": False, "error": f"文件不存在: {path}"}, ensure_ascii=False)
        
        if os.path.isdir(path):
            return json.dumps({"success": False, "error": f"路径是目录，不是文件: {path}"}, ensure_ascii=False)
        
        with open(path, "r", encoding=encoding) as f:
            content = f.read()
        
        lines = content.split("\n")
        result = {
            "success": True,
            "path": path,
            "lines": len(lines),
            "size": len(content),
            "content": content
        }
        return json.dumps(result, ensure_ascii=False)

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
        result = {
            "success": True,
            "path": path,
            "exists": exists,
            "is_directory": is_dir,
            "is_file": exists and not is_dir
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
            items.append({
                "name": item,
                "is_directory": os.path.isdir(item_path),
                "is_file": os.path.isfile(item_path),
                "size": os.path.getsize(item_path) if os.path.isfile(item_path) else None
            })
        
        result = {
            "success": True,
            "path": path,
            "count": len(items),
            "items": items
        }
        return json.dumps(result, ensure_ascii=False)