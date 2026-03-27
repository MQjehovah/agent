import asyncio
import json
import logging
from typing import Dict, Any
from . import BuiltinTool

logger = logging.getLogger("agent.tools")


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
        
        logger.debug(f"执行命令: {command}")
        
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
            
            stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""
            
            result = {
                "success": process.returncode == 0,
                "return_code": process.returncode,
                "stdout": stdout_str[:4000] if len(stdout_str) > 4000 else stdout_str,
                "stderr": stderr_str[:1000] if len(stderr_str) > 1000 else stderr_str
            }
            
            logger.debug(f"命令执行完成: return_code={process.returncode} stdout={stdout_str} stderr={stderr_str}")
            
            return json.dumps(result, ensure_ascii=False)
            
        except Exception as e:
            logger.error(f"命令执行失败: {e}")
            return json.dumps({
                "success": False,
                "error": str(e)
            }, ensure_ascii=False)