import asyncio
import json
import logging
import sys
from typing import Dict, Any
from . import BuiltinTool

logger = logging.getLogger("agent.tools")


def decode_output(data: bytes) -> str:
    if not data:
        return ""
    # 按优先级尝试不同编码
    encodings = ["utf-8", "gbk", "cp936", "latin-1"]
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # 所有编码都失败，使用替换模式
    return data.decode("utf-8", errors="replace")


class ShellTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "shell"
    
    @property
    def description(self) -> str:
        return "在终端执行shell命令，返回命令输出结果"
    
    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的shell命令"
                },
                "timeout": {
                    "type": "integer",
                    "description": "命令执行超时时间（秒），默认30秒",
                    "default": 30
                },
                "cwd": {
                    "type": "string",
                    "description": "命令执行的工作目录，默认当前目录"
                }
            },
            "required": ["command"]
        }
    
    async def execute(self, **kwargs) -> str:
        command = kwargs.get("command", "")
        timeout = kwargs.get("timeout", 30)
        cwd = kwargs.get("cwd")
        
        if not command:
            return json.dumps({"success": False, "error": "命令不能为空"}, ensure_ascii=False)
        
        # logger.debug(f"执行命令: {command}")
        
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.warning(f"命令执行超时: {command}")
                return json.dumps({
                    "success": False,
                    "error": f"命令执行超时（{timeout}秒）"
                }, ensure_ascii=False)
            
            stdout_str = decode_output(stdout)
            stderr_str = decode_output(stderr)
            
            result = {
                "success": process.returncode == 0,
                "return_code": process.returncode,
                "stdout": stdout_str[:4000] if len(stdout_str) > 4000 else stdout_str,
                "stderr": stderr_str[:1000] if len(stderr_str) > 1000 else stderr_str
            }
            
            # logger.debug(f"命令执行完成: return_code={process.returncode} stdout={stdout_str} stderr={stderr_str}")
            
            return json.dumps(result, ensure_ascii=False)
            
        except Exception as e:
            logger.error(f"命令执行失败: {e}")
            return json.dumps({
                "success": False,
                "error": str(e)
            }, ensure_ascii=False)