from . import BuiltinTool
from typing import Dict, Any

class BindSessionTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "bind_session"

    @property
    def description(self) -> str:
        return """将插件（飞书/钉钉/webhook）会话绑定到当前 CLI 会话。
绑定后，插件消息会共享 CLI 的对话上下文。
使用场景：用户需要通过飞书继续跟进当前任务时。"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["bind", "unbind"],
                    "description": "bind-绑定插件会话, unbind-解绑"
                }
            },
            "required": ["action"]
        }

    async def execute(self, action: str = "bind") -> str:
        try:
            import sys
            main_mod = sys.modules.get("__main__")
            if not main_mod or not hasattr(main_mod, "BOUND_PLUGIN_SESSION"):
                return "错误: 无法访问主模块会话绑定"
            if action == "bind":
                main_mod.BOUND_PLUGIN_SESSION = getattr(main_mod, "CLI_SESSION_ID", "")
                cid = main_mod.BOUND_PLUGIN_SESSION[:8] if main_mod.BOUND_PLUGIN_SESSION else ""
                return f"已绑定，插件消息将与 CLI 共享上下文 ({cid}...)"
            else:
                main_mod.BOUND_PLUGIN_SESSION = ""
                return "已解绑，插件恢复独立会话"
        except Exception as e:
            return f"操作失败: {e}"
